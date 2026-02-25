import os
import json
import argparse
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def fetch_orders(base_url: str, api_key: str, status_id: int, limit: int, max_pages: int) -> List[Dict[str, Any]]:
    """
    Тянем /api/order/list/ постранично.
    На скрине видно page/limit, поэтому поддерживаем пагинацию.
    Если у вас на аккаунте page не нужен — всё равно безопасно, просто вернёт первую страницу.
    """
    session = requests.Session()
    session.headers.update(
        {
            "accept": "application/json",
            "X-Api-Key": api_key,
        }
    )

    all_items: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        params = {
            "filter[statusId]": status_id,
            "limit": limit,
            "page": page,
        }
        url = base_url.rstrip("/") + "/api/order/list/"
        r = session.get(url, params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")

        payload = r.json()
        data = payload.get("data") or []
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected payload shape: 'data' is not a list. Keys={list(payload.keys())}")

        if not data:
            break

        all_items.extend(data)

        # если вернулось меньше limit — вероятно, это последняя страница
        if len(data) < limit:
            break

    return all_items


def summarize_order(o: Dict[str, Any]) -> Dict[str, Any]:
    delivery = (o.get("ord_delivery_data") or [{}])
    d0 = delivery[0] if isinstance(delivery, list) and delivery else {}

    primary = o.get("primaryContact") or {}
    phones = primary.get("phone") or []
    phone = phones[0] if isinstance(phones, list) and phones else None

    products = o.get("products") or []
    prods_short = []
    if isinstance(products, list):
        for p in products[:5]:
            prods_short.append(
                {
                    "amount": p.get("amount"),
                    "parameter": p.get("parameter"),  # часто тут код
                    "sku": p.get("sku"),
                    "barcode": p.get("barcode"),
                    "text": p.get("text"),
                    "price": p.get("price"),
                }
            )

    return {
        "id": o.get("id"),
        "tabletkiOrder": o.get("tabletkiOrder"),
        "orderTime": o.get("orderTime"),
        "updateAt": o.get("updateAt"),
        "statusId": o.get("statusId"),
        "supplier": o.get("supplier"),
        "supplierlist": o.get("supplierlist"),
        "phone": phone,
        "trackingNumber": d0.get("trackingNumber"),
        "cityName": d0.get("cityName"),
        "address": d0.get("address"),
        "postpaySum": d0.get("postpaySum"),
        "products": prods_short,
        "raw_token": o.get("token"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", type=int, default=_env_int("SALESDRIVE_STATUS_ID", 21))
    parser.add_argument("--limit", type=int, default=_env_int("SALESDRIVE_LIMIT", 100))
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--raw", action="store_true", help="Печатать весь JSON ответа (может быть очень большим)")
    args = parser.parse_args()

    load_dotenv()

    base_url = os.getenv("SALESDRIVE_BASE_URL", "").strip()
    api_key = os.getenv("SALESDRIVE_API_KEY", "").strip()

    if not base_url:
        raise SystemExit("Missing env SALESDRIVE_BASE_URL (e.g. https://petrenko.salesdrive.me)")
    if not api_key:
        raise SystemExit("Missing env SALESDRIVE_API_KEY")

    orders = fetch_orders(base_url, api_key, args.status, args.limit, args.max_pages)

    if args.raw:
        print(json.dumps({"data": orders}, ensure_ascii=False, indent=2))
        return
    print(f"\n[OK] SalesDrive orders fetched: {len(orders)} (statusId={args.status})\n")

    # печатаем короткий, читаемый итог по каждому заказу
    for i, o in enumerate(orders, start=1):
        s = summarize_order(o)
        print(f"--- #{i} ---")
        print(
            f"id={s['id']} tabletkiOrder={s['tabletkiOrder']} statusId={s['statusId']} supplier={s['supplier']} supplierlist={s['supplierlist']}\n"
            f"orderTime={s['orderTime']} updateAt={s['updateAt']}\n"
            f"phone={s['phone']} trackingNumber={s['trackingNumber']}\n"
            f"city={s['cityName']} address={s['address']} postpaySum={s['postpaySum']}\n"
        )
        print("products:")
        for p in s["products"]:
            print(f"  - amount={p.get('amount')} code={p.get('parameter')} sku={p.get('sku')} barcode={p.get('barcode')} price={p.get('price')}")
            # text может быть длинным — печатаем аккуратно
            txt = (p.get("text") or "").strip()
            if txt:
                print(f"    {txt}")
        print()

    if not orders:
        print("[INFO] Заказов с таким статусом не найдено.")


if __name__ == "__main__":
    main()
