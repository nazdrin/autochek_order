# scripts/step9_confirm_order.py
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")
TIMEOUT_MS = int(os.getenv("BIOTUS_TIMEOUT_MS", "25000"))

ART = ROOT / "artifacts"
ART.mkdir(parents=True, exist_ok=True)


async def pick_checkout_page(context):
    pages = list(context.pages)
    for p in pages:
        try:
            if p.is_closed():
                continue
        except Exception:
            pass
        try:
            url = p.url or ""
        except Exception:
            url = ""
        if "opt.biotus" in url and "/checkout" in url:
            try:
                await p.bring_to_front()
            except Exception:
                pass
            return p

    # fallback: любая вкладка opt.biotus
    for p in pages:
        try:
            url = p.url or ""
        except Exception:
            url = ""
        if "opt.biotus" in url:
            try:
                await p.bring_to_front()
            except Exception:
                pass
            return p

    if pages:
        return pages[0]

    return await context.new_page()


async def _wait_not_disabled(locator, timeout_ms: int):
    """Ждём, пока у кнопки исчезнет disabled (и она станет кликабельной)."""
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_err = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await locator.count() == 0:
                await asyncio.sleep(0.2)
                continue
            el = locator.first
            # visible?
            try:
                await el.wait_for(state="visible", timeout=1500)
            except Exception:
                await asyncio.sleep(0.2)
                continue

            # disabled attr?
            disabled = await el.get_attribute("disabled")
            if disabled is None:
                return True
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.25)

    if last_err:
        raise RuntimeError(f"Кнопка не стала активной: {last_err}")
    raise RuntimeError("Кнопка не стала активной (disabled не исчез) за таймаут.")


async def _click_payment_confirm_if_shown(page, timeout_ms: int):
    """Если после подтверждения заказа появилось окно 'Оплата замовлення' — нажимаем 'Підтвердити'."""
    # В модалке кнопка часто имеет текст 'Підтвердити'
    confirm_btn = page.get_by_role("button", name="Підтвердити").locator(":visible")

    # Иногда роль может не определиться — fallback по тексту
    if await confirm_btn.count() == 0:
        confirm_btn = page.get_by_text("Підтвердити", exact=False).locator(":visible").first

    # Если кнопки нет — считаем, что модалка не появилась (или уже закрылась)
    if await confirm_btn.count() == 0:
        return False

    # Небольшой скрин перед кликом
    try:
        await page.screenshot(path=str(ART / "step9_2_payment_modal_before.png"), full_page=True)
    except Exception:
        pass

    # Клик по 'Підтвердити'
    try:
        await confirm_btn.first.click(timeout=min(timeout_ms, 8000))
    except PWTimeoutError:
        await confirm_btn.first.click(force=True)

    # Дать UI закрыть модалку
    await page.wait_for_timeout(800)

    try:
        await page.screenshot(path=str(ART / "step9_3_payment_modal_after.png"), full_page=True)
    except Exception:
        pass

    return True


async def main():
    async with async_playwright() as p:
        if USE_CDP:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await pick_checkout_page(context)
        else:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

        await page.wait_for_timeout(200)

        # Кнопка "Підтверджую замовлення" (основной способ)
        btn = page.get_by_role("button", name="Підтверджую замовлення").locator(":visible")

        # fallback: если роль не отработала — по тексту
        if await btn.count() == 0:
            btn = page.get_by_text("Підтверджую замовлення", exact=False).locator(":visible").first

        if await btn.count() == 0:
            await page.screenshot(path=str(ART / "step9_err_no_button.png"), full_page=True)
            raise RuntimeError('Не нашёл кнопку "Підтверджую замовлення" на checkout.')

        await page.screenshot(path=str(ART / "step9_0_before_click.png"), full_page=True)

        # Ждём, пока кнопка станет активной (перестанет быть disabled)
        await _wait_not_disabled(btn, TIMEOUT_MS)

        # Клик
        try:
            await btn.first.click(timeout=TIMEOUT_MS)
        except PWTimeoutError:
            # fallback: force click
            await btn.first.click(force=True)

        await page.wait_for_timeout(1200)
        await page.screenshot(path=str(ART / "step9_1_after_click.png"), full_page=True)

        # Если появилось окно 'Оплата замовлення' — подтверждаем оплату
        confirmed = False
        try:
            # ждем немного появления модалки (не весь TIMEOUT, чтобы не зависать)
            await page.wait_for_timeout(300)
            confirmed = await _click_payment_confirm_if_shown(page, TIMEOUT_MS)
        except Exception:
            confirmed = False

        if confirmed:
            print("OK: clicked 'Підтверджую замовлення' and confirmed payment modal ('Підтвердити').")
        else:
            print("OK: clicked 'Підтверджую замовлення'. (Payment modal not detected)")

        if not USE_CDP:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())