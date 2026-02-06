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

 # Backward-compatible inputs:
# - legacy: BIOTUS_CITY_QUERY (+ optional BIOTUS_CITY_MUST_CONTAIN)
# - structured: BIOTUS_CITY_TYPE / BIOTUS_CITY_NAME / BIOTUS_CITY_AREA / BIOTUS_CITY_REGION
CITY_TYPE = (os.getenv("BIOTUS_CITY_TYPE") or "").strip()       # e.g. "с.", "м.", "смт"
CITY_NAME = (os.getenv("BIOTUS_CITY_NAME") or "").strip()       # e.g. "Калинівка"
CITY_AREA = (os.getenv("BIOTUS_CITY_AREA") or "").strip()       # e.g. "Київська"
CITY_REGION = (os.getenv("BIOTUS_CITY_REGION") or "").strip()   # e.g. "Вишгородський"

CITY_QUERY_LEGACY = (os.getenv("BIOTUS_CITY_QUERY") or "").strip()
MUST_CONTAIN_RAW = (os.getenv("BIOTUS_CITY_MUST_CONTAIN") or "").strip()

# IMPORTANT precedence:
# If structured CITY_NAME is provided, we ALWAYS use it for the search input,
# even if BIOTUS_CITY_QUERY is still present in .env (e.g. default/previous value like "Київ").
# Legacy BIOTUS_CITY_QUERY is used only when CITY_NAME is not provided.
if CITY_NAME:
    # IMPORTANT: SlimSelect search works reliably by the name only (e.g. "Калинівка").
    # Do NOT prepend CITY_TYPE (e.g. "с.") into the search input.
    CITY_QUERY = CITY_NAME
else:
    CITY_QUERY = CITY_QUERY_LEGACY

TIMEOUT_MS = int(os.getenv("BIOTUS_TIMEOUT_MS", "15000"))  # общий таймаут ожиданий


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("ё", "е")
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


def _contains_token(hay: str, token: str) -> bool:
    return norm(token) in norm(hay)


def _contains_all(hay: str, tokens: List[str]) -> bool:
    return all(_contains_token(hay, t) for t in tokens if t)


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


async def choose_best_option(
    options,
    query: str,
    city_type: str,
    city_name: str,
    area_name: str,
    region_name: str,
    must_tokens: List[str],
):
    """
    SlimSelect: input = фильтр, выбор = клик по .ss-option.

    Логика подбора (structured деградация):
    - всегда требуем совпадение по городу (CITY_NAME если задан, иначе query)
    - если заданы area и region: сначала пробуем city+area+region
      - если НЕ найдено (т.е. район/region не совпал) -> пробуем city+area
      - если всё ещё не найдено (т.е. область/area не совпала) -> пробуем city+region
      - иначе -> city only

    ВАЖНО: если город = м. Київ, то проверку делаем ТОЛЬКО по городу.
    """
    qn = norm(query)
    ct = norm(city_type) if city_type else ""  # soft preference (e.g. "с.")
    cn = norm(city_name) if city_name else ""
    if not cn:
        cn = qn

    an = norm(area_name) if area_name else ""
    rn = norm(region_name) if region_name else ""

    cnt = await options.count()
    if cnt == 0:
        return None

    def _norm_city_name(s: str) -> str:
        # Normalize city name only (no type, no punctuation noise)
        s = norm(s)
        # common prefixes in UA/RU for city type
        s = re.sub(r"^(м\.?\s+|с\.?\s+|смт\.?\s+)", "", s).strip()
        return s

    def _extract_city_from_option(txt: str) -> str:
        """Biotus city option is typically like: 'м. Харків / Харківська обл. / Харківський р-н'.
        We only want the city part (left side before first '/'), with type removed.
        """
        raw = (txt or "").strip()
        left = raw.split("/")[0].strip()  # e.g. 'м. Харків' or 'с. Харківці'
        return _norm_city_name(left)

    # If a structured CITY_NAME is provided, require an exact city-name match.
    # This prevents accidental matches like 'Харків' in 'Харківці'.
    cn_exact = _norm_city_name(city_name) if city_name else ""

    def city_match(txt: str) -> bool:
        if cn_exact:
            return _extract_city_from_option(txt) == cn_exact
        # Legacy / fallback: allow substring match
        t = norm(txt)
        return cn in t

    def type_pref(txt: str) -> bool:
        if not ct:
            return False
        # Prefer when the left city segment contains the requested type
        left = (txt or "").split("/")[0]
        return ct in norm(left)

    async def pick_best(predicate) -> Optional[object]:
        best = None
        best_score = -1
        for i in range(min(cnt, 200)):
            raw = await options.nth(i).inner_text()
            t = norm(raw)
            if predicate(t):
                score = 10
                # Bonus for exact city match when we can extract it (helps legacy too)
                try:
                    if _extract_city_from_option(raw) == _norm_city_name(cn):
                        score += 2
                except Exception:
                    pass
                if type_pref(t):
                    score += 1
                if score > best_score:
                    best_score = score
                    best = options.nth(i)
        return best

    # Special-case: Kyiv -> match by city only.
    # Accept both "київ" and "м київ" in inputs.
    if cn == "київ" or cn == "м київ" or cn.startswith("київ") or cn.startswith("м київ"):
        return await pick_best(lambda t: city_match(t))

    # Structured passes (deterministic деградация)
    if an and rn:
        # Pass 1: city + area + region
        best = await pick_best(lambda t: city_match(t) and _contains_all(t, [an, rn]))
        if best is not None:
            return best
        # Pass 2: район (region) не совпадает -> проверяем city + area
        best = await pick_best(lambda t: city_match(t) and _contains_all(t, [an]))
        if best is not None:
            return best
        # Pass 3: область (area) не совпадает -> проверяем city + region
        best = await pick_best(lambda t: city_match(t) and _contains_all(t, [rn]))
        if best is not None:
            return best
        # Pass 4: оба не совпали / нет точного совпадения -> city only
        return await pick_best(lambda t: city_match(t))

    if an:
        # Only area provided -> city + area
        best = await pick_best(lambda t: city_match(t) and _contains_all(t, [an]))
        if best is not None:
            return best
        return await pick_best(lambda t: city_match(t))

    if rn:
        # Only region provided -> city + region
        best = await pick_best(lambda t: city_match(t) and _contains_all(t, [rn]))
        if best is not None:
            return best
        return await pick_best(lambda t: city_match(t))

    # Legacy-only уточнение
    legacy_need = [norm(t) for t in must_tokens if t]
    if legacy_need:
        best = await pick_best(lambda t: city_match(t) and _contains_all(t, legacy_need))
        if best is not None:
            return best

    # Fallback: first city match (prefer CITY_TYPE when possible)
    return await pick_best(lambda t: city_match(t))


