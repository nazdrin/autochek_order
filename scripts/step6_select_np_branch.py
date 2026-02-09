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
    # normalize special NP point text: remove weight brackets
    q_raw = re.sub(r"\s*\(до [^)]+\)\s*", " ", q_raw).strip()
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
            rf"^\s*(?:Мобільне\s+)?Відділення\s*№\s*{re.escape(num)}(?!\d)",
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

        tn_raw = re.sub(r"\s*\(до [^)]+\)\s*", " ", option_text, flags=re.IGNORECASE)
        tn_clean = _norm(tn_raw)

        for t in must_tokens:
            if t not in tn_clean:
                return False

        # строгий номер
        if strict_re is not None:
            return bool(strict_re.search(option_text))

        # --- ADDRESS POINT (ключевой кейс Дорошенка) ---
        if addr_mode:
            tn_addr = norm_addr(tn_raw)
            # Must contain all strong tokens (street name etc.)
            if addr_tokens and not all(tok in tn_addr for tok in addr_tokens):
                return False
            # And house number if present
            if house_num and house_num not in tn_addr:
                return False
            return True

        # обычный пункт
        if kind == "point":
            return qn in tn_clean

        # отделение
        return qn in tn_clean

    return matches, strict_re, num


def _normalize_addr_query(q_raw: str) -> str:
    q = (q_raw or "").strip()
    q = re.sub(r"\s*\(до [^)]+\)\s*", " ", q, flags=re.IGNORECASE).strip()
    return q


def _addr_matches(option_text: str, addr_query: str) -> bool:
    """Match address by strong tokens + house number."""
    if not option_text or not addr_query:
        return False

    def norm_addr(s: str) -> str:
        s = _norm(s)
        s = s.replace("пункт приймання-видачі", "")
        s = s.replace("пункт приймання видачі", "")
        s = s.replace("пункт", "")
        s = s.replace("відділення", "")
        s = s.replace("№", " ")
        s = s.replace("вулиця", "вул")
        s = re.sub(r"\s+", " ", s).strip()
        return s

    q = norm_addr(addr_query)
    t = norm_addr(option_text)

    nums = re.findall(r"\d+", q)
    house_num = nums[-1] if nums else None

    raw_tokens = [tok for tok in re.split(r"\s+", q) if tok]
    addr_tokens = [t for t in raw_tokens if len(t) >= 4 and t not in {"вул", "пр", "пл"}]

    if addr_tokens and not all(tok in t for tok in addr_tokens):
        return False
    if house_num and house_num not in t:
        return False
    return True


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

    for _ in range(25):  # ~2.5 секунд
        try:
            if await sec.locator('div.ss-main').count() > 0:
                break
            if await sec.get_by_text("Введіть вулицю", exact=False).count() > 0:
                break
        except Exception:
            pass
        await page.wait_for_timeout(100)


async def _wait_np_section_idle(page, sec, timeout_ms: int = 2500):
    # Try to wait for common spinners/preloaders/overlays to disappear (best-effort)
    busy = sec.locator(
        ".loading:visible, .loader:visible, .spinner:visible, .preloader:visible, "
        "[class*=\"loading\"]:visible, [class*=\"spinner\"]:visible, "
        ".overlay:visible, .modal-backdrop:visible, .backdrop:visible"
    )
    try:
        if await busy.count() > 0:
            await busy.first.wait_for(state="hidden", timeout=timeout_ms)
            return
    except Exception:
        pass
    await page.wait_for_timeout(200)


async def _find_np_ss_main(sec):
    preferred = sec.locator(
        'div.ss-main:has-text("Введіть вулицю або номер відділення"), '
        'div.ss-main:has-text("Введіть вулицю")'
    )
    try:
        if await preferred.count() > 0:
            return preferred.first, await preferred.count()
    except Exception:
        pass

    any_main = sec.locator("div.ss-main")
    try:
        cnt = await any_main.count()
    except Exception:
        cnt = 0
    return (any_main.first if cnt > 0 else None), cnt


async def _popup_ok(popup) -> bool:
    try:
        if await popup.count() == 0:
            return False
        if not await popup.is_visible():
            return False
        has_list = await popup.locator("div.ss-list").count() > 0
        has_input = await popup.locator("div.ss-search input, input[type='search']").count() > 0
        return bool(has_list and has_input)
    except Exception:
        return False


