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
# BIOTUS_BRANCH_QUERY="8"  или "Відділення №8"
BRANCH_QUERY = os.getenv("BIOTUS_BRANCH_QUERY", "8").strip()

# если хочешь дополнительно зафиксировать конкретный адрес/часть строки:
# BIOTUS_BRANCH_MUST_CONTAIN="Набережно-Хрещатицька"

BRANCH_MUST_CONTAIN = os.getenv("BIOTUS_BRANCH_MUST_CONTAIN", "").strip()


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
    # Сначала попробуем уже открытые страницы
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
        # запасной вариант: любая вкладка opt.biotus
        if ("opt.biotus" in (url or "")) and best is None:
            best = p

    if best:
        try:
            await best.bring_to_front()
        except Exception:
            pass
        return best

    # Если вообще нет страниц (или все пустые) — создадим новую
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

        # В CDP не берем contexts[0].pages[0] вслепую — часто это about:blank,
        # или другая вкладка. Ищем активную вкладку checkout.
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


async def _delivery_np_section(page):
    """Вернуть локатор секции доставки НП 'до відділення' (где поле отделения).

    Важно: на странице есть похожий выпадающий контрол города (step5) и контрол отделения.
    Самый надежный якорь для отделения — контейнер с классами `container_shipping_method` и `container_WarehouseWarehouse`
    (по сохраненному HTML страницы).
    """
    # 1) Самый надежный селектор по классам контейнера (есть в HTML checkout)
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

    # 2) Фолбэк: ищем по тексту опции и поднимаемся до ближайшего большого контейнера
    opt = page.get_by_text("Нова пошта до відділення", exact=False).first
    if await opt.count() > 0:
        root = opt.locator('xpath=ancestor::div[contains(@class,"container_shipping_method")][1]')
        if await root.count() > 0:
            return root.first
        root2 = opt.locator('xpath=ancestor::*[self::section or self::div][3]')
        if await root2.count() > 0:
            return root2.first

    # 3) Последний фолбэк: секция по заголовку "Доставка"
    delivery_title = page.get_by_text("Доставка", exact=False).first
    if await delivery_title.count() > 0:
        root = delivery_title.locator('xpath=ancestor::*[self::section or self::div][2]')
        if await root.count() > 0:
            return root.first

    return page


async def _ensure_np_branch_mode(page):
    sec = await _delivery_np_section(page)

    # Критично: клик по тексту может не переключать радио.
    # Ищем input[type=radio] внутри секции и делаем check().
    radio = sec.locator('input[type="radio"]:visible')

    # Иногда радио скрыто, а кликабельный label/div видим.
    # Поэтому: 1) пробуем check() по radio, 2) если не вышло — кликаем по контейнеру секции.
    try:
        if await radio.count() > 0:
            # если уже выбран — ок
            try:
                if await radio.first.is_checked():
                    return
            except Exception:
                pass
            await radio.first.check(force=True)
    except Exception:
        pass

    # Фолбэк: клик по секции/лейблу (реальным кликом мыши)
    try:
        clickable = sec.locator('label:has-text("Нова пошта до відділення"), div:has-text("Нова пошта до відділення")').first
        if await clickable.count() > 0:
            await _human_click(page, clickable)
        else:
            txt = sec.get_by_text("Нова пошта до відділення", exact=False).first
            if await txt.count() > 0:
                await _human_click(page, txt)
    except Exception:
        pass

    # Ждём, что контрол отделения появился/стал доступен в ЭТОЙ секции.
    # (Проверяем по наличию ss-main или placeholder)
    for _ in range(80):  # ~8 секунд
        try:
            if await sec.locator('div.ss-main').count() > 0:
                break
            if await sec.get_by_text("Введіть вулицю", exact=False).count() > 0:
                break
        except Exception:
            pass
        await page.wait_for_timeout(100)


