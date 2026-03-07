import asyncio
import argparse
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
    return s


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

    matched_row = None
    for i in range(min(count, 25)):
        row = row_candidates.nth(i)
        try:
            txt = re.sub(r"\s+", " ", (await row.inner_text(timeout=900)) or "").strip()
            if sku_norm and sku_norm in _norm_sku(txt):
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


async def _step3_add_single_item(page, item: Sup6Item, *, is_last: bool) -> dict | None:
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
                return fail_payload

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
                    return fail_after_click
                await _step3_debug_pause(page, f"after_modal_confirm_sku={sku}")

            showcart = page.locator("a.showcart:visible, a.showcart:has-text('Показати кошик')").first
            await showcart.wait_for(state="visible", timeout=min(8000, SUP6_TIMEOUT_MS))
            if is_last:
                await showcart.click(timeout=min(5000, SUP6_TIMEOUT_MS), force=True)
                try:
                    await page.wait_for_url(re.compile(r"/cart\.html|make-order"), timeout=SUP6_TIMEOUT_MS)
                except Exception:
                    pass
                cart_rows = page.locator("button.vm2-remove_from_cart:visible, .cart-summary tr:has(button.vm2-remove_from_cart), .cart-view tr")
                if await cart_rows.count() <= 0:
                    return _step3_fail(f"CART_VERIFY_FAILED:{sku}", details={"sku": sku})
            else:
                await _step3_close_fancybox(page)
                await _step3_debug_pause(page, f"after_close_fancybox_sku={sku}")

            return None
        except Exception as e:
            print(f"[SUP6] step3_add_items: attempt failed sku={sku} attempt={attempt}: {e}")
            if attempt >= max_attempts:
                return _step3_fail(f"STEP3_ADD_FAILED:{sku}", details={"sku": sku, "error": str(e)})
            await page.wait_for_timeout(220)

    return _step3_fail(f"STEP3_ADD_FAILED:{sku}", details={"sku": sku, "error": "unknown"})


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


