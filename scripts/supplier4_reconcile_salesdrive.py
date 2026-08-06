"""Repair SalesDrive after a Monsterlab Drop order was accepted but unconfirmed by UI."""
from __future__ import annotations

import argparse
import re

from orchestrator import ORCH_DONE_STATUS_ID, salesdrive_update_status


def _pair(value: str) -> tuple[int, str]:
    order_id_raw, sep, supplier_number = str(value or "").partition(":")
    if not sep or not order_id_raw.isdigit() or not re.fullmatch(r"(?:D\d+|\d+)", supplier_number, re.I):
        raise argparse.ArgumentTypeError("expected SALES_DRIVE_ID:SUPPLIER_NUMBER, e.g. 22374:D20250")
    return int(order_id_raw), supplier_number.upper()


def main() -> int:
    parser = argparse.ArgumentParser(description="Set a confirmed SUP4 order to done in SalesDrive.")
    parser.add_argument("--confirmed", action="append", type=_pair, required=True)
    args = parser.parse_args()
    for order_id, supplier_number in args.confirmed:
        salesdrive_update_status(order_id, ORCH_DONE_STATUS_ID, number_sup=supplier_number)
        print(f"[SUP4-RECONCILE] order_id={order_id} statusId={ORCH_DONE_STATUS_ID} numberSup={supplier_number}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
