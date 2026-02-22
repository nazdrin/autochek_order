# scripts/step9_confirm_order.py
import asyncio
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")
TIMEOUT_MS = int(os.getenv("BIOTUS_TIMEOUT_MS", "25000"))

ART = ROOT / "artifacts"
ART.mkdir(parents=True, exist_ok=True)


async def pick_checkout_page(context):
    pages = list(context.pages)
    for p in pages:
        try:
            if p.is_closed():
                continue
        except Exception:
            pass
        try:
            url = p.url or ""
        except Exception:
            url = ""
        if "opt.biotus" in url and "/checkout" in url:
            try:
                await p.bring_to_front()
            except Exception:
                pass
            return p

    # fallback: любая вкладка opt.biotus
    for p in pages:
        try:
            url = p.url or ""
        except Exception:
            url = ""
        if "opt.biotus" in url:
            try:
                await p.bring_to_front()
            except Exception:
                pass
            return p

    if pages:
        return pages[0]

    return await context.new_page()


async def _wait_not_disabled(locator, timeout_ms: int):
    """Ждём, пока у кнопки исчезнет disabled (и она станет кликабельной)."""
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_err = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await locator.count() == 0:
                await asyncio.sleep(0.2)
                continue
            el = locator.first
            # visible?
            try:
                await el.wait_for(state="visible", timeout=1500)
            except Exception:
                await asyncio.sleep(0.2)
                continue

            # disabled attr?
            disabled = await el.get_attribute("disabled")
            if disabled is None:
                return True
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.25)

    if last_err:
        raise RuntimeError(f"Кнопка не стала активной: {last_err}")
    raise RuntimeError("Кнопка не стала активной (disabled не исчез) за таймаут.")


async def _dom_click(locator):
    """Click via DOM (element.click()) as a fallback when Playwright click is swallowed by overlays."""
    try:
        await locator.evaluate("el => el.click()")
        return True
    except Exception:
        return False


