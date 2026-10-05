"""Disable Playwright screenshot persistence for operational script processes.

Python imports this module automatically before executing a file from this
directory.  A screenshot can still be explicitly enabled for a one-off local
diagnostic session with RUNTIME_DISABLE_SCREENSHOTS=0.
"""
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from services.runtime_cleanup import cleanup_startup

    cleanup_startup(ROOT, include_exports=True)
except Exception:
    # Startup cleanup must never prevent an order-processing script from running.
    pass


if (os.getenv("RUNTIME_DISABLE_SCREENSHOTS") or "1").strip().lower() not in {"0", "false", "no", "off"}:
    try:
        from playwright.async_api import Page

        async def _discard_screenshot(self, *args, **kwargs):
            return b"" if not kwargs.get("path") else None

        Page.screenshot = _discard_screenshot
    except ImportError:
        pass
