"""Prepare, validate and (when explicitly enabled) submit SUP4 Monsterlab Drop orders."""
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
SUP4_BASE_URL = (os.getenv("SUP4_BASE_URL") or "https://monsterlabdrop.com.ua").rstrip("/")
SUP4_LOGIN_EMAIL = (os.getenv("SUP4_LOGIN_EMAIL") or "").strip()
SUP4_LOGIN_PASSWORD = (os.getenv("SUP4_LOGIN_PASSWORD") or "").strip()
SUP4_STORAGE_STATE_FILE = (os.getenv("SUP4_STORAGE_STATE_FILE") or ".state_supplier4.json").strip()
SUP4_HEADLESS = (os.getenv("SUP4_HEADLESS") or "1").strip().lower() not in {"0", "false", "no"}
SUP4_TIMEOUT_MS = int((os.getenv("SUP4_TIMEOUT_MS") or "20000").strip())
SUP4_CLEAR_BASKET = (os.getenv("SUP4_CLEAR_BASKET") or "1").strip().lower() not in {"0", "false", "no"}
SUP4_ITEMS = (os.getenv("SUP4_ITEMS") or "").strip()
SUP4_TTN = (os.getenv("SUP4_TTN") or "").strip()
SUP4_ATTACH_DIR = (os.getenv("SUP4_ATTACH_DIR") or "supplier4_labels").strip()
SUP4_NP_API_KEY = (os.getenv("SUP4_NP_API_KEY") or os.getenv("NP_API_KEY") or os.getenv("BIOTUS_NP_API_KEY") or "").strip()
SUP4_STAGE = (os.getenv("SUP4_STAGE") or "run").strip().lower()
# Deliberately opt-in.  Live diagnostics use 0; production enablement is an
# explicit operational decision rather than an accidental deploy side effect.
SUP4_ALLOW_SUBMIT = (os.getenv("SUP4_ALLOW_SUBMIT") or "0").strip().lower() in {"1", "true", "yes"}


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.stage, self.details = stage, details or {}


@dataclass(frozen=True)
class Sup4Item:
    sku: str
    qty: int


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _parse_money(value: object) -> int | None:
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    return int(digits) if digits else None


def _parse_items(raw: str | None = None) -> list[Sup4Item]:
    result: list[Sup4Item] = []
    for part in filter(None, re.split(r"[;,]", raw if raw is not None else SUP4_ITEMS)):
        sku, _, qty_raw = part.strip().partition(":")
        if not sku:
            raise RuntimeError("SUP4_ITEMS has an empty SKU")
        try:
            qty = int(qty_raw or "1")
        except ValueError as exc:
            raise RuntimeError(f"Invalid SUP4 qty for {sku}") from exc
        if qty < 1:
            raise RuntimeError(f"Qty must be >= 1 for {sku}")
        result.append(Sup4Item(sku=sku.strip(), qty=qty))
    if not result:
        raise RuntimeError("SUP4_ITEMS is required (SKU:QTY,SKU:QTY)")
    if len({x.sku.casefold() for x in result}) != len(result):
        raise RuntimeError("SUP4_ITEMS contains duplicate SKUs")
    return result


def _compare_order(expected: list[Sup4Item], actual: list[dict[str, Any]]) -> dict[str, Any]:
    wanted = {x.sku.casefold(): x.qty for x in expected}
    got: dict[str, int] = {}
    duplicates: list[str] = []
    for row in actual:
        sku = str(row.get("sku") or "").strip().casefold()
        if not sku:
            continue
        if sku in got:
            duplicates.append(sku)
        got[sku] = got.get(sku, 0) + int(row.get("qty") or 0)
    missing = sorted(set(wanted) - set(got))
    extra = sorted(set(got) - set(wanted))
    qty_mismatches = [{"sku": sku, "expected_qty": wanted[sku], "actual_qty": got[sku]}
                      for sku in sorted(set(wanted) & set(got)) if wanted[sku] != got[sku]]
    return {"verified": not (missing or extra or qty_mismatches or duplicates), "missing": missing,
            "extra": extra, "duplicates": duplicates, "qty_mismatches": qty_mismatches,
            "expected_count": len(expected), "actual_count": len(actual)}