async def _get_popup_for_ss_main(page, ss_main):
    if ss_main is None:
        return None, False, False

    # sibling ss-content
    try:
        sib = ss_main.locator("xpath=following-sibling::div[contains(@class,'ss-content')]").first
        if await _popup_ok(sib):
            return sib, True, True
    except Exception:
        pass

    # parent siblings up to 3 levels
    parent = ss_main
    for _ in range(3):
        try:
            parent = parent.locator("xpath=..")
            sib = parent.locator("xpath=following-sibling::div[contains(@class,'ss-content')]").first
            if await _popup_ok(sib):
                return sib, True, True
        except Exception:
            pass

    # fallback: last visible popup, prefer one with options
    try:
        visible = page.locator("div.ss-content:visible")
        vcnt = await visible.count()
        if vcnt > 0:
            for i in range(vcnt):
                cand = visible.nth(i)
                try:
                    if await cand.locator("div.ss-option:visible").count() > 0 and await _popup_ok(cand):
                        return cand, True, False
                except Exception:
                    pass
            last = visible.nth(vcnt - 1)
            if await _popup_ok(last):
                return last, True, False
    except Exception:
        pass

    return None, False, False


async def _ensure_np_dropdown_open(page, ss_main, timeout_ms: int = 2500):
    if ss_main is None:
        return None, False, False
    try:
        await _human_click(page, ss_main)
    except Exception:
        try:
            await ss_main.click(force=True)
        except Exception:
            return None, False, False

    checks = max(5, int(timeout_ms / 100))
    for _ in range(checks):
        popup, ok, sibling = await _get_popup_for_ss_main(page, ss_main)
        if ok and popup is not None:
            return popup, True, sibling
        await page.wait_for_timeout(100)
    return None, False, False


async def _get_np_search_input(popup):
    if popup is None:
        return None
    return popup.locator("div.ss-search input, input[type='search']").first


async def _wait_options_visible(page, options, timeout_ms: int = 2500) -> bool:
    checks = max(5, int(timeout_ms / 100))
    for _ in range(checks):
        try:
            if await options.count() > 0:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(100)
    return False


def _selected_text_ok(selected_txt: str, kind: str, num: str, must_tokens: list[str]) -> bool:
    if not selected_txt:
        return False
    st = _norm(selected_txt)
    if "введіть" in st:
        return False
    # must_contain tokens
    for t in must_tokens:
        if t and t not in st:
            return False
    # strict numeric validation for branches
    if kind == "branch" and num:
        # require 'Відділення №<n>' and avoid address numbers like 9/2
        strict = re.compile(rf"^\s*Відділення\s*№\s*{re.escape(num)}(?!\d)", re.IGNORECASE)
        if strict.search(selected_txt):
            return True
        return False
    return True


def _is_placeholder(text: str) -> bool:
    return "введіть" in _norm(text)


async def _get_selected_text(sec, ss_main=None) -> str:
    if ss_main is not None:
        loc = ss_main.locator("div.ss-single").first
    else:
        loc = sec.locator("div.ss-main .ss-single, .ss-single").first
    try:
        if await loc.count() == 0:
            return ""
        return (await loc.inner_text()).strip()
    except Exception:
        return ""


