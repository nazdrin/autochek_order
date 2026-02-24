import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BASKET_URL = "https://crm.dobavki.ua/client/basket/"
PRODUCTS_URL = "https://crm.dobavki.ua/client/product/list/"
STORAGE_STATE_FILE = (os.getenv("SUP2_STORAGE_STATE_FILE") or ".state_supplier2.json").strip()
TTN = (os.getenv("SUP2_TTN") or "").strip()
NP_API_KEY = (
    os.getenv("SUP2_NP_API_KEY")
    or os.getenv("NP_API_KEY")
    or os.getenv("BIOTUS_NP_API_KEY")
    or ""
).strip()
LABELS_DIR = ROOT / "supplier2_labels"
SUPPLIER_RESULT_JSON_PREFIX = "SUPPLIER_RESULT_JSON="
SUBMIT_CHECKPOINT_FILE = ROOT / ".supplier2_submit_checkpoint.json"


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
CLEAR_BASKET = _to_bool(os.getenv("SUP2_CLEAR_BASKET", "1"), True)
DEBUG_PAUSE_SECONDS = _to_int(os.getenv("SUP2_DEBUG_PAUSE_SECONDS", "0"), 0)
MAX_DELETE = _to_int(os.getenv("SUP2_MAX_DELETE", "50"), 50)
STRICT_AVAILABILITY = _to_bool(os.getenv("SUP2_STRICT_AVAILABILITY", "1"), True)
LABELS_MAX_FILES = _to_int(os.getenv("SUP2_LABELS_MAX_FILES", "50"), 50)
LABELS_MAX_AGE_DAYS = _to_int(os.getenv("SUP2_LABELS_MAX_AGE_DAYS", "7"), 7)
DELETE_LABEL_AFTER_ATTACH = _to_bool(os.getenv("SUP2_DELETE_LABEL_AFTER_ATTACH", "1"), True)


@dataclass(frozen=True)
class Item:
    sku: str
    qty: int


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.details = details or {}


def _write_submit_checkpoint(ttn: str, url: str, submitted: bool, order_number: str = "") -> None:
    payload = {
        "ts": int(time.time()),
        "ttn": str(ttn or ""),
        "url": str(url or ""),
        "submitted": bool(submitted),
        "order_number": str(order_number or ""),
    }
    try:
        tmp = SUBMIT_CHECKPOINT_FILE.with_suffix(SUBMIT_CHECKPOINT_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SUBMIT_CHECKPOINT_FILE)
    except Exception:
        pass


def _parse_availability_value(availability_raw: str) -> int | None:
    # Examples:
    # - "50+" -> 50
    # - "7" -> 7
    # - "Под заказ 2-3 дня" -> None
    low = (availability_raw or "").strip().lower()
    if not low:
        return None

    preorder_markers = ("под заказ", "під замовлення", "під заказ")
    if any(marker in low for marker in preorder_markers):
        return None

    m_plus = re.match(r"^\s*(\d+)\s*\+\s*$", availability_raw or "")
    if m_plus:
        try:
            return int(m_plus.group(1))
        except Exception:
            return None

    m_num = re.match(r"^\s*(\d+)\s*$", availability_raw or "")
    if not m_num:
        return None

    try:
        return int(m_num.group(1))
    except Exception:
        return None


async def _debug_pause_if_needed() -> None:
    if DEBUG_PAUSE_SECONDS > 0:
        await asyncio.sleep(DEBUG_PAUSE_SECONDS)


def _normalize_qty(value) -> int:
    try:
        qty = int(str(value).strip())
    except Exception as e:
        raise RuntimeError(f"Invalid qty: {value}") from e
    if qty < 1:
        raise RuntimeError(f"Qty must be >= 1, got: {qty}")
    return qty


