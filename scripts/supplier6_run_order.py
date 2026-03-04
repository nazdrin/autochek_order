import asyncio
import argparse
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _to_int(value: str, default: int) -> int:
    try:
        iv = int((value or "").strip())
        return iv if iv > 0 else default
    except Exception:
        return default


def _to_bool(value: str, default: bool) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


SUP6_BASE_URL = (os.getenv("SUP6_BASE_URL") or "https://proteinplus.pro").strip() or "https://proteinplus.pro"
SUP6_USERNAME = (os.getenv("SUP6_USERNAME") or "").strip()
SUP6_PASSWORD = (os.getenv("SUP6_PASSWORD") or "").strip()
SUP6_STORAGE_STATE_FILE = (os.getenv("SUP6_STORAGE_STATE_FILE") or ".state_supplier6.json").strip()
SUP6_HEADLESS = _to_bool(os.getenv("SUP6_HEADLESS", "1"), True)
SUP6_TIMEOUT_MS = _to_int(os.getenv("SUP6_TIMEOUT_MS", "20000"), 20000)
SUP6_STAGE = (os.getenv("SUP6_STAGE") or "login").strip().lower() or "login"
SUP6_FORCE_LOGIN = _to_bool(os.getenv("SUP6_FORCE_LOGIN", "0"), False)
SUP6_KEEP_OPEN_SECONDS = _to_int(os.getenv("SUP6_KEEP_OPEN_SECONDS", "0"), 0)
SUP6_CLEAR_CART_PAUSE_SECONDS = _to_int(os.getenv("SUP6_CLEAR_CART_PAUSE_SECONDS", "20"), 20)
SUPPLIER_RESULT_JSON_PREFIX = "SUPPLIER_RESULT_JSON="
SUP6_MAKE_ORDER_URL = f"{SUP6_BASE_URL.rstrip('/')}/make-order.html"
SUP6_CART_URL = f"{SUP6_BASE_URL.rstrip('/')}/cart.html"


def _state_path() -> Path:
    if not SUP6_STORAGE_STATE_FILE:
        raise RuntimeError("SUP6_STORAGE_STATE_FILE is empty.")
    path = Path(SUP6_STORAGE_STATE_FILE)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _is_state_file_valid(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("cookies"), list) and isinstance(payload.get("origins"), list)


async def _safe_is_visible(locator) -> bool:
    try:
        if await locator.count() <= 0:
            return False
        return await locator.first.is_visible()
    except Exception:
        return False


async def _auth_header_state(page) -> dict:
    login_loc = page.locator(
        "a:has-text('УВІЙТИ'), button:has-text('УВІЙТИ'), [role='button']:has-text('УВІЙТИ'), "
        "a:has-text('Увійти'), button:has-text('Увійти'), [role='button']:has-text('Увійти')"
    )
    account_loc = page.locator(
        "a:has-text('Вийти'), button:has-text('Вийти'), [role='button']:has-text('Вийти'), "
        "a:has-text('Кабінет'), button:has-text('Кабінет'), "
        "a:has-text('Профіль'), button:has-text('Профіль'), "
        "a:has-text('Мій акаунт'), button:has-text('Мій акаунт')"
    )
    return {
        "login_visible": await _safe_is_visible(login_loc),
        "account_visible": await _safe_is_visible(account_loc),
    }


async def _is_logged_in(page) -> tuple[bool, dict]:
    deadline = asyncio.get_running_loop().time() + min(3.0, SUP6_TIMEOUT_MS / 1000.0)
    stable_no_login_hits = 0
    last_state = {"login_visible": None, "account_visible": None}

    while asyncio.get_running_loop().time() < deadline:
        state = await _auth_header_state(page)
        last_state = state
        if state["account_visible"]:
            return True, state
        if state["login_visible"] is False:
            stable_no_login_hits += 1
            if stable_no_login_hits >= 2:
                return True, state
        else:
            stable_no_login_hits = 0
        await page.wait_for_timeout(200)

    # Fallback: if explicit login button is not visible, treat as logged-in.
    return bool(last_state.get("account_visible")) or (last_state.get("login_visible") is False), last_state


