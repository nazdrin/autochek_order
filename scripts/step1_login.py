import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

BASE_URL = os.getenv("BIOTUS_BASE_URL", "https://opt.biotus.ua")
LOGIN = os.getenv("BIOTUS_LOGIN")
PASSWORD = os.getenv("BIOTUS_PASSWORD")
AFTER_URL = os.getenv("BIOTUS_AFTER_LOGIN_URL")

if not LOGIN or not PASSWORD or not AFTER_URL:
    raise SystemExit("Нет BIOTUS_LOGIN/BIOTUS_PASSWORD/BIOTUS_AFTER_LOGIN_URL в .env")


async def wait_human_check_if_any(page):
    """
    Cloudflare иногда показывает страницу 'Проверяем, человек ли вы...'.
    Мы НЕ пытаемся это обходить. Просто ждём, пока пользователь пройдёт проверку.
    """
    # Дадим время на появление/прохождение проверки
    # (в окне браузера ты увидишь эту страницу)
    for _ in range(120):  # ~120 секунд с шагом 1с
        url = page.url.lower()
        title = (await page.title()).lower()

        # Признаки Cloudflare/turnstile (могут отличаться, но этого достаточно)
        if "challenges.cloudflare.com" in url or "checking" in title or "cloudflare" in title:
            await page.wait_for_timeout(1000)
            continue

        # Если дошли до нормальной страницы сайта — выходим
        if "opt.biotus.ua" in url:
            return

        await page.wait_for_timeout(1000)


async def do_login_if_needed(page):
    # Если уже авторизован и видим кабинет — логин не нужен
    # (часто после сохранённой сессии так и будет)
    url = page.url.lower()
    if "/sales/" in url or "order/history" in url:
        return

    # Пытаемся найти поля и выполнить вход
    # (если в этот момент всё ещё Cloudflare — просто ничего не найдём, поэтому
    # сначала нужно пройти проверку, см. wait_human_check_if_any)
    email_selectors = [
        'input[type="email"]',
        'input[name*="mail" i]',
        'input[name*="login" i]',
        'input[placeholder*="mail" i]',
        'input[placeholder*="email" i]',
    ]
    pass_selectors = [
        'input[type="password"]',
        'input[name*="pass" i]',
    ]

    email = None
    for sel in email_selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            email = loc.first
            break

    pwd = None
    for sel in pass_selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            pwd = loc.first
            break

    if email and pwd:
        await email.fill(LOGIN)
        await pwd.fill(PASSWORD)

        # кнопка входа
        for sel in [
            'button[type="submit"]',
            'button:has-text("Увійти")',
            'button:has-text("Войти")',
            'button:has-text("Login")',
            'input[type="submit"]',
        ]:
            btn = page.locator(sel)
            if await btn.count() > 0:
                await btn.first.click()
                break


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)

        # Важно: отдельный контекст, потом его state сохраним
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(BASE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(800)

        # 1) Если вылез Cloudflare — пройди вручную
        print("Если появится проверка Cloudflare/капча — пройди её вручную в открытом окне браузера.")
        await wait_human_check_if_any(page)

        # 2) После прохождения — пробуем логин
        await do_login_if_needed(page)
        await page.wait_for_timeout(1200)

        # 3) Переходим на целевую страницу (если ещё не там)
        await page.goto(AFTER_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)

        await page.screenshot(path=str(ART / "step1_after_login.png"), full_page=True)

        state_path = ART / "storage_state.json"
        await context.storage_state(path=str(state_path))
        print(f"OK: state saved to {state_path}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())