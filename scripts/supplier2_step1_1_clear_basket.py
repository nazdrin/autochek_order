import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BASKET_URL = (os.getenv("SUP2_BASKET_URL") or "https://crm.dobavki.ua/client/basket/").strip()
STORAGE_STATE_FILE = (os.getenv("SUP2_STORAGE_STATE_FILE") or ".state_supplier2.json").strip()


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
MAX_DELETE = _to_int(os.getenv("SUP2_MAX_DELETE", "50"), 50)


def _is_login_url(url: str) -> bool:
    return "/client/login" in (url or "")


async def _count_rows(page) -> int:
    rows = page.locator("table.os-table tbody tr")
    try:
        return await rows.count()
    except Exception:
        return 0


async def _count_delete_links(page) -> int:
    links = page.locator("div.os-product-delete a.delete")
    try:
        return await links.count()
    except Exception:
        return 0


async def _collect_delete_ids(page) -> list[int]:
    ids = await page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll("a.delete[onclick*='delete_from_basket']"));
            const out = [];
            for (const a of links) {
                const oc = a.getAttribute("onclick") || "";
                const m = oc.match(/delete_from_basket\\((\\d+)\\s*,\\s*(\\d+)\\)/);
                if (m) out.push(parseInt(m[1], 10));
            }
            return Array.from(new Set(out));
        }"""
    )
    if not isinstance(ids, list):
        return []
    return [int(x) for x in ids if isinstance(x, (int, float))]


async def _run() -> tuple[bool, dict]:
    if not STORAGE_STATE_FILE:
        raise RuntimeError("SUP2_STORAGE_STATE_FILE is empty.")

    state_path = Path(STORAGE_STATE_FILE)
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    if not state_path.exists():
        raise RuntimeError(f"Storage state file not found: {state_path}")

    browser = None
    context = None
    page = None
    removed = 0
    delete_count = 0
    current_url = BASKET_URL
    debug_ids: list[int] = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()

            await page.goto(BASKET_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            current_url = page.url or BASKET_URL
            if _is_login_url(page.url or ""):
                raise RuntimeError("Not logged in")

            has_fn = await page.evaluate("typeof delete_from_basket === 'function'")
            if not has_fn:
                raise RuntimeError("delete_from_basket is not available")

            while True:
                ids = await _collect_delete_ids(page)
                debug_ids = ids
                delete_count = len(ids)

                if not ids:
                    break

                if removed >= MAX_DELETE:
                    raise RuntimeError("Too many deletes")

                for basket_id in ids:
                    if removed >= MAX_DELETE:
                        raise RuntimeError("Too many deletes")
                    await page.evaluate("([id]) => delete_from_basket(id, 0)", [basket_id])
                    removed += 1
                    await page.wait_for_timeout(250)

                await page.reload(wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                current_url = page.url or current_url

            current_url = page.url or current_url
            return True, {"ok": True, "removed": removed, "url": current_url}
    except Exception as e:
        if page is not None:
            current_url = page.url or current_url
            try:
                delete_count = len(await _collect_delete_ids(page))
            except Exception:
                pass
        return False, {
            "ok": False,
            "error": str(e),
            "url": current_url,
            "delete_count": delete_count,
            "debug_ids": debug_ids,
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
