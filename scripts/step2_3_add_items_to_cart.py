from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")
BASE_URL = (os.getenv("BIOTUS_BASE_URL") or "https://opt.biotus.ua").rstrip("/")

AFTER_URL = os.getenv("BIOTUS_AFTER_LOGIN_URL")
STATE = ROOT / "artifacts" / "storage_state.json"

TIMEOUT_MS = int(os.getenv("BIOTUS_TIMEOUT_MS", "15000"))

# Show cart modal at the end so step4_checkout can click "Оформити" when cart verification is disabled.
SHOW_CART_MODAL_AT_END = os.getenv("BIOTUS_SHOW_CART_MODAL_AT_END", "1") == "1"

# New format: "SKU=QTY,SKU2=QTY2"
ITEMS_RAW = os.getenv("BIOTUS_ITEMS", "").strip()

# Backward compatible:
SINGLE_SKU = os.getenv("BIOTUS_TEST_SKU", "").strip()
SINGLE_QTY = int(os.getenv("BIOTUS_QTY", "1"))

CART_VERIFY_ENABLED = os.getenv("BIOTUS_CART_VERIFY", "1") == "1"
CART_VERIFY_STRICT = os.getenv("BIOTUS_CART_VERIFY_STRICT", "1") == "1"
CART_VERIFY_SCREENSHOT = os.getenv("BIOTUS_CART_VERIFY_SCREENSHOT", "1") == "1"
CART_VERIFY_SAVE_OK_HTML = os.getenv("BIOTUS_CART_VERIFY_SAVE_OK_HTML", "0") == "1"

SKU_TOKEN_RE = re.compile(r"(?<![A-Z0-9-])([A-Z0-9]+(?:-[A-Z0-9]+)+)(?![A-Z0-9-])", re.I)


@dataclass
class Item:
    sku: str
    qty: int


def _normalize_sku(sku: str) -> str:
    return (sku or "").strip().upper()