async def _open_login_form(page) -> None:
    triggers = [
        page.get_by_role("link", name=re.compile(r"увійти", re.IGNORECASE)).first,
        page.get_by_role("button", name=re.compile(r"увійти", re.IGNORECASE)).first,
        page.locator("a:has-text('УВІЙТИ'), button:has-text('УВІЙТИ'), [role='button']:has-text('УВІЙТИ')").first,
        page.locator("a:has-text('Увійти'), button:has-text('Увійти'), [role='button']:has-text('Увійти')").first,
    ]

    trigger = None
    for loc in triggers:
        try:
            if await loc.count() > 0:
                trigger = loc
                break
        except Exception:
            continue
    if trigger is None:
        raise RuntimeError('Login trigger "УВІЙТИ" not found.')

    click_error = None
    for attempt in (1, 2, 3):
        try:
            await trigger.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
            if attempt >= 2:
                await trigger.scroll_into_view_if_needed(timeout=min(3000, SUP6_TIMEOUT_MS))
            force_click = attempt == 3
            if force_click:
                print('[SUP6] login trigger click: using force=True (fallback).')
            await trigger.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=force_click)
            return
        except Exception as e:
            click_error = e
            await page.wait_for_timeout(220)

    raise RuntimeError(f'Could not click "УВІЙТИ": {click_error}')


async def _ensure_remember_me_checked(page) -> None:
    form = page.locator("form#login-form").first
    checkbox_candidates = [
        form.locator("input#modlgn-remember").first,
        form.locator("input[type='checkbox'][name='remember']").first,
        form.locator("input[type='checkbox'][name*='remember']").first,
        page.get_by_label(re.compile(r"запам[ʼ'`]?ятати мене", re.IGNORECASE)).first,
        page.locator("label:has-text(\"Запам'ятати мене\") input[type='checkbox']").first,
        page.locator("label:has-text('Запам’ятати мене') input[type='checkbox']").first,
    ]

    checkbox = None
    for candidate in checkbox_candidates:
        try:
            if await candidate.count() > 0:
                checkbox = candidate
                break
        except Exception:
            continue

    if checkbox is None:
        raise RuntimeError("Remember me checkbox not found in login form.")

    await checkbox.wait_for(state="attached", timeout=min(5000, SUP6_TIMEOUT_MS))
    if not await checkbox.is_checked():
        label_candidates = [
            page.locator("label[for='modlgn-remember']").first,
            page.locator("label:has-text(\"Запам'ятати мене\")").first,
            page.locator("label:has-text('Запам’ятати мене')").first,
        ]
        for label in label_candidates:
            try:
                if await label.count() <= 0:
                    continue
                await label.wait_for(state="visible", timeout=min(2500, SUP6_TIMEOUT_MS))
                await label.click(timeout=min(2500, SUP6_TIMEOUT_MS))
                if await checkbox.is_checked():
                    break
            except Exception:
                continue

    if not await checkbox.is_checked():
        # Some templates keep checkbox hidden and bind no label click; force-check as fallback.
        await checkbox.check(timeout=min(3000, SUP6_TIMEOUT_MS), force=True)

    if not await checkbox.is_checked():
        raise RuntimeError("Failed to enable remember me checkbox.")
    print("[SUP6] remember me: enabled")


