import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

USE_CDP = os.getenv("BIOTUS_USE_CDP", "0") == "1"
CDP_ENDPOINT = os.getenv("BIOTUS_CDP_ENDPOINT", "http://127.0.0.1:9222")

ATTACH_DIR = Path(os.getenv("BIOTUS_ATTACH_DIR", "/Users/dmitrijnazdrin/rpa_biotus/маркировки"))
ATTACH_EXTS = tuple(x.strip().lower() for x in os.getenv("BIOTUS_ATTACH_EXTS", ".pdf,.png,.jpg,.jpeg").split(","))

TIMEOUT_MS = int(os.getenv("BIOTUS_TIMEOUT_MS", "15000"))
TTN = os.getenv("BIOTUS_TTN", "").strip()


def pick_file(folder: Path) -> Path:
    if not folder.exists():
        raise RuntimeError(f"Папка не найдена: {folder}")

    if not TTN:
        raise RuntimeError(
            "BIOTUS_TTN не задан. Укажи BIOTUS_TTN в .env или переменных окружения, "
            "чтобы выбрать файл накладной по номеру ТТН."
        )

    # Ищем файл, в имени которого есть TTN (без учёта регистра) и расширение входит в ATTACH_EXTS
    ttn_lower = TTN.lower()
    files = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in ATTACH_EXTS
        and ttn_lower in p.name.lower()
    ]

    if not files:
        raise RuntimeError(
            f"Не найден файл в папке {folder} с TTN='{TTN}' и расширением {ATTACH_EXTS}. "
            "Ожидаю, например: marking-<TTN>.pdf"
        )

    # если вдруг несколько совпадений — берём самый свежий
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files[0]


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
    file_path = pick_file(ATTACH_DIR)
    print(f"[INFO] Will attach: {file_path}")

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