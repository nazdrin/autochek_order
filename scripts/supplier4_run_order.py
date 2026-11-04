"""Prepare, validate and (when explicitly enabled) submit SUP4 Monsterlab Drop orders."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PyPDF2 import PdfReader, PdfWriter
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
from services.runtime_cleanup import cleanup_startup
SUP4_BASE_URL = (os.getenv("SUP4_BASE_URL") or "https://monsterlabdrop.com.ua").rstrip("/")
if re.search(r"//(?:www\.)?monsterlab\.com\.ua$", SUP4_BASE_URL, re.I):
    SUP4_BASE_URL = "https://monsterlabdrop.com.ua"
SUP4_LOGIN_EMAIL = (os.getenv("SUP4_LOGIN_EMAIL") or "").strip()
SUP4_LOGIN_PASSWORD = (os.getenv("SUP4_LOGIN_PASSWORD") or "").strip()
SUP4_STORAGE_STATE_FILE = (os.getenv("SUP4_STORAGE_STATE_FILE") or ".state_supplier4.json").strip()
SUP4_EXPECTED_ACCOUNT = (os.getenv("SUP4_EXPECTED_ACCOUNT") or "").strip()
SUP4_EXPECTED_STORE = (os.getenv("SUP4_EXPECTED_STORE") or "Старий кабінет").strip()
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


def _is_supplier_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold()
    base_host = (urlsplit(SUP4_BASE_URL).hostname or "").casefold()
    return bool(host and base_host and (host == base_host or host.endswith("." + base_host)))


def _validated_ttn(value: object) -> str:
    digits = _digits(value)
    if len(digits) != 14:
        raise RuntimeError("SUP4_TTN must contain exactly 14 digits for Nova Poshta")
    return digits


def _parse_money(value: object) -> int | None:
    """Parse Ukrainian currency into minor units (kopecks)."""
    text = re.sub(r"[^0-9,.]", "", str(value or ""))
    if not text:
        return None
    if "," in text and "." in text:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
        grouping_separator = "." if decimal_separator == "," else ","
        text = text.replace(grouping_separator, "").replace(decimal_separator, ".")
    else:
        text = text.replace(",", ".")
    try:
        return int((Decimal(text) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None


def _parse_items(raw: str | None = None) -> list[Sup4Item]:
    result: list[Sup4Item] = []
    for part in filter(None, re.split(r"[;,]", raw if raw is not None else SUP4_ITEMS)):
        sku, _, qty_raw = part.strip().partition(":")
        if not sku:
            raise RuntimeError("SUP4_ITEMS has an empty SKU")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{1,40}", sku, re.I):
            raise RuntimeError(f"Invalid SUP4 SKU: {sku}")
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


def _storage_state_path() -> Path:
    path = Path(SUP4_STORAGE_STATE_FILE)
    return path if path.is_absolute() else ROOT / path


async def _save_storage_state(context) -> Path:
    path = _storage_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(path))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _debug_dir() -> Path:
    path = ROOT / "artifacts" / "supplier4_debug"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _debug(page, stage: str, label: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = _debug_dir() / f"{stamp}_{stage}_{label}"
    artifacts: dict[str, str] = {}
    try:
        masks = [page.locator('input[type="password"]'), page.locator('input[type="email"]'),
                 page.get_by_label(re.compile(r"Номер\s*ТТН", re.I)),
                 page.get_by_text(re.compile(r"label[^\s]*\.pdf", re.I))]
        ttn_digits = _digits(SUP4_TTN)
        if len(ttn_digits) == 14:
            masks.append(page.get_by_text(re.compile(rf"(?<!\d){re.escape(ttn_digits)}(?!\d)")))
        await page.screenshot(path=str(stem.with_suffix(".png")), full_page=True, mask=masks)
        artifacts["screenshot"] = str(stem.with_suffix(".png"))
    except Exception:
        pass
    diagnostic_path = stem.with_suffix(".json")
    artifacts["diagnostic"] = str(diagnostic_path)
    payload = _sanitize_diagnostic({"stage": stage, "label": label, "url": page.url,
               "title": await page.title() if not page.is_closed() else "",
               "artifacts": artifacts, **(extra or {})})
    try:
        diagnostic_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        payload.get("artifacts", {}).pop("diagnostic", None)
    return payload


def _sanitize_diagnostic(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_diagnostic(item) for key, item in value.items()
                if str(key).casefold() not in {"password", "token", "authorization", "api_key", "apikey"}}
    if isinstance(value, list):
        return [_sanitize_diagnostic(item) for item in value]
    if isinstance(value, str):
        result = value
        for secret in (SUP4_LOGIN_PASSWORD, SUP4_NP_API_KEY, SUP4_LOGIN_EMAIL, SUP4_TTN):
            if secret:
                result = result.replace(secret, "[redacted]")
        ttn_digits = _digits(SUP4_TTN)
        if len(ttn_digits) == 14:
            spaced_ttn = r"\D*".join(map(re.escape, ttn_digits))
            result = re.sub(spaced_ttn, "[redacted]", result)
        return re.sub(r"(?i)(apiKey/)[^/]+", r"\1[redacted]", result)[:1000]
    return value


@contextmanager
def _account_lock():
    lock_path = Path(tempfile.gettempdir()) / "supplier4_monsterlab_account.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
        else:
            import fcntl
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StageError("account_lock", "SUP4_ACCOUNT_ALREADY_IN_USE") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def _login(page) -> None:
    stage = "login"
    await page.goto(SUP4_BASE_URL, wait_until="domcontentloaded")
    try:
        if not await page.get_by_role("link", name="Каталог", exact=True).count():
            if not SUP4_LOGIN_EMAIL or not SUP4_LOGIN_PASSWORD:
                raise StageError(stage, "SUP4 credentials are required")
            # A restored but expired storage state may carry stale site cookies
            # or localStorage. Drop that context before a fresh UI login; the
            # saved state file is replaced only after the account is verified.
            await page.context.clear_cookies()
            await page.evaluate("() => localStorage.clear()")
            await page.goto(SUP4_BASE_URL, wait_until="domcontentloaded")
            # The new public landing page renders the login form behind a modal
            # opened by this link. The inputs exist in the DOM before the modal
            # is visible, so filling them without opening it leaves the browser
            # on the public home page and the login never happens.
            login_link = page.get_by_role("link", name=re.compile(r"Увійти в кабінет", re.I))
            if await login_link.count():
                await login_link.first.click()
            await page.get_by_label(re.compile(r"^email$", re.I)).wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
            email = page.get_by_label(re.compile(r"email|електронн", re.I)).first
            password = page.get_by_label(re.compile(r"пароль|password", re.I)).first
            await email.fill(SUP4_LOGIN_EMAIL)
            await password.fill(SUP4_LOGIN_PASSWORD)
            await page.get_by_role("button", name=re.compile(r"увійти|вхід|sign in", re.I)).click()
            await page.get_by_role("link", name="Каталог", exact=True).wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
        await page.get_by_role("link", name="Замовлення", exact=True).wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
        # First-step onboarding is an account-level gate in the new cabinet.
        # Only treat it as blocking when shown inside an alert/dialog with an
        # explicit instruction to complete it, avoiding help-menu false positives.
        for region in (page.locator('[role="alert"]'), page.get_by_role("dialog")):
            for index in range(await region.count()):
                current = region.nth(index)
                if not await current.is_visible():
                    continue
                notice = _norm(await current.inner_text())
                if "перші кроки" in notice and re.search(r"(пройдіть|завершіть|потрібно|спершу|перед першим)", notice):
                    raise StageError(stage, "FIRST_STEPS_REQUIRED", {"notice": notice[:300]})
        if not SUP4_EXPECTED_ACCOUNT:
            raise StageError(stage, "SUP4_EXPECTED_ACCOUNT_REQUIRED",
                             {"hint": "Set the visible MonsterLab profile name in SUP4_EXPECTED_ACCOUNT"})
        expected = _norm(SUP4_EXPECTED_ACCOUNT)
        identity_found = expected in _norm(await page.locator("body").inner_text())
        if not identity_found:
            buttons = page.get_by_role("button")
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                label = " ".join(filter(None, [await button.inner_text(), await button.get_attribute("aria-label"), await button.get_attribute("title")]))
                if re.search(r"проф|акаунт|account|user", label, re.I):
                    await button.click()
                    break
            identity_found = expected in _norm(await page.locator("body").inner_text())
        if not identity_found:
            raise StageError(stage, "UNEXPECTED_SUP4_ACCOUNT")
    except Exception as exc:
        if isinstance(exc, StageError):
            raise
        raise StageError(stage, "LOGIN_VERIFICATION_FAILED", await _debug(page, stage, "verification_failed", {
            "exception_type": type(exc).__name__, "exception": str(exc),
        })) from exc


async def _is_empty_cart(page) -> bool:
    if not page.url.endswith("/cabinet/cart"):
        await page.goto(f"{SUP4_BASE_URL}/cabinet/cart", wait_until="domcontentloaded")
    await page.get_by_role("heading", name="Кошик", exact=True).wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
    return await page.locator('a[href*="/cabinet/catalog/"]').count() == 0


async def _clear_cart(page) -> None:
    await page.goto(f"{SUP4_BASE_URL}/cabinet/cart", wait_until="domcontentloaded")
    stage = "clear_cart"
    if await _is_empty_cart(page):
        return
    # Use only the explicit per-product remove controls on cart rows.
    for _ in range(120):
        if await _is_empty_cart(page):
            return
        remove = page.get_by_role("button", name=re.compile(r"^Прибрати .+ з кошика$", re.I)).first
        try:
            if await remove.count() == 0:
                break
            await remove.click(timeout=3000)
            await page.wait_for_timeout(200)
        except Exception:
            await page.wait_for_timeout(200)
    if not await _is_empty_cart(page):
        raise StageError(stage, "CART_NOT_EMPTY_AFTER_CLEAR", await _debug(page, stage, "not_empty"))


def _stock_lower_bound(text: str) -> tuple[int, bool] | None:
    match = re.search(r"(\d+)\s*(\+)?\s*шт\.?", text, re.I)
    return (int(match.group(1)), bool(match.group(2))) if match else None


async def _search_card(page, sku: str, *, required_qty: int = 1):
    stage = "add_items"
    search = page.get_by_placeholder(re.compile(r"Назва, бренд або артикул", re.I)).first
    await search.fill(sku)
    await search.press("Enter")
    product_link = page.locator(f'a[href="/cabinet/catalog/{sku}"]')
    try:
        await product_link.wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
    except Exception as exc:
        raise StageError(
            stage,
            "SEARCH_EXACT_SKU_NOT_FOUND",
            await _debug(page, stage, "exact_sku_not_found", {"sku": sku, "matches": await product_link.count()}),
        ) from exc
    if await product_link.count() != 1:
        raise StageError(stage, "SEARCH_EXACT_SKU_NOT_FOUND", await _debug(page, stage, "ambiguous_exact_sku", {"sku": sku, "matches": await product_link.count()}))
    # Catalog rows are data-tour-marked in the new cabinet. The nearest div
    # containing a button is only the product title block and omits stock and
    # price columns, so read the full row instead.
    card = product_link.locator('xpath=ancestor::div[@data-tour="catalog-row"][1]')
    text = await card.inner_text()
    if sku.casefold() not in _norm(text) or re.search(r"(?:^|\s)немає(?:\s|$)", _norm(text)):
        raise StageError(stage, "PRODUCT_NOT_AVAILABLE", await _debug(page, stage, "not_available", {"sku": sku, "card": text}))
    stock = _stock_lower_bound(text)
    if not stock:
        raise StageError(stage, "PRODUCT_STOCK_UNVERIFIED", await _debug(page, stage, "stock_unverified", {"sku": sku, "card": text}))
    available, is_lower_bound = stock
    if is_lower_bound and required_qty > available:
        raise StageError(stage, "PRODUCT_STOCK_UNVERIFIED", await _debug(page, stage, "stock_lower_bound", {
            "sku": sku, "requested": required_qty, "available_at_least": available,
        }))
    # Allow horizontal grouping spaces (including NBSP), but never newlines:
    # otherwise the SKU above the price can be concatenated into the amount.
    prices = re.findall(r"([0-9][0-9 \u00a0]*[,.]\d{2})[ \u00a0]*₴", text)
    unit_price = _parse_money(prices[0]) if prices else None
    if unit_price is None:
        raise StageError(stage, "PRODUCT_PRICE_UNREADABLE", await _debug(page, stage, "price_unreadable", {"sku": sku}))
    return card, available, unit_price


async def _add_item(page, item: Sup4Item) -> dict[str, Any]:
    await page.goto(f"{SUP4_BASE_URL}/cabinet/catalog", wait_until="domcontentloaded")
    card, available, unit_price = await _search_card(page, item.sku, required_qty=item.qty)
    if item.qty > available:
        raise StageError("add_items", "PRODUCT_STOCK_INSUFFICIENT", await _debug(page, "add_items", "requested_over_stock", {"sku": item.sku, "requested": item.qty, "available": available}))
    await page.goto(f"{SUP4_BASE_URL}/cabinet/cart", wait_until="domcontentloaded")
    current = await _read_cart(page)
    current_qty = next((int(row["qty"]) for row in current if row["sku"].casefold() == item.sku.casefold()), 0)
    await page.goto(f"{SUP4_BASE_URL}/cabinet/catalog", wait_until="domcontentloaded")
    for _ in range(current_qty, item.qty):
        card, _, _ = await _search_card(page, item.sku, required_qty=item.qty)
        button = card.get_by_role("button", name=re.compile(r"^(?:У кошик|Додати .+ у кошик|Додати ще одну: .+)$", re.I)).first
        if await button.count() != 1:
            raise StageError("add_items", "PRODUCT_ADD_CONTROL_NOT_FOUND", await _debug(page, "add_items", "add_button_missing", {"sku": item.sku, "controls": await button.count()}))
        await button.click()
    actual = await _read_cart_at_url(page, item.sku)
    if actual != item.qty:
        raise StageError("add_items", "CART_ORDER_MISMATCH", await _debug(page, "add_items", "quantity_not_set", {"sku": item.sku, "expected": item.qty, "actual": actual}))
    return {"sku": item.sku, "expected_qty": item.qty, "actual_qty": actual,
            "unit_price_minor": unit_price, "line_total_minor": unit_price * item.qty,
            "verified": True, "verified_stage": "catalog"}


async def _read_cart(page) -> list[dict[str, Any]]:
    if not page.url.endswith("/cabinet/cart"):
        await page.goto(f"{SUP4_BASE_URL}/cabinet/cart", wait_until="domcontentloaded")
    await page.get_by_role("heading", name="Кошик", exact=True).wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
    result: list[dict[str, Any]] = []
    links = page.locator('a[href*="/cabinet/catalog/"]')
    for i in range(await links.count()):
        link = links.nth(i)
        href = await link.get_attribute("href") or ""
        sku = href.rsplit("/", 1)[-1].strip()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{1,40}", sku, re.I):
            continue
        # The new cart renders one product per <li>; the nearest button-bearing
        # div only contains the product title and excludes the quantity control.
        row = link.locator("xpath=ancestor::li[1]")
        raw_text = await row.inner_text()
        text = re.sub(r"\s+", " ", raw_text).strip()
        qty_match = re.search(r"×\s*(\d+)", text) or re.search(r"Кількість\s+(\d+)\b", text, re.I)
        if not qty_match:
            raise StageError("verify_cart", "CART_QUANTITY_UNREADABLE", {"sku": sku})
        line_total = _cart_line_total(raw_text, int(qty_match.group(1)))
        result.append({"sku": sku, "qty": int(qty_match.group(1)),
                       "price": line_total, "text": text})
    return result


def _cart_line_total(raw_text: str, qty: int) -> int | None:
    # Read amounts before collapsing newlines: the quantity control sits above
    # the prices and must not become a thousands group. Total precedes unit price.
    amounts = re.findall(r"([0-9][0-9 \u00a0]*[,.]\d{2})[ \u00a0]*₴", raw_text)
    values = [_parse_money(amount) for amount in amounts]
    if not values:
        return None
    if len(values) > 1 and values[0] != values[-1] * qty:
        raise StageError("verify_cart", "CART_ROW_PRICE_INCONSISTENT", {
            "qty": qty, "line_total_minor": values[0], "unit_price_minor": values[-1],
        })
    return values[0]


async def _read_cart_at_url(page, sku: str) -> int:
    cart = await _read_cart(page)
    return next((int(row["qty"]) for row in cart if row["sku"].casefold() == sku.casefold()), 0)


def _compare_cart_prices(items: list[Sup4Item], actual: list[dict[str, Any]], prices: dict[str, int]) -> dict[str, Any]:
    mismatches = []
    actual_by_sku = {str(row.get("sku") or "").casefold(): row for row in actual}
    for item in items:
        row = actual_by_sku.get(item.sku.casefold(), {})
        expected_total = prices.get(item.sku.casefold(), -1) * item.qty
        if row.get("price") != expected_total:
            mismatches.append({"sku": item.sku, "expected_line_total_minor": expected_total,
                               "actual_line_total_minor": row.get("price")})
    return {"verified": not mismatches, "mismatches": mismatches}


async def _select_toggle(page, name: str) -> None:
    toggle = page.get_by_role("button", name=name, exact=True)
    if await toggle.count() == 1:
        state = await toggle.get_attribute("aria-pressed") or await toggle.get_attribute("aria-checked")
        if state is None:
            state = await toggle.get_attribute("value")
        if str(state).casefold() not in {"true", "1", "checked"}:
            await toggle.click()
    else:
        # The new checkout renders carrier tiles as visually hidden radio inputs,
        # not buttons with accessible names. Use the supplier's known radio value
        # and verify its checked state after selection.
        carrier_values = {"нова пошта": "nova-poshta", "укрпошта": "ukrposhta",
                          "rozetka delivery": "rozetka-delivery", "meest": "meest"}
        value = carrier_values.get(_norm(name))
        radio = page.locator(f'input[type="radio"][value="{value}"]') if value else page.locator("input[type=radio][data-no-match]")
        if await radio.count() != 1:
            raise StageError("checkout_options", "CHECKOUT_OPTION_NOT_FOUND", {"option": name, "matches": await toggle.count()})
        if not await radio.is_checked():
            await radio.check(force=True)
        if not await radio.is_checked():
            raise StageError("checkout_options", "CHECKOUT_OPTION_SELECTION_UNVERIFIED", {"option": name})
    await page.wait_for_timeout(150)


async def _preflight_store_availability(page) -> dict[str, Any]:
    """Check the configured store before clearing a non-empty cart when checkout permits it."""
    await page.goto(f"{SUP4_BASE_URL}/cabinet/checkout", wait_until="domcontentloaded")
    heading = page.get_by_role("heading", name="Оформлення замовлення", exact=True)
    if await heading.count() == 0:
        if await _is_empty_cart(page):
            return {"verified": False, "deferred": True, "reason": "CHECKOUT_REQUIRES_CART"}
        raise StageError("preflight_store", "CHECKOUT_UNAVAILABLE_BEFORE_CART_CLEAR",
                         await _debug(page, "preflight_store", "checkout_unavailable"))
    store_button = _store_selector_button(page)
    if await store_button.count() != 1:
        raise StageError("preflight_store", "STORE_SELECTOR_NOT_FOUND",
                         await _debug(page, "preflight_store", "store_selector_missing"))
    await store_button.click()
    listbox = page.get_by_role("listbox", name="Магазин")
    option = page.get_by_role("option", name=SUP4_EXPECTED_STORE, exact=True)
    available = await option.count() == 1
    if not available and await listbox.count() == 1:
        available = await listbox.get_by_text(SUP4_EXPECTED_STORE, exact=True).count() > 0
    await page.keyboard.press("Escape")
    if not available:
        raise StageError("preflight_store", "EXPECTED_STORE_UNAVAILABLE",
                         await _debug(page, "preflight_store", "expected_store_missing", {"store": SUP4_EXPECTED_STORE}))
    return {"verified": True, "store": SUP4_EXPECTED_STORE}


async def _select_checkout_options(page) -> None:
    store_button = _store_selector_button(page)
    if await store_button.count() == 0:
        # Some Chromium builds expose only the selected value as the button's
        # accessible name, while Safari includes the external "Магазин" label.
        store_button = page.locator("button").filter(has_text=re.compile(re.escape(SUP4_EXPECTED_STORE), re.I))
    if await store_button.count():
        await store_button.first.wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
    if await store_button.count() != 1:
        raise StageError("checkout_options", "STORE_SELECTOR_NOT_FOUND", {"matches": await store_button.count()})
    await store_button.click()
    option = page.get_by_role("option", name=SUP4_EXPECTED_STORE, exact=True)
    if await option.count() == 1:
        await option.click()
    else:
        store_text = page.get_by_text(SUP4_EXPECTED_STORE, exact=True)
        if await store_text.count() < 1:
            raise StageError("checkout_options", "EXPECTED_STORE_UNAVAILABLE", {"store": SUP4_EXPECTED_STORE})
        await store_text.last.click()
    await _select_toggle(page, "Нова пошта")
    mode = page.get_by_label(re.compile(r"У мене вже є ТТН", re.I))
    if await mode.count() != 1:
        raise StageError("checkout_options", "TTN_MODE_UNAVAILABLE")
    if not await mode.is_checked():
        await mode.check(force=True)
    if not await mode.is_checked():
        raise StageError("checkout_options", "TTN_MODE_SELECTION_UNVERIFIED")
    if await page.get_by_label(re.compile(r"Номер\s*ТТН", re.I)).count() != 1:
        raise StageError("checkout_options", "READY_TTN_MODE_NOT_SELECTED")


def _store_selector_button(page):
    name = re.compile(rf"^(?:Магазин\b|{re.escape(SUP4_EXPECTED_STORE)}$)", re.I)
    return page.get_by_role("button", name=name)


async def _read_checkout_items(page) -> list[dict[str, Any]]:
    links = page.locator('a[href*="/cabinet/catalog/"]')
    item_links: list[tuple[str, str]] = []
    for i in range(await links.count()):
        link = links.nth(i)
        href = await link.get_attribute("href") or ""
        sku = href.rsplit("/", 1)[-1].strip()
        if re.fullmatch(r"[A-Z0-9][A-Z0-9-]{1,40}", sku, re.I):
            row = link.locator("xpath=ancestor::li[1]")
            if await row.count() == 0:
                row = link.locator("xpath=ancestor::div[.//a[starts-with(@href, '/cabinet/catalog/')]][1]")
            item_links.append((sku, await row.inner_text()))
    return _detail_order_items(item_links)


def _validate_label_pdf(path: Path, *, ttn: str, trusted_np_download: bool = False) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size == 0 or path.stat().st_size > 1024 * 1024:
            raise ValueError("LABEL_SIZE_INVALID")
        reader = PdfReader(str(path), strict=True)
        if len(reader.pages) != 1:
            raise ValueError("LABEL_PAGE_COUNT_INVALID")
        page = reader.pages[0]
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        expected_pt = 100 * 72 / 25.4
        if abs(width - expected_pt) > 3 or abs(height - expected_pt) > 3:
            raise ValueError("LABEL_PAGE_SIZE_INVALID")
        extracted = " ".join((p.extract_text() or "") for p in reader.pages)
        if _digits(ttn) not in _digits(extracted) and not (trusted_np_download and _digits(ttn) in _digits(path.name)):
            raise ValueError("LABEL_TTN_MISMATCH")
        return {"size_bytes": path.stat().st_size, "page_count": len(reader.pages),
                "width_pt": round(width, 2), "height_pt": round(height, 2), "ttn_verified": True}
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "LABEL_PDF_INVALID"
        raise StageError("attach_invoice_label", reason, {"file_name": path.name}) from exc


def _label_matches_ttn(path: Path, ttn: str) -> bool:
    """Require a readable 100×100 mm PDF and TTN text; names alone are not proof."""
    if not _digits(ttn):
        return False
    try:
        _validate_label_pdf(path, ttn=ttn)
        return True
    except StageError:
        return False


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
        raise RuntimeError("NP label download failed") from exc
    # NP's nominal 100x100 marking includes a small printer margin (101x101.5 mm).
    # Scale the complete official page, preserving all barcode and address content.
    reader = PdfReader(str(temp_path), strict=True)
    if len(reader.pages) == 1:
        page = reader.pages[0]
        target = 100 * 72 / 25.4
        tolerance = 2 * 72 / 25.4
        if all(abs(float(side) - target) <= tolerance for side in (page.mediabox.width, page.mediabox.height)):
            page.scale_to(target, target)
            writer = PdfWriter()
            writer.add_page(page)
            with temp_path.open("wb") as stream:
                writer.write(stream)
    _validate_label_pdf(temp_path, ttn=ttn, trusted_np_download=True)
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
    ttn = _validated_ttn(ttn)
    inp = page.get_by_label(re.compile(r"номер\s*ттн", re.I)).first
    try:
        await inp.fill(ttn)
        actual = await inp.input_value()
    except Exception as exc:
        raise StageError(stage, "TTN_VALUE_MISMATCH", await _debug(page, stage, "input_failed", {"ttn": ttn})) from exc
    if _digits(actual) != _digits(ttn):
        raise StageError(stage, "TTN_VALUE_MISMATCH", await _debug(page, stage, "value_mismatch", {"expected": ttn, "actual": actual}))
    label = _resolve_label(ttn)
    pdf_check = _validate_label_pdf(label, ttn=ttn, trusted_np_download=label.name == f"label-{ttn}.pdf")
    candidates = page.locator('input[type="file"]')
    pdf_inputs = []
    for i in range(await candidates.count()):
        candidate = candidates.nth(i)
        accept = (await candidate.get_attribute("accept") or "").casefold()
        name = (await candidate.get_attribute("name") or "").casefold()
        context_text = _norm(await candidate.evaluate("el => (el.closest('label') || el.parentElement)?.innerText || ''"))
        is_label_input = "pdf" in accept or ("pdf" in context_text and "етикет" in context_text)
        if is_label_input and not any(word in name for word in ("deposit", "topup", "receipt")):
            pdf_inputs.append(candidate)
    if len(pdf_inputs) != 1:
        raise StageError(stage, "LABEL_INPUT_AMBIGUOUS", {"pdf_inputs": len(pdf_inputs)})
    file_input = pdf_inputs[0]
    try:
        await file_input.set_input_files(str(label))
        files = await file_input.evaluate("el => el.files ? el.files.length : 0")
        await page.get_by_text(re.compile(re.escape(label.name), re.I)).wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
        attachment_text = _norm(await page.locator("body").inner_text())
        current_ttn = await inp.input_value()
    except Exception as exc:
        raise StageError(stage, "LABEL_ATTACH_VERIFY_FAILED", await _debug(page, stage, "attach_failed")) from exc
    if int(files or 0) != 1 or label.name.casefold() not in attachment_text:
        raise StageError(stage, "LABEL_ATTACH_VERIFY_FAILED", await _debug(page, stage, "attach_not_visible", {"file": label.name}))
    if _digits(current_ttn) != _digits(ttn):
        raise StageError(stage, "TTN_VALUE_MISMATCH", await _debug(page, stage, "value_changed_after_attach", {"expected": ttn, "actual": current_ttn}))
    return {"ttn": ttn, **pdf_check, "file": str(label), "file_name": label.name, "attached": True}


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


def _success_page_order_number(text: str) -> str:
    """Extract the D-number shown under НОМЕР ЗАМОВЛЕННЯ on the final page."""
    if "замовлення створено" not in _norm(text):
        return ""
    match = re.search(r"НОМЕР\s+ЗАМОВЛЕННЯ\s*:?\s*(D\d{4,})\b", str(text or ""), re.I)
    return match.group(1).upper() if match else ""


async def _read_success_page_order_number(page) -> str:
    heading = page.get_by_role("heading", name=re.compile(r"Замовлення створено", re.I))
    try:
        await heading.first.wait_for(state="visible", timeout=min(2000, SUP4_TIMEOUT_MS))
    except PlaywrightTimeoutError:
        return ""
    body = await page.locator("body").inner_text()
    number = _success_page_order_number(body)
    if not number:
        raise StageError("submit_checkout_order", "CONFIRMATION_PAGE_ORDER_NUMBER_MISSING",
                         await _debug(page, "submit_checkout_order", "confirmation_number_missing"))
    return number


def _checkpoint_path(ttn: str) -> Path:
    digest = hashlib.sha256(_digits(ttn).encode("ascii")).hexdigest()[:16]
    return _debug_dir() / f"submit_{digest}.json"


def _write_checkpoint(ttn: str, state: str, **details: Any) -> Path:
    path = _checkpoint_path(ttn)
    payload = {"ttn": _digits(ttn), "state": state, "updated_at": datetime.now().isoformat(timespec="seconds"), **details}
    fd, temp_name = tempfile.mkstemp(prefix=".sup4-checkpoint-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return path


def _read_checkpoint(ttn: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_checkpoint_path(ttn).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _detail_order_items(links: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Extract quantity only from each product's own row, never from order totals."""
    items = []
    for sku, row_text in links:
        match = re.search(r"\b(\d+)\s*шт\.?", row_text, re.I) or re.search(
            r"^\s*(\d+)\s*×\s*[0-9][0-9 \u00a0]*[,.]\d{2}[ \u00a0]*₴", row_text, re.M
        )
        if not match:
            items.append({"sku": sku, "qty": 0})
        else:
            items.append({"sku": sku, "qty": int(match.group(1))})
    return items


