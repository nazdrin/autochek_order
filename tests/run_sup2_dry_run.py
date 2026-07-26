"""Run a redacted, non-submitting SUP2 checkout smoke test.

Use ``--virtual`` for the Kam'янка regression scenario or omit it to exercise
the latest real SUP2 order from SalesDrive.  Both variants only fill checkout:
SUP2_DRY_RUN prevents the submit click and this script never updates SalesDrive.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def latest_sup2_order(status: int | None = None, order_id: int | None = None) -> dict:
    session = requests.Session()
    session.headers.update({"accept": "application/json", "X-Api-Key": os.environ["SALESDRIVE_API_KEY"]})
    params = {"limit": 100, "page": 1}
    if status is not None:
        params["filter[statusId]"] = status
    response = session.get(
        os.environ["SALESDRIVE_BASE_URL"].rstrip("/") + "/api/order/list/",
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    orders = [order for order in response.json().get("data", []) if str(order.get("supplierlist")) == "41"]
    if order_id is not None:
        return next(order for order in orders if int(order.get("id") or 0) == order_id)
    return next(iter(orders))


def sup2_items(order: dict) -> str:
    parts = []
    for product in order.get("products") or []:
        description = str(product.get("description") or "").strip()
        if not description:
            continue
        parts.append(f"{description.split(',', 1)[0].strip()}:{max(1, int(product.get('amount') or 1))}")
    if not parts:
        raise RuntimeError("Selected SalesDrive SUP2 order has no usable SKU.")
    return ",".join(parts)


def virtual_kamianka_order(source: dict) -> dict:
    return {
        # Keep the real order's products; only the destination is replaced by
        # the regression case. No supplier order is sent in dry-run mode.
        "products": source["products"],
        "primaryContact": {"name": "Тест", "lName": "Перевірка", "phone": ["0501234567"]},
        "ord_delivery_data": [{
            "cityName": "Кам’янка",
            "areaName": "Черкаський р-н",
            "regionName": "Черкаська обл.",
            "branchNumber": 2,
            "address": "Відділення №2 (до 10 кг): вул. Захисників України, 54",
        }],
        "shipping_address": "Відділення №2 (до 10 кг): вул. Захисників України, 54",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--virtual", action="store_true")
    parser.add_argument("--status", type=int, help="Use a SUP2 order from this SalesDrive status.")
    parser.add_argument("--order-id", type=int, help="Run one exact SalesDrive order ID (must be a SUP2 order).")
    parser.add_argument("--visible", action="store_true", help="Show the supplier browser window.")
    parser.add_argument("--pause-seconds", type=int, default=0, help="Keep the checkout window open after dry-run.")
    parser.add_argument(
        "--manual-submit-wait-seconds",
        type=int,
        default=0,
        help="Wait for a human to click submit; automation never clicks the button.",
    )
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    source = latest_sup2_order(args.status, args.order_id)
    order = virtual_kamianka_order(source) if args.virtual else source
    env = os.environ.copy()
    env.update({
        "SUP2_ORDER_JSON": json.dumps(order, ensure_ascii=False),
        "SUP2_ITEMS": sup2_items(order),
        "SUP2_DRY_RUN": "1",
        "SUP2_HEADLESS": "0" if args.visible else "1",
        "SUP2_CLEAR_BASKET": "1",
        "SUP2_DEBUG_PAUSE_SECONDS": str(max(0, args.pause_seconds)),
        "SUP2_MANUAL_SUBMIT_WAIT_SECONDS": str(max(0, args.manual_submit_wait_seconds)),
    })
    run = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/supplier2_run_order.py")],
        env=env,
        text=True,
        capture_output=True,
        timeout=max(180, args.pause_seconds + args.manual_submit_wait_seconds + 180),
    )
    marker = next((line for line in run.stdout.splitlines() if line.startswith("SUPPLIER_RESULT_JSON=")), "")
    payload = json.loads(marker.split("=", 1)[1]) if marker else {"ok": False, "error": "result marker missing"}
    delivery = payload.get("delivery") or {}
    print(json.dumps({
        "source_order_id": source.get("id"),
        "scenario": "virtual_kamianka" if args.virtual else "real_salesdrive_order",
        "returncode": run.returncode,
        "ok": payload.get("ok"),
        "stage": payload.get("stage"),
        "error": payload.get("error"),
        "submitted": payload.get("submitted"),
        "dry_run": payload.get("dry_run"),
        "city": delivery.get("city"),
        "warehouse": delivery.get("warehouse"),
    }, ensure_ascii=False))
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
