import sys
import tempfile
import unittest
from pathlib import Path
from PyPDF2 import PdfWriter
from PyPDF2.generic import DecodedStreamObject, DictionaryObject, NameObject

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from supplier4_run_order import Sup4Item, _cart_qty_checks, _classify_submit_rejection, _compare_order, _compare_order_details, _detail_order_items, _history_order_number, _keycrm_order_number, _label_matches_ttn, _label_status_received, _parse_items, _parse_money, _submit_not_ready_reason, _success_page_order_number, _supplier_error_text, _validated_ttn  # noqa: E402
from orchestrator import build_sup4_items, is_ambiguous_sup4_submit_failure  # noqa: E402


class Supplier4DropValidationTests(unittest.TestCase):
    @staticmethod
    def _write_pdf(path: Path, text: str, width: float = 100 * 72 / 25.4, height: float = 100 * 72 / 25.4):
        writer = PdfWriter()
        writer.add_blank_page(width=width, height=height)
        page = writer.pages[0]
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 10 10 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        with path.open("wb") as output:
            writer.write(output)

    def test_parse_items_normalizes_valid_input(self):
        self.assertEqual(_parse_items(" SOL-00947:2 ; GAV-0157 "), [Sup4Item("SOL-00947", 2), Sup4Item("GAV-0157", 1)])

    def test_invalid_or_duplicate_supplier_items_are_rejected(self):
        for raw in ("", "SOL-00947:0", "SOL-00947:1,SOL-00947:2"):
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeError):
                    _parse_items(raw)

    def test_money_is_parsed_as_minor_units_without_losing_decimals(self):
        self.assertEqual(20000, _parse_money("200,00 ₴"))
        self.assertEqual(123450, _parse_money("1 234,50 ₴"))
        self.assertEqual(123450, _parse_money("1.234,50 ₴"))

    def test_nova_poshta_ttn_requires_exactly_fourteen_digits(self):
        self.assertEqual("20450000000001", _validated_ttn("2045 0000 0000 01"))
        for value in ("", "1234567890123", "204500000000011"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                _validated_ttn(value)

    def test_existing_order_is_accepted_only_when_store_carrier_ttn_and_items_match(self):
        detail = {"store": "Старий кабінет", "carrier": "Нова пошта", "ttn": "20450000000001",
                  "number": "D23857", "label_received": True, "items": [{"sku": "CEN-21151", "qty": 2}]}
        expected = [Sup4Item("CEN-21151", 2)]
        self.assertTrue(_compare_order_details(expected, detail, detail["ttn"])["verified"])
        detail["items"][0]["qty"] = 1
        self.assertFalse(_compare_order_details(expected, detail, detail["ttn"])["verified"])
        detail["items"][0]["qty"] = 2
        detail["label_received"] = False
        self.assertFalse(_compare_order_details(expected, detail, detail["ttn"])["verified"])

    def test_detail_quantity_comes_only_from_each_product_row(self):
        rows = [("CEN-21151", "21st Century B-1 1 шт."), ("CEN-22251", "21st Century B-2")]
        self.assertEqual([{"sku": "CEN-21151", "qty": 1}, {"sku": "CEN-22251", "qty": 0}], _detail_order_items(rows))

    def test_label_confirmation_distinguishes_received_from_missing(self):
        self.assertTrue(_label_status_received("Завантажено"))
        self.assertTrue(_label_status_received("PDF отримано"))
        self.assertTrue(_label_status_received("Переглянути до 15 листопада"))
        self.assertFalse(_label_status_received("Не надходила"))

    def test_supplier_request_domain_matches_without_assuming_an_api_path(self):
        from supplier4_run_order import _is_supplier_host
        self.assertTrue(_is_supplier_host("https://monsterlabdrop.com.ua/checkout/submit-v2"))
        self.assertTrue(_is_supplier_host("https://api.monsterlabdrop.com.ua/orders/new"))
        self.assertFalse(_is_supplier_host("https://example.com/api/orders"))

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
            self._write_pdf(named, "20450398117642")
            self.assertTrue(_label_matches_ttn(named, "20450398117642"))
            content = Path(directory) / "label.pdf"
            self._write_pdf(content, "20450398117642")
            self.assertTrue(_label_matches_ttn(content, "20450398117642"))
            self.assertFalse(_label_matches_ttn(content, "20450398117643"))

    def test_label_name_alone_cannot_match_and_pdf_page_size_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            named = Path(directory) / "label-20450398117642.pdf"
            self._write_pdf(named, "wrong-ttn")
            self.assertFalse(_label_matches_ttn(named, "20450398117642"))
            wrong_size = Path(directory) / "wrong-size.pdf"
            self._write_pdf(wrong_size, "20450398117642", width=200, height=200)
            self.assertFalse(_label_matches_ttn(wrong_size, "20450398117642"))

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

    def test_success_page_number_is_read_from_number_order_field(self):
        self.assertEqual("D510001", _success_page_order_number(
            "Замовлення створено. # НОМЕР ЗАМОВЛЕННЯ D510001. За цим номером замовлення шукають менеджери."
        ))
        self.assertEqual("", _success_page_order_number("Замовлення створено, номер не показано"))
        self.assertEqual("", _success_page_order_number("Кошик"))

    def test_disabled_submit_classifies_deposit_error(self):
        self.assertEqual("INSUFFICIENT_DEPOSIT", _submit_not_ready_reason("Не вистачає 633 ₴ на депозиті. Поповніть баланс."))
        self.assertEqual("SUBMIT_NOT_READY", _submit_not_ready_reason("Номер ТТН ще не введено"))

    def test_submit_rejection_classifies_ukrainian_stock_shortage(self):
        result = _classify_submit_rejection(
            409,
            {"error": "Недостатньо товару: лише 2 шт в наявності"},
            [Sup4Item("SOL-00947", 4)],
        )
        self.assertIsNotNone(result)
        self.assertIn("INSUFFICIENT_STOCK", result["message"])
        self.assertIn("requested=SOL-00947:4", result["message"])
        self.assertEqual(2, result["available_qty"])

    def test_supplier_refusal_message_drops_contact_and_arbitrary_payload_fields(self):
        result = _classify_submit_rejection(
            409,
            {"error": "Недостатньо товару", "recipient": "Buyer", "phone": "+380 67 123 45 67",
             "ttn": "20450000000001"},
            [Sup4Item("CEN-21151", 2)],
        )
        self.assertIsNotNone(result)
        self.assertNotIn("380", result["supplier_message"])
        self.assertNotIn("20450000000001", result["supplier_message"])
        self.assertEqual("", _supplier_error_text({"recipient": "Buyer", "phone": "123"}))

    def test_submit_rejection_classifies_russian_and_english_stock_shortages(self):
        for message in ("Недостаточно товара на складе", "Product is out of stock"):
            with self.subTest(message=message):
                result = _classify_submit_rejection(422, {"message": message}, [Sup4Item("GAV-0157", 2)])
                self.assertIsNotNone(result)
                self.assertIn("requested=GAV-0157:2", result["message"])

    def test_submit_rejection_keeps_unknown_or_server_errors_ambiguous(self):
        items = [Sup4Item("SOL-00947", 4)]
        self.assertIsNone(_classify_submit_rejection(400, {"error": "Номер ТТН не заповнений"}, items))
        self.assertIsNone(_classify_submit_rejection(500, {"error": "Недостатньо товару"}, items))

    def test_only_unknown_submit_failures_are_ambiguous_for_salesdrive(self):
        self.assertFalse(is_ambiguous_sup4_submit_failure("submit_checkout_order", "INSUFFICIENT_STOCK: requested=SOL-00947:4"))
        self.assertTrue(is_ambiguous_sup4_submit_failure("submit_checkout_order", "SUBMIT_CONFIRMATION_NOT_FOUND"))
        self.assertFalse(is_ambiguous_sup4_submit_failure("add_items", "INSUFFICIENT_STOCK: requested=SOL-00947:4"))

    def test_salesdrive_description_is_the_supplier_article(self):
        order = {"products": [{"description": "SOL-00947", "sku": "1016545", "amount": 2}]}
        self.assertEqual("SOL-00947:2", build_sup4_items(order))


if __name__ == "__main__":
    unittest.main()
