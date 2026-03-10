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

from playwright.async_api import Page

import src.selectors as sel
from src.browser import TockBrowser
from src.checker import AvailableSlot
from src.config import Config
from src.notifier import Notifier

logger = logging.getLogger(__name__)

BASE_URL = "https://www.exploretock.com"

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
        self, slots: list[AvailableSlot]
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
            try:
                success = await self._book_single(slot, booking_won)
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
        self, slot: AvailableSlot, booking_won: asyncio.Event
    ) -> bool:
        """
        Full booking flow for one slot on its own Playwright page.
        Returns True if the booking was confirmed.
        """
        if self.config.dry_run:
            self.notifier.dry_run_would_book(slot)
            return False

        page = await self.browser.new_page()
        try:
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

            # Wait for day buttons to render inside the calendar dropdown.
            # Use wait_for_selector so we move on as soon as they're ready.
            try:
                await page.wait_for_selector(
                    sel.get("available_day_button"), timeout=5000
                )
            except Exception:
                pass  # day may not have is-available class yet; proceed anyway

            # ── Step 2: click the calendar day ────────────────────────
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            if not await self._click_calendar_day(page, slot):
                return False

            # Wait for the slot list to start loading after the day click
            await page.wait_for_timeout(400)

            # ── Step 3: click the time slot ───────────────────────────
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            if not await self._click_time_slot(page, slot):
                return False

            # Brief tick so checkout navigation begins before we start waiting
            await page.wait_for_timeout(200)

            # ── Step 4: wait for checkout page ────────────────────────
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            if not await self._wait_for_checkout(page, slot):
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
            await page.close()

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    async def _click_calendar_day(self, page: Page, slot: AvailableSlot) -> bool:
        """Click the calendar button matching slot.slot_date."""
        key = "available_day_button"
        selector = sel.get(key)
        target_num = str(slot.slot_date.day)

        day_buttons = await page.query_selector_all(selector)
        for btn in day_buttons:
            try:
                # Button text content IS the day number directly.
                # (Old code looked for child span.B2; now span.MuiTypography-root.
                # Reading btn.text_content() is span-class-agnostic and more robust.)
                text = (await btn.text_content() or "").strip()
                if text == target_num:
                    await btn.click()
                    return True
            except Exception:
                continue

        logger.error(
            f"SELECTOR_FAILED: key='{key}'\n"
            f"  Could not find or click day {target_num} for {slot.slot_date_str}.\n"
            f"  → Update src/selectors.py"
        )
        return False

    async def _click_time_slot(self, page: Page, slot: AvailableSlot) -> bool:
        """Find the time slot closest to slot.slot_time and click it."""
        slot_key = "available_slot_button"
        time_key = "slot_time_text"
        slot_selector = sel.get(slot_key)
        time_selector = sel.get(time_key)

        try:
            await page.wait_for_selector(slot_selector, timeout=10000)
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{slot_key}'  selector={slot_selector!r}\n"
                f"  No time slots appeared after clicking the day.\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
            return False

        slot_buttons = await page.query_selector_all(slot_selector)

        # Try exact match first
        for btn in slot_buttons:
            try:
                span = await btn.query_selector(time_selector)
                if span:
                    text = (await span.text_content() or "").strip()
                    if text.upper() == slot.slot_time.upper():
                        await btn.click()
                        logger.info(f"[book] Clicked time slot: {slot.slot_time}")
                        return True
            except Exception:
                continue

        # Fallback: click the first available slot (checker already picked closest)
        if slot_buttons:
            try:
                first_span = await slot_buttons[0].query_selector(time_selector)
                actual_time = ""
                if first_span:
                    actual_time = (await first_span.text_content() or "").strip()
                logger.warning(
                    f"[book] Exact time '{slot.slot_time}' not found on page; "
                    f"clicking first available slot ({actual_time or 'unknown time'})."
                )
                await slot_buttons[0].click()
                return True
            except Exception as e:
                logger.error(f"[book] Could not click first slot: {e}")

        logger.error(
            f"SELECTOR_FAILED: key='{slot_key}'  No slots found after day click.\n"
            f"  → Update src/selectors.py"
        )
        return False

    async def _wait_for_checkout(self, page: Page, slot: AvailableSlot) -> bool:
        """Return True when the checkout/booking-details page is detected."""
        key = "checkout_container"
        selector = sel.get(key)
        try:
            await page.wait_for_selector(selector, timeout=20000)
            logger.info(f"[book] Checkout page loaded for {slot.slot_date_str}.")
            return True
        except Exception as e:
            # URL-based fallback
            url = page.url
            if any(p in url for p in ("/checkout", "/reservation", "/book")):
                logger.info(f"[book] Checkout detected via URL: {url}")
                return True
            logger.error(
                f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
                f"  Checkout page not detected after clicking time slot.\n"
                f"  Current URL: {url}\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
            return False

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

        # Click confirm
        confirm_key = "confirm_button"
        confirm_selector = sel.get(confirm_key)
        try:
            await page.wait_for_selector(confirm_selector, timeout=15000)
            await page.click(confirm_selector)
            logger.info("[book] Clicked confirm button.")
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{confirm_key}'  selector={confirm_selector!r}\n"
                f"  Could not find or click the confirm button.\n"
                f"  Current URL: {page.url}\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
            return False

        # Verify confirmation — wait_for_selector polls on its own, no fixed sleep needed
        confirmed_key = "booking_confirmed"
        confirmed_selector = sel.get(confirmed_key)
        try:
            await page.wait_for_selector(confirmed_selector, timeout=20000)
            logger.info(f"[book] Confirmation element found — BOOKED: {slot}")
            return True
        except Exception:
            # URL-based fallback
            url = page.url
            if any(p in url for p in ("confirmation", "confirmed", "success")):
                logger.info(f"[book] Booking confirmed via URL: {url}")
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
        key = "cvc_input"
        selector = sel.get(key)

        # Search main frame first, then all iframes (Stripe embeds CVC in iframe)
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
        for frame in frames:
            try:
                el = await frame.query_selector(selector)
                if el:
                    await el.fill(self.config.card_cvc)
                    label = frame.name or frame.url or "main"
                    logger.info(f"[book] CVC filled (frame: {label}).")
                    return
            except Exception:
                continue

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