async def _find_branch_input(page):
    sec = await _delivery_np_section(page)

    # Важно: НЕ полагаемся на name у <select>.
    # На странице есть похожая логика для города (step5), но для отделений name/структура
    # может отличаться между версиями. Самый стабильный якорь — плейсхолдер
    # "Введіть вулицю або номер відділення".

    trigger = None

    # 1) Самый точный: ss-main в секции, содержащий нужный плейсхолдер
    cand = sec.locator(
        'div.ss-main:has-text("Введіть вулицю або номер відділення"), '
        'div.ss-main:has-text("Введіть вулицю")'
    ).first
    if await cand.count() > 0:
        trigger = cand

    # 2) Фолбэк: кликабельный текст плейсхолдера внутри секции
    if trigger is None:
        opener_text = sec.get_by_text("Введіть вулицю або номер відділення", exact=False).first
        if await opener_text.count() > 0:
            trigger = opener_text

    # 3) Фолбэк: если секция определилась неидеально — ищем по всей странице
    if trigger is None:
        cand2 = page.locator(
            'div.ss-main:has-text("Введіть вулицю або номер відділення"), '
            'div.ss-main:has-text("Введіть вулицю")'
        ).first
        if await cand2.count() > 0:
            trigger = cand2

    if trigger is None:
        return None

    # Открываем dropdown (реальным кликом мыши — стабильнее для SlimSelect)
    try:
        await _human_click(page, trigger)
    except Exception:
        pass

    # Небольшая пауза: у SlimSelect часто есть анимация открытия
    await page.wait_for_timeout(120)
    # Ждём появления поля поиска в ОТКРЫТОМ выпадающем списке.
    # SlimSelect кладёт input обычно в div.ss-content / div.ss-search (часто как "портал" вне секции).
    popup_input = page.locator(
        'div.ss-content:visible input, '
        'div.ss-search:visible input, '
        'div.ss-content:visible input[type="search"], '
        'input.select2-search__field:visible, '
        'input[role="searchbox"]:visible'
    ).filter(
        has_not=page.locator('input#address-firstname, input#address-telephone, input#track-number')
    )

    for _ in range(120):  # ~12 секунд
        try:
            if await popup_input.count() > 0:
                return popup_input.first
        except Exception:
            pass
        await page.wait_for_timeout(100)

    # Если не появилось поле поиска, возможно список уже открыт, но без input (редко).
    return None


# --- Helper: ensure dropdown open and options visible ---
async def _ensure_branch_dropdown_open(page, inp=None):
    """Make sure the branch dropdown is open and options are present.
    This UI is flaky: the dropdown can close on blur/scroll; we re-open and wait."""
    # If options already visible — ok
    opts = page.locator('div.ss-content:visible div.ss-list .ss-option:visible')
    try:
        if await opts.count() > 0:
            return True
    except Exception:
        pass

    # Re-open by clicking the ss-main in the delivery section
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

    # If we already have an input, focus it again to prevent blur from closing dropdown
    if inp is not None:
        try:
            await _human_click(page, inp)
        except Exception:
            pass

    # Wait a bit for async rendering
    for _ in range(80):  # ~8s
        try:
            if await opts.count() > 0:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(100)

    return False

async def _wait_suggestions_list(page):
    # Prefer SlimSelect dropdown that is actually open (visible)
    slim_opts = page.locator('div.ss-content:visible div.ss-list .ss-option:visible')

    # Fallbacks (other libraries)
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


