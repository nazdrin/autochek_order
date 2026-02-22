# scripts/orchestrator.py
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import math
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

import re

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


FETCH_SCRIPT = ROOT / "scripts" / "salesdrive_fetch_status21.py"
STEP2_3_SCRIPT = ROOT / "scripts" / "step2_3_add_items_to_cart.py"
STEP4_SCRIPT = ROOT / "scripts" / "step4_checkout.py"
STEP5_DROP_TAB_SCRIPT = ROOT / "scripts" / "step5_select_drop_tab.py"
STEP5_CITY_SCRIPT = ROOT / "scripts" / "step5_select_city.py"

STEP5_FILL_NAME_PHONE_SCRIPT = ROOT / "scripts" / "step5_fill_name_phone.py"
STEP6_BRANCH_SCRIPT = ROOT / "scripts" / "step6_select_np_branch.py"
STEP6_TERMINAL_SCRIPT = ROOT / "scripts" / "step6_1_select_np_terminal.py"
STEP7_TTN_SCRIPT = ROOT / "scripts" / "step7_fill_ttn.py"

STEP8_ATTACH_SCRIPT = ROOT / "scripts" / "step8_attach_invoice_file.py"
STEP9_CONFIRM_SCRIPT = ROOT / "scripts" / "step9_confirm_order.py"
SUP2_RUN_ORDER_SCRIPT = ROOT / "scripts" / "supplier2_run_order.py"


POLL_SECONDS = int(os.getenv("ORCH_POLL_SECONDS", "60"))
TIMEOUT_SEC = int(os.getenv("ORCH_STEP_TIMEOUT_SEC", "600"))  # общий fallback таймаут
BIOTUS_TZ = (os.getenv("BIOTUS_TZ") or "Europe/Kyiv").strip() or "Europe/Kyiv"
BIOTUS_PAUSE_DAY = (os.getenv("BIOTUS_PAUSE_DAY") or "").strip()
BIOTUS_PAUSE_NIGHT = (os.getenv("BIOTUS_PAUSE_NIGHT") or "").strip()

# Per-step timeouts (seconds). Can be overridden via env ORCH_TIMEOUT_<STEP_KEY>.
# Example: ORCH_TIMEOUT_STEP6_BRANCH=20
STEP_TIMEOUT_DEFAULTS: Dict[str, int] = {
    "FETCH": int(os.getenv("ORCH_TIMEOUT_FETCH", "60")),
    "STEP2_3": int(os.getenv("ORCH_TIMEOUT_STEP2_3", "90")),
    "STEP4": int(os.getenv("ORCH_TIMEOUT_STEP4", "45")),
    "STEP5_DROP_TAB": int(os.getenv("ORCH_TIMEOUT_STEP5_DROP_TAB", "25")),
    "STEP5_CITY": int(os.getenv("ORCH_TIMEOUT_STEP5_CITY", "35")),
    "STEP5_FILL_NAME_PHONE": int(os.getenv("ORCH_TIMEOUT_STEP5_FILL_NAME_PHONE", "30")),
    "STEP6_BRANCH": int(os.getenv("ORCH_TIMEOUT_STEP6_BRANCH", "20")),
    "STEP6_TERMINAL": int(os.getenv("ORCH_TIMEOUT_STEP6_TERMINAL", "25")),
    "STEP7_TTN": int(os.getenv("ORCH_TIMEOUT_STEP7_TTN", "25")),
    "STEP8_ATTACH": int(os.getenv("ORCH_TIMEOUT_STEP8_ATTACH", "75")),
    "STEP9_CONFIRM": int(os.getenv("ORCH_TIMEOUT_STEP9_CONFIRM", "45")),
    "SUP2_RUN_ORDER": int(os.getenv("ORCH_TIMEOUT_SUP2_RUN_ORDER", "180")),
    "SALESDRIVE_UPDATE": int(os.getenv("ORCH_TIMEOUT_SALESDRIVE_UPDATE", "30")),
}

def _timeout_for_step(step_key: str) -> int:
    """Return timeout for a logical step key, with TIMEOUT_SEC as fallback."""
    try:
        v = STEP_TIMEOUT_DEFAULTS.get(step_key)
        if isinstance(v, int) and v > 0:
            return v
    except Exception:
        pass
    return TIMEOUT_SEC


def parse_windows(spec: str) -> list[tuple[dtime, dtime]]:
    spec = (spec or "").strip()
    if not spec:
        return []

    windows: list[tuple[dtime, dtime]] = []
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError(f"Invalid pause window '{part}': expected HH:MM-HH:MM")
        start_s, end_s = (x.strip() for x in part.split("-", 1))
        try:
            start_t = dtime.fromisoformat(start_s)
            end_t = dtime.fromisoformat(end_s)
        except ValueError as e:
            raise ValueError(f"Invalid pause window '{part}': expected HH:MM-HH:MM") from e
        windows.append((start_t, end_t))
    return windows


def in_window(now: datetime, windows: list[tuple[dtime, dtime]]) -> bool:
    now_t = now.time()
    for start_t, end_t in windows:
        if start_t <= end_t:
            if start_t <= now_t < end_t:
                return True
        else:
            # crosses midnight, e.g. 23:00-07:00
            if now_t >= start_t or now_t < end_t:
                return True
    return False