async def _submit_login(page) -> None:
    user_input = page.locator("#modlgn-username").first
    pass_input = page.locator("#modlgn-passwd").first
    form = page.locator("form#login-form, form:has(#modlgn-username):has(#modlgn-passwd)").first
    submit = form.locator(
        "input[type='submit'][value*='Увійти'], button[type='submit']:has-text('Увійти'), button[type='submit']:has-text('УВІЙТИ')"
    ).first
    if await submit.count() <= 0:
        submit = page.locator(
            "input[type='submit'][value*='Увійти'], button:has-text('Увійти'), button:has-text('УВІЙТИ')"
        ).first

    await user_input.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await pass_input.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await user_input.fill(SUP6_USERNAME, timeout=SUP6_TIMEOUT_MS)
    await pass_input.fill(SUP6_PASSWORD, timeout=SUP6_TIMEOUT_MS)
    await _ensure_remember_me_checked(page)
    await submit.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    submit_error = None
    for attempt in (1, 2, 3):
        try:
            if attempt == 3:
                print("[SUP6] login submit: using force=True fallback")
            await submit.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=(attempt == 3))
            return
        except Exception as e:
            submit_error = e
            await page.wait_for_timeout(220)

    try:
        await pass_input.press("Enter", timeout=min(2500, SUP6_TIMEOUT_MS))
        return
    except Exception:
        pass

    raise RuntimeError(f"Could not click submit 'Увійти': {submit_error}")


async def _wait_login_success(page) -> dict:
    user_input = page.locator("#modlgn-username").first
    pass_input = page.locator("#modlgn-passwd").first
    deadline = asyncio.get_running_loop().time() + (SUP6_TIMEOUT_MS / 1000.0)
    last_state = {"login_visible": None, "account_visible": None, "form_visible": None}

    while asyncio.get_running_loop().time() < deadline:
        header_state = await _auth_header_state(page)
        form_visible = False
        try:
            form_visible = (
                (await user_input.count() > 0 and await user_input.is_visible())
                or (await pass_input.count() > 0 and await pass_input.is_visible())
            )
        except Exception:
            form_visible = False

        last_state = {
            "login_visible": header_state["login_visible"],
            "account_visible": header_state["account_visible"],
            "form_visible": form_visible,
        }

        auth_ok = bool(header_state["account_visible"]) or (header_state["login_visible"] is False)
        if auth_ok and not form_visible:
            return last_state

        await page.wait_for_timeout(200)

    raise RuntimeError(f"Login success was not detected: {last_state}")


async def ensure_logged_in(page, context, state_path: Path, *, force_login: bool = False) -> dict:
    await page.goto(SUP6_BASE_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)

    already_logged, checks = await _is_logged_in(page)
    if already_logged and not force_login:
        await context.storage_state(path=str(state_path))
        return {"reused_session": True, "checks": checks}
    if already_logged and force_login:
        print("[SUP6] force login enabled: ignoring active session and submitting credentials")

    if not SUP6_USERNAME or not SUP6_PASSWORD:
        raise RuntimeError("SUP6_USERNAME/SUP6_PASSWORD are required when login is needed.")

    await _open_login_form(page)
    await page.locator("#modlgn-username").first.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await page.locator("#modlgn-passwd").first.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await _submit_login(page)
    success_checks = await _wait_login_success(page)
    await context.storage_state(path=str(state_path))
    return {"reused_session": False, "checks_before": checks, "checks_after": success_checks}