def _parse_items() -> list[Item]:
    items_json_raw = (os.getenv("SUP2_ITEMS_JSON") or "").strip()
    if items_json_raw:
        try:
            data = json.loads(items_json_raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"SUP2_ITEMS_JSON is not valid JSON: {e}") from e
        if not isinstance(data, list) or not data:
            raise RuntimeError("SUP2_ITEMS_JSON must be a non-empty JSON list.")
        out: list[Item] = []
        for idx, entry in enumerate(data):
            if not isinstance(entry, dict):
                raise RuntimeError(f"SUP2_ITEMS_JSON[{idx}] must be an object.")
            sku = str(entry.get("sku", "")).strip()
            if not sku:
                raise RuntimeError(f"SUP2_ITEMS_JSON[{idx}].sku is empty.")
            out.append(Item(sku=sku, qty=_normalize_qty(entry.get("qty", 1))))
        return out

    items_raw = (os.getenv("SUP2_ITEMS") or "").strip()
    if items_raw:
        out: list[Item] = []
        for idx, chunk in enumerate([p.strip() for p in items_raw.split(",") if p.strip()]):
            if ":" in chunk:
                sku_part, qty_part = chunk.split(":", 1)
                sku = sku_part.strip()
                qty = _normalize_qty(qty_part)
            else:
                sku = chunk.strip()
                qty = 1
            if not sku:
                raise RuntimeError(f"SUP2_ITEMS part #{idx + 1} has empty sku.")
            out.append(Item(sku=sku, qty=qty))
        if not out:
            raise RuntimeError("SUP2_ITEMS is set but empty after parsing.")
        return out

    raise RuntimeError("SUP2_ITEMS_JSON is required (or SUP2_ITEMS fallback).")


def _select_all_shortcut() -> str:
    return "Meta+A" if sys.platform == "darwin" else "Control+A"


def _is_login_url(url: str) -> bool:
    return "/client/login" in (url or "")


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


