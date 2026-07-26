import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BASE_URL = (os.getenv("SUP2_BASE_URL") or "https://dobavki.ua/ua").strip().rstrip("/")
HOME_URL = f"{BASE_URL}/"
CHECKOUT_URL = f"{BASE_URL}/checkout/"
UKRAINIAN_SITE = BASE_URL.rstrip("/").endswith("/ua")
PAYMENT_COD_VALUE = "15"
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
NAV_TIMEOUT_MS = max(TIMEOUT_MS, _to_int(os.getenv("SUP2_NAV_TIMEOUT_MS", "45000"), 45000))
HEADLESS = _to_bool(os.getenv("SUP2_HEADLESS", "0"), False)
CLEAR_BASKET = _to_bool(os.getenv("SUP2_CLEAR_BASKET", "1"), True)
DEBUG_PAUSE_SECONDS = _to_int(os.getenv("SUP2_DEBUG_PAUSE_SECONDS", "0"), 0)
DRY_RUN = _to_bool(os.getenv("SUP2_DRY_RUN", "0"), False)
# In a dry run the page is specifically being inspected, so keep a local
# record by default. Production runs stay opt-in because these files contain
# checkout data.
DEBUG_ARTIFACTS = _to_bool(os.getenv("SUP2_DEBUG_ARTIFACTS", "1" if DRY_RUN else "0"), DRY_RUN)
DEBUG_ARTIFACT_DIR = (os.getenv("SUP2_DEBUG_ARTIFACT_DIR") or "tmp/supplier2_debug").strip()
MANUAL_SUBMIT_WAIT_SECONDS = _to_int(os.getenv("SUP2_MANUAL_SUBMIT_WAIT_SECONDS", "0"), 0)
# Do not move focus from the last recipient field to the city autocomplete
# before submit. The old behaviour opened the city suggestions immediately
# before the order button was pressed.
SKIP_FINAL_FIELD_TAB = _to_bool(os.getenv("SUP2_SKIP_FINAL_FIELD_TAB", "1"), True)
PROMO_CODE = (os.getenv("SUP2_PROMO_CODE") or "SALE15").strip()
DISABLE_CITY_API = _to_bool(os.getenv("SUP2_DISABLE_CITY_API", "0"), False)
NP_API_KEY = (
    os.getenv("SUP2_NP_API_KEY")
    or os.getenv("BIOTUS_NP_API_KEY")
    or os.getenv("NP_API_KEY")
    or ""
).strip()
PRICE_TOLERANCE_UAH = Decimal("3")


@dataclass(frozen=True)
class Item:
    sku: str
    qty: int


@dataclass(frozen=True)
class Recipient:
    name: str
    phone_input: str
    phone_source: str
    city_query: str
    city_geo_hints: tuple[str, ...]
    branch_number: str
    branch_query: str
    branch_address: str
    delivery_kind: str
    email: str = ""


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.details = details or {}


def _write_submit_checkpoint(url: str, submitted: bool, order_number: str = "", **extra: Any) -> None:
    payload = {
        "ts": int(time.time()),
        "url": str(url or ""),
        "submitted": bool(submitted),
        "order_number": str(order_number or ""),
    }
    payload.update(extra)
    try:
        tmp = SUBMIT_CHECKPOINT_FILE.with_suffix(SUBMIT_CHECKPOINT_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SUBMIT_CHECKPOINT_FILE)
    except Exception:
        pass


async def _debug_pause_if_needed() -> None:
    if DEBUG_PAUSE_SECONDS > 0:
        await asyncio.sleep(DEBUG_PAUSE_SECONDS)


