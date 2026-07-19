import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orchestrator  # noqa: E402
from supplier6_run_order import _extract_prepay_total, _step6_select_payment, determine_payment_scenario  # noqa: E402


def order(payment_method, status_id=22, has_postpay=0):
    return {
        "id": 1001,
        "supplierlist": orchestrator.ORCH_SUP6_SUPPLIERLIST,
        "payment_method": payment_method,
        "statusId": status_id,
        "ord_delivery_data": [{"hasPostpay": has_postpay}],
        "products": [
            {"parameter": "SKU-1", "amount": 2, "price": "99.50"},
            {"parameter": "SKU-2", "amount": 1, "price": 10},
        ],
    }


class Supplier6PaymentScenarioTests(unittest.TestCase):
    def test_cod_is_selected_from_numeric_or_string_method(self):
        for payment_method in (20, "20"):
            with self.subTest(payment_method=payment_method):
                scenario = determine_payment_scenario(order(payment_method, status_id=1, has_postpay=1))
                self.assertEqual("cod", scenario["code"])
                self.assertEqual("Післяплата", scenario["payment_type"])
                self.assertTrue(scenario["has_postpay"])

    def test_prepay_is_selected_for_prom_and_bank_transfer(self):
        for payment_method in (54, "54", 16, "16"):
            with self.subTest(payment_method=payment_method):
                scenario = determine_payment_scenario(order(payment_method))
                self.assertEqual("prepay", scenario["code"])
                self.assertEqual("Передплата", scenario["payment_type"])
                self.assertFalse(scenario["has_postpay"])

    def test_method_is_authoritative_when_has_postpay_conflicts(self):
        scenario = determine_payment_scenario(order(54, has_postpay=1))
        self.assertEqual("prepay", scenario["code"])
        self.assertEqual({"expected": False, "actual": True}, scenario["has_postpay_mismatch"])

    def test_unknown_method_is_rejected(self):
        self.assertIsNone(determine_payment_scenario(order(999)))

    def test_prepay_total_comes_from_product_prices(self):
        self.assertEqual("209.00", str(_extract_prepay_total(order(54))))


class Supplier6QueueTests(unittest.TestCase):
    def test_queue_accepts_only_declared_status_payment_pairs(self):
        self.assertTrue(orchestrator.sup6_order_matches_payment_queue(order(20, status_id=1, has_postpay=1)))
        self.assertTrue(orchestrator.sup6_order_matches_payment_queue(order(54, status_id=22)))
        self.assertTrue(orchestrator.sup6_order_matches_payment_queue(order(16, status_id=22)))
        self.assertFalse(orchestrator.sup6_order_matches_payment_queue(order(20, status_id=22, has_postpay=1)))
        self.assertFalse(orchestrator.sup6_order_matches_payment_queue(order(54, status_id=18)))

    def test_filter_keeps_other_suppliers_and_drops_invalid_sup6(self):
        other = {"id": 2002, "supplierlist": 39, "statusId": 21}
        accepted, skipped = orchestrator.filter_sup6_payment_queue([order(54, status_id=18), order(20, status_id=1, has_postpay=1), other])
        self.assertEqual(1, skipped)
        self.assertEqual([1001, 2002], [item["id"] for item in accepted])


class _FakePaymentLocator:
    def __init__(self, page, text):
        self.page = page
        self.text = text

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.text in {"Передплата", "Післяплата"} else 0

    async def is_visible(self):
        return True

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def click(self, **_kwargs):
        self.page.selected = self.text

    async def get_attribute(self, name):
        if name == "class" and self.page.selected == self.text:
            return "selected"
        return ""

    async def is_checked(self):
        return self.page.selected == self.text


class _FakePaymentPage:
    def __init__(self):
        self.selected = ""

    def locator(self, selector):
        return _FakePaymentLocator(
            self,
            "Післяплата" if "Payment1" in selector or "value='1'" in selector or "Післяплата" in selector else "Передплата",
        )

    def get_by_text(self, text, **_kwargs):
        return _FakePaymentLocator(self, text)

    async def wait_for_timeout(self, _milliseconds):
        return None


class Supplier6PaymentSelectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_mocked_payment_selector_activates_each_expected_radio(self):
        page = _FakePaymentPage()
        self.assertTrue(await _step6_select_payment(page, "Передплата"))
        self.assertEqual("Передплата", page.selected)
        self.assertTrue(await _step6_select_payment(page, "Післяплата"))
        self.assertEqual("Післяплата", page.selected)


if __name__ == "__main__":
    unittest.main()
