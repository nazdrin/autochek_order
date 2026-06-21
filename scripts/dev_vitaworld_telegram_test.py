from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import orchestrator  # noqa: E402
from supplier63_vitaworld_telegram import (  # noqa: E402
    build_vitaworld_message,
    parse_vitaworld_items,
)


def _sample_order() -> dict:
    return {
        "id": 12345,
        "supplierlist": 63,
        "organizationId": 2,
        "ord_delivery_data": [{"trackingNumber": "20451465385274"}],
        "products": [
            {
                "id": "p1",
                "description": "SOR45931, витамины",
                "amount": "1",
                "name": "Product 1",
                "costPerItem": "100",
            },
            {
                "id": "p2",
                "description": "VW-200 other text",
                "amount": "3",
                "name": "Product 2",
                "costPerItem": "200",
            },
        ],
    }


def main() -> int:
    order = _sample_order()
    items = parse_vitaworld_items(order)
    assert items == [
        {"sku": "SOR45931", "qty": 1, "name": "Product 1"},
        {"sku": "VW-200", "qty": 3, "name": "Product 2"},
    ], items
    print("Vitaworld item parsing ok")

    message = build_vitaworld_message("20451465385274", items)
    expected = "SOR45931 * 1 шт\nVW-200 * 3 шт\nТТН: 20451465385274"
    assert message == expected, message
    print("Vitaworld Telegram message format ok")

    old_env = {
        "BIOTUS_NP_API_KEY": os.environ.get("BIOTUS_NP_API_KEY"),
        "NP_API_KEY": os.environ.get("NP_API_KEY"),
        "BIOTUS_NP_API_KEY_ORG_2": os.environ.get("BIOTUS_NP_API_KEY_ORG_2"),
        "NP_API_KEY_ORG_2": os.environ.get("NP_API_KEY_ORG_2"),
    }
    old_salesdrive_update_status = orchestrator.salesdrive_update_status
    try:
        os.environ["BIOTUS_NP_API_KEY"] = "main-key"
        os.environ.pop("NP_API_KEY", None)
        os.environ["BIOTUS_NP_API_KEY_ORG_2"] = "org2-key"
        os.environ.pop("NP_API_KEY_ORG_2", None)
        key, source = orchestrator.resolve_np_api_key_for_order(order)
        assert key == "org2-key", (key, source)
        assert source == "BIOTUS_NP_API_KEY_ORG_2", source
        print("Vitaworld NP organization key selection ok")

        updates: list[tuple[int, int, str, int]] = []

        def fake_salesdrive_update_status(order_id, status_id, comment=None, number_sup=None, products=None):
            updates.append((int(order_id), int(status_id), str(number_sup or ""), len(products or [])))

        orchestrator.salesdrive_update_status = fake_salesdrive_update_status
        state = {
            "vitaworld_sent": {
                "12345": {
                    "ttn": "20451465385274",
                    "chat_id": "-100123",
                }
            }
        }
        processed = orchestrator.process_one_order(order, state=state)
        assert processed is True
        assert updates == [(12345, 4, "TG_SENT", 2)], updates
        print("Vitaworld idempotent status retry ok")
    finally:
        orchestrator.salesdrive_update_status = old_salesdrive_update_status
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
