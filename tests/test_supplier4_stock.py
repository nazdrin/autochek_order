import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from supplier4_run_order import _stock_lower_bound


class StockTests(unittest.TestCase):
    def test_exact_stock(self):
        self.assertEqual(_stock_lower_bound('695,00 ₴\n18 шт.'), (18, False))

    def test_capped_stock(self):
        self.assertEqual(_stock_lower_bound('695,00 ₴\n25+ шт.'), (25, True))
        self.assertEqual(_stock_lower_bound('25 + шт.'), (25, True))

    def test_missing_stock_is_not_inferred_from_price(self):
        self.assertIsNone(_stock_lower_bound('695,00 ₴\nУ кошик'))