def _extract_qty_from_text(text: str) -> int | None:
    patterns = [
        r"(?:кількість|кол-?во|qty|quantity)\s*[:xх×]?\s*(\d+)",
        r"(?:^|\s)[xх×]\s*(\d+)(?:\b|\s|$)",
        r"\b(\d+)\s*(?:шт|pcs|pieces)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                v = int(m.group(1))
                if v > 0:
                    return v
            except Exception:
                continue
    return None


def _extract_sku_tokens(text: str) -> List[str]:
    text = text or ""
    labeled: List[str] = []
    for m in re.finditer(r"(?:sku|артикул|код(?:\s+товару)?)\s*[:#№-]?\s*([A-Z0-9]+(?:-[A-Z0-9]+)+)", text, re.I):
        labeled.append(_normalize_sku(m.group(1)))

    generic = [_normalize_sku(m.group(1)) for m in SKU_TOKEN_RE.finditer(text)]
    ordered: List[str] = []
    for sku in labeled + generic:
        if sku and sku not in ordered:
            ordered.append(sku)
    return ordered


def parse_expected_items(items_raw: str) -> Dict[str, int]:
    raw = (items_raw or "").strip()
    if not raw:
        return {}

    expected: Dict[str, int] = {}
    parts = [p.strip() for p in re.split(r"[,;\n]+", raw) if p.strip()]
    for part in parts:
        if "=" in part:
            sku_s, qty_s = part.split("=", 1)
            sku = _normalize_sku(sku_s)
            qty_s = qty_s.strip()
            qty = int(qty_s) if qty_s else 1
        else:
            sku = _normalize_sku(part)
            qty = 1

        if not sku:
            continue
        if qty < 1:
            qty = 1
        expected[sku] = expected.get(sku, 0) + qty

    return expected


def parse_items() -> List[Item]:
    expected = parse_expected_items(ITEMS_RAW)
    if expected:
        return [Item(sku=sku, qty=qty) for sku, qty in expected.items()]

    if not SINGLE_SKU:
        raise RuntimeError("Не задано BIOTUS_ITEMS и пустой BIOTUS_TEST_SKU. Нечего добавлять.")
    qty = SINGLE_QTY if SINGLE_QTY > 0 else 1
    return [Item(sku=_normalize_sku(SINGLE_SKU), qty=qty)]


class _CartHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.open_blocks: List[dict] = []
        self.completed_blocks: List[dict] = []

    @staticmethod
    def _attrs_dict(attrs) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for k, v in attrs:
            out[(k or "").lower()] = (v or "")
        return out

    @staticmethod
    def _looks_like_item_tag(tag: str, attrs: Dict[str, str]) -> bool:
        if tag not in {"tr", "div", "li", "article"}:
            return False
        text = " ".join(attrs.get(k, "") for k in ("class", "id", "data-role", "itemprop", "data-th")).lower()
        if not text:
            return False
        if any(x in text for x in ("product-item-name", "product-item-sku", "item-option", "item-options")):
            return False
        return bool(
            re.search(
                r"(?:\bcart-item\b|\bitem-info\b(?!-)|\bproduct-item\b(?!-)|\bminicart-item\b|\bshopping-cart-item\b|\bquote-item\b)",
                text,
            )
        )

    @staticmethod
    def _extract_qty_from_attrs(attrs: Dict[str, str]) -> int | None:
        qty_keys = ("qty", "quantity", "кількість", "кол")
        for k, v in attrs.items():
            lk = k.lower()
            lv = (v or "").strip()
            if not lv:
                continue
            if lk.startswith("data-") and "qty" in lk and lv.isdigit():
                n = int(lv)
                if n > 0:
                    return n
            if any(mark in lk for mark in qty_keys):
                if lv.isdigit():
                    n = int(lv)
                    if n > 0:
                        return n
            if lk == "value" and attrs.get("type", "").lower() in {"number", "text"}:
                name_blob = " ".join([attrs.get("name", ""), attrs.get("id", ""), attrs.get("class", "")]).lower()
                if any(mark in name_blob for mark in qty_keys) and lv.isdigit():
                    n = int(lv)
                    if n > 0:
                        return n
        return None

    def handle_starttag(self, tag, attrs):
        self.depth += 1
        attrs_d = self._attrs_dict(attrs)

        if self._looks_like_item_tag(tag, attrs_d):
            self.open_blocks.append(
                {
                    "depth": self.depth,
                    "text": [],
                    "qty_candidates": [],
                }
            )

        qty_from_attrs = self._extract_qty_from_attrs(attrs_d)
        if qty_from_attrs is not None:
            for blk in self.open_blocks:
                blk["qty_candidates"].append(qty_from_attrs)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not data:
            return
        for blk in self.open_blocks:
            blk["text"].append(data)

    def handle_endtag(self, tag):
        to_close = [blk for blk in self.open_blocks if blk["depth"] == self.depth]
        for blk in to_close:
            self.open_blocks.remove(blk)
            self.completed_blocks.append(blk)
        self.depth = max(0, self.depth - 1)


def parse_cart_html(html: str) -> Dict[str, int]:
    parser = _CartHTMLParser()
    parser.feed(html or "")

    found: Dict[str, int] = {}
    for blk in parser.completed_blocks:
        text = re.sub(r"\s+", " ", " ".join(blk.get("text", []))).strip()
        if not text:
            continue

        qty_candidates = [q for q in blk.get("qty_candidates", []) if isinstance(q, int) and q > 0]
        qty = qty_candidates[0] if qty_candidates else None
        if qty is None:
            qty = _extract_qty_from_text(text)
        if qty is None:
            qty = 1

        for sku in _extract_sku_tokens(text):
            found[sku] = found.get(sku, 0) + qty

    return found


async def read_cart_items(page) -> Dict[str, int]:
    html = await page.content()
    return parse_cart_html(html)


def _validate_cart(expected: Dict[str, int], found: Dict[str, int], *, strict: bool) -> List[str]:
    errors: List[str] = []
    for sku, exp_qty in expected.items():
        got_qty = found.get(sku)
        if got_qty is None:
            errors.append(f"SKU={sku}: expected={exp_qty}, found=ABSENT")
            continue

        if strict:
            if got_qty != exp_qty:
                errors.append(f"SKU={sku}: expected={exp_qty}, found={got_qty} (strict ==)")
        else:
            if got_qty < exp_qty:
                errors.append(f"SKU={sku}: expected>={exp_qty}, found={got_qty}")

    return errors


def _fmt_items(items: Dict[str, int]) -> str:
    if not items:
        return "<empty>"
    return ", ".join(f"{k}={v}" for k, v in sorted(items.items()))


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


async def _goto_cart_page(page):
    await _dismiss_cart_overlay(page)
    cart_url = f"{BASE_URL}/checkout/cart"
    await page.goto(cart_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)


async def _save_cart_html(page, filename: str):
    html = await page.content()
    path = ART / filename
    path.write_text(html, encoding="utf-8")


async def verify_cart_or_raise(
    page,
    expected: Dict[str, int],
    *,
    strict: bool,
    screenshot_on_fail: bool,
    save_ok_html: bool,
    fail_prefix: str,
    ok_html_name: str,
):
    await _goto_cart_page(page)
    found = await read_cart_items(page)
    mismatches = _validate_cart(expected, found, strict=strict)

    if mismatches:
        await _save_cart_html(page, f"{fail_prefix}.html")
        if screenshot_on_fail:
            try:
                await page.screenshot(path=str(ART / f"{fail_prefix}.png"), full_page=True)
            except Exception:
                pass

        print("CART VERIFY FAILED")
        print(f"Expected: {_fmt_items(expected)}")
        print(f"Found: {_fmt_items(found)}")
        for line in mismatches:
            print(f" - {line}")
        raise RuntimeError("Cart verification failed: " + " | ".join(mismatches))

    if save_ok_html:
        await _save_cart_html(page, ok_html_name)

    print(f"CART VERIFY OK: {_fmt_items(expected)}")


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
    expected = {item.sku: item.qty for item in items}
    if not expected:
        raise RuntimeError("BIOTUS_ITEMS задан, но не удалось распарсить ни одного товара.")

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

                keep_open = SHOW_CART_MODAL_AT_END and not CART_VERIFY_ENABLED and (idx == len(items))
                await _click_add_to_cart(page, keep_cart_modal_open=keep_open)
                await page.screenshot(path=str(ART / f"step3_added_{idx}_{it.sku}.png"), full_page=True)

                # small pause between items (UI animations)
                await page.wait_for_timeout(500)

            if CART_VERIFY_ENABLED:
                await verify_cart_or_raise(
                    page,
                    expected,
                    strict=CART_VERIFY_STRICT,
                    screenshot_on_fail=CART_VERIFY_SCREENSHOT,
                    save_ok_html=CART_VERIFY_SAVE_OK_HTML,
                    fail_prefix="step2_3_cart_verify_failed",
                    ok_html_name="step2_3_cart_verify_ok.html",
                )
            elif SHOW_CART_MODAL_AT_END:
                # Safety: if we want the cart modal at end but it was not shown for some reason, try to open it.
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
