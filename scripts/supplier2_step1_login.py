import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


LOGIN_URL = (os.getenv("SUP2_LOGIN_URL") or "https://crm.dobavki.ua/client/login/").strip()
USERNAME = (os.getenv("SUP2_USERNAME") or "").strip()
PASSWORD = (os.getenv("SUP2_PASSWORD") or "").strip()
STORAGE_STATE_FILE = (os.getenv("SUP2_STORAGE_STATE_FILE") or ".state_supplier2.json").strip()
CDP_ENDPOINT = (os.getenv("SUP2_CDP_ENDPOINT") or "").strip()
USER_SELECTOR_ENV = (os.getenv("SUP2_USER_SELECTOR") or "").strip()
PASS_SELECTOR_ENV = (os.getenv("SUP2_PASS_SELECTOR") or "").strip()
SUBMIT_SELECTOR_ENV = (os.getenv("SUP2_SUBMIT_SELECTOR") or "").strip()
SUCCESS_SELECTOR = (os.getenv("SUP2_SUCCESS_SELECTOR") or "").strip()
SUCCESS_URL_CONTAINS = (os.getenv("SUP2_SUCCESS_URL_CONTAINS") or "").strip()


def _to_int(value: str, default: int) -> int:
    try:
        iv = int((value or "").strip())
        if iv > 0:
            return iv
    except Exception:
        pass
    return default


TIMEOUT_MS = _to_int(os.getenv("SUP2_TIMEOUT_MS", "20000"), 20000)
HEADLESS = (os.getenv("SUP2_HEADLESS") or "0").strip() == "1"


def _login_url_path(url: str) -> str:
    return "/client/login" in (url or "")


def _select_all_shortcut() -> str:
    return "Meta+A" if sys.platform == "darwin" else "Control+A"


class LoginFailedError(RuntimeError):
    def __init__(self, message: str, page_error: str = ""):
        super().__init__(message)
        self.page_error = page_error


async def _first_visible(page, selectors: list[str]):
    for sel in selectors:
        loc = page.locator(sel)
        try:
            cnt = await loc.count()
        except Exception:
            cnt = 0
        if cnt <= 0:
            continue
        for i in range(min(cnt, 5)):
            item = loc.nth(i)
            try:
                if await item.is_visible():
                    return item
            except Exception:
                continue
    return None


async def _wait_success(page) -> None:
    if SUCCESS_URL_CONTAINS:
        deadline = asyncio.get_event_loop().time() + (TIMEOUT_MS / 1000.0)
        while asyncio.get_event_loop().time() < deadline:
            cur = page.url or ""
            if (SUCCESS_URL_CONTAINS in cur) and (not _login_url_path(cur)):
                return
            await page.wait_for_timeout(150)
        raise RuntimeError(
            f"Login failed: URL did not match success conditions (contains '{SUCCESS_URL_CONTAINS}' and not /client/login)."
        )
    else:
        deadline = asyncio.get_event_loop().time() + (TIMEOUT_MS / 1000.0)
        while asyncio.get_event_loop().time() < deadline:
            if not _login_url_path(page.url or ""):
                break
            await page.wait_for_timeout(150)

    if SUCCESS_SELECTOR:
        await page.wait_for_selector(SUCCESS_SELECTOR, timeout=TIMEOUT_MS, state="visible")

    if _login_url_path(page.url or ""):
        raise RuntimeError("Login failed: still on /client/login.")


async def _fill_input_with_retry(page, locator, value: str, *, must_equal: bool) -> bool:
    for _ in range(2):
        await locator.wait_for(state="visible", timeout=TIMEOUT_MS)
        await locator.click(timeout=TIMEOUT_MS)
        await page.keyboard.press(_select_all_shortcut())
        await locator.type(value, delay=25, timeout=TIMEOUT_MS)
        await page.wait_for_timeout(120)
        try:
            current = await locator.input_value(timeout=TIMEOUT_MS)
        except Exception:
            current = ""

        if must_equal:
            if current == value:
                return True
        else:
            if bool((current or "").strip()):
                return True
    return False


