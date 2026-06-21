import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BASE_URL = "https://dobavki.ua"
HOME_URL = f"{BASE_URL}/"
CHECKOUT_URL = f"{BASE_URL}/checkout/"
PAYMENT_COD_VALUE = "15"
SUPPLIER_RESULT_JSON_PREFIX = "SUPPLIER_RESULT_JSON="
SUBMIT_CHECKPOINT_FILE = ROOT / ".supplier2_submit_checkpoint.json"


def _to_int(value: str, default: int) -> int:
    try:
        iv = int((value or "").strip())
        if iv > 0:
            return iv
    except Exception:
        pass
    return default


def _to_bool(value: str, default: bool) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


TIMEOUT_MS = _to_int(os.getenv("SUP2_TIMEOUT_MS", "20000"), 20000)
NAV_TIMEOUT_MS = max(TIMEOUT_MS, _to_int(os.getenv("SUP2_NAV_TIMEOUT_MS", "45000"), 45000))
HEADLESS = _to_bool(os.getenv("SUP2_HEADLESS", "0"), False)
CLEAR_BASKET = _to_bool(os.getenv("SUP2_CLEAR_BASKET", "1"), True)
DEBUG_PAUSE_SECONDS = _to_int(os.getenv("SUP2_DEBUG_PAUSE_SECONDS", "0"), 0)
DRY_RUN = _to_bool(os.getenv("SUP2_DRY_RUN", "0"), False)
PROMO_CODE = (os.getenv("SUP2_PROMO_CODE") or "SALE15").strip()


@dataclass(frozen=True)
class Item:
    sku: str
    qty: int


@dataclass(frozen=True)
class Recipient:
    name: str
    phone_input: str
    phone_source: str
    city_query: str
    branch_number: str
    branch_query: str
    email: str = ""


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.details = details or {}


def _write_submit_checkpoint(url: str, submitted: bool, order_number: str = "") -> None:
    payload = {
        "ts": int(time.time()),
        "url": str(url or ""),
        "submitted": bool(submitted),
        "order_number": str(order_number or ""),
    }
    try:
        tmp = SUBMIT_CHECKPOINT_FILE.with_suffix(SUBMIT_CHECKPOINT_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SUBMIT_CHECKPOINT_FILE)
    except Exception:
        pass


async def _debug_pause_if_needed() -> None:
    if DEBUG_PAUSE_SECONDS > 0:
        await asyncio.sleep(DEBUG_PAUSE_SECONDS)


async def _dismiss_known_overlays(page) -> None:
    try:
        await page.evaluate(
            """() => {
                for (const selector of ['#langPopupOverlayJS', '#langPopupJS']) {
                    const el = document.querySelector(selector);
                    if (el) el.remove();
                }
                document.documentElement.style.overflow = '';
                document.body.style.overflow = '';
                document.body.classList.remove('modal-open', 'is-modal-open');
            }"""
        )
    except Exception:
        pass


def _normalize_qty(value) -> int:
    try:
        qty = int(str(value).strip())
    except Exception as e:
        raise RuntimeError(f"Invalid qty: {value}") from e
    if qty < 1:
        raise RuntimeError(f"Qty must be >= 1, got: {qty}")
    return qty


def _parse_items() -> list[Item]:
    items_json_raw = (os.getenv("SUP2_ITEMS_JSON") or "").strip()
    if items_json_raw:
        try:
            data = json.loads(items_json_raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"SUP2_ITEMS_JSON is not valid JSON: {e}") from e
        if not isinstance(data, list) or not data:
            raise RuntimeError("SUP2_ITEMS_JSON must be a non-empty JSON list.")
        out: list[Item] = []
        for idx, entry in enumerate(data):
            if not isinstance(entry, dict):
                raise RuntimeError(f"SUP2_ITEMS_JSON[{idx}] must be an object.")
            sku = str(entry.get("sku", "")).strip()
            if not sku:
                raise RuntimeError(f"SUP2_ITEMS_JSON[{idx}].sku is empty.")
            out.append(Item(sku=sku, qty=_normalize_qty(entry.get("qty", 1))))
        return out

    items_raw = (os.getenv("SUP2_ITEMS") or "").strip()
    if items_raw:
        out: list[Item] = []
        for idx, chunk in enumerate([p.strip() for p in items_raw.split(",") if p.strip()]):
            if ":" in chunk:
                sku_part, qty_part = chunk.split(":", 1)
                sku = sku_part.strip()
                qty = _normalize_qty(qty_part)
            else:
                sku = chunk.strip()
                qty = 1
            if not sku:
                raise RuntimeError(f"SUP2_ITEMS part #{idx + 1} has empty sku.")
            out.append(Item(sku=sku, qty=qty))
        if not out:
            raise RuntimeError("SUP2_ITEMS is set but empty after parsing.")
        return out

    raise RuntimeError("SUP2_ITEMS_JSON is required (or SUP2_ITEMS fallback).")


