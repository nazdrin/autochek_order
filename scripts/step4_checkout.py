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

        await page.wait_for_timeout(300)

        # Успех = мы на странице checkout
        if "/checkout" in page.url:
            await page.screenshot(path=str(ART / "step4_already_on_checkout.png"), full_page=True)
            print(f"OK: already on checkout. Current URL: {page.url}")
            if not USE_CDP:
                await browser.close()
            return

        await page.screenshot(path=str(ART / "step4_before_checkout_click.png"), full_page=True)

        # 1) Кнопка "Оформити" в модалке корзины
        # По твоему DOM: #confirmButtons ... button[title="Оформити"]
        candidates = [
            # Prefer visible button inside the visible confirmButtons container (modal can be re-rendered)
            page.locator('#confirmButtons:visible button[title="Оформити"]:visible'),
            page.locator('#confirmButtons:visible button:has-text("Оформити"):visible'),
            page.get_by_role("button", name="Оформити"),
            page.locator('button[title="Оформити"]:visible'),
            page.locator('button:has-text("Оформити"):visible'),
        ]

        btn = None
        for c in candidates:
            try:
                if await c.count():
                    btn = c.first
                    break
            except Exception:
                continue

        if btn is None:
            raise RuntimeError('Не нашёл кнопку "Оформити". Убедись, что модалка корзины открыта.')

        # 2) Кликаем и ждём переход именно на /checkout
        # В каскадном запуске сайт может дольше "думать": увеличиваем таймаут ожидания,
        # и повторяем клик только если видно, что первый клик не запустил переход.
        last_err = None

        async def _btn_visible_enabled() -> bool:
            try:
                if await btn.count() == 0:
                    return False
                if not await btn.is_visible():
                    return False
                if await btn.is_disabled():
                    return False
                return True
            except Exception:
                return False

        for attempt in range(1, 4):
            try:
                # Если кнопку перекрыл оверлей, force помогает.
                await btn.click(force=True, timeout=5000)

                # Даём UI начать переход (часто кнопка меняет состояние/цвет)
                await page.wait_for_timeout(350)

                try:
                    await page.wait_for_url("**/checkout**", timeout=45000)
                    break
                except PWTimeout:
                    # Если после клика кнопка уже исчезла или стала disabled — переход начался,
                    # просто подождём ещё, но повторно НЕ кликаем.
                    if not await _btn_visible_enabled():
                        await page.wait_for_url("**/checkout**", timeout=45000)
                        break

                    # Иначе кнопка всё ещё видима/активна — возможно клик не засчитался, пробуем повторить.
                    raise

            except Exception as e:
                last_err = e
                await page.screenshot(path=str(ART / f"step4_click_attempt_{attempt}.png"), full_page=True)
                await page.wait_for_timeout(800)
                # пере-найти кнопку (DOM мог смениться)
                btn = page.locator('#confirmButtons:visible button[title="Оформити"]:visible').first

        if "/checkout" not in page.url:
            await page.screenshot(path=str(ART / "step4_after_checkout_failed.png"), full_page=True)
            raise RuntimeError(f"Не удалось перейти на /checkout. Current URL: {page.url}. Last error: {last_err}")

        await page.wait_for_timeout(800)
        await page.screenshot(path=str(ART / "step4_after_checkout.png"), full_page=True)
        print(f"OK: checkout opened. Current URL: {page.url}")

        # В CDP режиме не закрываем Chrome
        if not USE_CDP:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())