async def _extract_login_error(page) -> str:
    try:
        exact = page.locator("text=Неправильна комбінація").first
        if await exact.count() > 0 and await exact.is_visible():
            txt = (await exact.inner_text(timeout=1500)).strip()
            if txt:
                return txt
    except Exception:
        pass

    candidates = page.locator(":visible")
    try:
        cnt = await candidates.count()
    except Exception:
        cnt = 0

    for i in range(min(cnt, 300)):
        item = candidates.nth(i)
        try:
            txt = (await item.inner_text(timeout=700)).strip()
        except Exception:
            continue
        if not txt:
            continue
        low = txt.lower()
        if ("неправильна" in low) or ("помил" in low):
            return txt
    return ""


async def _run() -> tuple[bool, str, str, str]:
    if not USERNAME:
        return False, "SUP2_USERNAME is empty.", "", ""
    if not PASSWORD:
        return False, "SUP2_PASSWORD is empty.", "", ""

    user_selectors = [USER_SELECTOR_ENV] if USER_SELECTOR_ENV else ['input[name="login"][type="text"]']
    pass_selectors = [PASS_SELECTOR_ENV] if PASS_SELECTOR_ENV else ['input[name="password"][type="password"]']
    submit_selectors = [SUBMIT_SELECTOR_ENV] if SUBMIT_SELECTOR_ENV else ['input.os-button.js-form-validation[type="submit"][name="ok"]']

    browser = None
    context = None
    page = None
    try:
        async with async_playwright() as p:
            if CDP_ENDPOINT:
                browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
                context = await browser.new_context()
                page = await context.new_page()
            else:
                browser = await p.chromium.launch(headless=HEADLESS)
                context = await browser.new_context()
                page = await context.new_page()

            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)

            user_input = await _first_visible(page, user_selectors)
            if user_input is None:
                raise RuntimeError(f"Login field not found. Tried selectors: {user_selectors}")

            pass_input = await _first_visible(page, pass_selectors)
            if pass_input is None:
                raise RuntimeError(f"Password field not found. Tried selectors: {pass_selectors}")

            submit_btn = await _first_visible(page, submit_selectors)
            if submit_btn is None:
                raise RuntimeError(f"Submit button not found. Tried selectors: {submit_selectors}")

            ok_user = await _fill_input_with_retry(page, user_input, USERNAME, must_equal=True)
            if not ok_user:
                raise RuntimeError("Failed to set username input value.")

            ok_pass = await _fill_input_with_retry(page, pass_input, PASSWORD, must_equal=False)
            if not ok_pass:
                raise RuntimeError("Failed to set password input value.")

            await submit_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
            await submit_btn.click(timeout=TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
            except PWTimeoutError:
                pass

            if _login_url_path(page.url or ""):
                page_error = await _extract_login_error(page)
                raise LoginFailedError("Login failed", page_error=page_error)

            await _wait_success(page)

            await context.storage_state(path=STORAGE_STATE_FILE)
            return True, "", page.url or "", ""
    except LoginFailedError as e:
        return False, "Login failed", (page.url if page is not None else ""), e.page_error
    except PWTimeoutError as e:
        return False, f"Timeout: {e}", (page.url if page is not None else ""), ""
    except Exception as e:
        return False, str(e), (page.url if page is not None else ""), ""
    finally:
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
    ok, err, url, page_error = asyncio.run(_run())
    if ok:
        print(
            json.dumps(
                {"ok": True, "storage_state_file": STORAGE_STATE_FILE, "url": url},
                ensure_ascii=False,
            )
        )
        return 0

    payload = {"ok": False, "error": err, "url": url}
    if page_error:
        payload["page_error"] = page_error
    print(
        json.dumps(payload, ensure_ascii=False)
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
