import asyncio
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _to_int(value: str, default: int) -> int:
    try:
        iv = int((value or "").strip())
        return iv if iv > 0 else default
    except Exception:
        return default


def _to_bool(value: str, default: bool) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


SUP6_BASE_URL = (os.getenv("SUP6_BASE_URL") or "https://proteinplus.pro").strip() or "https://proteinplus.pro"
SUP6_USERNAME = (os.getenv("SUP6_USERNAME") or "").strip()
SUP6_PASSWORD = (os.getenv("SUP6_PASSWORD") or "").strip()
SUP6_STORAGE_STATE_FILE = (os.getenv("SUP6_STORAGE_STATE_FILE") or ".state_supplier6.json").strip()
SUP6_HEADLESS = _to_bool(os.getenv("SUP6_HEADLESS", "1"), True)
SUP6_TIMEOUT_MS = _to_int(os.getenv("SUP6_TIMEOUT_MS", "20000"), 20000)
SUP6_STAGE = (os.getenv("SUP6_STAGE") or "login").strip().lower() or "login"
SUP6_FORCE_LOGIN = _to_bool(os.getenv("SUP6_FORCE_LOGIN", "0"), False)
SUP6_KEEP_OPEN_SECONDS = _to_int(os.getenv("SUP6_KEEP_OPEN_SECONDS", "0"), 0)
SUP6_CLEAR_CART_PAUSE_SECONDS = _to_int(os.getenv("SUP6_CLEAR_CART_PAUSE_SECONDS", "20"), 20)
SUP6_STEP3_DEBUG_PAUSE_MS = _to_int(os.getenv("SUP6_STEP3_DEBUG_PAUSE_MS", "0"), 0)
SUP6_ITEMS = (os.getenv("SUP6_ITEMS") or "").strip()
SUP6_ITEMS_JSON = (os.getenv("SUP6_ITEMS_JSON") or "").strip()
SUP6_ORDER_JSON = (os.getenv("SUP6_ORDER_JSON") or os.getenv("BIOTUS_ORDER_JSON") or "").strip()
SUP6_BIOTUS_FULL_NAME = (os.getenv("BIOTUS_FULL_NAME") or "").strip()
SUP6_BIOTUS_PHONE_LOCAL = (os.getenv("BIOTUS_PHONE_LOCAL") or "").strip()
SUP6_CITY_NAME = (os.getenv("BIOTUS_CITY_NAME") or "").strip()
SUP6_CITY_AREA = (os.getenv("BIOTUS_CITY_AREA") or "").strip()
SUP6_CITY_REGION = (os.getenv("BIOTUS_CITY_REGION") or "").strip()
SUP6_CITY_TYPE = (os.getenv("BIOTUS_CITY_TYPE") or "").strip()
SUP6_BRANCH_QUERY = (os.getenv("BIOTUS_BRANCH_QUERY") or "").strip()
SUP6_BRANCH_KIND = (os.getenv("BIOTUS_BRANCH_KIND") or "").strip().lower()
SUP6_TERMINAL_QUERY = (os.getenv("BIOTUS_TERMINAL_QUERY") or "").strip()
SUPPLIER_RESULT_JSON_PREFIX = "SUPPLIER_RESULT_JSON="
SUP6_MAKE_ORDER_URL = f"{SUP6_BASE_URL.rstrip('/')}/make-order.html"
SUP6_CART_URL = f"{SUP6_BASE_URL.rstrip('/')}/cart.html"
SUP6_CHECKOUT_URL = f"{SUP6_BASE_URL.rstrip('/')}/shipping-and-payment.html"


@dataclass(frozen=True)
class Sup6Item:
    sku: str
    qty: int


def _to_decimal_number(value: object) -> Decimal | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.replace("\u00a0", " ").replace(" ", "")
    raw = raw.replace("грн", "").replace("₴", "")
    raw = raw.replace(",", ".")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if raw in {"", ".", "-", "-."}:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _money_to_stripped_intish(value: Decimal | None) -> str:
    if value is None:
        return ""
    q = value.quantize(Decimal("1")) if value == value.to_integral_value() else value.normalize()
    return str(q)


def _extract_money_candidates(text: str) -> list[Decimal]:
    raw = (text or "").replace("\u00a0", " ").replace(",", ".")
    parts = re.findall(r"\d+(?:[ .]\d{3})*(?:\.\d+)?", raw)
    out: list[Decimal] = []
    seen: set[str] = set()
    for p in parts:
        clean = p.replace(" ", "")
        d = _to_decimal_number(clean)
        if d is None:
            continue
        key = str(d.normalize())
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _state_path() -> Path:
    if not SUP6_STORAGE_STATE_FILE:
        raise RuntimeError("SUP6_STORAGE_STATE_FILE is empty.")
    path = Path(SUP6_STORAGE_STATE_FILE)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _is_state_file_valid(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("cookies"), list) and isinstance(payload.get("origins"), list)


async def _safe_is_visible(locator) -> bool:
    try:
        if await locator.count() <= 0:
            return False
        return await locator.first.is_visible()
    except Exception:
        return False


def _select_all_shortcut() -> str:
    return "Meta+A" if sys.platform == "darwin" else "Control+A"


def _norm_sku(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(v or "").strip().casefold())


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _parse_qty(value: object) -> int:
    try:
        qty = int(str(value).strip())
    except Exception as e:
        raise RuntimeError(f"Invalid qty value: {value!r}") from e
    if qty < 1:
        raise RuntimeError(f"Qty must be >= 1, got {qty}")
    return qty


def _parse_sup6_items(cli_items_raw: str = "") -> list[Sup6Item]:
    if SUP6_ITEMS_JSON:
        try:
            payload = json.loads(SUP6_ITEMS_JSON)
        except Exception as e:
            raise RuntimeError(f"SUP6_ITEMS_JSON is not valid JSON: {e}") from e
        if not isinstance(payload, list) or not payload:
            raise RuntimeError("SUP6_ITEMS_JSON must be a non-empty JSON list.")
        out: list[Sup6Item] = []
        for idx, row in enumerate(payload, start=1):
            if not isinstance(row, dict):
                raise RuntimeError(f"SUP6_ITEMS_JSON[{idx}] must be an object.")
            sku = str(
                row.get("sku")
                or row.get("articul")
                or row.get("article")
                or row.get("code")
                or ""
            ).strip()
            if not sku:
                raise RuntimeError(f"SUP6_ITEMS_JSON[{idx}] must contain sku/articul.")
            qty = _parse_qty(row.get("qty") or row.get("quantity") or row.get("count") or 1)
            out.append(Sup6Item(sku=sku, qty=qty))
        return out

    raw = (cli_items_raw or SUP6_ITEMS or "").strip()
    if not raw:
        raise RuntimeError("SUP6_ITEMS or SUP6_ITEMS_JSON is required for add_items stage.")
    parts = [p.strip() for p in re.split(r"[;,]", raw) if p.strip()]
    out2: list[Sup6Item] = []
    for idx, part in enumerate(parts, start=1):
        if ":" in part:
            sku_raw, qty_raw = part.split(":", 1)
            sku = sku_raw.strip()
            qty = _parse_qty(qty_raw)
        else:
            sku = part.strip()
            qty = 1
        if not sku:
            raise RuntimeError(f"SUP6_ITEMS part #{idx} has empty sku")
        out2.append(Sup6Item(sku=sku, qty=qty))
    return out2