async def _click_payment_confirm_if_shown(page, timeout_ms: int) -> bool:
    """Если после подтверждения заказа появилось окно 'Оплата замовлення' — нажимаем 'Підтвердити'.

    Важно: на странице иногда есть несколько элементов с текстом 'Підтвердити'.
    Поэтому кликаем ТОЛЬКО внутри модалки оплаты.

    Проблема, которую лечим:
    - в CDP/каскаде модалка может появляться с задержкой;
    - `:visible` иногда не совпадает с тем, что реально видно из‑за анимаций/перерисовок.
    Поэтому сначала ждём появления/видимости модалки, а уже потом ищем кнопку.
    """

    # Локаторы без :visible — видимость будем проверять через wait_for/is_visible
    overlay = page.locator("div.confirmBalanceOverlay")
    block = page.locator("div.balance-confirm-block")
    title = page.get_by_text("Оплата замовлення", exact=False)

    # 1) Ждём, что модалка реально появилась/стала видимой (до 7 сек или меньше общего таймаута)
    wait_deadline = asyncio.get_event_loop().time() + min(timeout_ms, 7000) / 1000
    container = None

    while asyncio.get_event_loop().time() < wait_deadline:
        try:
            if await block.count() > 0:
                try:
                    await block.first.wait_for(state="visible", timeout=250)
                    container = block.first
                    break
                except Exception:
                    pass
            if await overlay.count() > 0:
                try:
                    await overlay.first.wait_for(state="visible", timeout=250)
                    container = overlay.first
                    break
                except Exception:
                    pass
            if await title.count() > 0:
                try:
                    if await title.first.is_visible():
                        # Поднимаем контейнер от заголовка
                        cand = title.first.locator(
                            "xpath=ancestor::div[contains(@class,'balance-confirm-block')][1]"
                        )
                        if await cand.count() == 0:
                            cand = title.first.locator(
                                "xpath=ancestor::div[contains(@class,'confirmBalanceOverlay')][1]"
                            )
                        if await cand.count() > 0:
                            try:
                                await cand.first.wait_for(state="visible", timeout=250)
                            except Exception:
                                pass
                            container = cand.first
                            break
                except Exception:
                    pass
        except Exception:
            pass

        await asyncio.sleep(0.15)

    # Модалки нет
    if container is None:
        return False

    # Скрин перед кликом
    try:
        await page.screenshot(path=str(ART / "step9_2_payment_modal_before.png"), full_page=True)
    except Exception:
        pass

    # Если это модалка с недостаточным балансом — НЕ продолжаем.
    # В таком кейсе заказ не оформится, поэтому нужно завершать шаг ошибкой,
    # чтобы оркестратор НЕ менял статус заказа в SalesDrive.
    try:
        topup_btn = container.get_by_role("button", name=re.compile(r"Поповнити\s+баланс", re.I))
        if await topup_btn.count() > 0 and await topup_btn.first.is_visible():
            try:
                await page.screenshot(path=str(ART / "step9_err_insufficient_balance.png"), full_page=True)
            except Exception:
                pass
            raise RuntimeError("Недостаточно баланса: показано окно 'Поповнити баланс'. Заказ не подтвержден.")
    except Exception:
        # если не смогли проверить — продолжаем штатно
        pass

    # 2) Находим кнопку подтверждения строго внутри контейнера
    confirm_btn = container.locator("button:has-text('Підтвердити')")

    # Иногда текст внутри span
    if await confirm_btn.count() == 0:
        confirm_btn = container.get_by_role("button", name="Підтвердити")

    # Берём ТОЛЬКО видимую кнопку
    confirm_btn = confirm_btn.filter(has_not=container.locator("[disabled]"))

    # Если всё равно не нашли — попробуем более узкий селектор по верстке
    if await confirm_btn.count() == 0:
        confirm_btn = container.locator("div.messageBox button.button")

    if await confirm_btn.count() == 0:
        return False

    btn = confirm_btn.first

    # 3) Скролл/фокус — помогает Alpine
    try:
        await btn.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    try:
        await btn.focus(timeout=1000)
    except Exception:
        pass

    # Ждём, что кнопка кликабельна (если disabled используется)
    try:
        await _wait_not_disabled(confirm_btn, min(timeout_ms, 8000))
    except Exception:
        pass

    # 4) Кликаем (click -> force -> dispatch MouseEvent)
    clicked = False
    try:
        await btn.click(timeout=min(timeout_ms, 8000))
        clicked = True
    except Exception:
        pass

    if not clicked:
        try:
            await btn.click(force=True)
            clicked = True
        except Exception:
            pass

    if not clicked:
        # Alpine иногда "проглатывает" element.click(); шлём полноценный MouseEvent
        try:
            await btn.evaluate(
                """el => el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}))"""
            )
            clicked = True
        except Exception:
            pass

    if not clicked:
        return False

    # небольшой буфер, чтобы запрос/обработчик успели отработать
    await page.wait_for_timeout(600)

    # 5) Ждём признак успеха:
    # - модалка скрылась
    # - или URL сменился (уходим с /checkout)
    # - или появилась страница/текст успеха
    deadline = asyncio.get_event_loop().time() + min(timeout_ms, 20000) / 1000
    success_text = page.get_by_text("Дякуємо", exact=False)

    while asyncio.get_event_loop().time() < deadline:
        # 5.1 контейнер исчез/скрылся
        try:
            await container.wait_for(state="hidden", timeout=250)
            break
        except Exception:
            pass

        # 5.2 редирект
        try:
            url = page.url or ""
            if "opt.biotus" in url and "/checkout" not in url:
                break
        except Exception:
            pass

        # 5.3 текст успеха
        try:
            if await success_text.count() > 0 and await success_text.first.is_visible():
                break
        except Exception:
            pass

        await asyncio.sleep(0.25)

    # Если модалка всё ещё видна — считаем, что клик не сработал
    try:
        if await container.is_visible():
            return False
    except Exception:
        pass

    # Доп. защита: иногда после "Підтвердити" (из модалки) сайт редиректит на страницу баланса
    # из-за недостатка средств. В этом случае заказ НЕ оформлен — шаг должен упасть.
    try:
        url = page.url or ""
    except Exception:
        url = ""

    try:
        balance_hint = page.get_by_text("Ваш баланс", exact=False)
        topup_any = page.get_by_role("button", name=re.compile(r"Поповнити\s+баланс", re.I))
        not_enough = page.get_by_text(re.compile(r"не\s+вистачає", re.I))

        if "/balance" in url or "/balance/index" in url:
            try:
                await page.screenshot(path=str(ART / "step9_err_redirect_balance.png"), full_page=True)
            except Exception:
                pass
            raise RuntimeError("Недостатньо балансу: редирект на сторінку балансу після підтвердження оплати.")

        # Иногда URL ещё не сменился, но уже отрисована страница баланса/кнопка пополнения
        if (await balance_hint.count() > 0 and await balance_hint.first.is_visible()) or (
            await topup_any.count() > 0 and await topup_any.first.is_visible()
        ) or (await not_enough.count() > 0 and await not_enough.first.is_visible()):
            try:
                await page.screenshot(path=str(ART / "step9_err_insufficient_balance_after.png"), full_page=True)
            except Exception:
                pass
            raise RuntimeError("Недостатньо балансу: показано сторінку/кнопку поповнення після підтвердження.")
    except RuntimeError:
        raise
    except Exception:
        # не валим шаг из-за проблем с проверкой
        pass

    try:
        await page.screenshot(path=str(ART / "step9_3_payment_modal_after.png"), full_page=True)
    except Exception:
        pass

    return True


