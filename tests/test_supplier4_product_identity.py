import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from supplier4_run_order import Sup4Item, _cart_qty_checks, _compare_order, _history_api_order_number, _history_order_number, _keycrm_order_number, _label_matches_ttn, _parse_items, _submit_not_ready_reason  # noqa: E402
from orchestrator import build_sup4_items  # noqa: E402


class Supplier4DropValidationTests(unittest.TestCase):
    def test_parse_items_normalizes_valid_input(self):
        self.assertEqual(_parse_items(" SOL-00947:2 ; GAV-0157 "), [Sup4Item("SOL-00947", 2), Sup4Item("GAV-0157", 1)])

    def test_invalid_or_duplicate_supplier_items_are_rejected(self):
        for raw in ("", "SOL-00947:0", "SOL-00947:1,SOL-00947:2"):
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeError):
                    _parse_items(raw)

    def test_compare_order_accepts_exact_skus_and_quantities(self):
        result = _compare_order([Sup4Item("SOL-00947", 2)], [{"sku": "sol-00947", "qty": 2, "price": 735}])
        self.assertTrue(result["verified"])

    def test_compare_order_reports_missing_extra_and_wrong_quantity(self):
        result = _compare_order([Sup4Item("SOL-00947", 2)], [{"sku": "SOL-00947", "qty": 1}, {"sku": "GAV-0157", "qty": 1}])
        self.assertFalse(result["verified"])
        self.assertEqual(result["extra"], ["gav-0157"])
        self.assertEqual(result["qty_mismatches"][0]["actual_qty"], 1)

    def test_compare_order_fails_when_cart_has_an_unordered_item(self):
        result = _compare_order([Sup4Item("SOL-00947", 1)], [{"sku": "SOL-00947", "qty": 1}, {"sku": "GAV-0157", "qty": 1}])
        self.assertFalse(result["verified"])
        self.assertEqual(result["extra"], ["gav-0157"])

    def test_cart_qty_checks_include_expected_and_actual_values(self):
        checks = _cart_qty_checks([Sup4Item("SOL-00947", 2)], [{"sku": "sol-00947", "qty": 2}])
        self.assertEqual(checks, [{"sku": "SOL-00947", "expected_qty": 2, "actual_qty": 2, "verified": True, "verified_stage": "cart"}])

    def test_label_ttn_is_verified_from_name_or_pdf_content(self):
        with tempfile.TemporaryDirectory() as directory:
            named = Path(directory) / "label-20450398117642.pdf"
            named.write_bytes(b"%PDF test")
            self.assertTrue(_label_matches_ttn(named, "20450398117642"))
            content = Path(directory) / "label.pdf"
            content.write_bytes(b"%PDF 20450398117642")
            self.assertTrue(_label_matches_ttn(content, "20450398117642"))
            self.assertFalse(_label_matches_ttn(content, "20450398117643"))

    def test_empty_pdf_cannot_be_accepted_by_filename_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "label-20450398117642.pdf"
            empty.write_bytes(b"")
            self.assertFalse(_label_matches_ttn(empty, "20450398117642"))

    def test_keycrm_number_is_accepted_only_from_success_message(self):
        self.assertEqual("20225", _keycrm_order_number("Замовлення 20225 у KeyCRM"))
        self.assertEqual("", _keycrm_order_number("Замовлення передаю"))

    def test_history_d_number_requires_matching_ttn(self):
        row = "D20250 06.08.2026 ТТН: 20451504851422"
        self.assertEqual("D20250", _history_order_number(row, "20451504851422"))
        self.assertEqual("", _history_order_number(row, "20451504750956"))

    def test_history_api_d_number_requires_exact_ttn(self):
        rows = [{"id": "D20253", "ttn": "20451504695822"}]
        self.assertEqual("D20253", _history_api_order_number(rows, "20451504695822"))
        self.assertEqual("", _history_api_order_number(rows, "20451504695821"))

    def test_disabled_submit_classifies_deposit_error(self):
        self.assertEqual("INSUFFICIENT_DEPOSIT", _submit_not_ready_reason("Не вистачає 633 ₴ на депозиті. Поповніть баланс."))
        self.assertEqual("SUBMIT_NOT_READY", _submit_not_ready_reason("Номер ТТН ще не введено"))

    def test_salesdrive_description_is_the_supplier_article(self):
        order = {"products": [{"description": "SOL-00947", "sku": "1016545", "amount": 2}]}
        self.assertEqual("SOL-00947:2", build_sup4_items(order))


if __name__ == "__main__":
    unittest.main()
