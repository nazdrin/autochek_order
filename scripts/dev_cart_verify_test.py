from __future__ import annotations

from pathlib import Path

from step2_3_add_items_to_cart import _validate_cart, parse_cart_html, parse_cart_json_blob, parse_expected_items
from supplier3_run_order import LOGIN_EMAIL_SELECTOR, LOGIN_PASSWORD_SELECTOR, LOGIN_SUBMIT_SELECTOR

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"


def _load(name: str) -> str:
    path = ART / name
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8")


def main() -> int:
    expected = parse_expected_items("NOW-00109=2,SOL-03220=1")

    ok_found = parse_cart_html(_load("cart_sample_ok.html"))
    ok_errors = _validate_cart(expected, ok_found, strict=True)
    if ok_errors:
        print("FAIL: expected OK fixture to pass")
        print("Found:", ok_found)
        print("Errors:", ok_errors)
        return 1
    print("OK fixture passed:", ok_found)

    bad_found = parse_cart_html(_load("cart_sample_bad.html"))
    bad_errors = _validate_cart(expected, bad_found, strict=True)
    if not bad_errors:
        print("FAIL: expected BAD fixture to fail")
        print("Found:", bad_found)
        return 1

    print("BAD fixture failed as expected")
    for e in bad_errors:
        print(" -", e)

    dynamic_found = parse_cart_json_blob(
        {
            "cart": {
                "items": [
                    {"product_sku": "NOW-00109", "qty": 2, "product_name": "NOW Foods Test Product"},
                    {"sku": "SOL-03220", "quantity": "1", "name": "Solaray Test Product"},
                ]
            }
        }
    )
    dynamic_errors = _validate_cart(expected, dynamic_found, strict=True)
    if dynamic_errors:
        print("FAIL: expected dynamic cart data to pass")
        print("Found:", dynamic_found)
        print("Errors:", dynamic_errors)
        return 1
    print("Dynamic cart data passed:", dynamic_found)

    missing_errors = _validate_cart(parse_expected_items("THR-12502=1"), dynamic_found, strict=True)
    if not missing_errors:
        print("FAIL: expected missing SKU to fail")
        print("Found:", dynamic_found)
        return 1
    print("Missing SKU failed as expected")

    selector_blob = " ".join([LOGIN_EMAIL_SELECTOR, LOGIN_PASSWORD_SELECTOR, LOGIN_SUBMIT_SELECTOR])
    required_marks = ["email", "Е-пошта", "password", "Пароль", "Увійти"]
    missing_marks = [mark for mark in required_marks if mark not in selector_blob]
    if missing_marks:
        print("FAIL: DSN login selectors miss expected marks:", missing_marks)
        return 1
    print("DSN login selector smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