def _parse_order_payload() -> dict[str, Any]:
    raw = (os.getenv("SUP2_ORDER_JSON") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"SUP2_ORDER_JSON is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError("SUP2_ORDER_JSON must be a JSON object.")
    return data


def _first_delivery(order: dict[str, Any]) -> dict[str, Any]:
    odd = order.get("ord_delivery_data") or []
    if isinstance(odd, list) and odd:
        first = odd[0]
        return first if isinstance(first, dict) else {}
    if isinstance(odd, dict):
        return odd
    return {}


def _full_name_from_order(order: dict[str, Any]) -> str:
    pc = order.get("primaryContact") or {}
    if not isinstance(pc, dict):
        pc = {}
    last = str(pc.get("lName") or "").strip()
    first = str(pc.get("fName") or "").strip()
    middle = str(pc.get("mName") or pc.get("middleName") or "").strip()
    return " ".join(x for x in (last, first, middle) if x).strip()


def _first_phone_from_order(order: dict[str, Any]) -> str:
    pc = order.get("primaryContact") or {}
    if not isinstance(pc, dict):
        pc = {}
    phones = pc.get("phone") or []
    if isinstance(phones, list) and phones:
        return str(phones[0] or "").strip()
    return str(pc.get("phone") or "").strip()


def _first_email_from_order(order: dict[str, Any]) -> str:
    pc = order.get("primaryContact") or {}
    if not isinstance(pc, dict):
        pc = {}
    emails = pc.get("email") or pc.get("emails") or []
    if isinstance(emails, list) and emails:
        return str(emails[0] or "").strip()
    return str(emails or "").strip()


def _normalize_dobavki_phone(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if digits.startswith("380") and len(digits) >= 12:
        digits = digits[3:]
    if digits.startswith("0") and len(digits) >= 10:
        digits = digits[1:]
    if len(digits) > 9:
        digits = digits[-9:]
    if len(digits) != 9:
        raise RuntimeError(f"Cannot normalize phone for Dobavki mask: {raw!r}")
    return digits


def _normalize_city_query(city: str) -> str:
    city = str(city or "").strip()
    aliases = {
        "київ": "Киев",
        "м. київ": "Киев",
        "г. киев": "Киев",
        "киев": "Киев",
    }
    return aliases.get(city.lower(), city)


def _norm_match_text(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яіїєґ]+", "", str(value or "").lower())


def _branch_number_from_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(int(value))
    except Exception:
        return str(value).strip()


def _extract_recipient(order: dict[str, Any]) -> Recipient:
    delivery = _first_delivery(order)
    env_name = (os.getenv("SUP2_CUSTOMER_NAME") or "").strip()
    env_phone = (os.getenv("SUP2_CUSTOMER_PHONE") or "").strip()
    env_city = (os.getenv("SUP2_CITY_NAME") or "").strip()
    env_branch = (os.getenv("SUP2_BRANCH_NUMBER") or "").strip()
    env_branch_query = (os.getenv("SUP2_BRANCH_QUERY") or "").strip()
    env_email = (os.getenv("SUP2_CUSTOMER_EMAIL") or "").strip()

    name = env_name or _full_name_from_order(order)
    phone_source = env_phone or _first_phone_from_order(order)
    city_raw = env_city or str(delivery.get("cityName") or "").strip()
    branch_number = env_branch or _branch_number_from_value(delivery.get("branchNumber"))
    branch_query = env_branch_query or str(delivery.get("address") or "").strip()
    email = env_email or _first_email_from_order(order)

    missing = []
    if not name:
        missing.append("customer_name")
    if not phone_source:
        missing.append("customer_phone")
    if not city_raw:
        missing.append("city")
    if not branch_number and not branch_query:
        missing.append("branch_number_or_address")
    if missing:
        raise RuntimeError(f"Missing required SUP2 checkout data: {', '.join(missing)}")

    return Recipient(
        name=name,
        phone_input=_normalize_dobavki_phone(phone_source),
        phone_source=phone_source,
        city_query=_normalize_city_query(city_raw),
        branch_number=branch_number,
        branch_query=branch_query,
        email=email,
    )


async def _goto_retry(page, url: str, *, wait_until: str = "domcontentloaded", timeout: int = NAV_TIMEOUT_MS, attempts: int = 2) -> None:
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            await _dismiss_known_overlays(page)
            return
        except PWTimeoutError as e:
            last_error = e
            if attempt >= attempts:
                raise
            await page.wait_for_timeout(700)
    if last_error is not None:
        raise last_error


async def _safe_wait_networkidle(page, timeout: int = 3000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeoutError:
        pass


async def _clear_basket(page) -> dict:
    await _goto_retry(page, CHECKOUT_URL)
    removed = 0
    while True:
        remove_links = page.locator("a.order-i-remove.j-remove-p")
        try:
            count = await remove_links.count()
        except Exception:
            count = 0
        if count <= 0:
            break
        if removed >= 50:
            raise StageError("clear_basket", "Too many cart rows removed.", {"removed": removed})
        await remove_links.nth(0).click(timeout=TIMEOUT_MS, force=True)
        removed += 1
        await page.wait_for_timeout(900)

    coupon_remove = page.locator("a.j-coupon-remove")
    if await coupon_remove.count() > 0:
        try:
            await coupon_remove.first.click(timeout=3000, force=True)
            await page.wait_for_timeout(500)
        except Exception:
            pass

    remaining = await page.locator("a.order-i-remove.j-remove-p").count()
    if remaining:
        raise StageError("clear_basket", "Basket is not empty after cleanup.", {"remaining": remaining, "removed": removed})
    return {"removed": removed}


async def _wait_search_result(page, sku: str) -> list[dict[str, str]]:
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    latest: list[dict[str, str]] = []
    while asyncio.get_running_loop().time() < deadline:
        latest = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a.search-results__link')).map((a) => ({
                text: (a.innerText || '').trim(),
                href: a.href || ''
            })).filter((x) => x.href)"""
        )
        if latest:
            return latest
        await page.wait_for_timeout(250)
    raise StageError("add_items", f"Search result did not appear for sku={sku}", {"sku": sku, "latest": latest})


async def _open_product_candidate(page, sku: str, href: str, result_text: str = "") -> dict[str, str] | None:
    await _goto_retry(page, href)
    await _safe_wait_networkidle(page)

    body_text = await page.locator("body").inner_text(timeout=TIMEOUT_MS)
    if sku not in body_text:
        return None

    h1 = ""
    try:
        h1 = (await page.locator("h1").first.inner_text(timeout=TIMEOUT_MS)).strip()
    except Exception:
        pass
    return {"sku": sku, "href": page.url or href, "title": h1, "search_text": result_text}


async def _find_product_via_search_page(page, sku: str) -> dict[str, str]:
    await _goto_retry(page, f"{BASE_URL}/catalog/search/?q={sku}")
    candidates = await page.evaluate(
        """() => Array.from(document.querySelectorAll('main .catalogCard-title a[href], main .catalogCard a[href], main a[href]')).map((a) => ({
            text: (a.innerText || '').replace(/\\s+/g, ' ').trim(),
            href: a.href || ''
        })).filter((x) => {
            if (!x.href || !x.text) return false;
            if (x.href.includes('/catalog/search')) return false;
            if (x.href.includes('/filter/')) return false;
            if (x.href.includes('#')) return false;
            if (/^(🌿?Каталог|О нас|Контакты|Блог|Оплата|Отзывы|Политика|Обмен|Договор|Система|DOBAVKI|Витамины оптом|Партнерская|Подбор)/i.test(x.text)) return false;
            return x.href.startsWith(location.origin + '/');
        })"""
    )
    seen: set[str] = set()
    for candidate in candidates[:20]:
        href = str(candidate.get("href") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        product = await _open_product_candidate(page, sku, href, str(candidate.get("text") or ""))
        if product:
            return product
    raise StageError("add_items", f"No exact product page found for sku={sku}", {"sku": sku, "candidates": candidates[:20]})


async def _search_and_open_product(page, sku: str) -> dict[str, str]:
    await _goto_retry(page, HOME_URL)
    search = page.locator("input[name='q']").first
    await search.wait_for(state="visible", timeout=TIMEOUT_MS)
    await search.click(timeout=TIMEOUT_MS)
    await search.fill("", timeout=TIMEOUT_MS)
    await search.type(sku, delay=35, timeout=TIMEOUT_MS)

    results = await _wait_search_result(page, sku)
    href = results[0]["href"]
    product = await _open_product_candidate(page, sku, href, results[0].get("text", ""))
    if product:
        return product
    return await _find_product_via_search_page(page, sku)


async def _wait_ajax_cart_product(page, product_id: str) -> dict:
    if not product_id:
        await page.wait_for_timeout(1200)
        return {"product_id": "", "found": False}
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    latest: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        latest = await page.evaluate(
            """(idRaw) => {
                const out = {product_id: idRaw, hasAjaxCart: !!window.AjaxCart};
                try {
                    const ac = window.AjaxCart && window.AjaxCart.getInstance ? window.AjaxCart.getInstance() : null;
                    out.hasInstance = !!ac;
                    out.initialized = !!(ac && ac.Cart && ac.Cart.initialized);
                    out.ajaxProcessing = ac && ac.Cart ? Number(ac.Cart.ajaxProcessing || 0) : null;
                    const product = ac && ac.getProductById ? ac.getProductById(Number(idRaw), 'product') : null;
                    out.found = !!product;
                    out.quantity = product ? product.quantity : 0;
                    out.totalQuantity = ac && ac.Cart && ac.Cart.total ? ac.Cart.total.quantity : null;
                } catch (e) {
                    out.error = String(e);
                }
                return out;
            }""",
            product_id,
        )
        if latest.get("found") and int(latest.get("ajaxProcessing") or 0) == 0:
            return latest
        await page.wait_for_timeout(300)
    return latest


async def _add_items(page, items: list[Item]) -> list[dict]:
    added: list[dict] = []
    for item in items:
        product = await _search_and_open_product(page, item.sku)
        product_added = False
        for attempt in range(1, 4):
            if attempt > 1:
                await _goto_retry(page, product["href"])
                await _safe_wait_networkidle(page, timeout=6000)
            product_id = await page.evaluate(
                """() => {
                    const btn = document.querySelector('button.j-buy-button-add, button.j-buy-button-remove');
                    return btn && btn.id ? (btn.id.match(/(\\d+)/)?.[1] || '') : '';
                }"""
            )
            buy_btn = page.locator("button.j-buy-button-add").first
            await buy_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
            box = await buy_btn.bounding_box(timeout=TIMEOUT_MS)
            if box:
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                await buy_btn.click(timeout=TIMEOUT_MS)
            try:
                await page.get_by_text("Товар добавлен в корзину", exact=False).wait_for(state="visible", timeout=5000)
            except Exception:
                await page.wait_for_timeout(1200)
            await _wait_ajax_cart_product(page, product_id)

            await _goto_retry(page, CHECKOUT_URL)
            rows = await _read_checkout_rows(page)
            if _match_row_for_item(rows, product, item.sku) is not None:
                product_added = True
                break

            await _goto_retry(page, product["href"])
            await _safe_wait_networkidle(page, timeout=6000)
            js_result = await page.evaluate(
                """async () => {
                    const btn = document.querySelector('button.j-buy-button-add, button.j-buy-button-remove');
                    const idRaw = btn && btn.id ? btn.id.match(/(\\d+)/)?.[1] : '';
                    if (!idRaw) return {ok: false, reason: 'product id not found'};
                    const out = {
                        ok: true,
                        id: idRaw,
                        ajaxCart: false,
                        directPost: null,
                        csrfPrefix: '',
                        csrfLength: 0,
                        csrfSource: '',
                        hasChallengeCookie: document.cookie.includes('challenge_passed=')
                    };
                    if (window.AjaxCart && window.AjaxCart.getInstance) {
                        window.AjaxCart.getInstance().appendProduct({type: 'product', id: Number(idRaw), quantity: 1}, undefined, true);
                        out.ajaxCart = true;
                    }
                    if (typeof window.sendAjax === 'function') {
                        out.sendAjax = await new Promise((resolve) => {
                            window.sendAjax('/_widget/ajax_cart/appendProduct/', {
                                marker: 'DEFAULT',
                                product: {type: 'product', id: Number(idRaw), quantity: 1},
                                analytics: '{}'
                            }, (status, response) => resolve({
                                status,
                                responseText: JSON.stringify(response || {}).slice(0, 300)
                            }));
                        });
                    }
                    const body = new URLSearchParams();
                    body.set('marker', 'DEFAULT');
                    body.set('product[type]', 'product');
                    body.set('product[id]', idRaw);
                    body.set('product[quantity]', '1');
                    body.set('analytics', '{}');
                    const htmlToken = (document.documentElement.outerHTML.match(/GLOBAL_CSRF_TOKEN:\\s*['"]([^'"]+)['"]/) || [])[1] || '';
                    const hiddenToken = document.querySelector('input[name="CSRFToken"]')?.value || '';
                    const globalToken = (window.GLOBAL && window.GLOBAL.GLOBAL_CSRF_TOKEN) || '';
                    const windowToken = window.GLOBAL_CSRF_TOKEN || '';
                    const csrf = globalToken || htmlToken || windowToken || hiddenToken || '';
                    out.csrfSource = globalToken ? 'GLOBAL.GLOBAL_CSRF_TOKEN' : htmlToken ? 'html' : windowToken ? 'window.GLOBAL_CSRF_TOKEN' : hiddenToken ? 'hidden' : '';
                    out.csrfPrefix = csrf ? csrf.slice(0, 6) : '';
                    out.csrfLength = csrf ? csrf.length : 0;
                    if (csrf) body.set('CSRFToken', csrf);
                    const postViaXhr = () => new Promise((resolve) => {
                        const xhr = new XMLHttpRequest();
                        xhr.open('POST', '/_widget/ajax_cart/appendProduct/');
                        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
                        xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded; charset=UTF-8');
                        if (csrf) {
                            xhr.setRequestHeader('X-CSRF-Token', csrf);
                        }
                        xhr.onload = () => resolve({status: xhr.status, text: xhr.responseText.slice(0, 300)});
                        xhr.onerror = () => resolve({status: 0, text: 'xhr error'});
                        xhr.send(body.toString());
                    });
                    if (typeof fetch === 'function') {
                        const res = await fetch('/_widget/ajax_cart/appendProduct/', {
                            method: 'POST',
                            headers: {
                                'X-Requested-With': 'XMLHttpRequest',
                                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                'X-CSRF-Token': csrf
                            },
                            body: body.toString()
                        });
                        out.directPost = {status: res.status, text: (await res.text()).slice(0, 300)};
                    } else {
                        out.directPost = await postViaXhr();
                    }
                    return out;
                }"""
            )
            direct_post = js_result.get("directPost") if isinstance(js_result, dict) else None
            send_ajax = js_result.get("sendAjax") if isinstance(js_result, dict) else None
            send_ajax_ok = isinstance(send_ajax, dict) and str(send_ajax.get("status") or "") == "OK"
            direct_post_ok = isinstance(direct_post, dict) and int(direct_post.get("status") or 0) == 200
            if not (direct_post_ok or send_ajax_ok):
                await _wait_ajax_cart_product(page, str(js_result.get("id") or product_id))
            await _goto_retry(page, CHECKOUT_URL)
            rows = await _read_checkout_rows(page)
            if _match_row_for_item(rows, product, item.sku) is not None:
                product_added = True
                break

        if not product_added:
            raise StageError(
                "add_items",
                f"Product was not found in checkout cart after add click: sku={item.sku}",
                {"sku": item.sku, "product": product, "rows": await _read_checkout_rows(page), "js_result": locals().get("js_result")},
            )
        added.append({**product, "qty": item.qty})
    return added


async def _read_checkout_rows(page) -> list[dict[str, Any]]:
    return await page.evaluate(
        """() => Array.from(document.querySelectorAll('.order-i')).map((row, idx) => {
            const qty = row.querySelector('input.j-quantity-p');
            const titleLink = Array.from(row.querySelectorAll('a')).find((a) => (a.innerText || '').trim());
            return {
                idx,
                text: (row.innerText || '').replace(/\\s+/g, ' ').trim(),
                title: titleLink ? (titleLink.innerText || '').trim() : '',
                href: titleLink ? (titleLink.href || '') : '',
                qty: qty ? (qty.value || '') : ''
            };
        })"""
    )


def _match_row_for_item(rows: list[dict[str, Any]], added_item: dict, sku: str) -> int | None:
    href = str(added_item.get("href") or "").split("?", 1)[0].rstrip("/")
    title = str(added_item.get("title") or "").strip()
    for row in rows:
        row_href = str(row.get("href") or "").split("?", 1)[0].rstrip("/")
        if href and row_href and href == row_href:
            return int(row["idx"])
    if title:
        for row in rows:
            if title in str(row.get("text") or ""):
                return int(row["idx"])
    for row in rows:
        if sku in str(row.get("text") or ""):
            return int(row["idx"])
    return None


async def _set_item_quantities(page, items: list[Item], added: list[dict]) -> list[dict]:
    await _goto_retry(page, CHECKOUT_URL)
    await _safe_wait_networkidle(page)
    rows = await _read_checkout_rows(page)
    if not rows:
        raise StageError("set_quantities", "Checkout cart is empty after adding items.", {})

    results: list[dict] = []
    for item, added_item in zip(items, added):
        for _ in range(20):
            rows = await _read_checkout_rows(page)
            row_idx = _match_row_for_item(rows, added_item, item.sku)
            if row_idx is None:
                raise StageError(
                    "set_quantities",
                    f"Cart row not found for sku={item.sku}",
                    {"sku": item.sku, "rows": rows, "added_item": added_item},
                )
            row = page.locator(".order-i").nth(row_idx)
            qty_input = row.locator("input.j-quantity-p").first
            current_raw = (await qty_input.input_value(timeout=TIMEOUT_MS)).strip()
            try:
                current = int(current_raw)
            except Exception:
                raise StageError("set_quantities", "Invalid checkout qty value.", {"sku": item.sku, "value": current_raw})
            if current == item.qty:
                results.append({"sku": item.sku, "qty": item.qty, "row_idx": row_idx})
                break
            if current < item.qty:
                btn = row.locator("button.j-increase-p").first
            else:
                btn = row.locator("button.j-decrease-p").first
            await btn.wait_for(state="visible", timeout=TIMEOUT_MS)
            await btn.click(timeout=TIMEOUT_MS)
            await page.wait_for_timeout(900)
        else:
            raise StageError("set_quantities", f"Could not set qty for sku={item.sku}", {"sku": item.sku, "target": item.qty})

    return results


async def _select_city(page, city_query: str) -> dict:
    city_input = page.locator("#checkout-city").first
    await city_input.wait_for(state="visible", timeout=TIMEOUT_MS)

    api_result = await page.evaluate(
        """async ([cityQuery, timeoutMs]) => {
            const mod = window.CheckoutModule && CheckoutModule.getInstance && CheckoutModule.getInstance();
            const recipient = mod && mod.getComponentByName && mod.getComponentByName('Recipient');
            if (!recipient || !recipient.performAction || !recipient.setCity) {
                return {ok: false, reason: 'recipient_component_unavailable'};
            }
            const response = await new Promise((resolve) => {
                let done = false;
                const finish = (value) => {
                    if (done) return;
                    done = true;
                    resolve(value);
                };
                try {
                    const req = recipient.performAction('findCities', {term: cityQuery}, (status, data) => finish({status, data}));
                    if (req && req.fail) {
                        req.fail((xhr, textStatus, errorThrown) => finish({
                            status: 'AJAX_FAIL',
                            textStatus,
                            error: String(errorThrown || ''),
                            responseText: xhr && xhr.responseText ? String(xhr.responseText).slice(0, 500) : ''
                        }));
                    }
                } catch (e) {
                    finish({status: 'EXCEPTION', error: String(e)});
                }
                setTimeout(() => finish({status: 'TIMEOUT'}), timeoutMs);
            });
            const cities = response && response.data && Array.isArray(response.data.cities) ? response.data.cities : [];
            const city = cities.find((item) => item && item.id && !item.disabled) || null;
            if (!city) return {ok: false, reason: 'city_not_found', response};
            recipient.setCity(city.label || city.NPCityDescription || city.value || cityQuery, String(city.id), city.NPCityDescription || '');
            return {
                ok: true,
                city: {
                    id: String(city.id),
                    label: city.label || '',
                    NPCityDescription: city.NPCityDescription || ''
                },
                responseStatus: response.status
            };
        }""",
        [city_query, TIMEOUT_MS],
    )
    if isinstance(api_result, dict) and api_result.get("ok"):
        await page.wait_for_timeout(1800)
        city_value = (await city_input.input_value(timeout=TIMEOUT_MS)).strip()
        city_id = await page.locator('input[name="Recipient[delivery_city_id]"]').first.input_value(timeout=TIMEOUT_MS)
        if city_id:
            city = api_result.get("city") if isinstance(api_result.get("city"), dict) else {}
            return {
                "city_query": city_query,
                "city_value": city_value,
                "city_id": city_id,
                "city_label": city.get("label", ""),
                "np_city_description": city.get("NPCityDescription", ""),
                "selection": "api",
            }

    await city_input.click(timeout=TIMEOUT_MS)
    await city_input.fill("", timeout=TIMEOUT_MS)
    await city_input.type(city_query, delay=35, timeout=TIMEOUT_MS)
    await page.wait_for_timeout(1200)

    option = page.locator(".ui-autocomplete.ui-menu .ui-menu-item", has_text=f"г. {city_query}").first
    if await option.count() == 0:
        option = page.locator(".ui-autocomplete.ui-menu .ui-menu-item", has_text=city_query).first
    if await option.count() == 0:
        options = await page.evaluate(
            """() => Array.from(document.querySelectorAll('.ui-autocomplete.ui-menu .ui-menu-item')).map((el) => ({
                text: (el.innerText || el.textContent || '').trim(),
                disabled: el.classList.contains('ui-state-disabled')
            })).filter((item) => item.text)"""
        )
        query_norm = _norm_match_text(city_query)
        chosen_idx = -1
        if isinstance(options, list):
            for idx, item in enumerate(options):
                if not isinstance(item, dict) or item.get("disabled"):
                    continue
                text_norm = _norm_match_text(str(item.get("text") or ""))
                if query_norm and query_norm in text_norm:
                    chosen_idx = idx
                    break
            if chosen_idx < 0:
                for idx, item in enumerate(options):
                    if isinstance(item, dict) and item.get("text") and not item.get("disabled"):
                        chosen_idx = idx
                        break
        if chosen_idx >= 0:
            option = page.locator(".ui-autocomplete.ui-menu .ui-menu-item").nth(chosen_idx)
        else:
            raise StageError(
                "fill_checkout",
                "City autocomplete option not found.",
                {"city_query": city_query, "api_result": api_result, "options": options},
            )
    await option.click(timeout=TIMEOUT_MS)
    await page.wait_for_timeout(1800)

    city_value = (await city_input.input_value(timeout=TIMEOUT_MS)).strip()
    city_id = await page.locator('input[name="Recipient[delivery_city_id]"]').first.input_value(timeout=TIMEOUT_MS)
    if not city_id:
        raise StageError(
            "fill_checkout",
            "City id was not set after city selection.",
            {"city_query": city_query, "city_value": city_value, "api_result": api_result},
        )
    return {"city_query": city_query, "city_value": city_value, "city_id": city_id, "selection": "ui"}


async def _select_payment_cod(page) -> dict:
    payment = page.locator("#checkout-payment-type").first
    await payment.wait_for(state="attached", timeout=TIMEOUT_MS)
    await page.evaluate(
        """(value) => {
            const sel = document.querySelector('#checkout-payment-type');
            if (!sel) throw new Error('payment select not found');
            sel.value = value;
            sel.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        PAYMENT_COD_VALUE,
    )
    await page.wait_for_timeout(700)
    value = await payment.input_value(timeout=TIMEOUT_MS)
    text = await page.evaluate(
        """() => {
            const sel = document.querySelector('#checkout-payment-type');
            return sel && sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '';
        }"""
    )
    if value != PAYMENT_COD_VALUE:
        raise StageError("fill_checkout", "Payment type was not selected.", {"value": value, "expected": PAYMENT_COD_VALUE, "text": text})
    return {"value": value, "text": text}