async def step3_add_items_to_cart(page, items: list[Sup6Item]) -> dict:
    if not items:
        return _step3_fail("NO_ITEMS", details={"items": 0})

    added = 0
    processed: list[dict] = []
    for idx, item in enumerate(items):
        is_last = idx == len(items) - 1
        fail = await _step3_add_single_item(page, item, is_last=is_last)
        if fail is not None:
            fail_details = dict(fail.get("details") or {})
            fail_details["items_added"] = added
            fail_details["processed"] = processed
            fail["details"] = fail_details
            return fail
        added += 1
        processed.append({"sku": item.sku, "qty": item.qty})
        print(f"[SUP6] step3_add_items: added sku={item.sku} qty={item.qty}")

    # Final verification: ensure each requested SKU is present in cart page text.
    try:
        if "cart" not in (page.url or ""):
            await page.goto(SUP6_CART_URL, wait_until="domcontentloaded", timeout=SUP6_TIMEOUT_MS)
        body_text = re.sub(r"\s+", " ", (await page.inner_text("body")) or "")
        body_norm = _norm_sku(body_text)
        missing = [i.sku for i in items if _norm_sku(i.sku) and _norm_sku(i.sku) not in body_norm]
        if missing:
            return _step3_fail("CART_MISSING_SKU", details={"missing_skus": missing, "items_added": added, "items": processed})
    except Exception as e:
        return _step3_fail("CART_VERIFY_ERROR", details={"error": str(e), "items_added": added, "items": processed})

    finish_result = await proceed_from_cart_to_checkout(page)
    if not finish_result.get("ok"):
        finish_details = dict(finish_result.get("details") or {})
        finish_details["items_added"] = added
        finish_details["items"] = processed
        finish_result["details"] = finish_details
        return finish_result

    return {
        "ok": True,
        "step": "step3_add_items_to_cart",
        "details": {
            "items_added": added,
            "items": processed,
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
) -> tuple[bool, str, dict]:
    last_options: list[str] = []
    for _attempt in range(1, max_attempts + 1):
        opened = await _sumo_open(page, select_id)
        if not opened:
            await page.wait_for_timeout(220)
            continue
        container = await _sumo_container(page, select_id)
        if container is None:
            continue
        search_inputs = container.locator(".optWrapper input, .search-txt input, .search-txt")
        try:
            if await search_inputs.count() > 0:
                search_input = search_inputs.first
                if await search_input.is_visible():
                    await search_input.click(timeout=min(3000, SUP6_TIMEOUT_MS), force=True)
                    await search_input.fill("", timeout=min(3000, SUP6_TIMEOUT_MS))
                    await search_input.type(query, delay=12, timeout=min(5000, SUP6_TIMEOUT_MS))
        except Exception:
            pass

        await page.wait_for_timeout(260)
        options = container.locator(".optWrapper li.opt:not(.disabled):not(.hidden):visible")
        try:
            count = await options.count()
        except Exception:
            count = 0
        candidates: list[tuple[int, str]] = []
        for i in range(min(count, 200)):
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
    return False, "", {"reason": reason_not_found, "query": query, "seen_options": uniq_opts[:25]}


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
    city_norm = _norm_city_name_only(delivery["city"])
    area_norm = _norm_area_region(delivery["region"])

    def _city_matcher(txt: str) -> bool:
        raw = (txt or "").strip()
        if not raw:
            return False
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        left = parts[0] if parts else raw
        name = _norm_city_name_only(left)
        if name != city_norm:
            return False
        if area_norm and len(parts) > 1:
            region_part = _norm_area_region(parts[1])
            if region_part and area_norm not in region_part and region_part not in area_norm:
                return False
        return True

    ok_city, selected_city, city_meta = await _sumo_choose_option(
        page,
        select_id="selectCity",
        query=delivery["city"],
        matcher=_city_matcher,
        reason_not_found="NP_CITY_NOT_FOUND",
    )
    if not ok_city:
        return _step5_fail("NP_CITY_NOT_FOUND", details=city_meta)
    print(f"[SUP6] selected city: {selected_city}")

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
        },
    }


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
            result = await step3_add_items_to_cart(page, items)
            fill_result = None
            delivery_result = None
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
            return {
                "stage": "add_items",
                "url": page.url or SUP6_MAKE_ORDER_URL,
                "storage_state": str(state_path),
                "details": {
                    "login": login_info,
                    "add_items": result,
                    "fill_recipient": fill_result,
                    "fill_delivery": delivery_result,
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
            add_result = await step3_add_items_to_cart(page, items)
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
    if SUP6_STAGE == "run":
        return await _run_full_stage()
    raise RuntimeError(
        f"Unsupported SUP6_STAGE={SUP6_STAGE!r}. Expected 'login', 'clear_cart', 'add_items', "
        "'finish_cart', 'fill_recipient', 'fill_delivery' or 'run'."
    )


async def _amain(
    clear_cart_only: bool = False,
    finish_cart_only: bool = False,
    fill_recipient_only: bool = False,
    fill_delivery_only: bool = False,
    *,
    stage_override: str = "",
    items_override: str = "",
    order_json_override: str = "",
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
            elif stage in {"run"}:
                result = await _run_full_stage(items_override=items_override, order_json_override=order_json_override)
            else:
                raise RuntimeError(f"Unsupported --step value: {stage_override!r}")
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
        if fill_delivery_only:
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
        if fill_delivery_only:
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
    parser.add_argument("--step", default="", help="Stage shortcut: 1|2|3|4|5|login|clear_cart|add_items|finish_cart|fill_recipient|fill_delivery|run")
    parser.add_argument("--items", default="", help="Items for add_items stage, format: SKU1:2,SKU2:1")
    parser.add_argument("--order-json", default="", help="Order payload JSON (expects primaryContact with lName/fName/phone)")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            _amain(
                clear_cart_only=args.clear_cart,
                finish_cart_only=args.finish_cart_only,
                fill_recipient_only=args.fill_recipient_only,
                fill_delivery_only=args.fill_delivery_only,
                stage_override=args.step,
                items_override=args.items,
                order_json_override=args.order_json,
            )
        )
    )


if __name__ == "__main__":
    main()
