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

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from services.email_sender.gmail_smtp import send_email


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


def parse_supplier7_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    products = order.get("products") or []
    out: List[Dict[str, Any]] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        sku = _parse_sku(str(p.get("description") or ""))
        name = str(p.get("text") or p.get("name") or "").strip()
        try:
            qty = int(p.get("amount") or 1)
        except Exception:
            qty = 1
        if qty < 1:
            qty = 1
        out.append({"name": name, "sku": sku, "qty": qty})
    if not out:
        raise RuntimeError("No products for Supplier7 order")
    return out


def build_supplier7_subject() -> str:
    return _env("SUP7_SUBJECT", "ФОП Петренко І.А.")


def build_supplier7_body(order: Dict[str, Any], ttn: str, items: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for idx, it in enumerate(items):
        name = str(it.get("name") or "").strip() or "(без назви)"
        sku = str(it.get("sku") or "").strip()
        lines.append(name)
        lines.append(sku)
        if idx != len(items) - 1:
            lines.append("")
    lines.append("")
    lines.append(str(ttn or "").strip())
    return "\n".join(lines).strip() + "\n"


def parse_supplier7_to_emails() -> List[str]:
    raw = _env("SUP7_TO_EMAILS", "iostrianko@gmail.com")
    out = [x.strip() for x in raw.split(",") if x.strip()]
    if not out:
        raise RuntimeError("SUP7_TO_EMAILS is empty")
    return out


def _labels_dir() -> Path:
    raw = _env("SUP7_LABELS_DIR", "supplier7_labels")
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def download_label_pdf(ttn: str) -> Path:
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

    if not out_path.exists() or out_path.suffix.lower() != ".pdf" or out_path.stat().st_size <= 0:
        raise RuntimeError("Label download failed")
    return out_path


def build_supplier7_salesdrive_products(order: Dict[str, Any]) -> List[Dict[str, Any]]:
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


def supplier7_dry_run_enabled() -> bool:
    return _to_bool(_env("SUP7_DRY_RUN", "0"), False)


def supplier7_number_sup_value() -> str:
    return _env("SUP7_NUMBERSUP_VALUE", "EMAIL_SENT")


def send_supplier7_email(subject: str, body: str, recipients: List[str], pdf_path: Path) -> None:
    send_email(subject=subject, body_text=body, to_list=recipients, pdf_path=pdf_path)


def _extract_ttn_from_order(order: Dict[str, Any]) -> str:
    odd = order.get("ord_delivery_data") or []
    d0 = odd[0] if isinstance(odd, list) and odd else (odd if isinstance(odd, dict) else {})
    return str((d0 or {}).get("trackingNumber") or "").strip()


def run_supplier7_email_flow(order: Dict[str, Any], ttn: str) -> Dict[str, Any]:
    items = parse_supplier7_items(order)
    print(f"[SUP7] parsed items => {len(items)}")
    recipients = parse_supplier7_to_emails()
    subject = build_supplier7_subject()
    body = build_supplier7_body(order, ttn, items)

    if supplier7_dry_run_enabled():
        return {
            "ok": True,
            "supplier": "supplier7",
            "ttn": ttn,
            "email_to": recipients,
            "subject": subject,
            "pdf_path": "",
            "items": items,
            "numberSup": supplier7_number_sup_value(),
            "dry_run": True,
        }

    print(f"[SUP7] downloading label => {ttn}")
    pdf_path = download_label_pdf(ttn)
    print(f"[SUP7] sending email => {','.join(recipients)}")
    send_supplier7_email(subject=subject, body=body, recipients=recipients, pdf_path=pdf_path)
    print(f"[SUP7] email sent => {','.join(recipients)}")

    return {
        "ok": True,
        "supplier": "supplier7",
        "ttn": ttn,
        "email_to": recipients,
        "subject": subject,
        "pdf_path": str(pdf_path),
        "items": items,
        "numberSup": supplier7_number_sup_value(),
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
    ap = argparse.ArgumentParser(description="Supplier7 email-only flow")
    ap.add_argument("--order-json", default="", help="Order JSON string or path to JSON file")
    ap.add_argument("--order-json-file", default="", help="Path to order JSON file")
    ap.add_argument("--ttn", default="", help="TTN override")
    args = ap.parse_args()

    try:
        order = _load_order_payload(args.order_json, args.order_json_file)
        ttn = (args.ttn or "").strip() or _extract_ttn_from_order(order)
        if not ttn:
            raise RuntimeError("TTN is empty (pass --ttn or provide ord_delivery_data[0].trackingNumber)")
        result = run_supplier7_email_flow(order, ttn)
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
                items = parse_supplier7_items(order)
        except Exception:
            items = []
        ttn = (args.ttn or "").strip() or _extract_ttn_from_order(order)
        fail = {
            "ok": False,
            "supplier": "supplier7",
            "reason": str(e),
            "ttn": ttn,
            "items": items,
        }
        print(json.dumps(fail, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
