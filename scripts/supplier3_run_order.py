import asyncio
import json
import os
import re
import sys
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeoutError
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


SUP3_BASE_URL = (os.getenv("SUP3_BASE_URL") or "https://dsn.ua/").strip() or "https://dsn.ua/"
SUP3_EMAIL = (os.getenv("SUP3_EMAIL") or "").strip()
SUP3_PASSWORD = (os.getenv("SUP3_PASSWORD") or "").strip()
SUP3_LOGIN_EMAIL = (os.getenv("SUP3_LOGIN_EMAIL") or SUP3_EMAIL or "").strip()
SUP3_LOGIN_PASSWORD = (os.getenv("SUP3_LOGIN_PASSWORD") or SUP3_PASSWORD or "").strip()
SUP3_STORAGE_STATE_FILE = (os.getenv("SUP3_STORAGE_STATE_FILE") or ".state_supplier3.json").strip()
SUP3_HEADLESS = _to_bool(os.getenv("SUP3_HEADLESS", "1"), True)
SUP3_TIMEOUT_MS = _to_int(os.getenv("SUP3_TIMEOUT_MS", "20000"), 20000)
SUP3_DEBUG_PAUSE_SECONDS = _to_int(os.getenv("SUP3_DEBUG_PAUSE_SECONDS", os.getenv("SUP3_DEBUG_PAUSE", "0")), 0)
SUP3_STAGE = (os.getenv("SUP3_STAGE") or "run").strip().lower() or "run"
SUP3_FORCE_LOGIN = _to_bool(os.getenv("SUP3_FORCE_LOGIN", "0"), False)
SUP3_CLEAR_BASKET = _to_bool(os.getenv("SUP3_CLEAR_BASKET", "0"), False)
SUP3_USE_CDP = _to_bool(os.getenv("SUP3_USE_CDP", "0"), False)
SUP3_CDP_URL = (os.getenv("SUP3_CDP_URL") or "").strip()
SUP3_ITEMS = (os.getenv("SUP3_ITEMS") or "").strip()
SUP3_TTN = (os.getenv("SUP3_TTN") or "").strip()
SUP3_NP_API_KEY = (
    os.getenv("SUP3_NP_API_KEY")
    or os.getenv("NP_API_KEY")
    or os.getenv("SUP2_NP_API_KEY")
    or os.getenv("BIOTUS_NP_API_KEY")
    or ""
).strip()
SUP3_LABELS_DIR = ROOT / "supplier3_labels"


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.details = details or {}


@dataclass(frozen=True)
class Sup3Item:
    sku: str
    qty: int


async def _debug_pause_if_needed(page=None) -> None:
    if SUP3_DEBUG_PAUSE_SECONDS <= 0:
        return
    ms = SUP3_DEBUG_PAUSE_SECONDS * 1000
    if page is not None:
        try:
            await page.wait_for_timeout(ms)
            return
        except Exception:
            pass
    await asyncio.sleep(SUP3_DEBUG_PAUSE_SECONDS)


def _select_all_shortcut() -> str:
    return "Meta+A" if sys.platform == "darwin" else "Control+A"


def _state_path() -> Path:
    if not SUP3_STORAGE_STATE_FILE:
        raise RuntimeError("SUP3_STORAGE_STATE_FILE is empty.")
    p = Path(SUP3_STORAGE_STATE_FILE)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _failure_screenshot_path() -> Path:
    return ROOT / "supplier3_login_failed.png"


def _add_items_failure_screenshot_path() -> Path:
    return ROOT / "supplier3_add_items_failed.png"


def _normalize_qty(value) -> int:
    try:
        qty = int(str(value).strip())
    except Exception as e:
        raise RuntimeError(f"Invalid qty: {value}") from e
    if qty < 1:
        raise RuntimeError(f"Qty must be >= 1, got: {qty}")
    return qty


def _parse_sup3_items() -> list[Sup3Item]:
    raw = (SUP3_ITEMS or "").strip()
    if not raw:
        raise RuntimeError("SUP3_ITEMS is required for SUP3_STAGE=add_items (format: SKU1:2;SKU2:1)")

    out: list[Sup3Item] = []
    parts = [p.strip() for p in re.split(r"[;,]", raw) if p.strip()]
    for idx, part in enumerate(parts, start=1):
        if ":" in part:
            sku_raw, qty_raw = part.split(":", 1)
            sku = sku_raw.strip()
            qty = _normalize_qty(qty_raw)
        else:
            sku = part.strip()
            qty = 1
        if not sku:
            raise RuntimeError(f"SUP3_ITEMS part #{idx} has empty sku.")
        out.append(Sup3Item(sku=sku, qty=qty))

    if not out:
        raise RuntimeError("SUP3_ITEMS is empty after parsing.")
    return out


def _parse_price_uah(price_raw: str) -> int | None:
    txt = str(price_raw or "").replace("\xa0", " ")
    txt = txt.strip()
    if not txt:
        return None
    m = re.search(r"(\d[\d\s]*)", txt)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    if not digits:
        return None
    try:
        return int(digits)
    except Exception:
        return None


def _download_np_label_sup3(folder: Path, ttn: str, api_key: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    out_path = folder / f"label-{ttn}.pdf"
    url = (
        "https://my.novaposhta.ua/orders/printMarking100x100/"
        f"orders[]/{ttn}/type/pdf/apiKey/{api_key}/zebra"
    )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=max(10, SUP3_TIMEOUT_MS / 1000)) as resp:
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


async def _pick_checkout_file_input(page):
    selectors = [
        'input[type="file"][name*="invoiceFileName"]',
        'section.checkout-step[data-component="Delivery"] input[type="file"]',
        'input[type="file"].j-ignore',
        'input[type="file"]',
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                return loc, sel
        except Exception:
            continue
    raise RuntimeError("No checkout file input found")


async def _attach_invoice_label_file(page, label_path: Path) -> dict:
    stage = "attach_invoice_label"
    if not label_path or not label_path.exists() or label_path.stat().st_size <= 0:
        raise StageError(stage, "label_missing", {"label_path": str(label_path) if label_path else ""})

    file_input, sel_used = await _pick_checkout_file_input(page)
    await file_input.wait_for(state="attached", timeout=min(8000, SUP3_TIMEOUT_MS))
    try:
        await file_input.wait_for(state="visible", timeout=min(5000, SUP3_TIMEOUT_MS))
    except Exception:
        pass

    last_err = None
    for attempt in range(1, 4):
        try:
            await file_input.set_input_files(str(label_path))
            await page.wait_for_timeout(250)
            files_len = 0
            try:
                files_len = int(await file_input.evaluate("(el) => (el.files ? el.files.length : 0)"))
            except Exception:
                files_len = 0
            value = ""
            try:
                value = (await file_input.input_value(timeout=1000)).strip()
            except Exception:
                value = ""
            if files_len == 1 or value:
                print(f"[SUP3] label attached OK")
                return {
                    "file": str(label_path),
                    "file_input_selector": sel_used,
                    "input_value": value,
                    "files_len": files_len,
                }
            last_err = RuntimeError("file input did not keep attached file")
        except Exception as e:
            last_err = e
        await page.wait_for_timeout(250)

    raise StageError(
        stage,
        "File attach failed",
        {"file": str(label_path), "selector": sel_used, "error": str(last_err) if last_err else "unknown"},
    )


async def _submit_checkout_order_and_get_number(page) -> str:
    stage = "confirm_order"
    selectors_tried: list[str] = []
    submit_candidates = [
        ("button.btn-submit.special", page.locator("button.btn-submit.special").first),
        ("button[type='submit'].btn-submit", page.locator("button[type='submit'].btn-submit").first),
        ("button.btn-submit", page.locator("button.btn-submit").first),
        ("text=Оформити замовлення (button)", page.get_by_role("button", name=re.compile(r"Оформити замовлення", re.I)).first),
    ]

    submit_btn = None
    sel_used = ""
    for sel, loc in submit_candidates:
        selectors_tried.append(sel)
        try:
            if await loc.count() > 0:
                submit_btn = loc
                sel_used = sel
                break
        except Exception:
            continue

    if submit_btn is None:
        raise StageError(stage, "Submit button not found", {"selectors_tried": selectors_tried, "url": page.url or SUP3_BASE_URL})

    try:
        await submit_btn.wait_for(state="visible", timeout=min(6000, SUP3_TIMEOUT_MS))
    except Exception:
        pass

    print(f"[SUP3] checkout submit: click via {sel_used}")
    navigated = False
    try:
        async with page.expect_navigation(url=re.compile(r".*/checkout/complete/\d+/?"), wait_until="domcontentloaded", timeout=SUP3_TIMEOUT_MS):
            await submit_btn.click(timeout=min(5000, SUP3_TIMEOUT_MS), force=True)
        navigated = True
    except Exception:
        try:
            await submit_btn.click(timeout=min(5000, SUP3_TIMEOUT_MS), force=True)
        except Exception as e:
            raise StageError(stage, "Submit click failed", {"selector": sel_used, "error": str(e), "url": page.url or SUP3_BASE_URL})

    if not navigated:
        try:
            await page.wait_for_url(re.compile(r".*/checkout/complete/\d+/?"), timeout=min(7000, SUP3_TIMEOUT_MS))
            navigated = True
        except Exception:
            navigated = False

    success_section = page.locator("section.checkout__success").first
    success_visible = False
    try:
        if await success_section.count() > 0:
            await success_section.wait_for(state="visible", timeout=min(5000, SUP3_TIMEOUT_MS))
            success_visible = True
    except Exception:
        success_visible = False

    if not navigated and not success_visible:
        raise StageError(stage, "Did not reach checkout complete", {"url": page.url or SUP3_BASE_URL, "selector": sel_used})

    m = re.search(r"/checkout/complete/(\d+)/?", page.url or "")
    if m:
        order_number = m.group(1)
        print(f"[SUP3] checkout submit: order number => {order_number}")
        return order_number

    text_candidates = [success_section, page.locator("body").first]
    for loc in text_candidates:
        try:
            if await loc.count() == 0:
                continue
            txt = (await loc.inner_text(timeout=min(3000, SUP3_TIMEOUT_MS))).strip()
        except Exception:
            continue
        m_txt = re.search(r"Замовлення\s*№\s*(\d+)", txt, flags=re.IGNORECASE)
        if m_txt:
            order_number = m_txt.group(1)
            print(f"[SUP3] checkout submit: order number => {order_number}")
            return order_number

    raise StageError(stage, "Supplier order number not found", {"url": page.url or SUP3_BASE_URL})


async def _safe_is_visible(locator) -> bool | None:
    try:
        if await locator.count() == 0:
            return False
        return await locator.is_visible()
    except Exception:
        return None


async def _safe_outer_html_snippet(locator, max_len: int = 500) -> str:
    try:
        if await locator.count() == 0:
            return ""
        html = await locator.evaluate("(el) => el.outerHTML || ''")
        html = re.sub(r"\s+", " ", str(html or "")).strip()
        return html[:max_len]
    except Exception:
        return ""


async def _safe_is_enabled(locator) -> bool | None:
    try:
        if await locator.count() == 0:
            return False
        return await locator.is_enabled()
    except Exception:
        return None


async def _locator_diag(locator) -> dict:
    count = 0
    visible = None
    enabled = None
    try:
        count = await locator.count()
    except Exception:
        count = 0
    if count > 0:
        visible = await _safe_is_visible(locator.first)
        enabled = await _safe_is_enabled(locator.first)
    return {"count": count, "visible": visible, "enabled": enabled}


async def _best_effort_close_popups(page) -> None:
    candidates = [
        page.get_by_role("button", name=re.compile(r"^(OK|Добре|Прийняти|Погоджуюсь|Зрозуміло)$", re.I)).first,
        page.get_by_role("button", name=re.compile(r"(cookie|cookies)", re.I)).first,
        page.get_by_role("button", name=re.compile(r"(закрити|close|×|x)", re.I)).first,
        page.locator("[aria-label*='close' i], [title*='close' i], .close, .popup__close, .modal__close").first,
    ]
    for loc in candidates:
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=1500)
                await page.wait_for_timeout(150)
        except Exception:
            continue


