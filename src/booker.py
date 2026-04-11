"""
Booking logic.

Tock checkout flow (after clicking a time slot):
─────────────────────────────────────────────────
  Step 1 — Navigate to search page, click calendar day
  Step 2 — Click the target time slot button
  Step 3 — Checkout page loads (guest details + payment)
            • For FREE reservations: confirm button appears directly
            • For PAID/DEPOSIT reservations: payment section appears first
              - If saved card on file → proceed to confirm
              - If NO card on file   → bot PAUSES and notifies you to add one,
                                       then polls until card appears (up to 10 min)
  Step 4 — Click "Complete reservation" (or equivalent confirm button)
  Step 5 — Confirmation page detected → booking complete

CONCURRENT RACE LOGIC
──────────────────────
When multiple preferred-day slots are available, book_best_slot_race() launches
one asyncio task per calendar date simultaneously.

A shared asyncio.Lock ensures only ONE task can execute the confirm click.
After one task succeeds, it sets a shared asyncio.Event; all other tasks
check this event before the lock and abort immediately.

Because asyncio is single-threaded cooperative multitasking, the event check
inside the lock is effectively atomic — no double-bookings can occur.
"""

import asyncio
import logging
import os
import re
from datetime import datetime as _datetime

from playwright.async_api import Page

import src.selectors as sel
from src.browser import TockBrowser
from src.checker import AvailableSlot
from src.config import Config
from src.notifier import Notifier
from src.selectors import get_slot_button_selectors

logger = logging.getLogger(__name__)

BASE_URL = "https://www.exploretock.com"

_SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "debug_screenshots"
)
os.makedirs(_SCREENSHOT_DIR, exist_ok=True)

# Selectors that match generic "Book" buttons (restaurant/experience level, not time-slot).
# These must only be clicked if surrounding context confirms the target time.
_GENERIC_BOOK_SELECTORS: frozenset[str] = frozenset({
    'button:visible:has-text("Book")',
    'button:text("Book now")',
    'a:text("Book now")',
    '[data-testid="book-now"]',
    "button.SearchExperience-bookButton",
    "[data-testid='book-button']",
})

# How long to wait for the user to add a payment card (Tock holds slots ~10 min)
PAYMENT_WAIT_TIMEOUT_SEC = 540   # 9 minutes
PAYMENT_POLL_INTERVAL_SEC = 15


