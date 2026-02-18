import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BASKET_URL = (os.getenv("SUP2_BASKET_URL") or "https://crm.dobavki.ua/client/basket/").strip()
STORAGE_STATE_FILE = (os.getenv("SUP2_STORAGE_STATE_FILE") or ".state_supplier2.json").strip()
CITY_NAME = (os.getenv("SUP2_CITY_NAME") or "Київ").strip()


def _to_int(value: str, default: int) -> int:
    try:
        iv = int((value or "").strip())
        if iv > 0:
            return iv
    except Exception:
        pass
    return default


def _to_bool(value: str, default: bool) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


TIMEOUT_MS = _to_int(os.getenv("SUP2_TIMEOUT_MS", "20000"), 20000)
HEADLESS = _to_bool(os.getenv("SUP2_HEADLESS", "1"), True)
DEBUG_PAUSE_SECONDS = _to_int(os.getenv("SUP2_DEBUG_PAUSE_SECONDS", "0"), 0)


def _is_login_url(url: str) -> bool:
    return "/client/login" in (url or "")


def _select_all_shortcut() -> str:
    return "Meta+A" if sys.platform == "darwin" else "Control+A"


async def _get_dropdown_counts(page) -> dict[str, int]:
    selectors = ["ul.ui-autocomplete li", ".ui-menu-item", "[role='listbox'] [role='option']"]
    counts: dict[str, int] = {}

    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = await loc.count()
        except Exception:
            count = 0
        counts[selector] = count

    return counts


async def _wait_dropdown_visible(page) -> dict[str, int]:
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    latest = await _get_dropdown_counts(page)
    while asyncio.get_running_loop().time() < deadline:
        latest = await _get_dropdown_counts(page)
        if sum(latest.values()) > 0:
            return latest
        await page.wait_for_timeout(120)
    return latest


async def _run() -> tuple[bool, dict]:
    if not STORAGE_STATE_FILE:
        raise RuntimeError("SUP2_STORAGE_STATE_FILE is empty.")
    if not CITY_NAME:
        raise RuntimeError("SUP2_CITY_NAME is empty.")

    state_path = Path(STORAGE_STATE_FILE)
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    if not state_path.exists():
        raise RuntimeError(f"Storage state file not found: {state_path}")

    browser = None
    context = None
    page = None
    city_value = ""
    current_url = BASKET_URL
    dropdown_counts: dict[str, int] = {}
    screenshot_name = "supplier2_step3_city_failed.png"
    attempted_select = False

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()

            await page.goto(BASKET_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            current_url = page.url or BASKET_URL
            if _is_login_url(current_url):
                raise RuntimeError("Not logged in")

            city_input = page.locator("#js-novaposhta-delivery-city").first
            await city_input.wait_for(state="attached", timeout=TIMEOUT_MS)
            await city_input.scroll_into_view_if_needed(timeout=TIMEOUT_MS)
            await city_input.click(force=True, timeout=TIMEOUT_MS)
            await page.keyboard.press(_select_all_shortcut())
            await page.keyboard.press("Backspace")
            await city_input.type(CITY_NAME, delay=40, timeout=TIMEOUT_MS)
            await page.wait_for_timeout(350)
            dropdown_counts = await _wait_dropdown_visible(page)
            attempted_select = True

            options_total = sum(dropdown_counts.values())
            if options_total > 0:
                option = page.locator("ul.ui-autocomplete li", has_text="Київ").first
                if await option.count() == 0:
                    option = page.locator(".ui-menu-item", has_text="Київ").first
                if await option.count() == 0:
                    option = page.locator("[role='option']", has_text="Київ").first

                if await option.count() > 0:
                    await option.click(force=True, timeout=TIMEOUT_MS)
                else:
                    await city_input.press("ArrowDown")
                    await city_input.press("Enter")
            else:
                raise RuntimeError("Dropdown options did not appear")

            if DEBUG_PAUSE_SECONDS > 0:
                await page.wait_for_timeout(DEBUG_PAUSE_SECONDS * 1000)

            branch_label = page.locator("text=Відділення").first
            try:
                await branch_label.wait_for(timeout=TIMEOUT_MS)
            except Exception:
                city_value = (await city_input.input_value(timeout=TIMEOUT_MS)).strip()
                try:
                    await page.screenshot(path=screenshot_name, full_page=True)
                except Exception:
                    pass
                raise RuntimeError("Branch field 'Відділення' did not appear after city selection")

            city_value = (await city_input.input_value(timeout=TIMEOUT_MS)).strip()
            if CITY_NAME not in city_value:
                try:
                    await page.screenshot(path=screenshot_name, full_page=True)
                except Exception:
                    pass
                raise RuntimeError("City value does not contain 'Київ' after dropdown selection")

            current_url = page.url or current_url
            return True, {"ok": True, "city": city_value, "url": current_url}
    except Exception as e:
        if page is not None:
            current_url = page.url or current_url
            city_input = page.locator("#js-novaposhta-delivery-city").first
            try:
                city_value = (await city_input.input_value(timeout=TIMEOUT_MS)).strip()
            except Exception:
                pass
            if not dropdown_counts:
                try:
                    dropdown_counts = await _get_dropdown_counts(page)
                except Exception:
                    dropdown_counts = {}
            if attempted_select and DEBUG_PAUSE_SECONDS > 0:
                try:
                    await page.wait_for_timeout(DEBUG_PAUSE_SECONDS * 1000)
                except Exception:
                    pass
            try:
                if not Path(screenshot_name).exists():
                    await page.screenshot(path=screenshot_name, full_page=True)
            except Exception:
                pass

        payload = {
            "ok": False,
            "error": str(e),
            "city": city_value,
            "dropdown_counts": dropdown_counts,
            "screenshot": screenshot_name,
            "url": current_url,
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
    ok, payload = asyncio.run(_run())
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
