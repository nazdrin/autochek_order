import asyncio
import os
from pathlib import Path
import re

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")

FULL_NAME = os.getenv("BIOTUS_FULL_NAME", "Бойко Александр")
# вводим без +380, т.к. маска уже содержит +38(0__) ...
PHONE_LOCAL = re.sub(r"\D+", "", os.getenv("BIOTUS_PHONE_LOCAL", "50 417 58 07"))



async def _first_visible(loc):
    """Return first visible element from a Locator or None."""
    try:
        n = await loc.count()
    except Exception:
        return None
    for i in range(min(n, 8)):
        item = loc.nth(i)
        try:
            if await item.is_visible():
                return item
        except Exception:
            continue
    return None


async def _set_value_js(page, element, value: str):
    """Set value via JS + dispatch events (works for many reactive forms)."""
    await page.evaluate(
        """(el, val) => {
            el.focus();
            el.value = '';
            el.value = val;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        element,
        value,
    )


async def fill_by_label_text(page, label_text: str, value: str) -> bool:
    """ 
    Устойчивое заполнение поля по подписи.
    Важно: на странице много скрытых input (country_id и т.п.).
    Поэтому:
      - берём только ВИДИМЫЕ поля
      - делаем несколько попыток
      - проверяем, что значение реально установилось
    """

    async def try_fill(target) -> bool:
        # 1) самый безопасный вариант — JS set + события
        try:
            await target.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            await target.click(timeout=1500)
        except Exception:
            pass

        try:
            await _set_value_js(page, await target.element_handle(), value)
        except Exception:
            # 2) fallback: locator.fill
            try:
                await target.fill(value, timeout=2500)
            except Exception:
                return False

        # Проверяем, что значение установилось
        try:
            current = (await target.input_value()).strip()
        except Exception:
            try:
                current = (await target.get_attribute("value") or "").strip()
            except Exception:
                current = ""

        return current == value.strip()

    # Кандидаты для "имени" (от более точных к более общим)
    candidates = []

    # 0) Stable checkout IDs (Biotus checkout)
    if "Ім'я" in label_text or "прізвище" in label_text:
        candidates.append(page.locator("#address-firstname:visible"))

    # A) get_by_label (если label реально связан с input)
    try:
        candidates.append(page.get_by_label(label_text, exact=False))
    except Exception:
        pass

    # B) xpath: рядом с текстом метки/подписи, но только не hidden
    safe_label = label_text.replace('"', "")
    candidates.append(
        page.locator(
            f"xpath=//*[contains(normalize-space(), \"{safe_label}\")]/following::input[not(@type='hidden')][1]"
        )
    )

    # C) css: видимый input в блоке формы, где встречается текст лейбла
    # (часто label + input внутри одного контейнера)
    candidates.append(
        page.locator(
            "xpath=//*[contains(normalize-space(), \"Ім'я\") and contains(normalize-space(), \"прізвище\")]/ancestor::*[self::div or self::section][1]//input[not(@type='hidden') and not(@type='submit')]"
        )
    )

    # Пытаемся несколько раз, потому что вкладка "дроп" часто триггерит перерендер
    for attempt in range(1, 4):
        for loc in candidates:
            target = await _first_visible(loc)
            if not target:
                continue
            ok = await try_fill(target)
            if ok:
                return True
        # ждём чуть-чуть, чтобы UI успокоился
        await page.wait_for_timeout(500)

    return False


async def main():
    async with async_playwright() as p:
        if USE_CDP:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[-1] if context.pages else await context.new_page()
        else:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

        # Wait until checkout recipient form inputs are rendered
        await page.locator("#address-firstname").first.wait_for(state="visible", timeout=30000)

        await page.wait_for_timeout(800)
        await page.screenshot(path=str(ART / "step5_2_before_fill.png"), full_page=True)

        # подождём, пока форма вкладки (дроп) полностью дорендерится
        await page.wait_for_timeout(400)
        await page.wait_for_load_state("domcontentloaded")

        # Имя
        ok_name = await fill_by_label_text(page, "Ім'я та прізвище", FULL_NAME)
        if not ok_name:
            raise RuntimeError("Не нашёл поле 'Ім'я та прізвище отримувача'.")

        await page.wait_for_timeout(300)

        # Телефон (маска: +38(0__) ___-__-__).
        # Сначала пробуем стабильный ID поля на checkout.
        phone = page.locator("#address-telephone:visible")

        if await phone.count() == 0:
            # fallback: видимый tel
            phone = page.locator('input[type="tel"]:visible')

        if await phone.count() == 0:
            # запасные варианты: видимый input с плейсхолдером +38 или (0
            phone = page.locator('input:visible[placeholder*="+38" i]')

        if await phone.count() == 0:
            phone = page.locator('input:visible[placeholder*="(0" i]')

        if await phone.count() == 0:
            phone = page.locator(
                'xpath=//*[contains(normalize-space(), "Номер телефону")]/ancestor::*[self::div or self::section][1]//input[not(@type="hidden") and not(@type="submit")][1]'
            )

        if await phone.count() == 0:
            raise RuntimeError("Не нашёл видимое поле 'Номер телефону'.")

        phone = phone.first
        await phone.wait_for(state="visible", timeout=30000)
        await phone.scroll_into_view_if_needed()
        await phone.click()

        # очистить и ввести локальные цифры (после 0)
        await page.keyboard.press("Meta+A")  # macOS
        await page.keyboard.type(PHONE_LOCAL)

        await page.wait_for_timeout(800)
        await page.screenshot(path=str(ART / "step5_2_after_fill.png"), full_page=True)

        print("OK: имя и телефон заполнены. Проверь step5_2_after_fill.png")

        # В CDP режиме не закрываем Chrome
        if not USE_CDP:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())