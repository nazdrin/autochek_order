import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import supplier3_run_order as sup3  # noqa: E402
from orchestrator import build_sup3_items  # noqa: E402


class Supplier3QuantitySafetyTests(unittest.TestCase):
    def setUp(self):
        self.order_30690 = {
            "products": [
                {"description": "100-11-5311185-20", "amount": 2},
                {"description": "100-97-2438284-20", "amount": 2},
                {"description": "2023-10-5654", "amount": 2},
            ]
        }
        self.expected = [
            sup3.Sup3Item("100-11-5311185-20", 2),
            sup3.Sup3Item("100-97-2438284-20", 2),
            sup3.Sup3Item("2023-10-5654", 2),
        ]

    def test_order_30690_preserves_all_three_quantities(self):
        self.assertEqual(
            "100-11-5311185-20:2,100-97-2438284-20:2,2023-10-5654:2",
            build_sup3_items(self.order_30690),
        )
        self.assertTrue(sup3._compare_sup3_quantities(
            self.expected,
            [{"sku": item.sku, "qty": 2} for item in self.expected],
        )["verified"])

    def test_comparison_reports_zinc_quantity_reduction_and_missing_lines(self):
        reduced = sup3._compare_sup3_quantities(
            self.expected,
            [
                {"sku": "100-11-5311185-20", "qty": 2},
                {"sku": "100-97-2438284-20", "qty": 1},
                {"sku": "2023-10-5654", "qty": 2},
            ],
        )
        self.assertFalse(reduced["verified"])
        self.assertEqual(
            [{"sku": "100-97-2438284-20", "expected_qty": 2, "actual_qty": 1}],
            reduced["qty_mismatches"],
        )
        missing = sup3._compare_sup3_quantities(self.expected, [])
        self.assertEqual(3, len(missing["missing"]))

    def test_diagnostic_stage_never_submits_or_updates_salesdrive(self):
        page = SimpleNamespace(url="https://dsn.ua/checkout/")
        add_result = {
            "cart_qty_checks": [
                {"sku": item.sku, "expected_qty": item.qty, "actual_qty": item.qty, "fixed_in_cart": False}
                for item in self.expected
            ],
            "items_summary": {item.sku: {"product_title": f"Product {i}"} for i, item in enumerate(self.expected)},
        }
        with (
            patch.object(sup3, "SUP3_ITEMS", "100-11-5311185-20:2,100-97-2438284-20:2,2023-10-5654:2"),
            patch.object(sup3, "SUP3_DIAGNOSTIC_ORDER_ID", "30690"),
            patch.object(sup3, "_diagnostic_cart_preflight", new=AsyncMock()),
            patch.object(sup3, "_add_items", new=AsyncMock(return_value=add_result)),
            patch.object(sup3, "_best_effort_close_popups", new=AsyncMock()),
            patch.object(sup3, "_read_checkout_quantities", new=AsyncMock(return_value={"verified": True, "checks": []})),
            patch.object(sup3, "_cleanup_diagnostic_cart", new=AsyncMock(return_value={"ok": True, "removed_skus": []})),
            patch.object(sup3, "_submit_checkout_order_and_get_number", new=AsyncMock()) as submit,
        ):
            result = asyncio.run(sup3._diagnose_quantity_stage(page))
        self.assertTrue(result["ok"])
        self.assertFalse(result["submitted"])
        self.assertFalse(result["salesdrive_updated"])
        submit.assert_not_awaited()

    def test_nonempty_cart_aborts_before_adding_products(self):
        page = SimpleNamespace(url="https://dsn.ua/")
        with (
            patch.object(sup3, "SUP3_ITEMS", "100-11-5311185-20:2"),
            patch.object(sup3, "SUP3_DIAGNOSTIC_ORDER_ID", "30690"),
            patch.object(
                sup3,
                "_diagnostic_cart_preflight",
                new=AsyncMock(side_effect=sup3.StageError("diagnostic_preflight", "DIAGNOSTIC_CART_NOT_EMPTY")),
            ),
            patch.object(sup3, "_add_items", new=AsyncMock()) as add_items,
            patch.object(sup3, "_cleanup_diagnostic_cart", new=AsyncMock()) as cleanup,
            patch.object(sup3, "_submit_checkout_order_and_get_number", new=AsyncMock()) as submit,
        ):
            with self.assertRaisesRegex(sup3.StageError, "DIAGNOSTIC_CART_NOT_EMPTY"):
                asyncio.run(sup3._diagnose_quantity_stage(page))
        add_items.assert_not_awaited()
        cleanup.assert_not_awaited()
        submit.assert_not_awaited()

    def test_diagnostic_checkout_mismatch_cleans_cart_without_submit(self):
        page = SimpleNamespace(url="https://dsn.ua/checkout/")
        add_result = {
            "cart_qty_checks": [
                {"sku": item.sku, "expected_qty": item.qty, "actual_qty": item.qty}
                for item in self.expected
            ],
            "items_summary": {item.sku: {"product_title": f"Product {i}"} for i, item in enumerate(self.expected)},
        }
        with (
            patch.object(sup3, "SUP3_ITEMS", "100-11-5311185-20:2,100-97-2438284-20:2,2023-10-5654:2"),
            patch.object(sup3, "SUP3_DIAGNOSTIC_ORDER_ID", "30690"),
            patch.object(sup3, "_diagnostic_cart_preflight", new=AsyncMock()),
            patch.object(sup3, "_add_items", new=AsyncMock(return_value=add_result)),
            patch.object(sup3, "_best_effort_close_popups", new=AsyncMock()),
            patch.object(
                sup3,
                "_read_checkout_quantities",
                new=AsyncMock(return_value={"verified": False, "qty_mismatches": [{"sku": "ZINC", "expected_qty": 2, "actual_qty": 1}]}),
            ),
            patch.object(sup3, "_cleanup_diagnostic_cart", new=AsyncMock(return_value={"ok": True})) as cleanup,
            patch.object(sup3, "_submit_checkout_order_and_get_number", new=AsyncMock()) as submit,
        ):
            with self.assertRaisesRegex(sup3.StageError, "Checkout quantities differ") as raised:
                asyncio.run(sup3._diagnose_quantity_stage(page))
        cleanup.assert_awaited_once()
        submit.assert_not_awaited()
        diagnostic_result = raised.exception.details["diagnostic_result"]
        self.assertTrue(diagnostic_result["cleanup"]["ok"])
        self.assertFalse(diagnostic_result["submitted"])

    def test_regular_checkout_quantity_mismatch_blocks_ttn_and_submit(self):
        page = SimpleNamespace(url="https://dsn.ua/checkout/")
        mismatch = {
            "verified": False,
            "qty_mismatches": [{"sku": "100-97-2438284-20", "expected_qty": 2, "actual_qty": 1}],
        }
        with (
            patch.object(sup3, "SUP3_ITEMS", "100-97-2438284-20:2"),
            patch.object(sup3, "SUP3_TTN", "20450000000000"),
            patch.object(sup3, "_is_logged_in", new=AsyncMock(return_value=(True, {}))),
            patch.object(sup3, "_best_effort_close_popups", new=AsyncMock()),
            patch.object(sup3, "_read_checkout_quantities", new=AsyncMock(return_value=mismatch)),
            patch.object(sup3, "_fill_ttn_input", new=AsyncMock()) as fill_ttn,
            patch.object(sup3, "_download_np_label_sup3") as download_label,
            patch.object(sup3, "_submit_checkout_order_and_get_number", new=AsyncMock()) as submit,
        ):
            with self.assertRaisesRegex(sup3.StageError, "CHECKOUT_QTY_MISMATCH") as raised:
                asyncio.run(sup3._checkout_ttn_stage(page))
        self.assertEqual("checkout_qty_mismatch", raised.exception.stage)
        fill_ttn.assert_not_awaited()
        download_label.assert_not_called()
        submit.assert_not_awaited()

    def test_checkout_row_reader_fails_closed_when_line_missing_or_ambiguous(self):
        page = object()
        item = [sup3.Sup3Item("ABC-1", 2)]
        with patch.object(sup3, "_find_checkout_row", new=AsyncMock(return_value=None)):
            with self.assertRaisesRegex(sup3.StageError, "CHECKOUT_ROW_NOT_FOUND"):
                asyncio.run(sup3._read_checkout_quantities(page, item))
        with patch.object(sup3, "_find_checkout_row", new=AsyncMock(side_effect=RuntimeError("CHECKOUT_ROW_AMBIGUOUS"))):
            with self.assertRaisesRegex(sup3.StageError, "CHECKOUT_ROW_AMBIGUOUS"):
                asyncio.run(sup3._read_checkout_quantities(page, item))


if __name__ == "__main__":
    unittest.main()
