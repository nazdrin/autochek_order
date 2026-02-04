import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

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
        await page.screenshot(path=str(ART / "step5_before_drop_tab.png"), full_page=True)

        await page.wait_for_timeout(1200)

        # Кликаем вкладку "Для відправки дроп".
        # На странице оформления это может быть не role=button, поэтому используем текстовые и широкие селекторы.
        tab = page.get_by_text("Для відправки дроп", exact=False)

        if await tab.count() == 0:
            tab = page.locator("button, a, div, span").filter(has_text="Для відправки дроп")

        if await tab.count() == 0:
            # Доп. диагностика: сохраняем DOM-фрагмент в консоль по количеству совпадений основных локаторов
            print("DEBUG: no tab found by text. url=", page.url)
            raise RuntimeError('Не нашёл кнопку/вкладку "Для відправки дроп" (по тексту).')

        await tab.first.scroll_into_view_if_needed()
        await tab.first.click(force=True)

        await page.wait_for_timeout(800)

        await page.screenshot(path=str(ART / "step5_after_drop_tab.png"), full_page=True)

        print("OK: clicked 'Для відправки дроп'. Check step5_after_drop_tab.png")

        # В CDP режиме не закрываем Chrome
        if not USE_CDP:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())