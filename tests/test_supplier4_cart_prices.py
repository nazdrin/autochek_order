import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from supplier4_run_order import _cart_line_total, _detail_order_items, StageError


class CartPriceTests(unittest.TestCase):
    def test_two_units_uses_total_and_keeps_quantity_separate(self):
        text = 'TEO-00746\n2\n2 286,00 ₴\n1 143,00 ₴ × 2'
        self.assertEqual(_cart_line_total(text, 2), 228600)

    def test_single_unit(self):
        self.assertEqual(_cart_line_total('SKU\n1\n946,00 ₴\n946,00 ₴ × 1', 1), 94600)

    def test_inconsistent_supplier_amounts_are_blocked(self):
        with self.assertRaises(StageError):
            _cart_line_total('2 000,00 ₴\n1 143,00 ₴ × 2', 2)

    def test_checkout_quantity_before_unit_price(self):
        self.assertEqual(_detail_order_items([('TEO-00746', 'Terra Origin 90 капсул\n\n2 × 1 143,00 ₴\n2 286,00 ₴')]), [{'sku': 'TEO-00746', 'qty': 2}])
