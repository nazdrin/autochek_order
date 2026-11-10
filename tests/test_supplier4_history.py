import unittest
from unittest.mock import AsyncMock, patch

from scripts import supplier4_run_order as supplier4
from playwright.async_api import Error, async_playwright


TTN = "20450000000001"
HISTORY = "https://monsterlabdrop.com.ua/cabinet/orders"


class Supplier4HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        try:
            browser = await playwright.chromium.launch()
        except Error as exc:
            self.skipTest(f"Chromium unavailable: {str(exc).splitlines()[0]}")
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        for name, value in (
            ("SUP4_BASE_URL", HISTORY.rsplit("/cabinet", 1)[0]),
            ("SUP4_TIMEOUT_MS", 750),
            ("_debug", AsyncMock(return_value={})),
        ):
            override = patch.object(supplier4, name, value)
            override.start()
            self.addCleanup(override.stop)

    async def serve(self, result, *, legacy=False, apply=True, filtered=False):
        placeholder = "Номер замовлення або ТТН" if legacy else "Номер, ТТН чи ваш номер"
        handler = 'onkeydown="if(event.key===\'Enter\') location.search=\'q=\'+this.value"'
        controls = f'<button>{"Сьогодні" if filtered else "Будь-коли"}</button><button>Усі статуси</button>'
        initial = f'<input placeholder="Замовлення, ТТН, товар"><input placeholder="{placeholder}" {handler if apply else ""}>'

        async def route(request):
            body = result if "?q=" in request.request.url else initial
            await request.fulfill(content_type="text/html; charset=utf-8", body=f"<main>{controls}{body}</main>")

        await self.page.route("**/*", route)

    async def test_exact_ttn_across_pages_ignores_other_numbers(self):
        async def route(request):
            url = request.request.url
            def row(number, ttn):
                return (
                    f'<li><a href="/cabinet/orders/{number}">{number}</a>'
                    f'<span><span>{ttn}</span><button aria-label="Копіювати: Номер ТТН"></button></span></li>'
                )
            if "page=2" in url:
                body = row("D1236", TTN)
            elif "?q=" in url:
                body = row("D1234", TTN) + row("D1235", TTN + "0")
                body += f'<a aria-label="Сторінка 2 з 2" href="?q={TTN}&page=2">2</a>'
            else:
                body = (
                    '<input placeholder="Замовлення, ТТН, товар">'
                    '<input placeholder="Номер, ТТН чи ваш номер" '
                    'onkeydown="if(event.key===\'Enter\') location.search=\'q=\'+this.value">'
                )
            await request.fulfill(content_type="text/html; charset=utf-8", body=(
                f"<main><button>Будь-коли</button><button>Усі статуси</button>{body}</main>"
            ))

        await self.page.route("**/*", route)
        matches = await supplier4._find_orders_for_ttn(self.page, TTN)
        self.assertEqual([m["number"] for m in matches], ["D1234", "D1236"])

    async def test_legacy_field_and_confirmed_empty_result(self):
        await self.serve("<p>За цими умовами нічого не знайдено</p>", legacy=True)
        self.assertEqual(await supplier4._find_orders_for_ttn(self.page, TTN), [])

    async def test_search_must_be_applied(self):
        await self.serve("<p>За цими умовами нічого не знайдено</p>", legacy=True, apply=False)
        with self.assertRaisesRegex(supplier4.StageError, "HISTORY_SEARCH_NOT_APPLIED"):
            await supplier4._find_orders_for_ttn(self.page, TTN)

    async def test_filtered_empty_result_is_not_absence(self):
        await self.serve("<p>За цими умовами нічого не знайдено</p>", filtered=True)
        with self.assertRaisesRegex(supplier4.StageError, "HISTORY_SEARCH_FILTERED"):
            await supplier4._find_orders_for_ttn(self.page, TTN)

    async def test_run_stops_before_cart_on_unconfirmed_history(self):
        await self.serve("<p>Завантаження</p>")
        with (
            patch.object(supplier4, "SUP4_STAGE", "run"),
            patch.object(supplier4, "SUP4_TTN", TTN),
            patch.object(supplier4, "SUP4_ITEMS", "SKU-1:1"),
            patch.object(supplier4, "_login", AsyncMock()),
            patch.object(supplier4, "_save_storage_state", AsyncMock()),
            patch.object(supplier4, "_preflight_store_availability", AsyncMock()) as store,
            patch.object(supplier4, "_clear_cart", AsyncMock()) as cart,
            patch.object(supplier4, "async_playwright") as playwright,
        ):
            browser = AsyncMock()
            browser.new_context.return_value.new_page.return_value = self.page
            playwright.return_value.__aenter__.return_value.chromium.launch = AsyncMock(return_value=browser)
            with self.assertRaisesRegex(supplier4.StageError, "HISTORY_SEARCH_NOT_CONFIRMED"):
                await supplier4._run()
        store.assert_not_awaited()
        cart.assert_not_awaited()