async def _run_login_stage() -> dict:
    state_path = _state_path()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            if _is_state_file_valid(state_path) and not SUP6_FORCE_LOGIN:
                print(f"[SUP6] storage_state found: {state_path}")
                context = await browser.new_context(storage_state=str(state_path))
            else:
                if state_path.exists():
                    if SUP6_FORCE_LOGIN:
                        print(f"[SUP6] force login enabled, ignoring storage_state: {state_path}")
                    else:
                        print(f"[SUP6] storage_state invalid, re-login required: {state_path}")
                context = await browser.new_context()

            page = await context.new_page()
            result = await ensure_logged_in(page, context, state_path, force_login=SUP6_FORCE_LOGIN)
            if result.get("reused_session"):
                print("[SUP6] already logged in via stored state")
            if SUP6_KEEP_OPEN_SECONDS > 0:
                print(f"[SUP6] keep browser open for {SUP6_KEEP_OPEN_SECONDS}s")
                await page.wait_for_timeout(SUP6_KEEP_OPEN_SECONDS * 1000)
            return {
                "ok": True,
                "stage": "login",
                "url": page.url or SUP6_BASE_URL,
                "storage_state": str(state_path),
                "details": result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _open_minicart_panel(page, *, retries: int = 3) -> bool:
    panel = page.locator("div#cart-panel2.panel2, div#cart-panel2, div.cartpanel, .show_cart_link").first
    trigger = page.locator("a#cartpanel").first
    icon = page.locator("a#cartpanel i.fa-shopping-cart, a#cartpanel i").first

    if await _safe_is_visible(panel):
        return True

    for attempt in range(1, retries + 1):
        try:
            if await trigger.count() > 0:
                await trigger.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=(attempt >= 2))
            elif await icon.count() > 0:
                await icon.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=(attempt >= 2))
            else:
                raise RuntimeError("cart trigger not found (a#cartpanel).")

            await panel.wait_for(state="visible", timeout=min(4500, SUP6_TIMEOUT_MS))
            print(f"[SUP6] clear_cart: mini-cart opened (attempt={attempt})")
            return True
        except Exception as e:
            print(f"[SUP6] clear_cart: mini-cart open attempt={attempt} failed: {e}")
            await page.wait_for_timeout(250)

    return bool(await _safe_is_visible(panel))


async def _is_minicart_empty(page) -> bool:
    empty_el = page.locator("p.empty-cart").first
    if await _safe_is_visible(empty_el):
        return True

    panel = page.locator("div#cart-panel2, div.cartpanel").first
    if await panel.count() <= 0:
        return False

    try:
        text = ((await panel.inner_text(timeout=min(2000, SUP6_TIMEOUT_MS))) or "").casefold()
    except Exception:
        return False
    return ("ваш кошик порожній" in text) or ("ваш кошик порожнiй" in text)


async def _go_to_full_cart_from_minicart(page) -> None:
    link = page.locator("a.show_cart.show-cart-link, a.show_cart_link, a[href*='/cart.html']").first
    await link.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    print("[SUP6] clear_cart: opening full cart /cart.html")
    try:
        await asyncio.gather(
            page.wait_for_url(re.compile(r"/cart\.html"), timeout=SUP6_TIMEOUT_MS),
            link.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True),
        )
    except Exception:
        await link.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
        await page.wait_for_url(re.compile(r"/cart\.html"), timeout=SUP6_TIMEOUT_MS)
    await page.wait_for_load_state("domcontentloaded", timeout=min(12000, SUP6_TIMEOUT_MS))


