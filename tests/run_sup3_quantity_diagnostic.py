"""Reproduce one DSN order's quantities without submitting it or updating SalesDrive."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from orchestrator import build_sup3_items  # noqa: E402


def fetch_salesdrive_order(order_id: int, *, max_pages: int = 100) -> dict[str, Any]:
    base_url = os.getenv("SALESDRIVE_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("SALESDRIVE_API_KEY", "").strip()
    if not base_url or not api_key:
        raise RuntimeError("SALESDRIVE_BASE_URL and SALESDRIVE_API_KEY are required")

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "X-Api-Key": api_key})
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        response = session.get(
            base_url + "/api/order/list/",
            params={"limit": 100, "page": page},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"SalesDrive list lookup failed with HTTP {response.status_code} on page {page}")
        data = response.json().get("data") or []
        if not isinstance(data, list):
            raise RuntimeError("Unexpected SalesDrive response: data is not a list")
        if not data:
            break
        fresh = []
        for order in data:
            key = str(order.get("id") or "")
            if key and key not in seen:
                seen.add(key)
                fresh.append(order)
        if not fresh:
            raise RuntimeError("SalesDrive repeated a page; exact order lookup is unverified")
        match = next((order for order in fresh if str(order.get("id") or "") == str(order_id)), None)
        if match is not None:
            if str(match.get("supplierlist") or "") != "39":
                raise RuntimeError(f"SalesDrive order {order_id} is not a DSN order (supplierlist must be 39)")
            return match
    raise RuntimeError(f"SalesDrive order {order_id} was not found in the first {max_pages} pages")


def _result_from_stdout(stdout: str) -> dict[str, Any]:
    for line in reversed((stdout or "").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and ("ok" in value or "stage" in value):
            return value
    raise RuntimeError("Supplier diagnostic did not return its JSON result")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", type=int, required=True, help="Exact SalesDrive DSN order ID")
    parser.add_argument("--headless", action="store_true", help="Run the browser hidden; visible is the default")
    parser.add_argument("--artifact-dir", type=Path, default=ROOT / "artifacts" / "sup3_diagnostics")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_path = args.artifact_dir / f"order_{args.order_id}_{timestamp}.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    record: dict[str, Any] = {
        "order_id": args.order_id,
        "diagnostic_only": True,
        "submitted": False,
        "salesdrive_updated": False,
    }
    try:
        order = fetch_salesdrive_order(args.order_id)
        items = build_sup3_items(order)
        if not items:
            raise RuntimeError("SalesDrive order has no usable DSN SKU/quantity lines")
        record["expected_items"] = [
            {"sku": part.rsplit(":", 1)[0], "qty": int(part.rsplit(":", 1)[1])}
            for part in items.split(",")
        ]
        env = os.environ.copy()
        env.update({
            "SUP3_STAGE": "diagnose_quantities",
            "SUP3_ITEMS": items,
            "SUP3_DIAGNOSTIC_ORDER_ID": str(args.order_id),
            "SUP3_CLEAR_BASKET": "0",
            "SUP3_USE_CDP": "0",
            "SUP3_HEADLESS": "1" if args.headless else "0",
            "SUP3_TTN": "",
            # The supplier entrypoint's normal startup cleanup removes artifacts/.
            # A diagnostic must preserve unrelated files and its own report.
            "RUNTIME_CLEANUP_ON_START": "0",
        })
        run = subprocess.run(
            [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "supplier3_run_order.py")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=360,
            check=False,
        )
        try:
            supplier_result = _result_from_stdout(run.stdout)
        except Exception as exc:
            supplier_result = {"ok": False, "stage": "diagnose_quantities", "error": str(exc)}
        record["returncode"] = run.returncode
        record["diagnostic"] = supplier_result
        diagnostic_payload = supplier_result.get("details", {}).get("diagnostic_result") if isinstance(supplier_result.get("details"), dict) else None
        if not isinstance(diagnostic_payload, dict):
            diagnostic_payload = supplier_result
        cleanup = diagnostic_payload.get("cleanup") if isinstance(diagnostic_payload, dict) else None
        if isinstance(cleanup, dict) and cleanup.get("manual_cleanup_required"):
            record["manual_cleanup_required"] = True
        record["ok"] = run.returncode == 0 and bool(supplier_result.get("ok"))
    except subprocess.TimeoutExpired:
        record.update({
            "ok": False,
            "error": "Diagnostic timed out; inspect the DSN work-account cart manually before any new run.",
            "manual_cleanup_required": True,
        })
    except Exception as exc:
        record.update({"ok": False, "error": str(exc)[:1000]})

    artifact_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": record.get("ok", False), "order_id": args.order_id, "artifact": str(artifact_path),
                      "submitted": False, "salesdrive_updated": False,
                      "manual_cleanup_required": record.get("manual_cleanup_required", False)}, ensure_ascii=False))
    return 0 if record.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
