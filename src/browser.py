"""
Playwright browser management.

Responsibilities:
- Launch Chromium with stealth patches to avoid bot detection
- Log in to Tock and persist session cookies
- Provide authenticated pages to other modules
- Expose resilient _safe_fill / _safe_click helpers that log
  SELECTOR_FAILED with the exact key so updates to selectors.py are easy
"""

import json
import logging
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

import src.selectors as sel
from src.config import Config

logger = logging.getLogger(__name__)

# Where session cookies are persisted between runs
COOKIES_FILE = Path("session_cookies.json")

BASE_URL = "https://www.exploretock.com"

# ---------------------------------------------------------------------------
# Optional stealth import
# ---------------------------------------------------------------------------
try:
    from playwright_stealth import stealth_async
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False
    logger.warning(
        "playwright-stealth not installed — bot detection is more likely.\n"
        "  Fix: pip install playwright-stealth"
    )


class TockBrowser:
    def __init__(self, config: Config):
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the browser and restore saved session cookies if available."""
        self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-dev-shm-usage",
            ],
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )

        # Restore previously saved cookies (includes Cloudflare clearance)
        if COOKIES_FILE.exists():
            try:
                cookies = json.loads(COOKIES_FILE.read_text())
                await self._context.add_cookies(cookies)
                logger.info(
                    f"Restored {len(cookies)} session cookies from {COOKIES_FILE}"
                )
            except Exception as e:
                logger.warning(f"Could not restore session cookies: {e}")

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Page factory
    # ------------------------------------------------------------------

    async def new_page(self) -> Page:
        """Return a new page with stealth patches applied."""
        page = await self._context.new_page()

        if _STEALTH_AVAILABLE:
            await stealth_async(page)
        else:
            # Minimal manual patch when playwright-stealth is unavailable
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

        return page

    # ------------------------------------------------------------------
    # Login / session
    # ------------------------------------------------------------------

    async def login(self) -> bool:
        """
        Ensure the bot is authenticated with Tock.

        Flow:
        1. Navigate to the restaurant search page.
        2. If a logged-in indicator is found → session still valid, done.
        3. Otherwise navigate to /login, fill credentials, submit.
        4. Wait for redirect away from /login.
        5. Persist all cookies (including Cloudflare clearance).

        Returns True on success, False on failure.
        """
        page = await self.new_page()
        slug = self.config.restaurant_slug

        try:
            # ---- Step 1: Check existing session ----
            logger.info("Checking existing session…")
            search_url = f"{BASE_URL}/{slug}/search?date=2099-01-01&size=2&time=17:00"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            if await self._is_logged_in(page):
                logger.info("Session cookie valid — already logged in.")
                return True

            # ---- Step 2: Log in ----
            logger.info("No valid session found. Navigating to login page…")
            await page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)

            if not await self._safe_fill(page, "login_email", self.config.tock_email):
                return False
            await page.wait_for_timeout(400)

            if not await self._safe_fill(page, "login_password", self.config.tock_password):
                return False
            await page.wait_for_timeout(400)

            if not await self._safe_click(page, "login_submit"):
                return False

            # ---- Step 3: Wait for redirect away from /login ----
            try:
                await page.wait_for_function(
                    "!window.location.pathname.startsWith('/login')",
                    timeout=20000,
                )
            except Exception:
                logger.error(
                    "Login did not redirect away from /login within 20s.\n"
                    "  Possible causes:\n"
                    "    • Wrong credentials in .env\n"
                    "    • Cloudflare CAPTCHA appeared (run with HEADLESS=false to solve manually)\n"
                    "    • Tock login page structure changed (check login_* selectors)"
                )
                return False

            await self._save_cookies()
            logger.info("Login successful. Session cookies saved.")
            return True

        except Exception as e:
            logger.error(f"Login error: {e}")
            return False
        finally:
            await page.close()

    async def _is_logged_in(self, page: Page) -> bool:
        """Return True if an authenticated-only element is present."""
        try:
            el = await page.query_selector(sel.get("logged_in_indicator"))
            return el is not None
        except Exception:
            return False

    async def _save_cookies(self) -> None:
        cookies = await self._context.cookies()
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2))
        logger.debug(f"Saved {len(cookies)} cookies → {COOKIES_FILE}")

    # ------------------------------------------------------------------
    # Resilient selector helpers (used by login; booker/checker use their own)
    # ------------------------------------------------------------------

    async def _safe_fill(
        self, page: Page, key: str, value: str, timeout: int = 10_000
    ) -> bool:
        selector = sel.get(key)
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            await page.fill(selector, value)
            return True
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
                f"  → Update src/selectors.py to fix this.  Error: {e}"
            )
            return False

    async def _safe_click(
        self, page: Page, key: str, timeout: int = 10_000
    ) -> bool:
        selector = sel.get(key)
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            await page.click(selector)
            return True
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
                f"  → Update src/selectors.py to fix this.  Error: {e}"
            )
            return False
