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

# можно задавать:
# BIOTUS_BRANCH_QUERY="8"  или "Відділення №8" или "Пункт №4444" или "Пункт приймання-видачі: вул. ..."
BRANCH_QUERY = os.getenv("BIOTUS_BRANCH_QUERY", "8").strip()

# если хочешь дополнительно зафиксировать конкретный адрес/часть строки:
# BIOTUS_BRANCH_MUST_CONTAIN="Набережно-Хрещатицька"
# Можно несколько токенов через "," или ";"
BRANCH_MUST_CONTAIN = os.getenv("BIOTUS_BRANCH_MUST_CONTAIN", "").strip()

# Optional: force what type we want to pick from dropdown: "auto" | "branch" | "point"
# auto = infer from BIOTUS_BRANCH_QUERY (contains "пункт"/"відділення"), fallback = branch
BRANCH_KIND = os.getenv("BIOTUS_BRANCH_KIND", "auto").strip().lower()


# --- Helper: real mouse click for flaky widgets ---
async def _human_click(page, locator):
    """More reliable than locator.click(force=True) for flaky widgets.
    Forces a real mouse click at element center."""
    loc = locator.first
    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass

    # Try to get bounding box and click the center
    try:
        box = await loc.bounding_box()
    except Exception:
        box = None

    if box:
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        return

    # Fallback
    try:
        await loc.click()
    except Exception:
        try:
            await loc.click(force=True)
        except Exception:
            pass


def _looks_like_checkout(url: str, title: str) -> bool:
    u = (url or "").lower()
    t = (title or "").lower()
    return (
        "opt.biotus" in u
        and ("/checkout" in u or "checkout?" in u)
    ) or ("оформлення" in t) or ("checkout" in u)


async def _pick_active_page(context):
    """В CDP режиме Chrome может содержать много вкладок.
    Выбираем вкладку с checkout (или хотя бы opt.biotus).
    """
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

    try:
        page = await context.new_page()
        await page.bring_to_front()
        return page
    except Exception:
        return None


async def _connect(p):
    if USE_CDP:
        browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        page = await _pick_active_page(context)
        if not page:
            raise RuntimeError(
                "CDP: не удалось получить активную вкладку. "
                "Проверь, что Chrome запущен с remote debugging (порт 9222) и вкладка checkout открыта."
            )

        return browser, context, page

    browser = await p.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    return browser, context, page


