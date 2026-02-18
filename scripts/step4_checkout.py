import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

from step2_3_add_items_to_cart import parse_expected_items, verify_cart_or_raise

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")
BASE_URL = (os.getenv("BIOTUS_BASE_URL") or "https://opt.biotus.ua").rstrip("/")

ITEMS_RAW = (os.getenv("BIOTUS_ITEMS") or "").strip()
CART_VERIFY_ENABLED = os.getenv("BIOTUS_CART_VERIFY", "1") == "1"
CART_VERIFY_STRICT = os.getenv("BIOTUS_CART_VERIFY_STRICT", "1") == "1"
CART_VERIFY_SCREENSHOT = os.getenv("BIOTUS_CART_VERIFY_SCREENSHOT", "1") == "1"
CART_VERIFY_SAVE_OK_HTML = os.getenv("BIOTUS_CART_VERIFY_SAVE_OK_HTML", "0") == "1"


async def _try_direct_checkout(page) -> bool:
    try:
        await page.goto(f"{BASE_URL}/checkout", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
    except Exception:
        return False
    return "/checkout" in page.url


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

        if CART_VERIFY_ENABLED and ITEMS_RAW:
            expected = parse_expected_items(ITEMS_RAW)
            if expected:
                await verify_cart_or_raise(
                    page,
                    expected,
                    strict=CART_VERIFY_STRICT,
                    screenshot_on_fail=CART_VERIFY_SCREENSHOT,
                    save_ok_html=CART_VERIFY_SAVE_OK_HTML,
                    fail_prefix="step4_cart_verify_failed",
                    ok_html_name="step4_cart_verify_ok.html",
                )

        # После проверки мы можем оказаться на /checkout/cart; пробуем прямой переход на /checkout.
        if "/checkout" not in page.url and "/checkout/cart" in page.url:
            if await _try_direct_checkout(page):
                await page.screenshot(path=str(ART / "step4_after_checkout.png"), full_page=True)
                print(f"OK: checkout opened. Current URL: {page.url}")
                if not USE_CDP:
                    await browser.close()
                return

        await page.screenshot(path=str(ART / "step4_before_checkout_click.png"), full_page=True)

        # 1) Кнопка "Оформити" в модалке корзины/на cart page.
        candidates = [
            page.locator('#confirmButtons:visible button[title="Оформити"]:visible'),
            page.locator('#confirmButtons:visible button:has-text("Оформити"):visible'),
            page.get_by_role("button", name="Оформити"),
            page.locator('button[title="Оформити"]:visible'),
            page.locator('button:has-text("Оформити"):visible'),
            page.locator("a[href*='/checkout']:visible"),
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
            # Последний fallback: прямой переход на /checkout
            if await _try_direct_checkout(page):
                await page.screenshot(path=str(ART / "step4_after_checkout.png"), full_page=True)
                print(f"OK: checkout opened. Current URL: {page.url}")
                if not USE_CDP:
                    await browser.close()
                return
            raise RuntimeError('Не нашёл кнопку "Оформити" и не удалось открыть /checkout напрямую.')

        # 2) Кликаем и ждём переход именно на /checkout
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
                await btn.click(force=True, timeout=5000)
                await page.wait_for_timeout(350)

                try:
                    await page.wait_for_url("**/checkout**", timeout=45000)
                    break
                except PWTimeout:
                    if not await _btn_visible_enabled():
                        await page.wait_for_url("**/checkout**", timeout=45000)
                        break
                    raise

            except Exception as e:
                last_err = e
                await page.screenshot(path=str(ART / f"step4_click_attempt_{attempt}.png"), full_page=True)
                await page.wait_for_timeout(800)
                btn = page.locator('#confirmButtons:visible button[title="Оформити"]:visible').first

        if "/checkout" not in page.url:
            # Попытка прямого перехода как финальный fallback.
            if await _try_direct_checkout(page):
                await page.screenshot(path=str(ART / "step4_after_checkout.png"), full_page=True)
                print(f"OK: checkout opened. Current URL: {page.url}")
                if not USE_CDP:
                    await browser.close()
                return
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