async def _clear_basket(page) -> dict:
    await page.goto(BASKET_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    if _is_login_url(page.url or ""):
        raise StageError("clear_basket", "Not logged in", {})

    has_fn = await page.evaluate("typeof delete_from_basket === 'function'")
    if not has_fn:
        raise StageError("clear_basket", "delete_from_basket is not available", {})

    removed = 0
    while True:
        ids = await _collect_delete_ids(page)
        if not ids:
            break
        if removed >= MAX_DELETE:
            raise StageError("clear_basket", "Too many deletes", {"removed": removed, "ids": ids})
        for basket_id in ids:
            if removed >= MAX_DELETE:
                raise StageError("clear_basket", "Too many deletes", {"removed": removed, "ids": ids})
            await page.evaluate("([id]) => delete_from_basket(id, 0)", [basket_id])
            removed += 1
            await page.wait_for_timeout(250)
        await page.reload(wait_until="domcontentloaded", timeout=TIMEOUT_MS)

    final_ids = await _collect_delete_ids(page)
    if final_ids:
        raise StageError("clear_basket", "Basket is not empty after delete loop", {"remaining_ids": final_ids})
    return {"removed": removed}


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
        pass


async def _wait_for_row_by_sku(page, sku: str):
    row = page.locator("table.os-table tbody tr", has=page.locator(f"td:text-is('{sku}')")).first
    try:
        await row.wait_for(state="visible", timeout=TIMEOUT_MS)
        return row
    except Exception:
        pass

    candidates = page.locator("table.os-table tbody tr", has_text=sku)
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        try:
            count = await candidates.count()
        except Exception:
            count = 0
        for i in range(min(count, 200)):
            r = candidates.nth(i)
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
    return None


async def _set_row_qty(page, row, qty: int) -> None:
    qty_input = row.locator("input[name='count'].js-client-buy-count, input[name='count']").first
    await qty_input.wait_for(state="visible", timeout=TIMEOUT_MS)
    await qty_input.click(timeout=TIMEOUT_MS)
    await page.keyboard.press(_select_all_shortcut())
    await qty_input.type(str(qty), delay=20, timeout=TIMEOUT_MS)
    await qty_input.evaluate(
        """(el) => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }"""
    )


async def _get_availability_col_idx(page) -> int | None:
    header_selectors = [
        "table.os-table thead th",
        "table.os-table thead td",
        "table.os-table th",
        "table.os-table tr:first-child th",
        "table.os-table tr:first-child td",
    ]
    for selector in header_selectors:
        headers = page.locator(selector)
        try:
            count = await headers.count()
        except Exception:
            count = 0
        for i in range(count):
            try:
                txt = (await headers.nth(i).inner_text(timeout=TIMEOUT_MS)).strip().lower()
            except Exception:
                continue
            if "наявн" in txt:
                return i
    return None


async def _read_row_availability(row, availability_col_idx: int | None) -> tuple[str, int | None]:
    availability_raw = ""
    tds = row.locator("td")
    td_count = await tds.count()

    if availability_col_idx is not None and availability_col_idx < td_count:
        availability_raw = (await tds.nth(availability_col_idx).inner_text(timeout=TIMEOUT_MS)).strip()
    else:
        attr_loc = row.locator(
            "td[data-title*='наяв' i], td[data-title*='nalich' i], "
            "td[title*='наяв' i], td[title*='nalich' i]"
        ).first
        if await attr_loc.count() > 0:
            availability_raw = (await attr_loc.inner_text(timeout=TIMEOUT_MS)).strip()
        else:
            for j in range(min(td_count, 20)):
                try:
                    td_text = (await tds.nth(j).inner_text(timeout=TIMEOUT_MS)).strip()
                except Exception:
                    continue
                low = td_text.lower()
                if "наяв" in low or "налич" in low or re.search(r"\b\d+\s*шт\b", low):
                    availability_raw = td_text
                    break

    return availability_raw, _parse_availability_value(availability_raw)


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
            try:
                txt = (await loc.nth(i).inner_text(timeout=TIMEOUT_MS)).strip()
            except Exception:
                continue
            if txt:
                values.append(txt)
    return values


async def _wait_added_signal(page, before_counters: list[str]) -> None:
    success = page.locator("div.os-success").first
    error = page.locator("div.os-error, .os-alert-error, .alert-error").first
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
        current = await _read_cart_indicators(page)
        if current and current != before_counters:
            return
        await page.wait_for_timeout(120)


async def _raise_availability_error(
    page,
    item: Item,
    *,
    error_code: str,
    message: str,
    availability_col_idx: int | None,
    availability_raw: str,
    availability_val: int | None,
) -> None:
    screenshot_name = "supplier2_run_order_availability_failed.png"
    screenshot_path = ""
    try:
        await page.screenshot(path=screenshot_name, full_page=True)
        screenshot_path = screenshot_name
    except Exception:
        pass

    raise StageError(
        "add_items",
        message,
        {
            "error_code": error_code,
            "failed_item": {"sku": item.sku, "qty": item.qty},
            "required_qty": item.qty,
            "availability_col_idx": availability_col_idx,
            "availability_raw": availability_raw,
            "availability_val": availability_val,
            "screenshot": screenshot_path,
        },
    )


async def _add_items(page, items: list[Item]) -> list[dict]:
    await page.goto(PRODUCTS_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    if _is_login_url(page.url or ""):
        raise StageError("add_items", "Not authorized: redirected to login page.", {})

    added: list[dict] = []
    for item in items:
        await _set_search_sku(page, item.sku)
        await _click_show(page)
        availability_col_idx = await _get_availability_col_idx(page)
        row = await _wait_for_row_by_sku(page, item.sku)
        if row is None:
            raise StageError(
                "add_items",
                f"NOT_FOUND: sku={item.sku}, qty={item.qty}",
                {"error_code": "NOT_FOUND", "failed_item": {"sku": item.sku, "qty": item.qty}},
            )
        availability_raw, _ = await _read_row_availability(row, availability_col_idx)
        availability_val = _parse_availability_value(availability_raw)
        if availability_val is None:
            msg = (
                f"OUT_OF_STOCK: sku={item.sku}, qty={item.qty}, "
                f"availability_raw={availability_raw!r}, availability_val=None"
            )
            if STRICT_AVAILABILITY:
                await _raise_availability_error(
                    page,
                    item,
                    error_code="OUT_OF_STOCK",
                    message=msg,
                    availability_col_idx=availability_col_idx,
                    availability_raw=availability_raw,
                    availability_val=None,
                )
        elif availability_val < item.qty:
            msg = (
                f"INSUFFICIENT_STOCK: sku={item.sku}, qty={item.qty}, "
                f"availability_raw={availability_raw!r}, availability_val={availability_val}"
            )
            if STRICT_AVAILABILITY:
                await _raise_availability_error(
                    page,
                    item,
                    error_code="INSUFFICIENT_STOCK",
                    message=msg,
                    availability_col_idx=availability_col_idx,
                    availability_raw=availability_raw,
                    availability_val=availability_val,
                )

        await _set_row_qty(page, row, item.qty)
        before_counters = await _read_cart_indicators(page)
        add_btn = row.locator("a.js-client-buy-action").first
        await add_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
        await add_btn.click(timeout=TIMEOUT_MS)
        await _wait_added_signal(page, before_counters)
        availability_issue = None
        if availability_val is None:
            availability_issue = "OUT_OF_STOCK_NON_STRICT"
        elif availability_val < item.qty:
            availability_issue = "INSUFFICIENT_STOCK_NON_STRICT"
        added.append(
            {
                "sku": item.sku,
                "qty": item.qty,
                "availability_raw": availability_raw,
                "available_qty": availability_val,
                "availability_col_idx": availability_col_idx,
                "availability_issue": availability_issue,
            }
        )
    return added


async def _get_dropdown_counts(page) -> dict[str, int]:
    selectors = ["ul.ui-autocomplete li", ".ui-menu-item", "[role='listbox'] [role='option']"]
    out: dict[str, int] = {}
    for sel in selectors:
        loc = page.locator(sel)
        try:
            out[sel] = await loc.count()
        except Exception:
            out[sel] = 0
    return out


async def _wait_dropdown_visible(page) -> dict[str, int]:
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    latest = await _get_dropdown_counts(page)
    while asyncio.get_running_loop().time() < deadline:
        latest = await _get_dropdown_counts(page)
        if sum(latest.values()) > 0:
            return latest
        await page.wait_for_timeout(120)
    return latest


async def _select_city_kyiv(page) -> None:
    await page.goto(BASKET_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    if _is_login_url(page.url or ""):
        raise StageError("select_city", "Not logged in", {})

    city_input = page.locator("#js-novaposhta-delivery-city").first
    await city_input.wait_for(state="attached", timeout=TIMEOUT_MS)
    await city_input.scroll_into_view_if_needed(timeout=TIMEOUT_MS)
    await city_input.click(force=True, timeout=TIMEOUT_MS)
    await page.keyboard.press(_select_all_shortcut())
    await page.keyboard.press("Backspace")
    await city_input.type("Київ", delay=40, timeout=TIMEOUT_MS)
    await page.wait_for_timeout(350)

    dropdown_counts = await _wait_dropdown_visible(page)
    if sum(dropdown_counts.values()) <= 0:
        raise StageError("select_city", "Dropdown options did not appear", {"dropdown_counts": dropdown_counts})

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

    branch_label = page.locator("text=Відділення").first
    try:
        await branch_label.wait_for(timeout=TIMEOUT_MS)
    except Exception as e:
        city_value = (await city_input.input_value(timeout=TIMEOUT_MS)).strip()
        raise StageError(
            "select_city",
            "Branch field 'Відділення' did not appear after city selection",
            {"city_value": city_value},
        ) from e


async def _fill_comment(page, ttn: str) -> None:
    comments = page.locator('textarea[name="comments"]').first
    await comments.wait_for(state="visible", timeout=TIMEOUT_MS)
    await comments.scroll_into_view_if_needed(timeout=TIMEOUT_MS)
    await comments.click(force=True, timeout=TIMEOUT_MS)
    await page.keyboard.press(_select_all_shortcut())
    await page.keyboard.press("Backspace")
    await comments.fill(ttn, timeout=TIMEOUT_MS)
    await comments.press("Tab", timeout=TIMEOUT_MS)
    await page.wait_for_timeout(250)
    current = await comments.input_value(timeout=TIMEOUT_MS)
    if current != ttn:
        raise StageError("fill_comment", "TTN not set in comments", {"ttn": ttn, "value": current})


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
    _cleanup_labels_dir(folder, keep_names={out_path.name})
    return out_path


def _cleanup_labels_dir(folder: Path, keep_names: set[str] | None = None) -> None:
    keep_names = keep_names or set()
    if not folder.exists():
        return

    files: list[Path] = []
    for p in folder.glob("label-*.pdf"):
        if p.is_file():
            files.append(p)
    if not files:
        return

    now = time.time()
    deleted = 0

    if LABELS_MAX_AGE_DAYS > 0:
        max_age_sec = LABELS_MAX_AGE_DAYS * 24 * 60 * 60
        for p in files:
            if p.name in keep_names:
                continue
            try:
                if now - p.stat().st_mtime > max_age_sec:
                    p.unlink(missing_ok=True)
                    deleted += 1
            except Exception:
                continue

    if LABELS_MAX_FILES > 0:
        remaining = [p for p in files if p.exists() and p.name not in keep_names]
        remaining.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        for p in remaining[LABELS_MAX_FILES:]:
            try:
                p.unlink(missing_ok=True)
                deleted += 1
            except Exception:
                continue

    if deleted:
        print(f"[SUP2] Cleaned old labels: {deleted}")


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


async def _pick_file_input(page):
    visible = page.locator("input[type='file']:visible")
    if await visible.count() > 0:
        return visible.first
    any_input = page.locator("input[type='file']")
    if await any_input.count() > 0:
        return any_input.first
    raise RuntimeError("No input[type='file'] found on page.")


async def _attach_label_file(page, ttn: str) -> dict:
    if not NP_API_KEY:
        raise StageError("attach_label", "SUP2_NP_API_KEY (or NP_API_KEY) is required.", {})
    pdf_path = _download_np_label(LABELS_DIR, ttn, NP_API_KEY)

    attached = False
    attach_btn = page.get_by_role("button", name="Накладний файл").locator(":visible").first
    if await attach_btn.count() == 0:
        attach_btn = page.get_by_text("Накладний файл", exact=False).locator(":visible").first

    if await attach_btn.count() > 0:
        try:
            async with page.expect_file_chooser(timeout=TIMEOUT_MS) as fc_info:
                await attach_btn.click(timeout=TIMEOUT_MS)
            fc = await fc_info.value
            await fc.set_files(str(pdf_path))
            attached = True
        except Exception:
            attached = False

    if not attached:
        file_input = await _pick_file_input(page)
        await file_input.wait_for(state="attached", timeout=TIMEOUT_MS)
        try:
            await file_input.wait_for(state="visible", timeout=TIMEOUT_MS)
        except Exception:
            pass
        await _wait_enabled(file_input, TIMEOUT_MS)
        await file_input.set_input_files(str(pdf_path))
    await page.wait_for_timeout(600)

    if (page.url or "").startswith("chrome-error://") or (page.url or "").startswith("file://"):
        raise StageError(
            "attach_label",
            "File attach navigated browser to an invalid URL.",
            {"url": page.url or "", "file": str(pdf_path)},
        )

    file_input = await _pick_file_input(page)
    input_value = (await file_input.input_value(timeout=TIMEOUT_MS)).strip()
    if not input_value:
        raise StageError(
            "attach_label",
            "File input value is empty after set_input_files",
            {"file": str(pdf_path)},
        )

    return {"file": str(pdf_path), "input_value": input_value}


async def _submit_order_and_get_number(page, ttn: str) -> str:
    submit_btn = page.locator(
        "p.os-button.js-submit-button-client",
        has_text="Оформити замовлення",
    ).first

    if await submit_btn.count() == 0:
        fallback = page.get_by_text("Оформити замовлення", exact=False)
        fallback_count = await fallback.count()
        chosen = None
        for i in range(min(fallback_count, 10)):
            cand = fallback.nth(i)
            try:
                cls = await cand.evaluate("el => String(el.className || '')")
            except Exception:
                cls = ""
            if "os-button" in cls:
                chosen = cand
                break
        if chosen is None and fallback_count > 0:
            chosen = fallback.first
        if chosen is None:
            raise RuntimeError("Submit button 'Оформити замовлення' not found.")
        submit_btn = chosen

    await submit_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
    await submit_btn.click(timeout=TIMEOUT_MS)
    _write_submit_checkpoint(ttn=ttn, url=page.url or "", submitted=True)

    try:
        await page.wait_for_url(re.compile(r".*/client/order/\d+/?"), timeout=TIMEOUT_MS)
    except PWTimeoutError as e:
        raise StageError(
            "post_submit_failed",
            "Submit click may have created supplier order, but no success navigation was detected.",
            {"submitted": True, "ttn": ttn, "url": page.url or ""},
        ) from e

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
    except PWTimeoutError:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PWTimeoutError:
        pass

    m = re.search(r"/client/order/(\d+)/?", page.url or "")
    if m:
        order_no = m.group(1)
        _write_submit_checkpoint(ttn=ttn, url=page.url or "", submitted=True, order_number=order_no)
        return order_no

    candidates = [
        page.locator("h1").first,
        page.get_by_text("Ваш Процес", exact=False).first,
        page.locator("body").first,
    ]
    for loc in candidates:
        try:
            txt = (await loc.inner_text(timeout=TIMEOUT_MS)).strip()
        except Exception:
            continue
        m_txt = re.search(r"Ваш\s+Процес\s*(\d+)", txt, flags=re.IGNORECASE)
        if m_txt:
            order_no = m_txt.group(1)
            _write_submit_checkpoint(ttn=ttn, url=page.url or "", submitted=True, order_number=order_no)
            return order_no

    raise StageError(
        "post_submit_failed",
        "Supplier order was likely submitted, but order number could not be parsed.",
        {"submitted": True, "ttn": ttn, "url": page.url or ""},
    )


async def _run() -> tuple[bool, dict]:
    if not STORAGE_STATE_FILE:
        raise RuntimeError("SUP2_STORAGE_STATE_FILE is empty.")
    if not TTN:
        raise RuntimeError("SUP2_TTN is required")
    items = _parse_items()

    state_path = Path(STORAGE_STATE_FILE)
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    if not state_path.exists():
        raise RuntimeError(f"Storage state file not found: {state_path}")

    browser = None
    context = None
    page = None
    stage = "init"
    added: list[dict] = []
    supplier_order_number = ""
    attach_info: dict | None = None
    submitted = False
    paused_for_error = False

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS)
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()

            if CLEAR_BASKET:
                stage = "clear_basket"
                await _clear_basket(page)

            stage = "add_items"
            added = await _add_items(page, items)

            stage = "select_city"
            await _select_city_kyiv(page)

            stage = "fill_comment"
            await _fill_comment(page, TTN)

            stage = "attach_label"
            attach_info = await _attach_label_file(page, TTN)

            stage = "submit_order"
            supplier_order_number = await _submit_order_and_get_number(page, TTN)
            submitted = True

            if DELETE_LABEL_AFTER_ATTACH and attach_info:
                file_path_raw = str(attach_info.get("file", "")).strip()
                if file_path_raw:
                    try:
                        Path(file_path_raw).unlink(missing_ok=True)
                    except Exception:
                        pass

            return True, {
                "ok": True,
                "ttn": TTN,
                "submitted": submitted,
                "added": added,
                "supplier_order_number": supplier_order_number,
                "url": page.url or BASKET_URL,
            }
    except StageError as e:
        await _debug_pause_if_needed()
        paused_for_error = True
        return False, {
            "ok": False,
            "error": str(e),
            "stage": e.stage or stage,
            "url": page.url if page is not None else BASKET_URL,
            "submitted": bool((e.details or {}).get("submitted")) or submitted,
            "details": e.details or {},
        }
    except Exception as e:
        await _debug_pause_if_needed()
        paused_for_error = True
        return False, {
            "ok": False,
            "error": str(e),
            "stage": stage,
            "url": page.url if page is not None else BASKET_URL,
            "submitted": submitted,
            "details": {},
        }
    finally:
        try:
            if not paused_for_error:
                await _debug_pause_if_needed()
        except Exception:
            pass
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


def sup2_select_branch(*args, **kwargs):
    raise NotImplementedError


def sup2_confirm_order(*args, **kwargs):
    raise NotImplementedError


def main() -> int:
    try:
        ok, payload = asyncio.run(_run())
    except Exception as e:
        payload = {"ok": False, "error": str(e), "stage": "init", "url": BASKET_URL, "details": {}}
        ok = False

    print(SUPPLIER_RESULT_JSON_PREFIX + json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