async def main():
    if not CITY_QUERY:
        raise RuntimeError(
            "Пустой ввод города. Укажи либо BIOTUS_CITY_QUERY, "
            "либо BIOTUS_CITY_NAME (и опционально BIOTUS_CITY_TYPE/AREA/REGION)."
        )

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
        cur_n = norm(current)

        # For validation we compare against the structured city name if provided,
        # otherwise against the (legacy) query string.
        city_token = CITY_NAME if CITY_NAME else CITY_QUERY

        structured_need: List[str] = []
        if CITY_AREA:
            structured_need.append(CITY_AREA)
        if CITY_REGION:
            structured_need.append(CITY_REGION)

        ok_city = norm(city_token) in cur_n

        # Special-case Kyiv: ignore область/район in validation (они часто расходятся с выпадашкой)
        city_norm = norm(city_token)
        is_kyiv = city_norm == "київ" or city_norm == "м київ" or city_norm.startswith("київ") or city_norm.startswith("м київ")

        ok_structured = True if is_kyiv else (_contains_all(cur_n, structured_need) if structured_need else True)
        ok_legacy = _contains_all(cur_n, must_tokens) if must_tokens and (not structured_need or is_kyiv) else True

        if ok_city and ok_structured and ok_legacy:
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
        # Fill should be driven by the actual city name (plus optional type) when structured inputs are used.
        # CITY_QUERY already follows the precedence rules above.
        await city_input.fill("")
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
        chosen = await choose_best_option(
            options,
            CITY_QUERY,
            CITY_TYPE,
            CITY_NAME,
            CITY_AREA,
            CITY_REGION,
            must_tokens,
        )
        if chosen is None:
            raise RuntimeError(
                "Опции есть, но не нашёл подходящую под параметры города. "
                "Проверь BIOTUS_CITY_NAME/AREA/REGION или (legacy) BIOTUS_CITY_MUST_CONTAIN."
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
            mode = "STRUCTURED" if (CITY_AREA or CITY_REGION) else ("ADVANCED" if must_tokens else "SIMPLE")
            print(f"OK: city selected ({mode}). query='{CITY_QUERY}', selected='{after}'")

        if not USE_CDP:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())