async def main():
    kind = _infer_branch_kind(BRANCH_QUERY)  # "branch" or "point"
    matcher, strict_re, branch_no = _build_matcher(kind, BRANCH_QUERY, BRANCH_MUST_CONTAIN)
    TRY_ENTER = os.getenv("BIOTUS_BRANCH_TRY_ENTER", "0") == "1"

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

        sec = await _delivery_np_section(page)
        last_err = None
        last_screen = None

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

        # mode detection for points
        has_colon = ":" in (BRANCH_QUERY or "")
        has_num = bool(re.search(r"№\s*\d+", BRANCH_QUERY or ""))
        combined_mode = has_colon and has_num
        addr_mode = has_colon and not has_num
        numeric_mode = not addr_mode and not combined_mode

        expected_num = _branch_number_from_query(BRANCH_QUERY)
        expected_addr = ""

        if addr_mode or combined_mode:
            addr_part = BRANCH_QUERY.split(":", 1)[1].strip()
            addr_part = _normalize_addr_query(addr_part)
            # normalize common prefixes
            addr_part = re.sub(r"^\s*вул\.?\s+", "вул. ", addr_part, flags=re.IGNORECASE)
            expected_addr = addr_part
            query_to_type = addr_part

        # fallback queries for numbered branches/points
        fallback_queries: list[str] = []
        num = _branch_number_from_query(BRANCH_QUERY)
        if num:
            qn = BRANCH_QUERY.lower()
            if "пункт приймання-видачі" in qn:
                fallback_queries = [f"№{num}", num]
            elif "пункт" in qn:
                fallback_queries = [f"№{num}", num]
            elif "відділен" in qn:
                fallback_queries = [f"№{num}", num]
            else:
                fallback_queries = [f"№{num}", num]

        search_queries = [query_to_type]
        for fq in fallback_queries:
            if fq and fq not in search_queries:
                search_queries.append(fq)

        for attempt in range(1, 4):
            try:
                await _ensure_np_branch_mode(page)
                # ensure mode by clicking label/radio too
                try:
                    label = sec.locator('label:has-text("Нова пошта до відділення")').first
                    if await label.count() > 0:
                        await _human_click(page, label)
                except Exception:
                    pass
                try:
                    radio = sec.locator('input[type="radio"]:visible').first
                    if await radio.count() > 0:
                        await _human_click(page, radio)
                except Exception:
                    pass

                await _wait_np_section_idle(page, sec, timeout_ms=2500)
                if attempt == 1:
                    await page.screenshot(path=str(ART / "step6_0a_after_np_selected.png"), full_page=True)

                # Early exit if already selected
                try:
                    selected_now = (await sec.locator("div.ss-main").first.inner_text()).strip()
                except Exception:
                    selected_now = ""
                if selected_now and not _is_placeholder(selected_now):
                    if addr_mode or matcher(selected_now):
                        await page.screenshot(path=str(ART / "step6_already_selected.png"), full_page=True)
                        print("OK: branch already selected, skipping step6 selection")
                        if not USE_CDP:
                            await browser.close()
                        return

                ss_main, ss_main_count = await _find_np_ss_main(sec)

                # Ensure ss-main is not disabled
                trigger = ss_main
                try:
                    if trigger is not None and await trigger.count() > 0:
                        cls = await trigger.get_attribute("class")
                        aria = await trigger.get_attribute("aria-disabled")
                        if (cls and "ss-disabled" in cls) or (aria == "true"):
                            await page.wait_for_timeout(200)
                            await page.keyboard.press("Escape")
                            await page.wait_for_timeout(200)
                            continue
                except Exception:
                    pass

                # Early exit: if native <select> exists in section, use it
                sel = sec.locator('select[name="extension_attributes_warehouse_ref"]:visible').first
                if await sel.count() > 0:
                    await page.screenshot(path=str(ART / "step6_0b_branch_select_found.png"), full_page=True)
                    opt_loc = sel.locator("option")
                    opt_count = await opt_loc.count()
                    chosen_value = None
                    chosen_label = None

                    for i in range(min(opt_count, 500)):
                        o = opt_loc.nth(i)
                        try:
                            label_txt = (await o.inner_text()).strip()
                        except Exception:
                            continue
                        if not label_txt:
                            continue
                        if not matcher(label_txt):
                            continue
                        try:
                            val = await o.get_attribute("value")
                        except Exception:
                            val = None
                        if val:
                            chosen_value = val
                            chosen_label = label_txt
                            break

                    if not chosen_value:
                        await page.screenshot(path=str(ART / "step6_err_no_match.png"), full_page=True)
                        raise RuntimeError(
                            f"Не нашёл подходящий пункт ({kind}) для запроса '{BRANCH_QUERY}' в <select>. "
                            f"Проверь step6_err_no_match.png"
                        )

                    await sel.select_option(value=chosen_value)
                    await page.wait_for_timeout(300)
                    await page.screenshot(path=str(ART / "step6_2_after_selected.png"), full_page=True)
                    print(f"OK: пункт/отделение выбрано (select, {kind}). label='{chosen_label}'")
                    last_err = None
                    break

                popup, opened, popup_sibling = await _ensure_np_dropdown_open(page, ss_main)
                ss_open = False
                try:
                    ss_open = await popup.is_visible() if popup is not None else False
                except Exception:
                    ss_open = False

                search_inp = await _get_np_search_input(popup)
                has_search = False
                try:
                    if search_inp and await search_inp.count() > 0:
                        has_search = True
                except Exception:
                    has_search = False

                visible_contents = 0
                try:
                    visible_contents = await page.locator("div.ss-content:visible").count()
                except Exception:
                    visible_contents = 0

                opts = popup.locator("div.ss-option:visible") if popup is not None else sec.locator("div.ss-content:visible .ss-option:visible")
                try:
                    opt_count = await opts.count()
                except Exception:
                    opt_count = 0

                print(
                    f"[step6] attempt {attempt}: query='{search_queries[min(attempt - 1, len(search_queries) - 1)]}' "
                    f"ss_main_count={ss_main_count} popup_sibling={popup_sibling} dropdown_open={opened} "
                    f"ss_content={ss_open} search_input={has_search} visible_popups={visible_contents} "
                    f"options={opt_count}"
                )

                if not opened:
                    last_screen = ART / f"step6_retry_dropdown_not_open_{attempt}.png"
                    await page.screenshot(path=str(last_screen), full_page=True)
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                    continue

                await page.screenshot(path=str(ART / "step6_dbg_dropdown_open.png"), full_page=True)

                if not has_search:
                    last_screen = ART / f"step6_retry_no_search_input_{attempt}.png"
                    await page.screenshot(path=str(last_screen), full_page=True)
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                    continue

                # choose query for this attempt (primary + fallback)
                q_idx = min(attempt - 1, len(search_queries) - 1)
                q_to_type = search_queries[q_idx]

                await search_inp.click(force=True)
                await page.wait_for_timeout(120)
                try:
                    await search_inp.fill("")
                except Exception:
                    await page.keyboard.press("Meta+A")
                    await page.keyboard.press("Backspace")

                await search_inp.fill(q_to_type)
                await page.wait_for_timeout(200)
                try:
                    val = await search_inp.input_value()
                    print(f"[step6] attempt {attempt}: input_value='{val}'")
                except Exception:
                    pass

                opts = popup.locator("div.ss-option:visible")
                found = await _wait_options_visible(page, opts, timeout_ms=8000)
                if not found:
                    last_screen = ART / f"step6_retry_no_options_{attempt}.png"
                    await page.screenshot(path=str(last_screen), full_page=True)
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                    continue

                count_now = await opts.count()
                print(f"[step6] attempt {attempt}: options={count_now} query='{q_to_type}'")

                # optional ENTER (disabled by default)
                if TRY_ENTER:
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(200)

                selected_txt = await _get_selected_text(sec, ss_main=ss_main)
                selected_txt2 = ""

                ok = False
                must_tokens = _tokenize_must_contain(BRANCH_MUST_CONTAIN)
                if selected_txt:
                    if _selected_text_ok(selected_txt, kind, branch_no, must_tokens):
                        ok = True

                if not ok:
                    count = await opts.count()
                    chosen = None

                    if combined_mode:
                        if not expected_num or not expected_addr:
                            last_err = RuntimeError("no combined match")
                            raise last_err
                        for i in range(min(count, 80)):
                            item = opts.nth(i)
                            try:
                                txt = (await item.inner_text()).strip()
                            except Exception:
                                continue
                            if not txt:
                                continue
                            if "результатів не знайдено" in _norm(txt):
                                continue
                            if (re.search(rf"№\s*{re.escape(expected_num)}(?!\d)", txt)) and _addr_matches(txt, expected_addr):
                                chosen = item
                                break
                        if not chosen:
                            last_err = RuntimeError("no combined match")
                            raise last_err
                    elif addr_mode:
                        if count == 0:
                            last_err = RuntimeError("no suggestions")
                            raise last_err
                        for i in range(min(count, 80)):
                            item = opts.nth(i)
                            try:
                                txt = (await item.inner_text()).strip()
                            except Exception:
                                continue
                            if not txt:
                                continue
                            if "результатів не знайдено" in _norm(txt):
                                continue
                            if _addr_matches(txt, expected_addr):
                                chosen = item
                                break
                        if not chosen:
                            last_err = RuntimeError("no address match")
                            raise last_err
                    else:
                        # numeric-mode (strict by number)
                        if not expected_num:
                            last_err = RuntimeError("no numeric match")
                            raise last_err
                        num_re = re.compile(rf"№\s*{re.escape(expected_num)}(?!\d)", re.IGNORECASE)
                        for i in range(min(count, 80)):
                            item = opts.nth(i)
                            try:
                                txt = (await item.inner_text()).strip()
                            except Exception:
                                continue
                            if not txt:
                                continue
                            if "результатів не знайдено" in _norm(txt):
                                continue
                            if num_re.search(txt):
                                chosen = item
                                break
                        if not chosen:
                            last_err = RuntimeError("no numeric match")
                            raise last_err

                    if not chosen:
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
                        last_err = RuntimeError("no match")
                        raise last_err

                    try:
                        await chosen.scroll_into_view_if_needed()
                    except Exception:
                        pass

                    await chosen.click(force=True)
                    await page.wait_for_timeout(200)
                    # Wait for selected text to update (dropdown may stay open)
                    selected_txt2 = ""
                    updated = False
                    for _ in range(25):  # ~2.5s
                        selected_txt2 = await _get_selected_text(sec, ss_main=ss_main)
                        if _selected_text_ok(selected_txt2, kind, branch_no, must_tokens):
                            updated = True
                            break
                        await page.wait_for_timeout(100)

                if updated:
                    # dropdown may remain open; close and proceed
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    # success, stop retrying
                    selected_final = selected_txt2 or selected_txt
                    await page.screenshot(path=str(ART / "step6_2_after_selected.png"), full_page=True)
                    print(
                        f"OK: пункт/отделение выбрано ({kind}). query='{BRANCH_QUERY}', must='{BRANCH_MUST_CONTAIN}', selected='{selected_final}'"
                    )
                    last_err = None
                    break
                else:
                    await page.screenshot(
                        path=str(ART / f"step6_retry_selected_not_applied_{attempt}.png"),
                        full_page=True,
                    )
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                    continue

                    if combined_mode:
                        if not selected_txt2 or "введіть" in _norm(selected_txt2):
                            last_err = RuntimeError("selected value not applied")
                            raise last_err
                        if not (re.search(rf"№\s*{re.escape(expected_num)}(?!\d)", selected_txt2) and _addr_matches(selected_txt2, expected_addr)):
                            last_err = RuntimeError("selected value mismatch")
                            raise last_err
                    elif addr_mode:
                        if not selected_txt2 or "введіть" in _norm(selected_txt2):
                            last_err = RuntimeError("selected value not applied")
                            raise last_err
                        if not _addr_matches(selected_txt2, expected_addr):
                            last_err = RuntimeError("selected value mismatch")
                            raise last_err
                    else:
                        if not expected_num or not re.search(rf"№\s*{re.escape(expected_num)}(?!\d)", selected_txt2):
                            last_err = RuntimeError("selected value mismatch")
                            raise last_err

                selected_final = selected_txt2 or selected_txt
                await page.screenshot(path=str(ART / "step6_2_after_selected.png"), full_page=True)
                print(
                    f"OK: пункт/отделение выбрано ({kind}). query='{BRANCH_QUERY}', must='{BRANCH_MUST_CONTAIN}', selected='{selected_final}'"
                )
                last_err = None
                break

            except Exception as e:
                last_err = e
                if last_screen is None:
                    last_screen = ART / f"step6_retry_{attempt}.png"
                    await page.screenshot(path=str(last_screen), full_page=True)
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)

        if last_err is not None:
            if last_screen is None:
                last_screen = ART / "step6_err_no_match.png"
                await page.screenshot(path=str(last_screen), full_page=True)
            raise RuntimeError(
                f"Не удалось стабильно выбрать пункт/отделение после 3 попыток: {last_err}. "
                f"Смотри {last_screen.name}"
            )


if __name__ == "__main__":
    asyncio.run(main())
