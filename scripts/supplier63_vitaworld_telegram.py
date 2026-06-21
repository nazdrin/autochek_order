from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _to_bool(value: str, default: bool = False) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _parse_sku(description: str) -> str:
    text = str(description or "").strip()
    if not text:
        return ""
    return re.split(r"[\s,]+", text, maxsplit=1)[0].strip()


def parse_vitaworld_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    products = order.get("products") or []
    out: List[Dict[str, Any]] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        sku = _parse_sku(str(p.get("description") or ""))
        try:
            qty = int(p.get("amount") or 1)
        except Exception:
            qty = 1
        if qty < 1:
            qty = 1
        out.append(
            {
                "sku": sku,
                "qty": qty,
                "name": str(p.get("text") or p.get("name") or "").strip(),
            }
        )
    if not out:
        raise RuntimeError("No products for Vitaworld order")
    return out


def build_vitaworld_message(ttn: str, items: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for it in items:
        sku = str(it.get("sku") or "").strip() or "NO_SKU"
        try:
            qty = int(it.get("qty") or 1)
        except Exception:
            qty = 1
        if qty < 1:
            qty = 1
        lines.append(f"{sku} * {qty} шт")
    lines.append(f"ТТН: {str(ttn or '').strip()}")
    return "\n".join(lines).strip()


def build_vitaworld_salesdrive_products(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    products = order.get("products") or []
    out: List[Dict[str, Any]] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        raw_id = p.get("id")
        if raw_id in (None, ""):
            continue
        desc = str(p.get("description") or "").strip()
        out.append(
            {
                "id": raw_id,
                "name": p.get("text") or p.get("name") or "",
                "costPerItem": p.get("costPerItem"),
                "amount": p.get("amount"),
                "description": desc,
                "discount": p.get("discount") if p.get("discount") is not None else "",
                "sku": _parse_sku(desc),
            }
        )
    return out


def vitaworld_tg_bot_token() -> str:
    token = _env("VITAWORLD_TG_BOT_TOKEN") or _env("TG_BOT_TOKEN")
    if not token:
        raise RuntimeError("VITAWORLD_TG_BOT_TOKEN (or TG_BOT_TOKEN) is empty")
    return token


def vitaworld_tg_chat_id() -> str:
    chat_id = _env("VITAWORLD_TG_CHAT_ID")
    if not chat_id:
        raise RuntimeError("VITAWORLD_TG_CHAT_ID is empty")
    return chat_id


def _labels_dir() -> Path:
    raw = _env("VITAWORLD_LABELS_DIR", "vitaworld_labels")
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def download_vitaworld_label_pdf(ttn: str) -> Path:
    api_key = _env("BIOTUS_NP_API_KEY") or _env("NP_API_KEY")
    if not api_key:
        raise RuntimeError("BIOTUS_NP_API_KEY (or NP_API_KEY) is not set")

    folder = _labels_dir()
    out_path = folder / f"label-{ttn}.pdf"
    url = (
        "https://my.novaposhta.ua/orders/printMarking100x100/"
        f"orders[]/{ttn}/type/pdf/apiKey/{api_key}/zebra"
    )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=25) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            if status >= 400:
                raise RuntimeError(f"Nova Poshta API returned status {status}")
            data = resp.read()
            if not data:
                raise RuntimeError("Downloaded PDF is empty")
            out_path.write_bytes(data)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"NP API HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"NP API connection error: {e}") from e

    if not out_path.exists():
        raise RuntimeError("Label download failed: file does not exist")
    if out_path.suffix.lower() != ".pdf":
        raise RuntimeError("Label download failed: extension is not .pdf")
    if out_path.stat().st_size <= 0:
        raise RuntimeError("Label download failed: file size is zero")
    return out_path


def vitaworld_dry_run_enabled() -> bool:
    return _to_bool(_env("VITAWORLD_DRY_RUN", "0"), False)


def vitaworld_number_sup_value() -> str:
    return _env("VITAWORLD_NUMBERSUP_VALUE", "TG_SENT")


def send_vitaworld_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
        timeout=25,
    )
    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(f"Telegram sendMessage HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        payload = resp.json()
    except Exception as e:
        raise RuntimeError(f"Telegram sendMessage returned non-JSON response: {resp.text[:300]}") from e
    if not bool(payload.get("ok")):
        raise RuntimeError(f"Telegram sendMessage failed: {payload}")


def send_vitaworld_telegram_document(token: str, chat_id: str, pdf_path: Path, caption: str = "") -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with pdf_path.open("rb") as fh:
        files = {"document": (pdf_path.name, fh, "application/pdf")}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        resp = requests.post(url, data=data, files=files, timeout=45)
    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(f"Telegram sendDocument HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        payload = resp.json()
    except Exception as e:
        raise RuntimeError(f"Telegram sendDocument returned non-JSON response: {resp.text[:300]}") from e
    if not bool(payload.get("ok")):
        raise RuntimeError(f"Telegram sendDocument failed: {payload}")


def send_vitaworld_telegram(token: str, chat_id: str, text: str, pdf_path: Path) -> None:
    send_vitaworld_telegram_message(token, chat_id, text)
    send_vitaworld_telegram_document(token, chat_id, pdf_path)


def _extract_ttn_from_order(order: Dict[str, Any]) -> str:
    odd = order.get("ord_delivery_data") or []
    d0 = odd[0] if isinstance(odd, list) and odd else (odd if isinstance(odd, dict) else {})
    return str((d0 or {}).get("trackingNumber") or "").strip()


def run_vitaworld_telegram_flow(order: Dict[str, Any], ttn: str) -> Dict[str, Any]:
    items = parse_vitaworld_items(order)
    message = build_vitaworld_message(ttn, items)
    token = vitaworld_tg_bot_token()
    chat_id = vitaworld_tg_chat_id()

    if vitaworld_dry_run_enabled():
        return {
            "ok": True,
            "supplier": "vitaworld",
            "ttn": ttn,
            "chat_id": chat_id,
            "message": message,
            "pdf_path": "",
            "items": items,
            "numberSup": vitaworld_number_sup_value(),
            "dry_run": True,
        }

    print(f"[VITAWORLD] downloading label => {ttn}")
    pdf_path = download_vitaworld_label_pdf(ttn)
    print(f"[VITAWORLD] sending Telegram => chat_id={chat_id}")
    send_vitaworld_telegram(token=token, chat_id=chat_id, text=message, pdf_path=pdf_path)
    print(f"[VITAWORLD] Telegram sent => chat_id={chat_id}")

    return {
        "ok": True,
        "supplier": "vitaworld",
        "ttn": ttn,
        "chat_id": chat_id,
        "message": message,
        "pdf_path": str(pdf_path),
        "items": items,
        "numberSup": vitaworld_number_sup_value(),
    }


def _load_order_payload(order_json_arg: str, order_json_file: str) -> Dict[str, Any]:
    if order_json_file:
        p = Path(order_json_file)
        raw = p.read_text(encoding="utf-8")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise RuntimeError("order-json-file must contain JSON object")
        return obj
    raw_arg = (order_json_arg or "").strip()
    if not raw_arg:
        raise RuntimeError("--order-json or --order-json-file is required")
    p2 = Path(raw_arg)
    if p2.exists() and p2.is_file():
        obj = json.loads(p2.read_text(encoding="utf-8"))
    else:
        obj = json.loads(raw_arg)
    if not isinstance(obj, dict):
        raise RuntimeError("order-json must be a JSON object")
    return obj


def main() -> int:
    ap = argparse.ArgumentParser(description="Vitaworld supplier 63 Telegram flow")
    ap.add_argument("--order-json", default="", help="Order JSON string or path to JSON file")
    ap.add_argument("--order-json-file", default="", help="Path to order JSON file")
    ap.add_argument("--ttn", default="", help="TTN override")
    args = ap.parse_args()

    try:
        order = _load_order_payload(args.order_json, args.order_json_file)
        ttn = (args.ttn or "").strip() or _extract_ttn_from_order(order)
        if not ttn:
            raise RuntimeError("TTN is empty (pass --ttn or provide ord_delivery_data[0].trackingNumber)")
        result = run_vitaworld_telegram_flow(order, ttn)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if bool(result.get("ok")) else 1
    except Exception as e:
        order = {}
        try:
            order = _load_order_payload(args.order_json, args.order_json_file)
        except Exception:
            pass
        items: List[Dict[str, Any]] = []
        try:
            if order:
                items = parse_vitaworld_items(order)
        except Exception:
            items = []
        ttn = (args.ttn or "").strip() or _extract_ttn_from_order(order)
        fail = {
            "ok": False,
            "supplier": "vitaworld",
            "reason": str(e),
            "ttn": ttn,
            "items": items,
        }
        print(json.dumps(fail, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
