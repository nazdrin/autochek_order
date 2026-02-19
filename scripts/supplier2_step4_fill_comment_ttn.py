import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BASKET_URL = "https://crm.dobavki.ua/client/basket/"
STORAGE_STATE_FILE = (os.getenv("SUP2_STORAGE_STATE_FILE") or ".state_supplier2.json").strip()
TTN = (os.getenv("SUP2_TTN") or "").strip()


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


def _select_all_shortcut() -> str:
    return "Meta+A" if sys.platform == "darwin" else "Control+A"


async def _run() -> tuple[bool, dict]:
    if not STORAGE_STATE_FILE:
        raise RuntimeError("SUP2_STORAGE_STATE_FILE is empty.")
    if not TTN:
        return False, {"ok": False, "error": "SUP2_TTN is required", "ttn": "", "url": BASKET_URL}

    state_path = Path(STORAGE_STATE_FILE)
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    if not state_path.exists():
        raise RuntimeError(f"Storage state file not found: {state_path}")

    browser = None
    context = None
    page = None
    current_url = BASKET_URL

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()

            await page.goto(BASKET_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            current_url = page.url or BASKET_URL

            comments = page.locator('textarea[name="comments"]').first
            await comments.wait_for(state="visible", timeout=TIMEOUT_MS)
            await comments.scroll_into_view_if_needed(timeout=TIMEOUT_MS)
            await comments.click(force=True, timeout=TIMEOUT_MS)
            await page.keyboard.press(_select_all_shortcut())
            await page.keyboard.press("Backspace")
            await comments.fill(TTN, timeout=TIMEOUT_MS)
            await comments.press("Tab", timeout=TIMEOUT_MS)
            await page.wait_for_timeout(250)

            current = await comments.input_value(timeout=TIMEOUT_MS)
            if current != TTN:
                raise RuntimeError("TTN not set in comments")

            if DEBUG_PAUSE_SECONDS > 0:
                await page.wait_for_timeout(DEBUG_PAUSE_SECONDS * 1000)

            current_url = page.url or current_url
            return True, {"ok": True, "ttn": TTN, "url": current_url}
    except Exception as e:
        if page is not None:
            current_url = page.url or current_url
            if DEBUG_PAUSE_SECONDS > 0:
                try:
                    await page.wait_for_timeout(DEBUG_PAUSE_SECONDS * 1000)
                except Exception:
                    pass
        return False, {"ok": False, "error": str(e), "ttn": TTN, "url": current_url}
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