async def detect_and_fail_unavailable_modal(page, step_name: str) -> None:
    """
    DSN sometimes shows a native/HTML popup saying some items were not added to cart
    because they are unavailable. This is a business-logic error: fail fast.
    """
    selectors = [
        "#modal-overlay:visible",
        ".overlay:visible",
        "section.popup:visible",
        ".popup:visible",
        ".popup-block:visible",
        "body",
    ]
    snippets: list[str] = []
    for sel in selectors:
        loc = page.locator(sel)
        try:
            count = await loc.count()
        except Exception:
            count = 0
        for i in range(min(count, 5)):
            cand = loc.nth(i)
            try:
                if sel != "body" and not await cand.is_visible():
                    continue
                txt = (await cand.inner_text(timeout=500)).strip()
            except Exception:
                continue
            if not txt:
                continue
            txt_norm = re.sub(r"\s+", " ", txt).strip()
            txt_low = txt_norm.casefold()
            snippets.append(txt_norm[:300])
            if ("не були додані в кошик" in txt_low) or ("не доступ" in txt_low and "кошик" in txt_low):
                print(f"[SUP3] {step_name}: detected DSN unavailable-items modal")
                print(f"[SUP3] {step_name}: modal text => {txt_norm[:300]!r}")
                print(f"[SUP3] {step_name}: url => {page.url or SUP3_BASE_URL}")
                raise RuntimeError(f"{step_name}: DSN modal: items not added to cart / unavailable")