async def main():
    branch_no = _branch_number_from_query(BRANCH_QUERY)

    async with async_playwright() as p:
        browser, context, page = await _connect(p)

        # sanity-check: в CDP режиме иногда попадаем на about:blank
        if page.url == "about:blank":
            # пробуем еще раз выбрать лучшую вкладку
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

                # Пытаемся найти warehouse select даже если он скрыт
                wh = page.locator('select[name="extension_attributes_warehouse_ref"]')
                print("[DEBUG] warehouse select count:", await wh.count())
                if await wh.count() > 0:
                    try:
                        outer = await wh.first.evaluate('e=>e.outerHTML')
                        print("[DEBUG] warehouse select outerHTML~", outer.replace("\n", " ")[:300], "...")
                    except Exception:
                        pass

                # Пытаемся найти ss-main рядом или в секции
                ss = sec.locator('div.ss-main')
                print("[DEBUG] ss-main in section:", await ss.count())

                fields = page.locator('input:visible, textarea:visible, select:visible, [contenteditable="true"]:visible, [role="textbox"]:visible')
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
                "Не смог открыть/найти поле поиска отделения. "
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

            # Ищем нужную опцию по тексту (label) с защитой от 80/81.
            strict_re = re.compile(rf"^\s*Відділення\s*№\s*{re.escape(branch_no)}(?!\d)", re.IGNORECASE)

            opt_loc = inp.locator('option')
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
                if not strict_re.search(label):
                    continue
                if BRANCH_MUST_CONTAIN and BRANCH_MUST_CONTAIN.lower() not in label.lower():
                    continue
                try:
                    val = await o.get_attribute('value')
                except Exception:
                    val = None
                if val:
                    chosen_value = val
                    chosen_label = label
                    break

            if not chosen_value:
                await page.screenshot(path=str(ART / "step6_err_no_match.png"), full_page=True)
                raise RuntimeError(
                    f"Не нашёл подходящий пункт для отделения №{branch_no} в <select>. "
                    f"Проверь step6_err_no_match.png"
                )

            await inp.select_option(value=chosen_value)
            await page.wait_for_timeout(500)
            await page.screenshot(path=str(ART / "step6_2_after_selected.png"), full_page=True)
            print(f"OK: отделение выбрано (select). label='{chosen_label}'")

        else:
            # Ветка 2: кастомный инпут/комбобокс (SlimSelect)
            strict_re = re.compile(rf"^\s*Відділення\s*№\s*{re.escape(branch_no)}(?!\d)", re.IGNORECASE)

            # вводим так же, как пользователь: "Відділення №8", чтобы не путать с 80/81
            query_to_type = BRANCH_QUERY
            if BRANCH_QUERY.strip().isdigit():
                query_to_type = f"Відділення №{BRANCH_QUERY.strip()}"

            last_err = None

            for attempt in range(1, 4):
                try:
                    # 1) Фокус
                    await _human_click(page, inp)
                    await page.wait_for_timeout(150)

                    # 2) Очистка + ввод
                    try:
                        await inp.fill("")
                    except Exception:
                        await page.keyboard.press("Meta+A")
                        await page.keyboard.press("Backspace")

                    await inp.type(query_to_type, delay=25)
                    await page.wait_for_timeout(250)

                    # 3) Убедиться что список открыт и опции реально появились
                    opened = await _ensure_branch_dropdown_open(page, inp=inp)
                    if not opened:
                        raise RuntimeError("dropdown not opened")

                    opts = await _wait_suggestions_list(page)
                    if not opts:
                        raise RuntimeError("no suggestions")

                    # 4) Даем UI прогрузить подсказки (debounce)
                    await page.wait_for_timeout(250)

                    # 5) Сначала пробуем ENTER (выбирает текущую подсказку)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(600)

                    sec = await _delivery_np_section(page)
                    selected_txt = ""
                    try:
                        selected_txt = (await sec.locator('div.ss-main').first.inner_text()).strip()
                    except Exception:
                        pass

                    ok = False
                    if selected_txt and strict_re.search(selected_txt):
                        if not BRANCH_MUST_CONTAIN or BRANCH_MUST_CONTAIN.lower() in selected_txt.lower():
                            ok = True

                    # 6) Если ENTER не сработал — кликаем по правильной опции
                    if not ok:
                        count = await opts.count()
                        chosen = None
                        for i in range(min(count, 50)):
                            item = opts.nth(i)
                            try:
                                txt = (await item.inner_text()).strip()
                            except Exception:
                                continue
                            if not txt:
                                continue
                            if not strict_re.search(txt):
                                continue
                            if BRANCH_MUST_CONTAIN and BRANCH_MUST_CONTAIN.lower() not in txt.lower():
                                continue
                            chosen = item
                            break

                        if not chosen:
                            raise RuntimeError("no match")

                        try:
                            await chosen.scroll_into_view_if_needed()
                        except Exception:
                            pass

                        await _human_click(page, chosen)
                        await page.wait_for_timeout(650)

                    await page.screenshot(path=str(ART / "step6_2_after_selected.png"), full_page=True)
                    print(f"OK: отделение выбрано. query='{BRANCH_QUERY}', must='{BRANCH_MUST_CONTAIN}'")
                    last_err = None
                    break

                except Exception as e:
                    last_err = e
                    await page.screenshot(path=str(ART / f"step6_retry_{attempt}.png"), full_page=True)
                    # Небольшая пауза перед повтором, UI может моргать/перерисовываться
                    await page.wait_for_timeout(500)

            if last_err is not None:
                await page.screenshot(path=str(ART / "step6_err_no_match.png"), full_page=True)
                raise RuntimeError(f"Не удалось стабильно выбрать отделение после 3 попыток: {last_err}")


def _branch_number_from_query(q: str) -> str:
    m = re.search(r"\d+", q)
    return m.group(0) if m else q


if __name__ == "__main__":
    asyncio.run(main())