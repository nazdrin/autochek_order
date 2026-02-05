from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")

AFTER_URL = os.getenv("BIOTUS_AFTER_LOGIN_URL")
STATE = ROOT / "artifacts" / "storage_state.json"

TIMEOUT_MS = int(os.getenv("BIOTUS_TIMEOUT_MS", "15000"))

# Show cart modal at the end so step4_checkout can click "Оформити".
# During multi-item adds we still close the modal between items to not block the search.
SHOW_CART_MODAL_AT_END = os.getenv("BIOTUS_SHOW_CART_MODAL_AT_END", "1") == "1"

# New format: "SKU=QTY;SKU2=QTY2"
ITEMS_RAW = os.getenv("BIOTUS_ITEMS", "").strip()

# Backward compatible:
SINGLE_SKU = os.getenv("BIOTUS_TEST_SKU", "").strip()
SINGLE_QTY = int(os.getenv("BIOTUS_QTY", "1"))

PLACEHOLDER_SEARCH_UA = "Пошук"
PLACEHOLDER_SEARCH_RU = "Поиск"


@dataclass
class Item:
    sku: str
    qty: int


def parse_items() -> List[Item]:
    if ITEMS_RAW:
        items: List[Item] = []
        parts = [p.strip() for p in re.split(r"[;\n]+", ITEMS_RAW) if p.strip()]
        for p in parts:
            if "=" in p:
                sku, qty_s = p.split("=", 1)
                sku = sku.strip()
                qty_s = qty_s.strip()
                qty = int(qty_s) if qty_s else 1
            else:
                sku = p.strip()
                qty = 1
            if not sku:
                continue
            if qty < 1:
                qty = 1
            items.append(Item(sku=sku, qty=qty))
        if not items:
            raise RuntimeError("BIOTUS_ITEMS задан, но не удалось распарсить ни одного товара.")
        return items

    if not SINGLE_SKU:
        raise RuntimeError("Не задано BIOTUS_ITEMS и пустой BIOTUS_TEST_SKU. Нечего добавлять.")
    return [Item(sku=SINGLE_SKU, qty=SINGLE_QTY)]


async def _pick_active_page(context):
    pages = list(context.pages)
    if pages:
        p = pages[0]
        try:
            await p.bring_to_front()
        except Exception:
            pass
        return p
    p = await context.new_page()
    await p.bring_to_front()
    return p


async def _connect(p):
    if USE_CDP:
        browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await _pick_active_page(context)
        return browser, context, page

    if not STATE.exists():
        raise SystemExit("Нет storage_state.json. Сначала запусти step1_login.py")

    browser = await p.chromium.launch(headless=False)
    context = await browser.new_context(storage_state=str(STATE))
    page = await context.new_page()
    return browser, context, page


async def _find_header_search(page):
    search_candidates = [
        'input[placeholder*="Пошук" i]',
        'input[placeholder*="Поиск" i]',
        'header input[type="text"]',
        'header input[type="search"]',
        'input[type="search"]',
    ]
    for sel in search_candidates:
        loc = page.locator(sel)
        try:
            if await loc.count() > 0 and await loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


async def _open_product_by_sku(page, sku: str):
    search = await _find_header_search(page)
    # Если после добавления предыдущего товара висит модалка корзины — закроем её
    await _dismiss_cart_overlay(page)
    if search is None:
        raise RuntimeError("Не нашёл поле поиска в шапке.")

    await search.click()
    await search.fill(sku)
    await page.wait_for_timeout(400)

    # 1) Prefer dropdown click containing SKU
    try:
        item = page.get_by_text(sku, exact=False)
        await item.first.click(timeout=2500)
    except PWTimeout:
        # 2) fallback Enter
        await search.press("Enter")

    # Wait until product page is loaded: add-to-cart form or qty input or button
    await page.wait_for_timeout(700)
    add_btn = page.locator("button:has-text('В кошик'), a:has-text('В кошик'), .action.tocart, [data-role='tocart']")
    qty_inp = page.locator("input#qty, input[name='qty']")
    try:
        await asyncio.wait_for(
            _wait_any_visible([add_btn, qty_inp], timeout_ms=TIMEOUT_MS),
            timeout=(TIMEOUT_MS / 1000) + 2,
        )
    except Exception:
        # not fatal, but screenshot for debug
        await page.screenshot(path=str(ART / f"step2_ensure_product_{sku}.png"), full_page=True)