async def _search_by_sku(page, sku: str) -> None:
    search_input = page.locator('input.search__input[name="q"], input[name="q"]').first
    await search_input.wait_for(state="visible", timeout=SUP3_TIMEOUT_MS)
    print(f"[SUP3] add_items: search sku={sku}")
    start_url = page.url or SUP3_BASE_URL
    await search_input.click(timeout=SUP3_TIMEOUT_MS)
    await page.keyboard.press(_select_all_shortcut())
    await page.keyboard.press("Backspace")
    await search_input.type(sku, delay=20, timeout=SUP3_TIMEOUT_MS)
    try:
        await search_input.dispatch_event("input")
        await search_input.dispatch_event("change")
    except Exception:
        pass

    enter_sent = False
    target_search_re = re.compile(r"/katalog/search/\?q=", re.IGNORECASE)
    # Keep a small fallback chain, but avoid long "thinking" loops.
    for mode in ("locator_enter", "form_submit", "form_submit_button_click"):
        try:
            if mode == "locator_enter":
                await search_input.press("Enter", timeout=1500)
            elif mode == "form_submit":
                await search_input.evaluate(
                    """(el) => {
                        const form = el.closest('form');
                        if (form) {
                            if (typeof form.requestSubmit === 'function') form.requestSubmit();
                            else form.submit();
                            return true;
                        }
                        return false;
                    }"""
                )
            else:
                submit_clicked = await search_input.evaluate(
                    """(el) => {
                        const form = el.closest('form');
                        if (!form) return false;
                        const btn = form.querySelector(
                            'button[type="submit"], input[type="submit"], .search__btn, .search__submit'
                        );
                        if (!btn) return false;
                        btn.click();
                        return true;
                    }"""
                )
                if not submit_clicked:
                    raise RuntimeError("search submit button not found")
            print(f"[SUP3] add_items: search submit via {mode}")
            enter_sent = True
            # Give JS handlers a short chance; stop early on visible results page signal.
            await page.wait_for_timeout(250)
            if (page.url or "") != start_url or target_search_re.search(page.url or ""):
                break
        except Exception:
            continue
    if not enter_sent:
        print("[SUP3] add_items: WARN search submit method did not confirm success")
    try:
        await page.wait_for_url(re.compile(r"/katalog/search/\?q="), timeout=min(8000, SUP3_TIMEOUT_MS))
        print(f"[SUP3] add_items: search results url={page.url}")
    except Exception:
        print(f"[SUP3] add_items: search url wait skipped current_url={page.url}")
        # Last-resort deterministic fallback: open DSN search results URL directly.
        search_url = f"{SUP3_BASE_URL.rstrip('/')}/katalog/search/?q={urllib.parse.quote_plus(sku)}"
        print(f"[SUP3] add_items: search fallback goto {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=SUP3_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=SUP3_TIMEOUT_MS)
    except PWTimeoutError:
        pass
    await page.wait_for_timeout(250)


async def _wait_product_row_by_sku(page, sku: str):
    qty_input_selector = (
        "input.counter-field.j-buy-button-counter-input[type='number'], "
        "input.j-buy-button-counter-input[type='number'], input.counter-field[type='number'], input[type='number']"
    )
    product_scope = "table.productsTable, .productsTable, main, .content, body"
    candidate_selectors = [
        ("tr.j-product-row by td", page.locator("tr.j-product-row", has=page.locator(f"td:has-text('{sku}')")).first),
        ("tr.j-product-row by text", page.locator("tr.j-product-row", has_text=sku).first),
        ("productsTable tbody tr by text", page.locator("table.productsTable tbody tr", has_text=sku).first),
        ("productsTable tr by text", page.locator("table.productsTable tr", has_text=sku).first),
        (
            "generic row/card by text+qty",
            page.locator(
                "tr, .product, .product-item, .catalog-item, .productsTable-row, [class*='product']",
                has_text=sku,
                has=page.locator(qty_input_selector),
            ).first,
        ),
    ]
    for label, row in candidate_selectors:
        try:
            await row.wait_for(state="attached", timeout=min(2500, SUP3_TIMEOUT_MS))
            try:
                await row.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            print(f"[SUP3] add_items: row found via {label}")
            return row
        except Exception:
            continue

    rows = page.locator(
        "tr.j-product-row, table.productsTable tbody tr, table.productsTable tr, "
        ".product, .product-item, .catalog-item, .productsTable-row, [class*='product']"
    )
    purchasable_rows = page.locator(
        "tr.j-product-row, table.productsTable tbody tr, table.productsTable tr, "
        ".product, .product-item, .catalog-item, .productsTable-row, [class*='product']",
        has=page.locator(qty_input_selector),
    )
    deadline = asyncio.get_running_loop().time() + (SUP3_TIMEOUT_MS / 1000.0)
    search_text_needle = sku.casefold()
    sku_compact = re.sub(r"[^a-z0-9]+", "", search_text_needle)
    scrolled_once = False
    while asyncio.get_running_loop().time() < deadline:
        # If DSN search returns exactly one purchasable result and row text doesn't contain
        # the raw query verbatim, use it as a practical fallback.
        try:
            pcount = await purchasable_rows.count()
        except Exception:
            pcount = 0
        if pcount == 1:
            only = purchasable_rows.first
            try:
                await only.wait_for(state="visible", timeout=500)
            except Exception:
                pass
            print("[SUP3] add_items: row found via single purchasable result fallback")
            return only

        # Fallback by quantity input ancestor (DSN can render search result cards with classes
        # that don't match our row selectors, but qty input is still stable).
        qty_inputs = page.locator(qty_input_selector)
        try:
            qcount = await qty_inputs.count()
        except Exception:
            qcount = 0
        visible_qty_inputs = 0
        for qi in range(min(qcount, 20)):
            qloc = qty_inputs.nth(qi)
            try:
                if not await qloc.is_visible():
                    continue
                visible_qty_inputs += 1
            except Exception:
                continue
            anc = qloc.locator(
                "xpath=ancestor::tr[1] | ancestor::*[contains(@class,'product')][1] | ancestor::*[contains(@class,'item')][1]"
            ).first
            try:
                if await anc.count() > 0:
                    anc_text = re.sub(r"\s+", " ", (await anc.inner_text(timeout=700)) or "").strip()
                    anc_low = anc_text.casefold()
                    anc_compact = re.sub(r"[^a-z0-9]+", "", anc_low)
                    if search_text_needle in anc_low or (sku_compact and sku_compact in anc_compact):
                        print(f"[SUP3] add_items: row found via qty-input ancestor fallback idx={qi}")
                        return anc
                elif qcount == 1:
                    print("[SUP3] add_items: row found via single qty-input fallback")
                    return qloc
            except Exception:
                if qcount == 1 and visible_qty_inputs == 1:
                    print("[SUP3] add_items: row found via single visible qty-input fallback")
                    return qloc

        # Last practical fallback for DSN search pages: if there is exactly one visible qty input
        # inside the content area, use its nearest ancestor regardless of SKU text.
        if visible_qty_inputs == 1:
            for qi in range(min(qcount, 20)):
                qloc = qty_inputs.nth(qi)
                try:
                    if not await qloc.is_visible():
                        continue
                except Exception:
                    continue
                anc_any = qloc.locator(
                    "xpath=ancestor::tr[1] | ancestor::li[1] | ancestor::article[1] | ancestor::div[contains(@class,'product')][1] | ancestor::div[contains(@class,'item')][1]"
                ).first
                try:
                    if await anc_any.count() > 0:
                        print("[SUP3] add_items: row found via single visible qty-input + generic ancestor fallback")
                        return anc_any
                    print("[SUP3] add_items: row found via single visible qty-input direct fallback")
                    return qloc
                except Exception:
                    print("[SUP3] add_items: row found via single visible qty-input direct fallback")
                    return qloc

        # Another fallback: first visible purchasable row in products table on search page.
        try:
            table_rows = page.locator("table.productsTable tbody tr, table.productsTable tr")
            table_count = await table_rows.count()
        except Exception:
            table_count = 0
            table_rows = None
        if table_rows is not None and table_count > 0:
            for i in range(min(table_count, 50)):
                cand = table_rows.nth(i)
                try:
                    if not await cand.is_visible():
                        continue
                    row_qty = cand.locator(qty_input_selector).first
                    if await row_qty.count() > 0:
                        print(f"[SUP3] add_items: row found via first visible productsTable row with qty idx={i}")
                        return cand
                except Exception:
                    continue

        try:
            count = await rows.count()
        except Exception:
            count = 0
        for i in range(min(count, 200)):
            cand = rows.nth(i)
            try:
                text = (await cand.inner_text(timeout=1000)).strip()
            except Exception:
                continue
            text_low = text.casefold()
            if search_text_needle in text_low:
                print(f"[SUP3] add_items: row found via fallback scan idx={i}")
                return cand
            text_compact = re.sub(r"[^a-z0-9]+", "", text_low)
            if sku_compact and sku_compact in text_compact:
                print(f"[SUP3] add_items: row found via normalized fallback scan idx={i}")
                return cand
        # Search results are often rendered below the fold.
        if not scrolled_once:
            try:
                await page.mouse.wheel(0, 1800)
            except Exception:
                try:
                    await page.evaluate("window.scrollBy(0, 1800)")
                except Exception:
                    pass
            scrolled_once = True
        else:
            try:
                await page.mouse.wheel(0, 1000)
            except Exception:
                pass
        await page.wait_for_timeout(120)
    return None


async def _extract_row_price(row, sku: str, warnings: list[str]) -> tuple[int | None, str]:
    price_cell = row.locator("td.productsTable-cell__price").first
    price_raw = ""
    try:
        if await price_cell.count() > 0:
            price_raw = (await price_cell.inner_text(timeout=1500)).strip()
        else:
            # Fallback: try any price-like cell text in row
            row_text = (await row.inner_text(timeout=1500)).strip()
            m = re.search(r"\d[\d\s]*\s*грн", row_text, flags=re.IGNORECASE)
            if m:
                price_raw = m.group(0).strip()
    except Exception:
        price_raw = ""

    price_uah = _parse_price_uah(price_raw)
    if price_uah is None:
        warnings.append(f"PRICE_NOT_PARSED sku={sku} raw={price_raw!r}")
    return price_uah, price_raw


async def _is_row_unavailable(row) -> tuple[bool, str]:
    status_cell = row.locator("td.productsTable-cell.__status.__unavailable, td.__status.__unavailable").first
    try:
        if await status_cell.count() > 0:
            txt = (await status_cell.inner_text(timeout=1000)).strip()
            txt_norm = re.sub(r"\s+", " ", txt or "").strip()
            if txt_norm and ("немає в наявності" in txt_norm.casefold() or "не в наявності" in txt_norm.casefold()):
                return True, txt_norm
    except Exception:
        pass

    # Fallback by row text if DSN changes classes.
    try:
        row_text = re.sub(r"\s+", " ", (await row.inner_text(timeout=1000)) or "").strip()
    except Exception:
        row_text = ""
    row_low = row_text.casefold()
    if "немає в наявності" in row_low or "не в наявності" in row_low:
        return True, row_text[:300]
    return False, ""


async def _set_row_qty_fallback(row, target_qty: int) -> bool:
    page = row.page
    qty_input = row.locator(
        "input.counter-field.j-buy-button-counter-input[type='number'], "
        "input.j-buy-button-counter-input[type='number'], input.counter-field[type='number']"
    ).first
    plus_btn = row.locator("a.counter-btn_plus, .counter-btn_plus").first
    minus_btn = row.locator("a.counter-btn_minus, .counter-btn_minus").first

    try:
        current_raw = await qty_input.input_value(timeout=1000)
        current = int(re.sub(r"\D", "", current_raw or "") or "0")
    except Exception:
        current = 0

    for _ in range(30):
        if current == target_qty:
            return True
        try:
            if current < target_qty and await plus_btn.count() > 0:
                await plus_btn.click(timeout=1500)
                print(f"[SUP3] add_items: set_qty fallback plus -> target={target_qty}")
                await detect_and_fail_unavailable_modal(page, "add_items.set_qty_fallback.plus")
            elif current > target_qty and await minus_btn.count() > 0:
                await minus_btn.click(timeout=1500)
                print(f"[SUP3] add_items: set_qty fallback minus -> target={target_qty}")
                await detect_and_fail_unavailable_modal(page, "add_items.set_qty_fallback.minus")
            else:
                break
            await page.wait_for_timeout(120)
            await detect_and_fail_unavailable_modal(page, "add_items.set_qty_fallback.wait")
            current_raw = await qty_input.input_value(timeout=1000)
            current = int(re.sub(r"\D", "", current_raw or "") or "0")
        except Exception:
            break
    return current == target_qty


async def _set_row_qty(row, qty: int) -> None:
    page = row.page
    qty_input = row.locator(
        "input.counter-field.j-buy-button-counter-input[type='number'], "
        "input.j-buy-button-counter-input[type='number'], input.counter-field[type='number'], input[type='number']"
    ).first
    await qty_input.wait_for(state="attached", timeout=SUP3_TIMEOUT_MS)
    try:
        await qty_input.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass
    try:
        await qty_input.wait_for(state="visible", timeout=min(5000, SUP3_TIMEOUT_MS))
    except Exception:
        pass

    async def _read_qty_value() -> tuple[str, str]:
        try:
            v = await qty_input.input_value(timeout=1200)
        except Exception:
            v = ""
        return v, re.sub(r"\D", "", v or "")

    before_raw, before_digits = await _read_qty_value()
    print(f"[SUP3] add_items: set_qty start target={qty} current={before_raw!r}")

    # Attempt 1: direct fill + change events (fast path)
    try:
        await qty_input.click(timeout=min(3000, SUP3_TIMEOUT_MS), force=True)
        await page.keyboard.press(_select_all_shortcut())
        await page.keyboard.press("Backspace")
    except Exception:
        pass
    try:
        await qty_input.fill(str(qty), timeout=min(3000, SUP3_TIMEOUT_MS))
        try:
            await qty_input.dispatch_event("input")
            await qty_input.dispatch_event("change")
        except Exception:
            pass
        try:
            await qty_input.press("Enter", timeout=800)
        except Exception:
            try:
                await qty_input.blur()
            except Exception:
                pass
    except Exception:
        pass

    await detect_and_fail_unavailable_modal(page, "add_items.set_qty.after_fill")
    await page.wait_for_timeout(180)
    await detect_and_fail_unavailable_modal(page, "add_items.set_qty.after_fill_wait")
    val1_raw, val1_digits = await _read_qty_value()
    print(f"[SUP3] add_items: set_qty after fill current={val1_raw!r}")
    if val1_digits == str(qty):
        return

    # Attempt 2: JS set value + events (helps on some reactive inputs)
    try:
        await qty_input.evaluate(
            """(el, value) => {
                el.focus();
                el.value = String(value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.blur();
            }""",
            str(qty),
        )
    except Exception:
        pass
    await detect_and_fail_unavailable_modal(page, "add_items.set_qty.after_js")
    await page.wait_for_timeout(180)
    val2_raw, val2_digits = await _read_qty_value()
    print(f"[SUP3] add_items: set_qty after js current={val2_raw!r}")
    if val2_digits == str(qty):
        return

    # Attempt 3: +/- fallback
    ok = await _set_row_qty_fallback(row, qty)
    if not ok:
        raise RuntimeError(
            f"Could not set qty={qty}; current_before={before_raw!r}; after_fill={val1_raw!r}; after_js={val2_raw!r}"
        )
    await detect_and_fail_unavailable_modal(page, "add_items.set_qty.after_fallback")
    final_raw, _ = await _read_qty_value()
    print(f"[SUP3] add_items: set_qty final current={final_raw!r}")


async def _add_items(page) -> dict:
    stage = "add_items"
    items = _parse_sup3_items()
    warnings: list[str] = []
    items_summary: dict[str, dict] = {}

    async def _raise_add_items_error(message: str, *, error_code: str | None = None, failed_item: dict | None = None) -> None:
        screenshot_path = ""
        try:
            shot = _add_items_failure_screenshot_path()
            await page.screenshot(path=str(shot), full_page=True)
            screenshot_path = str(shot)
        except Exception:
            pass
        details: dict = {}
        if error_code:
            details["error_code"] = error_code
        if failed_item:
            details["failed_item"] = failed_item
        if warnings:
            details["warnings"] = list(warnings)
        if screenshot_path:
            details["screenshot"] = screenshot_path
        raise StageError(stage, message, details)

    # We assume valid storage_state session for this stage.
    already, checks = await _is_logged_in(page)
    if not already:
        raise StageError(stage, "Not authorized: session is not logged in.", {"checks": checks})

    await page.goto(SUP3_BASE_URL, wait_until="domcontentloaded", timeout=SUP3_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(5000, SUP3_TIMEOUT_MS))
    except PWTimeoutError:
        pass
    await _best_effort_close_popups(page)

    for item in items:
        sku = item.sku
        qty = item.qty
        await _search_by_sku(page, sku)

        # Wait for results table to appear (best-effort; some pages render quickly or cache).
        products_table = page.locator("table.productsTable, .productsTable").first
        try:
            await products_table.wait_for(state="attached", timeout=min(5000, SUP3_TIMEOUT_MS))
        except Exception:
            pass

        row = await _wait_product_row_by_sku(page, sku)
        if row is None:
            await _raise_add_items_error(
                f"NOT_FOUND: sku={sku}, qty={qty}",
                error_code="NOT_FOUND",
                failed_item={"sku": sku, "qty": qty},
            )

        unavailable, unavailable_text = await _is_row_unavailable(row)
        if unavailable:
            await _raise_add_items_error(
                f"OUT_OF_STOCK: sku={sku}, qty={qty}: {unavailable_text or 'Немає в наявності'}",
                error_code="OUT_OF_STOCK",
                failed_item={"sku": sku, "qty": qty},
            )

        price_uah, price_raw = await _extract_row_price(row, sku, warnings)

        try:
            await _set_row_qty(row, qty)
        except Exception as e:
            err_text = str(e)
            if "DSN modal: items not added to cart / unavailable" in err_text:
                await _raise_add_items_error(
                    f"OUT_OF_STOCK: sku={sku}, qty={qty}: {e}",
                    error_code="OUT_OF_STOCK",
                    failed_item={"sku": sku, "qty": qty},
                )
            await _raise_add_items_error(
                f"SET_QTY_FAILED: sku={sku}, qty={qty}: {e}",
                error_code="SET_QTY_FAILED",
                failed_item={"sku": sku, "qty": qty},
            )

        await page.wait_for_timeout(250)
        try:
            await detect_and_fail_unavailable_modal(page, f"add_items.sku={sku}.post_qty_wait")
        except RuntimeError as e:
            await _raise_add_items_error(
                f"OUT_OF_STOCK: sku={sku}, qty={qty}: {e}",
                error_code="OUT_OF_STOCK",
                failed_item={"sku": sku, "qty": qty},
            )
        items_summary[sku] = {
            "qty": qty,
            "price_uah": price_uah,
            "price_raw": price_raw,
        }
        print(f"[SUP3] add_items: sku={sku} qty={qty} price_uah={price_uah} price_raw={price_raw!r}")

    result = {
        "ok": True,
        "stage": stage,
        "url": page.url or SUP3_BASE_URL,
        "items_summary": items_summary,
    }
    if warnings:
        result["warnings"] = warnings
    return result


async def _open_cart_modal(page) -> None:
    await page.goto(SUP3_BASE_URL, wait_until="domcontentloaded", timeout=SUP3_TIMEOUT_MS)
    await page.wait_for_timeout(150)
    await _best_effort_close_popups(page)

    cart_link = page.locator("a.basket__link.j-basket-link, a.basket__link").first
    cart_link_fallback = page.locator("div.basket.j-basket-header a[href='#'], div.basket a[href='#']").first
    cart_box = page.locator("div.basket.j-basket-header, div.basket, .j-basket-header").first

    clicked = False
    click_errors: list[str] = []
    click_attempts = [
        ("a.basket__link", cart_link, False),
        ("a.basket__link(force)", cart_link, True),
        ("div.basket a[href='#']", cart_link_fallback, False),
        ("div.basket a[href='#'](force)", cart_link_fallback, True),
        ("div.basket/.j-basket-header", cart_box, False),
        ("div.basket/.j-basket-header(force)", cart_box, True),
    ]
    for name, loc, force in click_attempts:
        try:
            if await loc.count() == 0:
                continue
            vis = await _safe_is_visible(loc)
            print(f"[SUP3] cart open: try click {name} visible={vis}")
            await loc.click(timeout=SUP3_TIMEOUT_MS, force=force)
            clicked = True
            break
        except Exception as e:
            click_errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
    if not clicked:
        # JS fallback: many DSN header handlers are bound on the anchor.
        try:
            js_clicked = await page.evaluate(
                """() => {
                    const a = document.querySelector('a.basket__link');
                    if (!a) return false;
                    a.click();
                    return true;
                }"""
            )
            clicked = bool(js_clicked)
            if clicked:
                print("[SUP3] cart open: JS click a.basket__link")
        except Exception as e:
            click_errors.append(f"js a.basket__link: {type(e).__name__}: {e}")
    if not clicked:
        raise StageError(
            "clear_basket",
            "Cart header button not found/clickable",
            {"url": page.url or SUP3_BASE_URL, "click_errors": click_errors},
        )

    cart_modal = page.locator("section#cart.popup__cart, section#cart").first
    cart_table = cart_modal.locator("table.cart-items").first
    empty_markers = cart_modal.locator(".cart-empty, .popup__empty, .empty, text=порож").first
    overlay = page.locator("div#modal-overlay.overlay").first

    deadline = asyncio.get_running_loop().time() + (SUP3_TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        modal_visible = await _safe_is_visible(cart_modal)
        overlay_visible = await _safe_is_visible(overlay)
        cart_attached = False
        cart_display_block = False
        try:
            cart_attached = await cart_modal.count() > 0
        except Exception:
            cart_attached = False
        if cart_attached:
            try:
                cart_display_block = bool(
                    await cart_modal.evaluate(
                        """(el) => {
                            const st = window.getComputedStyle(el);
                            return st && st.display !== 'none' && st.visibility !== 'hidden';
                        }"""
                    )
                )
            except Exception:
                cart_display_block = False
        if modal_visible:
            try:
                if await cart_table.count() > 0:
                    return
            except Exception:
                pass
            try:
                if await empty_markers.count() > 0:
                    return
            except Exception:
                pass
            # table can be rendered a bit later; visible modal is enough to continue.
            return
        if cart_attached and (cart_display_block or overlay_visible):
            # Some DSN modal implementations render as attached + display:block while Playwright "visible"
            # can lag due to animation/overlay transitions.
            return
        await page.wait_for_timeout(120)

    raise StageError(
        "clear_basket",
        "Cart modal did not appear",
        {
            "url": page.url or SUP3_BASE_URL,
            "cart_modal_visible": await _safe_is_visible(cart_modal),
            "cart_modal_attached": (await cart_modal.count() > 0) if await cart_modal.count() >= 0 else None,
            "overlay_visible": await _safe_is_visible(overlay),
            "cart_outer_html": await _safe_outer_html_snippet(cart_modal, max_len=800),
            "click_errors": click_errors,
        },
    )


async def _find_cart_remove_button(row):
    preferred = row.locator(
        "a.cart-remove-btn, a.cart-remove, a.j-remove-p, a[href='#'].j-remove-p, .cart-remove-btn, .j-remove-p"
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
            outer = (await cand.evaluate("(el) => (el.outerHTML || '').toLowerCase()"))[:1000]
        except Exception:
            continue
        if "remove" in outer or "icon-cart-remove" in outer or "cart-remove" in outer:
            return cand, "svg_remove_fallback"

    icon_based = row.locator("[class*='icon'][class*='remove'], use").first
    try:
        if await icon_based.count() > 0:
            parent_clickable = row.locator("a, button, [role='button']").first
            if await parent_clickable.count() > 0:
                return parent_clickable, "generic_clickable_fallback"
    except Exception:
        pass

    return None, ""


async def _click_remove_with_optional_confirm(page, remove_btn, selector_used: str) -> tuple[bool, str]:
    short_click_timeout = min(2500, max(800, SUP3_TIMEOUT_MS))

    async def _safe_accept_dialog(dialog, mode_label: str) -> None:
        try:
            await dialog.accept()
        except Exception as e:
            msg = str(e)
            if "No dialog is showing" in msg:
                print(f"[SUP3] clear_basket: dialog already closed before accept ({mode_label})")
                return
            if "already handled" in msg.lower():
                print(f"[SUP3] clear_basket: dialog already handled before accept ({mode_label})")
                return
            raise

    async def _accept_html_confirm_if_present() -> bool:
        # Fallback only if site switches to custom modal in some flows.
        confirm_text = page.locator("text=Ви впевнені, що хочете видалити товар?").first
        ok_btn_candidates = [
            page.get_by_role("button", name=re.compile(r"^(ok|ок)$", re.I)).first,
            page.locator("button:has-text('OK'), button:has-text('ОК')").first,
            page.locator("a:has-text('OK'), a:has-text('ОК')").first,
            page.locator("input[type='button'][value='OK'], input[type='submit'][value='OK']").first,
        ]
        deadline = asyncio.get_running_loop().time() + 1.2
        while asyncio.get_running_loop().time() < deadline:
            if await _safe_is_visible(confirm_text):
                for btn in ok_btn_candidates:
                    try:
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click(timeout=800, force=True)
                            print("[SUP3] clear_basket: accepted HTML confirm modal")
                            return True
                    except Exception:
                        continue
            await page.wait_for_timeout(60)
        return False

    async def _click_with_auto_dialog(mode: str, *, force: bool = False, js: bool = False) -> tuple[bool, str]:
        loop = asyncio.get_running_loop()
        dialog_future = loop.create_future()

        def _on_dialog(dialog):
            async def _runner():
                try:
                    msg = dialog.message or ""
                    print(f"[SUP3] clear_basket: dialog via {mode}/{selector_used}: {msg!r}")
                    await _safe_accept_dialog(dialog, f"{mode}/{selector_used}")
                    if not dialog_future.done():
                        dialog_future.set_result(True)
                except Exception as e:
                    if not dialog_future.done():
                        dialog_future.set_exception(e)
            asyncio.create_task(_runner())

        page.once("dialog", _on_dialog)

        if js:
            await remove_btn.evaluate("(el) => el.click()")
        else:
            await remove_btn.click(timeout=short_click_timeout, force=force)

        try:
            await asyncio.wait_for(dialog_future, timeout=1.8)
            return True, mode
        except asyncio.TimeoutError:
            if await _accept_html_confirm_if_present():
                return True, f"{mode}+html_confirm"
            print(f"[SUP3] clear_basket: no dialog captured via {mode}/{selector_used}")
            return False, mode

    for mode, params in [
        ("normal_click", {"force": False, "js": False}),
        ("force_click", {"force": True, "js": False}),
        ("js_click", {"force": False, "js": True}),
    ]:
        try:
            return await _click_with_auto_dialog(mode, force=params["force"], js=params["js"])
        except Exception:
            continue

    raise RuntimeError("remove click failed across all click modes")


async def _clear_basket(page, *, stage_name: str = "clear_basket") -> dict:
    stage = stage_name
    cleared = 0
    cart_modal = None
    rows = None
    max_iters = 50

    async def _raise_clear_error(message: str, extra: dict | None = None) -> None:
        screenshot_path = ""
        try:
            shot = ROOT / "supplier3_clear_basket_failed.png"
            await page.screenshot(path=str(shot), full_page=True)
            screenshot_path = str(shot)
        except Exception:
            pass
        payload = {
            "ok": False,
            "error": message,
            "stage": stage,
            "url": page.url or SUP3_BASE_URL,
        }
        details = extra or {}
        if cart_modal is not None:
            details.setdefault("cart_modal_visible", await _safe_is_visible(cart_modal))
            form_snip = await _safe_outer_html_snippet(cart_modal, max_len=800)
            if form_snip:
                details.setdefault("cart_outer_html", form_snip)
        if rows is not None:
            try:
                details.setdefault("row_count", await rows.count())
            except Exception:
                pass
        if screenshot_path:
            payload["screenshot"] = screenshot_path
            details.setdefault("screenshot", screenshot_path)
        if details:
            payload["details"] = details
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        raise StageError(stage, message, details)

    try:
        await _open_cart_modal(page)
    except StageError as e:
        # DSN may not open cart modal when basket is empty; treat this as non-fatal and continue pipeline.
        if e.stage == stage and "Cart modal did not appear" in str(e):
            result = {
                "ok": True,
                "cleared": 0,
                "url": page.url or SUP3_BASE_URL,
                "skipped": "cart_empty_or_not_opened",
            }
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
            await _debug_pause_if_needed(page)
            return result
        raise
    cart_modal = page.locator("section#cart.popup__cart, section#cart").first
    rows = page.locator("section#cart tr.cart-item, section#cart table.cart-items tbody tr")

    for iteration in range(1, max_iters + 1):
        try:
            row_count = await rows.count()
        except Exception:
            row_count = 0

        if row_count <= 0:
            result = {"ok": True, "cleared": cleared, "url": page.url or SUP3_BASE_URL}
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
            await _debug_pause_if_needed(page)
            return result

        row = rows.first
        before_count = row_count
        remove_btn, selector_used = await _find_cart_remove_button(row)
        if remove_btn is None:
            await _raise_clear_error(
                "Remove button not found in cart row",
                extra={"iteration": iteration, "selector_used": selector_used or None},
            )

        print(f"[SUP3] clear_basket: iteration={iteration} rows={before_count} remove_selector={selector_used}")

        try:
            had_dialog, click_mode = await _click_remove_with_optional_confirm(page, remove_btn, selector_used)
            if not had_dialog:
                print(f"[SUP3] clear_basket: dialog not captured, retry once ({click_mode})")
                await page.wait_for_timeout(120)
                retry_rows = page.locator("section#cart tr.cart-item, section#cart table.cart-items tbody tr")
                try:
                    retry_count = await retry_rows.count()
                except Exception:
                    retry_count = 0
                if retry_count > 0:
                    retry_row = retry_rows.first
                    retry_btn, retry_selector = await _find_cart_remove_button(retry_row)
                    if retry_btn is None:
                        await _raise_clear_error(
                            "Remove button not found on retry",
                            extra={"iteration": iteration, "selector_used": selector_used},
                        )
                    had_dialog2, click_mode2 = await _click_remove_with_optional_confirm(
                        page, retry_btn, retry_selector or selector_used
                    )
                    if not had_dialog2:
                        await _raise_clear_error(
                            "Confirm dialog was not captured after retry",
                            extra={
                                "iteration": iteration,
                                "selector_used": retry_selector or selector_used,
                                "first_click_mode": click_mode,
                                "second_click_mode": click_mode2,
                            },
                        )
                    print(f"[SUP3] clear_basket: dialog captured on retry ({click_mode2})")
        except Exception as e:
            await _raise_clear_error(
                "Failed to click remove/accept confirm",
                extra={"iteration": iteration, "error": str(e), "selector_used": selector_used},
            )

        changed = False
        deadline = asyncio.get_running_loop().time() + min(3.5, SUP3_TIMEOUT_MS / 1000.0)
        # DSN cart updates via ajax loader; watch loader and row count.
        loader = page.locator("section#cart .j-cart-loader").first
        try:
            await row.wait_for(state="detached", timeout=1200)
            changed = True
        except Exception:
            pass
        while asyncio.get_running_loop().time() < deadline:
            try:
                current_count = await rows.count()
            except Exception:
                current_count = 0
            if current_count < before_count:
                changed = True
                break
            try:
                if await loader.count() > 0 and await loader.is_visible():
                    await page.wait_for_timeout(150)
                    continue
            except Exception:
                pass
            await page.wait_for_timeout(120)

        await page.wait_for_timeout(180)

        if not changed:
            await _raise_clear_error(
                "Cart row count did not decrease after delete",
                extra={"iteration": iteration, "before_count": before_count},
            )

        cleared += 1

    await _raise_clear_error("Too many delete iterations", extra={"max_iters": max_iters, "cleared": cleared})
    return {"ok": False, "cleared": cleared, "url": page.url or SUP3_BASE_URL}


async def _clear_cart_stage(page) -> dict:
    result = await _clear_basket(page, stage_name="clear_cart")
    removed = int(result.get("cleared") or 0)
    try:
        remaining = await page.locator("section#cart tr.cart-item, section#cart table.cart-items tbody tr").count()
    except Exception:
        remaining = 0
    payload = {
        "ok": True,
        "stage": "clear_cart",
        "removed_items_count": removed,
        "remaining_items_count": remaining,
        "url": page.url or SUP3_BASE_URL,
    }
    if "skipped" in result:
        payload["skipped"] = result["skipped"]
    return payload


async def _assert_cart_not_empty(page) -> None:
    stage = "checkout_ttn"
    try:
        await _open_cart_modal(page)
    except StageError:
        raise StageError(stage, "Cart is empty; will not fill TTN")

    rows = page.locator("section#cart tr.cart-item, section#cart table.cart-items tbody tr")
    try:
        row_count = await rows.count()
    except Exception:
        row_count = 0
    if row_count <= 0:
        raise StageError(stage, "Cart is empty; will not fill TTN")
    print(f"[SUP3] checkout_ttn: cart rows={row_count}")


async def _click_checkout_button(page) -> bool:
    stage = "checkout_ttn"
    selectors = [
        "section#cart a:has-text('Оформити замовлення')",
        "a:has-text('Оформити замовлення')",
    ]
    checkout_clicked = False
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            print(f"[SUP3] checkout_ttn: click checkout via {sel}")
            try:
                async with page.expect_navigation(
                    url=re.compile(r".*/checkout/.*"),
                    wait_until="domcontentloaded",
                    timeout=SUP3_TIMEOUT_MS,
                ):
                    await loc.click(timeout=min(3000, SUP3_TIMEOUT_MS), force=True)
            except Exception:
                await loc.click(timeout=min(3000, SUP3_TIMEOUT_MS), force=True)
                try:
                    await page.wait_for_url(re.compile(r".*/checkout/.*"), timeout=min(5000, SUP3_TIMEOUT_MS))
                except Exception:
                    pass
            checkout_clicked = True
            break
        except Exception:
            continue

    if not checkout_clicked:
        fallback_url = f"{SUP3_BASE_URL.rstrip('/')}/checkout/"
        print(f"[SUP3] checkout_ttn: checkout button not found, fallback goto {fallback_url}")
        await page.goto(fallback_url, wait_until="domcontentloaded", timeout=SUP3_TIMEOUT_MS)

    try:
        await page.wait_for_url(re.compile(r".*/checkout/.*"), timeout=min(5000, SUP3_TIMEOUT_MS))
    except Exception:
        if "/checkout/" not in (page.url or ""):
            raise StageError(stage, "Did not reach checkout", {"url": page.url or SUP3_BASE_URL})

    return checkout_clicked


async def _ensure_own_ttn_selected(page) -> bool:
    stage = "checkout_ttn"
    label_candidates = [
        page.get_by_text(re.compile(r"своя\s+накладн", re.I)).first,
        page.locator("label:has-text('своя накладна'), label:has-text('Своя накладна')").first,
        page.locator("[for]:has-text('своя накладна'), [for]:has-text('Своя накладная')").first,
    ]

    for loc in label_candidates:
        try:
            if await loc.count() > 0 and await loc.is_visible():
                print("[SUP3] checkout_ttn: select own TTN option")
                await loc.click(timeout=min(3000, SUP3_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(150)
                break
        except Exception:
            continue

    ttn_input_probe = page.locator(
        "dt.form-head:has-text('Вказати номер накладної') >> xpath=following-sibling::dd[1]//input"
    ).first

    radio_checked = False
    try:
        radios = page.locator("input[type='radio']")
        cnt = await radios.count()
        for i in range(min(cnt, 20)):
            r = radios.nth(i)
            try:
                if await r.is_checked():
                    radio_checked = True
                    break
            except Exception:
                continue
    except Exception:
        pass

    try:
        await ttn_input_probe.wait_for(state="visible", timeout=min(2500, SUP3_TIMEOUT_MS))
        enabled = await ttn_input_probe.is_enabled()
    except Exception:
        enabled = False

    if radio_checked or enabled:
        return True
    raise StageError(stage, "Own TTN option not selectable")


async def _fill_ttn_input(page, ttn: str) -> bool:
    stage = "checkout_ttn"
    selectors_tried = [
        "dt.form-head[Вказати номер накладної] -> following-sibling dd input",
        "input[name*='ttn']",
    ]
    head = page.locator("dt.form-head", has_text="Вказати номер накладної").first
    inp = head.locator("xpath=following-sibling::dd[1]//input").first
    if await inp.count() == 0:
        inp = page.locator("input[name*='ttn' i], input[id*='ttn' i]").first
    try:
        await inp.wait_for(state="visible", timeout=min(5000, SUP3_TIMEOUT_MS))
        if not await inp.is_enabled():
            raise StageError(stage, "TTN input not found", {"url": page.url or SUP3_BASE_URL, "selectors_tried": selectors_tried})
        await inp.click(timeout=min(3000, SUP3_TIMEOUT_MS))
        await inp.fill(ttn, timeout=min(3000, SUP3_TIMEOUT_MS))
        try:
            await inp.dispatch_event("input")
            await inp.dispatch_event("change")
        except Exception:
            pass
        await page.wait_for_timeout(120)
        current = await inp.input_value(timeout=min(2000, SUP3_TIMEOUT_MS))
    except StageError:
        raise
    except Exception:
        raise StageError(stage, "TTN input not found", {"url": page.url or SUP3_BASE_URL, "selectors_tried": selectors_tried})

    if ttn not in (current or ""):
        raise StageError(stage, "TTN input not found", {"url": page.url or SUP3_BASE_URL, "selectors_tried": selectors_tried})
    print(f"[SUP3] checkout_ttn: TTN set => {ttn}")
    return True


async def _checkout_ttn_stage(page) -> dict:
    stage = "checkout_ttn"
    if not SUP3_TTN:
        raise StageError(stage, "SUP3_TTN is required")

    already, checks = await _is_logged_in(page)
    if not already:
        raise StageError(stage, "Not authorized: session is not logged in.", {"checks": checks})

    await _assert_cart_not_empty(page)
    checkout_clicked = await _click_checkout_button(page)
    if "/checkout/" not in (page.url or ""):
        raise StageError(stage, "Did not reach checkout", {"url": page.url or SUP3_BASE_URL})

    await _best_effort_close_popups(page)
    radio_selected = await _ensure_own_ttn_selected(page)
    ttn_set = await _fill_ttn_input(page, SUP3_TTN)
    if not SUP3_NP_API_KEY:
        raise StageError("attach_invoice_label", "label_missing", {"reason": "NP API key is not configured"})
    try:
        label_path = _download_np_label_sup3(SUP3_LABELS_DIR, SUP3_TTN, SUP3_NP_API_KEY)
    except Exception as e:
        raise StageError("attach_invoice_label", "label_missing", {"error": str(e), "ttn": SUP3_TTN}) from e
    try:
        label_size = label_path.stat().st_size
    except Exception:
        label_size = 0
    print(f"[SUP3] label downloaded: {label_path} size={label_size}")
    attach_info = await _attach_invoice_label_file(page, label_path)
    supplier_order_number = await _submit_checkout_order_and_get_number(page)

    if (os.getenv("SUP3_DEBUG_PAUSE") or "").strip() == "1":
        await page.wait_for_timeout(25000)

    return {
        "ok": True,
        "stage": stage,
        "url": page.url or SUP3_BASE_URL,
        "checkout_clicked": checkout_clicked,
        "radio_selected": bool(radio_selected),
        "ttn_set": bool(ttn_set),
        "label_attached": True,
        "label_file": str(label_path),
        "attach_invoice_label": attach_info,
        "supplier_order_number": str(supplier_order_number),
    }


async def _login_trigger(page):
    # Prefer clickable ancestor with text "Вхід", not nested span only.
    selectors = [
        "a.userbar__button:has-text('Вхід')",
        "button:has-text('Вхід')",
        "a:has-text('Вхід')",
        "[role='button']:has-text('Вхід')",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                return loc
        except Exception:
            continue
    return page.get_by_text("Вхід", exact=True).first


async def _is_logged_in(page) -> tuple[bool, dict]:
    await page.goto(SUP3_BASE_URL, wait_until="domcontentloaded", timeout=SUP3_TIMEOUT_MS)
    await page.wait_for_timeout(150)
    await _best_effort_close_popups(page)
    login_visible = False
    logout_visible = False
    stable_no_login_hits = 0

    login_candidates = [
        page.locator("a:has-text('Вхід'), button:has-text('Вхід'), [role='button']:has-text('Вхід')"),
    ]
    logout_candidates = [
        page.locator("a:has-text('Вихід'), button:has-text('Вихід'), [role='button']:has-text('Вихід')"),
    ]

    # DSN header can render/update with a delay; do a short polling pass instead of one snapshot.
    deadline = asyncio.get_running_loop().time() + min(3.5, SUP3_TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        login_visible = False
        logout_visible = False

        for loc in login_candidates:
            try:
                if await loc.count() > 0 and await loc.first.is_visible():
                    login_visible = True
                    break
            except Exception:
                continue

        for loc in logout_candidates:
            try:
                if await loc.count() > 0 and await loc.first.is_visible():
                    logout_visible = True
                    break
            except Exception:
                continue

        if logout_visible:
            return True, {"login_visible": login_visible, "logout_visible": logout_visible}

        if not login_visible:
            stable_no_login_hits += 1
            if stable_no_login_hits >= 2:
                return True, {"login_visible": login_visible, "logout_visible": logout_visible}
        else:
            stable_no_login_hits = 0

        await page.wait_for_timeout(250)

    # best-effort final criteria:
    # a) no visible "Вхід"
    # b) visible "Вихід" if present
    already = (not login_visible) or logout_visible
    return already, {"login_visible": login_visible, "logout_visible": logout_visible}


async def _open_login_modal(page) -> None:
    trigger = await _login_trigger(page)
    try:
        await trigger.wait_for(state="visible", timeout=SUP3_TIMEOUT_MS)
    except Exception as e:
        raise StageError("login", 'Login trigger "Вхід" not found/visible', {}) from e

    try:
        await trigger.click(timeout=SUP3_TIMEOUT_MS)
    except Exception:
        # fallback: click nested text if ancestor click fails
        alt = page.get_by_text("Вхід", exact=True).first
        await alt.click(timeout=SUP3_TIMEOUT_MS)

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(4000, SUP3_TIMEOUT_MS))
    except PWTimeoutError:
        pass

    overlay = page.locator("div#modal-overlay.overlay").first
    modal = page.locator("section#sign-in, section.popup__login").filter(has=page.locator("form#login_form_id")).first
    form = modal.locator("form#login_form_id").first
    email_input = form.locator('input[name="user[email]"]').first
    pass_input = form.locator('input[name="user[pass]"]').first

    # Support both modal login and dedicated login page.
    deadline = asyncio.get_running_loop().time() + (SUP3_TIMEOUT_MS / 1000.0)
    saw_overlay = False
    saw_modal = False
    saw_form = False
    saw_fields = False
    form_visible = False
    while asyncio.get_running_loop().time() < deadline:
        try:
            saw_overlay = await overlay.is_visible()
        except Exception:
            saw_overlay = False
        try:
            saw_modal = await modal.is_visible()
        except Exception:
            saw_modal = False
        try:
            saw_form = await form.count() > 0
        except Exception:
            saw_form = False
        form_visible = await _safe_is_visible(form)
        try:
            saw_fields = (
                (await email_input.count() > 0)
                and (await pass_input.count() > 0)
                and await email_input.is_visible()
                and await pass_input.is_visible()
            )
        except Exception:
            saw_fields = False

        # Accept earlier once overlay + form are there; _submit_login_form will do strict visibility checks.
        if (saw_overlay and (saw_form or bool(form_visible))) or saw_fields or (saw_modal and saw_form):
            return
        await page.wait_for_timeout(120)

    raise StageError(
        "login",
        "Login modal/page did not appear",
        {
            "overlay_visible": saw_overlay,
            "modal_visible": saw_modal,
            "form_present": saw_form,
            "form_visible": form_visible,
            "fields_visible": saw_fields,
        },
    )


async def _submit_login_form(page) -> None:
    modal_selector = "section#sign-in, div#modal-overlay, section.popup__login"
    form_selector = "form#login_form_id"
    email_selector = 'input[name="user[email]"]'
    pass_selector = 'input[name="user[pass]"]'
    submit_selector = 'input[type="submit"][value="Увійти"]'
    overlay_selector = "div#modal-overlay.overlay"
    modal = page.locator(modal_selector).filter(has=page.locator(form_selector)).first
    form = modal.locator(form_selector).first
    email_input = form.locator(email_selector).first
    pass_input = form.locator(pass_selector).first
    submit_btn = form.locator(submit_selector).first

    print(f"[SUP3] login selectors: modal={modal_selector!r} form={form_selector!r}")
    print(f"[SUP3] login selectors: email={email_selector!r} pass={pass_selector!r} submit={submit_selector!r}")

    async def _build_login_diag(extra: dict | None = None) -> dict:
        overlay = page.locator(overlay_selector).first
        section_form = page.locator("section#sign-in form#login_form_id")
        details = {
            "selectors_used": {
                "modal": modal_selector,
                "form": form_selector,
                "email": email_selector,
                "password": pass_selector,
                "submit": submit_selector,
                "overlay": overlay_selector,
            },
            "diagnostics": {
                "section_signin_form": await _locator_diag(section_form),
                "email": await _locator_diag(email_input),
                "password": await _locator_diag(pass_input),
                "submit": await _locator_diag(submit_btn),
            },
            "overlay_visible": await _safe_is_visible(overlay),
            "modal_visible": await _safe_is_visible(modal),
            "url": page.url or SUP3_BASE_URL,
        }
        form_outer = await _safe_outer_html_snippet(form)
        if form_outer:
            details["form_outer_html"] = form_outer
        email_outer = await _safe_outer_html_snippet(email_input)
        if email_outer:
            details["email_input_outer_html"] = email_outer
        pass_outer = await _safe_outer_html_snippet(pass_input)
        if pass_outer:
            details["password_input_outer_html"] = pass_outer
        submit_outer = await _safe_outer_html_snippet(submit_btn)
        if submit_outer:
            details["submit_outer_html"] = submit_outer
        if extra:
            details.update(extra)
        return details

    async def _raise_login_stage_error(message: str, extra: dict | None = None) -> None:
        screenshot_path = ""
        try:
            shot = _failure_screenshot_path()
            await page.screenshot(path=str(shot), full_page=True)
            screenshot_path = str(shot)
        except Exception:
            pass
        details = await _build_login_diag(extra)
        if screenshot_path:
            details["screenshot"] = screenshot_path
        raise StageError("login", message, details)

    try:
        await modal.wait_for(state="visible", timeout=SUP3_TIMEOUT_MS)
        await form.wait_for(state="visible", timeout=SUP3_TIMEOUT_MS)
        await email_input.wait_for(state="visible", timeout=SUP3_TIMEOUT_MS)
        await pass_input.wait_for(state="visible", timeout=SUP3_TIMEOUT_MS)
        await submit_btn.wait_for(state="visible", timeout=SUP3_TIMEOUT_MS)
    except Exception as e:
        await _raise_login_stage_error("Login form fields not visible")
        raise StageError("login", "Login form fields not visible", {}) from e

    email_val = ""
    pass_val = ""

    async def _fill_and_verify(locator, value: str, *, field_name: str, expect_contains_at: bool = False) -> str:
        nonlocal email_val, pass_val

        for attempt in (1, 2):
            try:
                await locator.click(timeout=SUP3_TIMEOUT_MS)
                await locator.fill(value, timeout=SUP3_TIMEOUT_MS)
                await locator.dispatch_event("input")
                await locator.dispatch_event("change")
                await page.wait_for_timeout(120)
                current = await locator.input_value(timeout=SUP3_TIMEOUT_MS)
            except Exception:
                current = ""

            if field_name == "email":
                email_val = current
            elif field_name == "password":
                pass_val = current

            if current and (not expect_contains_at or "@" in current):
                if attempt == 2:
                    print(f"[SUP3] login {field_name}: value kept after retry")
                return current

            print(f"[SUP3] login {field_name}: value mismatch after fill attempt={attempt}")
            if attempt == 1:
                await page.wait_for_timeout(150)

        extra = {"email_value_after_type": email_val}
        if pass_val:
            extra["password_field_nonempty"] = True
        msg = "Email field did not keep value" if field_name == "email" else "Password field did not keep value"
        await _raise_login_stage_error(msg, extra=extra)
        return ""

    print("[SUP3] login input: fill email")
    await _fill_and_verify(email_input, SUP3_LOGIN_EMAIL, field_name="email", expect_contains_at=True)
    print("[SUP3] login input: fill password")
    await _fill_and_verify(pass_input, SUP3_LOGIN_PASSWORD, field_name="password", expect_contains_at=False)

    try:
        print("[SUP3] login submit: click input[type=submit][value='Увійти']")
        await submit_btn.click(timeout=SUP3_TIMEOUT_MS)
    except Exception as e:
        extra = {"email_value_after_type": email_val}
        if pass_val:
            extra["password_field_nonempty"] = True
        await _raise_login_stage_error('Submit input "Увійти" not clickable', extra=extra)
        raise StageError("login", 'Submit input "Увійти" not clickable', {}) from e


async def _wait_login_success(page) -> None:
    overlay = page.locator("div#modal-overlay.overlay").first
    login_text = page.locator("a:has-text('Вхід'), button:has-text('Вхід'), [role='button']:has-text('Вхід')").first
    logout_text = page.locator("a:has-text('Вихід'), button:has-text('Вихід'), [role='button']:has-text('Вихід')").first

    deadline = asyncio.get_running_loop().time() + (SUP3_TIMEOUT_MS / 1000.0)
    last_overlay_visible = None
    last_login_visible = None
    last_logout_visible = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            last_overlay_visible = await overlay.is_visible()
        except Exception:
            last_overlay_visible = False
        try:
            last_login_visible = (await login_text.count() > 0) and await login_text.is_visible()
        except Exception:
            last_login_visible = False
        try:
            last_logout_visible = (await logout_text.count() > 0) and await logout_text.is_visible()
        except Exception:
            last_logout_visible = False

        overlay_ok = not bool(last_overlay_visible)
        auth_ok = bool(last_logout_visible) or (not bool(last_login_visible))
        if overlay_ok and auth_ok:
            return

        await page.wait_for_timeout(150)

    raise StageError(
        "login",
        "Login success criteria not met (overlay/login header state).",
        {
            "overlay_visible": last_overlay_visible,
            "login_visible": last_login_visible,
            "logout_visible": last_logout_visible,
        },
    )


async def _ensure_logged_in(page) -> dict:
    already, checks = await _is_logged_in(page)
    if already and not SUP3_FORCE_LOGIN:
        return {"reused_session": True, "checks": checks}
    if not SUP3_FORCE_LOGIN:
        # One extra probe prevents false negatives when DSN header updates slowly.
        try:
            await page.wait_for_timeout(500)
            already_retry, checks_retry = await _is_logged_in(page)
            if already_retry:
                return {"reused_session": True, "checks": checks_retry}
        except Exception:
            pass
    if not SUP3_LOGIN_EMAIL or not SUP3_LOGIN_PASSWORD:
        raise StageError(
            "login",
            "SUP3_LOGIN_EMAIL/SUP3_LOGIN_PASSWORD are required when login is needed.",
            {"checks": checks, "force_login": SUP3_FORCE_LOGIN},
        )

    await page.goto(SUP3_BASE_URL, wait_until="domcontentloaded", timeout=SUP3_TIMEOUT_MS)
    await page.wait_for_timeout(150)
    await _best_effort_close_popups(page)
    await _open_login_modal(page)
    await _submit_login_form(page)
    await _wait_login_success(page)
    return {"reused_session": False, "checks": checks}


async def _save_state(context, path: Path) -> None:
    await context.storage_state(path=str(path))


async def _run() -> tuple[bool, dict]:
    if SUP3_STAGE not in {"login", "run", "add_items", "clear_cart", "checkout_ttn"}:
        raise RuntimeError(
            f"Unsupported SUP3_STAGE={SUP3_STAGE!r}. Expected 'login', 'run', 'add_items', 'clear_cart' or 'checkout_ttn'."
        )
    if SUP3_STAGE == "login":
        if not SUP3_LOGIN_EMAIL:
            raise RuntimeError("SUP3_LOGIN_EMAIL (or SUP3_EMAIL) is required")
        if not SUP3_LOGIN_PASSWORD:
            raise RuntimeError("SUP3_LOGIN_PASSWORD (or SUP3_PASSWORD) is required")
    if SUP3_STAGE == "add_items":
        _ = _parse_sup3_items()
    if SUP3_STAGE == "checkout_ttn" and not SUP3_TTN:
        raise RuntimeError("SUP3_TTN is required for SUP3_STAGE=checkout_ttn")

    state_path = _state_path()
    browser = None
    context = None
    page = None
    browser_owner = True
    context_owner = True
    stage = "login"
    error_screenshot = ""
    base_url_for_err = SUP3_BASE_URL
    clear_basket_result = None
    add_items_result = None
    clear_cart_result = None
    checkout_ttn_result = None

    try:
        async with async_playwright() as p:
            if SUP3_USE_CDP:
                if not SUP3_CDP_URL:
                    raise RuntimeError("SUP3_CDP_URL is required when SUP3_USE_CDP=1")
                browser = await p.chromium.connect_over_cdp(SUP3_CDP_URL)
                browser_owner = False
                if browser.contexts:
                    context = browser.contexts[0]
                    context_owner = False
                else:
                    context = await browser.new_context()
            else:
                browser = await p.chromium.launch(headless=SUP3_HEADLESS)
                if state_path.exists() and not SUP3_FORCE_LOGIN:
                    context = await browser.new_context(storage_state=str(state_path))
                else:
                    context = await browser.new_context()

            page = await context.new_page()
            if SUP3_STAGE == "add_items":
                stage = "add_items"
                add_items_result = await _add_items(page)
                await _save_state(context, state_path)
                return True, add_items_result
            if SUP3_STAGE == "checkout_ttn":
                stage = "checkout_ttn"
                checkout_ttn_result = await _checkout_ttn_stage(page)
                await _save_state(context, state_path)
                return True, checkout_ttn_result

            login_info = await _ensure_logged_in(page)
            if SUP3_STAGE == "clear_cart":
                stage = "clear_cart"
                clear_cart_result = await _clear_cart_stage(page)
                await _save_state(context, state_path)
                return True, clear_cart_result
            if SUP3_CLEAR_BASKET:
                clear_basket_result = await _clear_basket(page)

            if SUP3_STAGE == "run" and SUP3_ITEMS.strip():
                add_items_result = await _add_items(page)
            if SUP3_STAGE == "run" and SUP3_TTN:
                stage = "checkout_ttn"
                checkout_ttn_result = await _checkout_ttn_stage(page)
                stage = "login"

            await _save_state(context, state_path)

            if SUP3_STAGE == "run":
                # TODO: finalize checkout submit/confirm after checkout_ttn+attach_label flow.
                pass

            result = {
                "ok": True,
                "stage": "login",
                "url": page.url or SUP3_BASE_URL,
                "storage_state": str(state_path),
                "details": login_info,
            }
            if clear_basket_result is not None:
                result["details"]["clear_basket"] = clear_basket_result
            if add_items_result is not None:
                result["details"]["add_items"] = add_items_result
            if checkout_ttn_result is not None:
                result["details"]["checkout_ttn"] = checkout_ttn_result
            return True, result
    except StageError as e:
        if page is not None:
            try:
                shot = _failure_screenshot_path()
                await page.screenshot(path=str(shot), full_page=True)
                error_screenshot = str(shot)
            except Exception:
                pass
        await _debug_pause_if_needed(page)
        payload = {
            "ok": False,
            "stage": e.stage or stage,
            "error": str(e),
            "url": page.url if page is not None else base_url_for_err,
        }
        if error_screenshot:
            payload["screenshot"] = error_screenshot
        if e.details:
            payload["details"] = e.details
        return False, payload
    except Exception as e:
        if page is not None:
            try:
                shot = _failure_screenshot_path()
                await page.screenshot(path=str(shot), full_page=True)
                error_screenshot = str(shot)
            except Exception:
                pass
        await _debug_pause_if_needed(page)
        payload = {
            "ok": False,
            "stage": stage,
            "error": str(e),
            "url": page.url if page is not None else base_url_for_err,
        }
        if error_screenshot:
            payload["screenshot"] = error_screenshot
        return False, payload
    finally:
        try:
            if page is not None:
                await page.close()
        except Exception:
            pass
        try:
            if context is not None and context_owner:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None and browser_owner:
                await browser.close()
        except Exception:
            pass


def main() -> int:
    try:
        ok, payload = asyncio.run(_run())
    except Exception as e:
        payload = {"ok": False, "stage": "login", "error": str(e), "url": SUP3_BASE_URL}
        ok = False

    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
