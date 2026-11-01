"""Exercise SUP4's submit response handling with a local browser mock."""
from contextlib import asynccontextmanager
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supplier4_run_order as supplier4


class Supplier4SubmitResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_stock_response_is_captured_after_one_click(self):
        class Request:
            method = "POST"
            url = "https://monsterlabdrop.com.ua/checkout/submit-v2"

        class Response:
            request = Request()
            url = Request.url
            status = 409

            async def text(self):
                return json.dumps({"error": "Недостатньо товару: лише 2 шт в наявності"})

        class ResponseInfo:
            def __init__(self):
                self.response = None

            @property
            async def value(self):
                return self.response

        class Button:
            def __init__(self, page):
                self.page = page
                self.click_count = 0

            async def wait_for(self, **_kwargs):
                return None

            async def click(self):
                self.click_count += 1
                self.page.response = Response()

        class MissingHeading:
            first = None

            def __init__(self):
                self.first = self

            async def wait_for(self, **_kwargs):
                raise supplier4.PlaywrightTimeoutError("heading not present in this local fixture")

        class Page:
            def __init__(self):
                self.response = None
                self.button = Button(self)
                self.response_predicate = None

            def get_by_role(self, *_args, **_kwargs):
                if _args and _args[0] == "heading":
                    return MissingHeading()
                return self.button

            @asynccontextmanager
            async def expect_response(self, predicate, **_kwargs):
                info = ResponseInfo()
                self.response_predicate = predicate
                yield info
                if self.response is not None and predicate(self.response):
                    info.response = self.response

        page = Page()
        with tempfile.TemporaryDirectory() as directory:
            original_debug_dir = supplier4._debug_dir
            supplier4._debug_dir = lambda: Path(directory)
            try:
                with self.assertRaises(supplier4.StageError) as raised:
                    await supplier4._submit_and_confirm(
                        page, "20450000000001", [supplier4.Sup4Item("21655", 4)]
                    )
            finally:
                supplier4._debug_dir = original_debug_dir

        self.assertEqual(raised.exception.stage, "submit_checkout_order")
        self.assertIn("INSUFFICIENT_STOCK", str(raised.exception))
        self.assertEqual(page.button.click_count, 1)
        self.assertTrue(page.response_predicate(Response()))


if __name__ == "__main__":
    unittest.main()
