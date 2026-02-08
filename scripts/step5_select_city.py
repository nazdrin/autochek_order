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
CITY_TYPE_EQUIV = (os.getenv("BIOTUS_CITY_TYPE_EQUIV") or "смт=с-ще=селище").strip()
CITY_STRICT_TYPE = (os.getenv("BIOTUS_CITY_STRICT_TYPE") or "0").strip() == "1"
CITY_STRICT_REGION = (os.getenv("BIOTUS_CITY_STRICT_REGION") or "1").strip() == "1"
CITY_STRICT_AREA = (os.getenv("BIOTUS_CITY_STRICT_AREA") or "1").strip() == "1"

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

    # Ждём открытия контента (в SlimSelect это .ss-content) — короткое ожидание
    await page.locator(".ss-content:visible").first.wait_for(state="visible", timeout=min(TIMEOUT_MS, 3000))


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


def _norm_city_type_for_compare(s: str) -> str:
    s = norm(s)
    if s.startswith("смт") or s.startswith("селище") or s.startswith("с-ще"):
        return "с-ще"
    if s.startswith("м"):
        return "м"
    if s.startswith("с"):
        return "с"
    return s


def _extract_city_type_from_selected(txt: str) -> str:
    # "м. Харків / Харківська обл." -> "м."
    left = (txt or "").split("/")[0].strip().lower()
    m = re.match(r"^(м\.?|с\.?|смт\.?|с-ще\.?|селище)", left)
    return m.group(1) if m else ""


def _city_type_matches(selected_text: str, city_type: str) -> bool:
    if not city_type:
        return True
    sel_type = _extract_city_type_from_selected(selected_text)
    if not sel_type:
        # UI can omit type; don't fail validation in this case
        return True
    return _norm_city_type_for_compare(sel_type) == _norm_city_type_for_compare(city_type)


def _parse_type_equiv(spec: str) -> List[set]:
    groups: List[set] = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("=") if p.strip()]
        if parts:
            groups.append(set(parts))
    return groups


def _type_equiv_match(t: str, expected: str, equiv_groups: List[set]) -> bool:
    if not expected:
        return True
    t_n = _norm_city_type_for_compare(t)
    e_n = _norm_city_type_for_compare(expected)
    if t_n == e_n:
        return True
    for g in equiv_groups:
        gn = {_norm_city_type_for_compare(x) for x in g}
        if t_n in gn and e_n in gn:
            return True
    return False


def _extract_option_type(txt: str) -> str:
    left = (txt or "").split("/")[0].strip().lower()
    m = re.match(r"^(м\.?|с\.?|смт\.?|с-ще\.?|селище)", left)
    return m.group(1) if m else ""


def _norm_area_region(s: str) -> str:
    s = norm(s)
    s = s.replace("область", "").replace("обл.", "").replace("обл", "")
    s = s.replace("район", "").replace("р-н", "").replace("рн", "")
    s = re.sub(r"[\\.,;()\\[\\]]", " ", s)
    s = re.sub(r"\\s+", " ", s).strip()
    return s


def _parse_city_option(txt: str) -> tuple[str, str, str, str]:
    """
    Parse option like:
      "с-ще Літин / Вінницька обл. / Вінницький р-н"
      "м. Харків / Харківська обл. / Харківський р-н"
    Returns: (type, name, area, region) all normalized.
    """
    raw = (txt or "").strip()
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    left = parts[0] if parts else ""
    opt_type = _extract_option_type(left)
    name_only = _norm_city_name_only(left)
    area = _norm_area_region(parts[1]) if len(parts) > 1 else ""
    region = _norm_area_region(parts[2]) if len(parts) > 2 else ""
    return _norm_city_type_for_compare(opt_type), name_only, area, region


def _norm_city_name_only(s: str) -> str:
    s = norm(s)
    s = re.sub(r"^(м\.?\s+|с\.?\s+|смт\.?\s+|с-ще\.?\s+|селище\s+)", "", s).strip()
    return s


def _city_selected_ok(selected_text: str, expected_name: str, expected_type: str) -> bool:
    if not selected_text:
        return False
    _t, name_only, _a, _r = _parse_city_option(selected_text)
    return _norm_city_name_only(expected_name) == name_only


async def _assert_final_city_selected(page, expected_name: str, expected_type: str) -> None:
    selected = await get_selected_city_text(page)
    if not selected:
        await page.screenshot(path=str(ART / "step5_err_city_not_applied.png"), full_page=True)
        raise RuntimeError(
            f"City not applied. Expected '{expected_name}' type '{expected_type}', got ''. "
            "See artifacts/step5_err_city_not_applied.png"
        )
    if not _city_selected_ok(selected, expected_name, expected_type):
        await page.screenshot(path=str(ART / "step5_err_city_not_applied.png"), full_page=True)
        raise RuntimeError(
            f"City not applied. Expected '{expected_name}' type '{expected_type}', got '{selected}'. "
            "See artifacts/step5_err_city_not_applied.png"
        )

