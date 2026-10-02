"""Exercise SUP4's response subscription with real Python Playwright.

All supplier requests are fulfilled locally; no real order is submitted.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supplier4_run_order as supplier4


class Supplier4SubmitResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_stock_response_is_captured_after_one_click(self):
        requests = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                async def serve(route):
                    if route.request.method == "POST":
                        requests.append(route.request.url)
                        await route.fulfill(
                            status=409,
                            content_type="application/json",
                            body=json.dumps({"error": "Недостатньо товару: лише 2 шт в наявності"}),
                        )
                    else:
                        await route.fulfill(
                            content_type="text/html; charset=utf-8",
                            body='<button onclick="fetch(\'/api/orders\', {method: \'POST\'})">'
                                 'Оформити замовлення</button>',
                        )

                await page.route("https://sup4.test/**", serve)
                await page.goto("https://sup4.test/")
                with patch.object(supplier4, "_debug", AsyncMock(return_value={})):
                    with self.assertRaises(supplier4.StageError) as raised:
                        await supplier4._submit_and_confirm(
                            page, "20450000000001", [supplier4.Sup4Item("21655", 4)]
                        )
                self.assertEqual(raised.exception.stage, "submit_checkout_order")
                self.assertIn("INSUFFICIENT_STOCK", str(raised.exception))
                self.assertEqual(requests, ["https://sup4.test/api/orders"])
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
