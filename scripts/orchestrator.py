# scripts/orchestrator.py
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

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


POLL_SECONDS = int(os.getenv("ORCH_POLL_SECONDS", "60"))
TIMEOUT_SEC = int(os.getenv("ORCH_STEP_TIMEOUT_SEC", "600"))  # таймаут на шаг (10 минут по умолчанию)

STATE_FILE = Path(os.getenv("ORCH_STATE_FILE", str(ROOT / ".orch_state.json")))
MAX_PROCESSED_IDS = int(os.getenv("ORCH_MAX_PROCESSED_IDS", "200"))

# Optional defaults for downstream scripts
DEFAULT_BIOTUS_USE_CDP = os.getenv("BIOTUS_USE_CDP", "1")


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
    # TODO: заменить на Telegram/Slack/Email
    print(f"[NOTIFY] {message}", file=sys.stderr)


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


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"processed_ids": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"processed_ids": []}
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
        return data
    except Exception:
        return {"processed_ids": []}


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


def choose_np_step(address: str, branch_number: str) -> Tuple[str, Path, str, str]:
    """
    Returns: (step_name, script_path, env_key, env_value)
    - If address contains 'поштомат' -> terminal script and BIOTUS_TERMINAL_QUERY.
      Prefer passing the numeric branch number (e.g. 48437) because the UI search works by number.
    - Otherwise -> branch script and BIOTUS_BRANCH_QUERY (pass full address as before).
    """
    a_norm = (address or "").casefold()
    if "поштомат" in a_norm:
        value = (branch_number or "").strip() or (address or "").strip()
        return ("step6_1_select_np_terminal", STEP6_TERMINAL_SCRIPT, "BIOTUS_TERMINAL_QUERY", value)
    return ("step6_select_np_branch", STEP6_BRANCH_SCRIPT, "BIOTUS_BRANCH_QUERY", (address or "").strip())


def process_one_order(order: Dict[str, Any]) -> None:
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
    if not delivery_address:
        raise RuntimeError("Не найдено поле ord_delivery_data[0].address для выбора отделения/поштомата.")

    step6_name, step6_script, step6_env_key, step6_env_val = choose_np_step(delivery_address, delivery_branch_number)

    city_env = extract_city_env(order)
    for k, v in city_env.items():
        env[k] = v

    if city_env:
        print(
            f"[ORCH] City => name={city_env.get('BIOTUS_CITY_NAME','')} area={city_env.get('BIOTUS_CITY_AREA','')} region={city_env.get('BIOTUS_CITY_REGION','')} type={city_env.get('BIOTUS_CITY_TYPE','')}"
        )

    env[step6_env_key] = step6_env_val
    print(f"[ORCH] Delivery address => {delivery_address}")
    if delivery_branch_number:
        print(f"[ORCH] Delivery branchNumber => {delivery_branch_number}")
    print(f"[ORCH] Step6 => {step6_name} ({step6_env_key}='{step6_env_val}')")

    print(f"[ORCH] Using BIOTUS_ITEMS: {biotus_items}")

    steps: List[Tuple[str, Path]] = [
        ("step2_3_add_items_to_cart", STEP2_3_SCRIPT),
        ("step4_checkout", STEP4_SCRIPT),
        ("step5_select_drop_tab", STEP5_DROP_TAB_SCRIPT),
        ("step5_select_city", STEP5_CITY_SCRIPT),
        ("step5_fill_name_phone", STEP5_FILL_NAME_PHONE_SCRIPT),
        (step6_name, step6_script),
    ]

    for step_name, script in steps:
        rc, out, err = run_python(script, env=env, timeout_sec=TIMEOUT_SEC)
        if out.strip():
            print(out, end="" if out.endswith("\n") else "\n")
        if err.strip():
            print(err, file=sys.stderr, end="" if err.endswith("\n") else "\n")
        if rc != 0:
            raise RuntimeError(f"{step_name} завершился с кодом {rc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Сделать один цикл и выйти (для теста)")
    ap.add_argument("--dry-run", action="store_true", help="Не запускать step2_3, только вывести BIOTUS_ITEMS")
    args = ap.parse_args()

    if not FETCH_SCRIPT.exists():
        print(f"Fetch script not found: {FETCH_SCRIPT}", file=sys.stderr)
        return 2

    required = [
        ("Step2_3 script", STEP2_3_SCRIPT),
        ("Step4 script", STEP4_SCRIPT),
        ("Step5 drop tab script", STEP5_DROP_TAB_SCRIPT),
        ("Step5 city script", STEP5_CITY_SCRIPT),
        ("Step5 fill name/phone script", STEP5_FILL_NAME_PHONE_SCRIPT),
        ("Step6 branch script", STEP6_BRANCH_SCRIPT),
        ("Step6 terminal script", STEP6_TERMINAL_SCRIPT),
    ]
    for label, p in required:
        if not p.exists():
            print(f"{label} not found: {p}", file=sys.stderr)
            return 2

    state = load_state()
    processed_ids: List[int] = state.get("processed_ids", [])

    while True:
        try:
            env = os.environ.copy()
            rc, out, err = run_python(FETCH_SCRIPT, env=env, timeout_sec=60, args=["--raw"])
            if rc != 0:
                notify_stub(f"salesdrive_fetch_status21.py error rc={rc}: {err.strip()}")
                raise RuntimeError("Fetch failed")

            orders = parse_orders_from_fetch_output(out)
            if not orders:
                print("[ORCH] No orders in status=21. Sleeping...")
            else:
                # pick first unprocessed
                picked: Dict[str, Any] | None = None
                picked_id: int | None = None
                for o in orders:
                    try:
                        oid = int(o.get("id"))
                    except Exception:
                        continue
                    if oid not in processed_ids:
                        picked = o
                        picked_id = oid
                        break

                if picked is None:
                    print(f"[ORCH] Got {len(orders)} order(s) but all already processed. Sleeping...")
                else:
                    order = picked
                    order_id = picked_id
                    print(f"[ORCH] Got {len(orders)} order(s). Processing id={order_id}")

                    biotus_items = build_biotus_items(order)
                    print(f"[ORCH] BIOTUS_ITEMS => {biotus_items}")

                    if not args.dry_run:
                        process_one_order(order)
                        # mark processed only after success
                        processed_ids.append(int(order_id))
                        # keep file from growing forever
                        if len(processed_ids) > MAX_PROCESSED_IDS:
                            processed_ids = processed_ids[-MAX_PROCESSED_IDS:]
                        state["processed_ids"] = processed_ids
                        state["last_processed_id"] = int(order_id)
                        state["last_processed_at"] = int(time.time())
                        save_state(state)
                        print(f"[ORCH] Done steps 2_3->5 for order id={order_id}. (Next steps later)")
                    else:
                        print("[ORCH] dry-run enabled, step2_3 not executed.")

        except subprocess.TimeoutExpired:
            notify_stub("Timeout while running a step.")
        except Exception as e:
            notify_stub(f"Orchestrator error: {e}")

        if args.once:
            break

        time.sleep(POLL_SECONDS)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())