async def clear_cart(page) -> dict:
    max_iterations = 50
    print(f"[SUP6] clear_cart: open start page {SUP6_MAKE_ORDER_URL}")
    await page.goto(SUP6_MAKE_ORDER_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)

    if not await _open_minicart_panel(page, retries=3):
        return {"ok": False, "error": "mini-cart panel did not open"}

    if await _is_minicart_empty(page):
        print("[SUP6] clear_cart: mini-cart is already empty")
        return {"ok": True, "cart_empty": True, "removed": 0}

    try:
        await _go_to_full_cart_from_minicart(page)
    except Exception as e:
        return {"ok": False, "error": f"failed to open /cart.html: {e}"}

    removed = 0
    for iteration in range(1, max_iterations + 1):
        buttons = page.locator("button.vm2-remove_from_cart:visible")
        count = await buttons.count()
        print(f"[SUP6] clear_cart: iteration={iteration} remove_buttons={count}")
        if count <= 0:
            print(f"[SUP6] clear_cart: done, removed={removed}")
            break

        btn = buttons.first
        try:
            await asyncio.gather(
                page.wait_for_load_state("networkidle", timeout=min(15000, SUP6_TIMEOUT_MS)),
                btn.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True),
            )
        except Exception:
            try:
                await btn.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
            except Exception as e:
                return {"ok": False, "error": f"delete click failed on iteration {iteration}: {e}"}
            try:
                await page.wait_for_load_state("networkidle", timeout=min(15000, SUP6_TIMEOUT_MS))
            except Exception:
                pass

        changed = False
        deadline = asyncio.get_running_loop().time() + min(5.0, SUP6_TIMEOUT_MS / 1000.0)
        while asyncio.get_running_loop().time() < deadline:
            now_count = await page.locator("button.vm2-remove_from_cart:visible").count()
            if now_count < count:
                changed = True
                removed += 1
                break
            await page.wait_for_timeout(120)

        if not changed:
            return {"ok": False, "error": f"cart did not update after delete (iteration={iteration})"}
    else:
        return {"ok": False, "error": f"max iterations exceeded ({max_iterations})"}

    final_buttons = await page.locator("button.vm2-remove_from_cart:visible").count()
    if final_buttons > 0:
        return {"ok": False, "error": "remove buttons still present after clear loop"}

    # Optional post-check on mini-cart.
    try:
        await page.goto(SUP6_MAKE_ORDER_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
        if await _open_minicart_panel(page, retries=2):
            if await _is_minicart_empty(page):
                return {"ok": True, "cart_empty": True, "removed": removed}
    except Exception:
        pass
    return {"ok": True, "cart_empty": True, "removed": removed}


async def _run_clear_cart_stage(*, pause_seconds: int = 0) -> dict:
    state_path = _state_path()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            if _is_state_file_valid(state_path):
                print(f"[SUP6] storage_state found: {state_path}")
                context = await browser.new_context(storage_state=str(state_path))
            else:
                context = await browser.new_context()

            page = await context.new_page()
            result = await clear_cart(page)
            if pause_seconds > 0:
                print(f"[SUP6] clear_cart: keep browser open for {pause_seconds}s")
                await page.wait_for_timeout(pause_seconds * 1000)
            return {
                "stage": "clear_cart",
                "url": page.url or SUP6_CART_URL,
                "storage_state": str(state_path),
                **result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _run() -> dict:
    if SUP6_STAGE == "login":
        return await _run_login_stage()
    if SUP6_STAGE == "clear_cart":
        return await _run_clear_cart_stage()
    raise RuntimeError(f"Unsupported SUP6_STAGE={SUP6_STAGE!r}. Expected 'login' or 'clear_cart'.")


async def _amain(clear_cart_only: bool = False) -> int:
    try:
        if clear_cart_only:
            result = await _run_clear_cart_stage(pause_seconds=SUP6_CLEAR_CART_PAUSE_SECONDS)
        else:
            result = await _run()
        stage = str(result.get("stage") or SUP6_STAGE)
        print(f"[SUP6] {stage} ok")
        print(SUPPLIER_RESULT_JSON_PREFIX + json.dumps(result, ensure_ascii=False))
        return 0
    except PWTimeoutError as e:
        stage = "clear_cart" if clear_cart_only else SUP6_STAGE
        print(f"[SUP6] {stage} failed: timeout ({e})")
        print(SUPPLIER_RESULT_JSON_PREFIX + json.dumps({"ok": False, "stage": stage, "error": f"timeout: {e}"}))
        return 1
    except Exception as e:
        stage = "clear_cart" if clear_cart_only else SUP6_STAGE
        print(f"[SUP6] {stage} failed: {e}")
        print(SUPPLIER_RESULT_JSON_PREFIX + json.dumps({"ok": False, "stage": stage, "error": str(e)}))
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Supplier6 (proteinplus.pro) runner")
    parser.add_argument("--clear-cart", action="store_true", help="Run clear cart stage and keep browser open for SUP6_CLEAR_CART_PAUSE_SECONDS")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(clear_cart_only=args.clear_cart)))


if __name__ == "__main__":
    main()