def _branch_number_from_query(q: str) -> str:
    """Extract numeric branch/point id only when the query explicitly refers to a numbered
    branch/point (e.g. 'Відділення №8', 'Пункт №966') or when query is only digits.

    IMPORTANT: do NOT treat house numbers in address queries as branch numbers.
    """
    q_raw = (q or "").strip()
    if not q_raw:
        return ""

    # pure digits -> treat as number
    if re.fullmatch(r"\d+", q_raw):
        return q_raw

    m = re.search(r"(?:пункт|відділення)\s*№\s*(\d+)", q_raw, flags=re.IGNORECASE)
    if m:
        return m.group(1)

    return ""


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.replace("\u00a0", " ")
    # normalize dashes
    s = s.replace("–", "-").replace("—", "-")
    # remove punctuation that often differs in UI
    s = re.sub(r"[\.,:;()\[\]{}]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokenize_must_contain(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    parts = re.split(r"[;,]+", s)
    out: list[str] = []
    for p in parts:
        p = _norm(p)
        if p:
            out.append(p)
    return out


def _infer_branch_kind(branch_query: str) -> str:
    if BRANCH_KIND in {"branch", "point"}:
        return BRANCH_KIND
    qn = _norm(branch_query)
    if "пункт" in qn:
        return "point"
    if "відділен" in qn:
        return "branch"
    # default
    return "branch"


def _build_matcher(kind: str, query: str, must_contain: str):
    q_raw = query or ""
    qn = _norm(q_raw)
    must_tokens = _tokenize_must_contain(must_contain)

    num = _branch_number_from_query(q_raw)
    has_num = bool(num)

    # Address-style point query (e.g. 'Пункт приймання-видачі: вул. Дорошенка, 2'):
    # treat it as NOT numbered even if it contains house digits.
    addr_mode = (kind == "point") and (":" in (q_raw or "")) and (not has_num)

    strict_re = None

    if kind == "branch" and has_num:
        strict_re = re.compile(
            rf"^\s*Відділення\s*№\s*{re.escape(num)}(?!\d)",
            re.IGNORECASE,
        )

    elif kind == "point" and has_num:
        strict_re = re.compile(
            rf"^\s*Пункт\s*№\s*{re.escape(num)}(?!\d)",
            re.IGNORECASE,
        )

    # --- helper ---
    def norm_addr(s: str) -> str:
        s = _norm(s)
        s = s.replace("пункт приймання-видачі", "")
        s = s.replace("пункт приймання видачі", "")
        s = s.replace("пункт", "")
        s = s.replace("відділення", "")
        s = s.replace("№", " ")
        # normalize street words
        s = s.replace("вулиця", "вул")
        s = re.sub(r"\s+", " ", s).strip()
        return s

    addr_query = norm_addr(q_raw)

    # house number (last number in the query, if any)
    nums = re.findall(r"\d+", addr_query)
    house_num = nums[-1] if nums else None

    # strong tokens: ignore weak ones like "вул"
    raw_tokens = [t for t in re.split(r"\s+", addr_query) if t]
    addr_tokens = [t for t in raw_tokens if len(t) >= 4 and t not in {"вул", "пр", "пл"}]

    def matches(option_text: str) -> bool:
        if not option_text:
            return False

        tn = _norm(option_text)

        for t in must_tokens:
            if t not in tn:
                return False

        # строгий номер
        if strict_re is not None:
            return bool(strict_re.search(option_text))

        # --- ADDRESS POINT (ключевой кейс Дорошенка) ---
        if addr_mode:
            tn_addr = norm_addr(option_text)
            # Must contain all strong tokens (street name etc.)
            if addr_tokens and not all(tok in tn_addr for tok in addr_tokens):
                return False
            # And house number if present
            if house_num and house_num not in tn_addr:
                return False
            return True

        # обычный пункт
        if kind == "point":
            return qn in tn

        # отделение
        return qn in tn

    return matches, strict_re, num


async def _delivery_np_section(page):
    """Вернуть локатор секции доставки НП 'до відділення' (где поле отделения)."""
    sec = page.locator(
        'div.container_shipping_method.container_WarehouseWarehouse, '
        'div.container_WarehouseWarehouse, '
        'div.container_shipping_method:has-text("Нова пошта до відділення")'
    ).first

    try:
        if await sec.count() > 0:
            return sec
    except Exception:
        pass

    opt = page.get_by_text("Нова пошта до відділення", exact=False).first
    if await opt.count() > 0:
        root = opt.locator('xpath=ancestor::div[contains(@class,"container_shipping_method")][1]')
        if await root.count() > 0:
            return root.first
        root2 = opt.locator('xpath=ancestor::*[self::section or self::div][3]')
        if await root2.count() > 0:
            return root2.first

    delivery_title = page.get_by_text("Доставка", exact=False).first
    if await delivery_title.count() > 0:
        root = delivery_title.locator('xpath=ancestor::*[self::section or self::div][2]')
        if await root.count() > 0:
            return root.first

    return page


async def _ensure_np_branch_mode(page):
    sec = await _delivery_np_section(page)

    radio = sec.locator('input[type="radio"]:visible')

    try:
        if await radio.count() > 0:
            try:
                if await radio.first.is_checked():
                    return
            except Exception:
                pass
            await radio.first.check(force=True)
    except Exception:
        pass

    try:
        clickable = sec.locator(
            'label:has-text("Нова пошта до відділення"), div:has-text("Нова пошта до відділення")'
        ).first
        if await clickable.count() > 0:
            await _human_click(page, clickable)
        else:
            txt = sec.get_by_text("Нова пошта до відділення", exact=False).first
            if await txt.count() > 0:
                await _human_click(page, txt)
    except Exception:
        pass

    for _ in range(80):  # ~8 секунд
        try:
            if await sec.locator('div.ss-main').count() > 0:
                break
            if await sec.get_by_text("Введіть вулицю", exact=False).count() > 0:
                break
        except Exception:
            pass
        await page.wait_for_timeout(100)


# --- Helper: get the correct SlimSelect popup for NP branch ---
async def _get_branch_popup(page, inp=None):
    """Return the currently open SlimSelect popup for NP branch."""
    popup = page.locator(
        'div.ss-content:visible:has(input[type="search"][placeholder="Введіть вулицю або номер відділення"]), '
        'div.ss-content:visible:has(input[type="search"][aria-label="Введіть вулицю або номер відділення"])'
    ).first
    try:
        if await popup.count() > 0:
            return popup
    except Exception:
        pass

    if inp is not None:
        try:
            anc = inp.locator('xpath=ancestor::div[contains(@class,"ss-content")][1]').first
            if await anc.count() > 0:
                return anc
        except Exception:
            pass

    any_popup = page.locator('div.ss-content:visible').first
    try:
        if await any_popup.count() > 0:
            return any_popup
    except Exception:
        pass

    return None


async def _ensure_branch_dropdown_open(page, inp=None, popup=None):
    """Make sure the branch dropdown is open and options are present."""
    if popup is None:
        popup = await _get_branch_popup(page, inp=inp)

    opts = (
        popup.locator('div.ss-list .ss-option:visible')
        if popup is not None
        else page.locator('div.ss-content:visible div.ss-list .ss-option:visible')
    )
    try:
        if await opts.count() > 0:
            return True
    except Exception:
        pass

    sec = await _delivery_np_section(page)
    trigger = sec.locator(
        'div.ss-main:has-text("Введіть вулицю або номер відділення"), '
        'div.ss-main:has-text("Введіть вулицю"), '
        'div.ss-main'
    ).first

    try:
        if await trigger.count() > 0:
            await _human_click(page, trigger)
    except Exception:
        pass

    if inp is not None:
        try:
            await _human_click(page, inp)
        except Exception:
            pass

    for _ in range(80):  # ~8s
        try:
            if popup is None:
                popup = await _get_branch_popup(page, inp=inp)
                opts = (
                    popup.locator('div.ss-list .ss-option:visible')
                    if popup is not None
                    else page.locator('div.ss-content:visible div.ss-list .ss-option:visible')
                )
            if await opts.count() > 0:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(100)

    return False


async def _wait_suggestions_list(page, popup=None):
    if popup is None:
        popup = await _get_branch_popup(page)

    slim_opts = (
        popup.locator('div.ss-list .ss-option:visible')
        if popup is not None
        else page.locator('div.ss-content:visible div.ss-list .ss-option:visible')
    )

    select2_opts = page.locator('ul.select2-results__options:visible li.select2-results__option:visible')
    role_opts = page.locator('[role="listbox"]:visible [role="option"]:visible')

    candidates = [slim_opts, select2_opts, role_opts]

    for _ in range(120):  # ~12 секунд
        for c in candidates:
            try:
                if await c.count() > 0:
                    txt = (await c.first.inner_text()).strip()
                    if len(txt) >= 3:
                        return c
            except Exception:
                pass
        await page.wait_for_timeout(100)

    return None


async def _find_branch_input(page):
    sec = await _delivery_np_section(page)

    trigger = None

    cand = sec.locator(
        'div.ss-main:has-text("Введіть вулицю або номер відділення"), '
        'div.ss-main:has-text("Введіть вулицю")'
    ).first
    if await cand.count() > 0:
        trigger = cand

    if trigger is None:
        opener_text = sec.get_by_text("Введіть вулицю або номер відділення", exact=False).first
        if await opener_text.count() > 0:
            trigger = opener_text

    if trigger is None:
        cand2 = page.locator(
            'div.ss-main:has-text("Введіть вулицю або номер відділення"), '
            'div.ss-main:has-text("Введіть вулицю")'
        ).first
        if await cand2.count() > 0:
            trigger = cand2

    if trigger is None:
        return None

    try:
        await _human_click(page, trigger)
    except Exception:
        pass

    await page.wait_for_timeout(120)

    for _ in range(120):  # ~12 секунд
        popup = await _get_branch_popup(page)
        if popup is not None:
            try:
                inp = popup.locator('input[type="search"]').first
                if await inp.count() > 0 and await inp.is_visible():
                    return inp
            except Exception:
                pass
        await page.wait_for_timeout(100)

    return None


async def main():
    kind = _infer_branch_kind(BRANCH_QUERY)  # "branch" or "point"
    matcher, _strict_re, branch_no = _build_matcher(kind, BRANCH_QUERY, BRANCH_MUST_CONTAIN)

    async with async_playwright() as p:
        browser, context, page = await _connect(p)

        if page.url == "about:blank":
            picked = await _pick_active_page(context)
            if picked and picked.url != "about:blank":
                page = picked

        await page.wait_for_timeout(300)
        await page.screenshot(path=str(ART / "step6_0c_page_url.png"), full_page=True)

        await page.wait_for_timeout(500)
        await page.screenshot(path=str(ART / "step6_0_before_branch.png"), full_page=True)

        await _ensure_np_branch_mode(page)
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(ART / "step6_0a_after_np_selected.png"), full_page=True)

        inp = await _find_branch_input(page)
        if not inp:
            await page.screenshot(path=str(ART / "step6_err_no_input.png"), full_page=True)
            try:
                sec = await _delivery_np_section(page)
                print("[DEBUG] url=", page.url)
                print("[DEBUG] title=", await page.title())

                wh = page.locator('select[name="extension_attributes_warehouse_ref"]')
                print("[DEBUG] warehouse select count:", await wh.count())
                if await wh.count() > 0:
                    try:
                        outer = await wh.first.evaluate('e=>e.outerHTML')
                        print("[DEBUG] warehouse select outerHTML~", outer.replace("\n", " ")[:300], "...")
                    except Exception:
                        pass

                ss = sec.locator('div.ss-main')
                print("[DEBUG] ss-main in section:", await ss.count())

                fields = page.locator(
                    'input:visible, textarea:visible, select:visible, [contenteditable="true"]:visible, [role="textbox"]:visible'
                )
                n = await fields.count()
                print("[DEBUG] visible fields:", n)
                for i in range(min(n, 30)):
                    el = fields.nth(i)
                    tag = await el.evaluate('e=>e.tagName')
                    ph = await el.get_attribute('placeholder')
                    aria = await el.get_attribute('aria-label')
                    name = await el.get_attribute('name')
                    _id = await el.get_attribute('id')
                    cls = await el.get_attribute('class')
                    role = await el.get_attribute('role')
                    print(f"  [{i}] <{tag}> id={_id} name={name} role={role} placeholder={ph} aria={aria} class={cls}")
            except Exception as e:
                print("[DEBUG] dump failed:", e)

            raise RuntimeError(
                "Не смог открыть/найти поле поиска отделения/пункта. "
                "Открой страницу checkout, выбери 'Нова пошта до відділення' и убедись, что поле 'Введіть вулицю...' доступно. "
                "Смотри artifacts/step6_err_no_input.png"
            )

        await inp.scroll_into_view_if_needed()

        # Ветка 1: если это <select>, выбираем опцию напрямую.
        try:
            tag = (await inp.evaluate('e=>e.tagName')).upper()
        except Exception:
            tag = ""

        if tag == "SELECT":
            await page.screenshot(path=str(ART / "step6_0b_branch_select_found.png"), full_page=True)

            opt_loc = inp.locator("option")
            opt_count = await opt_loc.count()
            chosen_value = None
            chosen_label = None

            for i in range(min(opt_count, 500)):
                o = opt_loc.nth(i)
                try:
                    label = (await o.inner_text()).strip()
                except Exception:
                    continue
                if not label:
                    continue
                if not matcher(label):
                    continue
                try:
                    val = await o.get_attribute("value")
                except Exception:
                    val = None
                if val:
                    chosen_value = val
                    chosen_label = label
                    break

            if not chosen_value:
                await page.screenshot(path=str(ART / "step6_err_no_match.png"), full_page=True)
                raise RuntimeError(
                    f"Не нашёл подходящий пункт ({kind}) для запроса '{BRANCH_QUERY}' в <select>. "
                    f"Проверь step6_err_no_match.png"
                )

            await inp.select_option(value=chosen_value)
            await page.wait_for_timeout(500)
            await page.screenshot(path=str(ART / "step6_2_after_selected.png"), full_page=True)
            print(f"OK: пункт/отделение выбрано (select, {kind}). label='{chosen_label}'")

        else:
            # Ветка 2: кастомный инпут/комбобокс (SlimSelect)
            popup = await _get_branch_popup(page, inp=inp)

            # What we type into the search field:
            # - for numeric branch/point: type 'Відділення №<n>' or 'Пункт №<n>'
            # - for address-style point: type ONLY the address part (without 'Пункт приймання-видачі:'),
            #   then pick the FIRST suggestion.
            query_to_type = BRANCH_QUERY

            # numeric-only query -> keep old behavior
            if BRANCH_QUERY.strip().isdigit():
                if kind == "branch":
                    query_to_type = f"Відділення №{BRANCH_QUERY.strip()}"
                else:
                    query_to_type = f"Пункт №{BRANCH_QUERY.strip()}"

            # address-style point query: 'Пункт ...: <address>'
            addr_mode = (kind == "point") and (":" in (BRANCH_QUERY or "")) and (not _branch_number_from_query(BRANCH_QUERY))
            if addr_mode:
                addr_part = BRANCH_QUERY.split(":", 1)[1].strip()
                # normalize common prefixes
                addr_part = re.sub(r"^\s*вул\.?\s+", "вул. ", addr_part, flags=re.IGNORECASE)
                query_to_type = addr_part

            last_err = None

            for attempt in range(1, 4):
                try:
                    await _human_click(page, inp)
                    await page.wait_for_timeout(150)

                    try:
                        await inp.fill("")
                    except Exception:
                        await page.keyboard.press("Meta+A")
                        await page.keyboard.press("Backspace")

                    await inp.type(query_to_type, delay=25)
                    await page.wait_for_timeout(250)

                    popup = await _get_branch_popup(page, inp=inp)

                    opened = await _ensure_branch_dropdown_open(page, inp=inp, popup=popup)
                    if not opened:
                        raise RuntimeError("dropdown not opened")

                    opts = await _wait_suggestions_list(page, popup=popup)
                    if not opts:
                        raise RuntimeError("no suggestions")

                    await page.wait_for_timeout(250)

                    # пробуем ENTER (иногда выбирает подсказку)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(600)

                    sec = await _delivery_np_section(page)
                    selected_txt = ""
                    try:
                        selected_txt = (await sec.locator("div.ss-main").first.inner_text()).strip()
                    except Exception:
                        pass

                    ok = False
                    if selected_txt:
                        st = _norm(selected_txt)
                        # if still showing placeholder, nothing is selected
                        if "введіть" not in st and matcher(selected_txt):
                            ok = True

                    # Если ENTER не сработал — кликаем по правильной опции
                    if not ok:
                        count = await opts.count()
                        chosen = None

                        # Address-mode: just pick the first suggestion (what UI returned)
                        if addr_mode:
                            if count == 0:
                                raise RuntimeError("no suggestions")
                            chosen = opts.nth(0)
                        else:
                            for i in range(min(count, 80)):
                                item = opts.nth(i)
                                try:
                                    txt = (await item.inner_text()).strip()
                                except Exception:
                                    continue
                                if not txt:
                                    continue
                                if not matcher(txt):
                                    continue
                                chosen = item
                                break

                        if not chosen:
                            # debug: print first options to understand mismatch
                            try:
                                sample = []
                                for j in range(min(await opts.count(), 10)):
                                    t = (await opts.nth(j).inner_text()).strip()
                                    if t:
                                        sample.append(t)
                                if sample:
                                    print("[DEBUG] first options:")
                                    for s in sample:
                                        print("  -", s)
                            except Exception:
                                pass
                            raise RuntimeError("no match")

                        try:
                            await chosen.scroll_into_view_if_needed()
                        except Exception:
                            pass

                        await _human_click(page, chosen)
                        await page.wait_for_timeout(650)
                        # wait until options list collapses (selection applied)
                        for _ in range(40):  # ~4s
                            try:
                                pop2 = await _get_branch_popup(page, inp=inp)
                                if pop2 is None:
                                    break
                                if await pop2.locator('div.ss-list .ss-option:visible').count() == 0:
                                    break
                            except Exception:
                                break
                            await page.wait_for_timeout(100)

                        # повторная проверка — SlimSelect иногда не фиксирует выбор с первого раза
                        sec = await _delivery_np_section(page)
                        selected_txt2 = ""
                        try:
                            selected_txt2 = (await sec.locator("div.ss-main").first.inner_text()).strip()
                        except Exception:
                            pass

                        # In addr_mode we only require that placeholder is gone.
                        if addr_mode:
                            if not selected_txt2 or "введіть" in _norm(selected_txt2):
                                raise RuntimeError("selected value not applied")
                        else:
                            if not selected_txt2 or not matcher(selected_txt2):
                                raise RuntimeError("selected value not applied")

                    await page.screenshot(path=str(ART / "step6_2_after_selected.png"), full_page=True)
                    print(
                        f"OK: пункт/отделение выбрано ({kind}). query='{BRANCH_QUERY}', must='{BRANCH_MUST_CONTAIN}'"
                    )
                    last_err = None
                    break

                except Exception as e:
                    last_err = e
                    await page.screenshot(path=str(ART / f"step6_retry_{attempt}.png"), full_page=True)
                    await page.wait_for_timeout(500)

            if last_err is not None:
                await page.screenshot(path=str(ART / "step6_err_no_match.png"), full_page=True)
                raise RuntimeError(f"Не удалось стабильно выбрать пункт/отделение после 3 попыток: {last_err}")


if __name__ == "__main__":
    asyncio.run(main())