def _compare_order_details(expected: list[Sup4Item], detail: dict[str, Any], ttn: str) -> dict[str, Any]:
    actual = detail.get("items") if isinstance(detail.get("items"), list) else []
    comparison = _compare_order(expected, actual)
    required = {
        "order_number": bool(re.fullmatch(r"D\d{4,}", str(detail.get("number") or ""), re.I)),
        "store": _norm(detail.get("store")) == _norm(SUP4_EXPECTED_STORE),
        "carrier": _norm(detail.get("carrier")) == _norm("Нова пошта"),
        "ttn": _digits(detail.get("ttn")) == _digits(ttn),
        "label_received": bool(detail.get("label_received")),
    }
    return {**comparison, "checks": required, "verified": bool(comparison["verified"] and all(required.values()))}


def _label_status_received(value: object) -> bool:
    text = _norm(value)
    positive = ("надійшла", "надійшло", "отримана", "отримано", "завантажена", "завантажено",
                "прикріплена", "прикріплено", "прийнята", "прийнято", "підтверджена", "підтверджено",
                "переглянути")
    negative = ("не надійшла", "не надходила", "не отримана", "не завантажена", "не прикріплена",
                "не прийнята", "не підтверджена")
    return any(marker in text for marker in positive) and not any(marker in text for marker in negative)


