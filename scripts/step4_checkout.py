import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")


async def main():
    async with async_playwright() as p:
        if USE_CDP:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

        await page.wait_for_timeout(500)
        await page.screenshot(path=str(ART / "step4_before_checkout_click.png"), full_page=True)

        # 1) Кнопка "Оформити" в модалке корзины
        btn = page.get_by_role("button", name="Оформити")
        if await btn.count() == 0:
            btn = page.locator('button:has-text("Оформити")')

        if await btn.count() == 0:
            raise RuntimeError('Не нашёл кнопку "Оформити". Убедись, что модалка корзины открыта.')

        # 2) Кликаем и ждём навигацию
        old_url = page.url
        await btn.first.click()

        # Пытаемся дождаться смены URL или появления признака страницы оформления
        try:
            await page.wait_for_url(lambda url: url != old_url, timeout=10000)
        except PWTimeout:
            pass

        # Часто страница оформления имеет характерные слова/элементы
        # Подождём загрузку
        await page.wait_for_timeout(2000)

        await page.screenshot(path=str(ART / "step4_after_checkout.png"), full_page=True)

        print(f"OK: clicked 'Оформити'. Current URL: {page.url}")

        # В CDP режиме не закрываем Chrome
        if not USE_CDP:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())