async def _wait_any_visible(locators, timeout_ms=15000):
    # helper: waits until any locator becomes visible
    step = 100
    loops = max(1, timeout_ms // step)
    for _ in range(loops):
        for loc in locators:
            try:
                if await loc.count() > 0 and await loc.first.is_visible():
                    return
            except Exception:
                pass
        await asyncio.sleep(step / 1000)
    raise RuntimeError("timeout waiting any visible")


async def _dismiss_cart_overlay(page):
    """Закрывает всплывающее окно корзины (confirmOverlay), если оно появилось.

    При добавлении товара сайт часто показывает модалку "Ваш кошик".
    Она перекрывает шапку/поиск, поэтому перед следующим поиском её нужно закрыть.
    """
    overlay = page.locator("#confirmOverlay")
    try:
        if await overlay.count() == 0:
            return
        if not await overlay.first.is_visible():
            return
    except Exception:
        return

    # 1) Пробуем клик по крестику
    close_btn = page.locator(
        "#confirmOverlay .cross--close, #confirmOverlay .cross.close, #confirmOverlay .cross-close, #confirmOverlay .cross_close, "
        "#confirmOverlay [class*='cross'][class*='close'], #confirmOverlay button[aria-label*='Close' i], "
        "#confirmOverlay button:has-text('×'), #confirmOverlay button:has-text('✕')"
    ).first
    try:
        if await close_btn.count() > 0 and await close_btn.is_visible():
            await close_btn.click(force=True, timeout=2000)
    except Exception:
        pass

    # 2) Fallback: клик по оверлею вне модалки (как пользователь)
    try:
        if await overlay.first.is_visible():
            await overlay.first.click(position={"x": 5, "y": 5}, force=True, timeout=2000)
    except Exception:
        pass

    # 2.5) Fallback: ESC
    try:
        if await overlay.first.is_visible():
            await page.keyboard.press("Escape")
    except Exception:
        pass

    # 3) Дожидаемся исчезновения
    try:
        await overlay.first.wait_for(state="hidden", timeout=5000)
    except Exception:
        # не валим сценарий, но оставим артефакт
        try:
            await page.screenshot(path=str(ART / "step_cart_overlay_still_visible.png"), full_page=True)
        except Exception:
            pass


async def _open_cart_overlay(page):
    """Открывает модалку корзины (confirmOverlay), если она не открыта."""
    overlay = page.locator("#confirmOverlay").first
    try:
        if await overlay.count() > 0 and await overlay.is_visible():
            return True
    except Exception:
        pass

    # try common cart buttons/icons
    cart_candidates = [
        "a[href*='/checkout/cart']",
        "a[href*='checkout/cart']",
        "a:has(svg[class*='cart']), button:has(svg[class*='cart'])",
        "[class*='cart']:not(#confirmOverlay)",
        "a:has-text('Кошик')",
        "a:has-text('Корзина')",
    ]
    for sel in cart_candidates:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(force=True, timeout=3000)
                break
        except Exception:
            continue

    # wait a bit for overlay
    try:
        await overlay.wait_for(state="visible", timeout=6000)
        return True
    except Exception:
        return False


# --- Helper: clear cart if any items present ---
async def _clear_cart_if_any(page):
    """Очищает корзину в модалке `#confirmOverlay`, если в ней есть позиции.

    Нужно перед запуском сценария, т.к. в корзине могут оставаться товары с прошлых итераций.
    """
    opened = await _open_cart_overlay(page)
    if not opened:
        return

    overlay = page.locator("#confirmOverlay").first

    # Кнопки удаления (иконка корзины/треш) внутри строк товара.
    # На сайте встречается класс `remove_product`, но оставляем запасные варианты.
    remove_btns = page.locator(
        "#confirmOverlay .remove_product, "
        "#confirmOverlay [class*='remove_product'], "
        "#confirmOverlay [class*='remove'][class*='product'], "
        "#confirmOverlay button[class*='remove'], "
        "#confirmOverlay a[class*='remove']"
    )

    # Удаляем по одной позиции, пока кнопки удаления не закончатся.
    for _ in range(200):
        try:
            cnt = await remove_btns.count()
        except Exception:
            cnt = 0
        if cnt <= 0:
            break

        btn = remove_btns.first
        try:
            await btn.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            await btn.click(force=True, timeout=5000)
        except Exception:
            # иногда клик по иконке может не пройти из-за перекрытий — попробуем ещё раз
            try:
                await page.wait_for_timeout(200)
                await btn.click(force=True, timeout=5000)
            except Exception:
                break

        # Даем UI время убрать строку
        await page.wait_for_timeout(350)

    # Закрываем модалку, чтобы не блокировала дальнейшие действия
    await _dismiss_cart_overlay(page)

    # На всякий случай убеждаемся, что оверлей исчез
    try:
        await overlay.wait_for(state="hidden", timeout=5000)
    except Exception:
        pass


async def _set_qty(page, qty: int):
    if qty <= 1:
        return

    qty_loc = page.locator("input#qty, input[name='qty']").first
    if await qty_loc.count() == 0:
        # screenshot to tune selectors later
        await page.screenshot(path=str(ART / "step_qty_no_input.png"), full_page=True)
        raise RuntimeError("Не нашёл поле количества (qty).")

    try:
        await qty_loc.scroll_into_view_if_needed()
    except Exception:
        pass

    # clear and set
    try:
        await qty_loc.click()
        await qty_loc.fill("")
        await qty_loc.type(str(qty), delay=20)
    except Exception:
        # fallback keyboard
        await qty_loc.click(force=True)
        await page.keyboard.press("Meta+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(str(qty), delay=20)

    await page.wait_for_timeout(200)


async def _click_add_to_cart(page, *, keep_cart_modal_open: bool = False):
    await page.wait_for_timeout(200)

    add_re = re.compile(r"(в\s+кошик|до\s+кошика|у\s+кошик|в\s+корзин[уы]|add\s*to\s*cart)", re.I)

    btn = page.get_by_role("button").filter(has_text=add_re).first
    if await btn.count() == 0:
        btn = page.get_by_role("link").filter(has_text=add_re).first
    if await btn.count() == 0:
        btn = page.locator(
            "button:has-text('В кошик'), a:has-text('В кошик'), .action.tocart, [data-role='tocart'], [type='submit']:has-text('В кошик')"
        ).first

    if await btn.count() == 0:
        await page.screenshot(path=str(ART / "step3_no_add_btn.png"), full_page=True)
        raise RuntimeError('Не нашёл кнопку "В кошик".')

    try:
        await btn.scroll_into_view_if_needed()
    except Exception:
        pass

    await btn.click(force=True, timeout=30000)
    await page.wait_for_timeout(800)

    # Между товарами закрываем модалку, чтобы не блокировала поиск.
    # Но после последнего товара оставляем/открываем её, чтобы step4 мог нажать "Оформити".
    if keep_cart_modal_open:
        opened = await _open_cart_overlay(page)
        if not opened:
            # If modal doesn't exist on this UI, nothing to do.
            try:
                await page.screenshot(path=str(ART / "step_cart_overlay_not_opened.png"), full_page=True)
            except Exception:
                pass
    else:
        await _dismiss_cart_overlay(page)


async def main():
    items = parse_items()

    async with async_playwright() as p:
        browser = context = page = None
        try:
            browser, context, page = await _connect(p)

            if AFTER_URL:
                try:
                    await page.goto(AFTER_URL, wait_until="domcontentloaded")
                except Exception:
                    pass

            await page.wait_for_timeout(500)
            await page.screenshot(path=str(ART / "step2_3_start.png"), full_page=True)

            # Перед добавлением товаров очищаем корзину от прошлых позиций
            await _clear_cart_if_any(page)
            await page.screenshot(path=str(ART / "step_cart_cleared.png"), full_page=True)

            for idx, it in enumerate(items, start=1):
                print(f"[{idx}/{len(items)}] SKU={it.sku} QTY={it.qty}")

                await _dismiss_cart_overlay(page)

                await _open_product_by_sku(page, it.sku)
                await page.screenshot(path=str(ART / f"step2_open_{idx}_{it.sku}.png"), full_page=True)

                await _set_qty(page, it.qty)
                await page.screenshot(path=str(ART / f"step2_qty_{idx}_{it.sku}.png"), full_page=True)

                keep_open = SHOW_CART_MODAL_AT_END and (idx == len(items))
                await _click_add_to_cart(page, keep_cart_modal_open=keep_open)
                await page.screenshot(path=str(ART / f"step3_added_{idx}_{it.sku}.png"), full_page=True)

                # small pause between items (UI animations)
                await page.wait_for_timeout(500)

            # Safety: if we want the cart modal at end but it was not shown for some reason, try to open it.
            if SHOW_CART_MODAL_AT_END:
                await _open_cart_overlay(page)
                await page.wait_for_timeout(300)
                await page.screenshot(path=str(ART / "step_cart_overlay_final.png"), full_page=True)

            print("OK: all items processed (searched + qty set + added).")

        finally:
            # In CDP mode do NOT close Chrome
            if USE_CDP:
                return
            try:
                if context is not None:
                    await context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())