async def _find_orders_for_ttn(page, ttn: str) -> list[dict[str, str]]:
    ttn = _validated_ttn(ttn)
    await page.goto(f"{SUP4_BASE_URL}/cabinet/orders", wait_until="domcontentloaded")
    search = page.get_by_placeholder(re.compile(r"Номер замовлення або ТТН", re.I)).first
    await search.fill(ttn)
    await search.press("Enter")
    try:
        await page.wait_for_url(
            lambda url: parse_qs(urlsplit(str(url)).query).get("q") == [ttn],
            timeout=SUP4_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        raise StageError("preflight_history", "HISTORY_SEARCH_NOT_APPLIED",
                         await _debug(page, "preflight_history", "search_not_applied")) from exc
    search_confirmed = False
    for _ in range(max(1, min(20, SUP4_TIMEOUT_MS // 250))):
        body = await page.locator("body").inner_text()
        ttn_text = page.get_by_text(ttn, exact=True).first
        exact_ttn_visible = await ttn_text.count() > 0 and await ttn_text.is_visible()
        if exact_ttn_visible or "за цими умовами нічого не знайдено" in _norm(body):
            search_confirmed = True
            break
        await page.wait_for_timeout(250)
    if not search_confirmed:
        raise StageError("preflight_history", "HISTORY_SEARCH_NOT_CONFIRMED",
                         await _debug(page, "preflight_history", "search_not_confirmed"))
    matches: list[dict[str, str]] = []
    for page_number in range(100):
        anchors = page.locator('a[href^="/cabinet/orders/"]')
        page_signature: list[tuple[str, str]] = []
        for i in range(await anchors.count()):
            anchor = anchors.nth(i)
            if not await anchor.is_visible():
                continue
            href = await anchor.get_attribute("href") or ""
            if not re.fullmatch(r"/cabinet/orders/D\d+", href, re.I):
                continue
            row = anchor.locator("xpath=ancestor::div[.//a[starts-with(@href, '/cabinet/orders/')]][1]")
            text = re.sub(r"\s+", " ", await row.inner_text()).strip()
            page_signature.append((href, text))
            if re.search(rf"(?<!\d){re.escape(_digits(ttn))}(?!\d)", _digits(text)):
                matches.append({"number": href.rsplit("/", 1)[-1].upper(), "href": href, "row_text": text})

        next_page = page.locator(
            'button[aria-label*="наступ" i], button[title*="наступ" i], '
            'button[aria-label*="next" i], button[title*="next" i], a[rel="next"]'
        ).first
        if await next_page.count() == 0:
            next_page = page.get_by_role("button", name=re.compile(r"^(?:Наступна|Далі|Next|›|»|>)", re.I)).first
        if await next_page.count() == 0:
            next_page = page.get_by_role("link", name=re.compile(r"^(?:Далі|Наступна|Next)$", re.I)).first
        if await next_page.count() == 0 or not await next_page.is_enabled():
            return matches
        await next_page.click()
        await page.wait_for_timeout(350)
        next_anchors = page.locator('a[href^="/cabinet/orders/"]')
        next_signature = []
        for index in range(await next_anchors.count()):
            anchor = next_anchors.nth(index)
            if not await anchor.is_visible():
                continue
            href = await anchor.get_attribute("href") or ""
            if re.fullmatch(r"/cabinet/orders/D\d+", href, re.I):
                row = anchor.locator("xpath=ancestor::div[.//a[starts-with(@href, '/cabinet/orders/')]][1]")
                next_signature.append((href, re.sub(r"\s+", " ", await row.inner_text()).strip()))
        if next_signature == page_signature:
            raise StageError("preflight_history", "HISTORY_PAGINATION_STALLED", {"page": page_number + 1})
    raise StageError("preflight_history", "HISTORY_SEARCH_PAGE_LIMIT", {"max_pages": 100})


async def _read_order_detail(page, href: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
    await page.goto(f"{SUP4_BASE_URL}{href}", wait_until="domcontentloaded")
    await page.get_by_role("heading", name=re.compile(r"Замовлення\s+D\d+", re.I)).wait_for(
        state="visible", timeout=timeout_ms or SUP4_TIMEOUT_MS
    )
    body = re.sub(r"\s+", " ", await page.locator("body").inner_text()).strip()
    links = page.locator('a[href*="/cabinet/catalog/"]')
    item_links: list[tuple[str, str]] = []
    for i in range(await links.count()):
        link = links.nth(i)
        href_item = await link.get_attribute("href") or ""
        sku = href_item.rsplit("/", 1)[-1].strip()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{1,40}", sku, re.I):
            continue
        row = link.locator("xpath=ancestor::li[1]")
        if await row.count() == 0:
            row = link.locator("xpath=ancestor::div[.//a[starts-with(@href, '/cabinet/catalog/')]][1]")
        item_links.append((sku, re.sub(r"\s+", " ", await row.inner_text()).strip()))
    items = _detail_order_items(item_links)
    number_match = re.search(r"Замовлення\s+(D\d+)", body, re.I)
    ttn_match = re.search(r"\b(\d{14})\b", body)
    store = SUP4_EXPECTED_STORE if _norm(SUP4_EXPECTED_STORE) in _norm(body) else ""
    carrier = "Нова пошта" if "нова пошта" in _norm(body) else ""
    label_region = body.split("Етикетка", 1)[1][:100] if "Етикетка" in body else ""
    label_received = _label_status_received(label_region)
    return {"number": number_match.group(1).upper() if number_match else "", "ttn": ttn_match.group(1) if ttn_match else "",
            "store": store, "carrier": carrier, "items": items, "label_status": label_region,
            "label_received": label_received, "url": page.url}


async def _existing_order_result(page, ttn: str, items: list[Sup4Item], *, after_submit: bool = False) -> dict[str, Any] | None:
    matches = await _find_orders_for_ttn(page, ttn)
    if not matches:
        return None
    if len(matches) != 1:
        raise StageError("preflight_history", "DUPLICATE_TTN_ORDERS", {"matches": [m["number"] for m in matches]})
    detail = await _read_order_detail(page, matches[0]["href"])
    comparison = _compare_order_details(items, detail, ttn)
    if not comparison["verified"]:
        other_checks = {key: value for key, value in comparison["checks"].items() if key != "label_received"}
        if after_submit and all(other_checks.values()) and not comparison["checks"].get("label_received"):
            raise StageError("submit_checkout_order", "ORDER_LABEL_NOT_CONFIRMED", {
                "number": detail.get("number"), "label_status": detail.get("label_status"),
            })
        stage = "submit_checkout_order" if after_submit else "preflight_history"
        reason = "ORDER_CREATED_DETAIL_MISMATCH" if after_submit else "EXISTING_TTN_ORDER_MISMATCH"
        raise StageError(stage, reason, {"number": detail.get("number"), "comparison": comparison})
    _write_checkpoint(ttn, "confirmed", supplier_order_number=detail["number"], recovered=True)
    qty_checks = _cart_qty_checks(items, detail["items"])
    return {"ok": True, "stage": "confirmed", "submit_state": "confirmed", "submitted": True,
            "supplier_order_number": detail["number"], "recovered": True, "order_detail": detail,
            "order_comparison": comparison, "cart_qty_checks": qty_checks, "url": page.url}


def _submit_not_ready_reason(page_text: str) -> str:
    """Classify a disabled final button without guessing that an order was sent."""
    text = _norm(page_text)
    if "не вистачає" in text or "поповніть баланс" in text or "недостатньо коштів" in text:
        return "INSUFFICIENT_DEPOSIT"
    return "SUBMIT_NOT_READY"


def _supplier_error_text(payload: object) -> str:
    """Extract only explicit error fields; never flatten arbitrary payload data."""
    if isinstance(payload, dict):
        for key in ("error", "message", "detail", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (dict, list)):
                nested = _supplier_error_text(value)
                if nested:
                    return nested
        return ""
    if isinstance(payload, list):
        return " ".join(filter(None, (_supplier_error_text(value) for value in payload)))
    return "" if isinstance(payload, str) else ""


def _sanitize_supplier_message(value: object) -> str:
    message = re.sub(r"\s+", " ", str(value or "")).strip()
    message = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[redacted-email]", message)
    message = re.sub(r"(?<!\d)(?:\+?\d[\s().-]*){9,15}(?!\d)", "[redacted-number]", message)
    message = re.sub(r"\b\d{14}\b", "[redacted-number]", message)
    return str(_sanitize_diagnostic(message))[:300]


def _available_qty_from_supplier_error(message: str) -> int | None:
    """Return an explicitly reported available quantity, never a guessed one."""
    text = str(message or "")
    patterns = (
        r"(?:лише|только|only)\s+(\d+)\s*(?:шт\.?|pcs?|items?)?\s*(?:в\s+наявності|в\s+наличии|available|left)",
        r"(\d+)\s*(?:шт\.?|pcs?|items?)\s*(?:в\s+наявності|в\s+наличии|available|left)",
        r"(?:в\s+наявності|в\s+наличии|available|left)\D{0,20}(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1))
    return None


def _classify_submit_rejection(status: int, payload: object, items: list[Sup4Item]) -> dict[str, Any] | None:
    """Recognize a definitive stock rejection from the supplier submit response.

    Only a client-side rejection with an explicit stock signal is safe to mark
    failed in SalesDrive. Network errors, 5xx responses and unknown validation
    errors remain ambiguous after the submit click.
    """
    if not 400 <= int(status) < 500:
        return None
    supplier_message = _sanitize_supplier_message(_supplier_error_text(payload))
    normalized = _norm(supplier_message)
    stock_signals = (
        "немає в наявності", "більше немає в наявності", "недостатньо товар",
        "недостатня кількість", "недостатньо залиш", "нет в наличии",
        "недостаточно товара", "недостаточное количество", "недостаточно остат",
        "out of stock", "insufficient stock", "insufficient quantity",
        "not enough stock", "not enough items",
    )
    if not supplier_message or not any(signal in normalized for signal in stock_signals):
        return None

    requested = ", ".join(f"{item.sku}:{item.qty}" for item in items)
    available_qty = _available_qty_from_supplier_error(supplier_message)
    message = f"INSUFFICIENT_STOCK: requested={requested}; supplier={supplier_message[:300]}"
    if available_qty is not None:
        message += f"; available={available_qty}"
    return {
        "message": message,
        "status": int(status),
        "supplier_message": supplier_message[:300],
        "requested": requested,
        "available_qty": available_qty,
    }


async def _verify_submit_ready(page) -> dict[str, Any]:
    """Validate all checkout choices, totals and balance before submit."""
    stage = "checkout_ready"
    button = page.get_by_role("button", name="Створити замовлення", exact=True)
    try:
        await button.wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
        enabled = await button.is_enabled()
        body = await page.locator("body").inner_text()
    except Exception as exc:
        raise StageError(stage, "SUBMIT_NOT_READY", await _debug(page, stage, "button_not_ready")) from exc
    if not enabled:
        reason = _submit_not_ready_reason(body)
        raise StageError(stage, reason, await _debug(page, stage, "button_disabled", {"reason": reason}))
    if SUP4_EXPECTED_STORE not in body or "Нова пошта" not in body:
        raise StageError(stage, "CHECKOUT_SELECTION_MISMATCH", await _debug(page, stage, "selection_mismatch", {"store_ok": SUP4_EXPECTED_STORE in body, "carrier_ok": "Нова пошта" in body}))
    balance_match = re.search(r"Баланс зараз\s*([0-9][0-9 \u00a0]*[,.]\d{2})\s*₴", body, re.I)
    total_match = re.search(r"Оптова сума\s*([0-9][0-9 \u00a0]*[,.]\d{2})\s*₴", body, re.I)
    if not balance_match or not total_match:
        raise StageError(stage, "CHECKOUT_BALANCE_UNREADABLE", await _debug(page, stage, "balance_unreadable"))
    balance, total = _parse_money(balance_match.group(1)), _parse_money(total_match.group(1))
    if balance is None or total is None or balance < total:
        raise StageError(stage, "INSUFFICIENT_DEPOSIT", await _debug(page, stage, "insufficient_balance", {"balance_minor": balance, "total_minor": total}))
    return {"submit_ready": True, "balance_minor": balance, "total_minor": total,
            "store": SUP4_EXPECTED_STORE, "carrier": "Нова пошта"}


async def _submit_and_confirm(page, ttn: str, items: list[Sup4Item]) -> dict[str, Any]:
    """Click once and verify by confirmation number before using TTN history fallback."""
    stage = "submit_checkout_order"
    button = page.get_by_role("button", name="Створити замовлення", exact=True)
    try:
        await button.wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
        _write_checkpoint(ttn, "submit_in_flight", items=[{"sku": x.sku, "qty": x.qty} for x in items])
        submit_response = None
        try:
            async with page.expect_response(
                lambda response: response.request.method == "POST" and _is_supplier_host(response.url),
                timeout=8000,
            ) as response_info:
                await button.click()
            submit_response = await response_info.value
        except PlaywrightTimeoutError:
            submit_response = None

        status = int(submit_response.status) if submit_response is not None else None
        response_path = urlsplit(submit_response.url).path if submit_response is not None else ""
        confirmation_number = await _read_success_page_order_number(page)
        if confirmation_number:
            _write_checkpoint(ttn, "submit_in_flight",
                              items=[{"sku": x.sku, "qty": x.qty} for x in items],
                              confirmation_order_number=confirmation_number, http_status=status)
        supplier_message = ""
        if submit_response is not None and status is not None and 400 <= status < 600:
            response_text = await submit_response.text()
            try:
                response_payload: object = json.loads(response_text)
            except json.JSONDecodeError:
                response_payload = {"message": "Non-JSON supplier response"}
            supplier_message = _sanitize_supplier_message(_supplier_error_text(response_payload))

        if status is not None and 400 <= status < 500 and not confirmation_number:
            rejection = _classify_submit_rejection(status, response_payload, items)
            if rejection:
                _write_checkpoint(ttn, "rejected", http_status=status)
                raise StageError(stage, rejection["message"], {
                    "http_status": status, "supplier_message": supplier_message,
                    "submit_state": "rejected", "request_path": response_path,
                })

        if confirmation_number:
            direct_detail_error = ""
            try:
                detail = await _read_order_detail(
                    page, f"/cabinet/orders/{confirmation_number}",
                    timeout_ms=min(5000, SUP4_TIMEOUT_MS),
                )
                comparison = _compare_order_details(items, detail, ttn)
                if comparison["verified"]:
                    _write_checkpoint(ttn, "confirmed", supplier_order_number=confirmation_number,
                                      http_status=status, recovered=False)
                    return {
                        "ok": True, "stage": "confirmed", "submit_state": "confirmed", "submitted": True,
                        "supplier_order_number": confirmation_number,
                        "confirmation_order_number": confirmation_number,
                        "order_detail": detail, "order_comparison": comparison,
                        "cart_qty_checks": _cart_qty_checks(items, detail["items"]),
                        "http_status": status, "request_path": response_path, "url": page.url,
                    }
                direct_detail_error = "CONFIRMATION_DETAIL_MISMATCH"
            except Exception as exc:
                direct_detail_error = type(exc).__name__

            # The success page already gave us D-number. Search TTN history once
            # only if its detail route is temporarily unavailable or incomplete.
            try:
                existing = await _existing_order_result(page, ttn, items, after_submit=True)
            except StageError as exc:
                raise StageError(stage, "ORDER_CREATED_DETAIL_UNCONFIRMED", {
                    "confirmation_order_number": confirmation_number,
                    "direct_detail_result": direct_detail_error,
                    "fallback_history_error": exc.message,
                    "submit_state": "needs_reconciliation",
                    "checkpoint": str(_checkpoint_path(ttn)),
                }) from exc
            if existing and str(existing["supplier_order_number"]).upper() == confirmation_number.upper():
                _write_checkpoint(ttn, "confirmed", supplier_order_number=confirmation_number,
                                  http_status=status, recovered=False)
                return {**existing, "supplier_order_number": confirmation_number,
                        "confirmation_order_number": confirmation_number,
                        "submit_state": "confirmed", "http_status": status,
                        "request_path": response_path}
            raise StageError(stage, "ORDER_CREATED_DETAIL_UNCONFIRMED", {
                "confirmation_order_number": confirmation_number,
                "direct_detail_result": direct_detail_error,
                "fallback_history_number": (existing or {}).get("supplier_order_number", ""),
                "submit_state": "needs_reconciliation",
                "checkpoint": str(_checkpoint_path(ttn)),
            })

        deadline = asyncio.get_running_loop().time() + (SUP4_TIMEOUT_MS / 1000)
        label_pending: dict[str, Any] | None = None
        while True:
            try:
                existing = await _existing_order_result(page, ttn, items, after_submit=True)
                if existing:
                    history_number = str(existing["supplier_order_number"] or "").upper()
                    if confirmation_number and confirmation_number != history_number:
                        raise StageError(stage, "CONFIRMATION_ORDER_NUMBER_MISMATCH", {
                            "confirmation_order_number": confirmation_number,
                            "history_order_number": history_number,
                            "submit_state": "needs_reconciliation",
                        })
                    supplier_order_number = confirmation_number or history_number
                    _write_checkpoint(ttn, "confirmed", supplier_order_number=existing["supplier_order_number"],
                                      http_status=status, recovered=False)
                    return {**existing, "supplier_order_number": supplier_order_number,
                            "confirmation_order_number": confirmation_number,
                            "submit_state": "confirmed", "http_status": status,
                            "request_path": response_path}
            except StageError as exc:
                if exc.message != "ORDER_LABEL_NOT_CONFIRMED":
                    raise
                label_pending = exc.details
            if asyncio.get_running_loop().time() >= deadline:
                break
            await page.wait_for_timeout(900)

        if label_pending:
            raise StageError(stage, "ORDER_CREATED_LABEL_NOT_CONFIRMED", {
                "http_status": status, "submit_state": "needs_reconciliation", **label_pending,
                "checkpoint": str(_checkpoint_path(ttn)),
            })

        if confirmation_number:
            raise StageError(stage, "ORDER_CREATED_HISTORY_NOT_CONFIRMED", {
                "confirmation_order_number": confirmation_number, "http_status": status,
                "request_path": response_path, "submit_state": "needs_reconciliation",
                "checkpoint": str(_checkpoint_path(ttn)),
            })

        if status is not None and 400 <= status < 500:
            rejection = _classify_submit_rejection(status, response_payload, items)
            _write_checkpoint(ttn, "rejected", http_status=status)
            message = rejection["message"] if rejection else f"SUPPLIER_REJECTED_HTTP_{status}"
            raise StageError(stage, message, {"http_status": status, "supplier_message": supplier_message,
                                               "request_path": response_path,
                                               "submit_state": "rejected"})

        details = {"http_status": status, "request_path": response_path, "supplier_message": supplier_message,
                   "submit_state": "needs_reconciliation", "checkpoint": str(_checkpoint_path(ttn))}
        raise StageError(stage, "SUBMIT_OUTCOME_AMBIGUOUS_CHECK_HISTORY_BEFORE_RETRY", details)
    except StageError:
        raise
    except Exception as exc:
        raise StageError(stage, "SUBMIT_OUTCOME_AMBIGUOUS_CHECK_HISTORY_BEFORE_RETRY",
                         await _debug(page, stage, "confirmation_not_found", {
                             "checkpoint": str(_checkpoint_path(ttn)), "exception_type": type(exc).__name__,
                             "exception": str(exc),
                         })) from exc


async def _run() -> dict[str, Any]:
    if SUP4_STAGE not in {"run", "login", "clear_cart", "add_items", "checkout_ttn"}:
        raise RuntimeError("Unsupported SUP4_STAGE")
    items = _parse_items() if SUP4_STAGE in {"run", "add_items", "checkout_ttn"} else []
    if SUP4_STAGE in {"run", "checkout_ttn"}:
        _validated_ttn(SUP4_TTN)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP4_HEADLESS)
        state_path = _storage_state_path()
        context_options: dict[str, Any] = {"viewport": {"width": 1440, "height": 1080}}
        if state_path.is_file():
            context_options["storage_state"] = str(state_path)
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        try:
            await _login(page)
            await _save_storage_state(context)
            if SUP4_STAGE == "login":
                return {"ok": True, "stage": "login", "url": page.url}

            if SUP4_STAGE == "run":
                existing = await _existing_order_result(page, SUP4_TTN, items)
                if existing:
                    return existing
                checkpoint = _read_checkpoint(SUP4_TTN)
                if checkpoint and checkpoint.get("state") in {"submit_in_flight", "confirmed"}:
                    raise StageError("preflight_history", "PRIOR_SUBMIT_UNRESOLVED_CHECKPOINT", {
                        "checkpoint": str(_checkpoint_path(SUP4_TTN)),
                        "prior_state": checkpoint.get("state"),
                        "instruction": "Inspect MonsterLab history and this checkpoint before any retry",
                    })

            store_preflight = {"verified": False, "deferred": True, "reason": "NOT_RUN"}
            if SUP4_STAGE in {"run", "add_items"}:
                store_preflight = await _preflight_store_availability(page)

            if SUP4_CLEAR_BASKET and SUP4_STAGE in {"run", "clear_cart", "add_items"}:
                await _clear_cart(page)
            if SUP4_STAGE == "clear_cart":
                return {"ok": True, "stage": "clear_cart", "cart_empty": True, "url": page.url}

            await page.goto(f"{SUP4_BASE_URL}/cabinet/catalog", wait_until="domcontentloaded")
            checks: list[dict[str, Any]] = []
            if SUP4_STAGE in {"run", "add_items"}:
                for item in items:
                    checks.append(await _add_item(page, item))
                actual = await _read_cart(page)
                comparison = _compare_order(items, actual)
                if not comparison["verified"]:
                    raise StageError("add_items", "CART_ORDER_MISMATCH", await _debug(page, "add_items", "order_mismatch", {"comparison": comparison, "actual": actual}))
                prices = {str(row["sku"]).casefold(): int(row["unit_price_minor"]) for row in checks}
                price_comparison = _compare_cart_prices(items, actual, prices)
                if not price_comparison["verified"]:
                    raise StageError("add_items", "CART_PRICE_MISMATCH", await _debug(page, "add_items", "price_mismatch", price_comparison))
                checks = [{**check, **price} for check, price in zip(
                    _cart_qty_checks(items, actual),
                    [{"unit_price_minor": prices[item.sku.casefold()],
                      "line_total_minor": prices[item.sku.casefold()] * item.qty} for item in items],
                )]
                if SUP4_STAGE == "add_items":
                    return {"ok": True, "stage": "add_items", "cart_qty_checks": checks, "cart": actual,
                            "price_checks": price_comparison, "store_preflight": store_preflight, "url": page.url}
            else:
                actual = await _read_cart(page)
                comparison = _compare_order(items, actual)
                if not comparison["verified"]:
                    raise StageError("checkout_ttn", "CART_ORDER_MISMATCH", await _debug(page, "checkout_ttn", "initial_cart_mismatch", {"comparison": comparison, "actual": actual}))
                checks = []
                by_sku = {row["sku"].casefold(): row for row in actual}
                for item, qty_check in zip(items, _cart_qty_checks(items, actual)):
                    row = by_sku[item.sku.casefold()]
                    if int(row["price"] or 0) % item.qty:
                        raise StageError("checkout_ttn", "CART_PRICE_NOT_DIVISIBLE", {"sku": item.sku})
                    unit_price = int(row["price"] or 0) // item.qty
                    checks.append({**qty_check, "unit_price_minor": unit_price,
                                   "line_total_minor": unit_price * item.qty})

            await page.goto(f"{SUP4_BASE_URL}/cabinet/checkout", wait_until="domcontentloaded")
            await page.get_by_role("heading", name="Оформлення замовлення", exact=True).wait_for(state="visible", timeout=SUP4_TIMEOUT_MS)
            await _select_checkout_options(page)
            checkout_items = await _read_checkout_items(page)
            checkout_comparison = _compare_order(items, checkout_items)
            if not checkout_comparison["verified"]:
                raise StageError("checkout_items", "CHECKOUT_ORDER_MISMATCH", await _debug(page, "checkout_items", "checkout_mismatch", {"comparison": checkout_comparison, "actual": checkout_items}))

            label = await _fill_ttn_and_attach(page, SUP4_TTN)
            actual_after = await _read_checkout_items(page)
            comparison_after = _compare_order(items, actual_after)
            if not comparison_after["verified"]:
                raise StageError("checkout_ttn", "CHECKOUT_ORDER_MISMATCH", await _debug(page, "checkout_ttn", "post_label_order_mismatch", {"comparison": comparison_after, "actual": actual_after}))
            readiness = await _verify_submit_ready(page)
            expected_total = sum(int(check["line_total_minor"]) for check in checks)
            if readiness["total_minor"] != expected_total:
                raise StageError("checkout_ready", "CHECKOUT_TOTAL_MISMATCH", await _debug(page, "checkout_ready", "total_mismatch", {"expected_total_minor": expected_total, "actual_total_minor": readiness["total_minor"]}))
            if SUP4_ALLOW_SUBMIT:
                confirmation = await _submit_and_confirm(page, SUP4_TTN, items)
                _write_checkpoint(SUP4_TTN, "confirmed", supplier_order_number=confirmation["supplier_order_number"])
                return {"ok": True, "stage": "confirmed", "submit_state": "confirmed", "url": page.url,
                        "cart_qty_checks": checks, "cart": actual, "cart_comparison": comparison_after,
                        "ttn": label, "label": label, **readiness, **confirmation}
            return {"ok": True, "stage": "prepared", "submit_state": "prepared", "submitted": False, "submit_blocked": True,
                    "submit_block_reason": "SUBMIT_BLOCKED", "supplier_order_number": "", "url": page.url,
                    "cart_qty_checks": checks, "cart": actual, "cart_comparison": comparison_after,
                    "checkout_comparison": checkout_comparison, "store_preflight": store_preflight,
                    "ttn": label, "label": label, **readiness}
        except StageError as exc:
            if "artifacts" not in exc.details:
                exc.details = {**exc.details, "diagnostics": await _debug(
                    page, exc.stage, "stage_error", {"error": str(exc), "details": exc.details})}
            raise
        finally:
            await context.close()
            await browser.close()


def main() -> int:
    cleanup_startup(ROOT)
    try:
        with _account_lock():
            print(json.dumps(asyncio.run(_run()), ensure_ascii=False))
        return 0
    except StageError as exc:
        print(json.dumps(_sanitize_diagnostic({"ok": False, "stage": exc.stage, "error": str(exc), "details": exc.details}), ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps(_sanitize_diagnostic({"ok": False, "stage": SUP4_STAGE, "error": str(exc)}), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
