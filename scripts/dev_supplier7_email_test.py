from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from supplier7_email_supplier import build_supplier7_body, parse_supplier7_items  # noqa: E402


def _sample_order() -> dict:
    return {
        "id": 70001,
        "supplierlist": 48,
        "ord_delivery_data": [{"trackingNumber": "20451478685683"}],
        "products": [
            {
                "id": "p1",
                "description": "1029-S Перегородка міжпальцева",
                "amount": "2",
                "name": "Перегородка міжпальцева Торос Груп 1029 ортопедична з захистом на кісточку, розмір S",
                "costPerItem": "100",
            },
            {
                "id": "p2",
                "description": "ABC-200 another item",
                "amount": "2.0",
                "name": "Decimal amount item",
                "costPerItem": "200",
            },
            {
                "id": "p3",
                "description": "BAD-QTY fallback",
                "amount": "not-a-number",
                "name": "Bad amount item",
                "costPerItem": "300",
            },
        ],
    }


def main() -> int:
    order = _sample_order()
    items = parse_supplier7_items(order)
    assert items == [
        {
            "sku": "1029-S",
            "qty": 2,
            "name": "Перегородка міжпальцева Торос Груп 1029 ортопедична з захистом на кісточку, розмір S",
        },
        {"sku": "ABC-200", "qty": 2, "name": "Decimal amount item"},
        {"sku": "BAD-QTY", "qty": 1, "name": "Bad amount item"},
    ], items
    print("Supplier7 item parsing quantity ok")

    body = build_supplier7_body(order, "20451478685683", items)
    expected = (
        "Перегородка міжпальцева Торос Груп 1029 ортопедична з захистом на кісточку, розмір S\n"
        "1029-S\n"
        "К-во: 2\n"
        "\n"
        "Decimal amount item\n"
        "ABC-200\n"
        "К-во: 2\n"
        "\n"
        "Bad amount item\n"
        "BAD-QTY\n"
        "К-во: 1\n"
        "\n"
        "20451478685683\n"
    )
    assert body == expected, body
    print("Supplier7 email body quantity format ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