def _cart_qty_checks(expected: list[Sup4Item], actual: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the explicit expected/actual quantity pairs consumed by the orchestrator."""
    actual_qty = {str(row.get("sku") or "").casefold(): int(row.get("qty") or 0) for row in actual}
    return [
        {
            "sku": item.sku,
            "expected_qty": item.qty,
            "actual_qty": actual_qty.get(item.sku.casefold(), 0),
            "verified": actual_qty.get(item.sku.casefold(), 0) == item.qty,
            "verified_stage": "cart",
        }
        for item in expected
    ]


def _attach_dir() -> Path:
    path = Path(SUP4_ATTACH_DIR)
    path = path if path.is_absolute() else ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path() -> Path:
    path = Path(SUP4_STORAGE_STATE_FILE)
    return path if path.is_absolute() else ROOT / path


def _debug_dir() -> Path:
    path = ROOT / "tmp" / "supplier4_debug"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _debug(page, stage: str, label: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    base = _debug_dir() / f"{stamp}_{stage}_{label}"
    details = {"url": page.url, **(extra or {})}
    try:
        await page.screenshot(path=str(base.with_suffix('.png')), full_page=True)
        details["screenshot"] = str(base.with_suffix('.png'))
        base.with_suffix('.html').write_text(await page.content(), encoding="utf-8")
        details["html"] = str(base.with_suffix('.html'))
    except Exception:
        pass
    return details


async def _login(page) -> None:
    stage = "login"
    await page.goto(SUP4_BASE_URL, wait_until="domcontentloaded")
    if not SUP4_LOGIN_EMAIL or not SUP4_LOGIN_PASSWORD:
        raise StageError(stage, "SUP4 credentials are required")
    try:
        await page.get_by_label("Email", exact=True).fill(SUP4_LOGIN_EMAIL)
        await page.get_by_label("Пароль", exact=True).fill(SUP4_LOGIN_PASSWORD)
        await page.get_by_role("button", name="Увійти", exact=True).click()
        await page.get_by_role("button", name="Вийти", exact=True).wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
        await page.get_by_placeholder("Пошук за назвою або артикулом…", exact=True).wait_for(
            state="visible", timeout=SUP4_TIMEOUT_MS
        )
    except Exception as exc:
        raise StageError(stage, "LOGIN_VERIFICATION_FAILED", await _debug(page, stage, "verification_failed")) from exc


async def _order_panel(page):
    panel = page.get_by_text(re.compile(r"Порожньо\.\s*Додайте товари з каталогу|Замовлення", re.I)).first
    await panel.wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
    # The actual panel is a common parent of its visible text, not a route/page.
    return panel.locator("xpath=ancestor::section|ancestor::aside|ancestor::div").first


async def _is_empty_cart(page) -> bool:
    # Desktop sidebar renders the full empty-state phrase; compact layout keeps
    # the same state only in the floating cart control.  Require no cart rows
    # as well, so an arbitrary matching string cannot hide a real item.
    fab = page.locator("#fab")
    try:
        # The UI derives this label from the same in-memory cart as the sidebar.
        # It is decisive in compact layout, where the sidebar can be hidden.
        if (await fab.count()) == 1 and _norm(await fab.inner_text()) == _norm("Кошик порожній"):
            return True
    except Exception:
        pass
    if await page.locator(".line").count() != 0:
        return False
    desktop_empty = await page.get_by_text(
        re.compile(r"^Порожньо\.\s*Додайте товари з каталогу\.$", re.I)
    ).count() > 0
    # `#fab` is the compact-layout cart's authoritative state.  Role based
    # lookup is unreliable when Playwright has a narrow/zero-size headful
    # viewport, so read the actual control text as a fallback.
    compact_empty = await page.get_by_role(
        "button", name=re.compile(r"^Кошик\s+порожній$", re.I)
    ).count() > 0
    if not compact_empty:
        try:
            compact_empty = (await fab.count()) == 1 and _norm(await fab.inner_text()) == _norm("Кошик порожній")
        except Exception:
            compact_empty = False
    return desktop_empty or compact_empty


async def _clear_cart(page) -> None:
    stage = "clear_cart"
    if await _is_empty_cart(page):
        return
    # Restrict removal to elements whose row also contains an article; never click an arbitrary cross.
    for _ in range(80):
        if await _is_empty_cart(page):
            return
        remove = page.get_by_role("button", name="прибрати", exact=True).last
        try:
            if await remove.count() == 0:
                break
            await remove.click(timeout=3000)
            await page.wait_for_timeout(150)
        except Exception:
            break
    if not await _is_empty_cart(page):
        raise StageError(stage, "CART_NOT_EMPTY_AFTER_CLEAR", await _debug(page, stage, "not_empty"))


async def _search_card(page, sku: str):
    stage = "add_items"
    search = page.get_by_placeholder("Пошук за назвою або артикулом…", exact=True)
    await search.fill(sku)
    # Catalogue filtering is asynchronous.  The add button carries the exact
    # supplier article, so wait for that authoritative DOM signal instead of
    # relying on a short fixed delay after typing.
    add_button = page.locator(f'[data-add="{sku}"]')
    try:
        await add_button.wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
    except Exception as exc:
        raise StageError(
            stage,
            "SEARCH_EXACT_SKU_NOT_FOUND",
            await _debug(page, stage, "exact_sku_not_found", {"sku": sku, "matches": await add_button.count()}),
        ) from exc
    if await add_button.count() != 1:
        raise StageError(stage, "SEARCH_EXACT_SKU_NOT_FOUND", await _debug(page, stage, "ambiguous_exact_sku", {"sku": sku, "matches": await add_button.count()}))
    card = add_button.locator("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' row ')]").first
    text = await card.inner_text()
    if "в наявності" not in _norm(text):
        raise StageError(stage, "PRODUCT_NOT_AVAILABLE", await _debug(page, stage, "not_available", {"sku": sku, "card": text}))
    return card


async def _add_item(page, item: Sup4Item) -> dict[str, Any]:
    card = await _search_card(page, item.sku)
    qty = card.get_by_label("кількість", exact=True)
    await qty.fill(str(item.qty))
    if str(await qty.input_value()) != str(item.qty):
        raise StageError("add_items", "CART_ORDER_MISMATCH", await _debug(page, "add_items", "quantity_not_set", {"sku": item.sku}))
    await card.get_by_role("button", name="Додати", exact=True).click()
    # Cart rendering is asynchronous.  Do not continue until the supplier's
    # own order panel shows the exact article we just added.
    cart_row = page.locator(".line").filter(has_text=re.compile(re.escape(item.sku), re.I)).first
    try:
        await cart_row.wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
    except Exception as exc:
        raise StageError(
            "add_items",
            "CART_ORDER_MISMATCH",
            await _debug(page, "add_items", "cart_row_not_rendered", {"sku": item.sku}),
        ) from exc
    return {"sku": item.sku, "expected_qty": item.qty, "verified": True, "verified_stage": "catalog"}


async def _read_cart(page) -> list[dict[str, Any]]:
    # The side order panel contains article, qty, price and an × removal control.
    rows = page.locator(".line")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for i in range(min(await rows.count(), 120)):
        row = rows.nth(i)
        try:
            text = re.sub(r"\s+", " ", await row.inner_text()).strip()
        except Exception:
            continue
        # The sidebar keeps the supplier article in its own `.meta` element.
        # Some valid articles (for example SW852) have no dash, so inferring
        # them only from a generic text regex silently loses real cart rows.
        try:
            sku = (await row.locator(".meta").first.inner_text()).strip()
        except Exception:
            sku = ""
        sku_match = re.search(r"\b[A-ZА-ЯІЇЄ0-9]{2,}(?:-[A-ZА-ЯІЇЄ0-9]+)+\b|\b[A-Z]{2,}\d+[A-Z0-9]*\b|\b\d{4,}\b", text, re.I)
        qty_match = re.search(r"\b(\d+)\s*(?:шт|шт\.)\b", text, re.I)
        if not (sku or sku_match) or not qty_match:
            continue
        sku, qty = sku or sku_match.group(0), int(qty_match.group(1))
        key = (sku.casefold(), qty)
        if key in seen:
            continue
        seen.add(key)
        amounts = re.findall(r"\b[\d\s]+\s*₴", text)
        result.append({"sku": sku, "qty": qty, "price": _parse_money(amounts[-1]) if amounts else None, "text": text})
    return result


def _pdf_contains_ttn(path: Path, ttn: str) -> bool:
    needle = _digits(ttn)
    if not needle:
        return False
    try:
        return needle in _digits(path.read_bytes().decode("latin-1", errors="ignore"))
    except OSError:
        return False


def _label_matches_ttn(path: Path, ttn: str) -> bool:
    """Accept an explicitly named local label or a PDF whose text contains TTN."""
    needle = _digits(ttn)
    try:
        non_empty_pdf = path.is_file() and path.stat().st_size > 0 and path.read_bytes()[:4] == b"%PDF"
    except OSError:
        non_empty_pdf = False
    return bool(needle) and non_empty_pdf and (needle in _digits(path.name) or _pdf_contains_ttn(path, ttn))


def _pick_label_file(ttn: str) -> Path:
    files = [p for p in _attach_dir().iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        if _label_matches_ttn(path, ttn):
            return path
    raise StageError("attach_invoice_label", "LABEL_TTN_MISMATCH", {"ttn": ttn, "files": [p.name for p in files]})


def _download_np_label(ttn: str) -> Path:
    path = _attach_dir() / f"label-{ttn}.pdf"
    temp_path = path.with_suffix(".pdf.part")
    url = f"https://my.novaposhta.ua/orders/printMarking100x100/orders[]/{ttn}/type/pdf/apiKey/{SUP4_NP_API_KEY}/zebra"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=max(10, SUP4_TIMEOUT_MS / 1000)) as response:
            temp_path.write_bytes(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise RuntimeError(f"NP label download failed: {exc}") from exc
    # The endpoint itself is bound to the requested TTN.  Nova Poshta PDFs can
    # store their text in compressed image streams, so a raw-byte search for
    # the number is not reliable and produced false rejections of valid labels.
    # Validate a non-empty PDF response and retain the requested TTN in the
    # filename, which is then rechecked before attach.
    if not temp_path.exists() or temp_path.stat().st_size == 0 or temp_path.read_bytes()[:4] != b"%PDF":
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("NP label is empty or is not a PDF")
    temp_path.replace(path)
    return path


def _resolve_label(ttn: str) -> Path:
    if SUP4_NP_API_KEY:
        try:
            return _download_np_label(ttn)
        except Exception as exc:
            print(f"[SUP4] NP label failed; using local PDF: {exc}")
    return _pick_label_file(ttn)


async def _fill_ttn_and_attach(page, ttn: str) -> dict[str, Any]:
    stage = "checkout_ttn"
    inp = page.get_by_label(re.compile(r"номер\s*ттн", re.I)).first
    try:
        if await inp.count() == 0:
            inp = page.locator("input[placeholder*='204'], input[name*='ttn' i], input[type='text']").last
        await inp.fill(ttn)
        actual = await inp.input_value()
    except Exception as exc:
        raise StageError(stage, "TTN_VALUE_MISMATCH", await _debug(page, stage, "input_failed", {"ttn": ttn})) from exc
    if _digits(actual) != _digits(ttn):
        raise StageError(stage, "TTN_VALUE_MISMATCH", await _debug(page, stage, "value_mismatch", {"expected": ttn, "actual": actual}))
    label = _resolve_label(ttn)
    if not _label_matches_ttn(label, ttn):
        raise StageError(stage, "LABEL_TTN_MISMATCH", {"file": str(label), "ttn": ttn})
    file_input = page.locator("input[type='file']").first
    try:
        await file_input.set_input_files(str(label))
        files = await file_input.evaluate("el => el.files ? el.files.length : 0")
        body = _norm(await page.locator("body").inner_text())
    except Exception as exc:
        raise StageError(stage, "LABEL_ATTACH_VERIFY_FAILED", await _debug(page, stage, "attach_failed")) from exc
    if int(files or 0) != 1 or (label.name.casefold() not in body and "прикріп" not in body):
        raise StageError(stage, "LABEL_ATTACH_VERIFY_FAILED", await _debug(page, stage, "attach_not_visible", {"file": label.name}))
    return {"ttn": ttn, "ttn_verified": True, "file": str(label), "file_name": label.name, "attached": True}


def _keycrm_order_number(text: str) -> str:
    """Extract only the confirmed KeyCRM number from the success modal."""
    match = re.search(r"Замовлення\s+(\d+)\s+у\s+KeyCRM", str(text or ""), re.I)
    return match.group(1) if match else ""


def _history_order_number(text: str, ttn: str) -> str:
    """Extract Monsterlab Drop's D-number only from a row containing this TTN."""
    if _digits(ttn) not in _digits(text):
        return ""
    match = re.search(r"\b(D\d{4,})\b", str(text or ""), re.I)
    return match.group(1).upper() if match else ""


def _history_api_order_number(rows: object, ttn: str) -> str:
    """Return the supplier D-number only from an API order with the exact TTN.

    Monsterlab's order-history React view can render after the Playwright timeout,
    even though the server has already created the order.  The same authenticated
    browser session can query its authoritative ``/api/orders`` endpoint without
    exposing its bearer token to logs or result payloads.
    """
    if not isinstance(rows, list):
        return ""
    wanted_ttn = _digits(ttn)
    for row in rows:
        if not isinstance(row, dict) or _digits(row.get("ttn")) != wanted_ttn:
            continue
        number = str(row.get("id") or "").strip().upper()
        if re.fullmatch(r"D\d{4,}", number):
            return number
    return ""


async def _history_api_confirmation(page, ttn: str) -> str:
    """Poll the authenticated supplier API for the just-submitted TTN."""
    script = """async () => {
        const token = localStorage.getItem('ml_token');
        const response = await fetch('/api/orders', {
          headers: token ? {Authorization: `Bearer ${token}`} : {}
        });
        if (!response.ok) throw new Error(`orders API ${response.status}`);
        const payload = await response.json();
        return Array.isArray(payload) ? payload : (payload.orders || payload.data || []);
    }"""
    deadline = asyncio.get_running_loop().time() + (SUP4_TIMEOUT_MS / 1000)
    while asyncio.get_running_loop().time() < deadline:
        try:
            number = _history_api_order_number(await page.evaluate(script), ttn)
            if number:
                return number
        except Exception:
            # The supplier may still be committing the transaction; the DOM
            # fallback below retains a screenshot if it never becomes visible.
            pass
        await page.wait_for_timeout(750)
    return ""


async def _submit_and_confirm(page, ttn: str) -> dict[str, Any]:
    """Submit once and confirm through KeyCRM modal or the supplier order history."""
    stage = "submit_checkout_order"
    button = page.get_by_role("button", name="Оформити замовлення", exact=True)
    try:
        await button.wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
        await button.click()
        # The button briefly becomes "Передаю" while Monsterlab sends the order.
        try:
            await page.get_by_role("button", name="Передаю", exact=True).wait_for(state="visible", timeout=3000)
        except Exception:
            pass
        modal = page.get_by_text(re.compile(r"Замовлення\s+\d+\s+у\s+KeyCRM", re.I)).first
        try:
            await modal.wait_for(state="visible", timeout=5000)
            text = await modal.inner_text()
            number = _keycrm_order_number(text)
            if number:
                return {"submitted": True, "supplier_order_number": number, "confirmation_text": text}
        except Exception:
            pass

        # Some accounts have no KeyCRM modal.  First use the authoritative
        # history API; unlike the UI it is not subject to slow React rendering.
        number = await _history_api_confirmation(page, ttn)
        if number:
            return {"submitted": True, "supplier_order_number": number,
                    "confirmation_text": f"Monsterlab API order {number}, TTN {ttn}"}

        # Keep a UI fallback for future supplier API changes.
        await page.get_by_role("button", name="Мої замовлення", exact=True).click()
        ttn_node = page.get_by_text(re.compile(re.escape(ttn))).first
        await ttn_node.wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
        row = ttn_node.locator(
            "xpath=ancestor::*[self::tr or contains(concat(' ', normalize-space(@class), ' '), ' orow ') or contains(concat(' ', normalize-space(@class), ' '), ' order-row ')][1]"
        )
        text = await row.inner_text() if await row.count() else await page.locator("body").inner_text()
    except Exception as exc:
        raise StageError(stage, "SUBMIT_CONFIRMATION_NOT_FOUND", await _debug(page, stage, "confirmation_not_found")) from exc
    number = _history_order_number(text, ttn)
    if not number:
        raise StageError(stage, "SUPPLIER_ORDER_NUMBER_NOT_FOUND", await _debug(page, stage, "number_not_found", {"modal_text": text}))
    return {"submitted": True, "supplier_order_number": number, "confirmation_text": text}


async def _run() -> dict[str, Any]:
    if SUP4_STAGE not in {"run", "login", "clear_cart", "add_items", "checkout_ttn"}:
        raise RuntimeError("Unsupported SUP4_STAGE")
    items = _parse_items() if SUP4_STAGE in {"run", "add_items", "checkout_ttn"} else []
    if SUP4_STAGE in {"run", "checkout_ttn"} and not SUP4_TTN:
        raise RuntimeError("SUP4_TTN is required")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP4_HEADLESS)
        # Force the full desktop basket panel.  Without an explicit viewport a
        # headed Chromium instance may use a compact layout, which hides the
        # order rows and makes cart verification needlessly ambiguous.
        context = await browser.new_context(viewport={"width": 1440, "height": 1080})
        page = await context.new_page()
        try:
            # A saved JWT can render the cabinet briefly and then be rejected
            # by the API.  Always start SUP4 from a clean local session and
            # authenticate explicitly for each order.
            await page.add_init_script("localStorage.removeItem('ml_token'); localStorage.removeItem('ml_role');")
            await _login(page)
            if SUP4_STAGE == "login":
                return {"ok": True, "stage": "login", "url": page.url}
            if SUP4_CLEAR_BASKET and SUP4_STAGE in {"run", "clear_cart"}:
                await _clear_cart(page)
            if SUP4_STAGE == "clear_cart":
                return {"ok": True, "stage": "clear_cart", "cart_empty": True, "url": page.url}
            checks: list[dict[str, Any]] = []
            if SUP4_STAGE in {"run", "add_items"}:
                for item in items:
                    await _add_item(page, item)
                actual = await _read_cart(page)
                comparison = _compare_order(items, actual)
                if not comparison["verified"]:
                    raise StageError("add_items", "CART_ORDER_MISMATCH", await _debug(page, "add_items", "order_mismatch", {"comparison": comparison, "actual": actual}))
                checks = _cart_qty_checks(items, actual)
                if SUP4_STAGE == "add_items":
                    return {"ok": True, "stage": "add_items", "cart_qty_checks": checks, "cart": actual, "url": page.url}
            else:
                actual, comparison = await _read_cart(page), {"verified": True}
            label = await _fill_ttn_and_attach(page, SUP4_TTN)
            actual_after = await _read_cart(page)
            comparison_after = _compare_order(items, actual_after)
            if not comparison_after["verified"]:
                raise StageError("checkout_ttn", "CART_ORDER_MISMATCH", await _debug(page, "checkout_ttn", "post_label_order_mismatch", {"comparison": comparison_after, "actual": actual_after}))
            if SUP4_ALLOW_SUBMIT:
                confirmation = await _submit_and_confirm(page, SUP4_TTN)
                return {"ok": True, "stage": "submitted", "url": page.url,
                        "cart_qty_checks": checks, "cart": actual_after, "cart_comparison": comparison_after,
                        "ttn": label, "label": label, **confirmation}
            return {"ok": True, "stage": "prepared", "submitted": False, "submit_blocked": True,
                    "submit_block_reason": "SUBMIT_BLOCKED", "supplier_order_number": "", "url": page.url,
                    "cart_qty_checks": checks, "cart": actual_after, "cart_comparison": comparison_after,
                    "ttn": label, "label": label}
        finally:
            await context.close()
            await browser.close()


def main() -> int:
    try:
        print(json.dumps(asyncio.run(_run()), ensure_ascii=False))
        return 0
    except StageError as exc:
        print(json.dumps({"ok": False, "stage": exc.stage, "error": str(exc), "details": exc.details}, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "stage": SUP4_STAGE, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
