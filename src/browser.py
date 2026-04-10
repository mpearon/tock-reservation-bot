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

COOKIES_FILE = Path("session_cookies.json")
BASE_URL = "https://www.exploretock.com"

# How long to wait for login redirect (seconds).
# Set long enough for the user to manually solve any CAPTCHA in the browser window.
LOGIN_REDIRECT_TIMEOUT_MS = 120_000  # 2 minutes

# ---------------------------------------------------------------------------
# playwright-stealth — supports both v1 (stealth_async) and v2 (Stealth class)
# ---------------------------------------------------------------------------
_stealth_apply = None  # will be an async callable (page) -> None, or None

try:
    # v2.x API
    from playwright_stealth import Stealth as _Stealth
    _stealth_instance = _Stealth(init_scripts_only=True)

    async def _stealth_apply(page: Page) -> None:
        await _stealth_instance.apply_stealth_async(page)

    logger.debug("playwright-stealth v2 loaded (Stealth class)")
except ImportError:
    pass

if _stealth_apply is None:
    try:
        # v1.x API (fallback)
        from playwright_stealth import stealth_async as _stealth_async_v1

        async def _stealth_apply(page: Page) -> None:
            await _stealth_async_v1(page)

        logger.debug("playwright-stealth v1 loaded (stealth_async)")
    except ImportError:
        pass

if _stealth_apply is None:
    logger.warning(
        "playwright-stealth not available — bot-detection resistance is reduced.\n"
        "  The bot will still work but may trigger Cloudflare challenges more often."
    )

    async def _stealth_apply(page: Page) -> None:  # type: ignore[misc]
        # Minimal manual patch
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
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

        if COOKIES_FILE.exists():
            try:
                cookies = json.loads(COOKIES_FILE.read_text())
                await self._context.add_cookies(cookies)
                logger.info(f"Restored {len(cookies)} session cookies from {COOKIES_FILE}")
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
        await _stealth_apply(page)
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
        4. Wait up to 2 minutes for redirect away from /login.
           (This gives time to manually solve any Cloudflare CAPTCHA in the
            browser window when running with HEADLESS=false.)
        5. Persist all cookies.

        Returns True on success, False on failure.
        """
        page = await self.new_page()
        slug = self.config.restaurant_slug

        try:
            # ---- Step 1: Check existing session ----
            logger.info("Checking existing session…")
            # Use today's date so we get a valid calendar page
            from datetime import date
            today = date.today().isoformat()
            check_url = (
                f"{BASE_URL}/{slug}/search"
                f"?date={today}&size={self.config.party_size}&time={self.config.preferred_time}"
            )
            await page.goto(check_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)

            if await self._is_logged_in(page):
                logger.info("Session cookie valid — already logged in.")
                return True

            # ---- Step 2: Navigate to login ----
            logger.info("No valid session. Navigating to login page…")
            await page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)  # let any Cloudflare JS settle

            # ---- Step 3: Fill credentials ----
            if not await self._safe_fill(page, "login_email", self.config.tock_email):
                return False
            await page.wait_for_timeout(500)

            if not await self._safe_fill(page, "login_password", self.config.tock_password):
                return False
            await page.wait_for_timeout(500)

            logger.info("Credentials filled. Clicking sign-in…")
            if not await self._safe_click(page, "login_submit"):
                return False

            # ---- Step 4: Wait for redirect ----
            logger.info(
                f"Waiting up to {LOGIN_REDIRECT_TIMEOUT_MS // 1000}s for login redirect…"
                + (
                    "\n  *** HEADED MODE: If a CAPTCHA appears in the browser window, "
                    "solve it manually — the bot will continue automatically. ***"
                    if not self.config.headless else ""
                )
            )
            try:
                await page.wait_for_function(
                    "!window.location.pathname.startsWith('/login')",
                    timeout=LOGIN_REDIRECT_TIMEOUT_MS,
                )
            except Exception:
                current_url = page.url
                logger.error(
                    f"Login did not redirect away from /login within "
                    f"{LOGIN_REDIRECT_TIMEOUT_MS // 1000}s.\n"
                    f"  Current URL: {current_url}\n"
                    f"  Possible causes:\n"
                    f"    • Wrong email or password in .env\n"
                    f"    • Cloudflare CAPTCHA not solved (use HEADLESS=false)\n"
                    f"    • Tock login page structure changed (check login_* selectors in "
                    f"      src/selectors.py)"
                )
                return False

            await self._save_cookies()
            logger.info(f"Login successful → {page.url}. Cookies saved.")
            return True

        except Exception as e:
            logger.error(f"Login error: {e}")
            return False
        finally:
            await page.close()

    async def warm_session(self) -> bool:
        """
        Navigate to the restaurant's main Tock page to refresh Cloudflare
        cookies before the sniper window opens. Also re-logins if the session
        has expired. Saves updated cookies to disk.

        Called once when sniper mode activates so every rapid poll starts
        with a fresh cf_clearance token.

        Returns True on success, False if the session could not be warmed.
        """
        url = f"{BASE_URL}/{self.config.restaurant_slug}"
        page = None
        try:
            page = await self.new_page()
            logger.info(f"[warm] Refreshing session: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # Wait for network to settle (Cloudflare JS runs after domcontentloaded)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # networkidle timeout is fine; proceed with current state

            if not await self._is_logged_in(page):
                logger.warning(
                    "[warm] Session appears expired — re-authenticating before sniper fires."
                )
                await page.close()
                success = await self.login()
                if not success:
                    logger.error("[warm] Re-login failed during pre-warm!")
                return success

            await self._save_cookies()
            logger.info("[warm] Session healthy — cookies refreshed and saved.")
            return True
        except Exception as e:
            logger.warning(f"[warm] Session warm failed (non-critical): {e}")
            return False
        finally:
            try:
                if page is not None:
                    await page.close()
            except Exception:
                pass

    async def _is_logged_in(self, page: Page) -> bool:
        """Return True if an authenticated-only element is present on the page."""
        try:
            el = await page.query_selector(sel.get("logged_in_indicator"))
            return el is not None
        except Exception:
            return False

    async def _save_cookies(self) -> None:
        cookies = await self._context.cookies()
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2))
        logger.debug(f"Saved {len(cookies)} cookies → {COOKIES_FILE}")

    async def get_cookies(self) -> list[dict]:
        """Return all cookies in the current browser context."""
        return await self._context.cookies()

    @staticmethod
    async def find_in_frames(page: Page, selector: str):
        """
        Search the main frame and all child iframes for *selector*.
        Returns the first matching element, or None.

        Tock embeds some inputs (e.g. CVC) inside Stripe iframes, so a plain
        page.query_selector() misses them.
        """
        for frame in [page.main_frame] + [f for f in page.frames if f != page.main_frame]:
            try:
                el = await frame.query_selector(selector)
                if el:
                    return el
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Resilient selector helpers
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
