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

AFTER_URL = os.getenv("BIOTUS_AFTER_LOGIN_URL")
SKU = os.getenv("BIOTUS_TEST_SKU", "MNW-532832")

STATE = ROOT / "artifacts" / "storage_state.json"
if not USE_CDP:
    if not STATE.exists():
        raise SystemExit("Нет storage_state.json. Сначала запусти step1_login.py")


async def main():
    async with async_playwright() as p:
        if USE_CDP:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(storage_state=str(STATE))
            page = await context.new_page()

        await page.goto(AFTER_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        await page.screenshot(path=str(ART / "step2_start.png"), full_page=True)

        # Верхний поиск (на скринах он в шапке)
        # Пробуем найти input рядом с лупой/placeholder "Пошук" / "Поиск"
        search_candidates = [
            'input[placeholder*="Пошук" i]',
            'input[placeholder*="Поиск" i]',
            'header input[type="text"]',
            'input[type="search"]',
        ]

        search = None
        for sel in search_candidates:
            loc = page.locator(sel)
            if await loc.count() > 0:
                search = loc.first
                break

        if search is None:
            raise RuntimeError("Не нашёл поле поиска в шапке. Нужен селектор по факту.")

        await search.click()
        await search.fill(SKU)
        await page.wait_for_timeout(800)

        # На скрине при вводе появляется выпадающий список с результатами.
        # Пытаемся кликнуть по результату, содержащему SKU.
        try:
            item = page.get_by_text(SKU, exact=False)
            await item.first.click(timeout=3000)
        except PWTimeout:
            # если нет выпадающего, попробуем Enter
            await search.press("Enter")

        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(ART / "step2_after_search_click.png"), full_page=True)

        mode = "CDP mode" if USE_CDP else "storage_state mode"
        print(f"OK: search attempted in {mode}, check artifacts screenshots")

        if not USE_CDP:
            await browser.close()
        else:
            # Не закрываем браузер в CDP режиме, т.к. это пользовательский Chrome
            pass


if __name__ == "__main__":
    asyncio.run(main())