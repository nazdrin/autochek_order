# scripts/step6_1_select_np_terminal.py
from __future__ import annotations

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

# Example: 'Поштомат "Нова Пошта" №1014' or just '1014' or 'Лукʼянівська 27'
TERMINAL_QUERY = os.getenv("BIOTUS_TERMINAL_QUERY", "").strip()
TERMINAL_MUST_CONTAIN = os.getenv("BIOTUS_TERMINAL_MUST_CONTAIN", "").strip()

# optional: if "1" and query has number -> strict match by №<num>
TERMINAL_STRICT = os.getenv("BIOTUS_TERMINAL_STRICT", "0") == "1"

# Support a common timeout env var.
TIMEOUT_MS = int(os.getenv("BIOTUS_TIMEOUT_MS", "15000"))

PLACEHOLDER_TERMINAL = "Введіть вулицю або номер поштомата"


# ----------------- helpers -----------------
async def _human_click(page, locator):
    loc = locator.first
    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        box = await loc.bounding_box()
    except Exception:
        box = None
    if box:
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        return
    try:
        await loc.click()
    except Exception:
        try:
            await loc.click(force=True)
        except Exception:
            pass


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("\u00a0", " ").replace("\u200b", " ")
    # normalize dashes
    s = s.replace("–", "-").replace("—", "-")
    # normalize apostrophes/quotes often used in UA addresses
    s = (
        s.replace("ʼ", "'")
        .replace("’", "'")
        .replace("`", "'")
        .replace("\u02bc", "'")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u00b4", "'")
    )
    # normalize quotes
    s = s.replace("\u00ab", '"').replace("\u00bb", '"').replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokenize_tokens(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    parts = re.split(r"[;,]+", s)
    out = []
    for p in parts:
        p = _norm(p)
        if p:
            out.append(p)
    return out


def _extract_number(s: str) -> str | None:
    m = re.search(r"\d+", s or "")
    return m.group(0) if m else None


def _build_terminal_matcher(query: str, must_contain: str):
    q_raw = query or ""
    qn = _norm(q_raw)
    must_tokens = _tokenize_tokens(must_contain)

    num = _extract_number(q_raw)
    has_num = bool(num and re.fullmatch(r"\d+", num))

    # For "№1014" avoid matching 10140
    strict_re = None
    if has_num:
        strict_re = re.compile(rf"(?:№\s*{re.escape(num)})(?!\d)", re.IGNORECASE)

    # For non-number searches: use strong tokens (>=3 chars) from query
    raw_tokens = [t for t in re.split(r"[\s/,\-]+", qn) if t]
    strong_tokens = [t for t in raw_tokens if len(t) >= 3 and t not in {"вул", "пр", "пл", "буд"}]

    def matches(option_text: str) -> bool:
        if not option_text:
            return False
        tn = _norm(option_text)

        # must_contain tokens (if provided)
        for t in must_tokens:
            if t not in tn:
                return False

        # strict by number when we have number and strict enabled OR query looks like number-only
        if has_num and (TERMINAL_STRICT or qn.isdigit() or "№" in q_raw):
            return bool(strict_re and strict_re.search(option_text))

        # otherwise token-based
        if strong_tokens:
            return all(tok in tn for tok in strong_tokens)

        return qn in tn

    return matches


def _looks_like_checkout(url: str, title: str) -> bool:
    u = (url or "").lower()
    t = (title or "").lower()
    return (
        "opt.biotus" in u and ("/checkout" in u or "checkout?" in u)
    ) or ("оформлення" in t) or ("checkout" in u)


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


# ----------------- find/ensure terminal mode -----------------
async def _delivery_terminal_section(page):
    # Prefer class-based (as on your devtools screenshot: rate_WarehouseTerminals / container_WarehouseTerminals)
    sec = page.locator(
        'div.container_shipping_method.container_WarehouseTerminals, '
        'div.container_WarehouseTerminals, '
        'div.amcheckout-method[data-shipping-method*="newposhta_WarehouseTerminals"], '
        'div.container_shipping_method:has-text("Нова пошта до поштомата")'
    ).first
    try:
        if await sec.count() > 0:
            return sec
    except Exception:
        pass
    # fallback by text
    txt = page.get_by_text("Нова пошта до поштомата", exact=False).first
    if await txt.count() > 0:
        root = txt.locator('xpath=ancestor::div[contains(@class,"container_shipping_method")][1]')
        if await root.count() > 0:
            return root.first
    return page


async def _ensure_terminal_mode(page):
    """Make sure shipping method 'Нова пошта до поштомата' is selected.

    IMPORTANT: when running in cascade, default method is often 'до відділення',
    so terminal section/field may not exist until we switch the radio.
    """

    # Prefer clicking the label because Alpine/Magento themes часто вешают обработчик на label.
    term_label = page.locator(
        'label.amcheckout-label.-radio[for^="s_method_newposhta_WarehouseTerminals"], '
        'label[for^="s_method_newposhta_WarehouseTerminals"], '
        'label[for*="newposhta_WarehouseTerminals"], '
        'label[for*="WarehouseTerminals"]'
    ).first

    term_radio = page.locator(
        'input[type="radio"][id^="s_method_newposhta_WarehouseTerminals"], '
        'input[type="radio"][id*="newposhta_WarehouseTerminals"], '
        'input[type="radio"][value*="WarehouseTerminals"], '
        'input[type="radio"][id*="WarehouseTerminals"]'
    ).first

    # If already checked — nothing to do
    try:
        if await term_radio.count() > 0:
            try:
                if await term_radio.is_checked():
                    return
            except Exception:
                pass
    except Exception:
        pass

    # Click label first (most reliable)
    try:
        if await term_label.count() > 0:
            await _human_click(page, term_label)
            await page.wait_for_timeout(250)
    except Exception:
        pass

    # Fallback: try checking/clicking the radio
    try:
        if await term_radio.count() > 0:
            try:
                await term_radio.check(force=True)
            except Exception:
                await _human_click(page, term_radio)
            await page.wait_for_timeout(250)
    except Exception:
        pass

    # Final fallback: click by visible text
    try:
        fallback_click = page.get_by_text("Нова пошта до поштомата", exact=False).first
        if await fallback_click.count() > 0:
            await _human_click(page, fallback_click)
            await page.wait_for_timeout(250)
    except Exception:
        pass

    # Wait until terminal UI is present
    wait_target = page.locator(
        f'div.container_WarehouseTerminals div.ss-main:has-text("{PLACEHOLDER_TERMINAL}"), '
        f'div.container_shipping_method.container_WarehouseTerminals div.ss-main:has-text("{PLACEHOLDER_TERMINAL}"), '
        f'div.container_shipping_method:has-text("Нова пошта до поштомата") div.ss-main:has-text("{PLACEHOLDER_TERMINAL}")'
    ).first

    for _ in range(max(30, TIMEOUT_MS // 100)):
        try:
            if await wait_target.count() > 0 and await wait_target.is_visible():
                return
        except Exception:
            pass
        await page.wait_for_timeout(100)


async def _get_terminal_popup(page, inp=None, sec=None):
    # Strict: popup must contain the terminal search input placeholder.
    popup = page.locator(
        f'div.ss-content:visible:has(input[type="search"][placeholder="{PLACEHOLDER_TERMINAL}"]), '
        f'div.ss-content:visible:has(input[type="search"][aria-label="{PLACEHOLDER_TERMINAL}"])'
    ).first
    try:
        if await popup.count() > 0:
            return popup
    except Exception:
        pass

    # If we have section, try to scope to it
    if sec is not None:
        try:
            popup2 = sec.locator(
                f'div.ss-content:visible:has(input[type="search"][placeholder="{PLACEHOLDER_TERMINAL}"]), '
                f'div.ss-content:visible:has(input[type="search"][aria-label="{PLACEHOLDER_TERMINAL}"])'
            ).first
            if await popup2.count() > 0:
                return popup2
        except Exception:
            pass

    # If we have the input — take its nearest ss-content ancestor
    if inp is not None:
        try:
            anc = inp.locator('xpath=ancestor::div[contains(@class,"ss-content")][1]').first
            if await anc.count() > 0:
                return anc
        except Exception:
            pass

    # Do NOT fallback to any visible ss-content (it can belong to city/branch etc. and cause endless waits)
    return None


async def _find_terminal_input(page):
    sec = await _delivery_terminal_section(page)

    # click open trigger in this section
    trigger = sec.locator(
        f'div.ss-main:has-text("{PLACEHOLDER_TERMINAL}"), div.ss-main'
    ).first
    if await trigger.count() == 0:
        return None

    await _human_click(page, trigger)
    await page.wait_for_timeout(150)

    for _ in range(max(50, TIMEOUT_MS // 100)):
        popup = await _get_terminal_popup(page, sec=sec)
        if popup is not None:
            inp = popup.locator('input[type="search"]').first
            try:
                if await inp.count() > 0 and await inp.is_visible():
                    return inp
            except Exception:
                pass
        await page.wait_for_timeout(100)

    return None


async def _wait_terminal_options(page, popup=None):
    if popup is None:
        try:
            popup = await _get_terminal_popup(page, sec=await _delivery_terminal_section(page))
        except Exception:
            popup = None
    if popup is None:
        return None

    opts = popup.locator("div.ss-list .ss-option:visible")
    for _ in range(max(50, TIMEOUT_MS // 100)):
        try:
            if await opts.count() > 0:
                txt = (await opts.first.inner_text()).strip()
                if txt:
                    return opts
        except Exception:
            pass
        await page.wait_for_timeout(100)
    return None



async def _wait_popup_collapse(page, inp):
    for _ in range(50):  # ~5s
        try:
            pop = await _get_terminal_popup(page, inp=inp)
            if pop is None:
                return
            if await pop.locator("div.ss-list .ss-option:visible").count() == 0:
                return
        except Exception:
            return
        await page.wait_for_timeout(100)


# --- Robust CDP disconnect helper ---
async def _disconnect_cdp(browser):
    """Best-effort disconnect for CDP mode.

    IMPORTANT: In CDP mode we must NOT close the real Chrome window.
    Also `browser.close()` may hang. We only stop Playwright's CDP connection.
    """
    if browser is None:
        return

    try:
        impl = getattr(browser, "_impl_obj", None)
        conn = getattr(impl, "_connection", None)
        if conn is None:
            return

        stop_async = getattr(conn, "stop_async", None)
        if callable(stop_async):
            await asyncio.wait_for(stop_async(), timeout=2.0)
            return

        stop = getattr(conn, "stop", None)
        if callable(stop):
            res = stop()
            if asyncio.iscoroutine(res):
                await asyncio.wait_for(res, timeout=2.0)
            return
    except Exception:
        return


async def main():
    if not TERMINAL_QUERY:
        raise RuntimeError("BIOTUS_TERMINAL_QUERY is empty. Set it to terminal number/text.")

    matcher = _build_terminal_matcher(TERMINAL_QUERY, TERMINAL_MUST_CONTAIN)
    query_dbg = TERMINAL_QUERY

    async with async_playwright() as p:
        browser = context = page = None
        try:
            browser, context, page = await _connect(p)

            if page.url == "about:blank":
                page = await _pick_active_page(context)

            await page.wait_for_timeout(250)
            await page.screenshot(path=str(ART / "step6_1_0_before.png"), full_page=True)

            await _ensure_terminal_mode(page)
            await page.wait_for_timeout(250)
            await page.screenshot(path=str(ART / "step6_1_0a_after_terminal_mode.png"), full_page=True)

            # If a terminal is already selected and matches the requested one, exit early.
            try:
                sec0 = await _delivery_terminal_section(page)
                current_txt = (await sec0.locator("div.ss-main").first.inner_text()).strip()
            except Exception:
                current_txt = ""

            if current_txt:
                ctn = _norm(current_txt)
                if _norm(PLACEHOLDER_TERMINAL) not in ctn and matcher(current_txt):
                    await page.screenshot(path=str(ART / "step6_1_already_selected.png"), full_page=True)
                    print(f"OK: terminal already selected. query='{query_dbg}', must='{TERMINAL_MUST_CONTAIN}', selected='{current_txt}'")
                    return

            inp = await _find_terminal_input(page)
            if not inp:
                await page.screenshot(path=str(ART / "step6_1_err_no_input.png"), full_page=True)
                raise RuntimeError(
                    "Не смог открыть/найти поле поиска поштомата. "
                    "Открой checkout, выбери 'Нова пошта до поштомата' и убедись, что поле доступно."
                )

            await inp.scroll_into_view_if_needed()

            last_err = None
            for attempt in range(1, 4):
                try:
                    await _human_click(page, inp)
                    await page.wait_for_timeout(100)

                    # clear
                    try:
                        await inp.fill("")
                    except Exception:
                        await page.keyboard.press("Meta+A")
                        await page.keyboard.press("Backspace")

                    # type query
                    await inp.type(TERMINAL_QUERY, delay=25)
                    await page.wait_for_timeout(250)

                    popup = await _get_terminal_popup(page, inp=inp)
                    opts = await _wait_terminal_options(page, popup=popup)
                    if not opts:
                        raise RuntimeError("no suggestions")

                    # try Enter
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(450)

                    sec = await _delivery_terminal_section(page)
                    selected_txt = ""
                    try:
                        selected_txt = (await sec.locator("div.ss-main").first.inner_text()).strip()
                    except Exception:
                        pass

                    # if placeholder is still there -> not selected
                    ok = False
                    if selected_txt:
                        stn = _norm(selected_txt)
                        if _norm(PLACEHOLDER_TERMINAL) not in stn and matcher(selected_txt):
                            ok = True

                    if not ok:
                        # click a matching option; if none match, click first option (fallback)
                        count = await opts.count()
                        chosen = None
                        for i in range(min(count, 100)):
                            item = opts.nth(i)
                            try:
                                txt = (await item.inner_text()).strip()
                            except Exception:
                                continue
                            if not txt:
                                continue
                            if matcher(txt):
                                chosen = item
                                break

                        if chosen is None:
                            # fallback: choose first suggestion (useful for address-ish cases)
                            chosen = opts.first

                        await _human_click(page, chosen)
                        await page.wait_for_timeout(500)
                        await _wait_popup_collapse(page, inp)

                        # verify again
                        selected_txt2 = ""
                        try:
                            selected_txt2 = (await sec.locator("div.ss-main").first.inner_text()).strip()
                        except Exception:
                            pass

                        if not selected_txt2:
                            raise RuntimeError("selected value empty")
                        st2 = _norm(selected_txt2)
                        if _norm(PLACEHOLDER_TERMINAL) in st2:
                            raise RuntimeError("selected value still placeholder")

                        # If we clicked first as fallback, don’t over-reject — just ensure it’s not placeholder.
                        # But if we had a matcher-hit, enforce it:
                        if chosen is not opts.first and not matcher(selected_txt2):
                            raise RuntimeError("selected value mismatch")

                    # Force-close dropdown and wait until the selected value is visible/stable
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    try:
                        await _human_click(page, page.locator("body"))
                    except Exception:
                        pass
                    await page.wait_for_timeout(250)

                    # Wait for ss-content to disappear (dropdown collapsed)
                    for _ in range(20):  # max ~5s, дальше не ждём — выбор уже проверен выше
                        try:
                            try:
                                sec = await _delivery_terminal_section(page)
                                pop = await _get_terminal_popup(page, sec=sec)
                            except Exception:
                                break
                            if pop is None or (await pop.count() == 0) or (not await pop.is_visible()):
                                break
                        except Exception:
                            break
                        await page.wait_for_timeout(250)

                    # Re-read selected value after UI settles
                    try:
                        sec = await _delivery_terminal_section(page)
                        selected_txt = (await sec.locator("div.ss-main").first.inner_text()).strip()
                    except Exception:
                        pass

                    await page.screenshot(path=str(ART / "step6_1_after_selected.png"), full_page=True)
                    final_selected = ""
                    try:
                        sec = await _delivery_terminal_section(page)
                        final_selected = (await sec.locator("div.ss-main").first.inner_text()).strip()
                    except Exception:
                        final_selected = (selected_txt or "").strip()

                    print(f"OK: terminal selected. query='{query_dbg}', must='{TERMINAL_MUST_CONTAIN}', selected='{final_selected}'")
                    return

                except Exception as e:
                    last_err = e
                    await page.screenshot(path=str(ART / f"step6_1_retry_{attempt}.png"), full_page=True)
                    await page.wait_for_timeout(500)

            if last_err is not None:
                await page.screenshot(path=str(ART / "step6_1_err_no_match.png"), full_page=True)
                raise RuntimeError(f"Не удалось стабильно выбрать поштомат после 3 попыток: {last_err}")
        finally:
            # In CDP mode we must NOT close the real Chrome window.
            # But we DO want the script to finish promptly in cascade runs.
            # `browser.close()` in CDP mode should only disconnect from Chrome.
            if USE_CDP:
                try:
                    if browser is not None:
                        await asyncio.wait_for(browser.close(), timeout=2.0)
                except Exception:
                    pass
                return

            # Non-CDP: normal close
            try:
                if context is not None:
                    await asyncio.wait_for(context.close(), timeout=3.0)
            except Exception:
                pass
            try:
                if browser is not None:
                    await asyncio.wait_for(browser.close(), timeout=3.0)
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())