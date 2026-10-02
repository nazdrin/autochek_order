import importlib.util
from pathlib import Path
import sys
import unittest

from playwright.async_api import async_playwright

PATH = Path(__file__).resolve().parents[1] / "scripts/supplier2_run_order.py"
SPEC = importlib.util.spec_from_file_location("sup2_phone_test", PATH)
sup2 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sup2
SPEC.loader.exec_module(sup2)


class Supplier2PhoneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.old_timeout = sup2.TIMEOUT_MS
        sup2.TIMEOUT_MS = 700
        sup2.DEBUG_ARTIFACTS = False
        self.recipient = sup2.Recipient(
            name="Тест Перевірка", phone_input="501234567", phone_source="0501234567",
            city_query="Київ", city_geo_hints=(), branch_number="1",
            branch_query="1", branch_address="", delivery_kind="warehouse",
        )

    async def asyncTearDown(self):
        sup2.TIMEOUT_MS = self.old_timeout
        await self.browser.close()
        await self.playwright.stop()

    async def widget(self, *, correct=True):
        await self.page.set_content('''
            <input id="callback-phone" type="tel">
            <input id="checkout-name">
            <input id="delivery-phone-quick" type="tel" style="display:none"
                data-relation-input="Quick[delivery_phone]">
            <input id="delivery-phone-recipient" type="tel"
                data-relation-input="Recipient[delivery_phone]">
            <input id="delivery-phone-recipient-hidden" type="hidden"
                name="Recipient[delivery_phone]">
            <input id="checkout-city">
        ''')
        await self.page.evaluate("""(correct) => {
            const visible = document.querySelector('#delivery-phone-recipient');
            visible.addEventListener('blur', () => {
                document.querySelector('#delivery-phone-recipient-hidden').value =
                    correct ? '+380' + visible.value : '+380671234567';
            });
        }""", correct)

    async def test_new_widget_uses_recipient_and_commits_full_number(self):
        await self.widget()
        result = await sup2._fill_recipient_fields(self.page, self.recipient)
        self.assertEqual(result['phone_payload'], '+380501234567')
        self.assertEqual(await self.page.locator('#callback-phone').input_value(), '')
        self.assertEqual(await self.page.locator('#delivery-phone-quick').input_value(), '')
        self.assertEqual(await self.page.evaluate('document.activeElement.id'), 'checkout-name')

    async def test_rejects_wrong_phone_even_with_matching_last_seven_digits(self):
        await self.widget(correct=False)
        with self.assertRaisesRegex(sup2.StageError, 'did not synchronize'):
            await sup2._fill_recipient_fields(self.page, self.recipient)

    async def test_legacy_phone_field_remains_supported(self):
        await self.page.set_content('<input id="checkout-name"><input id="checkout-phone">')
        result = await sup2._fill_recipient_fields(self.page, self.recipient)
        self.assertEqual(result['phone'], '501234567')

    async def test_checkout_idle_waits_for_cart_ajax(self):
        await self.page.evaluate('''() => {
            const cart = {Cart: {ajaxProcessing: 1}};
            window.AjaxCart = {getInstance: () => cart};
            setTimeout(() => {cart.Cart.ajaxProcessing = 0;}, 150);
        }''')
        result = await sup2._wait_for_checkout_idle(self.page)
        self.assertEqual(result['cartAjaxProcessing'], 0)


if __name__ == '__main__':
    unittest.main()
