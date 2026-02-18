import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

PRODUCTS_URL = (os.getenv("SUP2_PRODUCTS_URL") or "https://crm.dobavki.ua/client/product/list/").strip()
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


@dataclass(frozen=True)
class Item:
    sku: str
    qty: int


class ItemParseError(RuntimeError):
    pass


def _normalize_qty(value) -> int:
    try:
        qty = int(str(value).strip())
    except Exception as e:
        raise ItemParseError(f"Invalid qty: {value}") from e
    if qty < 1:
        raise ItemParseError(f"Qty must be >= 1, got: {qty}")
    return qty


def _parse_items() -> list[Item]:
    items_json_raw = (os.getenv("SUP2_ITEMS_JSON") or "").strip()
    if items_json_raw:
        try:
            data = json.loads(items_json_raw)
        except json.JSONDecodeError as e:
            raise ItemParseError(f"SUP2_ITEMS_JSON is not valid JSON: {e}") from e
        if not isinstance(data, list) or not data:
            raise ItemParseError("SUP2_ITEMS_JSON must be a non-empty JSON list.")

        parsed: list[Item] = []
        for idx, entry in enumerate(data):
            if not isinstance(entry, dict):
                raise ItemParseError(f"SUP2_ITEMS_JSON[{idx}] must be an object.")
            sku = str(entry.get("sku", "")).strip()
            if not sku:
                raise ItemParseError(f"SUP2_ITEMS_JSON[{idx}].sku is empty.")
            qty = _normalize_qty(entry.get("qty", 1))
            parsed.append(Item(sku=sku, qty=qty))
        return parsed

    items_raw = (os.getenv("SUP2_ITEMS") or "").strip()
    if items_raw:
        parsed: list[Item] = []
        parts = [p.strip() for p in items_raw.split(",") if p.strip()]
        if not parts:
            raise ItemParseError("SUP2_ITEMS is set but empty after parsing.")

        for idx, chunk in enumerate(parts):
            if ":" in chunk:
                sku_part, qty_part = chunk.split(":", 1)
                sku = sku_part.strip()
                qty = _normalize_qty(qty_part)
            else:
                sku = chunk.strip()
                qty = 1

            if not sku:
                raise ItemParseError(f"SUP2_ITEMS part #{idx + 1} has empty sku.")
            parsed.append(Item(sku=sku, qty=qty))
        return parsed

    raise ItemParseError("SUP2_ITEMS_JSON is required (or SUP2_ITEMS fallback).")


def _is_login_url(url: str) -> bool:
    return "/client/login" in (url or "")


def _select_all_shortcut() -> str:
    return "Meta+A" if sys.platform == "darwin" else "Control+A"


async def _wait_for_row_by_sku(page, sku: str):
    table = page.locator("table.os-table").first
    await table.wait_for(state="visible", timeout=TIMEOUT_MS)

    row = page.locator("table.os-table tbody tr", has=page.locator(f"td:text-is('{sku}')")).first
    try:
        await row.wait_for(state="visible", timeout=TIMEOUT_MS)
        return row
    except Exception:
        pass

    # Fallback: broader text match + exact td check
    candidate_rows = page.locator("table.os-table tbody tr", has_text=sku)
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        try:
            count = await candidate_rows.count()
        except Exception:
            count = 0

        for i in range(min(count, 200)):
            r = candidate_rows.nth(i)
            tds = r.locator("td")
            try:
                td_count = await tds.count()
            except Exception:
                td_count = 0

            for j in range(min(td_count, 20)):
                try:
                    txt = (await tds.nth(j).inner_text(timeout=TIMEOUT_MS)).strip()
                except Exception:
                    continue
                if txt == sku:
                    return r

        await page.wait_for_timeout(120)

    raise RuntimeError(f"SKU row not found in table: {sku}")


async def _set_search_sku(page, sku: str) -> None:
    articul_input = page.locator('input[name="articul"]').first
    await articul_input.wait_for(state="visible", timeout=TIMEOUT_MS)
    await articul_input.click(timeout=TIMEOUT_MS)
    await page.keyboard.press(_select_all_shortcut())
    await page.keyboard.press("Backspace")
    await articul_input.type(sku, delay=20, timeout=TIMEOUT_MS)


