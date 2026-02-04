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

TTN = os.getenv("BIOTUS_TTN", "").strip()


def _looks_like_checkout(url: str, title: str) -> bool:
    u = (url or "").lower()
    t = (title or "").lower()
    return ("opt.biotus" in u and ("/checkout" in u or "checkout?" in u)) or ("оформлення" in t)


async def _pick_active_page(context):
    pages = list(context.pages)
    best = None
    for p in pages:
        try:
            if p.is_closed():
                continue
        except Exception:
            pass

        try:
            url = p.url
        except Exception:
            url = ""
        try:
            title = await p.title()
        except Exception:
            title = ""

        if _looks_like_checkout(url, title):
            best = p
            break
        if ("opt.biotus" in (url or "")) and best is None:
            best = p

    if best:
        try:
            await best.bring_to_front()
        except Exception:
            pass
        return best

    page = await context.new_page()
    await page.bring_to_front()
    return page


async def _connect(p):
    if USE_CDP:
        browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await _pick_active_page(context)
        return browser, context, page

    browser = await p.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    return browser, context, page


async def main():
    if not TTN:
        raise RuntimeError("Задай BIOTUS_TTN, например: BIOTUS_TTN=20400012345678")

    async with async_playwright() as p:
        browser, context, page = await _connect(p)

        # убедимся что мы на checkout
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(ART / "step7_0_before.png"), full_page=True)

        # Самый надежный селектор
        inp = page.locator("input#track-number:visible").first

        # Иногда на странице есть overlay/ленивая отрисовка — подождем пока поле станет интерактивным
        await inp.wait_for(state="visible", timeout=15000)
        await inp.scroll_into_view_if_needed()

        # Надежное заполнение: клик -> очистка -> fill -> blur (чтобы сработал onchange)
        await inp.click()
        await inp.fill("")
        await inp.fill(TTN)
        await page.keyboard.press("Tab")  # зафиксировать value/сработать change

        await page.wait_for_timeout(500)
        await page.screenshot(path=str(ART / "step7_1_after.png"), full_page=True)

        # Проверка что реально записалось
        val = await inp.input_value()
        if val.strip() != TTN:
            raise RuntimeError(f"TTN не зафиксировался. input_value='{val}' ожидал='{TTN}'")

        print(f"OK: TTN заполнен: {TTN}")

        # CDP browser не закрываем (чтобы не закрыть твой Chrome)
        if not USE_CDP:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())