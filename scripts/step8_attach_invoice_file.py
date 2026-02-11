import asyncio
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


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _extract_pdf_text_simple(file_path: Path) -> str:
    try:
        from PyPDF2 import PdfReader
    except Exception as e:  # pragma: no cover - used at runtime only
        raise RuntimeError("PyPDF2 не установлен. Установи: pip install PyPDF2") from e

    reader = PdfReader(str(file_path))
    chunks = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            chunks.append("")
    return "\n".join(chunks)


def validate_invoice_pdf_or_raise(file_path: Path, ttn_number: str, order_id: str | None = None) -> None:
    ttn_digits = _digits_only(ttn_number)
    file_digits = _digits_only(file_path.name)

    text = _extract_pdf_text_simple(file_path)
    text_digits = _digits_only(text)
    sample = " ".join(text.split())
    if len(sample) > 120:
        sample = sample[:120] + "..."

    if not ttn_digits:
        msg = (
            f"[ERROR] Invoice PDF validation failed: order_id={order_id or 'n/a'} "
            f"ttn={ttn_number} file={file_path} sample=\"{sample}\""
        )
        print(msg)
        raise RuntimeError("TTN пустой, проверка PDF невозможна.")

    if file_digits != ttn_digits:
        msg = (
            f"[ERROR] Invoice PDF validation failed: order_id={order_id or 'n/a'} "
            f"ttn={ttn_number} file={file_path} sample=\"{sample}\""
        )
        print(msg)
        raise RuntimeError("Номер ТТН не совпадает с именем файла.")

    if ttn_digits not in text_digits:
        msg = (
            f"[ERROR] Invoice PDF validation failed: order_id={order_id or 'n/a'} "
            f"ttn={ttn_number} file={file_path} sample=\"{sample}\""
        )
        print(msg)
        raise RuntimeError("Номер ТТН не найден внутри PDF.")


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


async def main():
    file_path = download_np_label(ATTACH_DIR)
    print(f"[INFO] Downloaded and will attach: {file_path}")
    validate_invoice_pdf_or_raise(file_path, TTN, ORDER_ID or None)

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

        # удаляем локальный файл
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