async def _extract_order_number_if_success(page) -> tuple[str, str]:
    warning = ""
    success_re = re.compile(r".*/checkout/onepage/success(?:[/?#].*)?$")

    # Пытаемся дождаться success URL, но не ломаем успешный шаг, если номер не удалось вытащить.
    try:
        await page.wait_for_url(success_re, timeout=min(TIMEOUT_MS, 15000))
    except Exception:
        pass

    try:
        current_url = page.url or ""
    except Exception:
        current_url = ""

    if not success_re.match(current_url):
        return "", "order number not found"

    raw_text = ""
    try:
        order_block = page.locator("div.order-number").first
        if await order_block.count() > 0:
            raw_text = (await order_block.inner_text(timeout=2000)).strip()
    except Exception:
        raw_text = ""

    if not raw_text:
        try:
            fallback = page.get_by_text(re.compile(r"Замовлення.*BO-", re.I)).first
            if await fallback.count() > 0:
                raw_text = (await fallback.inner_text(timeout=2000)).strip()
        except Exception:
            raw_text = ""

    if raw_text:
        m = re.search(r"\b(BO-\d+)\b", raw_text) or re.search(r"№\s*(BO-\d+)", raw_text)
        if m:
            return m.group(1), ""

    return "", "order number not found"


async def main() -> dict:
    async with async_playwright() as p:
        if USE_CDP:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await pick_checkout_page(context)
        else:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

        await page.wait_for_timeout(200)

        # Кнопка "Підтверджую замовлення" (основной способ)
        btn = page.get_by_role("button", name="Підтверджую замовлення").locator(":visible")

        # fallback: если роль не отработала — по тексту
        if await btn.count() == 0:
            btn = page.get_by_text("Підтверджую замовлення", exact=False).locator(":visible").first

        if await btn.count() == 0:
            await page.screenshot(path=str(ART / "step9_err_no_button.png"), full_page=True)
            raise RuntimeError('Не нашёл кнопку "Підтверджую замовлення" на checkout.')

        await page.screenshot(path=str(ART / "step9_0_before_click.png"), full_page=True)

        # Ждём, пока кнопка станет активной (перестанет быть disabled)
        await _wait_not_disabled(btn, TIMEOUT_MS)

        # Клик
        try:
            await btn.first.click(timeout=TIMEOUT_MS)
        except PWTimeoutError:
            # fallback: force click
            await btn.first.click(force=True)

        await page.wait_for_timeout(700)
        await page.screenshot(path=str(ART / "step9_1_after_click.png"), full_page=True)

        # Если появилось окно 'Оплата замовлення' — подтверждаем оплату
        confirmed = False
        try:
            # Даём странице шанс показать модалку (иногда появляется не сразу)
            confirmed = await _click_payment_confirm_if_shown(page, TIMEOUT_MS)
        except RuntimeError as e:
            # Критические бизнес-ошибки (например, недостаточно баланса) должны падать наружу.
            raise
        except Exception:
            confirmed = False

        if confirmed:
            print("OK: clicked 'Підтверджую замовлення' and confirmed payment modal ('Підтвердити').")
        else:
            print("OK: clicked 'Підтверджую замовлення'. (Payment modal not confirmed / not shown)")

        # Финальная проверка: если оказались на странице баланса/пополнения — заказ не оформлен.
        try:
            await page.wait_for_timeout(500)
        except Exception:
            pass

        try:
            url = page.url or ""
        except Exception:
            url = ""

        try:
            balance_hint = page.get_by_text("Ваш баланс", exact=False)
            topup_any = page.get_by_role("button", name=re.compile(r"Поповнити\s+баланс", re.I))
            not_enough = page.get_by_text(re.compile(r"не\s+вистачає", re.I))

            if "/balance" in url or "/balance/index" in url:
                try:
                    await page.screenshot(path=str(ART / "step9_err_balance_page_final.png"), full_page=True)
                except Exception:
                    pass
                raise RuntimeError("Недостатньо балансу: відкрилася сторінка балансу. Замовлення не оформлено.")

            if (await balance_hint.count() > 0 and await balance_hint.first.is_visible()) or (
                await topup_any.count() > 0 and await topup_any.first.is_visible()
            ) or (await not_enough.count() > 0 and await not_enough.first.is_visible()):
                try:
                    await page.screenshot(path=str(ART / "step9_err_balance_hint_final.png"), full_page=True)
                except Exception:
                    pass
                raise RuntimeError("Недостатньо балансу: знайдено 'Поповнити баланс/Ваш баланс/не вистачає'.")
        except RuntimeError:
            raise
        except Exception:
            pass

        order_number, warning = await _extract_order_number_if_success(page)

        if not USE_CDP:
            await context.close()
            await browser.close()

        payload = {"ok": True, "order_number": order_number}
        if warning:
            payload["warning"] = warning
        return payload


if __name__ == "__main__":
    try:
        payload = asyncio.run(main())
        print(json.dumps(payload, ensure_ascii=False))
        raise SystemExit(0)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        raise SystemExit(2)
