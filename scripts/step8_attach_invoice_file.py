import asyncio
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")

ATTACH_DIR = Path(os.getenv("BIOTUS_ATTACH_DIR", "/Users/dmitrijnazdrin/rpa_biotus/маркировки"))

TIMEOUT_MS = int(os.getenv("BIOTUS_TIMEOUT_MS", "15000"))
TTN = os.getenv("BIOTUS_TTN", "").strip()
ORDER_ID = os.getenv("BIOTUS_ORDER_ID", "").strip()
# Nova Poshta API key (prefer BIOTUS_NP_API_KEY, but also allow NP_API_KEY)
NP_API_KEY = (os.getenv("BIOTUS_NP_API_KEY") or os.getenv("NP_API_KEY") or "").strip()

logger = logging.getLogger(__name__)


def normalize_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def validate_invoice_filename_or_raise(
    file_path: Path, ttn_number: str, order_id: str | None = None
) -> None:
    expected_ttn_digits = normalize_digits(ttn_number)
    filename_digits = normalize_digits(file_path.name)

    if not expected_ttn_digits:
        logger.error(
            "Invoice filename validation failed: order_id=%s ttn=%s file=%s",
            order_id or "n/a",
            ttn_number,
            file_path,
        )
        raise RuntimeError("TTN пустой, проверка имени файла невозможна.")

    if expected_ttn_digits not in filename_digits:
        logger.error(
            "Invoice filename validation failed: order_id=%s ttn=%s file=%s",
            order_id or "n/a",
            ttn_number,
            file_path,
        )
        raise RuntimeError("Номер ТТН не совпадает с именем файла.")

    logger.info(
        "Invoice filename validation passed: order_id=%s ttn=%s file=%s",
        order_id or "n/a",
        ttn_number,
        file_path,
    )


def download_np_label(folder: Path) -> Path:
    """Download Nova Poshta 100x100 marking label (pdf) for TTN into folder and return path."""
    if not folder.exists():
        # keep behavior consistent: folder must exist
        raise RuntimeError(f"Папка не найдена: {folder}")

    if not TTN:
        raise RuntimeError(
            "BIOTUS_TTN не задан. Укажи BIOTUS_TTN в .env или переменных окружения, "
            "чтобы скачать накладную по номеру ТТН."
        )

    if not NP_API_KEY:
        raise RuntimeError(
            "Не задан API ключ Новой Почты. Укажи BIOTUS_NP_API_KEY (или NP_API_KEY) "
            "в .env или переменных окружения."
        )

    out_path = folder / f"marking-{TTN}.pdf"

    url = (
        "https://my.novaposhta.ua/orders/printMarking100x100/"
        f"orders[]/{TTN}/type/pdf/apiKey/{NP_API_KEY}/zebra"
    )

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=max(10, TIMEOUT_MS / 1000)) as resp:
            status = getattr(resp, "status", 200)
            if status and int(status) >= 400:
                raise RuntimeError(f"Nova Poshta API вернул статус {status}")
            data = resp.read()
            if not data or len(data) < 1000:
                # heuristic: empty/too small pdf is likely an error page
                raise RuntimeError("Скачанный файл слишком маленький — похоже, NP API вернул ошибку")
            out_path.write_bytes(data)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ошибка NP API HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ошибка NP API соединения: {e}") from e

    return out_path


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


def filename_matches_ttn(name: str, ttn_number: str) -> bool:
    expected_ttn_digits = normalize_digits(ttn_number)
    if not expected_ttn_digits:
        return False
    return expected_ttn_digits in normalize_digits(name)


async def get_attached_file_names(page) -> list[str]:
    names: list[str] = []
    items = page.locator(".fileup-file, .fileup-description")
    count = await items.count()
    for i in range(count):
        try:
            text = (await items.nth(i).inner_text(timeout=1500)).strip()
        except Exception:
            continue
        if text:
            names.append(" ".join(text.split()))

    # dedupe while preserving order
    seen = set()
    unique_names: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        unique_names.append(name)
    return unique_names


async def remove_all_attached_files(page, max_rounds: int = 5) -> None:
    file_items = page.locator(".fileup-file")
    for _ in range(max_rounds):
        remove_btns = page.locator('span.fileup-remove[title="Удалить"], .fileup-remove')
        remove_count = await remove_btns.count()
        if remove_count == 0:
            if await file_items.count() == 0:
                return
            await page.wait_for_timeout(250)
            continue

        before_count = await file_items.count()

        # UI updates after each click, so click the first visible each iteration.
        for _ in range(remove_count):
            btn = remove_btns.first
            try:
                await btn.click(timeout=TIMEOUT_MS)
            except Exception:
                break
            await page.wait_for_timeout(150)

        for _ in range(8):
            current_count = await file_items.count()
            if current_count == 0 or current_count < before_count:
                break
            await page.wait_for_timeout(250)

    # one last short wait to settle DOM
    await page.wait_for_timeout(250)