async def _wait_options_visible(options, timeout_ms: int) -> bool:
    deadline = timeout_ms / 100
    for _ in range(int(deadline)):
        try:
            if await options.count() > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.1)
    return False


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

    Логика:
    - обязательный exact match по CITY_NAME (left part, без префикса типа)
    - AREA/REGION используются как фильтры по режимам A/B/C/D
    - CITY_TYPE влияет на score, "смт"~"с-ще"~"селище" эквивалентны
    """
    qn = norm(query)
    expected_name = _norm_city_name_only(city_name) if city_name else _norm_city_name_only(qn)
    expected_area = _norm_area_region(area_name) if area_name else ""
    expected_region = _norm_area_region(region_name) if region_name else ""
    equiv_groups = _parse_type_equiv(CITY_TYPE_EQUIV)

    cnt = await options.count()
    if cnt == 0:
        return None

    matches = []
    for i in range(min(cnt, 200)):
        raw = (await options.nth(i).inner_text()).strip()
        if not raw:
            continue
        opt_type, name_only, area_norm, region_norm = _parse_city_option(raw)
        if name_only != expected_name:
            continue
        matches.append((i, raw, opt_type, area_norm, region_norm))

    if not matches:
        raise RuntimeError("city not found")

    print(f"[step5] candidates with city match: {len(matches)}")

    # Mode A: city + area + region
    mode = "A"
    if expected_area and expected_region:
        cand = [m for m in matches if expected_area in m[3] and expected_region in m[4]]
    else:
        cand = []
    if not cand:
        # Mode B: city + area
        mode = "B"
        cand = [m for m in matches if expected_area and expected_area in m[3]] if expected_area else []
    if not cand:
        # Mode C: city + region
        mode = "C"
        cand = [m for m in matches if expected_region and expected_region in m[4]] if expected_region else []
    if not cand:
        # Mode D: city only
        mode = "D"
        cand = matches

    def score_item(item):
        _i, raw, opt_type, area_norm, region_norm = item
        score = 0
        if expected_area and expected_area in area_norm:
            score += 5
        if expected_region and expected_region in region_norm:
            score += 5
        if city_type:
            type_ok = _type_equiv_match(opt_type, city_type, equiv_groups)
            if type_ok:
                score += 2
                if raw.lower().split("/")[0].strip().startswith(opt_type):
                    score += 1
        return score

    cand.sort(key=lambda x: (-score_item(x), x[0]))
    idx, raw, _t, _a, _r = cand[0]
    print(
        f"[step5] mode={mode} expected city='{expected_name}', area='{expected_area}', region='{expected_region}', "
        f"type='{city_type}' -> picked='{raw}'"
    )
    return options.nth(idx)


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

        ok_legacy = _contains_all(cur_n, must_tokens) if must_tokens else True
        if _city_selected_ok(current, CITY_NAME if CITY_NAME else CITY_QUERY, CITY_TYPE) and ok_legacy:
            print(f"OK: city already selected. current='{current}'")
            if not USE_CDP:
                await browser.close()
            return

        # 2) Открываем dropdown + ввод (retry)
        options = None
        last_err = None
        for attempt in range(1, 4):
            try:
                await open_city_dropdown(page)
                city_input = await find_city_search_input(page)
                if await city_input.count() == 0:
                    await page.screenshot(path=str(ART / f"step5_retry_no_input_{attempt}.png"), full_page=True)
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(250)
                    raise RuntimeError("city input not found")

                await city_input.click(force=True)
                # Fill should be driven by the actual city name (plus optional type) when structured inputs are used.
                # CITY_QUERY already follows the precedence rules above.
                await city_input.fill("")
                await city_input.fill(CITY_QUERY)
                await page.wait_for_timeout(200)  # SlimSelect debounce (fast)

                options = await find_city_options(page)
                found = await _wait_options_visible(options, 2500)
                if not found:
                    await page.screenshot(path=str(ART / f"step5_retry_no_options_{attempt}.png"), full_page=True)
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(250)
                    raise RuntimeError("no options")

                last_err = None
                break
            except Exception as e:
                last_err = e

        if last_err is not None or options is None:
            await page.screenshot(path=str(ART / "step5_3_city_dropdown.png"), full_page=True)
            raise RuntimeError(
                "После ввода города не появились опции SlimSelect (.ss-option) "
                "после 3 попыток. См. artifacts/step5_3_city_dropdown.png"
            )

        # 5) Выбираем лучшую опцию
        try:
            chosen = await choose_best_option(
                options,
                CITY_QUERY,
                CITY_TYPE,
                CITY_NAME,
                CITY_AREA,
                CITY_REGION,
                must_tokens,
            )
        except RuntimeError as e:
            await page.screenshot(path=str(ART / "step5_err_city_not_applied.png"), full_page=True)
            raise
        if chosen is None:
            raise RuntimeError(
                "Опции есть, но не нашёл подходящую под параметры города. "
                "Проверь BIOTUS_CITY_NAME/AREA/REGION или (legacy) BIOTUS_CITY_MUST_CONTAIN."
            )

        await chosen.click(force=True)

        # 6) Ждём, пока SlimSelect зафиксирует выбор (dropdown закроется) — быстро и надёжно
        try:
            await page.locator(".ss-content:visible").first.wait_for(state="hidden", timeout=3000)
        except Exception:
            # Fallback: small pause to let UI settle
            await page.wait_for_timeout(200)

        # проверяем выбранный текст из ss-single
        after = ""
        for _ in range(25):  # ~2.5s
            after = await get_selected_city_text(page)
            if after and norm(CITY_QUERY) in norm(after):
                break
            await page.wait_for_timeout(100)
        await page.screenshot(path=str(ART / "step5_3_after_city_selected.png"), full_page=True)

        # Hard assert: must match final city + type
        try:
            await _assert_final_city_selected(page, CITY_NAME if CITY_NAME else CITY_QUERY, CITY_TYPE)
        except RuntimeError as e:
            print(f"ERROR: city not applied. {e}")
            raise

        mode = "STRUCTURED" if (CITY_AREA or CITY_REGION) else ("ADVANCED" if must_tokens else "SIMPLE")
        print(f"OK: city selected final='{after}' ({mode})")

        if not USE_CDP:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
