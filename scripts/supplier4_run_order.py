import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import Dialog
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _to_int(value: str, default: int) -> int:
    try:
        iv = int((value or "").strip())
        return iv if iv >= 0 else default
    except Exception:
        return default


def _to_bool(value: str, default: bool) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


SUP4_BASE_URL = (os.getenv("SUP4_BASE_URL") or "https://monsterlab.com.ua").strip() or "https://monsterlab.com.ua"
SUP4_LOGIN_EMAIL = (os.getenv("SUP4_LOGIN_EMAIL") or "").strip()
SUP4_LOGIN_PASSWORD = (os.getenv("SUP4_LOGIN_PASSWORD") or "").strip()
SUP4_STORAGE_STATE_FILE = (os.getenv("SUP4_STORAGE_STATE_FILE") or ".state_supplier4.json").strip()
SUP4_HEADLESS = _to_bool(os.getenv("SUP4_HEADLESS", "1"), True)
SUP4_TIMEOUT_MS = _to_int(os.getenv("SUP4_TIMEOUT_MS", "20000"), 20000)
SUP4_CLEAR_BASKET = _to_bool(os.getenv("SUP4_CLEAR_BASKET", "1"), True)
SUP4_ITEMS = (os.getenv("SUP4_ITEMS") or "").strip()
SUP4_TTN = (os.getenv("SUP4_TTN") or "").strip()
SUP4_ATTACH_DIR = (os.getenv("SUP4_ATTACH_DIR") or "supplier4_labels").strip()
SUP4_PAUSE_SEC = _to_int(os.getenv("SUP4_PAUSE_SEC", "0"), 0)
SUP4_STAGE = (os.getenv("SUP4_STAGE") or "run").strip().lower() or "run"
SUP4_FORCE_LOGIN = _to_bool(os.getenv("SUP4_FORCE_LOGIN", "0"), False)
SUP4_SKIP_SUBMIT = _to_bool(os.getenv("SUP4_SKIP_SUBMIT", "0"), False)
SUP4_NP_API_KEY = (
    os.getenv("SUP4_NP_API_KEY")
    or os.getenv("NP_API_KEY")
    or os.getenv("BIOTUS_NP_API_KEY")
    or ""
).strip()


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.details = details or {}


@dataclass(frozen=True)
class Sup4Item:
    sku: str
    qty: int


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _select_all_shortcut() -> str:
    return "Meta+A" if sys.platform == "darwin" else "Control+A"


def _state_path() -> Path:
    p = Path(SUP4_STORAGE_STATE_FILE)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _attach_dir_path() -> Path:
    p = Path(SUP4_ATTACH_DIR)
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _parse_qty(raw: str) -> int:
    try:
        qty = int(str(raw).strip())
    except Exception as e:
        raise RuntimeError(f"Invalid qty: {raw}") from e
    if qty < 1:
        raise RuntimeError(f"Qty must be >= 1, got: {qty}")
    return qty


def _parse_items() -> list[Sup4Item]:
    raw = SUP4_ITEMS.strip()
    if not raw:
        raise RuntimeError("SUP4_ITEMS is required (format: SKU1:2,SKU2:1)")
    out: list[Sup4Item] = []
    parts = [p.strip() for p in re.split(r"[;,]", raw) if p.strip()]
    for idx, part in enumerate(parts, start=1):
        if ":" in part:
            sku_raw, qty_raw = part.split(":", 1)
            sku = sku_raw.strip()
            qty = _parse_qty(qty_raw)
        else:
            sku = part.strip()
            qty = 1
        if not sku:
            raise RuntimeError(f"SUP4_ITEMS part #{idx} has empty sku")
        out.append(Sup4Item(sku=sku, qty=qty))
    return out


async def _accept_dialog(dialog: Dialog) -> None:
    try:
        await dialog.accept()
    except Exception:
        try:
            await dialog.dismiss()
        except Exception:
            pass


async def _best_effort_close_popups(page) -> None:
    sels = [
        ".popup-close",
        ".Modal-close",
        "#modal-overlay + section .popup-close",
        "button[aria-label='Close']",
    ]
    for _ in range(2):
        for sel in sels:
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=500, force=True)
                    await page.wait_for_timeout(120)
            except Exception:
                continue


async def _is_logged_in(page) -> bool:
    try:
        if await page.get_by_text(re.compile(r"вийти", re.I)).first.is_visible():
            return True
    except Exception:
        pass
    try:
        if await page.locator("a.userbar__button, .userbar__button").filter(has_text=re.compile(r"вхід", re.I)).first.is_visible():
            return False
    except Exception:
        pass
    try:
        if await page.locator(".userbar, .header__section_user").first.is_visible():
            txt = (await page.locator(".userbar, .header__section_user").first.inner_text(timeout=1000)).casefold()
            return "вхід" not in txt
    except Exception:
        pass
    return False


