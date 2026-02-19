import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BASKET_URL = "https://crm.dobavki.ua/client/basket/"
STORAGE_STATE_FILE = (os.getenv("SUP2_STORAGE_STATE_FILE") or ".state_supplier2.json").strip()
TTN = (os.getenv("SUP2_TTN") or "").strip()
NP_API_KEY = (
    os.getenv("SUP2_NP_API_KEY")
    or os.getenv("NP_API_KEY")
    or os.getenv("BIOTUS_NP_API_KEY")
    or ""
).strip()
LABELS_DIR = ROOT / "supplier2_labels"


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


def _download_np_label(folder: Path, ttn: str, api_key: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)

    out_path = folder / f"label-{ttn}.pdf"
    url = (
        "https://my.novaposhta.ua/orders/printMarking100x100/"
        f"orders[]/{ttn}/type/pdf/apiKey/{api_key}/zebra"
    )

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=max(10, TIMEOUT_MS / 1000)) as resp:
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
    if ttn not in out_path.name:
        raise RuntimeError("Downloaded file name does not contain TTN")

    return out_path


async def _wait_enabled(locator, timeout_ms: int) -> None:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await locator.is_enabled():
                return
        except Exception:
            pass
        await locator.page.wait_for_timeout(120)
    raise RuntimeError("File input is not enabled")


async def _run() -> tuple[bool, dict]:
    screenshot = "supplier2_attach_label_failed.png"

    browser = None
    context = None
    page = None
    current_url = BASKET_URL
    input_value = ""

    try:
        if not STORAGE_STATE_FILE:
            raise RuntimeError("SUP2_STORAGE_STATE_FILE is empty.")
        if not TTN:
            raise RuntimeError("SUP2_TTN is required")
        if not NP_API_KEY:
            raise RuntimeError("SUP2_NP_API_KEY (or NP_API_KEY) is required.")

        state_path = Path(STORAGE_STATE_FILE)
        if not state_path.is_absolute():
            state_path = ROOT / state_path
        if not state_path.exists():
            raise RuntimeError(f"Storage state file not found: {state_path}")

        pdf_path = _download_np_label(LABELS_DIR, TTN, NP_API_KEY)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()

            await page.goto(BASKET_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            current_url = page.url or BASKET_URL

            file_input = page.locator("input[type='file']").first
            await file_input.wait_for(state="attached", timeout=TIMEOUT_MS)
            try:
                await file_input.wait_for(state="visible", timeout=TIMEOUT_MS)
            except Exception:
                pass
            await _wait_enabled(file_input, TIMEOUT_MS)

            await file_input.set_input_files(str(pdf_path))
            input_value = (await file_input.input_value(timeout=TIMEOUT_MS)).strip()
            if not input_value:
                raise RuntimeError("File input value is empty after set_input_files")

            if DEBUG_PAUSE_SECONDS > 0:
                await page.wait_for_timeout(DEBUG_PAUSE_SECONDS * 1000)

            current_url = page.url or current_url
            return True, {
                "ok": True,
                "ttn": TTN,
                "file": str(pdf_path),
                "input_value": input_value,
                "url": current_url,
            }
    except Exception as e:
        if page is not None:
            current_url = page.url or current_url
            try:
                await page.screenshot(path=screenshot, full_page=True)
            except Exception:
                pass
            if DEBUG_PAUSE_SECONDS > 0:
                try:
                    await page.wait_for_timeout(DEBUG_PAUSE_SECONDS * 1000)
                except Exception:
                    pass
        return False, {
            "ok": False,
            "error": str(e),
            "ttn": TTN,
            "url": current_url,
            "screenshot": screenshot,
        }
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