async def check_and_close_limit_modal(page) -> bool:
    limit_candidates = [
        page.get_by_text("Количество выбранных файлов превышает лимит", exact=False).locator(":visible"),
        page.get_by_text("превышает лимит (1)", exact=False).locator(":visible"),
    ]
    seen_limit = False
    for loc in limit_candidates:
        if (await loc.count()) > 0:
            seen_limit = True
            break
    if not seen_limit:
        return False

    close_btn = page.get_by_role("button", name="Закрыть").locator(":visible").first
    if await close_btn.count() == 0:
        close_btn = page.get_by_text("Закрыть", exact=False).locator(":visible").first
    if await close_btn.count() > 0:
        try:
            await close_btn.click(timeout=TIMEOUT_MS)
        except Exception:
            pass
    await page.wait_for_timeout(300)
    return True


async def attach_file_once(page, btn, file_path: Path) -> bool:
    attached = False

    # 1) основной путь: file chooser
    try:
        async with page.expect_file_chooser(timeout=TIMEOUT_MS) as fc_info:
            await btn.first.click()
        fc = await fc_info.value
        await fc.set_files(str(file_path))
        attached = True
        print("[INFO] Attached via file chooser")
    except Exception as e:
        print(f"[WARN] file chooser not captured: {e}")

    # 2) fallback: прямой input[type=file]
    if not attached:
        inp = page.locator('input[type="file"]:visible').first
        if await inp.count() == 0:
            inp = page.locator('input[type="file"]').first
        if await inp.count() == 0:
            raise RuntimeError("Не нашёл input[type=file] для загрузки файла.")
        await inp.set_input_files(str(file_path))
        attached = True
        print("[INFO] Attached via input[type=file]")

    # даём UI обработать аплоад
    await page.wait_for_timeout(800)
    return await check_and_close_limit_modal(page)


async def main():
    file_path = download_np_label(ATTACH_DIR)
    print(f"[INFO] Downloaded and will attach: {file_path}")
    validate_invoice_filename_or_raise(file_path, TTN, ORDER_ID or None)

    async with async_playwright() as p:
        if USE_CDP:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await pick_checkout_page(context)
        else:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

        # убедимся что страница жива
        await page.wait_for_timeout(200)

        # Кнопка "Накладний файл" (делаем :visible)
        btn = page.get_by_role("button", name="Накладний файл").locator(":visible")
        if await btn.count() == 0:
            btn = page.get_by_text("Накладний файл", exact=False).locator(":visible").first

        if await btn.count() == 0:
            raise RuntimeError('Не нашёл кнопку "Накладний файл" на странице.')

        limit_modal_seen = False
        current_attached = await get_attached_file_names(page)

        # Если уже прикреплён файл с текущим TTN, считаем шаг выполненным.
        if any(filename_matches_ttn(name, TTN) for name in current_attached):
            print(f"[INFO] Attachment already matches TTN: {current_attached}")
        else:
            if current_attached:
                print(f"[INFO] Removing stale attachment(s): {current_attached}")
                await remove_all_attached_files(page)

            # Одна повторная попытка после модалки лимита.
            for attempt in range(2):
                saw_limit = await attach_file_once(page, btn, file_path)
                limit_modal_seen = limit_modal_seen or saw_limit
                if saw_limit:
                    print("[WARN] Limit modal detected; removing attachments and retrying")
                    await remove_all_attached_files(page)
                    if attempt == 0:
                        continue
                break

        # Жёсткая верификация: на странице должен быть файл с текущим TTN.
        final_attached = await get_attached_file_names(page)
        if not final_attached:
            screenshot = "step8_err_attach_limit.png" if limit_modal_seen else "step8_err_no_attachment.png"
            await page.screenshot(path=screenshot, full_page=True)
            if limit_modal_seen:
                raise RuntimeError(
                    "Модалка лимита была показана, но после ретрая файл не прикрепился."
                )
            raise RuntimeError("После операций файл не прикреплён.")

        if not any(filename_matches_ttn(name, TTN) for name in final_attached):
            screenshot = (
                "step8_err_attach_limit.png"
                if limit_modal_seen
                else "step8_err_attached_mismatch.png"
            )
            await page.screenshot(path=screenshot, full_page=True)
            if limit_modal_seen:
                raise RuntimeError(
                    f"После модалки лимита и ретрая корректный файл не прикрепился: {final_attached}"
                )
            raise RuntimeError(
                f"Прикреплённый файл не соответствует TTN {TTN}: {final_attached}"
            )

        # удаляем локальный файл только после успешной верификации
        try:
            file_path.unlink()
            print(f"[OK] Deleted local file: {file_path.name}")
        except Exception as e:
            print(f"[WARN] Failed to delete file: {e}")

        # В CDP НЕ закрываем Chrome/контекст, чтобы не убить твою вкладку
        if not USE_CDP:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