async def _select_warehouse(page, recipient: Recipient) -> dict:
    deadline = asyncio.get_running_loop().time() + (TIMEOUT_MS / 1000.0)
    meta: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        meta = await page.evaluate(
            """([branchNumber, branchQuery]) => {
                const sel = Array.from(document.querySelectorAll('select')).find((s) => s.name.includes('warehouse.id'));
                if (!sel) return {found: false};
                const options = Array.from(sel.options).map((o) => ({value: o.value, text: o.text, selected: o.selected}));
                const num = String(branchNumber || '').trim();
                const query = String(branchQuery || '').trim().toLowerCase();
                let option = null;
                if (num) {
                    const re = new RegExp(`(?:Отделение|Відділення|Пункт)\\\\s*№${num}(?!\\\\d)`, 'i');
                    option = options.find((o) => re.test(o.text));
                }
                if (!option && query) {
                    option = options.find((o) => o.text.toLowerCase().includes(query));
                }
                return {
                    found: true,
                    name: sel.name,
                    id: sel.id,
                    current: sel.value,
                    selectedText: sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '',
                    option,
                    optionCount: options.length
                };
            }""",
            [recipient.branch_number, recipient.branch_query],
        )
        if meta.get("found") and meta.get("option"):
            break
        await page.wait_for_timeout(300)
    if not meta.get("found"):
        raise StageError("fill_checkout", "Warehouse select did not appear.", {"recipient": recipient.__dict__})
    option = meta.get("option")
    if not option:
        raise StageError(
            "fill_checkout",
            "Warehouse option not found.",
            {"branch_number": recipient.branch_number, "branch_query": recipient.branch_query, "meta": meta},
        )

    warehouse = page.locator(f'select[name="{meta["name"]}"]').first
    await warehouse.wait_for(state="attached", timeout=TIMEOUT_MS)
    await page.evaluate(
        """([name, value]) => {
            const sel = Array.from(document.querySelectorAll('select')).find((s) => s.name === name);
            if (!sel) throw new Error('warehouse select not found');
            sel.value = value;
            sel.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        [meta["name"], str(option["value"])],
    )
    await page.wait_for_timeout(900)
    selected_text = await page.evaluate(
        """(name) => {
            const sel = Array.from(document.querySelectorAll('select')).find((s) => s.name === name);
            return sel && sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '';
        }""",
        meta["name"],
    )
    if str(option["text"]) != str(selected_text):
        raise StageError("fill_checkout", "Warehouse selection did not stick.", {"expected": option, "selected_text": selected_text})
    return {"value": option["value"], "text": selected_text, "branch_number": recipient.branch_number}


async def _apply_coupon(page) -> dict:
    if not PROMO_CODE:
        return {"code": "", "applied": False, "text": ""}

    coupon_input = page.locator(".j-coupon-input").first
    input_visible = await coupon_input.count() > 0 and await coupon_input.is_visible()
    if not input_visible:
        toggle = page.locator("a.j-coupon-add").first
        if await toggle.count() > 0:
            await toggle.click(timeout=TIMEOUT_MS, force=True)
            await page.wait_for_timeout(500)

    if await coupon_input.count() == 0 or not await coupon_input.is_visible():
        raise StageError("fill_checkout", "Coupon input is not visible.", {"code": PROMO_CODE})

    await coupon_input.fill(PROMO_CODE, timeout=TIMEOUT_MS)
    submit = page.locator("a.j-coupon-submit").first
    await submit.wait_for(state="visible", timeout=TIMEOUT_MS)
    await submit.click(timeout=TIMEOUT_MS)
    await page.wait_for_timeout(1800)
    coupon_text = await page.evaluate(
        """() => {
            const text = document.body.innerText || '';
            const lines = text.split('\\n').map((x) => x.trim()).filter(Boolean);
            return lines.filter((x) => /купон|скид|SALE/i.test(x)).join(' | ');
        }"""
    )
    return {"code": PROMO_CODE, "applied": bool(coupon_text), "text": coupon_text}


async def _fill_text_field(page, selector: str, value: str, *, clear: bool = True) -> str:
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=TIMEOUT_MS)
    await loc.click(timeout=TIMEOUT_MS)
    if clear:
        await loc.press("ControlOrMeta+A", timeout=TIMEOUT_MS)
        await loc.press("Backspace", timeout=TIMEOUT_MS)
    await loc.type(value, delay=25, timeout=TIMEOUT_MS)
    await loc.press("Tab", timeout=TIMEOUT_MS)
    await page.wait_for_timeout(200)
    return (await loc.input_value(timeout=TIMEOUT_MS)).strip()


async def _fill_recipient_fields(page, recipient: Recipient) -> dict:
    name_value = await _fill_text_field(page, "#checkout-name", recipient.name)
    phone_value = await _fill_text_field(page, "#checkout-phone", recipient.phone_input)
    email_value = ""
    if recipient.email:
        email_value = await _fill_text_field(page, "#checkout-email", recipient.email)

    expected_phone_tail = recipient.phone_input[-7:]
    if not name_value:
        raise StageError("fill_checkout", "Recipient name is empty after fill.", {"expected": recipient.name})
    if expected_phone_tail not in "".join(ch for ch in phone_value if ch.isdigit()):
        raise StageError(
            "fill_checkout",
            "Recipient phone did not match after fill.",
            {"source": recipient.phone_source, "input": recipient.phone_input, "value": phone_value},
        )
    return {"name": name_value, "phone": phone_value, "email": email_value}


async def _read_totals(page) -> dict:
    return await page.evaluate(
        """() => {
            const text = document.body.innerText || '';
            const lines = text.split('\\n').map((x) => x.trim()).filter(Boolean);
            return {
                totals: lines.filter((x) => /^(Итого|Скидка|Доставка)|грн$|–\\s*\\d/.test(x)).slice(-30),
                summary: lines.filter((x) => /Итого|Скидка по купону|грн/.test(x)).slice(-30)
            };
        }"""
    )


async def _fill_checkout(page, recipient: Recipient) -> dict:
    await _goto_retry(page, CHECKOUT_URL)
    await _safe_wait_networkidle(page)

    city = await _select_city(page, recipient.city_query)
    payment = await _select_payment_cod(page)
    warehouse = await _select_warehouse(page, recipient)
    coupon = await _apply_coupon(page)
    customer = await _fill_recipient_fields(page, recipient)

    submit = page.locator("button.j-submit", has_text="Оформить заказ").first
    await submit.wait_for(state="visible", timeout=TIMEOUT_MS)
    if not await submit.is_enabled():
        raise StageError("fill_checkout", "Submit button is disabled after checkout fill.", {})

    rows = await _read_checkout_rows(page)
    totals = await _read_totals(page)
    return {
        "customer": customer,
        "delivery": {"city": city, "warehouse": warehouse},
        "payment": payment,
        "coupon": coupon,
        "rows": rows,
        "totals": totals,
    }


def _parse_supplier_order_number(url: str, body_text: str) -> str:
    patterns = [
        r"/order/(\d+)",
        r"(?:заказ|замовлення|order)\s*(?:№|#|No\.?|Nº)?\s*(\d{3,})",
        r"(?:заказ|замовлення|order)\D{0,40}(\d{3,})",
        r"№\s*(\d{3,})",
    ]
    haystacks = [url or "", body_text or ""]
    for haystack in haystacks:
        for pattern in patterns:
            m = re.search(pattern, haystack, flags=re.IGNORECASE)
            if m:
                return m.group(1)
    return ""


async def _submit_order(page) -> str:
    submit = page.locator("button.j-submit", has_text="Оформить заказ").first
    await submit.wait_for(state="visible", timeout=TIMEOUT_MS)
    await submit.click(timeout=TIMEOUT_MS)
    _write_submit_checkpoint(url=page.url or "", submitted=True)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
    except PWTimeoutError:
        pass
    try:
        await page.wait_for_function(
            """() => {
                const text = document.body ? document.body.innerText : '';
                return /Ваш\\s+заказ\\s+принят/i.test(text)
                    || /Заказ\\s*№\\s*\\d{3,}/i.test(text)
                    || /(?:замовлення|order)\\D{0,40}\\d{3,}/i.test(text);
            }""",
            timeout=TIMEOUT_MS,
        )
    except PWTimeoutError:
        await page.wait_for_timeout(2500)

    body_text = ""
    try:
        body_text = await page.locator("body").inner_text(timeout=TIMEOUT_MS)
    except Exception:
        pass
    order_no = _parse_supplier_order_number(page.url or "", body_text)
    _write_submit_checkpoint(url=page.url or "", submitted=True, order_number=order_no)
    if not order_no:
        raise StageError(
            "post_submit_order_number",
            "Supplier order was submitted, but Dobavki order number could not be parsed.",
            {
                "submitted": True,
                "url": page.url or "",
                "body_excerpt": re.sub(r"\s+", " ", body_text or "").strip()[:1000],
            },
        )
    return order_no


async def _run() -> tuple[bool, dict]:
    items = _parse_items()
    order_payload = _parse_order_payload()
    recipient = _extract_recipient(order_payload)

    browser = None
    context = None
    page = None
    stage = "init"
    added: list[dict] = []
    quantity_result: list[dict] = []
    checkout_result: dict[str, Any] = {}
    supplier_order_number = ""
    submitted = False
    paused_for_error = False

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=HEADLESS, args=["--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(
                locale="ru-RU",
                timezone_id="Europe/Kiev",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            await context.add_cookies(
                [
                    {
                        "name": "challenge_passed",
                        "value": "267d96f1c40f797eb54a2e87d56b51d1c409b40dd91f567631828d9f455161cc",
                        "domain": "dobavki.ua",
                        "path": "/",
                        "sameSite": "Lax",
                    }
                ]
            )
            page = await context.new_page()

            if CLEAR_BASKET:
                stage = "clear_basket"
                clear_result = await _clear_basket(page)
            else:
                clear_result = {"removed": 0, "skipped": True}

            stage = "add_items"
            added = await _add_items(page, items)

            stage = "set_quantities"
            quantity_result = await _set_item_quantities(page, items, added)

            stage = "fill_checkout"
            checkout_result = await _fill_checkout(page, recipient)

            if DRY_RUN:
                supplier_order_number = "DRY_RUN"
            else:
                stage = "submit_order"
                supplier_order_number = await _submit_order(page)
                submitted = True

            return True, {
                "ok": True,
                "submitted": submitted,
                "dry_run": DRY_RUN,
                "added": added,
                "quantities": quantity_result,
                "clear_basket": clear_result,
                "supplier_order_number": supplier_order_number,
                "url": page.url or CHECKOUT_URL,
                **checkout_result,
            }
    except StageError as e:
        await _debug_pause_if_needed()
        paused_for_error = True
        return False, {
            "ok": False,
            "error": str(e),
            "stage": e.stage or stage,
            "url": page.url if page is not None else CHECKOUT_URL,
            "submitted": bool((e.details or {}).get("submitted")) or submitted,
            "details": e.details or {},
        }
    except Exception as e:
        await _debug_pause_if_needed()
        paused_for_error = True
        return False, {
            "ok": False,
            "error": str(e),
            "stage": stage,
            "url": page.url if page is not None else CHECKOUT_URL,
            "submitted": submitted,
            "details": {},
        }
    finally:
        try:
            if not paused_for_error:
                await _debug_pause_if_needed()
        except Exception:
            pass
        try:
            if page is not None:
                await page.close()
        except Exception:
            pass
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


def sup2_select_branch(*args, **kwargs):
    raise NotImplementedError


def sup2_confirm_order(*args, **kwargs):
    raise NotImplementedError


def main() -> int:
    try:
        ok, payload = asyncio.run(_run())
    except Exception as e:
        payload = {"ok": False, "error": str(e), "stage": "init", "url": CHECKOUT_URL, "details": {}}
        ok = False

    print(SUPPLIER_RESULT_JSON_PREFIX + json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