def seconds_until_window_end(now: datetime, windows: list[tuple[dtime, dtime]]) -> int:
    now_t = now.time()
    candidates: list[int] = []
    for start_t, end_t in windows:
        is_active = False
        if start_t <= end_t:
            is_active = start_t <= now_t < end_t
            if is_active:
                end_dt = datetime.combine(now.date(), end_t, tzinfo=now.tzinfo)
        else:
            is_active = now_t >= start_t or now_t < end_t
            if is_active:
                if now_t >= start_t:
                    end_dt = datetime.combine(now.date() + timedelta(days=1), end_t, tzinfo=now.tzinfo)
                else:
                    end_dt = datetime.combine(now.date(), end_t, tzinfo=now.tzinfo)
        if is_active:
            delta_sec = math.ceil((end_dt - now).total_seconds())
            candidates.append(max(0, delta_sec))
    if not candidates:
        return 0
    return min(candidates)

STATE_FILE = Path(os.getenv("ORCH_STATE_FILE", str(ROOT / ".orch_state.json")))
MAX_PROCESSED_IDS = int(os.getenv("ORCH_MAX_PROCESSED_IDS", "200"))

# New config constants
BATCH_SIZE = int(os.getenv("ORCH_BATCH_SIZE", "5"))
BACKOFF_BASE_SEC = int(os.getenv("ORCH_BACKOFF_BASE_SEC", "60"))
BACKOFF_MAX_SEC = int(os.getenv("ORCH_BACKOFF_MAX_SEC", "3600"))

# New orchestrator failure control constants
ORCH_MAX_ATTEMPTS = int(os.getenv("ORCH_MAX_ATTEMPTS", "3"))
ORCH_FAIL_STATUS_ID = int(os.getenv("ORCH_FAIL_STATUS_ID", "20"))
ORCH_FAIL_MARK = (os.getenv("ORCH_FAIL_MARK") or "🟥").strip() or "🟥"




# Optional defaults for downstream scripts
BIOTUS_HEADLESS = (os.getenv("BIOTUS_HEADLESS") or "0").strip() == "1"

# If headless is requested, default to non-CDP unless explicitly overridden.
DEFAULT_BIOTUS_USE_CDP = os.getenv("BIOTUS_USE_CDP")
if not DEFAULT_BIOTUS_USE_CDP:
    DEFAULT_BIOTUS_USE_CDP = "0" if BIOTUS_HEADLESS else "1"

ORCH_DONE_STATUS_ID = int(os.getenv("ORCH_DONE_STATUS_ID", "4"))
SALESDRIVE_BASE_URL = (os.getenv("SALESDRIVE_BASE_URL") or "https://petrenko.salesdrive.me").rstrip("/")
SALESDRIVE_API_KEY = (os.getenv("SALESDRIVE_API_KEY") or "").strip()
ORCH_HEADLESS = (os.getenv("ORCH_HEADLESS") or "").strip()
ORCH_SUP2_HEADLESS = (os.getenv("ORCH_SUP2_HEADLESS") or ORCH_HEADLESS or "1").strip()
ORCH_SUP2_STORAGE_STATE_FILE = (os.getenv("ORCH_SUP2_STORAGE_STATE_FILE") or ".state_supplier2.json").strip()
ORCH_SUP2_CLEAR_BASKET = (os.getenv("ORCH_SUP2_CLEAR_BASKET") or "1").strip()


def build_full_name(order: Dict[str, Any]) -> str:
    pc = order.get("primaryContact") or {}
    l = (pc.get("lName") or "").strip()
    f = (pc.get("fName") or "").strip()
    # We prefer "Last First" to match your earlier tests
    full = " ".join([x for x in [l, f] if x]).strip()
    return full


def format_phone_local(order: Dict[str, Any]) -> str:
    pc = order.get("primaryContact") or {}
    phones = pc.get("phone") or []
    raw = ""
    if isinstance(phones, list) and phones:
        raw = str(phones[0] or "")
    else:
        raw = str(pc.get("phone") or "")

    # keep digits only
    digits = "".join(ch for ch in raw if ch.isdigit())

    # strip leading country code 380 if present
    if digits.startswith("380") and len(digits) >= 12:
        digits = digits[3:]

    # now we expect UA mobile 10 digits: XX XXX XX XX
    if len(digits) >= 10:
        digits = digits[-10:]
        return f"{digits[0:2]} {digits[2:5]} {digits[5:7]} {digits[7:9]} {digits[9:10]}".replace("  ", " ").strip()

    return digits