class TockBooker:
    def __init__(self, config: Config, browser: TockBrowser, notifier: Notifier):
        self.config = config
        self.browser = browser
        self.notifier = notifier
        # Lock ensures only one concurrent task can execute the confirm step
        self._confirm_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public: race multiple slots
    # ------------------------------------------------------------------

    async def book_best_slot_race(
        self, slots: list[AvailableSlot],
        warm_pages: dict[str, Page] | None = None,
    ) -> AvailableSlot | None:
        """
        Pick the best slot per calendar date, then attempt all of them
        concurrently. Returns the first successfully booked slot, or None.

        "Best" = closest to config.preferred_time (checker already sorts them,
        so the first entry per date is the best).
        """
        if not slots:
            return None

        candidates = self._best_per_date(slots)
        logger.info(
            f"Starting concurrent booking race for {len(candidates)} slot(s): "
            + " | ".join(str(s) for s in candidates)
        )

        booking_won = asyncio.Event()
        winner: list[AvailableSlot] = []

        async def attempt(slot: AvailableSlot) -> None:
            self.notifier.booking_attempting(slot)
            page = warm_pages.get(slot.slot_date_str) if warm_pages else None
            try:
                success = await self._book_single(slot, booking_won, warm_page=page)
                if success:
                    winner.append(slot)
            except Exception as e:
                logger.error(f"[book] Unhandled exception for {slot}: {e}")

        tasks = [asyncio.create_task(attempt(s)) for s in candidates]
        await asyncio.gather(*tasks, return_exceptions=True)

        return winner[0] if winner else None

    # ------------------------------------------------------------------
    # Internal: book one slot
    # ------------------------------------------------------------------

    async def _book_single(
        self, slot: AvailableSlot, booking_won: asyncio.Event,
        warm_page: Page | None = None,
    ) -> bool:
        """
        Full booking flow for one slot on its own Playwright page.
        Returns True if the booking was confirmed.

        If warm_page is provided (sniper mode), skips Steps 1-2 (navigation +
        day click) and jumps straight to clicking the time slot — saving ~3-5s.
        """
        if self.config.dry_run:
            self.notifier.dry_run_would_book(slot)
            return False

        # Use warm page from checker (sniper mode) or create fresh
        page = warm_page if warm_page and not warm_page.is_closed() else None
        owns_page = page is None  # only close pages we created
        if page is None:
            page = await self.browser.new_page()

        try:
            if owns_page:
                # ── Step 1: load search page ──────────────────────────────
                url = (
                    f"{BASE_URL}/{self.config.restaurant_slug}/search"
                    f"?date={slot.slot_date_str}"
                    f"&size={self.config.party_size}"
                    f"&time={self.config.preferred_time}"
                )
                logger.info(f"[book] {slot} → {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                if not await self._wait_for_selector(
                    page, "calendar_container", context=str(slot), timeout=15000
                ):
                    return False

                # Wait for day buttons to render inside the calendar.
                try:
                    await page.wait_for_selector(
                        sel.get("all_day_button"), timeout=5000
                    )
                except Exception:
                    pass  # calendar may still be loading; proceed anyway

                # ── Step 2: click the calendar day ────────────────────────
                if booking_won.is_set():
                    self.notifier.booking_aborted(slot, "another slot already booked")
                    return False

                if not await self._click_calendar_day(page, slot):
                    return False

                # Wait reactively for slot buttons after day click
                for try_sel in get_slot_button_selectors()[:2]:
                    try:
                        await page.wait_for_selector(try_sel, timeout=2000)
                        break
                    except Exception:
                        continue
            else:
                logger.info(f"[book] {slot} → using warm page (skipping navigation)")

            await self._booking_screenshot(page, "01_booking_start")

            # ── Step 3: click the time slot ───────────────────────────
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            if not await self._click_time_slot(page, slot):
                return False

            # Scroll to bottom so the confirm button (which may be below the fold
            # on a 800px viewport) becomes accessible before checkout detection.
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass  # non-critical — proceed regardless

            await self._booking_screenshot(page, "02_after_slot_click")

            # ── Step 4: wait for checkout page ────────────────────────
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            checkout_ok = await self._wait_for_checkout(page, slot)
            await self._booking_screenshot(
                page,
                "03_checkout_loaded" if checkout_ok else "03_checkout_timeout"
            )
            if not checkout_ok:
                return False

            # ── Step 5: confirm (locked — only one task proceeds) ─────
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            async with self._confirm_lock:
                # Re-check inside the lock (another task may have won while
                # we were waiting to acquire it)
                if booking_won.is_set():
                    self.notifier.booking_aborted(
                        slot, "another slot confirmed while waiting for lock"
                    )
                    return False

                success = await self._confirm_booking(page, slot)
                if success:
                    booking_won.set()
                    self.notifier.booking_confirmed(slot)
                return success

        except Exception as e:
            logger.error(f"[book] Error booking {slot}: {e}")
            return False
        finally:
            if owns_page:
                await page.close()
            # Don't close warm pages — checker manages their lifecycle

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    async def _click_calendar_day(self, page: Page, slot: AvailableSlot) -> bool:
        """Click the calendar button matching slot.slot_date using single evaluate().

        Uses all_day_button (any in-month day) — NOT available_day_button —
        so we click days even when they lack the is-available class (e.g.
        Fuhuihua shows is-sold/is-disabled until the exact release moment).
        """
        selector = sel.get("all_day_button")
        target_num = str(slot.slot_date.day)

        result = await page.evaluate("""
        ([selector, targetNum]) => {
            const buttons = document.querySelectorAll(selector);
            for (const btn of buttons) {
                if (btn.textContent.trim() === targetNum) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }
        """, [selector, target_num])

        if result:
            logger.info(f"[book] Clicked day {target_num} for {slot.slot_date_str}")
            return True

        logger.error(
            f"SELECTOR_FAILED: key='all_day_button'\n"
            f"  Could not find or click day {target_num} for {slot.slot_date_str}.\n"
            f"  -> Update src/selectors.py"
        )
        return False

    async def _click_time_slot(self, page: Page, slot: AvailableSlot) -> bool:
        """Find the time slot matching slot.slot_time and click it.

        Iterates all matching buttons and compares text content to find the
        correct time. Falls back to first button if no text match is found.
        """
        slot_selectors = get_slot_button_selectors()

        # Wait reactively for slot buttons (not fixed sleep)
        for try_sel in slot_selectors[:2]:
            try:
                await page.wait_for_selector(try_sel, timeout=2000)
                break
            except Exception:
                continue

        # Find which selector has buttons
        matched_selector = None
        for try_sel in slot_selectors:
            try:
                count = await page.locator(try_sel).count()
                if count > 0:
                    matched_selector = try_sel
                    logger.debug(f"[book] Found {count} slot button(s) via {try_sel!r}")
                    break
            except Exception:
                continue

        if not matched_selector:
            logger.error(
                "[book] No slot buttons found after clicking the day.\n"
                "  Tried all known selectors.\n"
                "  -> Update src/selectors.py"
            )
            return False

        # Iterate buttons to find one matching slot.slot_time
        locator = page.locator(matched_selector)
        count = await locator.count()
        target_time = slot.slot_time.strip().upper()
        is_generic = matched_selector in _GENERIC_BOOK_SELECTORS

        best_btn = None
        for i in range(count):
            btn = locator.nth(i)
            try:
                text = (await btn.text_content() or "").strip()

                # Exact time match in button text → click immediately
                if target_time in text.upper():
                    await btn.click()
                    logger.info(
                        f"[book] Clicked slot button matching '{slot.slot_time}': {text}"
                    )
                    return True

                # Regex time match in button text
                time_match = re.search(
                    r'\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b', text, re.IGNORECASE
                )
                if time_match and time_match.group(1).strip().upper() == target_time:
                    await btn.click()
                    logger.info(f"[book] Clicked slot button (regex match): {text}")
                    return True

                # Generic "Book" button: only click if parent container has target time.
                # This prevents clicking the restaurant-level "Book now" button by mistake.
                if is_generic:
                    try:
                        parent_text = (
                            await btn.locator("..").text_content() or ""
                        ).strip()
                    except Exception:
                        parent_text = ""
                    if target_time in parent_text.upper() or re.search(
                        r'\b' + re.escape(slot.slot_time) + r'\b',
                        parent_text, re.IGNORECASE
                    ):
                        await btn.click()
                        logger.info(
                            f"[book] Clicked generic 'Book' button — "
                            f"time confirmed in parent: {parent_text[:80]!r}"
                        )
                        return True
                    logger.debug(
                        f"[book] Generic button at index {i} skipped — "
                        f"no time match in parent: {parent_text[:80]!r}"
                    )
                    continue  # do NOT set best_btn for unmatched generic buttons

                if best_btn is None:
                    best_btn = btn
            except Exception:
                continue

        # Fallback: click first non-generic button (only reached for specific selectors)
        if best_btn is not None:
            try:
                text = (await best_btn.text_content() or "").strip()
                await best_btn.click()
                logger.warning(
                    f"[book] No exact time match for '{slot.slot_time}' — "
                    f"clicked first specific button: {text}"
                )
                return True
            except Exception as e:
                logger.error(f"[book] Could not click fallback slot button: {e}")
                return False

        logger.error(
            f"[book] No clickable slot button found for '{slot.slot_time}' "
            f"(selector: {matched_selector!r})"
        )
        return False

    async def _wait_for_checkout(self, page: Page, slot: AvailableSlot) -> bool:
        """Return True when the checkout/booking-details page is detected.

        Polls every 2s for up to 30s, checking three signals in order:
          1. checkout_container selector present
          2. URL contains /checkout, /reservation, or /book
          3. Any payment-related element present (saved card or add-card prompt)
        """
        key = "checkout_container"
        selector = sel.get(key)
        no_pay_sel = sel.get("no_payment_indicator")
        saved_card_sel = sel.get("saved_payment_card")
        total_wait = 30
        interval = 2

        for elapsed in range(0, total_wait, interval):
            # 1. Checkout container selector
            try:
                await page.wait_for_selector(selector, timeout=interval * 1000)
                logger.info(
                    f"[book] Checkout page loaded for {slot.slot_date_str} "
                    f"(+{elapsed}s)"
                )
                return True
            except Exception:
                pass

            # 2. URL-based detection
            url = page.url
            if any(p in url for p in ("/checkout", "/reservation", "/book")):
                logger.info(f"[book] Checkout detected via URL: {url}")
                return True

            # 3. Payment element detection
            try:
                pay_el = await page.query_selector(no_pay_sel)
                if pay_el is None:
                    pay_el = await page.query_selector(saved_card_sel)
                if pay_el:
                    logger.info(
                        f"[book] Checkout detected via payment element "
                        f"at +{elapsed + interval}s"
                    )
                    return True
            except Exception:
                pass

            logger.debug(
                f"[book] Waiting for checkout… {elapsed + interval}s / {total_wait}s"
            )

        url = page.url
        await self._booking_screenshot(page, "checkout_timeout_final")
        logger.error(
            f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
            f"  Checkout page not detected after {total_wait}s.\n"
            f"  Current URL: {url}\n"
            f"  → Update src/selectors.py"
        )
        return False

    async def _booking_screenshot(self, page: Page, step: str) -> None:
        """Save a screenshot at *step* during booking (only when debug_screenshots=True)."""
        if not self.config.debug_screenshots:
            return
        try:
            ts = _datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
            path = os.path.join(_SCREENSHOT_DIR, f"booking_{ts}_{step}.png")
            await page.screenshot(path=path, full_page=True)
            logger.info(f"[book] Screenshot saved: {path}")
        except Exception as e:
            logger.debug(f"[book] Screenshot failed at step '{step}': {e}")

    async def _confirm_booking(self, page: Page, slot: AvailableSlot) -> bool:
        """
        Handle payment validation and click the confirm button.

        Payment flow:
        ┌─ Is there a payment section on the page?
        │   NO  → free reservation; go straight to confirm
        │   YES ─┬─ Saved card found → proceed to confirm
        │         └─ No card found   → pause, notify user, wait up to 9 min
        └─ Click confirm → wait for confirmation page → return True/False
        """
        needs_payment = await self._page_needs_payment(page)
        has_card = await self._has_saved_card(page)

        if needs_payment and not has_card:
            self.notifier.no_payment_method(slot)
            logger.warning(
                f"[book] Waiting up to {PAYMENT_WAIT_TIMEOUT_SEC}s for a payment "
                f"card to be added to the Tock account…"
            )
            waited = 0
            while waited < PAYMENT_WAIT_TIMEOUT_SEC:
                await asyncio.sleep(PAYMENT_POLL_INTERVAL_SEC)
                waited += PAYMENT_POLL_INTERVAL_SEC
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                if await self._has_saved_card(page):
                    logger.info("[book] Payment card detected. Proceeding to confirm.")
                    break
                logger.info(
                    f"[book] Still waiting for payment card… "
                    f"({waited}/{PAYMENT_WAIT_TIMEOUT_SEC}s)"
                )
            else:
                logger.error(
                    "[book] Timed out waiting for payment card. Aborting this slot."
                )
                return False

        # Fill CVC for saved card if configured
        if self.config.card_cvc:
            await self._fill_cvc(page)
        elif needs_payment and has_card:
            logger.warning(
                "[book] Saved card detected but TOCK_CARD_CVC is not set in .env — "
                "checkout may fail if CVC is required."
            )

        # Wait once for the confirm button, then click with one retry on transient failure.
        # Waiting once (not per-retry) avoids a potential 30s timeout in the hot path.
        confirm_key = "confirm_button"
        confirm_selector = sel.get(confirm_key)
        try:
            await page.wait_for_selector(confirm_selector, timeout=15000)
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{confirm_key}'  selector={confirm_selector!r}\n"
                f"  Confirm button not found on page.\n"
                f"  Current URL: {page.url}\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
            return False

        for click_attempt in range(2):
            try:
                await page.click(confirm_selector)
                logger.info("[book] Clicked confirm button.")
                break
            except Exception as e:
                if click_attempt == 0:
                    logger.warning(
                        f"[book] Confirm click failed, retrying in 2s: {e}"
                    )
                    await asyncio.sleep(2)
                else:
                    logger.error(
                        f"SELECTOR_FAILED: key='{confirm_key}'  selector={confirm_selector!r}\n"
                        f"  Could not click the confirm button after 2 attempts.\n"
                        f"  Current URL: {page.url}\n"
                        f"  → Update src/selectors.py  Error: {e}"
                    )
                    return False

        # Verify confirmation.
        # Use 30s timeout (vs 20s) to handle slow Tock servers under heavy traffic.
        # If element not found, fall back to URL check twice (immediate + after 5s delay).
        confirmed_key = "booking_confirmed"
        confirmed_selector = sel.get(confirmed_key)
        try:
            await page.wait_for_selector(confirmed_selector, timeout=30000)
            logger.info(f"[book] Confirmation element found — BOOKED: {slot}")
            return True
        except Exception:
            url = page.url
            if any(p in url for p in ("confirmation", "confirmed", "success")):
                logger.info(f"[book] Booking confirmed via URL: {url}")
                return True
            # Server may be very slow to redirect under high traffic — wait 5s more
            logger.warning(
                "[book] Confirmation page not detected yet — waiting 5s for slow server…"
            )
            await asyncio.sleep(5)
            url = page.url
            if any(p in url for p in ("confirmation", "confirmed", "success")):
                logger.info(f"[book] Booking confirmed via URL (delayed): {url}")
                return True
            logger.error(
                f"SELECTOR_FAILED: key='{confirmed_key}'  selector={confirmed_selector!r}\n"
                f"  Confirmation page not detected after clicking confirm.\n"
                f"  Current URL: {url}\n"
                f"  → Check if booking actually succeeded, then update src/selectors.py"
            )
            return False

    # ------------------------------------------------------------------
    # Payment detection helpers
    # ------------------------------------------------------------------

    async def _page_needs_payment(self, page: Page) -> bool:
        """True if the checkout page shows any payment-related UI."""
        try:
            el = await page.query_selector(sel.get("no_payment_indicator"))
            if el:
                return True
            el2 = await page.query_selector(sel.get("saved_payment_card"))
            return el2 is not None
        except Exception:
            return False

    async def _fill_cvc(self, page: Page) -> None:
        """Fill the CVC field on the checkout page if it exists.

        Tock/Stripe may embed the CVC input inside an iframe, so we search
        both the main frame and all child frames.
        """
        selector = sel.get("cvc_input")
        # TockBrowser.find_in_frames searches main frame + all iframes (Stripe embeds CVC)
        el = await self.browser.find_in_frames(page, selector)
        if el:
            await el.fill(self.config.card_cvc)
            logger.info("[book] CVC filled.")
        else:
            logger.debug("[book] CVC field not found on page (may not be required).")

    async def _has_saved_card(self, page: Page) -> bool:
        """True if a saved payment card widget is visible."""
        try:
            el = await page.query_selector(sel.get("saved_payment_card"))
            return el is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Generic selector wait helper
    # ------------------------------------------------------------------

    async def _wait_for_selector(
        self, page: Page, key: str, context: str = "", timeout: int = 10000
    ) -> bool:
        selector = sel.get(key)
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
                f"  Context: {context}\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
            return False

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _best_per_date(self, slots: list[AvailableSlot]) -> list[AvailableSlot]:
        """
        From a list of slots (potentially many per date), return the single
        best slot per calendar date. Assumes slots are already sorted by
        proximity to preferred_time (checker does this), so first per date wins.
        """
        seen: set[str] = set()
        best: list[AvailableSlot] = []
        for slot in slots:
            if slot.slot_date_str not in seen:
                seen.add(slot.slot_date_str)
                best.append(slot)
        return best
