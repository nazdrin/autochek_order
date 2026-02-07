import asyncio
import os
from playwright.async_api import async_playwright

CDP = os.getenv("CDP_ENDPOINT", "http://127.0.0.1:9222")
OUT = os.getenv("BIOTUS_STATE_FILE", ".biotus_state.json")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        await ctx.storage_state(path=OUT)
        print("OK saved:", OUT)

asyncio.run(main())