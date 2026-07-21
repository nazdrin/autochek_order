import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "supplier2_run_order.py"
SPEC = importlib.util.spec_from_file_location("supplier2_run_order", MODULE_PATH)
sup2 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sup2
SPEC.loader.exec_module(sup2)


class Supplier2DeliveryValidationTests(unittest.TestCase):
    def recipient(self, *, branch_address: str) -> object:
        return sup2.Recipient(
            name="Тестовий Одержувач",
            phone_input="501234567",
            phone_source="0501234567",
            city_query="Кам'янка",
            city_geo_hints=("Черкаський", "Черкаська"),
            branch_number="2",
            branch_query="Відділення №2",
            branch_address=branch_address,
            delivery_kind="warehouse",
        )

    def test_screenshot_case_rejects_same_number_in_wrong_city(self) -> None:
        recipient = self.recipient(
            branch_address="Відділення №2 (до 10 кг): вул. Захисників України, 54"
        )
        result = sup2._validate_selected_delivery_text(
            selected_text="Відділення №2: вул. І. Сирка, 4-А",
            recipient=recipient,
            match_reason="branch_number",
            np_lookup={},
        )
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "branch_number_address_mismatch")

    def test_screenshot_case_accepts_exact_branch_and_address(self) -> None:
        recipient = self.recipient(
            branch_address="Відділення №2 (до 10 кг): вул. Захисників України, 54"
        )
        result = sup2._validate_selected_delivery_text(
            selected_text="Відділення №2: вул. Захисників України, 54",
            recipient=recipient,
            match_reason="branch_number",
            np_lookup={},
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["reason"], "branch_number_and_address")

    def test_city_hints_include_salesdrive_region_and_district(self) -> None:
        hints = sup2._delivery_city_geo_hints(
            {"areaName": "Черкаський р-н", "regionName": "Черкаська обл."},
            "Кам'янка",
        )
        self.assertIn("Черкаський р-н", hints)
        self.assertIn("Черкаська обл.", hints)

    def test_virtual_salesdrive_order_preserves_city_context(self) -> None:
        order = {
            "primaryContact": {"lName": "Тест", "fName": "Перевірка", "phone": ["0501234567"]},
            "shipping_address": "Відділення №2 (до 10 кг): вул. Захисників України, 54",
            "ord_delivery_data": [{
                "cityName": "Кам’янка",
                "areaName": "Черкаський р-н",
                "regionName": "Черкаська обл.",
                "branchNumber": 2,
                "address": "Відділення №2 (до 10 кг): вул. Захисників України, 54",
            }],
        }
        recipient = sup2._extract_recipient(order)
        self.assertEqual(recipient.city_query, "Кам’янка")
        self.assertIn("Черкаський р-н", recipient.city_geo_hints)
        self.assertIn("Черкаська обл.", recipient.city_geo_hints)


if __name__ == "__main__":
    unittest.main()