def _debug_dir_path() -> Path:
    path = Path(DEBUG_ARTIFACT_DIR)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _capture_debug_artifacts(page, stage: str, label: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist the exact rendered checkout state before a debug pause/close."""
    details: dict[str, Any] = dict(extra or {})
    details["url"] = page.url if page is not None else CHECKOUT_URL
    if not DEBUG_ARTIFACTS or page is None:
        return details

    # The checkout uses both hidden native selects and SelectBoxIt widgets.
    # Persist the actual delivery payload as well as the visible UI, so a
    # later server-side reset can be compared without submitting another order.
    try:
        details["checkout_delivery_payload"] = await page.evaluate(
            """() => {
                const checkout = document.querySelector('section.checkout') || document;
                const form = checkout.querySelector('form');
                const controls = Array.from(checkout.querySelectorAll('input, select, textarea'));
                const relevant = controls.filter((el) => /^(Delivery|Payment)\[/.test(el.name || ''));
                const valueOf = (el) => el.tagName === 'SELECT'
                    ? {value: el.value, text: el.options[el.selectedIndex]?.text || ''}
                    : {value: el.value || '', type: el.type || ''};
                const selectWidget = (select) => {
                    const id = select.id || '';
                    const text = document.querySelector(`#${CSS.escape(id)}SelectBoxItText`);
                    return text ? {value: text.dataset.val || '', text: text.textContent?.trim() || ''} : null;
                };
                return {
                    form_action: form?.getAttribute('action') || '',
                    delivery_type: Array.from(document.querySelectorAll('select')).filter((el) => el.name === 'Delivery[delivery_type]').map((el) => ({native: valueOf(el), widget: selectWidget(el)})),
                    warehouse: Array.from(document.querySelectorAll('select')).filter((el) => (el.name || '').includes('warehouse.id')).map((el) => ({native: valueOf(el), widget: selectWidget(el)})),
                    fields: relevant.map((el) => ({name: el.name, ...valueOf(el)})),
                };
            }"""
        )
    except Exception as exc:
        details["checkout_delivery_payload_error"] = str(exc)

    safe_stage = re.sub(r"[^a-zA-Z0-9._-]+", "_", stage or "stage").strip("_") or "stage"
    safe_label = re.sub(r"[^a-zA-Z0-9._-]+", "_", label or "artifact").strip("_") or "artifact"
    base = _debug_dir_path() / f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns() % 1_000_000_000:09d}_{safe_stage}_{safe_label}"
    screenshot_path = base.with_suffix(".png")
    html_path = base.with_suffix(".html")
    meta_path = base.with_suffix(".json")

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        details["screenshot"] = str(screenshot_path)
    except Exception as exc:
        details["screenshot_error"] = str(exc)
    try:
        html_path.write_text(await page.content(), encoding="utf-8")
        details["html"] = str(html_path)
    except Exception as exc:
        details["html_error"] = str(exc)
    try:
        meta_path.write_text(json.dumps(details, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        details["meta"] = str(meta_path)
    except Exception as exc:
        details["meta_error"] = str(exc)
    return details


async def _dismiss_known_overlays(page) -> None:
    try:
        await page.evaluate(
            """() => {
                for (const selector of ['#langPopupOverlayJS', '#langPopupJS']) {
                    const el = document.querySelector(selector);
                    if (el) el.remove();
                }
                document.documentElement.style.overflow = '';
                document.body.style.overflow = '';
                document.body.classList.remove('modal-open', 'is-modal-open');
            }"""
        )
    except Exception:
        pass


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


def _parse_order_payload() -> dict[str, Any]:
    raw = (os.getenv("SUP2_ORDER_JSON") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"SUP2_ORDER_JSON is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError("SUP2_ORDER_JSON must be a JSON object.")
    return data


def _parse_price_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None

    text = str(value or "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    candidates: list[str] = []
    if re.fullmatch(r"\d+(?:[.,]\d+)?", text):
        candidates.append(text)
    candidates.extend(re.findall(r"(\d[\d\s]*(?:[.,]\d+)?)\s*(?:грн|uah|₴)", text, flags=re.IGNORECASE))
    if not candidates:
        candidates.extend(re.findall(r"(?<!\d)(\d{2,6}(?:[.,]\d{1,2})?)(?!\d)", text))

    for raw in candidates:
        normalized = re.sub(r"\s+", "", raw).replace(",", ".")
        try:
            return Decimal(normalized).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            continue
    return None


def _build_order_price_map(order: dict[str, Any]) -> dict[str, dict[str, Any]]:
    products = order.get("products") or []
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(products, list):
        return out

    for p in products:
        if not isinstance(p, dict):
            continue
        desc = str(p.get("description") or "").strip()
        sku = desc.split(",", 1)[0].strip() if desc else ""
        if not sku:
            sku = str(p.get("sku") or p.get("parameter") or p.get("barcode") or "").strip()
        if not sku:
            continue

        price_field = ""
        price_raw: Any = None
        for field in ("price", "costPerItem"):
            if p.get(field) not in (None, ""):
                parsed = _parse_price_decimal(p.get(field))
                if parsed is not None:
                    price_field = field
                    price_raw = p.get(field)
                    out[sku] = {"price": parsed, "raw": price_raw, "field": price_field, "product_id": p.get("id")}
                    break
    return out


def _first_delivery(order: dict[str, Any]) -> dict[str, Any]:
    odd = order.get("ord_delivery_data") or []
    if isinstance(odd, list) and odd:
        first = odd[0]
        return first if isinstance(first, dict) else {}
    if isinstance(odd, dict):
        return odd
    return {}


def _full_name_from_order(order: dict[str, Any]) -> str:
    pc = order.get("primaryContact") or {}
    if not isinstance(pc, dict):
        pc = {}
    last = str(pc.get("lName") or "").strip()
    first = str(pc.get("fName") or "").strip()
    middle = _patronymic_from_order(order)
    return " ".join(x for x in (last, first, middle) if x).strip()


def _patronymic_from_order(order: dict[str, Any]) -> str:
    pc = order.get("primaryContact") or {}
    if not isinstance(pc, dict):
        pc = {}
    value = str(pc.get("mName") or pc.get("middleName") or "").strip()
    return value or "Побатькові"


def _first_phone_from_order(order: dict[str, Any]) -> str:
    pc = order.get("primaryContact") or {}
    if not isinstance(pc, dict):
        pc = {}
    phones = pc.get("phone") or []
    if isinstance(phones, list) and phones:
        return str(phones[0] or "").strip()
    return str(pc.get("phone") or "").strip()


def _first_email_from_order(order: dict[str, Any]) -> str:
    pc = order.get("primaryContact") or {}
    if not isinstance(pc, dict):
        pc = {}
    emails = pc.get("email") or pc.get("emails") or []
    if isinstance(emails, list) and emails:
        return str(emails[0] or "").strip()
    return str(emails or "").strip()


def _normalize_dobavki_phone(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if digits.startswith("380") and len(digits) >= 12:
        digits = digits[3:]
    if digits.startswith("0") and len(digits) >= 10:
        digits = digits[1:]
    if len(digits) > 9:
        digits = digits[-9:]
    if len(digits) != 9:
        raise RuntimeError(f"Cannot normalize phone for Dobavki mask: {raw!r}")
    return digits


def _normalize_city_query(city: str) -> str:
    city = str(city or "").strip()
    if UKRAINIAN_SITE:
        aliases = {
            "киев": "Київ",
            "г. киев": "Київ",
            "м. київ": "Київ",
            "київ": "Київ",
        }
    else:
        aliases = {
            "київ": "Киев",
            "м. київ": "Киев",
            "г. киев": "Киев",
            "киев": "Киев",
        }
    return aliases.get(city.lower(), city)


def _norm_match_text(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яіїєґ]+", "", str(value or "").lower())


def _ru_city_variant(value: str) -> str:
    text = str(value or "").strip()
    replacements = (
        ("Київ", "Киев"),
        ("київ", "киев"),
        ("івськ", "овск"),
        ("івський", "овский"),
        ("івська", "овская"),
        ("івське", "овское"),
        ("івка", "овка"),
        ("і", "и"),
        ("ї", "и"),
        ("є", "е"),
        ("ґ", "г"),
        ("І", "И"),
        ("Ї", "И"),
        ("Є", "Е"),
        ("Ґ", "Г"),
    )
    for src, dst in replacements:
        text = text.replace(src, dst)
    return text


def _unique_nonempty(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = re.sub(r"\s+", " ", str(value or "").strip())
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            out.append(clean)
    return out


def _city_search_terms(city: str) -> list[str]:
    raw = str(city or "").strip()
    no_parentheses = re.sub(r"\s*\([^)]*\)", "", raw).strip()
    before_comma = raw.split(",", 1)[0].strip()
    candidates = [raw, no_parentheses, before_comma]
    candidates.extend(_ru_city_variant(x) for x in list(candidates))
    if UKRAINIAN_SITE:
        aliases = {
            "київ": ["Київ", "Киев"],
            "киев": ["Київ", "Киев"],
            "львів": ["Львів", "Львов"],
            "львов": ["Львів", "Львов"],
            "харків": ["Харків", "Харьков"],
            "харьков": ["Харків", "Харьков"],
            "миколаїв": ["Миколаїв", "Николаев"],
            "николаев": ["Миколаїв", "Николаев"],
            "черкаси": ["Черкаси", "Черкассы"],
            "черкассы": ["Черкаси", "Черкассы"],
            "одеса": ["Одеса", "Одесса"],
            "одесса": ["Одеса", "Одесса"],
        }
    else:
        aliases = {
            "київ": ["Киев"],
            "киев": ["Киев", "Київ"],
            "львів": ["Львов"],
            "львов": ["Львов", "Львів"],
            "харків": ["Харьков"],
            "харьков": ["Харьков", "Харків"],
            "миколаїв": ["Николаев"],
            "николаев": ["Николаев", "Миколаїв"],
            "черкаси": ["Черкассы"],
            "черкассы": ["Черкассы", "Черкаси"],
            "одеса": ["Одесса"],
            "одесса": ["Одесса", "Одеса"],
        }
    for candidate in list(candidates):
        candidates.extend(aliases.get(candidate.lower(), []))
    return _unique_nonempty(candidates)


def _city_hint_terms(city: str) -> list[str]:
    raw = str(city or "").strip()
    hints: list[str] = []
    hints.extend(re.findall(r"\(([^)]*)\)", raw))
    hints.extend(part.strip() for part in re.split(r"[,;]", raw)[1:])
    expanded: list[str] = []
    for hint in hints:
        tokens = re.split(r"[\s,.;/]+", hint)
        for token in tokens:
            token = token.strip("()")
            if len(token) < 3:
                continue
            if token.lower() in {"р-н", "рн", "район", "обл", "область", "тг"}:
                continue
            expanded.append(token)
            expanded.append(_ru_city_variant(token))
    return _unique_nonempty(expanded)


def _delivery_city_geo_hints(delivery: dict[str, Any], city_name: str) -> tuple[str, ...]:
    """Return the non-city parts that disambiguate identically named NP cities.

    SalesDrive commonly keeps a settlement name in ``cityName`` and its district
    and region in separate fields.  The checkout autocomplete returns several
    same-name cities, so looking at ``cityName`` alone can silently select a city
    in another oblast (for example Кам'янка-Бузька instead of Кам'янка).
    """
    values = [
        city_name,
        str(delivery.get("areaName") or ""),
        str(delivery.get("regionName") or ""),
    ]
    hints: list[str] = []
    for value in values:
        hints.extend(_city_hint_terms(value))
        # Some SalesDrive payloads use a plain oblast name without commas or
        # parentheses; retain it as a whole autocomplete hint too.
        clean = re.sub(r"\s+", " ", value).strip()
        if len(clean) >= 3:
            hints.append(clean)
            hints.append(_ru_city_variant(clean))
    return tuple(_unique_nonempty(hints))


def _delivery_match_tokens(text: str, branch_number: str = "", city_name: str = "", extra_city_values: list[str] | None = None) -> list[str]:
    city_tokens = {
        _ru_city_variant(token.lower()).strip("№#")
        for token in re.split(
            r"[\s,.;:/()\"«»]+",
            " ".join([city_name, _ru_city_variant(city_name), *(extra_city_values or [])]),
        )
        if len(token) >= 4
    }
    generic_tokens = {
        "відділення",
        "отделение",
        "пункт",
        "приймання",
        "видачі",
        "выдачи",
        "прим",
        "поштомат",
        "почтомат",
        "нова",
        "новая",
        "пошта",
        "почта",
        "магазин",
        "одне",
        "место",
        "місце",
        "вул",
        "ул",
        "улица",
        "приймання-видачі",
        "прийманнявидачі",
        "прийманнявидачи",
        "приема-выдачи",
        "приемавыдачи",
    }
    branch_norm = str(branch_number or "").strip()
    tokens: list[str] = []
    for token in re.split(r"[\s,.;:/()\"«»]+", str(text or "")):
        token = token.strip()
        token_norm = _ru_city_variant(token.lower()).strip("№#")
        token_compact = re.sub(r"[^0-9a-zа-яіїєґ]+", "", token_norm)
        if not token_norm or token.startswith(("№", "#")):
            continue
        if token_norm == branch_norm:
            continue
        if token_norm.isdigit() and token_norm in {"1", "2", "3", "5", "10", "30"}:
            continue
        if len(token_norm) < 3 and not token_norm.isdigit():
            continue
        if token_norm in generic_tokens or token_compact in generic_tokens or token_norm in city_tokens or token_compact in city_tokens:
            continue
        tokens.append(token)
    return _unique_nonempty(tokens)


def _np_warehouse_lookup(city_name: str, branch_number: str) -> dict[str, Any]:
    if not NP_API_KEY or not city_name or not branch_number:
        return {"ok": False, "reason": "np_lookup_unavailable"}
    payload = {
        "apiKey": NP_API_KEY,
        "modelName": "AddressGeneral",
        "calledMethod": "getWarehouses",
        "methodProperties": {
            "CityName": city_name,
            "FindByString": str(branch_number),
            "Page": "1",
            "Limit": "50",
        },
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.novaposhta.ua/v2.0/json/",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "reason": "np_lookup_error", "error": str(e)}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"ok": False, "reason": "np_lookup_non_json", "raw": raw[:300]}
    if not parsed.get("success"):
        return {"ok": False, "reason": "np_lookup_failed", "errors": parsed.get("errors"), "warnings": parsed.get("warnings")}
    wanted = str(branch_number).strip()
    rows = parsed.get("data") if isinstance(parsed.get("data"), list) else []
    exact = None
    for row in rows:
        if str((row or {}).get("Number") or "").strip() == wanted:
            exact = row
            break
    if not exact:
        return {"ok": False, "reason": "np_branch_not_found", "count": len(rows)}
    text = " ".join(
        str(exact.get(key) or "")
        for key in ("Description", "DescriptionRu", "ShortAddress", "ShortAddressRu")
    )
    tokens = _delivery_match_tokens(
        text,
        branch_number,
        city_name,
        [
            str(exact.get("CityDescription") or ""),
            str(exact.get("CityDescriptionRu") or ""),
            str(exact.get("SettlementDescription") or ""),
            str(exact.get("SettlementDescriptionRu") or ""),
        ],
    )
    return {
        "ok": True,
        "number": str(exact.get("Number") or ""),
        "description": str(exact.get("Description") or ""),
        "short_address": str(exact.get("ShortAddress") or ""),
        "tokens": _unique_nonempty(tokens),
    }


def _branch_number_from_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(int(value))
    except Exception:
        return str(value).strip()


def _delivery_kind_from_delivery(delivery: dict[str, Any], branch_query: str) -> str:
    text = " ".join(
        str(value or "")
        for value in (
            branch_query,
            delivery.get("address"),
            delivery.get("shipping_address"),
            delivery.get("type"),
        )
    ).lower()
    if "поштомат" in text or "почтомат" in text or "postomat" in text:
        return "postomat"
    return "warehouse"


def _normalize_delivery_text(value: str) -> str:
    text = _ru_city_variant(str(value or "").lower())
    replacements = (
        ("вулиця", "ул"),
        ("вул.", "ул"),
        ("вул ", "ул "),
        ("улица", "ул"),
        ("проспект", "просп"),
        ("пр-т", "просп"),
        ("провулок", "пер"),
        ("переулок", "пер"),
    )
    for src, dst in replacements:
        text = text.replace(src, dst)
    return re.sub(r"[^0-9a-zа-яіїєґ]+", "", text)


def _delivery_number_matches(text: str, branch_number: str) -> bool:
    number = str(branch_number or "").strip()
    if not number:
        return False
    return bool(re.search(rf"(?:№|#)\s*{re.escape(number)}(?!\d)", str(text or ""), flags=re.IGNORECASE))


def _delivery_address_numbers(text: str, branch_number: str) -> list[str]:
    """Extract building numbers, excluding branch number and weight limits."""
    value = re.sub(r"\([^)]*\)", " ", str(text or ""))
    # Delivery labels put the actual address after a colon.  This also avoids
    # interpreting "Відділення №2" as a house number.
    if ":" in value:
        value = value.rsplit(":", 1)[-1]
    branch = str(branch_number or "").strip()
    numbers = re.findall(r"(?<!\d)(\d+(?:\s*[-/]\s*[0-9a-zа-яіїєґ]+)?)(?!\d)", value, flags=re.IGNORECASE)
    return _unique_nonempty(number for number in numbers if re.sub(r"\D", "", number) != branch)


def _selected_has_address_number(selected_text: str, wanted_number: str) -> bool:
    wanted = re.sub(r"\s+", "", str(wanted_number or "")).lower()
    if not wanted:
        return False
    selected_numbers = _delivery_address_numbers(selected_text, "")
    return wanted in {re.sub(r"\s+", "", value).lower() for value in selected_numbers}


def _validate_selected_delivery_text(
    *,
    selected_text: str,
    recipient: Recipient,
    match_reason: str,
    np_lookup: dict[str, Any],
    extra_tokens: list[str] | None = None,
) -> dict[str, Any]:
    branch_number_matches = _delivery_number_matches(selected_text, recipient.branch_number)
    expected_address_numbers = _delivery_address_numbers(recipient.branch_address, recipient.branch_number)
    matched_address_numbers = [
        number for number in expected_address_numbers if _selected_has_address_number(selected_text, number)
    ]
    # A branch number is only unique inside a city.  When SalesDrive supplied a
    # house number, require it too: it prevents an equally numbered branch in a
    # wrongly selected city from passing validation.
    if branch_number_matches and (not expected_address_numbers or matched_address_numbers):
        return {
            "valid": True,
            "reason": "branch_number_and_address" if expected_address_numbers else "branch_number",
            "score": len(matched_address_numbers),
            "hits": matched_address_numbers,
        }
    if branch_number_matches and expected_address_numbers:
        return {
            "valid": False,
            "reason": "branch_number_address_mismatch",
            "score": 0,
            "hits": [],
            "expected_address_numbers": expected_address_numbers,
        }

    tokens = list(extra_tokens or [])
    if isinstance(np_lookup, dict) and isinstance(np_lookup.get("tokens"), list):
        tokens.extend(np_lookup.get("tokens") or [])
    token_norms = _unique_nonempty(
        [_normalize_delivery_text(str(token or "")) for token in tokens if _normalize_delivery_text(str(token or ""))]
    )
    selected_norm = _normalize_delivery_text(selected_text)
    selected_tokens = [_normalize_delivery_text(token) for token in re.split(r"[^0-9a-zа-яіїєґ]+", selected_text.lower()) if token]
    hits = [
        token
        for token in token_norms
        if (token.isdigit() and token in selected_tokens)
        or (any(ch.isdigit() for ch in token) and token in selected_norm)
        or (not token.isdigit() and len(token) >= 4 and token in selected_norm)
    ]
    if match_reason in {"np_address", "address"} and len(hits) >= 2:
        return {"valid": True, "reason": match_reason, "score": len(hits), "hits": hits}

    return {"valid": False, "reason": match_reason or "unmatched", "score": len(hits), "hits": hits}


def _extract_recipient(order: dict[str, Any]) -> Recipient:
    delivery = _first_delivery(order)
    env_name = (os.getenv("SUP2_CUSTOMER_NAME") or "").strip()
    env_phone = (os.getenv("SUP2_CUSTOMER_PHONE") or "").strip()
    env_city = (os.getenv("SUP2_CITY_NAME") or "").strip()
    env_branch = (os.getenv("SUP2_BRANCH_NUMBER") or "").strip()
    env_branch_query = (os.getenv("SUP2_BRANCH_QUERY") or "").strip()
    env_email = (os.getenv("SUP2_CUSTOMER_EMAIL") or "").strip()
    env_patronymic = (os.getenv("SUP2_CUSTOMER_PATRONYMIC") or "").strip()

    patronymic = env_patronymic or _patronymic_from_order(order)
    name = env_name or _full_name_from_order(order)
    if env_name and patronymic and _norm_match_text(patronymic) not in _norm_match_text(name):
        name = f"{name} {patronymic}".strip()
    phone_source = env_phone or _first_phone_from_order(order)
    city_raw = env_city or str(delivery.get("cityName") or "").strip()
    branch_number = env_branch or _branch_number_from_value(delivery.get("branchNumber"))
    branch_query = env_branch_query or str(delivery.get("address") or "").strip()
    branch_address = str(order.get("shipping_address") or delivery.get("shipping_address") or "").strip()
    delivery_kind = _delivery_kind_from_delivery(delivery, branch_query)
    email = env_email or _first_email_from_order(order)

    missing = []
    if not name:
        missing.append("customer_name")
    if not phone_source:
        missing.append("customer_phone")
    if not city_raw:
        missing.append("city")
    if not branch_number and not branch_query:
        missing.append("branch_number_or_address")
    if missing:
        raise RuntimeError(f"Missing required SUP2 checkout data: {', '.join(missing)}")

    return Recipient(
        name=name,
        phone_input=_normalize_dobavki_phone(phone_source),
        phone_source=phone_source,
        city_query=_normalize_city_query(city_raw),
        city_geo_hints=_delivery_city_geo_hints(delivery, city_raw),
        branch_number=branch_number,
        branch_query=branch_query,
        branch_address=branch_address,
        delivery_kind=delivery_kind,
        email=email,
    )


async def _goto_retry(page, url: str, *, wait_until: str = "domcontentloaded", timeout: int = NAV_TIMEOUT_MS, attempts: int = 2) -> None:
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            await _dismiss_known_overlays(page)
            return
        except PWTimeoutError as e:
            last_error = e
            if attempt >= attempts:
                raise
            await page.wait_for_timeout(700)
    if last_error is not None:
        raise last_error


async def _safe_wait_networkidle(page, timeout: int = 3000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeoutError:
        pass


async def _clear_basket(page) -> dict:
    await _goto_retry(page, CHECKOUT_URL)
    removed = 0
    while True:
        remove_links = page.locator("a.order-i-remove.j-remove-p")
        try:
            count = await remove_links.count()
        except Exception:
            count = 0
        if count <= 0:
            break
        if removed >= 50:
            raise StageError("clear_basket", "Too many cart rows removed.", {"removed": removed})
        await remove_links.nth(0).click(timeout=TIMEOUT_MS, force=True)
        removed += 1
        await page.wait_for_timeout(900)

    coupon_remove = page.locator("a.j-coupon-remove")
    if await coupon_remove.count() > 0:
        try:
            await coupon_remove.first.click(timeout=3000, force=True)
            await page.wait_for_timeout(500)
        except Exception:
            pass

    remaining = await page.locator("a.order-i-remove.j-remove-p").count()
    if remaining:
        raise StageError("clear_basket", "Basket is not empty after cleanup.", {"remaining": remaining, "removed": removed})
    return {"removed": removed}


async def _wait_search_result(page, sku: str) -> list[dict[str, str]]:
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    latest: list[dict[str, str]] = []
    while asyncio.get_running_loop().time() < deadline:
        latest = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a.search-results__link')).map((a) => ({
                text: (a.innerText || '').trim(),
                href: a.href || ''
            })).filter((x) => x.href)"""
        )
        if latest:
            return latest
        await page.wait_for_timeout(250)
    raise StageError("add_items", f"Search result did not appear for sku={sku}", {"sku": sku, "latest": latest})


def _extract_article_match(body_text: str, sku: str) -> str:
    sku_value = str(sku or "").strip()
    if not sku_value:
        return ""
    pattern = rf"Артикул\s*:\s*({re.escape(sku_value)})\b"
    m = re.search(pattern, body_text or "", flags=re.IGNORECASE)
    return m.group(1) if m else ""


async def _open_product_candidate(page, sku: str, href: str, result_text: str = "") -> dict[str, str] | None:
    await _goto_retry(page, href)
    await _safe_wait_networkidle(page)

    body_text = await page.locator("body").inner_text(timeout=TIMEOUT_MS)
    article_match = _extract_article_match(body_text, sku)
    if not article_match:
        return None

    h1 = ""
    try:
        h1 = (await page.locator("h1").first.inner_text(timeout=TIMEOUT_MS)).strip()
    except Exception:
        pass
    return {"sku": sku, "href": page.url or href, "title": h1, "search_text": result_text, "article": article_match}


async def _find_product_via_search_page(page, sku: str) -> dict[str, str]:
    await _goto_retry(page, f"{BASE_URL}/catalog/search/?q={sku}")
    candidates = await page.evaluate(
        """() => {
            const out = [];
            const push = (a) => {
                if (!a) return;
                const text = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
                const href = a.href || '';
                if (!href || !text) return;
                if (href.includes('/catalog/search') || href.includes('/filter/') || href.includes('#')) return;
                if (!href.startsWith(location.origin + '/')) return;
                out.push({text, href});
            };
            for (const card of Array.from(document.querySelectorAll('main .catalogCard'))) {
                push(card.querySelector('.catalogCard-title a[href]') || card.querySelector('a[href]'));
            }
            if (!out.length) {
                for (const a of Array.from(document.querySelectorAll('main .catalogCard-title a[href]'))) push(a);
            }
            return out;
        }"""
    )
    seen: set[str] = set()
    for candidate in candidates[:3]:
        href = str(candidate.get("href") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        product = await _open_product_candidate(page, sku, href, str(candidate.get("text") or ""))
        if product:
            return product
    raise StageError("add_items", f"No exact product page found for sku={sku}", {"sku": sku, "candidates": candidates[:10]})


async def _search_and_open_product(page, sku: str) -> dict[str, str]:
    try:
        return await _find_product_via_search_page(page, sku)
    except StageError as search_page_error:
        search_page_details = search_page_error.details

    await _goto_retry(page, HOME_URL)
    search = page.locator("input[name='q']").first
    await search.wait_for(state="visible", timeout=TIMEOUT_MS)
    await search.click(timeout=TIMEOUT_MS)
    await search.fill("", timeout=TIMEOUT_MS)
    await search.type(sku, delay=35, timeout=TIMEOUT_MS)

    results = await _wait_search_result(page, sku)
    seen: set[str] = set()
    for result in results[:5]:
        href = str(result.get("href") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        product = await _open_product_candidate(page, sku, href, result.get("text", ""))
        if product:
            return product
    raise StageError(
        "add_items",
        f"No exact product page found for sku={sku}",
        {"sku": sku, "quick_results": results[:10], "search_page": search_page_details},
    )


async def _extract_product_page_price(page, sku: str) -> dict[str, Any]:
    candidates = await page.evaluate(
        """() => {
            const selectors = [
                "meta[itemprop='price']",
                "[itemprop='price'][content]",
                "[data-price]",
                ".product-price__main",
                ".product-price",
                ".productCard-price",
                ".price"
            ];
            const out = [];
            const push = (selector, el) => {
                if (!el) return;
                const raw = el.getAttribute('content')
                    || el.getAttribute('data-price')
                    || el.getAttribute('value')
                    || el.textContent
                    || '';
                const text = String(raw).replace(/\\s+/g, ' ').trim();
                if (text) out.push({selector, text});
            };
            for (const selector of selectors) {
                for (const el of Array.from(document.querySelectorAll(selector)).slice(0, 6)) {
                    push(selector, el);
                }
                if (out.length) break;
            }
            if (!out.length) {
                const body = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ');
                for (const match of body.matchAll(/\\d[\\d\\s]*(?:[,.]\\d+)?\\s*(?:грн|UAH|₴)/gi)) {
                    out.push({selector: 'body_fallback', text: match[0].trim()});
                    if (out.length >= 8) break;
                }
            }
            return out;
        }"""
    )
    if not isinstance(candidates, list):
        candidates = []

    parsed_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        parsed = _parse_price_decimal(candidate.get("text"))
        row = {**candidate, "price": str(parsed) if parsed is not None else None}
        parsed_candidates.append(row)
        if parsed is not None:
            return {"sku": sku, "price": parsed, "raw": candidate.get("text"), "selector": candidate.get("selector"), "candidates": parsed_candidates}

    raise StageError(
        "price_check",
        f"PRICE_NOT_FOUND_ON_SITE: sku={sku}",
        {"sku": sku, "url": page.url or "", "candidates": parsed_candidates},
    )


async def _verify_product_price(page, item: Item, product: dict[str, Any], order_price_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    order_price_info = order_price_map.get(item.sku)
    if not order_price_info:
        raise StageError(
            "price_check",
            f"ORDER_PRICE_NOT_FOUND: sku={item.sku}",
            {"sku": item.sku, "product": product},
        )

    order_price = order_price_info.get("price")
    if not isinstance(order_price, Decimal):
        raise StageError(
            "price_check",
            f"ORDER_PRICE_NOT_PARSED: sku={item.sku}",
            {"sku": item.sku, "order_price": order_price_info},
        )

    site_price_info = await _extract_product_page_price(page, item.sku)
    site_price = site_price_info["price"]
    diff = abs(site_price - order_price)
    check = {
        "sku": item.sku,
        "qty": item.qty,
        "order_price": str(order_price),
        "order_price_raw": order_price_info.get("raw"),
        "order_price_field": order_price_info.get("field"),
        "site_price": str(site_price),
        "site_price_raw": site_price_info.get("raw"),
        "site_price_selector": site_price_info.get("selector"),
        "difference": str(diff),
        "tolerance": str(PRICE_TOLERANCE_UAH),
        "product": product,
    }
    if diff > PRICE_TOLERANCE_UAH:
        raise StageError(
            "price_check",
            f"PRICE_MISMATCH: sku={item.sku} order={order_price} site={site_price} diff={diff} грн > {PRICE_TOLERANCE_UAH} грн",
            check,
        )
    print(f"[SUP2] price check ok: sku={item.sku} order={order_price} site={site_price} diff={diff}")
    return check


async def _wait_ajax_cart_product(page, product_id: str) -> dict:
    if not product_id:
        await page.wait_for_timeout(1200)
        return {"product_id": "", "found": False}
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    latest: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        latest = await page.evaluate(
            """(idRaw) => {
                const out = {product_id: idRaw, hasAjaxCart: !!window.AjaxCart};
                try {
                    const ac = window.AjaxCart && window.AjaxCart.getInstance ? window.AjaxCart.getInstance() : null;
                    out.hasInstance = !!ac;
                    out.initialized = !!(ac && ac.Cart && ac.Cart.initialized);
                    out.ajaxProcessing = ac && ac.Cart ? Number(ac.Cart.ajaxProcessing || 0) : null;
                    const product = ac && ac.getProductById ? ac.getProductById(Number(idRaw), 'product') : null;
                    out.found = !!product;
                    out.quantity = product ? product.quantity : 0;
                    out.totalQuantity = ac && ac.Cart && ac.Cart.total ? ac.Cart.total.quantity : null;
                } catch (e) {
                    out.error = String(e);
                }
                return out;
            }""",
            product_id,
        )
        if latest.get("found") and int(latest.get("ajaxProcessing") or 0) == 0:
            return latest
        await page.wait_for_timeout(300)
    return latest


async def _add_items(page, items: list[Item], order_price_map: dict[str, dict[str, Any]]) -> list[dict]:
    added: list[dict] = []
    for item in items:
        product = await _search_and_open_product(page, item.sku)
        price_check = await _verify_product_price(page, item, product, order_price_map)
        product_added = False
        for attempt in range(1, 4):
            if attempt > 1:
                await _goto_retry(page, product["href"])
                await _safe_wait_networkidle(page, timeout=6000)
            product_id = await page.evaluate(
                """() => {
                    const btn = document.querySelector('button.j-buy-button-add, button.j-buy-button-remove');
                    return btn && btn.id ? (btn.id.match(/(\\d+)/)?.[1] || '') : '';
                }"""
            )
            buy_btn = page.locator("button.j-buy-button-add").first
            await buy_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
            box = await buy_btn.bounding_box(timeout=TIMEOUT_MS)
            if box:
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                await buy_btn.click(timeout=TIMEOUT_MS)
            try:
                await page.get_by_text(re.compile(r"Товар\s+(?:добавлен|додано|доданий)", re.IGNORECASE)).wait_for(
                    state="visible", timeout=5000
                )
            except Exception:
                await page.wait_for_timeout(1200)
            await _wait_ajax_cart_product(page, product_id)

            await _goto_retry(page, CHECKOUT_URL)
            rows = await _read_checkout_rows(page)
            if _match_row_for_item(rows, product, item.sku) is not None:
                product_added = True
                break

            await _goto_retry(page, product["href"])
            await _safe_wait_networkidle(page, timeout=6000)
            js_result = await page.evaluate(
                """async () => {
                    const btn = document.querySelector('button.j-buy-button-add, button.j-buy-button-remove');
                    const idRaw = btn && btn.id ? btn.id.match(/(\\d+)/)?.[1] : '';
                    if (!idRaw) return {ok: false, reason: 'product id not found'};
                    const out = {
                        ok: true,
                        id: idRaw,
                        ajaxCart: false,
                        directPost: null,
                        csrfPrefix: '',
                        csrfLength: 0,
                        csrfSource: '',
                        hasChallengeCookie: document.cookie.includes('challenge_passed=')
                    };
                    if (window.AjaxCart && window.AjaxCart.getInstance) {
                        window.AjaxCart.getInstance().appendProduct({type: 'product', id: Number(idRaw), quantity: 1}, undefined, true);
                        out.ajaxCart = true;
                    }
                    if (typeof window.sendAjax === 'function') {
                        out.sendAjax = await new Promise((resolve) => {
                            window.sendAjax('/_widget/ajax_cart/appendProduct/', {
                                marker: 'DEFAULT',
                                product: {type: 'product', id: Number(idRaw), quantity: 1},
                                analytics: '{}'
                            }, (status, response) => resolve({
                                status,
                                responseText: JSON.stringify(response || {}).slice(0, 300)
                            }));
                        });
                    }
                    const body = new URLSearchParams();
                    body.set('marker', 'DEFAULT');
                    body.set('product[type]', 'product');
                    body.set('product[id]', idRaw);
                    body.set('product[quantity]', '1');
                    body.set('analytics', '{}');
                    const htmlToken = (document.documentElement.outerHTML.match(/GLOBAL_CSRF_TOKEN:\\s*['"]([^'"]+)['"]/) || [])[1] || '';
                    const hiddenToken = document.querySelector('input[name="CSRFToken"]')?.value || '';
                    const globalToken = (window.GLOBAL && window.GLOBAL.GLOBAL_CSRF_TOKEN) || '';
                    const windowToken = window.GLOBAL_CSRF_TOKEN || '';
                    const csrf = globalToken || htmlToken || windowToken || hiddenToken || '';
                    out.csrfSource = globalToken ? 'GLOBAL.GLOBAL_CSRF_TOKEN' : htmlToken ? 'html' : windowToken ? 'window.GLOBAL_CSRF_TOKEN' : hiddenToken ? 'hidden' : '';
                    out.csrfPrefix = csrf ? csrf.slice(0, 6) : '';
                    out.csrfLength = csrf ? csrf.length : 0;
                    if (csrf) body.set('CSRFToken', csrf);
                    const postViaXhr = () => new Promise((resolve) => {
                        const xhr = new XMLHttpRequest();
                        xhr.open('POST', '/_widget/ajax_cart/appendProduct/');
                        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
                        xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded; charset=UTF-8');
                        if (csrf) {
                            xhr.setRequestHeader('X-CSRF-Token', csrf);
                        }
                        xhr.onload = () => resolve({status: xhr.status, text: xhr.responseText.slice(0, 300)});
                        xhr.onerror = () => resolve({status: 0, text: 'xhr error'});
                        xhr.send(body.toString());
                    });
                    if (typeof fetch === 'function') {
                        const res = await fetch('/_widget/ajax_cart/appendProduct/', {
                            method: 'POST',
                            headers: {
                                'X-Requested-With': 'XMLHttpRequest',
                                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                'X-CSRF-Token': csrf
                            },
                            body: body.toString()
                        });
                        out.directPost = {status: res.status, text: (await res.text()).slice(0, 300)};
                    } else {
                        out.directPost = await postViaXhr();
                    }
                    return out;
                }"""
            )
            direct_post = js_result.get("directPost") if isinstance(js_result, dict) else None
            send_ajax = js_result.get("sendAjax") if isinstance(js_result, dict) else None
            send_ajax_ok = isinstance(send_ajax, dict) and str(send_ajax.get("status") or "") == "OK"
            direct_post_ok = isinstance(direct_post, dict) and int(direct_post.get("status") or 0) == 200
            if not (direct_post_ok or send_ajax_ok):
                await _wait_ajax_cart_product(page, str(js_result.get("id") or product_id))
            await _goto_retry(page, CHECKOUT_URL)
            rows = await _read_checkout_rows(page)
            if _match_row_for_item(rows, product, item.sku) is not None:
                product_added = True
                break

        if not product_added:
            raise StageError(
                "add_items",
                f"Product was not found in checkout cart after add click: sku={item.sku}",
                {"sku": item.sku, "product": product, "rows": await _read_checkout_rows(page), "js_result": locals().get("js_result")},
            )
        added.append({**product, "qty": item.qty, "price_check": price_check})
    return added


async def _read_checkout_rows(page) -> list[dict[str, Any]]:
    return await page.evaluate(
        """() => Array.from(document.querySelectorAll('.order-i')).map((row, idx) => {
            const qty = row.querySelector('input.j-quantity-p');
            const titleLink = Array.from(row.querySelectorAll('a')).find((a) => (a.innerText || '').trim());
            return {
                idx,
                text: (row.innerText || '').replace(/\\s+/g, ' ').trim(),
                title: titleLink ? (titleLink.innerText || '').trim() : '',
                href: titleLink ? (titleLink.href || '') : '',
                qty: qty ? (qty.value || '') : ''
            };
        })"""
    )


def _match_row_for_item(rows: list[dict[str, Any]], added_item: dict, sku: str) -> int | None:
    href = str(added_item.get("href") or "").split("?", 1)[0].rstrip("/")
    title = str(added_item.get("title") or "").strip()
    for row in rows:
        row_href = str(row.get("href") or "").split("?", 1)[0].rstrip("/")
        if href and row_href and href == row_href:
            return int(row["idx"])
    if title:
        for row in rows:
            if title in str(row.get("text") or ""):
                return int(row["idx"])
    for row in rows:
        if sku in str(row.get("text") or ""):
            return int(row["idx"])
    return None


async def _set_item_quantities(page, items: list[Item], added: list[dict]) -> list[dict]:
    await _goto_retry(page, CHECKOUT_URL)
    await _safe_wait_networkidle(page)
    rows = await _read_checkout_rows(page)
    if not rows:
        raise StageError("set_quantities", "Checkout cart is empty after adding items.", {})

    results: list[dict] = []
    for item, added_item in zip(items, added):
        for _ in range(20):
            rows = await _read_checkout_rows(page)
            row_idx = _match_row_for_item(rows, added_item, item.sku)
            if row_idx is None:
                raise StageError(
                    "set_quantities",
                    f"Cart row not found for sku={item.sku}",
                    {"sku": item.sku, "rows": rows, "added_item": added_item},
                )
            row = page.locator(".order-i").nth(row_idx)
            qty_input = row.locator("input.j-quantity-p").first
            current_raw = (await qty_input.input_value(timeout=TIMEOUT_MS)).strip()
            try:
                current = int(current_raw)
            except Exception:
                raise StageError("set_quantities", "Invalid checkout qty value.", {"sku": item.sku, "value": current_raw})
            if current == item.qty:
                results.append({"sku": item.sku, "qty": item.qty, "row_idx": row_idx})
                break
            if current < item.qty:
                btn = row.locator("button.j-increase-p").first
            else:
                btn = row.locator("button.j-decrease-p").first
            await btn.wait_for(state="visible", timeout=TIMEOUT_MS)
            await btn.click(timeout=TIMEOUT_MS)
            await page.wait_for_timeout(900)
        else:
            raise StageError("set_quantities", f"Could not set qty for sku={item.sku}", {"sku": item.sku, "target": item.qty})

    return results


async def _select_city(page, city_query: str, city_geo_hints: tuple[str, ...] = ()) -> dict:
    city_input = page.locator("#checkout-city").first
    await city_input.wait_for(state="visible", timeout=TIMEOUT_MS)

    city_terms = _city_search_terms(city_query)
    city_hints = _unique_nonempty([*_city_hint_terms(city_query), *city_geo_hints])
    api_result = {"ok": False, "reason": "city_api_disabled", "cityTerms": city_terms, "cityHints": city_hints}
    if not DISABLE_CITY_API:
        api_result = await page.evaluate(
            """async ([cityQuery, cityTerms, cityHints, timeoutMs]) => {
            const mod = window.CheckoutModule && CheckoutModule.getInstance && CheckoutModule.getInstance();
            const recipient = mod && mod.getComponentByName && mod.getComponentByName('Recipient');
            if (!recipient || !recipient.performAction || !recipient.setCity) {
                return {ok: false, reason: 'recipient_component_unavailable'};
            }

            const normalize = (value) => String(value || '').toLowerCase().replace(/[^0-9a-zа-яіїєґ]+/g, '');
            const hintsNorm = (cityHints || []).map(normalize).filter(Boolean);
            const allCities = [];
            const responses = [];
            const seen = new Set();

            for (const term of cityTerms || [cityQuery]) {
                const response = await new Promise((resolve) => {
                    let done = false;
                    const finish = (value) => {
                        if (done) return;
                        done = true;
                        resolve(value);
                    };
                    try {
                        const req = recipient.performAction('findCities', {term}, (status, data) => finish({term, status, data}));
                        if (req && req.fail) {
                            req.fail((xhr, textStatus, errorThrown) => finish({
                                term,
                                status: 'AJAX_FAIL',
                                textStatus,
                                error: String(errorThrown || ''),
                                responseText: xhr && xhr.responseText ? String(xhr.responseText).slice(0, 500) : ''
                            }));
                        }
                    } catch (e) {
                        finish({term, status: 'EXCEPTION', error: String(e)});
                    }
                    setTimeout(() => finish({term, status: 'TIMEOUT'}), timeoutMs);
                });
                responses.push(response);
                const cities = response && response.data && Array.isArray(response.data.cities) ? response.data.cities : [];
                for (const item of cities) {
                    if (!item || !item.id || item.disabled || seen.has(String(item.id))) continue;
                    seen.add(String(item.id));
                    allCities.push({...item, _term: term});
                }
            }

            const scored = allCities.map((city, idx) => {
                const text = normalize([city.label, city.NPCityDescription].filter(Boolean).join(' '));
                let score = 0;
                for (const hint of hintsNorm) {
                    if (hint && text.includes(hint)) score += 100;
                }
                return {city, score, idx};
            });
            scored.sort((a, b) => (b.score - a.score) || (a.idx - b.idx));
            const best = scored.length ? scored[0] : null;
            const ties = best ? scored.filter((row) => row.score === best.score) : [];
            const city = best ? best.city : null;
            if (!city) return {ok: false, reason: 'city_not_found', responses, cityTerms, cityHints};
            // Never rely on the provider's result order for an ambiguous name.
            // When SalesDrive gave geographic context, it must match an option;
            // otherwise a distinct settlement with the same name could be sent.
            if (allCities.length > 1 && best.score <= 0) {
                return {ok: false, reason: 'city_ambiguous_no_geo_match', responses, cityTerms, cityHints,
                    candidates: scored.slice(0, 10).map((row) => ({label: row.city.label || '', npCity: row.city.NPCityDescription || '', score: row.score}))};
            }
            if (ties.length > 1 && best.score > 0) {
                return {ok: false, reason: 'city_ambiguous_geo_match', responses, cityTerms, cityHints,
                    candidates: ties.slice(0, 10).map((row) => ({label: row.city.label || '', npCity: row.city.NPCityDescription || '', score: row.score}))};
            }
            recipient.setCity(city.label || city.NPCityDescription || city.value || cityQuery, String(city.id), city.NPCityDescription || '');
            return {
                ok: true,
                city: {
                    id: String(city.id),
                    label: city.label || '',
                    NPCityDescription: city.NPCityDescription || '',
                    term: city._term || ''
                },
                cityTerms,
                cityHints,
                selectedScore: scored.length ? scored[0].score : 0
            };
        }""",
            [city_query, city_terms, city_hints, TIMEOUT_MS],
        )
    if isinstance(api_result, dict) and api_result.get("ok"):
        await page.wait_for_timeout(1800)
        city_value = (await city_input.input_value(timeout=TIMEOUT_MS)).strip()
        city_id = await page.locator('input[name="Recipient[delivery_city_id]"]').first.input_value(timeout=TIMEOUT_MS)
        if city_id:
            city = api_result.get("city") if isinstance(api_result.get("city"), dict) else {}
            return {
                "city_query": city_query,
                "city_value": city_value,
                "city_id": city_id,
                "city_label": city.get("label", ""),
                "np_city_description": city.get("NPCityDescription", ""),
                "city_search_terms": city_terms,
                "city_hints": city_hints,
                "selection": "api",
            }

    attempted_options: list[dict[str, Any]] = []
    option = None
    ui_city_query = ""
    for term in city_terms:
        ui_city_query = term
        await city_input.click(timeout=TIMEOUT_MS)
        await city_input.fill("", timeout=TIMEOUT_MS)
        await city_input.type(ui_city_query, delay=35, timeout=TIMEOUT_MS)
        await page.wait_for_timeout(1200)

        options = await page.evaluate(
            """() => Array.from(document.querySelectorAll('.ui-autocomplete.ui-menu .ui-menu-item')).map((el) => ({
                text: (el.innerText || el.textContent || '').trim(),
                disabled: el.classList.contains('ui-state-disabled')
            })).filter((item) => item.text)"""
        )
        attempted_options.append({"term": term, "options": options[:10] if isinstance(options, list) else []})
        query_norms = [_norm_match_text(x) for x in _unique_nonempty([term, city_query, *city_terms]) if _norm_match_text(x)]
        chosen_idx = -1
        if isinstance(options, list):
            for idx, item in enumerate(options):
                if not isinstance(item, dict) or item.get("disabled"):
                    continue
                text_norm = _norm_match_text(str(item.get("text") or ""))
                if any(query_norm and query_norm in text_norm for query_norm in query_norms):
                    chosen_idx = idx
                    break
            if chosen_idx < 0 and len(options) == 1 and isinstance(options[0], dict) and not options[0].get("disabled"):
                chosen_idx = 0
        if chosen_idx >= 0:
            option = page.locator(".ui-autocomplete.ui-menu .ui-menu-item").nth(chosen_idx)
            break
    if option is None:
        raise StageError(
            "fill_checkout",
            "City autocomplete option not found.",
            {
                "city_query": city_query,
                "city_search_terms": city_terms,
                "city_hints": city_hints,
                "api_result": api_result,
                "attempted_options": attempted_options,
            },
        )
    # The UI fallback must obey the same ambiguity rule as the API path.
    selected_option_text = str((attempted_options[-1].get("options") or [])[chosen_idx].get("text") or "") if chosen_idx >= 0 else ""
    if len((attempted_options[-1].get("options") or [])) > 1:
        hint_norms = [_norm_match_text(value) for value in city_hints if _norm_match_text(value)]
        if not any(hint and hint in _norm_match_text(selected_option_text) for hint in hint_norms):
            raise StageError(
                "fill_checkout",
                "City autocomplete is ambiguous and no geographic context matched.",
                {"city_query": city_query, "city_hints": city_hints, "attempted_options": attempted_options},
            )
    await option.click(timeout=TIMEOUT_MS)
    await page.wait_for_timeout(1800)

    city_value = (await city_input.input_value(timeout=TIMEOUT_MS)).strip()
    city_id = await page.locator('input[name="Recipient[delivery_city_id]"]').first.input_value(timeout=TIMEOUT_MS)
    if not city_id:
        raise StageError(
            "fill_checkout",
            "City id was not set after city selection.",
            {"city_query": city_query, "city_value": city_value, "api_result": api_result},
        )
    return {"city_query": city_query, "city_value": city_value, "city_id": city_id, "selection": "ui"}


async def _wait_for_checkout_idle(page, *, timeout_ms: int | None = None) -> dict[str, Any]:
    """Wait for CheckoutModule/cart AJAX to finish before the next action."""
    timeout = int(timeout_ms or TIMEOUT_MS)
    deadline = asyncio.get_running_loop().time() + (timeout / 1000.0)
    latest: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        latest = await page.evaluate(
            """() => {
                const module = window.CheckoutModule && CheckoutModule.getInstance ? CheckoutModule.getInstance() : null;
                const submit = document.querySelector('#checkout-container button.j-submit');
                const loaders = Array.from(document.querySelectorAll('#checkout-container .j-loader'));
                const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                return {
                    moduleSubmitting: !!(module && module.submitting),
                    postponeSubmit: !!(module && module.isPostponeSubmit),
                    submitDisabled: !!(submit && submit.disabled),
                    loaderVisible: loaders.some(visible),
                };
            }"""
        )
        if isinstance(latest, dict) and not latest.get("moduleSubmitting") and not latest.get("loaderVisible"):
            return latest
        await page.wait_for_timeout(250)
    raise StageError("fill_checkout", "Checkout AJAX did not settle in time.", {"state": latest})


async def _click_selectboxit_option(page, select_id: str, value: str, expected_text: str) -> dict[str, Any]:
    """Select an option through the site's visible SelectBoxIt widget."""
    if not select_id:
        raise StageError("fill_checkout", "Visible select widget id is missing.", {"value": value, "text": expected_text})
    widget = page.locator(f"#{select_id}SelectBoxIt").first
    if await widget.count() != 1:
        raise StageError(
            "fill_checkout",
            "Visible select widget was not found.",
            {"select_id": select_id, "value": value, "text": expected_text},
        )
    await widget.wait_for(state="visible", timeout=TIMEOUT_MS)
    await widget.click(timeout=TIMEOUT_MS)
    await page.wait_for_timeout(250)

    safe_value = str(value).replace('\\', '\\\\').replace('"', '\\"')
    option = page.locator(f'#{select_id}SelectBoxItOptions li[data-val="{safe_value}"]').first
    if await option.count() != 1:
        raise StageError(
            "fill_checkout",
            "Visible select option was not found.",
            {"select_id": select_id, "value": value, "text": expected_text},
        )
    if not await option.is_visible():
        # Large cities render only a filtered subset of warehouse options.
        # Use the widget's own search field instead of clicking a hidden LI.
        search = page.locator(f"#{select_id}SelectBoxItSearchField").first
        if await search.count() == 1 and await search.is_visible():
            branch_match = re.search(r"(?:№|#)\s*(\d+)", str(expected_text or ""))
            query = branch_match.group(1) if branch_match else str(expected_text or "")[:80]
            await search.fill(query, timeout=TIMEOUT_MS)
            await page.wait_for_timeout(500)
    await option.wait_for(state="visible", timeout=TIMEOUT_MS)
    await option.click(timeout=TIMEOUT_MS)
    await page.wait_for_timeout(250)
    await _wait_for_checkout_idle(page)

    state = await page.evaluate(
        """([selectId, expectedValue]) => {
            const sel = document.getElementById(selectId);
            const widgetText = document.getElementById(`${selectId}SelectBoxItText`);
            return {
                native: {
                    value: sel ? sel.value : '',
                    text: sel && sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '',
                },
                widget: {
                    value: widgetText ? widgetText.getAttribute('data-val') || '' : '',
                    text: widgetText ? (widgetText.textContent || '').trim() : '',
                },
                expectedValue: String(expectedValue || ''),
            };
        }""",
        [select_id, str(value)],
    )
    return state if isinstance(state, dict) else {}


async def _select_payment_cod(page) -> dict:
    payment = page.locator("#checkout-payment-type").first
    await payment.wait_for(state="attached", timeout=TIMEOUT_MS)
    option_text = await page.evaluate(
        """(value) => {
            const sel = document.querySelector('#checkout-payment-type');
            const option = sel && Array.from(sel.options).find((item) => String(item.value) === String(value));
            return option ? option.text : '';
        }""",
        PAYMENT_COD_VALUE,
    )
    state = await _click_selectboxit_option(page, "checkout-payment-type", PAYMENT_COD_VALUE, str(option_text or ""))
    value = str((state.get("native") or {}).get("value") or "")
    text = str((state.get("native") or {}).get("text") or "")
    if value != PAYMENT_COD_VALUE:
        raise StageError("fill_checkout", "Payment type was not selected.", {"value": value, "expected": PAYMENT_COD_VALUE, "text": text})
    return {"value": value, "text": text}


async def _select_delivery_method(page, recipient: Recipient) -> dict:
    target_words = ["поштомат", "почтомат"] if recipient.delivery_kind == "postomat" else ["отделение", "відділення"]
    meta = await page.evaluate(
        """(targetWords) => {
            const selects = Array.from(document.querySelectorAll('select'));
            const toRow = (sel) => {
                const options = Array.from(sel.options).map((o) => ({value: o.value, text: o.text, selected: o.selected}));
                const text = options.map((o) => o.text).join(' | ').toLowerCase();
                const option = options.find((o) => targetWords.some((word) => String(o.text || '').toLowerCase().includes(word)));
                return {
                    name: sel.name,
                    id: sel.id,
                    current: sel.value,
                    selectedText: sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '',
                    option,
                    optionCount: options.length,
                    text
                };
            };
            const byName = selects.find((sel) => sel.name === 'Delivery[delivery_type]' || sel.id === 'checkout-delivery-type');
            if (byName) return toRow(byName);
            const candidates = selects.map(toRow).filter((row) => row.option
                && row.optionCount > 1
                && row.optionCount <= 10
                && /курьер|кур'єр|отделение|відділення|поштомат|почтомат|новой почты|нової пошти/.test(row.text));
            return candidates[0] || {option: null, selectCount: selects.length};
        }""",
        target_words,
    )
    option = meta.get("option") if isinstance(meta, dict) else None
    if not option:
        raise StageError(
            "fill_checkout",
            "Delivery method option not found.",
            {"delivery_kind": recipient.delivery_kind, "branch_query": recipient.branch_query, "meta": meta},
        )

    widget_state = await _click_selectboxit_option(
        page,
        str(meta.get("id") or ""),
        str(option.get("value") or ""),
        str(option.get("text") or ""),
    )
    selected = widget_state.get("native") if isinstance(widget_state, dict) else {}
    selected_text = str((selected or {}).get("text") or "")
    selected_value = str((selected or {}).get("value") or "")
    widget_selected = widget_state.get("widget") if isinstance(widget_state, dict) else {}
    if selected_value != str(option.get("value") or "") or str((widget_selected or {}).get("value") or "") != str(option.get("value") or ""):
        raise StageError(
            "fill_checkout",
            "Delivery method selection did not stick.",
            {"expected": option, "selected": widget_state, "meta": meta},
        )
    if not any(word in selected_text.lower() for word in target_words):
        raise StageError(
            "fill_checkout",
            "Delivery method final validation failed.",
            {"delivery_kind": recipient.delivery_kind, "expected_words": target_words, "selected": widget_state, "meta": meta},
        )

    return {
        "kind": recipient.delivery_kind,
        "selected": True,
        "value": selected_value,
        "text": selected_text,
        "name": meta.get("name", ""),
    }


async def _select_warehouse(page, recipient: Recipient) -> dict:
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    meta: dict[str, Any] = {}
    np_lookup = _np_warehouse_lookup(recipient.city_query, recipient.branch_number)
    branch_address_tokens = _delivery_match_tokens(recipient.branch_address, recipient.branch_number, recipient.city_query)
    while asyncio.get_running_loop().time() < deadline:
        meta = await page.evaluate(
            """([branchNumber, branchQuery, branchAddressTokens, npLookup]) => {
                const sel = Array.from(document.querySelectorAll('select')).find((s) => s.name.includes('warehouse.id'));
                if (!sel) return {found: false};
                const options = Array.from(sel.options).map((o) => ({value: o.value, text: o.text, selected: o.selected}));
                const num = String(branchNumber || '').trim();
                const query = String(branchQuery || '').trim().toLowerCase();
                const normalize = (value) => String(value || '')
                    .toLowerCase()
                    .replace(/вулиця|улица/g, 'ул')
                    .replace(/вул\\.?/g, 'ул')
                    .replace(/проспект|пр-т/g, 'просп')
                    .replace(/провулок|переулок/g, 'пер')
                    .replace(/[^0-9a-zа-яіїєґ]+/g, '');
                let option = null;
                let matchReason = '';
                let addressMatches = [];
                if (num) {
                    const re = new RegExp(`(?:Отделение|Відділення|Пункт|Поштомат|Почтомат)[^№#]{0,100}(?:№|#)\\\\s*${num}(?!\\\\d)`, 'i');
                    option = options.find((o) => re.test(o.text));
                    if (option) matchReason = 'branch_number';
                }
                if (!option && query) {
                    option = options.find((o) => o.text.toLowerCase().includes(query));
                    if (option) matchReason = 'branch_query';
                }
                if (!option) {
                    const addressTokens = [
                        ...(Array.isArray(branchAddressTokens) ? branchAddressTokens : []),
                        ...((npLookup && npLookup.ok && Array.isArray(npLookup.tokens)) ? npLookup.tokens : [])
                    ];
                    const tokenNorms = Array.from(new Set(addressTokens.map(normalize).filter((token) => token.length >= 2)));
                    const scored = options.map((o) => {
                        const text = normalize(o.text);
                        const optionTokens = String(o.text || '').toLowerCase()
                            .replace(/вулиця|улица/g, 'ул')
                            .replace(/вул\\.?/g, 'ул')
                            .replace(/проспект|пр-т/g, 'просп')
                            .replace(/провулок|переулок/g, 'пер')
                            .split(/[^0-9a-zа-яіїєґ]+/)
                            .map(normalize)
                            .filter(Boolean);
                        const hits = tokenNorms.filter((token) => /^\\d+$/.test(token) ? optionTokens.includes(token) : text.includes(token));
                        return {option: o, hits, score: hits.length};
                    }).filter((row) => row.score > 0);
                    scored.sort((a, b) => b.score - a.score);
                    addressMatches = scored.slice(0, 5).map((row) => ({
                        text: row.option.text,
                        value: row.option.value,
                        hits: row.hits,
                        score: row.score
                    }));
                    const best = scored[0];
                    if (best && best.score >= 2) {
                        option = best.option;
                        matchReason = 'address';
                    }
                }
                return {
                    found: true,
                    name: sel.name,
                    id: sel.id,
                    current: sel.value,
                    selectedText: sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '',
                    option,
                    optionCount: options.length,
                    matchReason,
                    npLookup,
                    addressMatches
                };
            }""",
            [recipient.branch_number, recipient.branch_query, branch_address_tokens, np_lookup],
        )
        if meta.get("found") and meta.get("option"):
            break
        await page.wait_for_timeout(300)
    if not meta.get("found"):
        raise StageError("fill_checkout", "Warehouse select did not appear.", {"recipient": recipient.__dict__})
    option = meta.get("option")
    if not option:
        raise StageError(
            "fill_checkout",
            "Warehouse option not found.",
            {
                "branch_number": recipient.branch_number,
                "branch_query": recipient.branch_query,
                "branch_address": recipient.branch_address,
                "branch_address_tokens": branch_address_tokens,
                "np_lookup": np_lookup,
                "meta": meta,
            },
        )

    warehouse = page.locator(f'select[name="{meta["name"]}"]').first
    await warehouse.wait_for(state="attached", timeout=TIMEOUT_MS)
    widget_state = await _click_selectboxit_option(
        page,
        str(meta.get("id") or ""),
        str(option.get("value") or ""),
        str(option.get("text") or ""),
    )
    selected = widget_state.get("native") if isinstance(widget_state, dict) else {}
    selected_text = str((selected or {}).get("text") or "")
    selected_value = str((selected or {}).get("value") or "")
    widget_selected = widget_state.get("widget") if isinstance(widget_state, dict) else {}
    if selected_value != str(option.get("value") or "") or str((widget_selected or {}).get("value") or "") != str(option.get("value") or ""):
        raise StageError("fill_checkout", "Warehouse selection did not stick.", {"expected": option, "selected": widget_state})
    if str(option["text"]) != str(selected_text):
        raise StageError("fill_checkout", "Warehouse selection did not stick.", {"expected": option, "selected": widget_state})
    validation = _validate_selected_delivery_text(
        selected_text=selected_text,
        recipient=recipient,
        match_reason=str(meta.get("matchReason") or ""),
        np_lookup=np_lookup,
        extra_tokens=branch_address_tokens,
    )
    if not validation.get("valid"):
        raise StageError(
            "fill_checkout",
            "Selected delivery point failed final validation.",
            {
                "selected_text": selected_text,
                "branch_number": recipient.branch_number,
                "branch_query": recipient.branch_query,
                "delivery_kind": recipient.delivery_kind,
                "match_reason": meta.get("matchReason", ""),
                "validation": validation,
                "np_lookup": np_lookup,
                "meta": meta,
            },
        )
    return {
        "value": option["value"],
        "text": selected_text,
        "branch_number": recipient.branch_number,
        "match_reason": meta.get("matchReason", ""),
        "validation": validation,
        "np_lookup": np_lookup if meta.get("matchReason") == "np_address" else {},
    }


async def _apply_coupon(page) -> dict:
    if not PROMO_CODE:
        return {"code": "", "applied": False, "text": ""}

    async def read_coupon_state() -> dict:
        return await page.evaluate(
            """(code) => {
                const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                const body = document.body ? document.body.innerText || '' : '';
                const lines = body.split('\\n').map(norm).filter(Boolean);
                const couponLines = lines.filter((x) => /(?:знижка|скидка)\\s+по\\s+купону/i.test(x));
                const codeSeen = lines.some((x) => x.toUpperCase().includes(String(code || '').toUpperCase()));
                const removeVisible = Array.from(document.querySelectorAll('a.j-coupon-remove'))
                    .some((el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                const detailsText = norm(Array.from(document.querySelectorAll('.order-details, .checkout-total, .order-summary'))
                    .map((el) => el.innerText || el.textContent || '').join('\\n'));
                return {
                    applied: couponLines.length > 0,
                    text: couponLines.join(' | '),
                    codeSeen,
                    removeVisible,
                    detailsText,
                };
            }""",
            PROMO_CODE,
        )

    last_state: dict[str, Any] = {}
    for attempt in range(1, 4):
        coupon_input = page.locator(".j-coupon-input").first
        input_visible = await coupon_input.count() > 0 and await coupon_input.is_visible()
        if not input_visible:
            toggle = page.locator("a.j-coupon-add").first
            if await toggle.count() > 0:
                await toggle.click(timeout=TIMEOUT_MS, force=True)
                await page.wait_for_timeout(500)

        if await coupon_input.count() == 0 or not await coupon_input.is_visible():
            raise StageError("fill_checkout", "Coupon input is not visible.", {"code": PROMO_CODE, "attempt": attempt})

        await coupon_input.fill(PROMO_CODE, timeout=TIMEOUT_MS)
        input_value = (await coupon_input.input_value(timeout=TIMEOUT_MS)).strip()
        if input_value.upper() != PROMO_CODE.upper():
            raise StageError(
                "fill_checkout",
                "Coupon input value did not stick.",
                {"code": PROMO_CODE, "value": input_value, "attempt": attempt},
            )

        submit = page.locator("a.j-coupon-submit").first
        await submit.wait_for(state="visible", timeout=TIMEOUT_MS)
        await submit.click(timeout=TIMEOUT_MS)
        try:
            await page.wait_for_function(
                """(code) => {
                    const text = document.body ? document.body.innerText || '' : '';
                    return /(?:Знижка|Скидка)\\s+по\\s+купону/i.test(text)
                        || text.toUpperCase().includes(String(code || '').toUpperCase());
                }""",
                arg=PROMO_CODE,
                timeout=5000,
            )
        except PWTimeoutError:
            pass
        await page.wait_for_timeout(700)
        last_state = await read_coupon_state()
        if last_state.get("applied"):
            return {
                "code": PROMO_CODE,
                "applied": True,
                "text": str(last_state.get("text") or ""),
                "remove_visible": bool(last_state.get("removeVisible")),
            }

    raise StageError(
        "fill_checkout",
        "Coupon was not confirmed in checkout totals.",
        {"code": PROMO_CODE, "state": last_state},
    )


async def _read_success_page(page) -> dict:
    return await page.evaluate(
        """() => {
            const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
            const root = document.querySelector('section.checkout.__success') || document.querySelector('section.checkout');
            const complete = document.querySelector('section.checkout-complete') || root;
            const headingEl = root ? root.querySelector('h1.main-h, h1') : null;
            const numberEl = complete ? complete.querySelector('.h2') : null;
            const heading = norm(headingEl ? headingEl.innerText || headingEl.textContent || '' : '');
            const numberText = norm(numberEl ? numberEl.innerText || numberEl.textContent || '' : '');
            const completeText = norm(complete ? complete.innerText || complete.textContent || '' : '');
            const couponLines = complete
                ? (complete.innerText || '').split('\\n').map(norm).filter((x) => /(?:знижка|скидка)\\s+по\\s+купону/i.test(x))
                : [];
            const m = numberText.match(/(?:Замовлення|Заказ)\\s*№\\s*(\\d{5,})/i);
            return {
                url: location.href,
                title: document.title || '',
                successRoot: !!root,
                completeRoot: !!complete,
                heading,
                numberText,
                orderNumber: m ? m[1] : '',
                couponText: couponLines.join(' | '),
                completeText,
            };
        }"""
    )


async def _wait_for_manual_submit(page) -> dict[str, Any]:
    """Wait for a human to click checkout submit; this function never clicks it."""
    if MANUAL_SUBMIT_WAIT_SECONDS <= 0:
        return {"enabled": False, "click_detected": False}

    await page.evaluate(
        """() => {
            window.__sup2ManualSubmit = {clicked: false, at: 0};
            document.addEventListener('click', (event) => {
                const button = event.target && event.target.closest && event.target.closest('button.j-submit');
                if (button) window.__sup2ManualSubmit = {clicked: true, at: Date.now()};
            }, true);
        }"""
    )
    deadline = asyncio.get_running_loop().time() + MANUAL_SUBMIT_WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        state = await page.evaluate("""() => window.__sup2ManualSubmit || {clicked: false, at: 0}""")
        if isinstance(state, dict) and state.get("clicked"):
            # Let the site's AJAX/navigation handler settle before recording it.
            await page.wait_for_timeout(4000)
            success = await _read_success_page(page)
            return {
                "enabled": True,
                "click_detected": True,
                "wait_seconds": MANUAL_SUBMIT_WAIT_SECONDS,
                "success": success,
            }
        await page.wait_for_timeout(250)
    return {"enabled": True, "click_detected": False, "wait_seconds": MANUAL_SUBMIT_WAIT_SECONDS}


async def _fill_text_field(page, selector: str, value: str, *, clear: bool = True, tab_after: bool = True) -> str:
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=TIMEOUT_MS)
    await loc.click(timeout=TIMEOUT_MS)
    if clear:
        await loc.press("ControlOrMeta+A", timeout=TIMEOUT_MS)
        await loc.press("Backspace", timeout=TIMEOUT_MS)
    await loc.type(value, delay=25, timeout=TIMEOUT_MS)
    if tab_after:
        await loc.press("Tab", timeout=TIMEOUT_MS)
    await page.wait_for_timeout(200)
    return (await loc.input_value(timeout=TIMEOUT_MS)).strip()


async def _fill_recipient_fields(page, recipient: Recipient) -> dict:
    name_value = await _fill_text_field(page, "#checkout-name", recipient.name)
    if DEBUG_ARTIFACTS:
        await _capture_debug_artifacts(
            page,
            "fill_checkout",
            "after_name",
            extra={"field": "name", "value_present": bool(name_value)},
        )
    phone_value = await _fill_text_field(
        page,
        "#checkout-phone",
        recipient.phone_input,
        tab_after=not SKIP_FINAL_FIELD_TAB or bool(recipient.email),
    )
    if DEBUG_ARTIFACTS:
        await _capture_debug_artifacts(
            page,
            "fill_checkout",
            "after_phone",
            extra={"field": "phone", "value_present": bool(phone_value)},
        )
    email_value = ""
    if recipient.email:
        email_value = await _fill_text_field(page, "#checkout-email", recipient.email, tab_after=not SKIP_FINAL_FIELD_TAB)

    expected_phone_tail = recipient.phone_input[-7:]
    if not name_value:
        raise StageError("fill_checkout", "Recipient name is empty after fill.", {"expected": recipient.name})
    if expected_phone_tail not in "".join(ch for ch in phone_value if ch.isdigit()):
        raise StageError(
            "fill_checkout",
            "Recipient phone did not match after fill.",
            {"source": recipient.phone_source, "input": recipient.phone_input, "value": phone_value},
        )
    return {"name": name_value, "phone": phone_value, "email": email_value}


async def _read_totals(page) -> dict:
    return await page.evaluate(
        """() => {
            const text = document.body.innerText || '';
            const lines = text.split('\\n').map((x) => x.trim()).filter(Boolean);
            return {
                totals: lines.filter((x) => /^(Итого|Разом|Скидка|Знижка|Доставка)|грн$|–\\s*\\d/.test(x)).slice(-30),
                summary: lines.filter((x) => /Итого|Разом|Скидка по купону|Знижка за купоном|грн/.test(x)).slice(-30)
            };
        }"""
    )


async def _fill_checkout(page, recipient: Recipient) -> dict:
    await _goto_retry(page, CHECKOUT_URL)
    await _safe_wait_networkidle(page)

    # Applying a coupon reloads the cart totals via AJAX. Do it before the
    # delivery controls so that the final warehouse choice is made last and
    # cannot be replaced by the first option during that reload.
    coupon = await _apply_coupon(page)
    await _wait_for_checkout_idle(page)
    city = await _select_city(page, recipient.city_query, recipient.city_geo_hints)
    payment = await _select_payment_cod(page)
    delivery_method = await _select_delivery_method(page, recipient)
    warehouse = await _select_warehouse(page, recipient)
    customer = await _fill_recipient_fields(page, recipient)
    await _wait_for_checkout_idle(page)

    submit = page.locator("button.j-submit").first
    await submit.wait_for(state="visible", timeout=TIMEOUT_MS)
    if not await submit.is_enabled():
        raise StageError("fill_checkout", "Submit button is disabled after checkout fill.", {})

    rows = await _read_checkout_rows(page)
    totals = await _read_totals(page)
    return {
        "customer": customer,
        "delivery": {"city": city, "method": delivery_method, "warehouse": warehouse},
        "payment": payment,
        "coupon": coupon,
        "rows": rows,
        "totals": totals,
    }


def _parse_supplier_order_number(url: str, body_text: str) -> str:
    patterns = [
        r"/(?:order|orders)/(\d{5,})(?:[/?#]|$)",
        r"(?:заказ|замовлення|order)\s*(?:№|#|No\.?|Nº)\s*(\d{5,})\b",
        r"(?:номер|number)\s+(?:заказу|заказа|замовлення|order)\s*(?:№|#|No\.?|Nº)?\s*(\d{5,})\b",
        r"(?:заказу|заказа|замовлення|order)\s*(?:№|#|No\.?|Nº)\s*(\d{5,})\b",
    ]
    haystacks = [url or "", body_text or ""]
    for haystack in haystacks:
        for pattern in patterns:
            m = re.search(pattern, haystack, flags=re.IGNORECASE)
            if m:
                return m.group(1)
    return ""


async def _submit_order(page) -> str:
    submit = page.locator("button.j-submit").first
    await submit.wait_for(state="visible", timeout=TIMEOUT_MS)
    await submit.click(timeout=TIMEOUT_MS)
    _write_submit_checkpoint(url=page.url or "", submitted=True)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
    except PWTimeoutError:
        pass
    try:
        await page.wait_for_function(
            """() => {
                const text = document.body ? document.body.innerText : '';
                const success = document.querySelector('section.checkout.__success');
                const complete = document.querySelector('section.checkout-complete');
                const numberText = complete && complete.querySelector('.h2')
                    ? complete.querySelector('.h2').innerText || ''
                    : '';
                return (
                    (location.href.includes('/checkout/complete/') && success && complete)
                    || /Ваш\\s+заказ\\s+принят/i.test(text)
                    || /Ваше\\s+замовлення\\s+прийнято/i.test(text)
                ) && /(?:Заказ|Замовлення)\\s*№\\s*\\d{5,}/i.test(numberText || text);
            }""",
            timeout=TIMEOUT_MS,
        )
    except PWTimeoutError:
        await page.wait_for_timeout(2500)

    success = await _read_success_page(page)
    body_text = str(success.get("completeText") or "")
    order_no = str(success.get("orderNumber") or "").strip()
    if not order_no:
        order_no = _parse_supplier_order_number(page.url or "", body_text)
    _write_submit_checkpoint(url=page.url or "", submitted=True, order_number=order_no, success=success)
    success_url = "/checkout/complete/" in str(success.get("url") or page.url or "")
    success_heading = bool(re.search(r"(Ваш\s+заказ\s+принят|Ваше\s+замовлення\s+отримано|Ваше\s+замовлення\s+прийнято)", str(success.get("heading") or ""), re.I))
    if not success_url or not success.get("successRoot") or not success.get("completeRoot") or not success_heading or not order_no:
        raise StageError(
            "post_submit_order_number",
            "Supplier order was submitted, but Dobavki success page could not be verified.",
            {
                "submitted": True,
                "url": page.url or "",
                "success": success,
                "body_excerpt": re.sub(r"\s+", " ", body_text or "").strip()[:1000],
            },
        )
    if PROMO_CODE and not success.get("couponText"):
        raise StageError(
            "post_submit_coupon",
            "Supplier order was submitted, but coupon line is missing on Dobavki success page.",
            {"submitted": True, "url": page.url or "", "success": success},
        )
    return order_no


async def _run() -> tuple[bool, dict]:
    items = _parse_items()
    order_payload = _parse_order_payload()
    recipient = _extract_recipient(order_payload)
    order_price_map = _build_order_price_map(order_payload)

    browser = None
    context = None
    page = None
    stage = "init"
    added: list[dict] = []
    quantity_result: list[dict] = []
    checkout_result: dict[str, Any] = {}
    supplier_order_number = ""
    submitted = False
    paused_for_error = False

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS, args=["--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(
                locale="ru-RU",
                timezone_id="Europe/Kiev",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            await context.add_cookies(
                [
                    {
                        "name": "challenge_passed",
                        "value": "267d96f1c40f797eb54a2e87d56b51d1c409b40dd91f567631828d9f455161cc",
                        "domain": "dobavki.ua",
                        "path": "/",
                        "sameSite": "Lax",
                    }
                ]
            )
            page = await context.new_page()

            if CLEAR_BASKET:
                stage = "clear_basket"
                clear_result = await _clear_basket(page)
            else:
                clear_result = {"removed": 0, "skipped": True}

            stage = "add_items"
            added = await _add_items(page, items, order_price_map)

            stage = "set_quantities"
            quantity_result = await _set_item_quantities(page, items, added)

            stage = "fill_checkout"
            checkout_result = await _fill_checkout(page, recipient)

            if MANUAL_SUBMIT_WAIT_SECONDS > 0:
                stage = "manual_submit_wait"
                checkout_result["debug_artifacts"] = await _capture_debug_artifacts(
                    page,
                    "fill_checkout",
                    "ready_for_manual_submit",
                    extra={
                        "manual_submit": True,
                        "submitted": False,
                        "message": "Checkout is filled. Waiting for a human to click submit; automation will not click it.",
                    },
                )
                manual_submit = await _wait_for_manual_submit(page)
                checkout_result["manual_submit"] = manual_submit
                checkout_result["manual_submit_artifacts"] = await _capture_debug_artifacts(
                    page,
                    stage,
                    "after_manual_submit" if manual_submit.get("click_detected") else "manual_submit_timeout",
                    extra=manual_submit,
                )
                success = manual_submit.get("success") if isinstance(manual_submit.get("success"), dict) else {}
                supplier_order_number = str(success.get("orderNumber") or "") if manual_submit.get("click_detected") else ""
                submitted = bool(supplier_order_number)
            elif DRY_RUN:
                checkout_result["debug_artifacts"] = await _capture_debug_artifacts(
                    page,
                    stage,
                    "ready_for_submit",
                    extra={
                        "dry_run": True,
                        "submitted": False,
                        "message": "Checkout is filled and the submit button was deliberately not clicked.",
                    },
                )
                supplier_order_number = "DRY_RUN"
            else:
                stage = "submit_order"
                supplier_order_number = await _submit_order(page)
                submitted = True
                checkout_result["debug_artifacts"] = await _capture_debug_artifacts(
                    page,
                    "post_submit_order_number",
                    "supplier_order_confirmed",
                    extra={
                        "dry_run": False,
                        "submitted": True,
                        "supplier_order_number": supplier_order_number,
                        "message": "Supplier confirmation page after submit.",
                    },
                )

            return True, {
                "ok": True,
                "submitted": submitted,
                "dry_run": DRY_RUN,
                "added": added,
                "quantities": quantity_result,
                "clear_basket": clear_result,
                "supplier_order_number": supplier_order_number,
                "url": page.url or CHECKOUT_URL,
                **checkout_result,
            }
    except StageError as e:
        details = await _capture_debug_artifacts(page, e.stage or stage, "stage_error", extra=e.details or {})
        await _debug_pause_if_needed()
        paused_for_error = True
        return False, {
            "ok": False,
            "error": str(e),
            "stage": e.stage or stage,
            "url": page.url if page is not None else CHECKOUT_URL,
            "submitted": bool(details.get("submitted")) or submitted,
            "details": details,
        }
    except Exception as e:
        details = await _capture_debug_artifacts(
            page,
            stage,
            "unexpected_error",
            extra={"exception_type": type(e).__name__},
        )
        await _debug_pause_if_needed()
        paused_for_error = True
        return False, {
            "ok": False,
            "error": str(e),
            "stage": stage,
            "url": page.url if page is not None else CHECKOUT_URL,
            "submitted": submitted,
            "details": details,
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
        payload = {"ok": False, "error": str(e), "stage": "init", "url": CHECKOUT_URL, "details": {}}
        ok = False

    print(SUPPLIER_RESULT_JSON_PREFIX + json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