async def _login(page) -> None:
    stage = "login"
    if not SUP4_LOGIN_EMAIL or not SUP4_LOGIN_PASSWORD:
        raise StageError(stage, "SUP4_LOGIN_EMAIL/SUP4_LOGIN_PASSWORD are required")

    await page.goto(SUP4_BASE_URL, wait_until="domcontentloaded")
    await _best_effort_close_popups(page)

    already = await _is_logged_in(page)
    if already and not SUP4_FORCE_LOGIN:
        print("[SUP4] login ok: already authorized")
        return

    login_btns = [
        "a.userbar__button[data-modal='#sign-in']",
        "a.userbar__button",
        "a:has-text('Вхід')",
    ]
    clicked = False
    for sel in login_btns:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=min(3500, SUP4_TIMEOUT_MS), force=True)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        raise StageError(stage, "Login trigger not found")

    try:
        await page.locator("section#sign-in:visible, section.popup_login:visible, #modal-overlay:visible").first.wait_for(
            state="visible", timeout=min(5000, SUP4_TIMEOUT_MS)
        )
    except Exception:
        # Continue with visible-only form lookup below.
        pass

    email = page.locator("#login_form_id input[name='user[email]']:visible, input[name='user[email]']:visible").first
    passwd = page.locator("#login_form_id input[name='user[pass]']:visible, input[name='user[pass]']:visible").first
    submit = page.locator(
        "#login_form_id .j-submit-auth:visible, "
        "#login_form_id button:has-text('Увійти'):visible, "
        "#login_form_id input[type='submit']:visible, "
        "button:has-text('Увійти'):visible"
    ).first

    try:
        await email.wait_for(state="visible", timeout=min(7000, SUP4_TIMEOUT_MS))
        await email.fill(SUP4_LOGIN_EMAIL)
        await passwd.fill(SUP4_LOGIN_PASSWORD)
        await submit.click(timeout=min(4000, SUP4_TIMEOUT_MS))
    except Exception as e:
        raise StageError(stage, f"Login form interaction failed: {e}") from e

    deadline = asyncio.get_running_loop().time() + (SUP4_TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        await _best_effort_close_popups(page)
        if await _is_logged_in(page):
            print("[SUP4] login ok")
            return
        await page.wait_for_timeout(200)

    raise StageError(stage, "Login verification failed")


async def _open_cart_modal(page) -> None:
    stage = "clear_cart"
    modal = page.locator("section#cart, section.popup__cart").first
    overlay = page.locator("#modal-overlay.overlay, #modal-overlay").first
    click_errors: list[str] = []

    async def _visible() -> bool:
        try:
            return await modal.is_visible()
        except Exception:
            return False

    # If cart modal is already open, do not click basket again.
    # Re-clicking basket while modal is open can produce stray clicks behind overlay.
    if await _visible():
        return

    basket_candidates = [
        "a.basket__link.j-basket-link",
        "a.j-basket-link",
        "a.basket__link",
        ".header [data-icon='basket'] a",
        "a[href='#'].basket__link",
    ]
    for sel in basket_candidates:
        loc = page.locator(sel).first
        try:
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
        except Exception:
            continue

        for mode in ("normal", "force", "js"):
            try:
                if mode == "js":
                    await loc.evaluate("(el) => el.click()")
                elif mode == "force":
                    await loc.click(timeout=min(3000, SUP4_TIMEOUT_MS), force=True)
                else:
                    await loc.click(timeout=min(3000, SUP4_TIMEOUT_MS))
                await page.wait_for_timeout(220)
                if await _visible():
                    return
            except Exception as e:
                click_errors.append(f"{sel}/{mode}: {e}")

    # Fallback: some themes keep modal hidden despite basket click, force-open cart popup.
    try:
        await page.evaluate(
            """() => {
                const ov = document.querySelector('#modal-overlay');
                if (ov) {
                    ov.style.display = 'block';
                    ov.classList.add('overlay');
                }
                const cart = document.querySelector('section#cart');
                if (cart) {
                    cart.style.display = 'block';
                    cart.classList.add('popup');
                    cart.classList.add('__cart');
                }
            }"""
        )
        await page.wait_for_timeout(180)
        if await _visible():
            return
    except Exception as e:
        click_errors.append(f"force-open-js: {e}")

    raise StageError(
        stage,
        "Cart modal not opened",
        {
            "url": page.url,
            "click_errors": click_errors[-6:],
            "cart_attached": await modal.count() > 0,
            "cart_visible": await _visible(),
            "overlay_visible": (await overlay.is_visible()) if await overlay.count() > 0 else False,
        },
    )


async def _cart_rows_count(page) -> int:
    rows = page.locator(
        "section#cart tr[id^='product_'], "
        "section#cart tr:has(td[id^='product_']), "
        "section#cart tr:has(td.cart-cell__remove)"
    )
    try:
        return await rows.count()
    except Exception:
        return 0


async def _is_cart_empty_state(page) -> bool:
    try:
        empty_markers = [
            page.get_by_text(re.compile(r"кошик\\s+порож", re.I)).first,
            page.get_by_text(re.compile(r"корзин[аы]\\s+пуст", re.I)).first,
            page.locator("section#cart .cart-empty, .cart-empty").first,
        ]
        for m in empty_markers:
            try:
                if await m.count() > 0 and await m.is_visible():
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Practical fallback: no removable rows and no qty inputs in cart area.
    try:
        rows = await _cart_rows_count(page)
    except Exception:
        rows = 0
    if rows == 0:
        return True
    try:
        qty_inputs = page.locator(
            "section#cart input.counter-field, section#cart input.j-quantity-p, "
            "tr[id^='product_'] input.counter-field, tr[id^='product_'] input.j-quantity-p"
        )
        return (await qty_inputs.count()) == 0
    except Exception:
        return False


async def _find_cart_remove_button(row):
    preferred = row.locator(
        "a.cart-remove-btn, a.cart-remove, a.j-remove-p, a[href='#'].j-remove-p, "
        ".cart-remove-btn, .j-remove-p, "
        "td.cart-cell__remove [onclick], td.cart-cell__remove a, td.cart-cell__remove button"
    ).first
    try:
        if await preferred.count() > 0:
            return preferred, "preferred_remove_selector"
    except Exception:
        pass

    clickable_with_svg = row.locator("a:has(svg), button:has(svg), [role='button']:has(svg)")
    try:
        cand_count = await clickable_with_svg.count()
    except Exception:
        cand_count = 0
    for i in range(min(cand_count, 10)):
        cand = clickable_with_svg.nth(i)
        try:
            outer = ((await cand.evaluate("(el) => (el.outerHTML || '').toLowerCase()")) or "")[:1000]
        except Exception:
            continue
        if "remove" in outer or "icon-cart-remove" in outer or "cart-remove" in outer:
            return cand, "svg_remove_fallback"

    generic = row.locator(
        "td.cart-cell__remove a[onclick*='remove'], "
        "td.cart-cell__remove button[onclick*='remove'], "
        "a.j-remove-p, .j-remove-p, .cart-remove-btn"
    ).first
    try:
        if await generic.count() > 0:
            return generic, "generic_remove_fallback"
    except Exception:
        pass
    return None, ""


async def _click_remove_with_optional_confirm(page, remove_btn, selector_used: str) -> str:
    short_click_timeout = min(2500, max(900, SUP4_TIMEOUT_MS))

    async def _safe_accept_dialog(dialog, mode_label: str) -> None:
        try:
            msg = (dialog.message or "").strip()
            if msg:
                print(f"[SUP4] clear_cart: dialog via {mode_label}: {msg!r}")
            await dialog.accept()
        except Exception:
            pass

    async def _accept_html_confirm_if_present() -> bool:
        confirm_candidates = [
            page.get_by_text(re.compile(r"впевнені|видалити|confirm", re.I)).first,
            page.locator(".confirm, .modal-confirm, .swal2-popup").first,
        ]
        ok_btn_candidates = [
            page.get_by_role("button", name=re.compile(r"^(ok|ок|так|yes|підтвердити)$", re.I)).first,
            page.locator("button:has-text('OK'), button:has-text('ОК'), button:has-text('Так')").first,
            page.locator("a:has-text('OK'), a:has-text('ОК'), a:has-text('Так')").first,
        ]
        deadline = asyncio.get_running_loop().time() + 1.2
        while asyncio.get_running_loop().time() < deadline:
            visible_confirm = False
            for c in confirm_candidates:
                try:
                    if await c.count() > 0 and await c.is_visible():
                        visible_confirm = True
                        break
                except Exception:
                    continue
            if visible_confirm:
                for btn in ok_btn_candidates:
                    try:
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click(timeout=900, force=True)
                            return True
                    except Exception:
                        continue
            await page.wait_for_timeout(70)
        return False

    for mode, params in [
        ("normal_click", {"force": False, "js": False}),
        ("force_click", {"force": True, "js": False}),
        ("js_click", {"force": False, "js": True}),
    ]:
        loop = asyncio.get_running_loop()
        dialog_future = loop.create_future()

        def _on_dialog(dialog):
            async def _runner():
                try:
                    await _safe_accept_dialog(dialog, f"{mode}/{selector_used}")
                    if not dialog_future.done():
                        dialog_future.set_result(True)
                except Exception as e:
                    if not dialog_future.done():
                        dialog_future.set_exception(e)
            asyncio.create_task(_runner())

        page.on("dialog", _on_dialog)
        try:
            if params["js"]:
                await remove_btn.evaluate("(el) => el.click()")
            else:
                await remove_btn.click(timeout=short_click_timeout, force=params["force"])
            try:
                await asyncio.wait_for(dialog_future, timeout=1.2)
            except asyncio.TimeoutError:
                _ = await _accept_html_confirm_if_present()
            return mode
        except Exception:
            continue
        finally:
            try:
                page.remove_listener("dialog", _on_dialog)
            except Exception:
                pass

    raise RuntimeError("remove click failed across all click modes")


async def _clear_cart(page) -> int:
    stage = "clear_cart"
    max_iters = 50
    removed = 0
    for iteration in range(1, max_iters + 1):
        await _open_cart_modal(page)

        rows = await _cart_rows_count(page)
        if rows <= 0:
            print(f"[SUP4] cart cleared: removed={removed}")
            return removed

        item_rows = page.locator(
            "section#cart tr[id^='product_'], "
            "section#cart tr:has(td[id^='product_']), "
            "section#cart tr:has(td.cart-cell__remove), "
            "tr[id^='product_'], "
            "tr:has(td[id^='product_']), "
            "tr:has(td.cart-cell__remove)"
        )
        row = item_rows.first
        remove_btn, selector_used = await _find_cart_remove_button(row)
        if remove_btn is None:
            raise StageError(stage, "Could not find remove control in cart", {"rows": rows, "url": page.url})

        print(f"[SUP4] clear_cart: iteration={iteration} rows={rows} remove_selector={selector_used}")
        try:
            click_mode = await _click_remove_with_optional_confirm(page, remove_btn, selector_used)
        except Exception as e:
            raise StageError(
                stage,
                f"Failed to click remove control: {e}",
                {"rows": rows, "selector_used": selector_used, "url": page.url},
            ) from e

        changed = False
        deadline = asyncio.get_running_loop().time() + min(3.8, SUP4_TIMEOUT_MS / 1000.0)
        while asyncio.get_running_loop().time() < deadline:
            now_rows = await _cart_rows_count(page)
            if now_rows < rows:
                changed = True
                removed += 1
                break
            if await _is_cart_empty_state(page):
                changed = True
                removed += 1
                break
            await page.wait_for_timeout(120)
        if not changed:
            # one extra re-open/recount for last-row race conditions
            try:
                await _open_cart_modal(page)
            except Exception:
                pass
            if await _is_cart_empty_state(page):
                changed = True
                removed += 1
        if not changed:
            raise StageError(
                stage,
                "Cart row count did not decrease after delete",
                {"rows_before": rows, "selector_used": selector_used, "click_mode": click_mode, "url": page.url},
            )

    raise StageError(stage, "Too many delete iterations", {"max_iters": max_iters, "removed": removed, "url": page.url})


async def _open_search_and_fill(page, sku: str) -> None:
    stage = "add_items"
    head_search = page.locator("input[placeholder*='пошук' i], input[name='q'], .header input[type='search']").first
    multi = page.locator("input.multi-input, .multi-search input[type='text']").first

    try:
        if await multi.count() > 0 and await multi.is_visible():
            target = multi
        else:
            await head_search.wait_for(state="visible", timeout=min(5000, SUP4_TIMEOUT_MS))
            await head_search.click(timeout=min(2500, SUP4_TIMEOUT_MS), force=True)
            await page.wait_for_timeout(120)
            if await multi.count() > 0 and await multi.is_visible():
                target = multi
            else:
                target = head_search

        await target.click(timeout=min(2500, SUP4_TIMEOUT_MS), force=True)
        await page.keyboard.press(_select_all_shortcut())
        await page.keyboard.press("Backspace")
        await target.fill(sku, timeout=min(3000, SUP4_TIMEOUT_MS))
    except Exception as e:
        raise StageError(stage, f"Search fill failed for sku={sku}: {e}") from e


async def _open_product_from_dropdown(page, sku: str) -> None:
    stage = "add_items"
    results = page.locator(".multi-results .multi-item a[href], .multi-results .multi-item, .multi-grid .multi-item a[href]")
    try:
        await results.first.wait_for(state="visible", timeout=min(6000, SUP4_TIMEOUT_MS))
    except Exception as e:
        raise StageError(stage, f"Search dropdown did not appear for sku={sku}: {e}") from e

    count = await results.count()
    chosen = None
    for i in range(min(count, 15)):
        row = results.nth(i)
        try:
            txt = re.sub(r"\s+", " ", (await row.inner_text(timeout=900)) or "").strip().casefold()
            href = (await row.get_attribute("href") or "").strip()
            if sku.casefold() in txt or (href and "/" in href and "monsterlab.com.ua" in href):
                chosen = row
                break
        except Exception:
            continue
    if chosen is None:
        chosen = results.first

    try:
        await chosen.click(timeout=min(3500, SUP4_TIMEOUT_MS), force=True)
    except Exception as e:
        raise StageError(stage, f"Dropdown click failed for sku={sku}: {e}") from e

    try:
        await page.wait_for_url(re.compile(r"monsterlab\.com\.ua/.+"), timeout=min(8000, SUP4_TIMEOUT_MS))
    except Exception:
        pass

    print(f"[SUP4] sku found in dropdown: {sku}")
    print(f"[SUP4] product page opened: {page.url}")


async def _click_buy_on_product(page, sku: str) -> None:
    stage = "add_items"
    sels = [
        "a.j-buy-button",
        "a.btn_special:has-text('Купити')",
        "button:has-text('Купити')",
        "a:has-text('Купити')",
    ]
    for sel in sels:
        btn = page.locator(sel).first
        try:
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=min(3500, SUP4_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(250)
                print(f"[SUP4] buy clicked: sku={sku}")
                return
        except Exception:
            continue
    raise StageError(stage, f"Buy button not found for sku={sku}")


async def _wait_cart_modal(page) -> None:
    stage = "add_items"
    modal = page.locator("section#cart.popup__cart, section#cart").first
    try:
        await modal.wait_for(state="visible", timeout=min(6000, SUP4_TIMEOUT_MS))
        print("[SUP4] modal opened")
    except Exception as e:
        raise StageError(stage, f"Cart modal did not open: {e}") from e


async def _read_modal_qty(page) -> tuple[Any, int | None]:
    inp = page.locator("section#cart input.counter-field.j-quantity-p, section#cart input.counter-field, section#cart input[data-step]").first
    try:
        await inp.wait_for(state="visible", timeout=min(4000, SUP4_TIMEOUT_MS))
        raw = await inp.input_value(timeout=min(1500, SUP4_TIMEOUT_MS))
        digits = re.sub(r"\D", "", raw or "")
        return inp, (int(digits) if digits else None)
    except Exception:
        return inp, None


async def _set_modal_qty(page, target_qty: int) -> None:
    stage = "add_items"
    inp, current = await _read_modal_qty(page)
    if current == target_qty:
        print(f"[SUP4] qty set: {target_qty}")
        return

    if target_qty < 1:
        raise StageError(stage, f"Invalid target qty={target_qty}")

    try:
        await inp.click(timeout=min(2500, SUP4_TIMEOUT_MS), force=True)
        await page.keyboard.press(_select_all_shortcut())
        await page.keyboard.press("Backspace")
        await inp.fill(str(target_qty), timeout=min(2500, SUP4_TIMEOUT_MS))
        try:
            await inp.dispatch_event("input")
            await inp.dispatch_event("change")
        except Exception:
            pass
        await page.wait_for_timeout(180)
    except Exception:
        pass

    _, current = await _read_modal_qty(page)
    if current != target_qty:
        plus = page.locator("section#cart .counter-btn__plus, section#cart .j-increase-p, section#cart .counter-btn_plus").first
        minus = page.locator("section#cart .counter-btn__minus, section#cart .j-decrease-p, section#cart .counter-btn_minus").first
        for _ in range(25):
            _, cur = await _read_modal_qty(page)
            if cur == target_qty:
                break
            try:
                if cur is None:
                    break
                if cur < target_qty and await plus.count() > 0:
                    await plus.click(timeout=min(2000, SUP4_TIMEOUT_MS), force=True)
                elif cur > target_qty and await minus.count() > 0:
                    await minus.click(timeout=min(2000, SUP4_TIMEOUT_MS), force=True)
                else:
                    break
                await page.wait_for_timeout(130)
            except Exception:
                break

    _, final_qty = await _read_modal_qty(page)
    if final_qty != target_qty:
        raise StageError(stage, f"CART_QTY_VERIFY_FAILED: expected={target_qty} actual={final_qty}")
    print(f"[SUP4] qty set: {target_qty}")


async def _continue_or_checkout(page, *, last_item: bool) -> None:
    stage = "add_items"
    modal = page.locator("section#cart.popup__cart, section#cart").first
    try:
        await modal.wait_for(state="visible", timeout=min(5000, SUP4_TIMEOUT_MS))
    except Exception as e:
        raise StageError(stage, f"Cart modal not visible before continue/checkout: {e}") from e

    if last_item:
        async def _wait_checkout_opened_short() -> bool:
            deadline = asyncio.get_running_loop().time() + 2.8
            while asyncio.get_running_loop().time() < deadline:
                if "/checkout/" in (page.url or "") or "/checkout" in (page.url or ""):
                    return True
                try:
                    if await page.locator("form#checkout-form, section.checkout, .checkout, .checkout-main").count() > 0:
                        return True
                except Exception:
                    pass
                await page.wait_for_timeout(90)
            return "/checkout/" in (page.url or "") or "/checkout" in (page.url or "")

        checkout_links = [
            ("modal checkout text", modal.locator("a:has-text('Оформити замовлення'), button:has-text('Оформити замовлення')").first),
            ("modal checkout href", modal.locator("a[href*='/checkout']").first),
            (
                "page cart popup checkout",
                page.locator(
                    "section#cart a:has-text('Оформити замовлення'), section#cart button:has-text('Оформити замовлення'), "
                    ".popup__cart a:has-text('Оформити замовлення'), .popup__cart button:has-text('Оформити замовлення'), "
                    "section#cart a[href*='/checkout'], .popup__cart a[href*='/checkout']"
                ).first,
            ),
        ]
        for label, loc in checkout_links:
            try:
                if "/checkout/" in (page.url or "") or "/checkout" in (page.url or ""):
                    print("[SUP4] checkout opened")
                    return
                if await loc.count() == 0:
                    continue
                print(f"[SUP4] click checkout from modal via {label}")
                try:
                    async with page.expect_navigation(url=re.compile(r".*/checkout/?"), wait_until="domcontentloaded", timeout=min(12000, SUP4_TIMEOUT_MS)):
                        await loc.click(timeout=min(4000, SUP4_TIMEOUT_MS), force=True)
                except Exception:
                    await loc.click(timeout=min(4000, SUP4_TIMEOUT_MS), force=True)
                    try:
                        await page.wait_for_url(re.compile(r".*/checkout/?"), timeout=min(10000, SUP4_TIMEOUT_MS))
                    except Exception:
                        if not await _wait_checkout_opened_short():
                            raise

                if "/checkout/" in (page.url or "") or "/checkout" in (page.url or ""):
                    print("[SUP4] checkout opened")
                    return
                if await _wait_checkout_opened_short():
                    print("[SUP4] checkout opened")
                    return
            except Exception:
                if "/checkout/" in (page.url or "") or "/checkout" in (page.url or "") or await _wait_checkout_opened_short():
                    print("[SUP4] checkout opened")
                    return
                continue
        # last-resort fallback: direct checkout URL after modal interactions
        try:
            await page.goto(f"{SUP4_BASE_URL.rstrip('/')}/checkout/", wait_until="domcontentloaded")
        except Exception:
            pass
        if "/checkout/" in (page.url or "") or "/checkout" in (page.url or ""):
            print("[SUP4] checkout opened")
            return
        raise StageError(stage, "Could not click checkout button from cart modal")

    sels = [
        "section#cart button.btn_clear",
        "section#cart .btn_clear",
        "section#cart a:has-text('Повернутись до покупок')",
        "section#cart button:has-text('Повернутись до покупок')",
        "section#cart .popup-close",
    ]
    for sel in sels:
        b = page.locator(sel).first
        try:
            if await b.count() > 0 and await b.is_visible():
                await b.click(timeout=min(3500, SUP4_TIMEOUT_MS), force=True)
                deadline = asyncio.get_running_loop().time() + 2.0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        if not await modal.is_visible():
                            return
                    except Exception:
                        return
                    await page.wait_for_timeout(90)
                await _best_effort_close_popups(page)
                return
        except Exception:
            continue
    raise StageError(stage, "Could not click return-to-shopping button")


async def _add_items(page, items: list[Sup4Item]) -> None:
    for idx, item in enumerate(items):
        await page.goto(SUP4_BASE_URL, wait_until="domcontentloaded")
        await _best_effort_close_popups(page)
        await _open_search_and_fill(page, item.sku)
        await _open_product_from_dropdown(page, item.sku)
        await _click_buy_on_product(page, item.sku)
        await _wait_cart_modal(page)
        await _set_modal_qty(page, item.qty)
        await _continue_or_checkout(page, last_item=(idx == len(items) - 1))


async def _ensure_checkout(page) -> None:
    stage = "checkout_ttn"
    if "/checkout" not in (page.url or ""):
        await page.goto(f"{SUP4_BASE_URL.rstrip('/')}/checkout/", wait_until="domcontentloaded")
    if "/checkout" not in (page.url or ""):
        raise StageError(stage, "Did not reach checkout", {"url": page.url})


async def _ensure_own_ttn_selected(page) -> bool:
    stage = "checkout_ttn"
    own_radio_candidates = [
        page.locator("input[type='radio'][value='own_ttn']").first,
        page.locator("input[type='radio'][value='own_ttn' i]").first,
        page.locator("input[type='radio'][name*='recipient_person' i][value*='own' i]").first,
        page.locator("input[type='radio'][name*='recipient' i][value*='ttn' i]").first,
    ]
    own_radio = None
    for cand in own_radio_candidates:
        try:
            if await cand.count() > 0:
                own_radio = cand
                break
        except Exception:
            continue
    if own_radio is None:
        raise StageError(stage, "Own TTN radio not found")

    try:
        await own_radio.wait_for(state="attached", timeout=min(5000, SUP4_TIMEOUT_MS))
    except Exception:
        raise StageError(stage, "Own TTN radio not found")

    try:
        if await own_radio.is_checked():
            return True
    except Exception:
        pass

    candidates = [
        page.locator("label.recipient-person__item", has_text=re.compile(r"своя\s+наклад", re.I)).first,
        page.locator("label:has-text('Своя накладна'), label:has-text('своя накладна')").first,
        page.get_by_text(re.compile(r"своя\s+наклад", re.I)).first,
        own_radio,
    ]
    for loc in candidates:
        try:
            if await loc.count() > 0:
                try:
                    await loc.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass
                await loc.click(timeout=min(3000, SUP4_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(220)
                if await own_radio.is_checked():
                    return True
        except Exception:
            continue

    if await own_radio.is_checked():
        return True
    raise StageError(stage, "Own TTN option not selectable")


async def _get_ttn_input(page):
    selectors = [
        "dt.form-head:has-text('Вказати номер накладної') >> xpath=following-sibling::dd[1]//input",
        "input[name*='ttNumber']",
        "input[name*='deliveryInfo.ttNumber']",
        "input[name*='deliveryInfo'][name*='tt']",
        "input[name*='ttn' i]",
        "input[id*='ttn' i]",
        "dd.form-item__wide input.field",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                return loc, sel
        except Exception:
            continue
    return None, ""


async def _fill_ttn(page, ttn: str) -> None:
    stage = "checkout_ttn"
    selectors_tried: list[str] = []

    # Checkout block may re-render after selecting recipient type.
    try:
        await page.locator("section.checkout-step[data-component='Delivery'], .checkout-container, form#checkout-form").first.wait_for(
            state="visible", timeout=min(6000, SUP4_TIMEOUT_MS)
        )
    except Exception:
        pass

    inp = None
    sel = ""
    deadline = asyncio.get_running_loop().time() + min(6.0, SUP4_TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        got, used = await _get_ttn_input(page)
        if used:
            selectors_tried.append(used)
        if got is not None:
            try:
                await got.wait_for(state="visible", timeout=min(1000, SUP4_TIMEOUT_MS))
                inp = got
                sel = used
                break
            except Exception:
                pass
        await page.wait_for_timeout(140)
    if inp is None:
        raise StageError(stage, "TTN input not found", {"selectors_tried": selectors_tried, "url": page.url})

    try:
        await inp.wait_for(state="visible", timeout=min(5000, SUP4_TIMEOUT_MS))
        await inp.click(timeout=min(3000, SUP4_TIMEOUT_MS), force=True)
        await page.keyboard.press(_select_all_shortcut())
        await page.keyboard.press("Backspace")
        await inp.fill(ttn, timeout=min(3000, SUP4_TIMEOUT_MS))
        await inp.dispatch_event("input")
        await inp.dispatch_event("change")
        await page.wait_for_timeout(220)
        current = await inp.input_value(timeout=min(1500, SUP4_TIMEOUT_MS))
    except Exception as e:
        raise StageError(stage, f"TTN fill failed via {sel}: {e}", {"selectors_tried": selectors_tried}) from e

    if _digits_only(ttn) not in _digits_only(current):
        raise StageError(stage, "TTN value was not saved in input", {"current_value": str(current), "selectors_tried": selectors_tried})
    print(f"[SUP4] ttn filled: {ttn}")


def _pick_label_file(ttn: str) -> Path:
    stage = "attach_invoice_label"
    attach_dir = _attach_dir_path()
    exts = {".pdf", ".png", ".jpg", ".jpeg"}
    files = [p for p in attach_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not files:
        raise StageError(stage, f"No files in SUP4_ATTACH_DIR={attach_dir}")

    needle = _digits_only(ttn)
    if needle:
        matched = [p for p in files if needle in _digits_only(p.name)]
        if matched:
            matched.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return matched[0]

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def _download_np_label_sup4(folder: Path, ttn: str, api_key: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    out_path = folder / f"label-{ttn}.pdf"
    url = (
        "https://my.novaposhta.ua/orders/printMarking100x100/"
        f"orders[]/{ttn}/type/pdf/apiKey/{api_key}/zebra"
    )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=max(10, SUP4_TIMEOUT_MS / 1000)) as resp:
            status = getattr(resp, "status", 200)
            if status and int(status) >= 400:
                raise RuntimeError(f"Nova Poshta API returned status {status}")
            data = resp.read()
            if not data:
                raise RuntimeError("Downloaded PDF is empty")
            out_path.write_bytes(data)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"NP API HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"NP API connection error: {e}") from e

    if not out_path.exists():
        raise RuntimeError("Downloaded PDF file does not exist")
    if out_path.stat().st_size <= 0:
        raise RuntimeError("Downloaded PDF file size is zero")
    return out_path


def _resolve_label_file(ttn: str) -> Path:
    # Prefer direct download by NP API (same behavior as supplier3), fallback to local files.
    if SUP4_NP_API_KEY:
        try:
            p = _download_np_label_sup4(_attach_dir_path(), ttn, SUP4_NP_API_KEY)
            print(f"[SUP4] label downloaded: {p.name}")
            return p
        except Exception as e:
            print(f"[SUP4] label download failed, fallback to local file: {e}")
    return _pick_label_file(ttn)


async def _attach_label_file(page, ttn: str) -> dict:
    stage = "attach_invoice_label"
    fpath = _resolve_label_file(ttn)
    inp = page.locator("input[type='file'][name*='invoiceFileName'], input[type='file'].j-ignore, input[type='file']").first
    try:
        await inp.wait_for(state="attached", timeout=min(6000, SUP4_TIMEOUT_MS))
        await inp.set_input_files(str(fpath))
        await page.wait_for_timeout(200)
        files_len = await inp.evaluate("el => (el.files && el.files.length) ? el.files.length : 0")
    except Exception as e:
        raise StageError(stage, f"Attach failed: {e}") from e

    if int(files_len or 0) <= 0:
        raise StageError(stage, "Attach verification failed: files length is zero")

    print(f"[SUP4] file attached: {fpath.name}")
    return {"file": str(fpath), "file_name": fpath.name, "files_len": int(files_len)}


async def _submit_checkout(page) -> None:
    stage = "submit_checkout_order"
    async def _ensure_agreement_checked() -> None:
        # Some themes use custom agreement controls near submit text.
        checkbox_candidates = [
            page.locator(".checkout-user-agreement input[type='checkbox']").first,
            page.locator("input[type='checkbox'][name*='agreement' i], input[type='checkbox'][name*='userAgreement' i]").first,
        ]
        for chk in checkbox_candidates:
            try:
                if await chk.count() == 0:
                    continue
                checked = False
                try:
                    checked = await chk.is_checked()
                except Exception:
                    checked = False
                if checked:
                    return
                try:
                    await chk.check(timeout=min(2200, SUP4_TIMEOUT_MS), force=True)
                except Exception:
                    try:
                        await chk.click(timeout=min(2200, SUP4_TIMEOUT_MS), force=True)
                    except Exception:
                        try:
                            await chk.evaluate(
                                """(el) => {
                                    el.checked = true;
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                }"""
                            )
                        except Exception:
                            pass
                await page.wait_for_timeout(120)
                return
            except Exception:
                continue

        # Fallback: click custom agreement wrappers/labels.
        wrapper_candidates = [
            page.locator(".checkout-user-agreement").first,
            page.locator(".checkout-user-agreement__default").first,
            page.locator("label:has-text('Підтверджуючи замовлення')").first,
            page.get_by_text(re.compile(r"Підтверджуючи замовлення", re.I)).first,
        ]
        for w in wrapper_candidates:
            try:
                if await w.count() == 0:
                    continue
                await w.click(timeout=min(2200, SUP4_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(120)
                return
            except Exception:
                continue

    sels = [
        "button.btn.j-submit.btn_submit._special",
        "button.btn.j-submit._special",
        "button.btn.j-submit",
        "button:has-text('Оформити замовлення')",
    ]
    await _ensure_agreement_checked()
    for sel in sels:
        btn = page.locator(sel).first
        try:
            if await btn.count() == 0:
                continue
            try:
                await btn.wait_for(state="visible", timeout=min(3500, SUP4_TIMEOUT_MS))
            except Exception:
                pass
            try:
                await btn.scroll_into_view_if_needed(timeout=1200)
            except Exception:
                pass
            for mode in ("normal", "force", "js"):
                try:
                    await _ensure_agreement_checked()
                    if mode == "js":
                        await btn.evaluate("(el) => el.click()")
                    elif mode == "force":
                        await btn.click(timeout=min(3500, SUP4_TIMEOUT_MS), force=True)
                    else:
                        await btn.click(timeout=min(3500, SUP4_TIMEOUT_MS))
                    # Treat click as successful only if completion signals appear shortly.
                    signal_deadline = asyncio.get_running_loop().time() + 4.8
                    while asyncio.get_running_loop().time() < signal_deadline:
                        url = page.url or ""
                        if "/checkout/complete/" in url:
                            print(f"[SUP4] submitted via {sel}/{mode}")
                            return
                        try:
                            if await page.locator("text=/Ваше\\s+замовлення\\s+отримано/i").count() > 0:
                                print(f"[SUP4] submitted via {sel}/{mode}")
                                return
                        except Exception:
                            pass
                        try:
                            if await page.locator("text=/Замовлення\\s*[№Nº]\\s*\\d+/i").count() > 0:
                                print(f"[SUP4] submitted via {sel}/{mode}")
                                return
                        except Exception:
                            pass
                        await page.wait_for_timeout(150)
                except Exception:
                    continue
        except Exception:
            continue

    # Collect validation hints if submit stayed on checkout.
    error_text = ""
    try:
        candidates = page.locator(".field-error, .error, .form-error, .checkout-error, .invalid-feedback")
        count = await candidates.count()
        msgs = []
        for i in range(min(count, 5)):
            t = re.sub(r"\s+", " ", (await candidates.nth(i).inner_text(timeout=700)) or "").strip()
            if t:
                msgs.append(t)
        if msgs:
            error_text = "; ".join(msgs)
    except Exception:
        pass
    raise StageError(
        stage,
        "Submit click did not lead to checkout complete",
        {"url": page.url, "validation": error_text},
    )


async def _wait_complete_and_parse_number(page) -> str:
    stage = "submit_checkout_order"
    # Wait until either checkout/complete URL or success texts become visible.
    complete_ready = False
    deadline = asyncio.get_running_loop().time() + min(20.0, (SUP4_TIMEOUT_MS + 10000) / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        url = page.url or ""
        if "/checkout/complete/" in url:
            complete_ready = True
            break
        try:
            if await page.locator("text=/Ваше\\s+замовлення\\s+отримано/i").count() > 0:
                complete_ready = True
                break
        except Exception:
            pass
        try:
            if await page.locator("text=/Замовлення\\s*[№Nº]\\s*\\d+/i").count() > 0:
                complete_ready = True
                break
        except Exception:
            pass
        await page.wait_for_timeout(180)

    body_text = ""
    try:
        body_text = re.sub(r"\s+", " ", await page.inner_text("body")).strip()
    except Exception:
        body_text = ""

    # IMPORTANT: parse supplier number from text only, never from URL.
    m = re.search(r"Замовлення\s*[№Nº]\s*(\d{3,})", body_text, flags=re.IGNORECASE)
    if not m:
        try:
            txt = await page.locator("text=/Замовлення\\s*[№Nº]\\s*\\d+/i").first.inner_text(timeout=2500)
            m = re.search(r"(\d{3,})", txt or "")
        except Exception:
            m = None
    if not m:
        try:
            html = await page.content()
        except Exception:
            html = ""
        m = re.search(r"Замовлення\s*[№Nº]\s*(\d{3,})", html or "", flags=re.IGNORECASE)

    if not m:
        raise StageError(
            stage,
            "Could not parse supplier order number on complete page",
            {"url": page.url, "complete_ready": complete_ready},
        )

    number = m.group(1)
    print(f"[SUP4] complete page parsed with order number: {number}")
    return number


async def _checkout_and_submit(page, ttn: str) -> dict:
    await _ensure_checkout(page)
    own_selected = await _ensure_own_ttn_selected(page)
    await _fill_ttn(page, ttn)
    attach_info = await _attach_label_file(page, ttn)
    if SUP4_SKIP_SUBMIT:
        print("[SUP4] submit skipped by SUP4_SKIP_SUBMIT=1")
        if SUP4_PAUSE_SEC > 0:
            await page.wait_for_timeout(SUP4_PAUSE_SEC * 1000)
        return {
            "ok": True,
            "radio_selected": bool(own_selected),
            "ttn_set": True,
            "ttn_verified_before_submit": True,
            "label_attached": True,
            "attach_invoice_label": attach_info,
            "submitted": False,
            "supplier_order_number": "",
        }
    await _submit_checkout(page)
    supplier_number = await _wait_complete_and_parse_number(page)

    if SUP4_PAUSE_SEC > 0:
        await page.wait_for_timeout(SUP4_PAUSE_SEC * 1000)

    return {
        "ok": True,
        "radio_selected": bool(own_selected),
        "ttn_set": True,
        "ttn_verified_before_submit": True,
        "label_attached": True,
        "attach_invoice_label": attach_info,
        "submitted": True,
        "supplier_order_number": supplier_number,
    }


async def _run() -> tuple[bool, dict[str, Any]]:
    if SUP4_STAGE not in {"run", "login", "clear_cart", "add_items", "checkout_ttn"}:
        raise RuntimeError("Unsupported SUP4_STAGE. Expected 'run', 'login', 'clear_cart', 'add_items', 'checkout_ttn'.")
    if SUP4_STAGE in {"run", "checkout_ttn"} and not SUP4_TTN:
        raise RuntimeError("SUP4_TTN is required for this stage")

    items = _parse_items() if SUP4_STAGE in {"run", "add_items"} else []

    storage_path = _state_path()
    browser = None
    context = None
    page = None
    stage = "login"
    result: dict[str, Any] = {"ok": False, "stage": stage, "url": SUP4_BASE_URL}

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=SUP4_HEADLESS)
            if storage_path.exists() and not SUP4_FORCE_LOGIN:
                context = await browser.new_context(storage_state=str(storage_path), viewport={"width": 1600, "height": 1000})
            else:
                context = await browser.new_context(viewport={"width": 1600, "height": 1000})

            context.set_default_timeout(SUP4_TIMEOUT_MS)
            page = await context.new_page()
            page.on("dialog", _accept_dialog)

            stage = "login"
            await _login(page)
            await context.storage_state(path=str(storage_path))

            if SUP4_STAGE == "login":
                result = {"ok": True, "stage": "login", "url": page.url, "debug": {"login": "ok"}}
                return True, result

            if SUP4_STAGE in {"run", "clear_cart"} and SUP4_CLEAR_BASKET:
                stage = "clear_cart"
                removed = await _clear_cart(page)
                print("[SUP4] cart cleared")
                if SUP4_STAGE == "clear_cart":
                    result = {"ok": True, "stage": "clear_cart", "url": page.url, "debug": {"removed": removed}}
                    return True, result

            if SUP4_STAGE in {"run", "add_items"}:
                stage = "add_items"
                await _add_items(page, items)
                if SUP4_STAGE == "add_items":
                    result = {
                        "ok": True,
                        "stage": "add_items",
                        "url": page.url,
                        "debug": {"items": [{"sku": i.sku, "qty": i.qty} for i in items]},
                    }
                    return True, result

            if SUP4_STAGE in {"run", "checkout_ttn"}:
                stage = "checkout_ttn"
                checkout_info = await _checkout_and_submit(page, SUP4_TTN)
                result = {
                    "ok": True,
                    "stage": "run" if SUP4_STAGE == "run" else "checkout_ttn",
                    "url": page.url,
                    "numberSup": checkout_info.get("supplier_order_number"),
                    "supplier_order_number": checkout_info.get("supplier_order_number"),
                    "details": {"checkout_ttn": checkout_info},
                    "debug": {
                        "login_ok": True,
                        "cart_cleared": bool(SUP4_CLEAR_BASKET),
                        "items_count": len(items),
                    },
                }
                return True, result

            raise StageError(stage, "No action executed for stage")

    except Exception as e:
        if isinstance(e, StageError):
            payload = {
                "ok": False,
                "stage": e.stage,
                "error": str(e),
                "url": page.url if page is not None else SUP4_BASE_URL,
                "debug": e.details,
            }
        else:
            payload = {
                "ok": False,
                "stage": stage,
                "error": f"{type(e).__name__}: {e}",
                "url": page.url if page is not None else SUP4_BASE_URL,
            }
        return False, payload
    finally:
        try:
            if page is not None:
                await page.close()
        except Exception:
            pass
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass


def main() -> int:
    try:
        ok, payload = asyncio.run(_run())
    except Exception as e:
        ok = False
        payload = {"ok": False, "stage": "run", "error": str(e), "url": SUP4_BASE_URL}

    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