def notify_stub(message: str) -> None:
    """Send notification to Telegram if TG_BOT_TOKEN and TG_CHAT_ID are set; always prints to stderr."""
    print(f"[NOTIFY] {message}", file=sys.stderr)

    token = (os.getenv("TG_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TG_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            _ = resp.read()
    except Exception as e:
        # Don't crash orchestrator due to notify issues
        print(f"[NOTIFY] Telegram send failed: {e}", file=sys.stderr)


def short_reason(step_name: str, rc: int | None, out: str, err: str, exc: Exception | None = None) -> str:
    """Build a short human-readable reason for notifications."""
    if exc is not None:
        return f"{step_name}: {type(exc).__name__}: {str(exc).strip()}"

    # Prefer stderr last non-empty line
    src = (err or "").strip().splitlines()
    if src:
        last = src[-1].strip()
        if last:
            return f"{step_name}: {last}"

    src2 = (out or "").strip().splitlines()
    if src2:
        last2 = src2[-1].strip()
        if last2:
            return f"{step_name}: {last2}"

    if rc is not None:
        return f"{step_name}: failed rc={rc}"
    return f"{step_name}: failed"


class StepError(RuntimeError):
    def __init__(self, step: str, reason: str):
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason

# --- SalesDrive integration ---
def salesdrive_update_status(
    order_id: int,
    status_id: int,
    comment: str | None = None,
    number_sup: str | None = None,
) -> None:
    """Update order status in SalesDrive using /api/order/update/. Raises on failure.
    Optionally supports a comment (short, max 800 chars)."""
    if not SALESDRIVE_API_KEY:
        raise RuntimeError("SALESDRIVE_API_KEY is not set")

    url = f"{SALESDRIVE_BASE_URL}/api/order/update/"
    data_obj: Dict[str, Any] = {"statusId": int(status_id)}
    if comment:
        # SalesDrive commonly supports "comment"; keep it short.
        data_obj["comment"] = str(comment)[:800]
    if number_sup and str(number_sup).strip():
        data_obj["numberSup"] = str(number_sup).strip()
    payload = {
        "id": int(order_id),
        "data": data_obj,
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("accept", "application/json")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Api-Key", SALESDRIVE_API_KEY)

    try:
        with urllib.request.urlopen(req, timeout=_timeout_for_step("SALESDRIVE_UPDATE")) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(f"SalesDrive update failed HTTP {resp.status}: {body[:300]}")
            # salesdrive often returns json; we don't require specific fields now
    except Exception as e:
        raise RuntimeError(f"SalesDrive update failed: {e}")


def run_python(script: Path, env: Dict[str, str], timeout_sec: int, args: List[str] | None = None) -> Tuple[int, str, str]:
    cmd = [sys.executable, "-u", str(script)]
    if args:
        cmd += args
    p = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    return p.returncode, p.stdout, p.stderr


def parse_orders_from_fetch_output(stdout: str) -> List[Dict[str, Any]]:
    """
    Ожидаем, что salesdrive_fetch_status21.py печатает JSON.
    Поддерживаем:
      - весь объект {"data":[...]}
      - или напрямую список [...]
    """
    stdout = stdout.strip()
    if not stdout:
        return []

    # иногда в stdout могут быть посторонние строки; пытаемся вытащить JSON "с конца"
    # 1) попробуем как есть
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        # 2) попробуем найти первую "{" или "[" и парсить оттуда
        cut_idx = None
        for i, ch in enumerate(stdout):
            if ch in "{[":
                cut_idx = i
                break
        if cut_idx is None:
            return []
        obj = json.loads(stdout[cut_idx:])

    if isinstance(obj, dict):
        data = obj.get("data") or []
        return data if isinstance(data, list) else []
    if isinstance(obj, list):
        return obj
    return []


def parse_json_from_stdout(stdout: str) -> Any:
    stdout = (stdout or "").strip()
    if not stdout:
        raise ValueError("stdout is empty")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        cut_idx = None
        for i, ch in enumerate(stdout):
            if ch in "{[":
                cut_idx = i
                break
        if cut_idx is None:
            raise
        return json.loads(stdout[cut_idx:])


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"processed_ids": [], "failed": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"processed_ids": [], "failed": {}}
        ids = data.get("processed_ids")
        if not isinstance(ids, list):
            ids = []
        # ensure ints where possible
        norm_ids: List[int] = []
        for x in ids:
            try:
                norm_ids.append(int(x))
            except Exception:
                continue
        data["processed_ids"] = norm_ids
        failed = data.get("failed")
        if not isinstance(failed, dict):
            failed = {}
        data["failed"] = failed
        return data
    except Exception:
        return {"processed_ids": [], "failed": {}}
def _now_ts() -> int:
    return int(time.time())


def _backoff_seconds(fail_count: int) -> int:
    # 1st failure => base, 2nd => 2x, 3rd => 4x ... capped
    sec = BACKOFF_BASE_SEC * (2 ** max(0, fail_count - 1))
    return min(sec, BACKOFF_MAX_SEC)


def mark_failed(state: Dict[str, Any], order_id: int, step: str, reason: str) -> None:
    failed: Dict[str, Any] = state.setdefault("failed", {})
    key = str(order_id)
    entry = failed.get(key) or {}
    try:
        cnt = int(entry.get("count") or 0)
    except Exception:
        cnt = 0
    cnt += 1
    terminal = False
    if ORCH_MAX_ATTEMPTS > 0 and cnt >= ORCH_MAX_ATTEMPTS:
        terminal = True
    wait_sec = _backoff_seconds(cnt)
    # If terminal, disable future retries by setting next_ts far in the future
    next_ts = _now_ts() + wait_sec
    if terminal:
        next_ts = _now_ts() + 10 * 365 * 24 * 3600  # ~10 years
    entry.update(
        {
            "count": cnt,
            "next_ts": next_ts,
            "last_step": step,
            "last_error": reason[:500],
            "updated_at": _now_ts(),
            "terminal": terminal,
        }
    )
    failed[key] = entry


# Helpers for terminal/attempts
def is_terminal_failed(state: Dict[str, Any], order_id: int) -> bool:
    failed = state.get("failed")
    if not isinstance(failed, dict):
        return False
    entry = failed.get(str(order_id))
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("terminal"))

def get_fail_count(state: Dict[str, Any], order_id: int) -> int:
    failed = state.get("failed")
    if not isinstance(failed, dict):
        return 0
    entry = failed.get(str(order_id))
    if not isinstance(entry, dict):
        return 0
    try:
        return int(entry.get("count") or 0)
    except Exception:
        return 0


def clear_failed(state: Dict[str, Any], order_id: int) -> None:
    failed = state.get("failed")
    if isinstance(failed, dict):
        failed.pop(str(order_id), None)


def is_backoff_active(state: Dict[str, Any], order_id: int) -> Tuple[bool, int]:
    failed = state.get("failed")
    if not isinstance(failed, dict):
        return False, 0
    entry = failed.get(str(order_id))
    if not isinstance(entry, dict):
        return False, 0
    try:
        next_ts = int(entry.get("next_ts") or 0)
    except Exception:
        next_ts = 0
    now = _now_ts()
    return (next_ts > now), max(0, next_ts - now)


def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        notify_stub(f"Failed to save state file {STATE_FILE}: {e}")


def build_biotus_items(order: Dict[str, Any]) -> str:
    products = order.get("products") or []
    items: List[str] = []

    for p in products:
        desc = (p.get("description") or "").strip()
        amount = p.get("amount")

        if not desc:
            continue

        # SKU = первый токен до запятой
        sku = desc.split(",", 1)[0].strip()
        if not sku:
            continue

        try:
            qty = int(amount)
        except Exception:
            qty = 1

        items.append(f"{sku}={qty}")

    return ";".join(items)


def build_sup2_items(order: Dict[str, Any]) -> str:
    products = order.get("products") or []
    items: List[str] = []

    for p in products:
        desc = str(p.get("description") or "").strip()
        sku = desc.split(",", 1)[0].strip() if desc else ""
        if not sku:
            continue

        amount = p.get("amount")
        try:
            qty = int(amount)
        except Exception:
            qty = 1
        if qty < 1:
            qty = 1

        items.append(f"{sku}:{qty}")

    return ",".join(items)


def get_first_delivery_block(order: Dict[str, Any]) -> Dict[str, Any]:
    odd = order.get("ord_delivery_data") or []
    if isinstance(odd, list) and odd:
        first = odd[0]
        return first if isinstance(first, dict) else {}
    if isinstance(odd, dict):
        return odd
    return {}


def extract_city_env(order: Dict[str, Any]) -> Dict[str, str]:
    d = get_first_delivery_block(order)
    city_name = (d.get("cityName") or "").strip()
    area_name = (d.get("areaName") or "").strip()
    region_name = (d.get("regionName") or "").strip()
    city_type = (d.get("cityType") or "").strip()

    env: Dict[str, str] = {}
    if city_name:
        env["BIOTUS_CITY_NAME"] = city_name
    if area_name:
        env["BIOTUS_CITY_AREA"] = area_name
    if region_name is not None:
        # allow empty string explicitly as some flows expect it
        env["BIOTUS_CITY_REGION"] = region_name
    if city_type:
        env["BIOTUS_CITY_TYPE"] = city_type
    return env


def extract_delivery_info(order: Dict[str, Any]) -> Tuple[str, str]:
    """Return (address, branch_number_str). branch_number_str may be '' when missing."""
    d = get_first_delivery_block(order)
    address = (d.get("address") or "").strip()
    bn = d.get("branchNumber")
    bn_str = ""
    if bn is not None:
        try:
            bn_str = str(int(bn))
        except Exception:
            bn_str = str(bn).strip()
    return address, bn_str


def extract_tracking_number(order: Dict[str, Any]) -> str:
    d = get_first_delivery_block(order)
    return (d.get("trackingNumber") or "").strip()


# Extract shipping_address from order
def extract_shipping_address(order: Dict[str, Any]) -> str:
    return (order.get("shipping_address") or "").strip()


# --- Helper: normalize spaces ---
def _normalize_spaces(s: str) -> str:
    """Collapse all whitespace to single spaces and strip."""
    return re.sub(r'\s+', ' ', s or '').strip()


# --- Helper: build branch query from shipping/address info ---
def build_branch_query_from_shipping(shipping_address: str, fallback_address: str) -> str:
    """
    Build a SHORT query string suitable for Biotus NP dropdown search.

    Inputs:
      - shipping_address: order.shipping_address (often includes full label + limits + address)
      - fallback_address: ord_delivery_data[0].address

    Rules:
      - For "поштомат" we search by number whenever possible.
      - For "відділення" we search by "Відділення №N" whenever possible.
      - For "пункт":
          * if number exists -> "Пункт №N"
          * if NO number -> use the address tail after ':' (e.g. "вул. Сонячна 105а")
      - Always drop weight limits like "(до 30 кг)" from the query.
    """
    src = (shipping_address or "").strip() or (fallback_address or "").strip()
    if not src:
        return ""

    s = _normalize_spaces(src)
    s_lower = s.casefold()

    # Remove common service suffixes like "(до 30 кг)"
    s = re.sub(r"\(\s*до\s*\d+\s*кг\s*\)", "", s, flags=re.IGNORECASE)
    s = _normalize_spaces(s)
    s_lower = s.casefold()

    def _normalize_no_markers(text: str) -> str:
        # Normalize various "number" markers to a single "№"
        t = text
        t = re.sub(r"(?<!\w)N\s*[º°]\s*", "№", t, flags=re.IGNORECASE)
        t = re.sub(r"(?<!\w)N\s*[oо]\s*", "№", t, flags=re.IGNORECASE)
        t = re.sub(r"№\s*", "№", t)
        return t

    def extract_number(ss: str) -> str | None:
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

    if os.environ.get("ORCH_DEBUG") == "1":
        dbg_num = extract_number(s)
        print(f"[ORCH_DEBUG] build_branch_query_from_shipping: src='{src}' num='{dbg_num}'")

    def after_colon_tail(ss: str) -> str:
        # If we have "...: address" return only address part.
        if ":" in ss:
            tail = ss.split(":", 1)[1]
            return _normalize_spaces(tail)
        return ""

    # --- Postomat ---
    if "поштомат" in s_lower:
        num = extract_number(s)
        if num:
            return _normalize_spaces(num)
        # fallback: try address tail
        tail = after_colon_tail(s)
        return tail or _normalize_spaces(s)

    # --- Pickup point / пункт приймання-видачі (special case) ---
    if "пункт приймання-видачі" in s_lower and ":" in s:
        left, right = s.split(":", 1)
        left = _normalize_spaces(left)
        right = _normalize_spaces(right)
        # remove weight brackets like "(до 30 кг)"
        right = re.sub(r"\s*\(до [^)]+\)\s*", " ", right, flags=re.IGNORECASE).strip()
        m = re.search(r"№\s*(\d+)", left)
        if m:
            return _normalize_spaces(f"Пункт приймання-видачі №{m.group(1)}: {right}")
        return _normalize_spaces(f"Пункт приймання-видачі: {right}")

    # --- Pickup point / пункт ---
    # IMPORTANT:
    # We only build the specific form "Пункт приймання-видачі №N" when the source text
    # actually contains "Пункт приймання-видачі". For plain "Пункт №N" we keep it as is.
    if "пункт" in s_lower:
        has_pryimannya = "пункт приймання-видачі" in s_lower
        num = extract_number(s)

        # Case A: "Пункт приймання-видачі №N ..." -> search by the same prefix + number
        if has_pryimannya and num:
            return _normalize_spaces(f"Пункт приймання-видачі №{num}")

        # Case B: plain "Пункт №N ..." -> keep "Пункт №N" (do NOT switch to приймання-видачі)
        if (not has_pryimannya) and num:
            return _normalize_spaces(f"Пункт №{num}")

        # Case C: no number -> search by address tail after ':' if present (keep as-is)
        tail = after_colon_tail(s)
        if tail:
            return tail

        # fallback: remove leading service words and search by remaining text
        s2 = re.sub(r"\(\s*до\s*\d+\s*кг\s*\)", "", s, flags=re.IGNORECASE)
        if has_pryimannya:
            s2 = re.sub(r"^\s*Пункт\s+приймання\-видачі\s*", "", s2, flags=re.IGNORECASE)
        else:
            s2 = re.sub(r"^\s*Пункт\s*", "", s2, flags=re.IGNORECASE)
        return _normalize_spaces(s2) or _normalize_spaces(s)

    # --- Warehouse отделение ---
    if "відділен" in s_lower:
        num = extract_number(s)
        if num:
            return _normalize_spaces(f"Відділення №{num}")
        # if no number, try address tail
        tail = after_colon_tail(s)
        return tail or _normalize_spaces("Відділення")

    # --- Unknown: try number, else address tail, else full ---
    num = extract_number(s)
    if num:
        return _normalize_spaces(num)
    tail = after_colon_tail(s)
    return tail or _normalize_spaces(s)


# --- Helper: detect branch kind (punkt or viddilennya) ---
def detect_branch_kind(shipping_address: str, fallback_address: str) -> str:
    """Return 'punkt' or 'viddilennya' based on text. Default to 'viddilennya'."""
    src = (shipping_address or "").strip() or (fallback_address or "").strip()
    s = (src or "").casefold()
    if "пункт" in s:
        return "punkt"
    return "viddilennya"


def choose_np_step(address: str, branch_number: str, shipping_address: str) -> Tuple[str, Path, str, str]:
    """
    Returns: (step_name, script_path, env_key, env_value)

    Detection of delivery kind:
      - Uses `address` from ord_delivery_data[0].address for kind detection (it contains "поштомат"/"відділення"/"пункт").

    What we pass into scripts:
      - If it's a postomat ("поштомат" in address) -> terminal script and BIOTUS_TERMINAL_QUERY.
        Prefer passing the numeric branch number (e.g. 48437) because the UI search works by number.
      - Otherwise -> branch script and BIOTUS_BRANCH_QUERY.
        Pass a short query (e.g. "Відділення №2", "Пункт №964", or "48437") extracted from shipping_address or address,
        using build_branch_query_from_shipping.
    """
    a_norm = (address or "").casefold()
    if "поштомат" in a_norm:
        value = (branch_number or "").strip() or (address or "").strip()
        return ("step6_1_select_np_terminal", STEP6_TERMINAL_SCRIPT, "BIOTUS_TERMINAL_QUERY", value)

    query = build_branch_query_from_shipping(shipping_address, address)
    return ("step6_select_np_branch", STEP6_BRANCH_SCRIPT, "BIOTUS_BRANCH_QUERY", query)


def process_one_biotus_order(order: Dict[str, Any]) -> None:
    order_id = order.get("id")
    try:
        order_id_int = int(order_id)
    except Exception:
        raise RuntimeError(f"Invalid order id: {order_id}")
    biotus_items = build_biotus_items(order)
    if not biotus_items:
        raise RuntimeError("Не смог сформировать BIOTUS_ITEMS из order['products'].")

    full_name = build_full_name(order)
    phone_local = format_phone_local(order)

    env = os.environ.copy()
    env.setdefault("BIOTUS_USE_CDP", DEFAULT_BIOTUS_USE_CDP)
    env["BIOTUS_ITEMS"] = biotus_items

    if full_name:
        env["BIOTUS_FULL_NAME"] = full_name
    if phone_local:
        env["BIOTUS_PHONE_LOCAL"] = phone_local

    delivery_address, delivery_branch_number = extract_delivery_info(order)
    shipping_address = extract_shipping_address(order)
    tracking_number = extract_tracking_number(order)
    if tracking_number:
        env["BIOTUS_TTN"] = tracking_number
        print(f"[ORCH] TTN => {tracking_number}")
    if not delivery_address:
        raise RuntimeError("Не найдено поле ord_delivery_data[0].address для выбора отделения/поштомата.")

    step6_name, step6_script, step6_env_key, step6_env_val = choose_np_step(
        delivery_address, delivery_branch_number, shipping_address
    )

    # Help step6_select_np_branch.py choose correct matching logic for "Пункт"
    if step6_env_key == "BIOTUS_BRANCH_QUERY":
        env["BIOTUS_BRANCH_KIND"] = detect_branch_kind(shipping_address, delivery_address)

    city_env = extract_city_env(order)
    for k, v in city_env.items():
        env[k] = v

    if city_env:
        print(
            f"[ORCH] City => name={city_env.get('BIOTUS_CITY_NAME','')} area={city_env.get('BIOTUS_CITY_AREA','')} region={city_env.get('BIOTUS_CITY_REGION','')} type={city_env.get('BIOTUS_CITY_TYPE','')}"
        )

    env[step6_env_key] = step6_env_val
    print(f"[ORCH] Delivery address => {delivery_address}")
    if shipping_address:
        print(f"[ORCH] Shipping address => {shipping_address}")
    if delivery_branch_number:
        print(f"[ORCH] Delivery branchNumber => {delivery_branch_number}")
    extra_kind = ""
    if step6_env_key == "BIOTUS_BRANCH_QUERY":
        extra_kind = f", BIOTUS_BRANCH_KIND='{env.get('BIOTUS_BRANCH_KIND','')}'"
    print(f"[ORCH] Step6 => {step6_name} ({step6_env_key}='{step6_env_val}'{extra_kind})")

    print(f"[ORCH] Using BIOTUS_ITEMS: {biotus_items}")

    steps: List[Tuple[str, str, Path]] = [
        ("STEP2_3", "step2_3_add_items_to_cart", STEP2_3_SCRIPT),
        ("STEP4", "step4_checkout", STEP4_SCRIPT),
        ("STEP5_DROP_TAB", "step5_select_drop_tab", STEP5_DROP_TAB_SCRIPT),
        ("STEP5_CITY", "step5_select_city", STEP5_CITY_SCRIPT),
        ("STEP5_FILL_NAME_PHONE", "step5_fill_name_phone", STEP5_FILL_NAME_PHONE_SCRIPT),
        ("STEP6_TERMINAL" if step6_name == "step6_1_select_np_terminal" else "STEP6_BRANCH", step6_name, step6_script),
        ("STEP7_TTN", "step7_fill_ttn", STEP7_TTN_SCRIPT),
        ("STEP8_ATTACH", "step8_attach_invoice_file", STEP8_ATTACH_SCRIPT),
        ("STEP9_CONFIRM", "step9_confirm_order", STEP9_CONFIRM_SCRIPT),
    ]

    current_step = None
    current_key = None
    biotus_order_number = ""
    for step_key, step_name, script in steps:
        current_step = step_name
        current_key = step_key
        step_timeout = _timeout_for_step(step_key)
        try:
            rc, out, err = run_python(script, env=env, timeout_sec=step_timeout)
        except subprocess.TimeoutExpired as te:
            raise StepError(step_name, f"timeout after {step_timeout}s") from te

        if out.strip():
            print(out, end="" if out.endswith("\n") else "\n")
        if err.strip():
            print(err, file=sys.stderr, end="" if err.endswith("\n") else "\n")

        if rc != 0:
            reason = short_reason(step_name, rc, out, err, None)
            raise StepError(step_name, reason)

        if step_name == "step9_confirm_order":
            try:
                payload = parse_json_from_stdout(out)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                biotus_order_number = str(payload.get("order_number") or "").strip()
                if not biotus_order_number:
                    warning = str(payload.get("warning") or "order number not found").strip()
                    print(f"[ORCH] WARN step9_confirm_order: {warning}")

    # If all steps succeeded, update SalesDrive order status
    if biotus_order_number:
        salesdrive_update_status(order_id_int, ORCH_DONE_STATUS_ID, number_sup=biotus_order_number)
        print(
            f"[ORCH] SalesDrive status updated: order_id={order_id_int} -> statusId={ORCH_DONE_STATUS_ID}, numberSup={biotus_order_number}"
        )
    else:
        salesdrive_update_status(order_id_int, ORCH_DONE_STATUS_ID)
        print(f"[ORCH] SalesDrive status updated: order_id={order_id_int} -> statusId={ORCH_DONE_STATUS_ID}")


def process_one_dobavki_order(order: Dict[str, Any]) -> None:
    order_id = order.get("id")
    try:
        order_id_int = int(order_id)
    except Exception:
        raise RuntimeError(f"Invalid order id: {order_id}")

    sup2_items = build_sup2_items(order)
    if not sup2_items:
        raise RuntimeError("Не смог сформировать SUP2_ITEMS из order['products'].")

    tracking_number = extract_tracking_number(order)
    if not tracking_number:
        raise RuntimeError("Не найдено поле ord_delivery_data[0].trackingNumber для SUP2_TTN.")

    if not ORCH_SUP2_STORAGE_STATE_FILE:
        raise RuntimeError("ORCH_SUP2_STORAGE_STATE_FILE is empty.")

    env = os.environ.copy()
    env.setdefault("SUP2_HEADLESS", ORCH_SUP2_HEADLESS)
    env["SUP2_STORAGE_STATE_FILE"] = ORCH_SUP2_STORAGE_STATE_FILE
    env["SUP2_ITEMS"] = sup2_items
    env["SUP2_TTN"] = tracking_number
    env["SUP2_CLEAR_BASKET"] = ORCH_SUP2_CLEAR_BASKET
    if "SUP2_DEBUG_PAUSE_SECONDS" in os.environ:
        env["SUP2_DEBUG_PAUSE_SECONDS"] = os.environ["SUP2_DEBUG_PAUSE_SECONDS"]

    print(f"[ORCH] Dobavki SUP2_ITEMS => {sup2_items}")
    print(f"[ORCH] Dobavki TTN => {tracking_number}")

    step_name = "supplier2_run_order"
    step_timeout = _timeout_for_step("SUP2_RUN_ORDER")
    try:
        rc, out, err = run_python(SUP2_RUN_ORDER_SCRIPT, env=env, timeout_sec=step_timeout)
    except subprocess.TimeoutExpired as te:
        raise StepError(step_name, f"timeout after {step_timeout}s") from te

    if out.strip():
        print(out, end="" if out.endswith("\n") else "\n")
    if err.strip():
        print(err, file=sys.stderr, end="" if err.endswith("\n") else "\n")

    payload: Dict[str, Any] | None = None
    try:
        parsed = parse_json_from_stdout(out)
        if isinstance(parsed, dict):
            payload = parsed
    except Exception:
        payload = None

    if rc != 0:
        if payload:
            stage = str(payload.get("stage") or step_name)
            msg = str(payload.get("error") or f"rc={rc}")
            raise StepError(stage, msg)
        reason = short_reason(step_name, rc, out, err, None)
        raise StepError(step_name, reason)

    if not payload:
        raise StepError(step_name, "No JSON object in supplier2_run_order stdout.")
    if not bool(payload.get("ok")):
        stage = str(payload.get("stage") or step_name)
        msg = str(payload.get("error") or "supplier2_run_order returned ok=false")
        raise StepError(stage, msg)

    supplier_order_number = str(payload.get("supplier_order_number") or "").strip()
    if not supplier_order_number:
        raise StepError(step_name, "supplier_order_number is empty in supplier2_run_order response.")

    salesdrive_update_status(order_id_int, ORCH_DONE_STATUS_ID, number_sup=supplier_order_number)
    print(
        f"[ORCH] SalesDrive status updated: order_id={order_id_int} -> statusId={ORCH_DONE_STATUS_ID}, numberSup={supplier_order_number}"
    )


def process_one_order(order: Dict[str, Any]) -> None:
    supplier = str(order.get("supplier") or "").strip()
    supplier_norm = supplier.casefold()

    if supplier_norm == "dobavki.ua":
        process_one_dobavki_order(order)
        return

    if supplier_norm == "biotus":
        process_one_biotus_order(order)
        return

    raise RuntimeError(f"Unsupported supplier '{supplier}'. Expected 'Biotus' or 'Dobavki.ua'.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Сделать один цикл и выйти (для теста)")
    ap.add_argument("--dry-run", action="store_true", help="Не запускать step2_3, только вывести BIOTUS_ITEMS")
    args = ap.parse_args()

    if not FETCH_SCRIPT.exists():
        print(f"Fetch script not found: {FETCH_SCRIPT}", file=sys.stderr)
        return 2
    try:
        tz = ZoneInfo(BIOTUS_TZ)
    except Exception as e:
        print(f"Invalid BIOTUS_TZ '{BIOTUS_TZ}': {e}", file=sys.stderr)
        return 2

    try:
        pause_day_windows = parse_windows(BIOTUS_PAUSE_DAY)
        pause_night_windows = parse_windows(BIOTUS_PAUSE_NIGHT)
    except ValueError as e:
        print(f"Invalid pause window spec: {e}", file=sys.stderr)
        return 2

    required = [
        ("Supplier2 run order script", SUP2_RUN_ORDER_SCRIPT),
        ("Step2_3 script", STEP2_3_SCRIPT),
        ("Step4 script", STEP4_SCRIPT),
        ("Step5 drop tab script", STEP5_DROP_TAB_SCRIPT),
        ("Step5 city script", STEP5_CITY_SCRIPT),
        ("Step5 fill name/phone script", STEP5_FILL_NAME_PHONE_SCRIPT),
        ("Step6 branch script", STEP6_BRANCH_SCRIPT),
        ("Step6 terminal script", STEP6_TERMINAL_SCRIPT),
        ("Step7 ttn script", STEP7_TTN_SCRIPT),
        ("Step8 attach invoice script", STEP8_ATTACH_SCRIPT),
        ("Step9 confirm order script", STEP9_CONFIRM_SCRIPT),
    ]
    for label, p in required:
        if not p.exists():
            print(f"{label} not found: {p}", file=sys.stderr)
            return 2

    state = load_state()
    state.setdefault("failed", {})
    processed_ids: List[int] = state.get("processed_ids", [])

    while True:
        now = datetime.now(tz)
        day_active = in_window(now, pause_day_windows)
        night_active = in_window(now, pause_night_windows)
        if day_active or night_active:
            labels: List[str] = []
            if day_active:
                labels.append("day")
            if night_active:
                labels.append("night")
            seconds_to_end = seconds_until_window_end(now, pause_day_windows + pause_night_windows)
            print(
                f"[ORCH] Pause window active ({'/'.join(labels)}). Sleeping until end: {now.isoformat()} (seconds={seconds_to_end})"
            )
            if args.once:
                return 0
            time.sleep(seconds_to_end if seconds_to_end > 0 else POLL_SECONDS)
            continue

        try:
            env = os.environ.copy()
            rc, out, err = run_python(FETCH_SCRIPT, env=env, timeout_sec=_timeout_for_step("FETCH"), args=["--raw"])
            if rc != 0:
                notify_stub(f"salesdrive_fetch_status21.py error rc={rc}: {err.strip()}")
                raise RuntimeError("Fetch failed")

            orders = parse_orders_from_fetch_output(out)
            if not orders:
                print("[ORCH] No orders in status=21. Sleeping...")
            else:
                # process up to BATCH_SIZE orders per fetch (skip processed and those in backoff)
                eligible: List[Tuple[int, Dict[str, Any]]] = []
                for o in orders:
                    try:
                        oid = int(o.get("id"))
                    except Exception:
                        continue
                    if oid in processed_ids:
                        continue
                    if is_terminal_failed(state, oid):
                        continue
                    backoff_active, wait_left = is_backoff_active(state, oid)
                    if backoff_active:
                        continue
                    eligible.append((oid, o))
                    if len(eligible) >= BATCH_SIZE:
                        break

                if not eligible:
                    # maybe everything is processed or in backoff or terminal failed
                    in_backoff = 0
                    terminal = 0
                    for o in orders:
                        try:
                            oid = int(o.get("id"))
                        except Exception:
                            continue
                        if oid in processed_ids:
                            continue
                        if is_terminal_failed(state, oid):
                            terminal += 1
                            continue
                        if is_backoff_active(state, oid)[0]:
                            in_backoff += 1
                    if in_backoff or terminal:
                        parts = []
                        if in_backoff:
                            parts.append(f"{in_backoff} in backoff")
                        if terminal:
                            parts.append(f"{terminal} terminal failed")
                        print(f"[ORCH] Got {len(orders)} order(s); " + ", ".join(parts) + ". Nothing eligible now. Sleeping...")
                    else:
                        print(f"[ORCH] Got {len(orders)} order(s) but all already processed. Sleeping...")
                else:
                    print(f"[ORCH] Got {len(orders)} order(s). Will process up to {len(eligible)} this cycle.")
                    paused_during_batch = False
                    for order_id, order in eligible:
                        now = datetime.now(tz)
                        day_active = in_window(now, pause_day_windows)
                        night_active = in_window(now, pause_night_windows)
                        if day_active or night_active:
                            labels: List[str] = []
                            if day_active:
                                labels.append("day")
                            if night_active:
                                labels.append("night")
                            seconds_to_end = seconds_until_window_end(now, pause_day_windows + pause_night_windows)
                            print(
                                f"[ORCH] Pause window active ({'/'.join(labels)}). Sleeping until end: {now.isoformat()} (seconds={seconds_to_end})"
                            )
                            if args.once:
                                return 0
                            time.sleep(seconds_to_end if seconds_to_end > 0 else POLL_SECONDS)
                            paused_during_batch = True
                            break

                        print(f"[ORCH] Processing id={order_id}")
                        supplier = str(order.get("supplier") or "").strip()
                        supplier_norm = supplier.casefold()
                        if supplier_norm == "biotus":
                            biotus_items = build_biotus_items(order)
                            print(f"[ORCH] BIOTUS_ITEMS => {biotus_items}")
                        elif supplier_norm == "dobavki.ua":
                            sup2_items = build_sup2_items(order)
                            print(f"[ORCH] SUP2_ITEMS => {sup2_items}")
                        else:
                            print(f"[ORCH] supplier => {supplier}")

                        if args.dry_run:
                            print("[ORCH] dry-run enabled, no steps executed.")
                            # In dry-run we do not mark processed_ids; this allows repeated dry-run tests.
                            continue

                        try:
                            process_one_order(order)
                        except Exception as e:
                            if isinstance(e, StepError):
                                step = e.step
                                reason = e.reason
                            else:
                                step = "pipeline"
                                reason = f"{type(e).__name__}: {str(e)}".strip()

                            # Record failure and decide if it became terminal
                            mark_failed(state, int(order_id), step, reason)
                            save_state(state)

                            fail_count = get_fail_count(state, int(order_id))
                            became_terminal = is_terminal_failed(state, int(order_id))

                            # Do NOT send intermediate notifications. Only notify + change status when terminal.
                            if became_terminal:
                                red_reason = f"{ORCH_FAIL_MARK} Order {order_id} failed after {fail_count} attempt(s). Last step: {step}. Reason: {reason}"
                                # Try to set SalesDrive status to FAIL status with a short red comment.
                                try:
                                    salesdrive_update_status(int(order_id), ORCH_FAIL_STATUS_ID, comment=red_reason)
                                    print(f"[ORCH] SalesDrive status updated: order_id={order_id} -> statusId={ORCH_FAIL_STATUS_ID}")
                                except Exception as se:
                                    # Include SalesDrive error in notify, but keep going
                                    red_reason = f"{red_reason}\nSalesDrive update error: {se}"
                                notify_stub(red_reason)

                            # continue with next order
                            continue

                        # success
                        processed_ids.append(int(order_id))
                        if len(processed_ids) > MAX_PROCESSED_IDS:
                            processed_ids = processed_ids[-MAX_PROCESSED_IDS:]
                        state["processed_ids"] = processed_ids
                        state["last_processed_id"] = int(order_id)
                        state["last_processed_at"] = _now_ts()
                        clear_failed(state, int(order_id))
                        save_state(state)
                        print(f"[ORCH] Done pipeline for order id={order_id}.")
                    if paused_during_batch:
                        continue

        except subprocess.TimeoutExpired:
            notify_stub("[ORCH] Timeout while running a step.")
        except Exception as e:
            notify_stub(f"[ORCH] Orchestrator error: {type(e).__name__}: {e}")

        if args.once:
            break

        time.sleep(POLL_SECONDS)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
