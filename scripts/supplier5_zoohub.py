from __future__ import annotations

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
    token = re.split(r"[\s,]+", text, maxsplit=1)[0].strip()
    return token


def parse_zoohub_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
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
        raise RuntimeError("No products for Zoohub order")
    return out


def build_zoohub_salesdrive_products(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    products = order.get("products") or []
    out: List[Dict[str, Any]] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        raw_id = p.get("id")
        if raw_id in (None, ""):
            # Keep Zoohub email flow non-blocking: skip rows without id for SalesDrive products update.
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


def _unique_skus_in_order(items: List[Dict[str, Any]]) -> List[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        sku = str(x.get("sku") or "").strip()
        if not sku:
            continue
        if sku in seen:
            continue
        seen.add(sku)
        out.append(sku)
    return out


def build_zoohub_subject(items: List[Dict[str, Any]]) -> str:
    prefix = _env("ZOOHUB_SUBJECT_PREFIX", "Zoohub")
    skus = _unique_skus_in_order(items)
    sku_part = ", ".join(skus) if skus else "NO_SKU"
    return f"{prefix}: {sku_part}"


def build_zoohub_body(order_id: int, ttn: str, items: List[Dict[str, Any]]) -> str:
    intro = _env("ZOOHUB_EMAIL_BODY_INTRO")
    lines: List[str] = []
    if intro:
        lines.append(intro)
        lines.append("")
    lines.append(f"TTN: {ttn}")
    lines.append("")
    lines.append("Товари:")
    for it in items:
        sku = str(it.get("sku") or "").strip() or "NO_SKU"
        qty = int(it.get("qty") or 1)
        name = str(it.get("name") or "").strip()
        lines.append(f"- {sku} x{qty} ({name})")
    lines.append("")
    lines.append(f"SalesDrive orderId: {order_id}")
    return "\n".join(lines)


def parse_to_emails() -> List[str]:
    raw = _env("ZOOHUB_TO_EMAILS")
    if not raw:
        raise RuntimeError("ZOOHUB_TO_EMAILS is empty")
    out = [x.strip() for x in raw.split(",") if x.strip()]
    if not out:
        raise RuntimeError("ZOOHUB_TO_EMAILS is empty")
    return out


def _labels_dir() -> Path:
    raw = _env("ZOOHUB_LABELS_DIR", "zoohub_labels")
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

    if not out_path.exists():
        raise RuntimeError("Label download failed: file does not exist")
    if out_path.suffix.lower() != ".pdf":
        raise RuntimeError("Label download failed: extension is not .pdf")
    if out_path.stat().st_size <= 0:
        raise RuntimeError("Label download failed: file size is zero")
    return out_path


def zoohub_dry_run_enabled() -> bool:
    return _to_bool(_env("ZOOHUB_DRY_RUN", "0"), False)


def zoohub_number_sup_value() -> str:
    return _env("ZOOHUB_NUMBERSUP_VALUE", "EMAIL_SENT")


def send_zoohub_email(subject: str, body: str, recipients: List[str], pdf_path: Path) -> None:
    send_email(subject=subject, body_text=body, to_list=recipients, pdf_path=pdf_path)