async def _click_show(page) -> None:
    show_btn = page.locator('input.os-button[type="submit"][value="Показати"]').first
    if await show_btn.count() == 0:
        show_btn = page.locator('input[type="submit"][value="Показати"]').first
    await show_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
    await show_btn.click(timeout=TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PWTimeoutError:
        # Table updates can be partial/AJAX; continue with explicit row wait.
        pass


async def _set_row_qty(page, row, qty: int) -> None:
    qty_input = row.locator("input[name='count'].js-client-buy-count, input[name='count']").first
    await qty_input.wait_for(state="visible", timeout=TIMEOUT_MS)

    current_raw = ""
    try:
        current_raw = (await qty_input.input_value(timeout=TIMEOUT_MS)).strip()
    except Exception:
        pass

    current_val = None
    if current_raw:
        m = re.search(r"\d+", current_raw)
        if m:
            try:
                current_val = int(m.group(0))
            except Exception:
                current_val = None

    if current_val == qty:
        return

    await qty_input.click(timeout=TIMEOUT_MS)
    await page.keyboard.press(_select_all_shortcut())
    await qty_input.type(str(qty), delay=20, timeout=TIMEOUT_MS)
    await qty_input.evaluate(
        """(el) => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }"""
    )


def _extract_counter_texts(texts: list[str]) -> list[str]:
    return [t.strip() for t in texts if (t or "").strip()]


async def _wait_added_signal(page, before_counters: list[str]) -> None:
    success = page.locator("div.os-success").first
    error = page.locator("div.os-error, .os-alert-error, .alert-error").first
    counter_locators = [
        page.locator("a[href*='cart'] .count, a[href*='cart'] .counter, .js-client-cart-count").first,
        page.locator("a[href*='cart']").first,
    ]

    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PWTimeoutError:
        pass

    deadline = asyncio.get_running_loop().time() + min(6.0, TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await success.count() > 0 and await success.is_visible():
                return
        except Exception:
            pass

        try:
            if await error.count() > 0 and await error.is_visible():
                msg = (await error.inner_text(timeout=TIMEOUT_MS)).strip()
                raise RuntimeError(msg or "Add to cart failed: error message is visible.")
        except RuntimeError:
            raise
        except Exception:
            pass

        current_texts: list[str] = []
        for loc in counter_locators:
            try:
                if await loc.count() > 0:
                    txt = (await loc.inner_text(timeout=TIMEOUT_MS)).strip()
                    if txt:
                        current_texts.append(txt)
            except Exception:
                continue

        now_norm = _extract_counter_texts(current_texts)
        before_norm = _extract_counter_texts(before_counters)
        if now_norm and now_norm != before_norm:
            return

        await page.wait_for_timeout(120)

    # Fallback success: no explicit success marker, but no visible error after networkidle.
    try:
        if await error.count() > 0 and await error.is_visible():
            msg = (await error.inner_text(timeout=TIMEOUT_MS)).strip()
            raise RuntimeError(msg or "Add to cart failed: error message is visible.")
    except RuntimeError:
        raise
    except Exception:
        pass


async def _read_cart_indicators(page) -> list[str]:
    selectors = [
        "a[href*='cart'] .count, a[href*='cart'] .counter, .js-client-cart-count",
        "a[href*='cart']",
    ]
    values: list[str] = []
    for sel in selectors:
        loc = page.locator(sel)
        try:
            cnt = await loc.count()
        except Exception:
            cnt = 0
        for i in range(min(cnt, 5)):
            item = loc.nth(i)
            try:
                txt = (await item.inner_text(timeout=TIMEOUT_MS)).strip()
            except Exception:
                continue
            if txt:
                values.append(txt)
    return values


async def _add_item(page, item: Item) -> None:
    await _set_search_sku(page, item.sku)
    await _click_show(page)

    row = await _wait_for_row_by_sku(page, item.sku)
    await _set_row_qty(page, row, item.qty)

    before_counters = await _read_cart_indicators(page)
    add_btn = row.locator("a.js-client-buy-action").first
    await add_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
    await add_btn.click(timeout=TIMEOUT_MS)

    await _wait_added_signal(page, before_counters)


async def _run() -> tuple[bool, dict]:
    items = _parse_items()

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
    added: list[dict] = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()

            await page.goto(PRODUCTS_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            if _is_login_url(page.url or ""):
                raise RuntimeError("Not authorized: redirected to login page.")

            for item in items:
                try:
                    await _add_item(page, item)
                    added.append({"sku": item.sku, "qty": item.qty})
                except Exception as e:
                    return False, {
                        "ok": False,
                        "error": str(e),
                        "failed_item": {"sku": item.sku, "qty": item.qty},
                        "url": page.url or PRODUCTS_URL,
                    }

            return True, {
                "ok": True,
                "added": added,
                "url": page.url or PRODUCTS_URL,
            }
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
    try:
        ok, payload = asyncio.run(_run())
    except Exception as e:
        payload = {
            "ok": False,
            "error": str(e),
            "url": PRODUCTS_URL,
        }
        ok = False

    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
