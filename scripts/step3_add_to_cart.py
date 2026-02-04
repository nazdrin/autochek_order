import asyncio
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")

# Если ты уже на странице товара — URL можно не задавать.
# Но на всякий случай можно хранить "последний товар" в env.
PRODUCT_URL = os.getenv("BIOTUS_PRODUCT_URL", "")  # опционально


async def main():
    async with async_playwright() as p:
        if USE_CDP:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            # На будущее. Сейчас используем CDP.
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

        # Если PRODUCT_URL задан — перейдём на него (иначе работаем с текущей открытой вкладкой)
        if PRODUCT_URL:
            await page.goto(PRODUCT_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

        await page.screenshot(path=str(ART / "step3_before_add.png"), full_page=True)

        # 1) Нажимаем кнопку "В кошик"
        # UI иногда рендерит кнопку как <button> или как <a>, плюс текст/пробелы могут отличаться.
        # Делаем устойчивый поиск + небольшие ожидания.
        await page.wait_for_timeout(800)

        # Чуть прокрутим вниз — на некоторых размерах окна кнопка может быть ниже зоны видимости.
        try:
            await page.mouse.wheel(0, 700)
        except Exception:
            pass
        await page.wait_for_timeout(300)

        add_re = re.compile(r"(в\s+кошик|до\s+кошика|у\s+кошик|в\s+корзин[уы]|add\s*to\s*cart)", re.I)

        # Сначала пробуем кнопки
        btn = page.get_by_role("button").filter(has_text=add_re).first

        # Если роль не определилась — пробуем ссылки
        if await btn.count() == 0:
            btn = page.get_by_role("link").filter(has_text=add_re).first

        # Фолбэк по CSS: любой элемент, похожий на кнопку
        if await btn.count() == 0:
            btn = page.locator(
                "button:has-text('В кошик'), a:has-text('В кошик'), .action.tocart, [data-role='tocart'], [type='submit']:has-text('В кошик')"
            ).first

        if await btn.count() == 0:
            raise RuntimeError('Не нашёл кнопку "В кошик". Пришли step3_before_add.png — уточним селектор.')

        # Прокрутка к элементу и клик (force=True на случай перекрытий/анимаций)
        try:
            await btn.scroll_into_view_if_needed()
        except Exception:
            pass

        await btn.click(force=True, timeout=30000)
        await page.wait_for_timeout(1200)

        await page.screenshot(path=str(ART / "step3_after_add_click.png"), full_page=True)

        # 2) Проверка успеха: появилось модальное окно "Ваш кошик" ИЛИ бейдж на иконке корзины
        cart_modal = page.get_by_text("Ваш кошик", exact=False)
        cart_badge = page.locator("a[href*='checkout/cart'], .minicart-wrapper .counter, .action.showcart .counter")

        ok = False
        if await cart_modal.count() > 0:
            ok = True
        elif await cart_badge.count() > 0:
            # если счётчик есть и видим
            try:
                if await cart_badge.first.is_visible():
                    ok = True
            except Exception:
                pass

        if not ok:
            # Не падаем, просто сообщаем — иногда UI меняется.
            print('Кнопка нажата, но не увидел явного признака корзины. Проверь step3_after_add_click.png.')
        else:
            print("OK: товар добавлен в корзину (есть признак корзины).")

        # В CDP режиме не закрываем Chrome
        if not USE_CDP:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())