def _parse_order_payload(order_json_raw: str = "") -> dict:
    raw = (order_json_raw or SUP6_ORDER_JSON or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"SUP6_ORDER_JSON is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise RuntimeError("SUP6_ORDER_JSON must be a JSON object.")
    return payload


def _build_full_name(order: dict) -> str:
    pc = order.get("primaryContact") or {}
    l = str(pc.get("lName") or "").strip()
    f = str(pc.get("fName") or "").strip()
    return " ".join([x for x in [l, f] if x]).strip()


def _format_phone_local(order: dict) -> str:
    pc = order.get("primaryContact") or {}
    phones = pc.get("phone") or []
    raw = ""
    if isinstance(phones, list) and phones:
        raw = str(phones[0] or "")
    else:
        raw = str(pc.get("phone") or "")

    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("380") and len(digits) >= 12:
        digits = digits[3:]
    if len(digits) >= 10:
        digits = digits[-10:]
        return f"{digits[0:2]} {digits[2:5]} {digits[5:7]} {digits[7:9]} {digits[9:10]}".replace("  ", " ").strip()
    return digits


def _split_last_first(full_name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", str(full_name or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], " ".join(parts[1:])


def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _norm_text(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("\u00a0", " ").replace("\u200b", " ")
    s = s.replace("ё", "е")
    s = s.replace("–", "-").replace("—", "-")
    s = (
        s.replace("ʼ", "'")
        .replace("’", "'")
        .replace("`", "'")
        .replace("\u02bc", "'")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_area_region(s: str) -> str:
    s = _norm_text(s)
    s = s.replace("область", "").replace("обл.", "").replace("обл", "")
    s = s.replace("район", "").replace("р-н", "").replace("рн", "")
    s = re.sub(r"[\.,;()\[\]{}]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_city_name_only(s: str) -> str:
    s = _norm_text(s)
    s = re.sub(r"^(м\.?\s+|с\.?\s+|смт\.?\s+|с-ще\.?\s+|селище\s+)", "", s).strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_district_name(s: str) -> str:
    s = _norm_text(s)
    s = s.replace("район", " ").replace("р-н", " ").replace("рн", " ")
    s = s.replace("область", " ").replace("обл.", " ").replace("обл", " ")
    s = re.sub(r"[()\\[\\]{}.,;:!\"“”'`’ʼ/\\\\\\-]+", " ", s)
    s = re.sub(r"\\s+", " ", s).strip()
    return s


def _split_city_option_text(txt: str) -> tuple[str, str]:
    raw = _normalize_spaces(txt or "")
    if not raw:
        return "", ""
    m = re.match(r"^(.*?)\\s*\\((.*?)\\)\\s*$", raw)
    if m:
        return _normalize_spaces(m.group(1)), _normalize_spaces(m.group(2))
    return raw, ""


def _district_soft_match(order_district_norm: str, option_district_norm: str) -> bool:
    if not order_district_norm or not option_district_norm:
        return False
    return (order_district_norm in option_district_norm) or (option_district_norm in order_district_norm)


def _normalize_no_markers(text: str) -> str:
    t = text or ""
    t = re.sub(r"(?<!\w)N\s*[º°]\s*", "№", t, flags=re.IGNORECASE)
    t = re.sub(r"(?<!\w)N\s*[oо]\s*", "№", t, flags=re.IGNORECASE)
    t = re.sub(r"№\s*", "№", t)
    return t


def _extract_number(ss: str) -> str | None:
    s_norm = _normalize_no_markers(ss)
    matches = list(re.finditer(r"№\s*(\d{1,6})", s_norm))
    if matches:
        for m in matches:
            num = m.group(1)
            if 4 <= len(num) <= 6:
                return num
        return matches[0].group(1)
    m2 = re.search(r"\b(\d{4,6})\b", s_norm)
    if m2:
        return m2.group(1)
    m3 = re.search(r"\b(\d{1,3})\b", s_norm)
    if m3:
        return m3.group(1)
    return None


def _extract_terminal_number(s: str) -> str | None:
    s = _normalize_no_markers(s or "")
    m = re.search(r"№\s*(\d{4,6})(?!\d)", s)
    if m:
        return m.group(1)
    groups = [m.group(0) for m in re.finditer(r"(?<!\d)\d{4,6}(?!\d)", s)]
    if not groups:
        return None
    max_len = max(len(g) for g in groups)
    for g in groups:
        if len(g) == max_len:
            return g
    return None


def _get_first_delivery_block(order: dict) -> dict:
    odd = order.get("ord_delivery_data") or []
    if isinstance(odd, list) and odd:
        first = odd[0]
        return first if isinstance(first, dict) else {}
    if isinstance(odd, dict):
        return odd
    return {}


def _extract_city_values(order: dict) -> dict:
    d = _get_first_delivery_block(order)
    return {
        "city_name": str(d.get("cityName") or "").strip(),
        "area_name": str(d.get("areaName") or "").strip(),
        "region_name": str(d.get("regionName") or "").strip(),
        "city_type": str(d.get("cityType") or "").strip(),
    }


def _extract_delivery_info(order: dict) -> tuple[str, str]:
    d = _get_first_delivery_block(order)
    address = str(d.get("address") or "").strip()
    bn = d.get("branchNumber")
    bn_str = ""
    if bn is not None:
        try:
            bn_str = str(int(bn))
        except Exception:
            bn_str = str(bn).strip()
    return address, bn_str


def _extract_shipping_address(order: dict) -> str:
    return str(order.get("shipping_address") or "").strip()


def _build_branch_query_from_shipping(shipping_address: str, fallback_address: str) -> str:
    src = (shipping_address or "").strip() or (fallback_address or "").strip()
    if not src:
        return ""
    s = _normalize_spaces(src)
    s = re.sub(r"\(\s*до\s*\d+\s*кг\s*\)", "", s, flags=re.IGNORECASE)
    s = _normalize_spaces(s)
    s_lower = s.casefold()

    def after_colon_tail(ss: str) -> str:
        if ":" in ss:
            tail = ss.split(":", 1)[1]
            return _normalize_spaces(tail)
        return ""

    if "поштомат" in s_lower:
        num = _extract_number(s)
        if num:
            return _normalize_spaces(num)
        tail = after_colon_tail(s)
        return tail or _normalize_spaces(s)

    if "пункт приймання-видачі" in s_lower and ":" in s:
        left, right = s.split(":", 1)
        left = _normalize_spaces(left)
        right = _normalize_spaces(right)
        right = re.sub(r"\s*\(до [^)]+\)\s*", " ", right, flags=re.IGNORECASE).strip()
        m = re.search(r"№\s*(\d+)", left)
        if m:
            return _normalize_spaces(f"Пункт приймання-видачі №{m.group(1)}: {right}")
        return _normalize_spaces(f"Пункт приймання-видачі: {right}")

    if "пункт" in s_lower:
        has_pryimannya = "пункт приймання-видачі" in s_lower
        num = _extract_number(s)
        if has_pryimannya and num:
            return _normalize_spaces(f"Пункт приймання-видачі №{num}")
        if (not has_pryimannya) and num:
            return _normalize_spaces(f"Пункт №{num}")
        tail = after_colon_tail(s)
        if tail:
            return tail
        s2 = re.sub(r"\(\s*до\s*\d+\s*кг\s*\)", "", s, flags=re.IGNORECASE)
        if has_pryimannya:
            s2 = re.sub(r"^\s*Пункт\s+приймання\-видачі\s*", "", s2, flags=re.IGNORECASE)
        else:
            s2 = re.sub(r"^\s*Пункт\s*", "", s2, flags=re.IGNORECASE)
        return _normalize_spaces(s2) or _normalize_spaces(s)

    if "відділен" in s_lower:
        num = _extract_number(s)
        if num:
            return _normalize_spaces(f"Відділення №{num}")
        tail = after_colon_tail(s)
        return tail or _normalize_spaces("Відділення")

    num = _extract_number(s)
    if num:
        return _normalize_spaces(num)
    tail = after_colon_tail(s)
    return tail or _normalize_spaces(s)


def _detect_branch_kind(shipping_address: str, fallback_address: str) -> str:
    src = (shipping_address or "").strip() or (fallback_address or "").strip()
    s = (src or "").casefold()
    if "пункт" in s:
        return "punkt"
    return "viddilennya"


def _extract_delivery_values(order_payload: dict | None = None) -> dict:
    payload = order_payload or {}
    city_name = ""
    area_name = ""
    region_name = ""
    city_type = ""
    address = ""
    branch_number = ""
    shipping_address = ""
    warehouse_query = ""
    warehouse_mode = "branch"
    branch_kind = ""

    if payload:
        city_data = _extract_city_values(payload)
        city_name = city_data["city_name"]
        area_name = city_data["area_name"]
        region_name = city_data["region_name"]
        city_type = city_data["city_type"]
        address, branch_number = _extract_delivery_info(payload)
        shipping_address = _extract_shipping_address(payload)

        a_norm = (address or "").casefold()
        if "поштомат" in a_norm:
            warehouse_mode = "terminal"
            warehouse_query = (branch_number or "").strip() or address
        else:
            warehouse_mode = "branch"
            warehouse_query = _build_branch_query_from_shipping(shipping_address, address)
            branch_kind = _detect_branch_kind(shipping_address, address)

    if not city_name:
        city_name = SUP6_CITY_NAME
    if not area_name:
        area_name = SUP6_CITY_AREA
    if not region_name:
        region_name = SUP6_CITY_REGION
    if not city_type:
        city_type = SUP6_CITY_TYPE
    if not warehouse_query:
        if SUP6_TERMINAL_QUERY:
            warehouse_mode = "terminal"
            warehouse_query = SUP6_TERMINAL_QUERY
        elif SUP6_BRANCH_QUERY:
            warehouse_mode = "branch"
            warehouse_query = SUP6_BRANCH_QUERY
            if SUP6_BRANCH_KIND in {"punkt", "viddilennya"}:
                branch_kind = SUP6_BRANCH_KIND
    if not branch_kind:
        branch_kind = "punkt" if SUP6_BRANCH_KIND == "punkt" else "viddilennya"

    region_for_select = area_name or region_name
    return {
        "region": _norm_area_region(region_for_select),
        "city": city_name.strip(),
        "district": region_name.strip(),
        "city_type": city_type.strip(),
        "warehouse_query": warehouse_query.strip(),
        "warehouse_mode": warehouse_mode,
        "branch_kind": branch_kind,
        "address": address,
        "shipping_address": shipping_address,
    }


def _extract_recipient_values(order_payload: dict | None = None) -> dict:
    payload = order_payload or {}
    last_name = ""
    first_name = ""
    phone = ""

    if isinstance(payload, dict) and payload:
        pc = payload.get("primaryContact") or {}
        if isinstance(pc, dict):
            last_name = str(pc.get("lName") or "").strip()
            first_name = str(pc.get("fName") or "").strip()
        phone = _format_phone_local(payload)

    if (not last_name or not first_name) and SUP6_BIOTUS_FULL_NAME:
        fallback_last, fallback_first = _split_last_first(SUP6_BIOTUS_FULL_NAME)
        if not last_name:
            last_name = fallback_last
        if not first_name:
            first_name = fallback_first

    if not phone and SUP6_BIOTUS_PHONE_LOCAL:
        phone = SUP6_BIOTUS_PHONE_LOCAL.strip()

    return {
        "last_name": last_name.strip(),
        "first_name": first_name.strip(),
        "phone": phone.strip(),
        "full_name": _build_full_name(payload) if payload else SUP6_BIOTUS_FULL_NAME,
    }


def _extract_order_product_rows(order_payload: dict | None = None) -> list[dict]:
    payload = order_payload if isinstance(order_payload, dict) else {}
    products = payload.get("products") or []
    if not isinstance(products, list):
        return []

    out: list[dict] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        desc = str(p.get("description") or "").strip()
        sku_from_desc = desc.split(",", 1)[0].strip() if desc else ""
        sku = str(
            sku_from_desc
            or p.get("parameter")
            or p.get("sku")
            or p.get("articul")
            or p.get("code")
            or ""
        ).strip()
        if not sku:
            continue
        try:
            qty = int(str(p.get("amount") or "1").strip())
        except Exception:
            qty = 1
        if qty < 1:
            qty = 1
        price_dec = _to_decimal_number(p.get("price"))
        name = str(p.get("name") or p.get("text") or "").strip()
        out.append(
            {
                "sku": sku,
                "sku_norm": _norm_sku(sku),
                "qty": qty,
                "price": p.get("price"),
                "price_dec": price_dec,
                "description": desc,
                "name": name,
            }
        )
    return out


def _build_step3_item_price_plan(items: list[Sup6Item], order_payload: dict | None = None) -> list[dict]:
    order_rows = _extract_order_product_rows(order_payload)
    buckets: dict[str, list[dict]] = {}
    for row in order_rows:
        buckets.setdefault(row["sku_norm"], []).append(row)

    plan: list[dict] = []
    for item in items:
        sku_norm = _norm_sku(item.sku)
        row = None
        bucket = buckets.get(sku_norm) or []
        if bucket:
            row = bucket.pop(0)
        plan.append(
            {
                "sku": item.sku,
                "qty": item.qty,
                "client_price": row.get("price") if row else None,
                "client_price_dec": row.get("price_dec") if row else None,
                "order_name": row.get("name") if row else "",
                "order_description": row.get("description") if row else "",
            }
        )
    return plan


def _strip_added_message_to_site_name(raw_text: str) -> str:
    text = _normalize_spaces(raw_text or "")
    if not text:
        return ""
    text = re.sub(r"\s+додано\s+до\s+кошика\.?$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^\s*\d+\s*[xх×]\s*", "", text, flags=re.IGNORECASE).strip()
    return text


def _norm_product_title(text: str) -> str:
    t = _norm_text(text or "")
    t = re.sub(r"\s+додано\s+до\s+кошика\.?$", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"^\s*\d+\s*[xх×]\s*", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _title_tokens(text: str) -> set[str]:
    t = _norm_product_title(text)
    t = t.replace("/", " ").replace("|", " ").replace("(", " ").replace(")", " ")
    t = re.sub(r"[^0-9a-zа-яіїєґ+\- ]+", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    raw = [x for x in t.split(" ") if x]
    out: set[str] = set()
    for tok in raw:
        if tok in {"грн", "uah", "usd", "$", "шт", "x"}:
            continue
        if tok.isdigit() and len(tok) == 1:
            continue
        if len(tok) >= 3 or tok.isdigit():
            out.add(tok)
    return out


def _extract_qty_prefix(text: str) -> int | None:
    t = _norm_text(text or "")
    m = re.match(r"^\s*(\d+)\s*[xх×]\s+", t)
    if not m:
        return None
    try:
        q = int(m.group(1))
        return q if q > 0 else None
    except Exception:
        return None


async def _auth_header_state(page) -> dict:
    login_loc = page.locator(
        "a:has-text('УВІЙТИ'), button:has-text('УВІЙТИ'), [role='button']:has-text('УВІЙТИ'), "
        "a:has-text('Увійти'), button:has-text('Увійти'), [role='button']:has-text('Увійти')"
    )
    account_loc = page.locator(
        "a:has-text('Вийти'), button:has-text('Вийти'), [role='button']:has-text('Вийти'), "
        "a:has-text('Кабінет'), button:has-text('Кабінет'), "
        "a:has-text('Профіль'), button:has-text('Профіль'), "
        "a:has-text('Мій акаунт'), button:has-text('Мій акаунт')"
    )
    return {
        "login_visible": await _safe_is_visible(login_loc),
        "account_visible": await _safe_is_visible(account_loc),
    }


async def _is_logged_in(page) -> tuple[bool, dict]:
    deadline = asyncio.get_running_loop().time() + min(3.0, SUP6_TIMEOUT_MS / 1000.0)
    stable_no_login_hits = 0
    last_state = {"login_visible": None, "account_visible": None}

    while asyncio.get_running_loop().time() < deadline:
        state = await _auth_header_state(page)
        last_state = state
        if state["account_visible"]:
            return True, state
        if state["login_visible"] is False:
            stable_no_login_hits += 1
            if stable_no_login_hits >= 2:
                return True, state
        else:
            stable_no_login_hits = 0
        await page.wait_for_timeout(200)

    # Fallback: if explicit login button is not visible, treat as logged-in.
    return bool(last_state.get("account_visible")) or (last_state.get("login_visible") is False), last_state


async def _open_login_form(page) -> None:
    triggers = [
        page.get_by_role("link", name=re.compile(r"увійти", re.IGNORECASE)).first,
        page.get_by_role("button", name=re.compile(r"увійти", re.IGNORECASE)).first,
        page.locator("a:has-text('УВІЙТИ'), button:has-text('УВІЙТИ'), [role='button']:has-text('УВІЙТИ')").first,
        page.locator("a:has-text('Увійти'), button:has-text('Увійти'), [role='button']:has-text('Увійти')").first,
    ]

    trigger = None
    for loc in triggers:
        try:
            if await loc.count() > 0:
                trigger = loc
                break
        except Exception:
            continue
    if trigger is None:
        raise RuntimeError('Login trigger "УВІЙТИ" not found.')

    click_error = None
    for attempt in (1, 2, 3):
        try:
            await trigger.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
            if attempt >= 2:
                await trigger.scroll_into_view_if_needed(timeout=min(3000, SUP6_TIMEOUT_MS))
            force_click = attempt == 3
            if force_click:
                print('[SUP6] login trigger click: using force=True (fallback).')
            await trigger.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=force_click)
            return
        except Exception as e:
            click_error = e
            await page.wait_for_timeout(220)

    raise RuntimeError(f'Could not click "УВІЙТИ": {click_error}')


async def _ensure_remember_me_checked(page) -> None:
    form = page.locator("form#login-form").first
    checkbox_candidates = [
        form.locator("input#modlgn-remember").first,
        form.locator("input[type='checkbox'][name='remember']").first,
        form.locator("input[type='checkbox'][name*='remember']").first,
        page.get_by_label(re.compile(r"запам[ʼ'`]?ятати мене", re.IGNORECASE)).first,
        page.locator("label:has-text(\"Запам'ятати мене\") input[type='checkbox']").first,
        page.locator("label:has-text('Запам’ятати мене') input[type='checkbox']").first,
    ]

    checkbox = None
    for candidate in checkbox_candidates:
        try:
            if await candidate.count() > 0:
                checkbox = candidate
                break
        except Exception:
            continue

    if checkbox is None:
        raise RuntimeError("Remember me checkbox not found in login form.")

    await checkbox.wait_for(state="attached", timeout=min(5000, SUP6_TIMEOUT_MS))
    if not await checkbox.is_checked():
        label_candidates = [
            page.locator("label[for='modlgn-remember']").first,
            page.locator("label:has-text(\"Запам'ятати мене\")").first,
            page.locator("label:has-text('Запам’ятати мене')").first,
        ]
        for label in label_candidates:
            try:
                if await label.count() <= 0:
                    continue
                await label.wait_for(state="visible", timeout=min(2500, SUP6_TIMEOUT_MS))
                await label.click(timeout=min(2500, SUP6_TIMEOUT_MS))
                if await checkbox.is_checked():
                    break
            except Exception:
                continue

    if not await checkbox.is_checked():
        # Some templates keep checkbox hidden and bind no label click; force-check as fallback.
        await checkbox.check(timeout=min(3000, SUP6_TIMEOUT_MS), force=True)

    if not await checkbox.is_checked():
        raise RuntimeError("Failed to enable remember me checkbox.")
    print("[SUP6] remember me: enabled")


async def _submit_login(page) -> None:
    user_input = page.locator("#modlgn-username").first
    pass_input = page.locator("#modlgn-passwd").first
    form = page.locator("form#login-form, form:has(#modlgn-username):has(#modlgn-passwd)").first
    submit = form.locator(
        "input[type='submit'][value*='Увійти'], button[type='submit']:has-text('Увійти'), button[type='submit']:has-text('УВІЙТИ')"
    ).first
    if await submit.count() <= 0:
        submit = page.locator(
            "input[type='submit'][value*='Увійти'], button:has-text('Увійти'), button:has-text('УВІЙТИ')"
        ).first

    await user_input.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await pass_input.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await user_input.fill(SUP6_USERNAME, timeout=SUP6_TIMEOUT_MS)
    await pass_input.fill(SUP6_PASSWORD, timeout=SUP6_TIMEOUT_MS)
    await _ensure_remember_me_checked(page)
    await submit.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    submit_error = None
    for attempt in (1, 2, 3):
        try:
            if attempt == 3:
                print("[SUP6] login submit: using force=True fallback")
            await submit.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=(attempt == 3))
            return
        except Exception as e:
            submit_error = e
            await page.wait_for_timeout(220)

    try:
        await pass_input.press("Enter", timeout=min(2500, SUP6_TIMEOUT_MS))
        return
    except Exception:
        pass

    raise RuntimeError(f"Could not click submit 'Увійти': {submit_error}")


async def _wait_login_success(page) -> dict:
    user_input = page.locator("#modlgn-username").first
    pass_input = page.locator("#modlgn-passwd").first
    deadline = asyncio.get_running_loop().time() + (SUP6_TIMEOUT_MS / 1000.0)
    last_state = {"login_visible": None, "account_visible": None, "form_visible": None}

    while asyncio.get_running_loop().time() < deadline:
        header_state = await _auth_header_state(page)
        form_visible = False
        try:
            form_visible = (
                (await user_input.count() > 0 and await user_input.is_visible())
                or (await pass_input.count() > 0 and await pass_input.is_visible())
            )
        except Exception:
            form_visible = False

        last_state = {
            "login_visible": header_state["login_visible"],
            "account_visible": header_state["account_visible"],
            "form_visible": form_visible,
        }

        auth_ok = bool(header_state["account_visible"]) or (header_state["login_visible"] is False)
        if auth_ok and not form_visible:
            return last_state

        await page.wait_for_timeout(200)

    raise RuntimeError(f"Login success was not detected: {last_state}")


async def ensure_logged_in(page, context, state_path: Path, *, force_login: bool = False) -> dict:
    await page.goto(SUP6_BASE_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)

    already_logged, checks = await _is_logged_in(page)
    if already_logged and not force_login:
        await context.storage_state(path=str(state_path))
        return {"reused_session": True, "checks": checks}
    if already_logged and force_login:
        print("[SUP6] force login enabled: ignoring active session and submitting credentials")

    if not SUP6_USERNAME or not SUP6_PASSWORD:
        raise RuntimeError("SUP6_USERNAME/SUP6_PASSWORD are required when login is needed.")

    await _open_login_form(page)
    await page.locator("#modlgn-username").first.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await page.locator("#modlgn-passwd").first.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await _submit_login(page)
    success_checks = await _wait_login_success(page)
    await context.storage_state(path=str(state_path))
    return {"reused_session": False, "checks_before": checks, "checks_after": success_checks}


async def _run_login_stage() -> dict:
    state_path = _state_path()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            if _is_state_file_valid(state_path) and not SUP6_FORCE_LOGIN:
                print(f"[SUP6] storage_state found: {state_path}")
                context = await browser.new_context(storage_state=str(state_path))
            else:
                if state_path.exists():
                    if SUP6_FORCE_LOGIN:
                        print(f"[SUP6] force login enabled, ignoring storage_state: {state_path}")
                    else:
                        print(f"[SUP6] storage_state invalid, re-login required: {state_path}")
                context = await browser.new_context()

            page = await context.new_page()
            result = await ensure_logged_in(page, context, state_path, force_login=SUP6_FORCE_LOGIN)
            if result.get("reused_session"):
                print("[SUP6] already logged in via stored state")
            if SUP6_KEEP_OPEN_SECONDS > 0:
                print(f"[SUP6] keep browser open for {SUP6_KEEP_OPEN_SECONDS}s")
                await page.wait_for_timeout(SUP6_KEEP_OPEN_SECONDS * 1000)
            return {
                "ok": True,
                "stage": "login",
                "url": page.url or SUP6_BASE_URL,
                "storage_state": str(state_path),
                "details": result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _open_minicart_panel(page, *, retries: int = 3) -> bool:
    panel = page.locator("div#cart-panel2.panel2, div#cart-panel2, div.cartpanel, .show_cart_link").first
    trigger = page.locator("a#cartpanel").first
    icon = page.locator("a#cartpanel i.fa-shopping-cart, a#cartpanel i").first

    if await _safe_is_visible(panel):
        return True

    for attempt in range(1, retries + 1):
        try:
            if await trigger.count() > 0:
                await trigger.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=(attempt >= 2))
            elif await icon.count() > 0:
                await icon.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=(attempt >= 2))
            else:
                raise RuntimeError("cart trigger not found (a#cartpanel).")

            await panel.wait_for(state="visible", timeout=min(4500, SUP6_TIMEOUT_MS))
            print(f"[SUP6] clear_cart: mini-cart opened (attempt={attempt})")
            return True
        except Exception as e:
            print(f"[SUP6] clear_cart: mini-cart open attempt={attempt} failed: {e}")
            await page.wait_for_timeout(250)

    return bool(await _safe_is_visible(panel))


async def _is_minicart_empty(page) -> bool:
    empty_el = page.locator("p.empty-cart").first
    if await _safe_is_visible(empty_el):
        return True

    panel = page.locator("div#cart-panel2, div.cartpanel").first
    if await panel.count() <= 0:
        return False

    try:
        text = ((await panel.inner_text(timeout=min(2000, SUP6_TIMEOUT_MS))) or "").casefold()
    except Exception:
        return False
    return ("ваш кошик порожній" in text) or ("ваш кошик порожнiй" in text)


async def _go_to_full_cart_from_minicart(page) -> None:
    link = page.locator("a.show_cart.show-cart-link, a.show_cart_link, a[href*='/cart.html']").first
    await link.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    print("[SUP6] clear_cart: opening full cart /cart.html")
    try:
        await asyncio.gather(
            page.wait_for_url(re.compile(r"/cart\.html"), timeout=SUP6_TIMEOUT_MS),
            link.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True),
        )
    except Exception:
        await link.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
        await page.wait_for_url(re.compile(r"/cart\.html"), timeout=SUP6_TIMEOUT_MS)
    await page.wait_for_load_state("domcontentloaded", timeout=min(12000, SUP6_TIMEOUT_MS))


async def clear_cart(page) -> dict:
    max_iterations = 50
    print(f"[SUP6] clear_cart: open start page {SUP6_MAKE_ORDER_URL}")
    await page.goto(SUP6_MAKE_ORDER_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)

    if not await _open_minicart_panel(page, retries=3):
        return {"ok": False, "error": "mini-cart panel did not open"}

    if await _is_minicart_empty(page):
        print("[SUP6] clear_cart: mini-cart is already empty")
        return {"ok": True, "cart_empty": True, "removed": 0}

    try:
        await _go_to_full_cart_from_minicart(page)
    except Exception as e:
        return {"ok": False, "error": f"failed to open /cart.html: {e}"}

    removed = 0
    for iteration in range(1, max_iterations + 1):
        buttons = page.locator("button.vm2-remove_from_cart:visible")
        count = await buttons.count()
        print(f"[SUP6] clear_cart: iteration={iteration} remove_buttons={count}")
        if count <= 0:
            print(f"[SUP6] clear_cart: done, removed={removed}")
            break

        btn = buttons.first
        try:
            await asyncio.gather(
                page.wait_for_load_state("networkidle", timeout=min(15000, SUP6_TIMEOUT_MS)),
                btn.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True),
            )
        except Exception:
            try:
                await btn.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
            except Exception as e:
                return {"ok": False, "error": f"delete click failed on iteration {iteration}: {e}"}
            try:
                await page.wait_for_load_state("networkidle", timeout=min(15000, SUP6_TIMEOUT_MS))
            except Exception:
                pass

        changed = False
        deadline = asyncio.get_running_loop().time() + min(5.0, SUP6_TIMEOUT_MS / 1000.0)
        while asyncio.get_running_loop().time() < deadline:
            now_count = await page.locator("button.vm2-remove_from_cart:visible").count()
            if now_count < count:
                changed = True
                removed += 1
                break
            await page.wait_for_timeout(120)

        if not changed:
            return {"ok": False, "error": f"cart did not update after delete (iteration={iteration})"}
    else:
        return {"ok": False, "error": f"max iterations exceeded ({max_iterations})"}

    final_buttons = await page.locator("button.vm2-remove_from_cart:visible").count()
    if final_buttons > 0:
        return {"ok": False, "error": "remove buttons still present after clear loop"}

    # Optional post-check on mini-cart.
    try:
        await page.goto(SUP6_MAKE_ORDER_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
        if await _open_minicart_panel(page, retries=2):
            if await _is_minicart_empty(page):
                return {"ok": True, "cart_empty": True, "removed": removed}
    except Exception:
        pass
    return {"ok": True, "cart_empty": True, "removed": removed}


async def _run_clear_cart_stage(*, pause_seconds: int = 0) -> dict:
    state_path = _state_path()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            if _is_state_file_valid(state_path):
                print(f"[SUP6] storage_state found: {state_path}")
                context = await browser.new_context(storage_state=str(state_path))
            else:
                context = await browser.new_context()

            page = await context.new_page()
            result = await clear_cart(page)
            if pause_seconds > 0:
                print(f"[SUP6] clear_cart: keep browser open for {pause_seconds}s")
                await page.wait_for_timeout(pause_seconds * 1000)
            return {
                "stage": "clear_cart",
                "url": page.url or SUP6_CART_URL,
                "storage_state": str(state_path),
                **result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


def _step3_fail(reason: str, *, details: dict | None = None) -> dict:
    return {
        "ok": False,
        "step": "step3_add_items_to_cart",
        "reason": reason,
        "details": details or {},
    }


def _step3_finish_fail(reason: str, *, details: dict | None = None) -> dict:
    return {
        "ok": False,
        "step": "step3_finish_cart",
        "reason": reason,
        "details": details or {},
    }


def _step4_fail(reason: str, *, details: dict | None = None) -> dict:
    return {
        "ok": False,
        "step": "step4_fill_recipient_info",
        "reason": reason,
        "details": details or {},
    }


def _step5_fail(reason: str, *, details: dict | None = None) -> dict:
    return {
        "ok": False,
        "step": "step5_fill_delivery_np_pickup",
        "reason": reason,
        "details": details or {},
    }


async def _step3_wait_search_input(page) -> None:
    search_input = await _step3_get_article_input(page)
    await search_input.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)


async def _step3_debug_pause(page, label: str) -> None:
    if SUP6_STEP3_DEBUG_PAUSE_MS <= 0:
        return
    print(f"[SUP6] step3_add_items: debug pause {SUP6_STEP3_DEBUG_PAUSE_MS}ms ({label})")
    await page.wait_for_timeout(SUP6_STEP3_DEBUG_PAUSE_MS)


async def _step3_get_article_input(page):
    candidates = [
        page.locator("th:has-text('Артикул') input.input-filter").first,
        page.locator("td:has-text('Артикул') input.input-filter").first,
        page.locator("input.input-filter[name*='articul' i]").first,
        page.locator("input.input-filter[placeholder*='Артикул' i]").first,
        page.locator("input.input-filter").first,
    ]
    for loc in candidates:
        try:
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue
    # Return last fallback so caller gets a meaningful timeout exception.
    return page.locator("input.input-filter").first


async def _step3_find_row_for_sku(page, sku: str):
    sku_norm = _norm_sku(sku)
    row_candidates = page.locator("tr:has(.addtocart-button), .product_item:has(.addtocart-button), li:has(.addtocart-button)")
    count = await row_candidates.count()
    first_btn = page.locator(".addtocart-button:visible").first

    # First, try strict "Артикул <sku>" match to disambiguate similar codes
    # like 23667-01 vs 23667-01_U002120.
    sku_re = re.escape(str(sku or "").strip())
    strict_patterns = [
        rf"(^|\s)артикул\s*[:\-]?\s*{sku_re}(?![_a-zA-Z0-9])",
        rf"(^|\s){sku_re}(?![_a-zA-Z0-9])",
    ]
    for pat in strict_patterns:
        try:
            marker = page.get_by_text(re.compile(pat, re.IGNORECASE)).first
            if await marker.count() <= 0:
                continue
            # Find nearest row-like ancestor that has add button.
            row = marker.locator(
                "xpath=ancestor::*[self::tr or contains(@class,'product_item') or self::li][.//*[contains(@class,'addtocart-button')]][1]"
            ).first
            if await row.count() > 0 and await row.is_visible():
                return row, None
        except Exception:
            continue

    matched_row = None
    for i in range(min(count, 25)):
        row = row_candidates.nth(i)
        try:
            txt = re.sub(r"\s+", " ", (await row.inner_text(timeout=900)) or "").strip()
            txt_norm = _norm_sku(txt)
            if sku_norm and sku_norm in txt_norm:
                # Avoid prefix collision for codes like XXX-01 vs XXX-01_U...
                txt_raw = (txt or "").casefold()
                if (str(sku).casefold() in txt_raw) and (f"{str(sku).casefold()}_" in txt_raw):
                    continue
                matched_row = row
                break
        except Exception:
            continue

    if matched_row is not None:
        return matched_row, None

    if await first_btn.count() <= 0:
        return None, _step3_fail(f"SKU_NOT_FOUND:{sku}", details={"sku": sku, "stage": "results_wait"})

    visible_rows = []
    for i in range(min(count, 10)):
        row = row_candidates.nth(i)
        try:
            if await row.is_visible():
                visible_rows.append(row)
        except Exception:
            continue

    # Conservative fallback: allow single visible row only.
    if len(visible_rows) == 1:
        print("[SUP6] step3_add_items: sku text exact match missing, using single visible row fallback")
        return visible_rows[0], None
    return None, _step3_fail(f"SKU_NOT_FOUND:{sku}", details={"sku": sku, "stage": "results_match", "visible_rows": len(visible_rows)})


async def _step3_detect_qty_limit(page) -> bool:
    selectors = [
        ".fancybox-inner",
        ".fancybox-wrap",
        ".fancybox-overlay",
        ".fancybox-skin",
        "body",
    ]
    needle = ("достигнута максимальна кількість", "досягнута максимальна кількість", "максимальна кількість")
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() <= 0:
                continue
            txt = ((await loc.inner_text(timeout=min(1200, SUP6_TIMEOUT_MS))) or "").casefold()
            if any(n in txt for n in needle):
                return True
        except Exception:
            continue
    return False


async def _step3_close_fancybox(page) -> None:
    close_btns = [
        ".fancybox-close",
        "a.fancybox-close",
        "button.fancybox-button--close",
        "button[title='Close']",
        "button[aria-label='Close']",
    ]
    for sel in close_btns:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=min(3000, SUP6_TIMEOUT_MS), force=True)
                return
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _step3_set_qty_in_modal(page, qty: int) -> None:
    qty_input = page.locator(
        "input.quantity-input.js-recalculate:visible, "
        ".fancybox-wrap input.quantity-input:visible, "
        "input.quantity-input:visible"
    ).first
    await qty_input.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await qty_input.click(timeout=min(3000, SUP6_TIMEOUT_MS))
    await page.keyboard.press(_select_all_shortcut())
    await page.keyboard.press("Backspace")
    await qty_input.fill(str(qty), timeout=min(3000, SUP6_TIMEOUT_MS))


async def _step3_click_add_in_modal(page, sku: str) -> dict | None:
    confirm_btn = page.locator(
        ".fancybox-wrap button.addtocart-button:visible, "
        ".fancybox-inner button.addtocart-button:visible, "
        "button.addtocart-button:visible"
    ).first
    await confirm_btn.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    await confirm_btn.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)

    showcart = page.locator("a.showcart:visible, a.showcart:has-text('Показати кошик')").first
    limit_deadline = asyncio.get_running_loop().time() + min(6.0, SUP6_TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < limit_deadline:
        if await _safe_is_visible(showcart):
            return None
        if await _step3_detect_qty_limit(page):
            return _step3_fail(f"QTY_LIMIT:{sku}", details={"sku": sku, "qty_limit": True})
        await page.wait_for_timeout(120)
    return _step3_fail(f"ADD_CONFIRM_TIMEOUT:{sku}", details={"sku": sku, "stage": "showcart_wait"})


async def _step3_extract_added_site_name(page, fallback_name: str = "") -> str:
    try:
        wrap = page.locator(".fancybox-inner, .fancybox-wrap").first
        if await wrap.count() > 0:
            all_text = _normalize_spaces((await wrap.inner_text(timeout=min(1600, SUP6_TIMEOUT_MS))) or "")
            if all_text:
                m = re.search(r"(\d+\s*[xх×]\s+.+?)\s+додано\s+до\s+кошика\.?", all_text, flags=re.IGNORECASE)
                if m:
                    cleaned = _strip_added_message_to_site_name(m.group(1))
                    if cleaned:
                        return cleaned
    except Exception:
        pass

    candidates = [
        page.locator(".fancybox-inner h4").first,
        page.locator(".fancybox-inner .title").first,
        page.locator(".fancybox-inner .product-name").first,
        page.locator(".fancybox-inner").first,
    ]
    for cand in candidates:
        try:
            if await cand.count() <= 0:
                continue
            txt = _normalize_spaces((await cand.inner_text(timeout=min(1800, SUP6_TIMEOUT_MS))) or "")
            if not txt:
                continue
            for line in [x.strip() for x in txt.splitlines() if x.strip()]:
                if "додано до кошика" in line.casefold():
                    cleaned = _strip_added_message_to_site_name(line)
                    if cleaned:
                        return cleaned
            cleaned_full = _strip_added_message_to_site_name(txt)
            if cleaned_full and cleaned_full.casefold() != txt.casefold():
                return cleaned_full
        except Exception:
            continue
    return _strip_added_message_to_site_name(fallback_name or "")


async def _step3_add_single_item(page, item: Sup6Item, *, is_last: bool, planned_client_price: object = None) -> tuple[dict | None, dict | None]:
    sku = item.sku
    qty = item.qty
    max_attempts = 2

    for attempt in range(1, max_attempts + 1):
        print(f"[SUP6] step3_add_items: sku={sku} qty={qty} attempt={attempt}/{max_attempts}")
        try:
            await page.goto(SUP6_MAKE_ORDER_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
            await _step3_wait_search_input(page)

            search_input = await _step3_get_article_input(page)
            await search_input.click(timeout=min(3000, SUP6_TIMEOUT_MS))
            await page.keyboard.press(_select_all_shortcut())
            await page.keyboard.press("Backspace")
            # Re-acquire input after clear because filter row can rerender.
            search_input = await _step3_get_article_input(page)
            await search_input.fill(sku, timeout=min(5000, SUP6_TIMEOUT_MS))
            await _step3_debug_pause(page, f"after_fill_sku={sku}")

            results_ready = page.locator(".addtocart-button:visible").first
            await results_ready.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
            row, fail_payload = await _step3_find_row_for_sku(page, sku)
            if fail_payload is not None:
                return fail_payload, None

            fallback_site_name = ""
            if row is not None:
                row_name_candidates = [
                    row.locator("a.product_name, .product-name, .product_name a").first,
                    row.locator("td:has(a), h3, h4").first,
                    row,
                ]
                for name_loc in row_name_candidates:
                    try:
                        if await name_loc.count() <= 0:
                            continue
                        fallback_site_name = _normalize_spaces((await name_loc.inner_text(timeout=min(1200, SUP6_TIMEOUT_MS))) or "")
                        if fallback_site_name:
                            break
                    except Exception:
                        continue

            add_btn = row.locator(".addtocart-button:visible").first if row is not None else results_ready
            await add_btn.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
            await _step3_debug_pause(page, f"after_click_add_sku={sku}")

            qty_modal_input = page.locator(
                "input.quantity-input.js-recalculate:visible, "
                ".fancybox-wrap input.quantity-input:visible, "
                "input.quantity-input:visible"
            ).first
            showcart_fast = page.locator("a.showcart:visible, a.showcart:has-text('Показати кошик')").first
            try:
                await qty_modal_input.wait_for(state="visible", timeout=min(7000, SUP6_TIMEOUT_MS))
            except Exception:
                # Some flows add qty=1 directly and show confirm without qty modal.
                if not await _safe_is_visible(showcart_fast):
                    raise

            if await _safe_is_visible(qty_modal_input):
                await _step3_set_qty_in_modal(page, qty)
                fail_after_click = await _step3_click_add_in_modal(page, sku)
                if fail_after_click is not None:
                    return fail_after_click, None
                await _step3_debug_pause(page, f"after_modal_confirm_sku={sku}")

            showcart = page.locator("a.showcart:visible, a.showcart:has-text('Показати кошик')").first
            await showcart.wait_for(state="visible", timeout=min(8000, SUP6_TIMEOUT_MS))
            site_name = await _step3_extract_added_site_name(page, fallback_name=fallback_site_name)
            if is_last:
                await showcart.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                try:
                    await page.wait_for_url(re.compile(r"/cart\.html|make-order"), timeout=SUP6_TIMEOUT_MS)
                except Exception:
                    pass
                cart_rows = page.locator("button.vm2-remove_from_cart:visible, .cart-summary tr:has(button.vm2-remove_from_cart), .cart-view tr")
                if await cart_rows.count() <= 0:
                    return _step3_fail(f"CART_VERIFY_FAILED:{sku}", details={"sku": sku}), None
            else:
                await _step3_close_fancybox(page)
                await _step3_debug_pause(page, f"after_close_fancybox_sku={sku}")

            return (
                None,
                {
                    "sku": sku,
                    "qty": qty,
                    "site_name": site_name,
                    "client_price": planned_client_price,
                },
            )
        except Exception as e:
            print(f"[SUP6] step3_add_items: attempt failed sku={sku} attempt={attempt}: {e}")
            if attempt >= max_attempts:
                return _step3_fail(f"STEP3_ADD_FAILED:{sku}", details={"sku": sku, "error": str(e)}), None
            await page.wait_for_timeout(220)

    return _step3_fail(f"STEP3_ADD_FAILED:{sku}", details={"sku": sku, "error": "unknown"}), None


async def _step3_cart_is_empty(page) -> bool:
    empty_markers = [
        page.locator("p.empty-cart, .empty-cart, .cart-empty").first,
        page.get_by_text("Ваш кошик порожній", exact=False).first,
        page.get_by_text("Ваш кошик порожнiй", exact=False).first,
    ]
    for marker in empty_markers:
        if await _safe_is_visible(marker):
            return True

    try:
        body = ((await page.inner_text("body")) or "").casefold()
        if ("ваш кошик порожній" in body) or ("ваш кошик порожнiй" in body):
            return True
    except Exception:
        pass

    remove_btns = page.locator("button.vm2-remove_from_cart")
    if await remove_btns.count() > 0:
        return False

    checkout_btn = page.locator("#checkoutFormSubmit, input[name='confirm'][type='submit']").first
    if await checkout_btn.count() > 0:
        return False

    rows = page.locator(".cart-view tr, .cart-summary tr")
    return (await rows.count()) <= 1


async def _step3_agreement_screen_detected(page) -> bool:
    checkbox = page.locator("#agreeBan, input[name='agreeBan'][type='checkbox']").first
    if await checkbox.count() > 0:
        return True

    text_markers = [
        page.get_by_text("Я ознайомлений", exact=False).first,
        page.get_by_text("Ці обмеження не стосуються дропшипінг-замовлень.", exact=False).first,
    ]
    for marker in text_markers:
        if await _safe_is_visible(marker):
            return True
    return False


async def _step3_get_checkout_button(page):
    candidates = [
        page.locator("form#checkoutForm #checkoutFormSubmit:visible").first,
        page.locator("form#checkoutForm input[name='confirm'][type='submit']:visible").first,
        page.locator("#checkoutFormSubmit:visible").first,
        page.locator("input[name='confirm'][type='submit']:visible").first,
        page.locator("#confirmButtons button[title='Оформити']:visible").first,
        page.locator("#confirmButtons button:has-text('Оформити'):visible").first,
        page.get_by_role("button", name=re.compile(r"Оформити", re.IGNORECASE)).first,
        page.get_by_text("Оформити замовлення", exact=False).first,
    ]
    for candidate in candidates:
        try:
            if await candidate.count() > 0 and await candidate.first.is_visible():
                return candidate
        except Exception:
            continue
    return None


async def _step3_click_checkout(page) -> bool:
    btn = await _step3_get_checkout_button(page)
    if btn is not None:
        try:
            await btn.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
        except Exception:
            pass
        for attempt in (1, 2):
            try:
                await btn.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=(attempt == 2))
                print("[SUP6] click checkout")
                return True
            except Exception:
                await page.wait_for_timeout(240)

    # Fallback: submit checkout form directly if submit button is hidden/not clickable.
    try:
        submitted = await page.evaluate(
            """() => {
                const form = document.querySelector('form#checkoutForm') || document.querySelector('form[name="checkoutForm"]');
                if (!form) return false;
                const submit = document.querySelector('#checkoutFormSubmit') || form.querySelector('input[name="confirm"][type="submit"]');
                if (submit && typeof submit.click === 'function') {
                    submit.click();
                    return true;
                }
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                    return true;
                }
                if (typeof form.submit === 'function') {
                    form.submit();
                    return true;
                }
                return false;
            }"""
        )
        if submitted:
            print("[SUP6] click checkout")
            return True
    except Exception:
        pass
    return False


async def _step3_wait_checkout_outcome(page, *, start_url: str) -> str:
    deadline = asyncio.get_running_loop().time() + (SUP6_TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        if await _step3_agreement_screen_detected(page):
            return "agreement"
        current_url = page.url or ""
        if current_url and ("cart.html" not in current_url):
            return "navigated"
        if current_url and current_url != start_url and ("cart.html" not in current_url):
            return "navigated"
        await page.wait_for_timeout(250)
    return "timeout"


async def _step3_check_agreement_checkbox(page) -> tuple[bool, str]:
    checkbox = page.locator("#agreeBan, input[name='agreeBan'][type='checkbox']").first
    if await checkbox.count() <= 0:
        return False, "AGREEMENT_CHECKBOX_NOT_FOUND"

    try:
        if await checkbox.is_checked():
            return True, ""
    except Exception:
        pass

    label_candidates = [
        page.locator("label[for='agreeBan']").first,
        page.locator(".cityBanBlock label:has-text('Я ознайомлений')").first,
        page.get_by_text("Я ознайомлений", exact=False).first,
    ]
    for label in label_candidates:
        try:
            if await label.count() > 0 and await label.first.is_visible():
                await label.first.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(220)
                if await checkbox.is_checked():
                    return True, ""
        except Exception:
            continue

    try:
        if await checkbox.is_visible():
            await checkbox.check(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
            if await checkbox.is_checked():
                return True, ""
    except Exception:
        pass

    # Hidden checkbox fallback: set checked through JS and emit events.
    try:
        js_checked = await page.evaluate(
            """() => {
                const el = document.querySelector('#agreeBan') || document.querySelector('input[name="agreeBan"][type="checkbox"]');
                if (!el) return false;
                el.checked = true;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return !!el.checked;
            }"""
        )
        if js_checked:
            await page.wait_for_timeout(220)
            return True, ""
    except Exception:
        pass

    return False, "AGREEMENT_CHECKBOX_CHECK_FAILED"


async def proceed_from_cart_to_checkout(page) -> dict:
    try:
        if "cart.html" not in (page.url or ""):
            await page.goto(SUP6_CART_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
        else:
            await page.wait_for_load_state("domcontentloaded", timeout=min(12000, SUP6_TIMEOUT_MS))
        print("[SUP6] cart page opened")
    except Exception as e:
        return _step3_finish_fail("CART_OPEN_FAILED", details={"error": str(e), "url": page.url or ""})

    if await _step3_cart_is_empty(page):
        return _step3_finish_fail("CART_EMPTY", details={"url": page.url or ""})

    # Some carts open directly on agreement block, without visible checkout submit first.
    if await _step3_agreement_screen_detected(page):
        print("[SUP6] agreement screen detected")
        checked, check_error = await _step3_check_agreement_checkbox(page)
        if not checked and check_error == "AGREEMENT_CHECKBOX_NOT_FOUND":
            return _step3_finish_fail("AGREEMENT_CHECKBOX_NOT_FOUND", details={"url": page.url or ""})
        if not checked:
            return _step3_finish_fail("AGREEMENT_CHECKBOX_CHECK_FAILED", details={"url": page.url or ""})
        print("[SUP6] agreement checkbox checked")
        await page.wait_for_timeout(250)
        post_agreement_url = page.url or ""
        if "cart.html" not in post_agreement_url:
            print("[SUP6] proceed to checkout ok")
            return {"ok": True, "step": "step3_finish_cart", "details": {"agreement_checked": True}}
        if not await _step3_click_checkout(page):
            return _step3_finish_fail(
                "AGREEMENT_CHECKED_BUT_CANNOT_CONTINUE",
                details={"agreement_checked": True, "url": page.url or ""},
            )
        post_outcome = await _step3_wait_checkout_outcome(page, start_url=post_agreement_url)
        if post_outcome == "navigated":
            print("[SUP6] proceed to checkout ok")
            return {"ok": True, "step": "step3_finish_cart", "details": {"agreement_checked": True}}
        return _step3_finish_fail(
            "AGREEMENT_CHECKED_BUT_NO_TRANSITION",
            details={"agreement_checked": True, "url": page.url or "", "outcome": post_outcome},
        )

    if not await _step3_click_checkout(page):
        return _step3_finish_fail(
            "CHECKOUT_BUTTON_NOT_FOUND",
            details={
                "url": page.url or "",
                "has_checkout_form": bool(await page.locator("form#checkoutForm, form[name='checkoutForm']").count()),
                "has_checkout_submit": bool(await page.locator("#checkoutFormSubmit, input[name='confirm'][type='submit']").count()),
            },
        )

    start_url = page.url or ""
    outcome = await _step3_wait_checkout_outcome(page, start_url=start_url)
    if outcome == "navigated":
        print("[SUP6] proceed to checkout ok")
        return {"ok": True, "step": "step3_finish_cart", "details": {"agreement_checked": False}}
    if outcome == "timeout":
        return _step3_finish_fail("CHECKOUT_TIMEOUT_NO_NAVIGATION", details={"url": page.url or ""})

    print("[SUP6] agreement screen detected")
    checked, check_error = await _step3_check_agreement_checkbox(page)
    if not checked and check_error == "AGREEMENT_CHECKBOX_NOT_FOUND":
        return _step3_finish_fail("AGREEMENT_CHECKBOX_NOT_FOUND", details={"url": page.url or ""})
    if not checked:
        return _step3_finish_fail("AGREEMENT_CHECKBOX_CHECK_FAILED", details={"url": page.url or ""})
    print("[SUP6] agreement checkbox checked")

    await page.wait_for_timeout(250)
    after_check_url = page.url or ""
    if "cart.html" not in after_check_url:
        print("[SUP6] proceed to checkout ok")
        return {"ok": True, "step": "step3_finish_cart", "details": {"agreement_checked": True}}

    clicked_after_agreement = await _step3_click_checkout(page)
    if not clicked_after_agreement:
        return _step3_finish_fail(
            "AGREEMENT_CHECKED_BUT_CANNOT_CONTINUE",
            details={"agreement_checked": True, "url": page.url or ""},
        )

    outcome_after = await _step3_wait_checkout_outcome(page, start_url=after_check_url)
    if outcome_after == "navigated":
        print("[SUP6] proceed to checkout ok")
        return {"ok": True, "step": "step3_finish_cart", "details": {"agreement_checked": True}}

    return _step3_finish_fail(
        "AGREEMENT_CHECKED_BUT_NO_TRANSITION",
        details={"agreement_checked": True, "url": page.url or "", "outcome": outcome_after},
    )


async def step3_add_items_to_cart(page, items: list[Sup6Item], order_payload: dict | None = None) -> dict:
    if not items:
        return _step3_fail("NO_ITEMS", details={"items": 0})

    added = 0
    processed: list[dict] = []
    mapped_items: list[dict] = []
    price_plan = _build_step3_item_price_plan(items, order_payload)
    for idx, item in enumerate(items):
        is_last = idx == len(items) - 1
        planned = price_plan[idx] if idx < len(price_plan) else {}
        fail, map_row = await _step3_add_single_item(
            page,
            item,
            is_last=is_last,
            planned_client_price=planned.get("client_price"),
        )
        if fail is not None:
            fail_details = dict(fail.get("details") or {})
            fail_details["items_added"] = added
            fail_details["processed"] = processed
            fail_details["supplier6_item_map"] = mapped_items
            fail["details"] = fail_details
            return fail
        added += 1
        processed.append({"sku": item.sku, "qty": item.qty})
        if map_row is None:
            map_row = {"sku": item.sku, "qty": item.qty, "site_name": "", "client_price": planned.get("client_price")}
        if not map_row.get("site_name"):
            map_row["site_name"] = planned.get("order_name") or ""
        mapped_items.append(map_row)
        print(f"[SUP6] step3_add_items: added sku={item.sku} qty={item.qty}")

    # Final verification: cart can hide SKU and show only product titles, so verify by rows/name, not SKU text.
    try:
        if "cart" not in (page.url or ""):
            await page.goto(SUP6_CART_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
        row_selectors = [
            ".cart-view tr:has(input[name^='quantity'])",
            ".cart-view tr:has(button.vm2-remove_from_cart)",
            ".cart-summary tr:has(button.vm2-remove_from_cart)",
            "button.vm2-remove_from_cart",
        ]
        cart_count = 0
        for sel in row_selectors:
            loc = page.locator(sel)
            try:
                c = await loc.count()
            except Exception:
                c = 0
            if c > cart_count:
                cart_count = c
        if cart_count < len(items):
            return _step3_fail(
                "CART_ITEMS_COUNT_MISMATCH",
                details={"expected_items": len(items), "cart_count": cart_count, "items_added": added, "items": processed},
            )

        # Additional soft check by site names captured from add-to-cart modal (if available).
        try:
            cart_text_norm = _norm_product_title((await page.inner_text("body")) or "")
        except Exception:
            cart_text_norm = ""
        missing_names: list[str] = []
        for m in mapped_items:
            site_name = str(m.get("site_name") or "").strip()
            if not site_name:
                continue
            n = _norm_product_title(site_name)
            if n and n not in cart_text_norm:
                missing_names.append(site_name)
        if missing_names and cart_count == 0:
            return _step3_fail(
                "CART_VERIFY_NAME_MISMATCH",
                details={"missing_site_names": missing_names, "items_added": added, "items": processed, "cart_count": cart_count},
            )
    except Exception as e:
        return _step3_fail("CART_VERIFY_ERROR", details={"error": str(e), "items_added": added, "items": processed})

    finish_result = await proceed_from_cart_to_checkout(page)
    if not finish_result.get("ok"):
        finish_details = dict(finish_result.get("details") or {})
        finish_details["items_added"] = added
        finish_details["items"] = processed
        finish_details["supplier6_item_map"] = mapped_items
        finish_result["details"] = finish_details
        return finish_result

    return {
        "ok": True,
        "step": "step3_add_items_to_cart",
        "details": {
            "items_added": added,
            "items": processed,
            "supplier6_item_map": mapped_items,
            "finish_cart": finish_result,
        },
    }


async def _step4_ensure_checkout_open(page) -> None:
    if "shipping-and-payment.html" not in (page.url or ""):
        await page.goto(SUP6_CHECKOUT_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
    else:
        await page.wait_for_load_state("domcontentloaded", timeout=min(12000, SUP6_TIMEOUT_MS))


async def _step4_is_dropshipping_selected(page) -> bool:
    label = page.locator("label[for='typeOfOrder1']").first
    try:
        if await label.count() > 0:
            class_name = (await label.get_attribute("class") or "").casefold()
            if "selected" in class_name:
                return True
    except Exception:
        pass
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const el = document.querySelector('#typeOfOrder1') || document.querySelector('input[type="radio"][id="typeOfOrder1"]');
                    return !!(el && el.checked);
                }"""
            )
        )
    except Exception:
        return False


async def _step4_select_dropshipping(page) -> bool:
    if await _step4_is_dropshipping_selected(page):
        print("[SUP6] dropshipping option selected")
        return True

    candidates = [
        page.get_by_text("Замовлення по системі дропшипінгу", exact=False).first,
        page.locator("label[for='typeOfOrder1']").first,
        page.locator("#typeOfOrder1").first,
    ]
    for candidate in candidates:
        try:
            if await candidate.count() <= 0:
                continue
            if await candidate.first.is_visible():
                await candidate.first.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
                await candidate.first.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(240)
                if await _step4_is_dropshipping_selected(page):
                    print("[SUP6] dropshipping option selected")
                    return True
        except Exception:
            continue

    # JS fallback for hidden input radio
    try:
        selected = await page.evaluate(
            """() => {
                const el = document.querySelector('#typeOfOrder1') || document.querySelector('input[type="radio"][id="typeOfOrder1"]');
                if (!el) return false;
                el.checked = true;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return !!el.checked;
            }"""
        )
        if selected:
            await page.wait_for_timeout(240)
            if await _step4_is_dropshipping_selected(page):
                print("[SUP6] dropshipping option selected")
                return True
    except Exception:
        pass
    return False


async def _step4_pick_field(page, selectors: list[str]):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


async def _step4_fill_text_field(page, field, value: str, *, phone_mode: bool = False) -> bool:
    try:
        await field.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
        await field.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
    except Exception:
        return False

    try:
        current = (await field.input_value() or "").strip()
    except Exception:
        current = ""

    if phone_mode:
        expected_digits_raw = _digits_only(value)
        if expected_digits_raw.startswith("380") and len(expected_digits_raw) >= 12:
            expected_digits_raw = expected_digits_raw[3:]
        expected_digits_local10 = expected_digits_raw[-10:] if len(expected_digits_raw) >= 10 else expected_digits_raw
        expected_digits_for_mask = (
            expected_digits_local10[1:]
            if len(expected_digits_local10) == 10 and expected_digits_local10.startswith("0")
            else expected_digits_local10
        )
        current_digits = _digits_only(current)
        if (
            len(current_digits) >= 9
            and (
                (expected_digits_for_mask and (current_digits.endswith(expected_digits_for_mask) or expected_digits_for_mask in current_digits))
                or (expected_digits_local10 and (current_digits.endswith(expected_digits_local10) or expected_digits_local10 in current_digits))
            )
        ):
            return True
    else:
        if current == value:
            return True

    try:
        await field.click(timeout=min(4000, SUP6_TIMEOUT_MS))
        await page.keyboard.press(_select_all_shortcut())
        await page.keyboard.press("Backspace")
        if phone_mode:
            digits_local10 = expected_digits_local10 or _digits_only(value)
            digits_local9 = digits_for_mask if (digits_for_mask := expected_digits_for_mask) else (digits_local10[1:] if len(digits_local10) == 10 else digits_local10)
            attempts = []
            if digits_local10:
                attempts.append(digits_local10)
            if digits_local9 and digits_local9 not in attempts:
                attempts.append(digits_local9)
            if digits_local10 and len(digits_local10) == 10:
                full380 = f"380{digits_local10[1:]}" if digits_local10.startswith("0") else f"380{digits_local10}"
                if full380 not in attempts:
                    attempts.append(full380)

            typed_ok = False
            for phone_attempt in attempts:
                try:
                    await field.click(timeout=min(3000, SUP6_TIMEOUT_MS))
                    await page.keyboard.press(_select_all_shortcut())
                    await page.keyboard.press("Backspace")
                    await page.keyboard.press(_select_all_shortcut())
                    await page.keyboard.press("Delete")
                    await field.type(phone_attempt, delay=25, timeout=min(6000, SUP6_TIMEOUT_MS))
                    await page.wait_for_timeout(220)
                    current_after = (await field.input_value() or "").strip()
                    current_digits_after = _digits_only(current_after)
                    if len(current_digits_after) >= 9:
                        typed_ok = True
                        break
                except Exception:
                    continue
            if not typed_ok:
                return False
        else:
            await field.fill(value, timeout=min(4000, SUP6_TIMEOUT_MS))
    except Exception:
        try:
            await page.evaluate(
                """(el, val) => {
                    el.focus();
                    el.value = '';
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                await field.element_handle(),
                (_digits_only(value) if phone_mode else value),
            )
        except Exception:
            return False

    try:
        updated = (await field.input_value() or "").strip()
    except Exception:
        updated = ""
    if phone_mode:
        expected_digits = _digits_only(value)
        if expected_digits.startswith("380") and len(expected_digits) >= 12:
            expected_digits = expected_digits[3:]
        expected_local10 = expected_digits[-10:] if len(expected_digits) >= 10 else expected_digits
        expected_mask_digits = expected_local10[1:] if len(expected_local10) == 10 and expected_local10.startswith("0") else expected_local10
        updated_digits = _digits_only(updated)
        if len(updated_digits) < 9:
            return False
        return bool(
            (expected_mask_digits and (updated_digits.endswith(expected_mask_digits) or expected_mask_digits in updated_digits))
            or (expected_local10 and (updated_digits.endswith(expected_local10) or expected_local10 in updated_digits))
            or (expected_digits and (updated_digits.endswith(expected_digits) or expected_digits in updated_digits))
        )
    return updated == value


def _format_phone_mask_ua(digits_raw: str) -> str:
    d = _digits_only(digits_raw)
    if d.startswith("380") and len(d) >= 12:
        d = d[3:]
    if len(d) == 9:
        d = "0" + d
    if len(d) >= 10:
        d = d[-10:]
    if len(d) < 10:
        return d
    # +38 (0XX) XXX-XX-XX
    return f"+38 ({d[0:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}"


async def _step4_fill_phone_field(page, value: str) -> tuple[bool, str]:
    expected_digits = _digits_only(value)
    if expected_digits.startswith("380") and len(expected_digits) >= 12:
        expected_digits = expected_digits[3:]
    if len(expected_digits) == 9:
        expected_digits = "0" + expected_digits
    if len(expected_digits) >= 10:
        expected_digits = expected_digits[-10:]

    # For masked input often only last 9 digits are user-entered (without leading 0).
    expected_digits_mask = expected_digits[1:] if len(expected_digits) == 10 and expected_digits.startswith("0") else expected_digits
    attempts = []
    if expected_digits:
        attempts.append(expected_digits)
    if expected_digits_mask and expected_digits_mask not in attempts:
        attempts.append(expected_digits_mask)
    if expected_digits:
        full_380 = f"380{expected_digits[1:]}" if expected_digits.startswith("0") else f"380{expected_digits}"
        if full_380 not in attempts:
            attempts.append(full_380)
    masked_text = _format_phone_mask_ua(expected_digits)
    if masked_text and masked_text not in attempts:
        attempts.append(masked_text)

    last_value = ""
    for _ in range(3):
        phone_field = await _step4_pick_field(page, ["#Phone", "input[name='form[Phone]']"])
        if phone_field is None:
            continue
        try:
            await phone_field.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
            await phone_field.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
        except Exception:
            continue

        for attempt_value in attempts:
            try:
                await phone_field.click(timeout=min(3000, SUP6_TIMEOUT_MS))
                await page.keyboard.press(_select_all_shortcut())
                await page.keyboard.press("Backspace")
                await page.keyboard.press(_select_all_shortcut())
                await page.keyboard.press("Delete")
                if _digits_only(attempt_value) == attempt_value:
                    await phone_field.type(attempt_value, delay=24, timeout=min(6000, SUP6_TIMEOUT_MS))
                else:
                    await phone_field.fill(attempt_value, timeout=min(4500, SUP6_TIMEOUT_MS))
                await page.wait_for_timeout(240)
                current = (await phone_field.input_value() or "").strip()
                last_value = current
                curr_digits = _digits_only(current)
                if curr_digits and (
                    (expected_digits_mask and (curr_digits.endswith(expected_digits_mask) or expected_digits_mask in curr_digits))
                    or (expected_digits and (curr_digits.endswith(expected_digits) or expected_digits in curr_digits))
                ):
                    return True, current
            except Exception:
                continue

        # JS fallback for masked controls.
        try:
            js_val = masked_text or expected_digits or value
            await page.evaluate(
                """(val) => {
                    const el = document.querySelector('#Phone') || document.querySelector('input[name="form[Phone]"]');
                    if (!el) return;
                    el.focus();
                    el.value = '';
                    el.value = String(val || '');
                    el.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, key:'0'}));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, key:'0'}));
                }""",
                js_val,
            )
            await page.wait_for_timeout(240)
            current = (await phone_field.input_value() or "").strip()
            last_value = current
            curr_digits = _digits_only(current)
            if curr_digits and (
                (expected_digits_mask and (curr_digits.endswith(expected_digits_mask) or expected_digits_mask in curr_digits))
                or (expected_digits and (curr_digits.endswith(expected_digits) or expected_digits in curr_digits))
            ):
                return True, current
        except Exception:
            pass

    return False, last_value


async def step4_fill_recipient_info(page, order_payload: dict | None = None) -> dict:
    try:
        await _step4_ensure_checkout_open(page)
        print("[SUP6] checkout page opened")
    except Exception as e:
        return _step4_fail("CHECKOUT_OPEN_FAILED", details={"error": str(e), "url": page.url or ""})

    recipient = _extract_recipient_values(order_payload)
    if not recipient["last_name"] or not recipient["first_name"] or not recipient["phone"]:
        return _step4_fail(
            "RECIPIENT_DATA_MISSING",
            details={
                "last_name": bool(recipient["last_name"]),
                "first_name": bool(recipient["first_name"]),
                "phone": bool(recipient["phone"]),
            },
        )

    if not await _step4_select_dropshipping(page):
        return _step4_fail("DROPSHIPPING_OPTION_NOT_FOUND", details={"url": page.url or ""})

    last_name_field = await _step4_pick_field(page, ["#lastName", "input[name='form[lastName]']"])
    first_name_field = await _step4_pick_field(page, ["#firstName", "input[name='form[firstName]']"])
    phone_field = await _step4_pick_field(page, ["#Phone", "input[name='form[Phone]']"])

    if last_name_field is None or first_name_field is None or phone_field is None:
        return _step4_fail(
            "RECIPIENT_FIELDS_NOT_FOUND",
            details={
                "has_last_name_field": last_name_field is not None,
                "has_first_name_field": first_name_field is not None,
                "has_phone_field": phone_field is not None,
            },
        )

    try:
        await last_name_field.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
        await first_name_field.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
        await phone_field.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
    except Exception as e:
        return _step4_fail("RECIPIENT_FIELDS_NOT_VISIBLE", details={"error": str(e)})

    if not await _step4_fill_text_field(page, last_name_field, recipient["last_name"], phone_mode=False):
        return _step4_fail("LAST_NAME_FILL_FAILED", details={"value": recipient["last_name"]})
    print("[SUP6] recipient last name filled")

    if not await _step4_fill_text_field(page, first_name_field, recipient["first_name"], phone_mode=False):
        return _step4_fail("FIRST_NAME_FILL_FAILED", details={"value": recipient["first_name"]})
    print("[SUP6] recipient first name filled")

    phone_ok, phone_after = await _step4_fill_phone_field(page, recipient["phone"])
    if not phone_ok:
        return _step4_fail(
            "PHONE_FILL_FAILED",
            details={"value": recipient["phone"], "phone_after": phone_after, "masked_expected": _format_phone_mask_ua(recipient["phone"])},
        )
    print("[SUP6] recipient phone filled")

    try:
        v_last = (await last_name_field.input_value() or "").strip()
        v_first = (await first_name_field.input_value() or "").strip()
        v_phone = (await phone_field.input_value() or "").strip()
    except Exception as e:
        return _step4_fail("RECIPIENT_VERIFY_FAILED", details={"error": str(e)})

    if not v_last or not v_first or not v_phone:
        return _step4_fail(
            "RECIPIENT_VALUES_EMPTY",
            details={
                "last_name_empty": not bool(v_last),
                "first_name_empty": not bool(v_first),
                "phone_empty": not bool(v_phone),
            },
        )

    return {
        "ok": True,
        "step": "step4_fill_recipient_info",
        "details": {
            "last_name": recipient["last_name"],
            "first_name": recipient["first_name"],
            "phone": recipient["phone"],
            "url": page.url or "",
        },
    }


async def _sumo_container(page, select_id: str):
    select = page.locator(f"select#{select_id}").first
    if await select.count() <= 0:
        return None
    try:
        return select.locator("xpath=ancestor::div[contains(@class,'SumoSelect')][1]").first
    except Exception:
        return None


async def _sumo_selected_text(page, select_id: str) -> str:
    container = await _sumo_container(page, select_id)
    if container is None:
        return ""
    candidates = [
        container.locator(".CaptionCont .search").first,
        container.locator(".CaptionCont span").first,
        container.locator(".CaptionCont").first,
    ]
    for c in candidates:
        try:
            if await c.count() > 0:
                txt = ((await c.inner_text()) or "").strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


async def _sumo_open(page, select_id: str) -> bool:
    container = await _sumo_container(page, select_id)
    if container is None:
        return False
    opener = container.locator(".CaptionCont, p.search").first
    for _ in range(3):
        try:
            if await opener.count() > 0:
                await opener.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
                await opener.click(timeout=min(4000, SUP6_TIMEOUT_MS), force=True)
            else:
                await container.click(timeout=min(4000, SUP6_TIMEOUT_MS), force=True)
            await page.wait_for_timeout(220)
            cls = ((await container.get_attribute("class")) or "").lower()
            if "open" in cls:
                return True
        except Exception:
            await page.wait_for_timeout(220)
    return False


async def _sumo_wait_enabled(page, select_id: str, timeout_ms: int) -> bool:
    container = await _sumo_container(page, select_id)
    if container is None:
        return False
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        try:
            cls = ((await container.get_attribute("class")) or "").casefold()
            if "disabled" not in cls:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(220)
    return False


async def _sumo_options_snapshot(page, select_id: str, limit: int = 40) -> list[str]:
    select = page.locator(f"select#{select_id}").first
    out: list[str] = []
    try:
        if await select.count() <= 0:
            return out
        options = select.locator("option")
        count = await options.count()
        for i in range(min(count, max(1, limit))):
            txt = ((await options.nth(i).inner_text()) or "").strip()
            if txt:
                out.append(_norm_text(txt))
    except Exception:
        return []
    return out


async def _sumo_wait_options_reload(page, select_id: str, prev_snapshot: list[str], timeout_ms: int) -> bool:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    prev = prev_snapshot or []
    while asyncio.get_running_loop().time() < deadline:
        curr = await _sumo_options_snapshot(page, select_id)
        if curr and curr != prev:
            return True
        await page.wait_for_timeout(220)
    return False


def _is_default_placeholder_text(text: str) -> bool:
    t = _norm_text(text)
    return ("виберіть" in t) or ("спочатку виберіть" in t)


def _branch_number_from_query(q: str) -> str:
    q_raw = (q or "").strip()
    if not q_raw:
        return ""
    if re.fullmatch(r"\d+", q_raw):
        return q_raw
    m = re.search(r"(?:пункт|відділення)\s*№\s*(\d+)", q_raw, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def _build_branch_option_matcher(kind: str, query: str):
    q_raw = re.sub(r"\s*\(до [^)]+\)\s*", " ", query or "", flags=re.IGNORECASE).strip()
    qn = _norm_text(q_raw)
    num = _branch_number_from_query(q_raw)
    has_num = bool(num)
    strict_re = None
    if kind == "viddilennya" and has_num:
        strict_re = re.compile(rf"(?:мобільне\s+)?відділення\s*№\s*{re.escape(num)}(?!\d)", re.IGNORECASE)
    elif kind == "punkt" and has_num:
        strict_re = re.compile(rf"пункт\s*(?:приймання\-видачі\s*)?№\s*{re.escape(num)}(?!\d)", re.IGNORECASE)

    def norm_addr(s: str) -> str:
        s = _norm_text(s)
        s = s.replace("пункт приймання-видачі", "")
        s = s.replace("пункт приймання видачі", "")
        s = s.replace("пункт", "")
        s = s.replace("відділення", "")
        s = s.replace("№", " ")
        s = s.replace("вулиця", "вул")
        s = re.sub(r"\s+", " ", s).strip()
        return s

    addr_mode = (kind == "punkt") and (":" in q_raw) and (not has_num)
    addr_query = norm_addr(q_raw)
    nums = re.findall(r"\d+", addr_query)
    house_num = nums[-1] if nums else None
    raw_tokens = [t for t in re.split(r"\s+", addr_query) if t]
    addr_tokens = [t for t in raw_tokens if len(t) >= 4 and t not in {"вул", "пр", "пл"}]

    def matches(option_text: str) -> bool:
        if not option_text:
            return False
        tn_raw = re.sub(r"\s*\(до [^)]+\)\s*", " ", option_text, flags=re.IGNORECASE)
        tn = _norm_text(tn_raw)
        if strict_re is not None:
            return bool(strict_re.search(option_text))
        if addr_mode:
            tn_addr = norm_addr(tn_raw)
            if addr_tokens and not all(tok in tn_addr for tok in addr_tokens):
                return False
            if house_num and house_num not in tn_addr:
                return False
            return True
        return qn in tn

    return matches


def _build_terminal_option_matcher(query: str):
    raw = query or ""
    num = _extract_terminal_number(raw)
    normalized = _norm_text(raw)
    if num:
        strict_re = re.compile(rf"(?:№\s*)?(?<!\d){re.escape(num)}(?!\d)", re.IGNORECASE)

        def matches(option_text: str) -> bool:
            return bool(option_text and strict_re.search(option_text))

        return matches

    raw_tokens = [t for t in re.split(r"[\s/,\-]+", normalized) if t]
    strong_tokens = [t for t in raw_tokens if len(t) >= 3 and t not in {"вул", "пр", "пл", "буд"}]

    def matches(option_text: str) -> bool:
        if not option_text:
            return False
        tn = _norm_text(option_text)
        if strong_tokens:
            return all(tok in tn for tok in strong_tokens)
        return normalized in tn

    return matches


async def _sumo_choose_option(
    page,
    *,
    select_id: str,
    query: str,
    matcher,
    reason_not_found: str,
    max_attempts: int = 3,
    require_query_typed: bool = False,
) -> tuple[bool, str, dict]:
    last_options: list[str] = []
    typed_debug = {"attempts": 0, "typed_ok": False, "last_typed_value": "", "input_found": False}
    for _attempt in range(1, max_attempts + 1):
        opened = await _sumo_open(page, select_id)
        if not opened:
            await page.wait_for_timeout(220)
            continue
        container = await _sumo_container(page, select_id)
        if container is None:
            continue
        options = container.locator(".optWrapper li.opt:not(.disabled):not(.hidden):visible")
        baseline_count = 0
        baseline_texts: list[str] = []
        try:
            baseline_count = await options.count()
            for bi in range(min(baseline_count, 50)):
                btxt = ((await options.nth(bi).inner_text()) or "").strip()
                if btxt:
                    baseline_texts.append(_norm_text(btxt))
        except Exception:
            baseline_count = 0
            baseline_texts = []

        search_inputs = container.locator(".optWrapper input, .search-txt input")
        query_typed_ok = not require_query_typed
        try:
            search_input = None
            if await search_inputs.count() > 0 and await search_inputs.first.is_visible():
                search_input = search_inputs.first
            if search_input is None:
                global_search = page.locator(
                    ".SumoSelect.open .optWrapper input:visible, .SumoSelect.open .search-txt input:visible, input[placeholder*='ошук']:visible"
                ).first
                if await global_search.count() > 0 and await global_search.is_visible():
                    search_input = global_search
            if search_input is not None:
                typed_debug["input_found"] = True
                await search_input.click(timeout=min(3000, SUP6_TIMEOUT_MS), force=True)
                await search_input.fill("", timeout=min(3000, SUP6_TIMEOUT_MS))
                await search_input.type(query, delay=12, timeout=min(5000, SUP6_TIMEOUT_MS))
                typed_debug["attempts"] = int(typed_debug["attempts"]) + 1
                typed_now = (await search_input.input_value()) or ""
                typed_debug["last_typed_value"] = typed_now
                if _norm_text(query) in _norm_text(typed_now):
                    query_typed_ok = True
            elif require_query_typed:
                # Some SumoSelect builds keep focus on hidden text field; type via keyboard as fallback.
                await container.click(timeout=min(3000, SUP6_TIMEOUT_MS), force=True)
                await page.keyboard.press(_select_all_shortcut())
                await page.keyboard.press("Backspace")
                await page.keyboard.type(query, delay=12)
                typed_debug["attempts"] = int(typed_debug["attempts"]) + 1
                active_val = await page.evaluate(
                    """() => {
                        const ae = document.activeElement;
                        if (!ae) return '';
                        return (ae.value || ae.textContent || '').toString();
                    }"""
                )
                typed_debug["last_typed_value"] = str(active_val or "")
                if _norm_text(query) in _norm_text(str(active_val or "")):
                    query_typed_ok = True
        except Exception:
            query_typed_ok = not require_query_typed

        if require_query_typed and not query_typed_ok:
            await page.wait_for_timeout(220)
            continue
        if query_typed_ok:
            typed_debug["typed_ok"] = True

        # Wait for dropdown filtering to actually apply (debounce/ajax).
        query_norm = _norm_text(query or "")
        filter_deadline = asyncio.get_running_loop().time() + min(3.5, SUP6_TIMEOUT_MS / 1000.0)
        while asyncio.get_running_loop().time() < filter_deadline:
            try:
                curr_count = await options.count()
            except Exception:
                curr_count = 0
            curr_texts_norm: list[str] = []
            try:
                for ci in range(min(curr_count, 50)):
                    ctxt = ((await options.nth(ci).inner_text()) or "").strip()
                    if ctxt:
                        curr_texts_norm.append(_norm_text(ctxt))
            except Exception:
                curr_texts_norm = []

            has_query_match = bool(query_norm) and any((query_norm in t) for t in curr_texts_norm)
            list_changed = (curr_count != baseline_count) or (curr_texts_norm != baseline_texts)
            if has_query_match or list_changed:
                break
            await page.wait_for_timeout(220)

        await page.wait_for_timeout(180)
        try:
            count = await options.count()
        except Exception:
            count = 0
        candidates: list[tuple[int, str]] = []
        scan_limit = min(count, 1200)
        for i in range(scan_limit):
            opt = options.nth(i)
            try:
                txt = ((await opt.inner_text()) or "").strip()
            except Exception:
                txt = ""
            if txt:
                last_options.append(txt)
            if txt and matcher(txt):
                candidates.append((i, txt))

        if candidates:
            pick_i, picked_text = candidates[0]
            pick = options.nth(pick_i)
            try:
                await pick.scroll_into_view_if_needed(timeout=min(2000, SUP6_TIMEOUT_MS))
            except Exception:
                pass
            try:
                await pick.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
            except Exception:
                try:
                    await pick.locator("label, span, p").first.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                except Exception:
                    await page.wait_for_timeout(220)
                    continue
            await page.wait_for_timeout(260)
            selected = await _sumo_selected_text(page, select_id)
            if selected and not _is_default_placeholder_text(selected):
                return True, selected, {"picked": picked_text}
        await page.wait_for_timeout(220)

    uniq_opts = []
    seen = set()
    for o in last_options:
        n = _norm_text(o)
        if n in seen:
            continue
        seen.add(n)
        uniq_opts.append(o)
    if require_query_typed and not bool(typed_debug.get("typed_ok")):
        return False, "", {
            "reason": "QUERY_NOT_TYPED",
            "query": query,
            "select_id": select_id,
            "typed_debug": typed_debug,
            "seen_options": uniq_opts[:25],
        }
    return False, "", {"reason": reason_not_found, "query": query, "seen_options": uniq_opts[:25], "typed_debug": typed_debug}


async def _step5_select_delivery_np_pickup(page) -> bool:
    preferred = [
        page.locator("label[for='typeOfDelivery0']").first,
        page.get_by_text("Самовивіз з Нової пошти", exact=False).first,
        page.locator("label:has-text('Самовивіз з Нової пошти')").first,
        page.locator("#typeOfDelivery0").first,
    ]

    async def _is_selected() -> bool:
        try:
            label = page.locator("label[for='typeOfDelivery0']").first
            if await label.count() > 0:
                cls = ((await label.get_attribute("class")) or "").casefold()
                if "selected" in cls:
                    return True
        except Exception:
            pass
        try:
            radio = page.locator("#typeOfDelivery0").first
            if await radio.count() > 0 and await radio.is_checked():
                return True
        except Exception:
            pass
        return False

    if await _is_selected():
        print("[SUP6] selected delivery type: Нова пошта самовивіз")
        return True

    for cand in preferred:
        try:
            if await cand.count() <= 0:
                continue
            if await cand.first.is_visible():
                await cand.first.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
                await cand.first.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(260)
                if await _is_selected():
                    print("[SUP6] selected delivery type: Нова пошта самовивіз")
                    return True
        except Exception:
            continue
    return False


async def _step5_select_delivery_payer_receiver(page) -> bool:
    async def _is_selected() -> bool:
        try:
            label = page.locator("label[for='typePayerDelivery1']").first
            if await label.count() > 0:
                cls = ((await label.get_attribute("class")) or "").casefold()
                if "selected" in cls:
                    return True
        except Exception:
            pass
        try:
            radio = page.locator("#typePayerDelivery1").first
            if await radio.count() > 0 and await radio.is_checked():
                return True
        except Exception:
            pass
        return False

    if await _is_selected():
        print("[SUP6] selected delivery payer: Одержувач")
        return True

    candidates = [
        page.locator("label[for='typePayerDelivery1']").first,
        page.get_by_text("Одержувач", exact=False).first,
        page.locator("#typePayerDelivery1").first,
    ]
    for cand in candidates:
        try:
            if await cand.count() <= 0:
                continue
            if await cand.first.is_visible():
                await cand.first.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
                await cand.first.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(220)
                if await _is_selected():
                    print("[SUP6] selected delivery payer: Одержувач")
                    return True
        except Exception:
            continue
    return False


async def _step5_select_city_with_district(
    page,
    *,
    city_query: str,
    district_raw: str,
    region_raw: str,
) -> tuple[bool, str, dict, str]:
    opened = await _sumo_open(page, "selectCity")
    if not opened:
        return False, "", {"query": city_query, "reason": "CITY_DROPDOWN_NOT_OPENED"}, "NP_CITY_NOT_FOUND"
    container = await _sumo_container(page, "selectCity")
    if container is None:
        return False, "", {"query": city_query, "reason": "CITY_CONTAINER_NOT_FOUND"}, "NP_CITY_NOT_FOUND"

    typed_debug = {"attempts": 0, "typed_ok": False, "last_typed_value": "", "input_found": False}
    search_input = None
    try:
        search_input = container.locator(".optWrapper input:visible, .search-txt input:visible").first
        if await search_input.count() <= 0 or (not await search_input.is_visible()):
            search_input = page.locator(".SumoSelect.open .optWrapper input:visible, .SumoSelect.open .search-txt input:visible").first
        if await search_input.count() > 0 and await search_input.is_visible():
            typed_debug["input_found"] = True
            await search_input.click(timeout=min(3000, SUP6_TIMEOUT_MS), force=True)
            await search_input.fill("", timeout=min(3000, SUP6_TIMEOUT_MS))
            await search_input.type(city_query, delay=12, timeout=min(5000, SUP6_TIMEOUT_MS))
            typed_debug["attempts"] = 1
            typed_now = (await search_input.input_value()) or ""
            typed_debug["last_typed_value"] = typed_now
            typed_debug["typed_ok"] = _norm_text(city_query) in _norm_text(typed_now)
    except Exception:
        pass

    # Some layouts render city popup without a visible text input.
    # In that case continue with direct option matching instead of hard-failing.
    if typed_debug["input_found"] and (not typed_debug["typed_ok"]):
        return (
            False,
            "",
            {"reason": "QUERY_NOT_TYPED", "query": city_query, "typed_debug": typed_debug, "select_id": "selectCity"},
            "NP_CITY_QUERY_NOT_TYPED",
        )

    options = container.locator(".optWrapper li.opt:not(.disabled):not(.hidden):visible")
    if typed_debug["typed_ok"]:
        deadline = asyncio.get_running_loop().time() + min(3.5, SUP6_TIMEOUT_MS / 1000.0)
        query_norm = _norm_city_name_only(city_query)
        while asyncio.get_running_loop().time() < deadline:
            curr_texts = []
            try:
                cnt = await options.count()
            except Exception:
                cnt = 0
            for i in range(min(cnt, 80)):
                try:
                    curr_texts.append(_norm_text((await options.nth(i).inner_text()) or ""))
                except Exception:
                    continue
            if any(query_norm in _norm_city_name_only(t) for t in curr_texts):
                break
            await page.wait_for_timeout(220)

    try:
        count = await options.count()
    except Exception:
        count = 0
    candidates: list[dict] = []
    seen_options: list[str] = []
    city_norm = _norm_city_name_only(city_query)
    order_district_norm = _norm_district_name(district_raw)
    order_region_norm = _norm_area_region(region_raw)
    for i in range(min(count, 1200)):
        opt = options.nth(i)
        try:
            txt = _normalize_spaces((await opt.inner_text()) or "")
        except Exception:
            txt = ""
        if not txt:
            continue
        seen_options.append(txt)
        base_raw, district_hint_raw = _split_city_option_text(txt)
        base_norm = _norm_city_name_only(base_raw)
        district_hint_norm = _norm_district_name(district_hint_raw)
        if base_norm != city_norm:
            continue
        candidates.append(
            {
                "idx": i,
                "text": txt,
                "base": base_raw,
                "district_hint": district_hint_raw,
                "district_hint_norm": district_hint_norm,
            }
        )

    print(f"[SUP6] city raw from order => {city_query}")
    print(f"[SUP6] district raw from order => {district_raw}")
    print(f"[SUP6] city candidates by name => {json.dumps([c['text'] for c in candidates], ensure_ascii=False)}")

    if not candidates:
        return (
            False,
            "",
            {
                "reason": "NP_CITY_NOT_FOUND",
                "query": city_query,
                "district_raw": district_raw,
                "region_raw": region_raw,
                "typed_debug": typed_debug,
                "seen_options": seen_options[:25],
                "candidates_by_name": [],
            },
            "NP_CITY_NOT_FOUND",
        )

    selected = None
    district_tiebreak_used = False
    if len(candidates) == 1:
        selected = candidates[0]
    else:
        if order_district_norm:
            district_tiebreak_used = True
            narrowed = [c for c in candidates if _district_soft_match(order_district_norm, c.get("district_hint_norm") or "")]
            print(f"[SUP6] district tie-break applied => {json.dumps([c['text'] for c in narrowed], ensure_ascii=False)}")
            if len(narrowed) == 1:
                selected = narrowed[0]
            elif len(narrowed) > 1:
                return (
                    False,
                    "",
                    {
                        "reason": "CITY_AMBIGUOUS",
                        "query": city_query,
                        "district_raw": district_raw,
                        "district_norm": order_district_norm,
                        "candidates_by_name": [c["text"] for c in candidates],
                        "candidates_after_district": [c["text"] for c in narrowed],
                        "typed_debug": typed_debug,
                    },
                    "CITY_AMBIGUOUS",
                )
            elif order_region_norm:
                narrowed_by_region = [c for c in candidates if _district_soft_match(order_region_norm, c.get("district_hint_norm") or "")]
                print(f"[SUP6] region tie-break applied => {json.dumps([c['text'] for c in narrowed_by_region], ensure_ascii=False)}")
                if len(narrowed_by_region) == 1:
                    selected = narrowed_by_region[0]
                elif len(narrowed_by_region) > 1:
                    return (
                        False,
                        "",
                        {
                            "reason": "CITY_AMBIGUOUS",
                            "query": city_query,
                            "district_raw": district_raw,
                            "district_norm": order_district_norm,
                            "region_raw": region_raw,
                            "region_norm": order_region_norm,
                            "candidates_by_name": [c["text"] for c in candidates],
                            "candidates_after_region": [c["text"] for c in narrowed_by_region],
                            "typed_debug": typed_debug,
                        },
                        "CITY_AMBIGUOUS",
                    )
        if selected is None:
            narrowed_by_region = []
            if order_region_norm:
                narrowed_by_region = [c for c in candidates if _district_soft_match(order_region_norm, c.get("district_hint_norm") or "")]
                print(f"[SUP6] region tie-break applied => {json.dumps([c['text'] for c in narrowed_by_region], ensure_ascii=False)}")
                if len(narrowed_by_region) == 1:
                    selected = narrowed_by_region[0]
            if selected is not None:
                pass
            else:
                return (
                    False,
                    "",
                    {
                        "reason": "CITY_AMBIGUOUS",
                    "query": city_query,
                    "district_raw": district_raw,
                    "district_norm": order_district_norm,
                    "candidates_by_name": [c["text"] for c in candidates],
                        "region_raw": region_raw,
                        "region_norm": order_region_norm,
                        "candidates_after_region": [c["text"] for c in narrowed_by_region],
                        "typed_debug": typed_debug,
                    },
                    "CITY_AMBIGUOUS",
                )

    pick = options.nth(int(selected["idx"]))
    try:
        await pick.scroll_into_view_if_needed(timeout=min(2000, SUP6_TIMEOUT_MS))
    except Exception:
        pass
    try:
        await pick.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
    except Exception:
        try:
            await pick.locator("label, span, p").first.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
        except Exception as e:
            return False, "", {"reason": "CITY_PICK_CLICK_FAILED", "error": str(e), "candidate": selected}, "NP_CITY_NOT_FOUND"

    await page.wait_for_timeout(260)
    selected_city = await _sumo_selected_text(page, "selectCity")
    if not selected_city or _is_default_placeholder_text(selected_city):
        return False, "", {"reason": "CITY_PICK_NOT_APPLIED", "candidate": selected}, "NP_CITY_NOT_FOUND"

    return (
        True,
        selected_city,
        {
            "city_raw": city_query,
            "district_raw": district_raw,
            "region_raw": region_raw,
            "candidates_by_name": [c["text"] for c in candidates],
            "selected_candidate": selected["text"],
            "district_tiebreak_used": district_tiebreak_used,
            "typed_debug": typed_debug,
        },
        "",
    )


async def step5_fill_delivery_np_pickup(page, order_payload: dict | None = None) -> dict:
    try:
        await _step4_ensure_checkout_open(page)
    except Exception as e:
        return _step5_fail("CHECKOUT_OPEN_FAILED", details={"error": str(e), "url": page.url or ""})

    if not await _step5_select_delivery_np_pickup(page):
        return _step5_fail("DELIVERY_TYPE_NOT_SELECTED", details={"expected": "Самовивіз з Нової пошти"})

    delivery = _extract_delivery_values(order_payload)
    if not delivery["region"]:
        return _step5_fail("NP_REGION_MISSING", details={"delivery": delivery})
    if not delivery["city"]:
        return _step5_fail("NP_CITY_MISSING", details={"delivery": delivery})
    if not delivery["warehouse_query"]:
        return _step5_fail("NP_WAREHOUSE_QUERY_MISSING", details={"delivery": delivery})

    city_snapshot_before = await _sumo_options_snapshot(page, "selectCity")

    region_query = delivery["region"]
    region_matcher = lambda txt: region_query in _norm_area_region(txt)  # noqa: E731
    ok_region, selected_region, region_meta = await _sumo_choose_option(
        page,
        select_id="selectRegion",
        query=region_query,
        matcher=region_matcher,
        reason_not_found="NP_REGION_NOT_FOUND",
    )
    if not ok_region:
        return _step5_fail("NP_REGION_NOT_FOUND", details=region_meta)
    print(f"[SUP6] selected region: {selected_region}")

    if not await _sumo_wait_enabled(page, "selectCity", SUP6_TIMEOUT_MS):
        return _step5_fail("NP_CITY_FIELD_DISABLED", details={"after_region": selected_region})
    await _sumo_wait_options_reload(page, "selectCity", city_snapshot_before, min(SUP6_TIMEOUT_MS, 6000))
    ok_city, selected_city, city_meta, city_err_reason = await _step5_select_city_with_district(
        page,
        city_query=delivery["city"],
        district_raw=delivery.get("district") or "",
        region_raw=delivery.get("region") or "",
    )
    if not ok_city:
        if city_err_reason == "NP_CITY_QUERY_NOT_TYPED":
            return _step5_fail("NP_CITY_QUERY_NOT_TYPED", details=city_meta)
        if city_err_reason == "CITY_AMBIGUOUS":
            return _step5_fail("CITY_AMBIGUOUS", details=city_meta)
        return _step5_fail("NP_CITY_NOT_FOUND", details=city_meta)
    print(f"[SUP6] selected city => {selected_city}")

    if not await _sumo_wait_enabled(page, "selectWarehouses", SUP6_TIMEOUT_MS):
        return _step5_fail("NP_WAREHOUSE_FIELD_DISABLED", details={"after_city": selected_city})
    if delivery["warehouse_mode"] == "terminal":
        warehouse_matcher = _build_terminal_option_matcher(delivery["warehouse_query"])
        warehouse_reason = "NP_TERMINAL_NOT_FOUND"
    else:
        warehouse_matcher = _build_branch_option_matcher(delivery["branch_kind"], delivery["warehouse_query"])
        warehouse_reason = "NP_WAREHOUSE_NOT_FOUND"

    ok_wh, selected_wh, wh_meta = await _sumo_choose_option(
        page,
        select_id="selectWarehouses",
        query=delivery["warehouse_query"],
        matcher=warehouse_matcher,
        reason_not_found=warehouse_reason,
    )
    if not ok_wh:
        return _step5_fail(warehouse_reason, details=wh_meta)
    print(f"[SUP6] selected warehouse: {selected_wh}")

    if not await _step5_select_delivery_payer_receiver(page):
        return _step5_fail("DELIVERY_PAYER_NOT_SELECTED", details={"expected": "Одержувач"})

    final_region = await _sumo_selected_text(page, "selectRegion")
    final_city = await _sumo_selected_text(page, "selectCity")
    final_wh = await _sumo_selected_text(page, "selectWarehouses")
    if (not final_region) or _is_default_placeholder_text(final_region):
        return _step5_fail("NP_REGION_EMPTY_AFTER_SELECT", details={"selected_region": final_region})
    if (not final_city) or _is_default_placeholder_text(final_city):
        return _step5_fail("NP_CITY_EMPTY_AFTER_SELECT", details={"selected_city": final_city})
    if (not final_wh) or _is_default_placeholder_text(final_wh):
        return _step5_fail("NP_WAREHOUSE_EMPTY_AFTER_SELECT", details={"selected_warehouse": final_wh})

    if not await _step5_select_delivery_np_pickup(page):
        return _step5_fail("DELIVERY_TYPE_VERIFY_FAILED")
    if not await _step5_select_delivery_payer_receiver(page):
        return _step5_fail("DELIVERY_PAYER_VERIFY_FAILED")

    return {
        "ok": True,
        "step": "step5_fill_delivery_np_pickup",
        "details": {
            "region": final_region,
            "city": final_city,
            "warehouse": final_wh,
            "payer": "Одержувач",
            "warehouse_mode": delivery["warehouse_mode"],
            "warehouse_query": delivery["warehouse_query"],
            "city_selection": city_meta,
        },
    }


def _step6_fail(reason: str, details: dict | None = None) -> dict:
    payload = {"ok": False, "step": "step6_fill_payment_and_client_prices", "reason": reason}
    if details:
        payload["details"] = details
    return payload


def _extract_payment_amount(order_payload: dict | None = None) -> Decimal | None:
    payload = order_payload if isinstance(order_payload, dict) else {}
    candidates = [
        payload.get("paymentAmount"),
        payload.get("postpaySum"),
    ]
    d0 = _get_first_delivery_block(payload)
    if isinstance(d0, dict):
        candidates.append(d0.get("postpaySum"))
    for c in candidates:
        val = _to_decimal_number(c)
        if val is not None:
            return val
    return None


async def _step6_select_payment_cod(page) -> bool:
    async def _is_selected() -> bool:
        for label_sel in ("label[for='typeOfPayment1']", "label:has-text('Післяплата')"):
            label = page.locator(label_sel).first
            try:
                if await label.count() > 0:
                    cls = ((await label.get_attribute("class")) or "").casefold()
                    if "selected" in cls:
                        return True
            except Exception:
                pass
        for radio_sel in ("#typeOfPayment1", "input[type='radio'][name='form[typeOfPayment]'][value='1']"):
            radio = page.locator(radio_sel).first
            try:
                if await radio.count() > 0 and await radio.is_checked():
                    return True
            except Exception:
                pass
        return False

    if await _is_selected():
        print("[SUP6] selected payment type: Післяплата")
        return True

    for cand in [
        page.locator("label[for='typeOfPayment1']").first,
        page.get_by_text("Післяплата", exact=False).first,
        page.locator("#typeOfPayment1").first,
    ]:
        try:
            if await cand.count() <= 0:
                continue
            if await cand.first.is_visible():
                await cand.first.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
                await cand.first.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(240)
                if await _is_selected():
                    print("[SUP6] selected payment type: Післяплата")
                    return True
        except Exception:
            continue
    return False


async def _step6_fill_cod_amount(page, amount: Decimal) -> tuple[bool, str]:
    field = await _step4_pick_field(page, ["#cashRedelivery", "input[name='form[cashRedelivery]']"])
    if field is None:
        return False, ""
    expected = _money_to_stripped_intish(amount)
    if not expected:
        expected = str(amount)
    try:
        await field.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
        await field.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
        await field.click(timeout=min(3000, SUP6_TIMEOUT_MS))
        await page.keyboard.press(_select_all_shortcut())
        await page.keyboard.press("Backspace")
        await field.fill(expected, timeout=min(5000, SUP6_TIMEOUT_MS))
        try:
            await field.press("Tab", timeout=min(1500, SUP6_TIMEOUT_MS))
        except Exception:
            pass
        await page.wait_for_timeout(240)
        val = (await field.input_value() or "").strip()
        if _to_decimal_number(val) is None:
            return False, val
        print(f"[SUP6] filled paymentAmount: {expected}")
        return True, val
    except Exception:
        try:
            val = (await field.input_value() or "").strip()
        except Exception:
            val = ""
        return False, val


async def _step6_collect_checkout_rows(page) -> list[dict]:
    rows = page.locator("div.clientPriceRow")
    out: list[dict] = []
    try:
        count = await rows.count()
    except Exception:
        count = 0
    for i in range(count):
        row = rows.nth(i)
        try:
            if not await row.is_visible():
                continue
        except Exception:
            continue
        name_loc = row.locator(".productName").first
        input_loc = row.locator("input[name^='form[clientPrice]']").first
        try:
            if await input_loc.count() <= 0:
                continue
            raw_name = _normalize_spaces((await name_loc.inner_text(timeout=min(1200, SUP6_TIMEOUT_MS))) or "")
            row_qty_attr = await name_loc.get_attribute("data-quantity") if await name_loc.count() > 0 else None
            row_qty = None
            if row_qty_attr:
                try:
                    q = int(str(row_qty_attr).strip())
                    row_qty = q if q > 0 else None
                except Exception:
                    row_qty = None
            if row_qty is None:
                row_qty = _extract_qty_prefix(raw_name)
            out.append(
                {
                    "row": row,
                    "name_raw": raw_name,
                    "name_norm": _norm_product_title(raw_name),
                    "qty": row_qty,
                    "input": input_loc,
                }
            )
        except Exception:
            continue
    return out


async def _step6_read_final_check_sum(page) -> tuple[Decimal | None, str]:
    # UI variants: span text, input value, data attrs.
    js = """() => {
        const candidates = [
          document.querySelector('#finalCheckSum'),
          document.querySelector('.checkSum #finalCheckSum'),
          document.querySelector('span#finalCheckSum'),
          document.querySelector('input#finalCheckSum'),
        ].filter(Boolean);
        for (const el of candidates) {
          const parts = [];
          if (typeof el.value === 'string') parts.push(el.value);
          if (typeof el.textContent === 'string') parts.push(el.textContent);
          if (typeof el.innerText === 'string') parts.push(el.innerText);
          if (typeof el.getAttribute === 'function') {
            parts.push(el.getAttribute('value') || '');
            parts.push(el.getAttribute('data-value') || '');
          }
          const joined = parts.join(' ').replace(/\\s+/g, ' ').trim();
          if (joined) return joined;
        }
        const block = document.querySelector('.checkSum');
        if (block) return (block.textContent || '').replace(/\\s+/g, ' ').trim();
        return '';
    }"""
    raw = ""
    try:
        raw = _normalize_spaces((await page.evaluate(js)) or "")
    except Exception:
        raw = ""
    return _to_decimal_number(raw), raw


async def _step6_trigger_recalc(page) -> None:
    try:
        await page.evaluate(
            """() => {
                const inputs = document.querySelectorAll("input[name^='form[clientPrice]'], #cashRedelivery, input[name='form[cashRedelivery]']");
                inputs.forEach((el) => {
                    try {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    } catch (_) {}
                });
            }"""
        )
    except Exception:
        pass


def _step6_build_fallback_map_from_order(order_payload: dict | None = None) -> list[dict]:
    rows = _extract_order_product_rows(order_payload)
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "sku": r.get("sku") or "",
                "qty": r.get("qty") or 1,
                "site_name": (r.get("name") or r.get("description") or ""),
                "client_price": r.get("price"),
            }
        )
    return out


def _step6_match_rows_with_map(checkout_rows: list[dict], item_map: list[dict]) -> tuple[bool, list[tuple[dict, dict]], dict]:
    prepared: list[dict] = []
    for idx, m in enumerate(item_map):
        if not isinstance(m, dict):
            continue
        site_name = str(m.get("site_name") or "").strip()
        qty_raw = m.get("qty")
        try:
            qty_val = int(str(qty_raw).strip())
            if qty_val < 1:
                qty_val = 1
        except Exception:
            qty_val = 1
        prepared.append(
            {
                "idx": idx,
                "sku": str(m.get("sku") or "").strip(),
                "qty": qty_val,
                "site_name": site_name,
                "site_name_norm": _norm_product_title(site_name),
                "client_price": m.get("client_price"),
                "used": False,
            }
        )

    matches: list[tuple[dict, dict]] = []
    if len(checkout_rows) == 1 and len(prepared) == 1:
        # Safe fallback for single-item checkout: still validate qty when available.
        only_row = checkout_rows[0]
        only_map = prepared[0]
        row_qty = only_row.get("qty")
        if (row_qty is None) or (int(row_qty) == int(only_map["qty"])):
            return True, [(only_row, only_map)], {}
    for row in checkout_rows:
        best = None
        best_score = -1
        rn = row["name_norm"]
        rq = row.get("qty")
        row_tokens = _title_tokens(row.get("name_raw") or rn)
        for m in prepared:
            if m["used"]:
                continue
            mn = m["site_name_norm"]
            score = -1
            if mn and rn == mn:
                score = 100
            elif mn and rn and (mn in rn or rn in mn):
                score = 80
            else:
                map_tokens = _title_tokens(m.get("site_name") or mn)
                if row_tokens and map_tokens:
                    overlap = row_tokens & map_tokens
                    union = row_tokens | map_tokens
                    ratio = (len(overlap) / len(union)) if union else 0.0
                    if len(overlap) >= 2 and ratio >= 0.25:
                        score = 60 + len(overlap)
            if score < 0:
                continue
            if rq and m["qty"] == rq:
                score += 15
            if rq and m["qty"] != rq:
                score -= 5
            if score > best_score:
                best_score = score
                best = m
        if best is None:
            return False, [], {"row_name": row.get("name_raw"), "row_qty": rq, "map_size": len(prepared)}
        best["used"] = True
        matches.append((row, best))
    return True, matches, {}


def _round_half_up_int(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _step6_calculate_integer_unit_prices(price_rows: list[dict], target_total_int: int) -> tuple[bool, list[int], dict]:
    if not price_rows:
        return False, [], {"reason": "NO_PRICE_ROWS"}
    unit_ints: list[int] = []
    qtys: list[int] = []
    for row in price_rows:
        raw_dec = _to_decimal_number(row.get("raw_price"))
        if raw_dec is None:
            return False, [], {"reason": "RAW_PRICE_MISSING", "row": row}
        qty = int(row.get("qty") or 1)
        if qty < 1:
            qty = 1
        qtys.append(qty)
        unit_ints.append(_round_half_up_int(raw_dec))

    subtotal = sum(u * q for u, q in zip(unit_ints, qtys))
    delta = int(target_total_int - subtotal)
    if delta == 0:
        return True, unit_ints, {"delta": 0}

    # Prefer qty=1 rows (exact +1/-1 control on total).
    idx_qty1 = [i for i, q in enumerate(qtys) if q == 1]
    if idx_qty1:
        pick = idx_qty1[0]
        candidate = unit_ints[pick] + delta
        if candidate < 0:
            return False, [], {"reason": "ROUNDING_NEGATIVE_PRICE", "idx": pick, "candidate": candidate}
        unit_ints[pick] = candidate
        subtotal2 = sum(u * q for u, q in zip(unit_ints, qtys))
        if subtotal2 == target_total_int:
            return True, unit_ints, {"delta": delta, "adjusted_idx": pick}

    # Greedy adjustments by qty chunks.
    sign = 1 if delta > 0 else -1
    remaining = abs(delta)
    order = sorted(range(len(qtys)), key=lambda i: qtys[i])
    for i in order:
        q = qtys[i]
        if q <= 0:
            continue
        steps = remaining // q
        if steps <= 0:
            continue
        if sign < 0:
            steps = min(steps, unit_ints[i])  # avoid negative unit price
        if steps <= 0:
            continue
        unit_ints[i] += sign * steps
        remaining -= steps * q
        if remaining == 0:
            break

    subtotal3 = sum(u * q for u, q in zip(unit_ints, qtys))
    if subtotal3 == target_total_int:
        return True, unit_ints, {"delta": delta}

    return False, [], {
        "reason": "ROUNDING_SUM_MISMATCH",
        "target_total_int": target_total_int,
        "subtotal": subtotal3,
        "delta": int(target_total_int - subtotal3),
        "qtys": qtys,
        "rounded_units": unit_ints,
    }


async def _step6_select_order_format_shipping(page) -> bool:
    async def _is_selected() -> bool:
        for label_sel in ("label[for='orderType1']", "label:has-text('Відправка')"):
            label = page.locator(label_sel).first
            try:
                if await label.count() > 0:
                    cls = ((await label.get_attribute("class")) or "").casefold()
                    if "selected" in cls:
                        return True
            except Exception:
                pass
        try:
            candidate = page.locator("input[type='radio']:checked").first
            if await candidate.count() > 0:
                parent_text = _norm_text((await candidate.locator("xpath=ancestor::*[self::label or self::div][1]").inner_text()) or "")
                if "відправка" in parent_text:
                    return True
        except Exception:
            pass
        return False

    if await _is_selected():
        print("[SUP6] selected order format: Відправка")
        return True

    candidates = [
        page.locator("label[for='orderType1']").first,
        page.get_by_text("Відправка", exact=False).first,
        page.locator("label:has-text('Відправка')").first,
    ]
    for cand in candidates:
        try:
            if await cand.count() <= 0:
                continue
            if await cand.first.is_visible():
                await cand.first.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
                await cand.first.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(220)
                if await _is_selected():
                    print("[SUP6] selected order format: Відправка")
                    return True
        except Exception:
            continue
    return False


async def _step6_check_ack(page) -> bool:
    checkbox = page.locator(
        "#importAntmsgConfirm, input[name='form[importAntmsgConfirm]'][type='checkbox'], "
        "input[id*='importAntmsgConfirm'][type='checkbox'], input[name*='importAntmsgConfirm'][type='checkbox'], "
        "input[id*='msgConfirm'][type='checkbox'], #agreeBan, input[name='agreeBan'][type='checkbox'], "
        "input[type='checkbox'][name*='agree']"
    ).first

    async def _checked() -> bool:
        try:
            if await checkbox.count() > 0 and await checkbox.is_checked():
                return True
        except Exception:
            pass
        for sel in ("label[for='importAntmsgConfirm']", "label[for='agreeBan']"):
            label = page.locator(sel).first
            try:
                if await label.count() > 0:
                    cls = ((await label.get_attribute("class")) or "").casefold()
                    if "selected" in cls:
                        return True
            except Exception:
                pass
        try:
            lbl_inline = page.locator(".rsform-block-importantmsgconfirm label.checkbox-inline.selected").first
            if await lbl_inline.count() > 0 and await lbl_inline.is_visible():
                return True
        except Exception:
            pass
        try:
            lbl_yes = page.locator("label.checkbox-inline:has-text('Так').selected").first
            if await lbl_yes.count() > 0 and await lbl_yes.is_visible():
                return True
        except Exception:
            pass
        return False

    if await _checked():
        print("[SUP6] acknowledgment checked")
        return True

    for cand in [
        page.locator("label[for='importAntmsgConfirm']").first,
        page.locator("label[for='agreeBan']").first,
        page.locator(".rsform-block-importantmsgconfirm label.checkbox-inline").first,
        page.locator("label.checkbox-inline:has-text('Так')").first,
        page.get_by_text("Я ознайомлений", exact=False).first,
        page.get_by_text("Так", exact=True).first,
        checkbox,
    ]:
        try:
            if await cand.count() > 0 and await cand.first.is_visible():
                await cand.first.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
                await cand.first.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                await page.wait_for_timeout(220)
                if await _checked():
                    print("[SUP6] acknowledgment checked")
                    return True
        except Exception:
            continue

    try:
        js = await page.evaluate(
            """() => {
                const block = document.querySelector('.rsform-block-importantmsgconfirm');
                const el = (block && block.querySelector('input[type="checkbox"]'))
                    || document.querySelector('#importAntmsgConfirm')
                    || document.querySelector('input[name="form[importAntmsgConfirm]"][type="checkbox"]')
                    || document.querySelector('input[id*="importAntmsgConfirm"][type="checkbox"]')
                    || document.querySelector('input[name*="importAntmsgConfirm"][type="checkbox"]')
                    || document.querySelector('#agreeBan')
                    || document.querySelector('input[name="agreeBan"][type="checkbox"]');
                if (el) {
                    el.checked = true;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    const lbl = el.closest('label.checkbox-inline') || (el.id ? document.querySelector(`label[for="${el.id}"]`) : null);
                    if (lbl) lbl.classList.add('selected');
                    return !!el.checked;
                }
                const lbl2 = (block && block.querySelector('label.checkbox-inline')) || document.querySelector('label.checkbox-inline[for="importAntmsgConfirm"]');
                if (lbl2) {
                    lbl2.classList.add('selected');
                    try { lbl2.click(); } catch (_) {}
                    return true;
                }
                return false;
            }"""
        )
        if js:
            await page.wait_for_timeout(220)
            if await _checked():
                print("[SUP6] acknowledgment checked")
                return True
    except Exception:
        pass

    return False


async def step6_fill_payment_and_client_prices(page, order_payload: dict | None = None, supplier6_item_map: list[dict] | None = None) -> dict:
    try:
        await _step4_ensure_checkout_open(page)
    except Exception as e:
        return _step6_fail("CHECKOUT_OPEN_FAILED", details={"error": str(e), "url": page.url or ""})

    if not await _step6_select_payment_cod(page):
        return _step6_fail("PAYMENT_TYPE_NOT_SELECTED", details={"expected": "Післяплата"})

    payment_amount_raw = _extract_payment_amount(order_payload)
    if payment_amount_raw is None:
        return _step6_fail("PAYMENT_AMOUNT_MISSING")
    target_total_int = _round_half_up_int(payment_amount_raw)
    print(f"[SUP6] rounded paymentAmount => {target_total_int}")
    cod_ok, cod_value = await _step6_fill_cod_amount(page, Decimal(target_total_int))
    if not cod_ok:
        return _step6_fail("PAYMENT_AMOUNT_FILL_FAILED", details={"expected": str(target_total_int), "value": cod_value})

    checkout_rows = await _step6_collect_checkout_rows(page)
    if not checkout_rows:
        return _step6_fail("CLIENT_PRICE_ROWS_NOT_FOUND")

    mapping = supplier6_item_map or []
    if not mapping:
        mapping = _step6_build_fallback_map_from_order(order_payload)
    if not mapping:
        return _step6_fail("ITEM_MAP_EMPTY")

    matched_ok, matches, match_meta = _step6_match_rows_with_map(checkout_rows, mapping)
    if not matched_ok:
        return _step6_fail("PRICE_ROW_MATCH_FAILED", details=match_meta)

    calc_rows: list[dict] = []
    for row_info, map_info in matches:
        qty = int(row_info.get("qty") or map_info.get("qty") or 1)
        if qty < 1:
            qty = 1
        calc_rows.append(
            {
                "sku": map_info.get("sku"),
                "site_name": map_info.get("site_name"),
                "qty": qty,
                "raw_price": map_info.get("client_price"),
            }
        )
    calc_ok, unit_ints, calc_meta = _step6_calculate_integer_unit_prices(calc_rows, target_total_int)
    if not calc_ok:
        if str(calc_meta.get("reason") or "") == "ROUNDING_SUM_MISMATCH":
            fallback_units = calc_meta.get("rounded_units")
            fallback_subtotal = calc_meta.get("subtotal")
            if isinstance(fallback_units, list) and fallback_units and isinstance(fallback_subtotal, int):
                unit_ints = [int(x) for x in fallback_units]
                target_total_int = int(fallback_subtotal)
                print(f"[SUP6] rounded paymentAmount => adjusted to reachable total {target_total_int}")
                cod_ok2, cod_value2 = await _step6_fill_cod_amount(page, Decimal(target_total_int))
                if not cod_ok2:
                    return _step6_fail(
                        "PAYMENT_AMOUNT_FILL_FAILED",
                        details={"expected": str(target_total_int), "value": cod_value2, "rounding_meta": calc_meta},
                    )
                cod_value = cod_value2
            else:
                return _step6_fail("ROUNDING_SUM_MISMATCH", details=calc_meta)
        else:
            return _step6_fail("ROUNDING_SUM_MISMATCH", details=calc_meta)
    prices_log = [{"sku": calc_rows[i]["sku"], "qty": calc_rows[i]["qty"], "unit_price_int": unit_ints[i]} for i in range(len(unit_ints))]
    print(f"[SUP6] calculated integer client prices => {json.dumps(prices_log, ensure_ascii=False)}")

    filled_count = 0
    for idx, (row_info, map_info) in enumerate(matches):
        target_str = str(unit_ints[idx])
        field = row_info["input"]
        try:
            await field.wait_for(state="visible", timeout=SUP6_TIMEOUT_MS)
            await field.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
            await field.click(timeout=min(3000, SUP6_TIMEOUT_MS))
            await page.keyboard.press(_select_all_shortcut())
            await page.keyboard.press("Backspace")
            await field.fill(target_str, timeout=min(4500, SUP6_TIMEOUT_MS))
            try:
                await field.press("Tab", timeout=min(1500, SUP6_TIMEOUT_MS))
            except Exception:
                pass
            await page.wait_for_timeout(220)
            after = (await field.input_value() or "").strip()
            after_dec = _to_decimal_number(after)
            if after_dec is None or int(after_dec) != int(unit_ints[idx]):
                return _step6_fail("CLIENT_PRICE_FILL_FAILED", details={"sku": map_info.get("sku"), "value": after})
            print(f"[SUP6] matched checkout row -> sku={map_info.get('sku')} site_name={map_info.get('site_name')}")
            print(f"[SUP6] filled client price for item: {target_str}")
            filled_count += 1
        except Exception as e:
            return _step6_fail("CLIENT_PRICE_FILL_FAILED", details={"sku": map_info.get("sku"), "error": str(e)})

    await _step6_trigger_recalc(page)
    await page.wait_for_timeout(280)
    cod_sum = _to_decimal_number(cod_value)
    final_sum = None
    final_sum_text = ""
    final_sum_candidates: list[Decimal] = []
    deadline = asyncio.get_running_loop().time() + (SUP6_TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        current_sum, current_raw = await _step6_read_final_check_sum(page)
        if current_raw:
            final_sum_text = current_raw
            final_sum_candidates = _extract_money_candidates(current_raw)
            if cod_sum is not None and final_sum_candidates:
                for cand in final_sum_candidates:
                    if cand == cod_sum:
                        final_sum = cand
                        break
                if final_sum is not None:
                    break
        if current_sum is not None:
            final_sum = current_sum
            if (cod_sum is None) or (final_sum == cod_sum):
                break
        await _step6_trigger_recalc(page)
        await page.wait_for_timeout(220)

    cod_sum = _to_decimal_number(cod_value)
    if final_sum is None and final_sum_candidates:
        if len(final_sum_candidates) == 1:
            final_sum = final_sum_candidates[0]
        elif cod_sum is not None:
            for cand in final_sum_candidates:
                if cand == cod_sum:
                    final_sum = cand
                    break
            if final_sum is None:
                final_sum = final_sum_candidates[-1]
        else:
            final_sum = final_sum_candidates[-1]

    if final_sum is None or cod_sum is None:
        return _step6_fail(
            "CHECK_SUM_READ_FAILED",
            details={
                "final_sum_text": final_sum_text,
                "final_sum_candidates": [str(x) for x in final_sum_candidates],
                "cod_value": cod_value,
            },
        )
    if int(final_sum) != int(cod_sum) or int(final_sum) != int(target_total_int):
        return _step6_fail(
            "CHECK_SUM_MISMATCH",
            details={
                "final_sum": str(final_sum),
                "cod_sum": str(cod_sum),
                "target_total_int": target_total_int,
                "final_sum_text": final_sum_text,
                "final_sum_candidates": [str(x) for x in final_sum_candidates],
            },
        )
    print(f"[SUP6] final check sum verified => {int(final_sum)}")

    if not await _step6_select_order_format_shipping(page):
        return _step6_fail("ORDER_FORMAT_NOT_SELECTED", details={"expected": "Відправка"})
    if not await _step6_check_ack(page):
        return _step6_fail("ACK_CHECK_FAILED")

    return {
        "ok": True,
        "step": "step6_fill_payment_and_client_prices",
        "details": {
            "payment_type": "Післяплата",
            "payment_amount": str(int(target_total_int)),
            "client_prices_filled": filled_count,
            "final_check_sum": str(int(final_sum)),
            "format": "Відправка",
            "ack_checked": True,
            "client_prices": prices_log,
            "target_total_int": target_total_int,
        },
    }


def _step7_fail(reason: str, details: dict | None = None) -> dict:
    payload = {"ok": False, "step": "step7_submit_order", "reason": reason}
    if details:
        payload["details"] = details
    return payload


async def _step7_get_submit_button(page):
    for cand in [
        page.locator("#SendDoppler").first,
        page.locator("button[name='SendDoppler']").first,
        page.locator("button#SendDoppler[type='button']").first,
        page.get_by_role("button", name=re.compile(r"Підтвердити", re.IGNORECASE)).first,
        page.get_by_text("Підтвердити", exact=False).first,
    ]:
        try:
            if await cand.count() > 0:
                return cand
        except Exception:
            continue
    return None


async def _step7_wait_submit_outcome(page, start_url: str) -> tuple[str, dict]:
    success_markers = [
        "Дякуємо за замовлення",
        "замовлення оформлено",
        "Ваше замовлення прийнято",
    ]
    validation_markers = [
        ".formError:visible",
        ".rsform-error:visible",
        ".alert-danger:visible",
        ".error:visible",
    ]
    deadline = asyncio.get_running_loop().time() + (SUP6_TIMEOUT_MS / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        url = page.url or ""
        if url and url != start_url and ("shipping-and-payment" not in url):
            return "success", {"url": url, "mode": "url_change"}
        for marker in success_markers:
            try:
                if await page.get_by_text(marker, exact=False).first.is_visible():
                    return "success", {"url": url, "mode": f"text:{marker}"}
            except Exception:
                continue
        for sel in validation_markers:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    txt = _normalize_spaces((await loc.inner_text()) or "")
                    return "validation", {"url": url, "error_text": txt, "selector": sel}
            except Exception:
                continue
        await page.wait_for_timeout(220)
    return "timeout", {"url": page.url or start_url}


async def step7_submit_order(page, order_payload: dict | None = None, pricing_details: dict | None = None) -> dict:
    try:
        await _step4_ensure_checkout_open(page)
    except Exception as e:
        return _step7_fail("CHECKOUT_OPEN_FAILED", {"error": str(e)})

    if not await _step6_select_payment_cod(page):
        return _step7_fail("PRECHECK_PAYMENT_NOT_SELECTED")
    if not await _step6_select_order_format_shipping(page):
        return _step7_fail("PRECHECK_FORMAT_NOT_SELECTED")
    if not await _step6_check_ack(page):
        return _step7_fail("PRECHECK_ACK_NOT_CHECKED")

    target_total_int = None
    if isinstance(pricing_details, dict):
        try:
            target_total_int = int(str(pricing_details.get("target_total_int") or pricing_details.get("payment_amount") or "").strip())
        except Exception:
            target_total_int = None
    if target_total_int is None:
        amount_raw = _extract_payment_amount(order_payload)
        if amount_raw is None:
            return _step7_fail("PRECHECK_PAYMENT_AMOUNT_MISSING")
        target_total_int = _round_half_up_int(amount_raw)

    check_sum_val, check_sum_text = await _step6_read_final_check_sum(page)
    candidates = _extract_money_candidates(check_sum_text)
    final_int = None
    if candidates:
        for c in candidates:
            if int(c) == int(target_total_int):
                final_int = int(c)
                break
        if final_int is None:
            final_int = int(candidates[-1])
    elif check_sum_val is not None:
        final_int = int(check_sum_val)
    if final_int is None or int(final_int) != int(target_total_int):
        return _step7_fail(
            "PRECHECK_SUM_MISMATCH",
            {"target_total_int": target_total_int, "final_sum": final_int, "check_sum_text": check_sum_text},
        )

    client_inputs = page.locator("input[name^='form[clientPrice]']")
    try:
        cnt = await client_inputs.count()
    except Exception:
        cnt = 0
    client_prices: list[int] = []
    for i in range(cnt):
        inp = client_inputs.nth(i)
        try:
            if not await inp.is_visible():
                continue
            val = (await inp.input_value() or "").strip()
            dec = _to_decimal_number(val)
            if dec is None:
                return _step7_fail("PRECHECK_CLIENT_PRICE_EMPTY", {"index": i, "value": val})
            iv = int(dec)
            if Decimal(iv) != dec:
                return _step7_fail("PRECHECK_CLIENT_PRICE_NOT_INTEGER", {"index": i, "value": val})
            client_prices.append(iv)
        except Exception as e:
            return _step7_fail("PRECHECK_CLIENT_PRICE_READ_FAILED", {"index": i, "error": str(e)})

    submit_btn = await _step7_get_submit_button(page)
    if submit_btn is None:
        return _step7_fail("SUBMIT_BUTTON_NOT_FOUND")

    start_url = page.url or ""
    clicked = False
    for attempt in (1, 2):
        try:
            await submit_btn.scroll_into_view_if_needed(timeout=min(2500, SUP6_TIMEOUT_MS))
            print("[SUP6] clicking submit => Підтвердити")
            await submit_btn.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=(attempt == 2))
            clicked = True
            break
        except Exception:
            await page.wait_for_timeout(220)
    if not clicked:
        return _step7_fail("SUBMIT_CLICK_FAILED")

    outcome, meta = await _step7_wait_submit_outcome(page, start_url)
    if outcome == "success":
        print("[SUP6] submit success")
        return {
            "ok": True,
            "step": "step7_submit_order",
            "details": {
                "payment_amount": target_total_int,
                "final_check_sum": final_int,
                "client_prices": client_prices,
                "submitted": True,
                "url": page.url or "",
                "outcome": meta,
            },
        }
    if outcome == "validation":
        print("[SUP6] submit failed => validation")
        return _step7_fail("SUBMIT_FAILED_VALIDATION", details=meta)
    print("[SUP6] submit failed => timeout")
    return _step7_fail("SUBMIT_TIMEOUT", details=meta)


async def _run_add_items_stage(*, items_override: str = "", order_json_override: str = "") -> dict:
    items = _parse_sup6_items(items_override)
    order_payload = _parse_order_payload(order_json_override)
    state_path = _state_path()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            if _is_state_file_valid(state_path) and not SUP6_FORCE_LOGIN:
                context = await browser.new_context(storage_state=str(state_path))
            else:
                context = await browser.new_context()
            page = await context.new_page()
            login_info = await ensure_logged_in(page, context, state_path, force_login=SUP6_FORCE_LOGIN)
            result = await step3_add_items_to_cart(page, items, order_payload)
            fill_result = None
            delivery_result = None
            payment_result = None
            submit_result = None
            if result.get("ok"):
                fill_result = await step4_fill_recipient_info(page, order_payload)
                if not fill_result.get("ok"):
                    return {
                        "ok": False,
                        "stage": "fill_recipient",
                        "url": page.url or SUP6_CHECKOUT_URL,
                        "storage_state": str(state_path),
                        "details": {"login": login_info, "add_items": result, "fill_recipient": fill_result},
                        "reason": fill_result.get("reason"),
                        "error": str(fill_result.get("reason") or "step4_fill_recipient_info failed"),
                    }
                delivery_result = await step5_fill_delivery_np_pickup(page, order_payload)
                if not delivery_result.get("ok"):
                    return {
                        "ok": False,
                        "stage": "fill_delivery",
                        "url": page.url or SUP6_CHECKOUT_URL,
                        "storage_state": str(state_path),
                        "details": {
                            "login": login_info,
                            "add_items": result,
                            "fill_recipient": fill_result,
                            "fill_delivery": delivery_result,
                        },
                        "reason": delivery_result.get("reason"),
                        "error": str(delivery_result.get("reason") or "step5_fill_delivery_np_pickup failed"),
                    }
                item_map = ((result.get("details") or {}).get("supplier6_item_map") or [])
                payment_result = await step6_fill_payment_and_client_prices(page, order_payload, item_map)
                if not payment_result.get("ok"):
                    return {
                        "ok": False,
                        "stage": "fill_payment",
                        "url": page.url or SUP6_CHECKOUT_URL,
                        "storage_state": str(state_path),
                        "details": {
                            "login": login_info,
                            "add_items": result,
                            "fill_recipient": fill_result,
                            "fill_delivery": delivery_result,
                            "fill_payment": payment_result,
                        },
                        "reason": payment_result.get("reason"),
                        "error": str(payment_result.get("reason") or "step6_fill_payment_and_client_prices failed"),
                    }
                submit_result = await step7_submit_order(page, order_payload, payment_result.get("details") or {})
                if not submit_result.get("ok"):
                    return {
                        "ok": False,
                        "stage": "submit_order",
                        "url": page.url or SUP6_CHECKOUT_URL,
                        "storage_state": str(state_path),
                        "details": {
                            "login": login_info,
                            "add_items": result,
                            "fill_recipient": fill_result,
                            "fill_delivery": delivery_result,
                            "fill_payment": payment_result,
                            "submit_order": submit_result,
                        },
                        "reason": submit_result.get("reason"),
                        "error": str(submit_result.get("reason") or "step7_submit_order failed"),
                    }
            return {
                "stage": "add_items",
                "url": page.url or SUP6_MAKE_ORDER_URL,
                "storage_state": str(state_path),
                "details": {
                    "login": login_info,
                    "add_items": result,
                    "fill_recipient": fill_result,
                    "fill_delivery": delivery_result,
                    "fill_payment": payment_result,
                    "submit_order": submit_result,
                },
                **result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _run_finish_cart_stage(*, pause_seconds: int = 18) -> dict:
    state_path = _state_path()
    if not _is_state_file_valid(state_path):
        return {
            "ok": False,
            "stage": "finish_cart",
            "storage_state": str(state_path),
            "error": f"storage_state is missing or invalid: {state_path}",
        }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()
            await page.goto(SUP6_CART_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
            result = await proceed_from_cart_to_checkout(page)
            if pause_seconds > 0:
                print(f"[SUP6] finish_cart: keep browser open for {pause_seconds}s")
                await page.wait_for_timeout(pause_seconds * 1000)
            return {
                "stage": "finish_cart",
                "url": page.url or SUP6_CART_URL,
                "storage_state": str(state_path),
                "details": {"finish_cart": result},
                **result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _run_fill_recipient_stage(*, pause_seconds: int = 18, order_json_override: str = "") -> dict:
    state_path = _state_path()
    if not _is_state_file_valid(state_path):
        return {
            "ok": False,
            "stage": "fill_recipient",
            "storage_state": str(state_path),
            "error": f"storage_state is missing or invalid: {state_path}",
        }

    order_payload = _parse_order_payload(order_json_override)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()
            await page.goto(SUP6_CHECKOUT_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
            result = await step4_fill_recipient_info(page, order_payload)
            if pause_seconds > 0:
                print(f"[SUP6] fill_recipient: keep browser open for {pause_seconds}s")
                await page.wait_for_timeout(pause_seconds * 1000)
            return {
                "stage": "fill_recipient",
                "url": page.url or SUP6_CHECKOUT_URL,
                "storage_state": str(state_path),
                "details": {"fill_recipient": result},
                **result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _run_fill_delivery_stage(*, pause_seconds: int = 18, order_json_override: str = "") -> dict:
    state_path = _state_path()
    if not _is_state_file_valid(state_path):
        return {
            "ok": False,
            "stage": "fill_delivery",
            "storage_state": str(state_path),
            "error": f"storage_state is missing or invalid: {state_path}",
        }

    order_payload = _parse_order_payload(order_json_override)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()
            await page.goto(SUP6_CHECKOUT_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
            result = await step5_fill_delivery_np_pickup(page, order_payload)
            if pause_seconds > 0:
                print(f"[SUP6] fill_delivery: keep browser open for {pause_seconds}s")
                await page.wait_for_timeout(pause_seconds * 1000)
            return {
                "stage": "fill_delivery",
                "url": page.url or SUP6_CHECKOUT_URL,
                "storage_state": str(state_path),
                "details": {"fill_delivery": result},
                **result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _run_fill_payment_stage(
    *,
    pause_seconds: int = 18,
    order_json_override: str = "",
    supplier6_item_map_override: str = "",
) -> dict:
    state_path = _state_path()
    if not _is_state_file_valid(state_path):
        return {
            "ok": False,
            "stage": "fill_payment",
            "storage_state": str(state_path),
            "error": f"storage_state is missing or invalid: {state_path}",
        }

    order_payload = _parse_order_payload(order_json_override)
    item_map: list[dict] = []
    raw_map = (supplier6_item_map_override or "").strip()
    if raw_map:
        try:
            parsed_map = json.loads(raw_map)
            if isinstance(parsed_map, list):
                item_map = [x for x in parsed_map if isinstance(x, dict)]
        except Exception:
            item_map = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()
            await page.goto(SUP6_CHECKOUT_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
            result = await step6_fill_payment_and_client_prices(page, order_payload, item_map)
            if pause_seconds > 0:
                print(f"[SUP6] fill_payment: keep browser open for {pause_seconds}s")
                await page.wait_for_timeout(pause_seconds * 1000)
            return {
                "stage": "fill_payment",
                "url": page.url or SUP6_CHECKOUT_URL,
                "storage_state": str(state_path),
                "details": {"fill_payment": result},
                **result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _run_submit_stage(
    *,
    pause_seconds: int = 18,
    order_json_override: str = "",
    supplier6_item_map_override: str = "",
) -> dict:
    state_path = _state_path()
    if not _is_state_file_valid(state_path):
        return {
            "ok": False,
            "stage": "submit_order",
            "storage_state": str(state_path),
            "error": f"storage_state is missing or invalid: {state_path}",
        }

    order_payload = _parse_order_payload(order_json_override)
    item_map: list[dict] = []
    raw_map = (supplier6_item_map_override or "").strip()
    if raw_map:
        try:
            parsed_map = json.loads(raw_map)
            if isinstance(parsed_map, list):
                item_map = [x for x in parsed_map if isinstance(x, dict)]
        except Exception:
            item_map = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()
            await page.goto(SUP6_CHECKOUT_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
            payment_result = await step6_fill_payment_and_client_prices(page, order_payload, item_map)
            if not payment_result.get("ok"):
                return {
                    "ok": False,
                    "stage": "fill_payment",
                    "url": page.url or SUP6_CHECKOUT_URL,
                    "storage_state": str(state_path),
                    "details": {"fill_payment": payment_result},
                    "reason": payment_result.get("reason"),
                    "error": str(payment_result.get("reason") or "step6_fill_payment_and_client_prices failed"),
                }
            submit_result = await step7_submit_order(page, order_payload, payment_result.get("details") or {})
            if pause_seconds > 0:
                print(f"[SUP6] submit_order: keep browser open for {pause_seconds}s")
                await page.wait_for_timeout(pause_seconds * 1000)
            return {
                "stage": "submit_order",
                "url": page.url or SUP6_CHECKOUT_URL,
                "storage_state": str(state_path),
                "details": {"fill_payment": payment_result, "submit_order": submit_result},
                **submit_result,
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _run_full_stage(*, items_override: str = "", order_json_override: str = "") -> dict:
    items = _parse_sup6_items(items_override)
    order_payload = _parse_order_payload(order_json_override)
    state_path = _state_path()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=SUP6_HEADLESS)
        context = None
        try:
            if _is_state_file_valid(state_path) and not SUP6_FORCE_LOGIN:
                context = await browser.new_context(storage_state=str(state_path))
            else:
                context = await browser.new_context()
            page = await context.new_page()
            login_info = await ensure_logged_in(page, context, state_path, force_login=SUP6_FORCE_LOGIN)
            clear_result = await clear_cart(page)
            if not clear_result.get("ok"):
                return {
                    "ok": False,
                    "stage": "clear_cart",
                    "url": page.url or SUP6_MAKE_ORDER_URL,
                    "storage_state": str(state_path),
                    "details": {"login": login_info, "clear_cart": clear_result},
                    "error": str(clear_result.get("error") or "clear_cart failed"),
                }
            add_result = await step3_add_items_to_cart(page, items, order_payload)
            if not add_result.get("ok"):
                return {
                    "ok": False,
                    "stage": "add_items",
                    "url": page.url or SUP6_MAKE_ORDER_URL,
                    "storage_state": str(state_path),
                    "details": {"login": login_info, "clear_cart": clear_result, "add_items": add_result},
                    "reason": add_result.get("reason"),
                    "error": str(add_result.get("reason") or "step3_add_items_to_cart failed"),
                }
            fill_result = await step4_fill_recipient_info(page, order_payload)
            if not fill_result.get("ok"):
                return {
                    "ok": False,
                    "stage": "fill_recipient",
                    "url": page.url or SUP6_CHECKOUT_URL,
                    "storage_state": str(state_path),
                    "details": {
                        "login": login_info,
                        "clear_cart": clear_result,
                        "add_items": add_result,
                        "fill_recipient": fill_result,
                    },
                    "reason": fill_result.get("reason"),
                    "error": str(fill_result.get("reason") or "step4_fill_recipient_info failed"),
                }
            delivery_result = await step5_fill_delivery_np_pickup(page, order_payload)
            if not delivery_result.get("ok"):
                return {
                    "ok": False,
                    "stage": "fill_delivery",
                    "url": page.url or SUP6_CHECKOUT_URL,
                    "storage_state": str(state_path),
                    "details": {
                        "login": login_info,
                        "clear_cart": clear_result,
                        "add_items": add_result,
                        "fill_recipient": fill_result,
                        "fill_delivery": delivery_result,
                    },
                    "reason": delivery_result.get("reason"),
                    "error": str(delivery_result.get("reason") or "step5_fill_delivery_np_pickup failed"),
                }
            item_map = ((add_result.get("details") or {}).get("supplier6_item_map") or [])
            payment_result = await step6_fill_payment_and_client_prices(page, order_payload, item_map)
            if not payment_result.get("ok"):
                return {
                    "ok": False,
                    "stage": "fill_payment",
                    "url": page.url or SUP6_CHECKOUT_URL,
                    "storage_state": str(state_path),
                    "details": {
                        "login": login_info,
                        "clear_cart": clear_result,
                        "add_items": add_result,
                        "fill_recipient": fill_result,
                        "fill_delivery": delivery_result,
                        "fill_payment": payment_result,
                    },
                    "reason": payment_result.get("reason"),
                    "error": str(payment_result.get("reason") or "step6_fill_payment_and_client_prices failed"),
                }
            submit_result = await step7_submit_order(page, order_payload, payment_result.get("details") or {})
            if not submit_result.get("ok"):
                return {
                    "ok": False,
                    "stage": "submit_order",
                    "url": page.url or SUP6_CHECKOUT_URL,
                    "storage_state": str(state_path),
                    "details": {
                        "login": login_info,
                        "clear_cart": clear_result,
                        "add_items": add_result,
                        "fill_recipient": fill_result,
                        "fill_delivery": delivery_result,
                        "fill_payment": payment_result,
                        "submit_order": submit_result,
                    },
                    "reason": submit_result.get("reason"),
                    "error": str(submit_result.get("reason") or "step7_submit_order failed"),
                }
            return {
                "ok": True,
                "stage": "run",
                "url": page.url or SUP6_CHECKOUT_URL,
                "storage_state": str(state_path),
                "details": {
                    "login": login_info,
                    "clear_cart": clear_result,
                    "add_items": add_result,
                    "fill_recipient": fill_result,
                    "fill_delivery": delivery_result,
                    "fill_payment": payment_result,
                    "submit_order": submit_result,
                },
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


async def _run() -> dict:
    if SUP6_STAGE == "login":
        return await _run_login_stage()
    if SUP6_STAGE == "clear_cart":
        return await _run_clear_cart_stage()
    if SUP6_STAGE == "add_items":
        return await _run_add_items_stage()
    if SUP6_STAGE in {"finish_cart", "finish-cart"}:
        return await _run_finish_cart_stage()
    if SUP6_STAGE in {"fill_recipient", "fill-recipient"}:
        return await _run_fill_recipient_stage()
    if SUP6_STAGE in {"fill_delivery", "fill-delivery"}:
        return await _run_fill_delivery_stage()
    if SUP6_STAGE in {"fill_payment", "fill-payment"}:
        return await _run_fill_payment_stage()
    if SUP6_STAGE in {"submit_order", "submit-order", "submit"}:
        return await _run_submit_stage()
    if SUP6_STAGE == "run":
        return await _run_full_stage()
    raise RuntimeError(
        f"Unsupported SUP6_STAGE={SUP6_STAGE!r}. Expected 'login', 'clear_cart', 'add_items', "
        "'finish_cart', 'fill_recipient', 'fill_delivery', 'fill_payment', 'submit_order' or 'run'."
    )


async def _amain(
    clear_cart_only: bool = False,
    finish_cart_only: bool = False,
    fill_recipient_only: bool = False,
    fill_delivery_only: bool = False,
    fill_payment_only: bool = False,
    submit_only: bool = False,
    *,
    stage_override: str = "",
    items_override: str = "",
    order_json_override: str = "",
    supplier6_item_map_override: str = "",
) -> int:
    try:
        if stage_override:
            stage = stage_override.strip().lower()
            if stage in {"1", "login"}:
                result = await _run_login_stage()
            elif stage in {"2", "clear_cart", "clear-cart"}:
                result = await _run_clear_cart_stage(pause_seconds=SUP6_CLEAR_CART_PAUSE_SECONDS if clear_cart_only else 0)
            elif stage in {"3", "add_items", "add-items"}:
                result = await _run_add_items_stage(items_override=items_override, order_json_override=order_json_override)
            elif stage in {"finish_cart", "finish-cart", "step3_finish_cart"}:
                result = await _run_finish_cart_stage()
            elif stage in {"4", "fill_recipient", "fill-recipient", "step4_fill_recipient_info"}:
                result = await _run_fill_recipient_stage(order_json_override=order_json_override)
            elif stage in {"5", "fill_delivery", "fill-delivery", "step5_fill_delivery_np_pickup"}:
                result = await _run_fill_delivery_stage(order_json_override=order_json_override)
            elif stage in {"6", "fill_payment", "fill-payment", "step6_fill_payment_and_client_prices"}:
                result = await _run_fill_payment_stage(
                    order_json_override=order_json_override,
                    supplier6_item_map_override=supplier6_item_map_override,
                )
            elif stage in {"7", "submit_order", "submit-order", "submit", "step7_submit_order"}:
                result = await _run_submit_stage(
                    order_json_override=order_json_override,
                    supplier6_item_map_override=supplier6_item_map_override,
                )
            elif stage in {"run"}:
                result = await _run_full_stage(items_override=items_override, order_json_override=order_json_override)
            else:
                raise RuntimeError(f"Unsupported --step value: {stage_override!r}")
        elif submit_only:
            result = await _run_submit_stage(
                order_json_override=order_json_override,
                supplier6_item_map_override=supplier6_item_map_override,
            )
        elif fill_payment_only:
            result = await _run_fill_payment_stage(
                order_json_override=order_json_override,
                supplier6_item_map_override=supplier6_item_map_override,
            )
        elif fill_delivery_only:
            result = await _run_fill_delivery_stage(order_json_override=order_json_override)
        elif fill_recipient_only:
            result = await _run_fill_recipient_stage(order_json_override=order_json_override)
        elif finish_cart_only:
            result = await _run_finish_cart_stage()
        elif clear_cart_only:
            result = await _run_clear_cart_stage(pause_seconds=SUP6_CLEAR_CART_PAUSE_SECONDS)
        else:
            result = await _run()
        stage = str(result.get("stage") or SUP6_STAGE)
        print(f"[SUP6] {stage} {'ok' if result.get('ok') else 'failed'}")
        print(SUPPLIER_RESULT_JSON_PREFIX + json.dumps(result, ensure_ascii=False))
        return 0 if bool(result.get("ok")) else 1
    except PWTimeoutError as e:
        if submit_only:
            stage = "submit_order"
        elif fill_payment_only:
            stage = "fill_payment"
        elif fill_delivery_only:
            stage = "fill_delivery"
        elif fill_recipient_only:
            stage = "fill_recipient"
        elif finish_cart_only:
            stage = "finish_cart"
        elif clear_cart_only:
            stage = "clear_cart"
        else:
            stage = SUP6_STAGE
        print(f"[SUP6] {stage} failed: timeout ({e})")
        print(SUPPLIER_RESULT_JSON_PREFIX + json.dumps({"ok": False, "stage": stage, "error": f"timeout: {e}"}))
        return 1
    except Exception as e:
        if submit_only:
            stage = "submit_order"
        elif fill_payment_only:
            stage = "fill_payment"
        elif fill_delivery_only:
            stage = "fill_delivery"
        elif fill_recipient_only:
            stage = "fill_recipient"
        elif finish_cart_only:
            stage = "finish_cart"
        elif clear_cart_only:
            stage = "clear_cart"
        else:
            stage = SUP6_STAGE
        print(f"[SUP6] {stage} failed: {e}")
        print(SUPPLIER_RESULT_JSON_PREFIX + json.dumps({"ok": False, "stage": stage, "error": str(e)}))
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Supplier6 (proteinplus.pro) runner")
    parser.add_argument("--clear-cart", action="store_true", help="Run clear cart stage and keep browser open for SUP6_CLEAR_CART_PAUSE_SECONDS")
    parser.add_argument("--finish-cart-only", action="store_true", help="Open cart.html from storage_state and finish step 3 (checkout + agreement)")
    parser.add_argument("--fill-recipient-only", action="store_true", help="Open checkout and fill recipient fields for dropshipping from order payload")
    parser.add_argument("--fill-delivery-only", action="store_true", help="Open checkout and fill NP pickup delivery fields from order payload")
    parser.add_argument("--fill-payment-only", action="store_true", help="Open checkout and fill payment type/amount/client prices/order format")
    parser.add_argument("--submit-only", action="store_true", help="Open checkout, run payment rounding/verification and click final Підтвердити")
    parser.add_argument("--step", default="", help="Stage shortcut: 1|2|3|4|5|6|7|login|clear_cart|add_items|finish_cart|fill_recipient|fill_delivery|fill_payment|submit_order|run")
    parser.add_argument("--items", default="", help="Items for add_items stage, format: SKU1:2,SKU2:1")
    parser.add_argument("--order-json", default="", help="Order payload JSON (expects primaryContact with lName/fName/phone)")
    parser.add_argument("--supplier6-item-map-json", default="", help="Optional JSON list with supplier6 item map for step6 standalone test")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            _amain(
                clear_cart_only=args.clear_cart,
                finish_cart_only=args.finish_cart_only,
                fill_recipient_only=args.fill_recipient_only,
                fill_delivery_only=args.fill_delivery_only,
                fill_payment_only=args.fill_payment_only,
                submit_only=args.submit_only,
                stage_override=args.step,
                items_override=args.items,
                order_json_override=args.order_json,
                supplier6_item_map_override=args.supplier6_item_map_json,
            )
        )
    )


if __name__ == "__main__":
    main()
