import asyncio
import os
import re
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")

CITY_QUERY = (os.getenv("BIOTUS_CITY_QUERY") or "").strip()
MUST_CONTAIN_RAW = (os.getenv("BIOTUS_CITY_MUST_CONTAIN") or "").strip()

TIMEOUT_MS = int(os.getenv("BIOTUS_TIMEOUT_MS", "15000"))  # общий таймаут ожиданий


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def split_tokens(s: str) -> List[str]:
    # BIOTUS_CITY_MUST_CONTAIN можно задавать через запятую или пробелы
    if not s:
        return []
    parts = re.split(r"[,;]+", s)
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


async def connect_page(pw):
    if USE_CDP:
        browser = await pw.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        return browser, context, page
    else:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        return browser, context, page


async def open_city_dropdown(page):
    """
    SlimSelect рендерит рядом с <select id="address-city"> контейнер <div class="ss-main">.
    Надёжно кликаем по нему.
    """
    select = page.locator("select#address-city")
    await select.wait_for(state="attached", timeout=TIMEOUT_MS)

    # Обычно ss-main стоит сразу после select (или рядом в DOM)
    ss_main = page.locator("select#address-city + .ss-main, select#address-city ~ .ss-main").first
    if await ss_main.count() == 0:
        # fallback: любой .ss-main в этом блоке получателя
        ss_main = page.locator(".ss-main:visible").first

    await ss_main.wait_for(state="visible", timeout=TIMEOUT_MS)
    await ss_main.click(force=True)

    # Ждём открытия контента (в SlimSelect это .ss-content)
    await page.locator(".ss-content:visible").first.wait_for(state="visible", timeout=TIMEOUT_MS)


async def get_selected_city_text(page) -> str:
    # выбранное значение SlimSelect показывает в .ss-single
    loc = page.locator("select#address-city + .ss-main .ss-single, .ss-main .ss-single").first
    if await loc.count() == 0:
        return ""
    try:
        return (await loc.inner_text()).strip()
    except Exception:
        return ""


async def find_city_search_input(page):
    # SlimSelect search input
    return page.locator(".ss-content:visible .ss-search input:visible").first


async def find_city_options(page):
    return page.locator(".ss-option:visible")


async def choose_best_option(options, query: str, must_tokens: List[str]):
    """
    Выбираем опцию:
    - если есть must_tokens: требуем их все
    - иначе: первая, где есть query
    """
    qn = norm(query)
    need = [norm(t) for t in must_tokens]

    cnt = await options.count()
    best = None

    # 1) Advanced: query + all must tokens
    if need:
        for i in range(min(cnt, 80)):
            txt = norm(await options.nth(i).inner_text())
            if qn in txt and all(t in txt for t in need):
                best = options.nth(i)
                break

    # 2) Simple: first containing query
    if best is None:
        for i in range(min(cnt, 80)):
            txt = norm(await options.nth(i).inner_text())
            if qn in txt:
                best = options.nth(i)
                break

    return best


async def main():
    if not CITY_QUERY:
        raise RuntimeError("BIOTUS_CITY_QUERY пустой. Пример: BIOTUS_CITY_QUERY='Бердичів'")

    must_tokens = split_tokens(MUST_CONTAIN_RAW)

    async with async_playwright() as pw:
        browser, context, page = await connect_page(pw)

        # ВАЖНО: мы предполагаем, что страница checkout уже открыта предыдущими шагами (CDP)
        # Поэтому goto тут не делаем.

        # 0) Скрин перед началом
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(ART / "step5_3_before_city.png"), full_page=True)

        # 1) Если уже выбрано и подходит — выходим
        current = await get_selected_city_text(page)
        if norm(CITY_QUERY) in norm(current) and (not must_tokens or all(norm(t) in norm(current) for t in must_tokens)):
            print(f"OK: city already selected. current='{current}'")
            if not USE_CDP:
                await browser.close()
            return

        # 2) Открываем dropdown
        await open_city_dropdown(page)

        # 3) Находим search input и вводим запрос
        city_input = await find_city_search_input(page)
        if await city_input.count() == 0:
            await page.screenshot(path=str(ART / "step5_3_no_city_input.png"), full_page=True)
            raise RuntimeError(
                "Не нашёл SlimSelect input для поиска города (.ss-search input). "
                "См. artifacts/step5_3_no_city_input.png"
            )

        await city_input.click(force=True)
        await city_input.fill(CITY_QUERY)
        await page.wait_for_timeout(300)  # SlimSelect debounce

        # 4) Ждём появления опций (они могут фильтроваться/подгружаться)
        options = await find_city_options(page)

        found = False
        for _ in range(30):  # ~15 сек (30 * 500ms)
            if await options.count() > 0:
                found = True
                break
            await page.wait_for_timeout(500)

        await page.screenshot(path=str(ART / "step5_3_city_dropdown.png"), full_page=True)

        if not found:
            raise RuntimeError(
                "После ввода города не появились опции SlimSelect (.ss-option). "
                "См. artifacts/step5_3_city_dropdown.png"
            )

        # 5) Выбираем лучшую опцию
        chosen = await choose_best_option(options, CITY_QUERY, must_tokens)
        if chosen is None:
            raise RuntimeError(
                "Опции есть, но не нашёл подходящую под CITY_QUERY / MUST_CONTAIN. "
                "Попробуй задать BIOTUS_CITY_MUST_CONTAIN, например: 'Житомирська' или 'Житомирська обл.'"
            )

        await chosen.click(force=True)

        # 6) Ждём, пока SlimSelect зафиксирует выбор (dropdown закроется)
        try:
            await page.wait_for_function(
                "() => !document.querySelector('.ss-list')",
                timeout=TIMEOUT_MS
            )
        except Exception:
            pass

        # проверяем выбранный текст из ss-single
        after = await get_selected_city_text(page)
        if not after:
            await page.wait_for_timeout(300)
            after = await get_selected_city_text(page)
        await page.screenshot(path=str(ART / "step5_3_after_city_selected.png"), full_page=True)

        if norm(CITY_QUERY) not in norm(after):
            print(f"WARN: city selection may not be fixed. after='{after}'. Check step5_3_after_city_selected.png")
        else:
            mode = "ADVANCED" if must_tokens else "SIMPLE"
            print(f"OK: city selected ({mode}). query='{CITY_QUERY}', selected='{after}'")

        if not USE_CDP:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())