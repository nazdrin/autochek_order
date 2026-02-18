from __future__ import annotations

from pathlib import Path

from step2_3_add_items_to_cart import _validate_cart, parse_cart_html, parse_expected_items

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
