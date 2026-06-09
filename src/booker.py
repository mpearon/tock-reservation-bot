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
from datetime import datetime
from enum import Enum

from playwright.async_api import Page

import src.selectors as sel
from src import selector_metrics
from src.browser import TockBrowser
from src.checker import AvailableSlot
from src.config import Config
from src.notifier import Notifier
from src.selectors import (
    get_slot_button_selectors,
    is_generic_slot_selector,
    is_playwright_selector,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.exploretock.com"


class BookingOutcome(Enum):
    """Result of a booking race.

    CONFIRMED           — slot booked and verified
    UNVERIFIED_CONFIRM  — confirm clicked but verification timed out;
                          Tock MAY have accepted. Operator must verify.
    FAILED              — no confirm clicked, or click verifiably failed
    """
    CONFIRMED = "confirmed"
    UNVERIFIED_CONFIRM = "unverified_confirm"
    FAILED = "failed"

_SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "debug_screenshots"
)

# Where _dump_click_failure() writes the page HTML when a slot-click fails.
# Always on (failures are rare) so the next release window is self-diagnosing:
# the dump reveals whether the slot vanished (race-loss) or the button was
# present but unmatched (scope bug) — the 2026-06-05 ambiguity.
_BOOKING_FAILURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "booking_failures"
)
# Cap dump files so a pathological failure burst can't exhaust the disk
# (self-DoS). ~50 full search-page HTMLs ≈ a few tens of MB.
_MAX_FAILURE_DUMPS = 50


def _write_failure_dump(
    html: str, clean_url: str, slot_str: str, date_str: str, stage: str
) -> str:
    """Synchronous dump writer (run via asyncio.to_thread). Creates the file
    owner-only (0600) so authenticated page HTML isn't world-readable even when
    the process umask is permissive. Returns the written path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(
        _BOOKING_FAILURE_DIR, f"faildom_{stage}_{ts}_{date_str}.html"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"<!-- stage={stage} url={clean_url} slot={slot_str} -->\n")
        f.write(html)
    return path

# Codex MEDIUM 1: classification of "generic Book" selectors (vs.
# specific per-time-slot selectors) now lives in src/selectors.py
# alongside `get_slot_button_selectors` so the two cannot drift apart.
# Use `is_generic_slot_selector(s)` for membership tests.

# Codex LOW 1 + B2 review MEDIUM: match the actual checkout-style URL
# path segment, not the full URL. Substrings in query string or
# fragment (e.g., `/search?next=/checkout/abc`) must NOT match.
_CHECKOUT_PATH_RE = re.compile(
    r"/(checkout|reservation|book)(?:/|$)", re.IGNORECASE
)


def _checkout_url_matches(url: str) -> bool:
    """Return True iff the URL's PATH (not query, not fragment) contains
    a checkout/reservation/book segment. Parses with urllib so query
    strings and fragments can never produce a false positive."""
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path or ""
    except Exception:
        return False
    return bool(_CHECKOUT_PATH_RE.search(path))


# Codex LOW 2: payment-visible JS includes a URL precondition so
# `/account/payment-methods` (which contains identical "Add card" text)
# cannot false-trigger checkout detection while the bot is on the wrong
# page. The URL check happens INSIDE the page context, not just in
# Python — so the JS sees the live page state at the moment it runs.
_PAYMENT_VISIBLE_JS_SOURCE = (
    "() => {"
    "  const path = (window.location && window.location.pathname || '').toLowerCase();"
    "  if (!/\\/(checkout|reservation|book)(\\/|$)/.test(path)) return false;"
    "  const card = document.querySelector("
    "    '[data-testid=\"saved-card\"], .SavedCard, "
    ".PaymentMethod--saved, [data-testid=\"payment-card\"]'"
    "  );"
    "  if (card) return true;"
    "  const ctrls = Array.from(document.querySelectorAll('button, a'));"
    "  return ctrls.some(el => "
    "    /add (payment|card|a card)/i.test(el.innerText || '')"
    "  );"
    "}"
)


# Single-evaluate slot-click JS (Phase B1.2). Called from _click_time_slot.
# Iterates DOM-side and clicks inside the same call, eliminating per-button
# Python↔browser round-trips. Honors strict_time_match by refusing the
# first-button fallback (Codex HIGH from Phase A+2).
_CLICK_TIME_SLOT_JS = r"""
(args) => {
  const { selector, targetTime, slotTimeRaw, isGeneric, strictTimeMatch } = args;
  const buttons = Array.from(document.querySelectorAll(selector));
  const upperTarget = targetTime;
  const escapedSlotTime = slotTimeRaw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const slotWordRe = new RegExp('\\b' + escapedSlotTime + '\\b', 'i');
  const timeRe = /\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b/i;

  let bestBtn = null;
  let bestText = null;

  for (const btn of buttons) {
    const text = (btn.textContent || btn.innerText || '').trim();
    const upperText = text.toUpperCase();

    if (upperText.includes(upperTarget)) {
      btn.click();
      return { clicked: true, text, reason: 'exact' };
    }

    const m = text.match(timeRe);
    if (m && m[1].trim().toUpperCase() === upperTarget) {
      btn.click();
      return { clicked: true, text, reason: 'regex' };
    }

    if (isGeneric) {
      const parent = btn.parentElement;
      const parentText = parent
        ? (parent.textContent || parent.innerText || '').trim()
        : '';
      const parentUpper = parentText.toUpperCase();
      if (parentUpper.includes(upperTarget) || slotWordRe.test(parentText)) {
        btn.click();
        return {
          clicked: true,
          text: parentText.slice(0, 80),
          reason: 'generic-parent',
        };
      }
      // Generic button without time confirmation in parent — never a fallback
      continue;
    }

    if (bestBtn === null) {
      bestBtn = btn;
      bestText = text;
    }
  }

  if (bestBtn !== null && !strictTimeMatch) {
    bestBtn.click();
    return { clicked: true, text: bestText, reason: 'first-fallback' };
  }
  if (bestBtn !== null && strictTimeMatch) {
    return {
      clicked: false,
      text: bestText,
      reason: 'strict-refused-fallback',
    };
  }
  return { clicked: false, text: null, reason: 'no-match' };
}
"""

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
        # Set the moment ANY task is about to click confirm. Other tasks
        # see this and abort even if booking_won never gets set (e.g. when
        # confirm-verification fails but Tock may have accepted the booking).
        # Prevents sequential double-booking from the confirm-uncertainty
        # window identified by Codex's adversarial review of Phase A+2.
        self._confirm_attempted = asyncio.Event()
        # Slot whose confirm click was attempted but could not be verified.
        # Set by _book_single's soft-win path. Read by book_best_slot_race
        # to return UNVERIFIED_CONFIRM. Only a process restart clears this —
        # NOT .clear() at the start of book_best_slot_race like
        # _confirm_attempted. This is the session-level guard that prevents
        # the next monitor poll from starting another race after a soft-win
        # (Codex pass 2 HIGH finding).
        self._unverified_confirm_slot: AvailableSlot | None = None

    # ------------------------------------------------------------------
    # Public: race multiple slots
    # ------------------------------------------------------------------

    async def book_best_slot_race(
        self, slots: list[AvailableSlot],
        warm_pages: dict[str, Page] | None = None,
    ) -> tuple[BookingOutcome, AvailableSlot | None]:
        """
        Race ALL provided slots concurrently — one asyncio task per slot,
        even multiple times on the same calendar date (e.g. Friday 5pm AND
        Friday 8pm). The first task to clear the confirm-lock and succeed
        is the winner; all others see ``booking_won`` set and abort.

        Phase A+2 changed this from "pick best slot per date" to "race all"
        because slots disappear within ~5s of release on competitive
        restaurants — racing both 5pm and 8pm of the same Friday roughly
        doubles the hit chance.

        Warm pages are claimed via pop(), so each warm page is owned by
        exactly one task. Additional same-date tasks open fresh pages.

        Returns a (BookingOutcome, slot_or_none) tuple:
          CONFIRMED          — slot booked and verified; slot is the winner
          UNVERIFIED_CONFIRM — confirm clicked but unverifiable; slot is the
                               attempted slot. Bot must idle until restart.
          FAILED             — all attempts failed; slot is None.
        """
        if not slots:
            return BookingOutcome.FAILED, None

        # Persistent uncertain-booking guard. Survives auto-restart, Mac mini
        # reboots, PM2 restarts. Operator must remove booking_uncertain.json
        # after verifying on Tock. Codex pass 3 found that the in-memory
        # _unverified_confirm_slot was erased by main.py's auto-restart loop
        # (constructs a fresh TockBooker), enabling silent double-booking.
        from src.booking_uncertain import read_uncertain
        persisted = read_uncertain()
        if persisted is not None:
            logger.warning(
                f"[book] Refusing to start race — booking_uncertain.json "
                f"records an unverifiable confirm for "
                f"{persisted.slot_date_str} ({persisted.day_of_week}) @ "
                f"{persisted.slot_time}. Verify at "
                "https://www.exploretock.com/account/reservations and "
                "remove the file before running again."
            )
            from src.checker import AvailableSlot as _AvailableSlot
            from datetime import date as _date_cls
            try:
                slot_date = _date_cls.fromisoformat(persisted.slot_date_str)
            except ValueError:
                slot_date = _date_cls.today()  # corrupt; degrade gracefully
            return (
                BookingOutcome.UNVERIFIED_CONFIRM,
                _AvailableSlot(
                    slot_date=slot_date,
                    slot_time=persisted.slot_time,
                    day_of_week=persisted.day_of_week,
                ),
            )

        # Fall through: also check the in-memory guard (race within same
        # process where the file write may have failed).
        if self._unverified_confirm_slot is not None:
            logger.warning(
                f"[book] Refusing to start race — previous race in this "
                f"process had unverified confirm for {self._unverified_confirm_slot}."
            )
            return BookingOutcome.UNVERIFIED_CONFIRM, self._unverified_confirm_slot

        # Reset the confirm-attempted gate for this race only. The
        # _unverified_confirm_slot above persists across races within
        # the same process — only a restart clears it.
        self._confirm_attempted.clear()

        # Phase A+2: race ALL slots, not just one per date. The asyncio.Lock +
        # booking_won.Event serialize the actual confirm click — so attempting
        # 5pm AND 8pm of the same Friday concurrently still produces at most
        # one booking. Maximizes hit rate when releases drop multiple times
        # on the same date.
        candidates = list(slots)  # shallow copy so caller mutations during the race are isolated
        logger.info(
            f"Starting concurrent booking race for {len(candidates)} slot(s): "
            + " | ".join(str(s) for s in candidates)
        )

        booking_won = asyncio.Event()
        winner: list[AvailableSlot] = []

        async def attempt(slot: AvailableSlot) -> None:
            self.notifier.booking_attempting(slot)
            # Use pop() so each warm page is claimed by exactly ONE task. After
            # Phase A+2's race-all-slots change, multiple slots on the same date
            # can race concurrently — without pop(), both tasks would grab the
            # same Page object and corrupt each other's DOM state. Subsequent
            # tasks for the same date fall through to new_page() (slower but
            # state-isolated; only the loser pays the cost).
            page = warm_pages.pop(slot.slot_date_str, None) if warm_pages else None
            try:
                success = await self._book_single(slot, booking_won, warm_page=page)
                if success:
                    winner.append(slot)
            except Exception as e:
                logger.error(f"[book] Unhandled exception for {slot}: {e}")

        tasks = [asyncio.create_task(attempt(s)) for s in candidates]
        await asyncio.gather(*tasks, return_exceptions=True)

        if winner:
            return BookingOutcome.CONFIRMED, winner[0]
        if self._unverified_confirm_slot is not None:
            return BookingOutcome.UNVERIFIED_CONFIRM, self._unverified_confirm_slot
        return BookingOutcome.FAILED, None

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

        # Use warm page from checker (sniper warm or normal-mode handoff) or
        # create fresh. A page is considered usable only if it's still open;
        # a closed handoff is logged so operators can distinguish between
        # "no warm page provided" and "provided but stale".
        warm_page_unusable = warm_page is not None and warm_page.is_closed()
        if warm_page_unusable:
            logger.warning(
                f"[book] {slot} — warm page provided but already closed; "
                "falling back to fresh navigation"
            )
        page = warm_page if warm_page and not warm_page.is_closed() else None
        owns_page = page is None  # True ⇒ booker created a fresh page; False ⇒ using warm
        # Phase B3.3: when we need a fresh page, prefer the pre-warmed pool
        # so we don't pay full new_page() launch cost on cold race tasks.
        # Falls through to new_page() when the pool is empty.
        # Use isinstance(PagePool) — not getattr(...) is not None — because
        # tests construct browser via MagicMock() where every attribute
        # auto-resolves to another MagicMock. PagePool is the only valid
        # source of pre-warmed pages, so isinstance is the safe sentinel.
        from src.page_pool import PagePool  # local import: avoids cycle
        from_pool = False
        if page is None:
            page_pool = getattr(self.browser, "page_pool", None)
            if isinstance(page_pool, PagePool):
                page = await page_pool.acquire()
                from_pool = True
            else:
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

                # B1.5: when skip_day_click_check is True, defer the calendar-day
                # click until after we try the time-slot click — Tock's
                # `?date=YYYY-MM-DD` URL may already select the date in the
                # SPA so the click is redundant. If the time-slot click finds
                # no buttons, we fall back to clicking the day and retrying
                # once.
                skip_click = self.config.skip_day_click_check
                day_clicked = False
                if not skip_click:
                    # Wait for day buttons to render inside the calendar.
                    try:
                        await page.wait_for_selector(
                            sel.get("all_day_button"), timeout=5000
                        )
                    except Exception:
                        pass  # calendar may still be loading; proceed anyway

                    # ── Step 2: click the calendar day ────────────────────
                    if booking_won.is_set():
                        self.notifier.booking_aborted(slot, "another slot already booked")
                        return False

                    if not await self._click_calendar_day(page, slot):
                        return False
                    day_clicked = True

                    # Wait reactively for slot buttons after day click
                    for try_sel in get_slot_button_selectors()[:2]:
                        try:
                            await page.wait_for_selector(try_sel, timeout=2000)
                            break
                        except Exception:
                            continue
            else:
                logger.info(f"[book] {slot} → using warm page (skipping navigation)")
                # No skip-day-click decision needed when the booker reuses a
                # warm page — the checker already had the correct date
                # selected. Initialize so the post-Step-3 fallback below
                # never trips for warm-page bookings.
                skip_click = False
                day_clicked = True

            await self._booking_screenshot(page, "01_booking_start")

            # ── Step 3: click the time slot ───────────────────────────
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            # When operating on a warm/handoff page, demand an exact time match.
            # The page was last touched by the checker, which detected this exact
            # slot — if it's no longer visible, the slot vanished and the
            # first-button fallback would book the wrong time (Codex review).
            using_warm_page = not owns_page

            # Fast path: the checker tagged the exact button it found with a
            # data-sniper-target attribute on this (retained warm) page. Click
            # it directly — skipping the ~4s selector rediscovery AND the
            # parent-only time match that lost the 2026-06-05 slots. If the tag
            # is gone the slot vanished (lost the race), so dump the DOM and
            # fail fast instead of slow-scanning a page whose slot is gone.
            if using_warm_page and slot.target_selector:
                slot_clicked = await self._click_tagged_slot(page, slot)
                if not slot_clicked:
                    # Tag missing: the slot may have vanished OR an SPA rerender
                    # dropped our custom attribute while the button is still
                    # present (also covers a transient click error). If another
                    # task already won, abort quietly (no rescan, no dump).
                    # Otherwise rescan the live DOM (strict) before giving up, so
                    # a benign rerender can't silently cost us a bookable slot.
                    if booking_won.is_set():
                        self.notifier.booking_aborted(slot, "another slot already booked")
                        return False
                    logger.info(
                        f"[book] {slot} — tagged element not found; rescanning "
                        "live DOM (strict) before giving up"
                    )
                    slot_clicked = await self._click_time_slot(
                        page, slot, strict_time_match=True
                    )
                    if not slot_clicked:
                        if not booking_won.is_set():
                            await self._dump_click_failure(page, slot, stage="click")
                        return False
            else:
                slot_clicked = await self._click_time_slot(
                    page, slot, strict_time_match=using_warm_page
                )
                if not slot_clicked and skip_click and not day_clicked:
                    # B1.5 fallback: skip-mode found no slot buttons; click the
                    # calendar day and retry the time-slot click once.
                    logger.debug(
                        f"[book] {slot} — skip-mode found no slot buttons; "
                        "falling back to click_calendar_day + re-click"
                    )
                    if booking_won.is_set():
                        self.notifier.booking_aborted(slot, "another slot already booked")
                        return False
                    if not await self._click_calendar_day(page, slot):
                        return False
                    day_clicked = True
                    for try_sel in get_slot_button_selectors()[:2]:
                        try:
                            await page.wait_for_selector(try_sel, timeout=2000)
                            break
                        except Exception:
                            continue
                    slot_clicked = await self._click_time_slot(
                        page, slot, strict_time_match=using_warm_page
                    )
                if not slot_clicked:
                    # Don't dump if another task already won — that's a clean
                    # race loss, not a diagnosable click failure.
                    if not booking_won.is_set():
                        await self._dump_click_failure(page, slot, stage="click")
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

            checkout_ok = await self._wait_for_checkout(
                page, slot, booking_won=booking_won
            )
            await self._booking_screenshot(
                page,
                "03_checkout_loaded" if checkout_ok else "03_checkout_timeout"
            )
            if not checkout_ok:
                if not booking_won.is_set():
                    await self._dump_click_failure(page, slot, stage="checkout")
                return False

            # ── Step 5a: prep (NO lock — concurrent across tasks) ─────
            # Phase B3.1: payment detection, CVC fill, and wait-for-confirm-
            # button used to all run under the lock, serializing every
            # racing task. They're idempotent per-page, so we run them
            # without the lock to maximize concurrency.
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            prep_ok = await self._prepare_for_confirm(
                page, slot, booking_won=booking_won
            )
            if not prep_ok:
                if not booking_won.is_set():
                    await self._dump_click_failure(page, slot, stage="prep")
                return False

            # ── Step 5b: confirm click + verify (locked — only one wins) ─
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
                if self._confirm_attempted.is_set():
                    # Another task already clicked confirm but couldn't verify.
                    # Tock may have accepted that booking — do NOT click again.
                    self.notifier.booking_aborted(
                        slot,
                        "another slot has an unverified confirm in flight — "
                        "skipping to avoid double-booking",
                    )
                    return False

                # Mark that we are about to click confirm. From this point on,
                # no other task can click even if our own verification fails
                # — the user must verify manually.
                self._confirm_attempted.set()

                success = await self._execute_confirm_click_and_verify(page, slot)
                if success:
                    booking_won.set()
                    self.notifier.booking_confirmed(slot)
                    return True

                # Click happened but verification failed. Tock may have
                # accepted the booking silently. Set BOTH the in-memory flag
                # AND write the disk file. The disk file survives main.py's
                # auto-restart loop (Codex pass 3 finding); the in-memory
                # flag covers the same-process case where disk write fails.
                self._unverified_confirm_slot = slot
                from src.booking_uncertain import (
                    UncertainBooking, write_uncertain
                )
                from datetime import datetime as _dt
                write_uncertain(UncertainBooking(
                    slot_date_str=slot.slot_date_str,
                    slot_time=slot.slot_time,
                    day_of_week=slot.day_of_week,
                    detected_at_iso=_dt.now().isoformat(),
                ))
                logger.error(
                    f"[book] Confirm clicked for {slot} but verification "
                    "failed. Possible silent success — operator must verify "
                    "AND remove booking_uncertain.json before next run."
                )
                self.notifier.error(
                    "⚠️ Booking confirmation unverifiable",
                    f"Clicked confirm for {slot} but could not verify success. "
                    "Tock MAY have accepted the booking. "
                    "1) Check https://www.exploretock.com/account/reservations "
                    "2) If no reservation: rm booking_uncertain.json then restart "
                    "3) If reservation present: keep the file in place; the bot "
                    "will refuse all future booking attempts. "
                    "This guard now SURVIVES restart (Codex pass 3 fix).",
                )
                booking_won.set()
                return False

        except Exception as e:
            logger.error(f"[book] Error booking {slot}: {e}")
            return False
        finally:
            # After Phase A+2: warm pages are popped from checker ownership
            # via pop_warm_page() and transferred to this method. The booker
            # is now responsible for closing them after every attempt — success
            # OR failure — to prevent the leak Codex pass 3 caught.
            #
            # Phase B3.3: pages obtained from the page pool go through
            # pool.release() (which closes them — DOM is dirty after a race
            # attempt). All other booker-owned pages still close directly.
            if page is not None:
                try:
                    if from_pool:
                        await self.browser.page_pool.release(page)
                    elif not page.is_closed():
                        await page.close()
                except Exception:
                    pass

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

    async def _click_time_slot(
        self, page: Page, slot: AvailableSlot,
        strict_time_match: bool = False,
    ) -> bool:
        """Find the time slot matching slot.slot_time and click it.

        Iterates all matching buttons and compares text content to find the
        correct time.

        strict_time_match=False (default): if no exact match is found, falls
          back to clicking the first non-generic button. Acceptable for fresh
          navigation where the page state was just established and a missing
          exact time often means the button text format changed slightly.

        strict_time_match=True: refuse the fallback. Used when the booker
          is operating on a warm/handoff page that was last touched by the
          checker — if the target time isn't visible now, the slot likely
          vanished and clicking any other button would book the wrong time
          (Codex adversarial review HIGH finding).
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

        # B2.4: telemetry for selector hit-rate analysis. Best-effort —
        # never let a metrics bug break the booking flow.
        try:
            selector_metrics.record_match("slot_button_book", matched_selector)
        except Exception as exc:
            logger.debug(f"[book] selector_metrics.record_match failed: {exc}")

        # Single browser-side iteration: match + click in one round-trip.
        target_time = slot.slot_time.strip().upper()
        is_generic = is_generic_slot_selector(matched_selector)

        # Codex HIGH 2: Playwright-only selectors (`:has-text`, `:text`,
        # `:visible`) cannot be passed to document.querySelectorAll —
        # fall back to a Python locator iteration for those, preserving
        # the JS fast path for plain CSS selectors.
        if is_playwright_selector(matched_selector):
            return await self._click_time_slot_locator_loop(
                page, slot, matched_selector, is_generic, strict_time_match
            )

        try:
            result = await page.evaluate(
                _CLICK_TIME_SLOT_JS,
                {
                    "selector": matched_selector,
                    "targetTime": target_time,
                    "slotTimeRaw": slot.slot_time,
                    "isGeneric": is_generic,
                    "strictTimeMatch": strict_time_match,
                },
            )
        except Exception as e:
            logger.error(f"[book] Slot-click evaluate failed: {type(e).__name__}: {e}")
            return False

        if not isinstance(result, dict):
            logger.error(f"[book] Slot-click JS returned unexpected shape: {result!r}")
            return False

        clicked = bool(result.get("clicked"))
        reason = result.get("reason", "")
        text = result.get("text")

        if clicked:
            if reason == "exact":
                logger.info(
                    f"[book] Clicked slot button matching '{slot.slot_time}': {text}"
                )
            elif reason == "regex":
                logger.info(f"[book] Clicked slot button (regex match): {text}")
            elif reason == "generic-parent":
                logger.info(
                    f"[book] Clicked generic 'Book' button — "
                    f"time confirmed in parent: {text!r}"
                )
            elif reason == "first-fallback":
                logger.warning(
                    f"[book] No exact time match for '{slot.slot_time}' — "
                    f"clicked first specific button: {text}"
                )
            else:
                logger.info(
                    f"[book] Clicked slot button (reason={reason!r}): {text}"
                )
            return True

        if reason == "strict-refused-fallback":
            logger.warning(
                f"[book] No exact time match for '{slot.slot_time}' on warm "
                "page; refusing first-button fallback (slot likely vanished) "
                "— returning False so the race tries another slot or a fresh "
                "page next poll"
            )
        else:
            logger.error(
                f"[book] No clickable slot button found for '{slot.slot_time}' "
                f"(selector: {matched_selector!r})"
            )
        return False

    async def _click_time_slot_locator_loop(
        self, page: Page, slot: AvailableSlot,
        matched_selector: str, is_generic: bool, strict_time_match: bool,
    ) -> bool:
        """Locator-based fallback for Playwright-only matched selectors
        (`:has-text`, `:text(`, `:visible`) which can't be passed to
        document.querySelectorAll. Mirrors the pre-B1.2 algorithm but
        runs on the slow path only when the JS fast path is unsafe.
        """
        target_time = slot.slot_time.strip().upper()
        slot_word_re = re.compile(
            r"\b" + re.escape(slot.slot_time) + r"\b", re.IGNORECASE
        )
        time_re = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b", re.IGNORECASE)

        locator = page.locator(matched_selector)
        try:
            count = await locator.count()
        except Exception as e:
            logger.error(f"[book] PW-locator count failed: {type(e).__name__}: {e}")
            return False

        best_btn = None
        for i in range(count):
            btn = locator.nth(i)
            try:
                text = (await btn.text_content() or "").strip()
                upper = text.upper()
                # Exact substring match
                if target_time in upper:
                    await btn.click()
                    logger.info(
                        f"[book] Clicked slot button matching '{slot.slot_time}': {text}"
                    )
                    return True
                # Regex time match
                m = time_re.search(text)
                if m and m.group(1).strip().upper() == target_time:
                    await btn.click()
                    logger.info(f"[book] Clicked slot button (regex match): {text}")
                    return True
                # Generic 'Book' button: only click if parent has the time
                if is_generic:
                    try:
                        parent_text = (
                            await btn.locator("..").text_content() or ""
                        ).strip()
                    except Exception:
                        parent_text = ""
                    if (
                        target_time in parent_text.upper()
                        or slot_word_re.search(parent_text)
                    ):
                        await btn.click()
                        logger.info(
                            f"[book] Clicked generic 'Book' button — "
                            f"time confirmed in parent: {parent_text[:80]!r}"
                        )
                        return True
                    continue  # never set best_btn for unmatched generic
                if best_btn is None:
                    best_btn = btn
            except Exception:
                continue

        if best_btn is not None and not strict_time_match:
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
        if best_btn is not None and strict_time_match:
            logger.warning(
                f"[book] No exact time match for '{slot.slot_time}' on warm "
                "page; refusing first-button fallback (slot likely vanished) "
                "— returning False so the race tries another slot or a fresh "
                "page next poll"
            )
        logger.error(
            f"[book] No clickable slot button found for '{slot.slot_time}' "
            f"(selector: {matched_selector!r})"
        )
        return False

    async def _click_tagged_slot(self, page: Page, slot: AvailableSlot) -> bool:
        """Click the exact button the checker tagged with data-sniper-target.

        Returns True if the tagged element was found and clicked. Returns False
        when the slot carries no tag (fresh-nav booking) or the tag is no longer
        in the DOM (the slot vanished between detection and this click) — the
        caller treats a False on a warm page as a vanished slot.
        """
        target = getattr(slot, "target_selector", None)
        if not target:
            return False
        try:
            loc = page.locator(target)
            if await loc.count() > 0:
                # Bounded timeout: if the tagged button is present-but-not-yet
                # actionable, fail fast (3s) and let the caller rescan, rather
                # than inheriting Playwright's 30s default on the race path.
                # no_wait_after=True: don't fold the post-click checkout
                # navigation into the click budget — _wait_for_checkout owns
                # that — so a successful click can't time out as a false miss.
                await loc.first.click(timeout=3000, no_wait_after=True)
                logger.info(
                    f"[book] Clicked checker-tagged slot {slot} via {target}"
                )
                return True
        except Exception as e:
            logger.error(
                f"[book] Tagged-slot click failed for {slot} "
                f"({target}): {type(e).__name__}: {e}"
            )
        return False

    async def _dump_click_failure(
        self, page: Page, slot: AvailableSlot, stage: str = "click"
    ) -> None:
        """Dump the live page HTML on a booking-stage failure so the next
        release window is self-diagnosing. `stage` (click/checkout/prep) is
        reflected in the filename and log line so the artifact is NOT mislabeled
        as a click-match failure when the click actually succeeded. Best-effort.
        """
        # Cap check FIRST (cheap listdir) so we don't pay for a full-page
        # serialization just to discard it once the cap is reached.
        try:
            os.makedirs(_BOOKING_FAILURE_DIR, mode=0o700, exist_ok=True)
            try:
                os.chmod(_BOOKING_FAILURE_DIR, 0o700)  # tighten a pre-existing dir
            except OSError:
                pass
            existing = [
                f for f in os.listdir(_BOOKING_FAILURE_DIR)
                if f.startswith("faildom_")
            ]
        except OSError:
            existing = []
        if len(existing) >= _MAX_FAILURE_DUMPS:
            logger.debug(
                f"[book] {stage} failure DOM dump skipped — "
                f"{_MAX_FAILURE_DUMPS}-file cap reached in {_BOOKING_FAILURE_DIR}"
            )
            return

        try:
            html = await page.content()
        except Exception as e:
            logger.debug(f"[book] {stage} failure dump skipped (no content): {e}")
            return

        # Strip query/fragment from the URL header — they carry session-ish
        # params not needed for a diagnostic artifact.
        raw_url = getattr(page, "url", "") or ""
        try:
            from urllib.parse import urlparse, urlunparse
            clean_url = urlunparse(urlparse(raw_url)._replace(query="", fragment=""))
        except Exception:
            clean_url = ""

        # Offload the blocking file write to a thread so a large HTML dump can't
        # stall the event loop (and another racing task's confirm-verify).
        try:
            path = await asyncio.to_thread(
                _write_failure_dump, html, clean_url, str(slot),
                slot.slot_date_str, stage,
            )
            logger.error(f"[book] {stage}-stage failure DOM dumped → {path}")
        except Exception as e:
            logger.debug(f"[book] {stage} failure DOM dump failed: {e}")

    async def _wait_for_checkout(
        self, page: Page, slot: AvailableSlot,
        booking_won: asyncio.Event | None = None,
    ) -> bool:
        """Return True when the checkout/booking-details page is detected.

        When `booking_won` is provided, a fourth waiter races it so a losing
        task bails in milliseconds (returning False) instead of sitting the full
        30s once another slot has won — avoiding wasted latency and spurious
        diagnostic dumps.

        Race three Playwright waiters concurrently and return True on the
        first success, cutting 0–1900 ms off the old 2-s polling tick:

          1. wait_for_selector(checkout_container)
          2. wait_for_url(predicate)        — URL has /checkout/, /reservation/, /book/
          3. wait_for_function(payment_visible_js) — payment form visible
                                                     AND URL precondition

        Returns False after a 30 s overall timeout (all three waiters
        either raised or did not resolve). Losing waiters are cancelled and
        awaited so Python doesn't warn about un-awaited coroutines.
        """
        key = "checkout_container"
        selector = sel.get(key)
        total_wait = 30
        timeout_ms = total_wait * 1000

        async def _via_selector():
            await page.wait_for_selector(selector, timeout=timeout_ms)
            return "selector"

        async def _via_url():
            await page.wait_for_url(_checkout_url_matches, timeout=timeout_ms)
            return "url"

        async def _via_function():
            await page.wait_for_function(_PAYMENT_VISIBLE_JS_SOURCE, timeout=timeout_ms)
            return "function"

        tasks = [
            asyncio.create_task(_via_selector(), name="wait_for_checkout::selector"),
            asyncio.create_task(_via_url(), name="wait_for_checkout::url"),
            asyncio.create_task(_via_function(), name="wait_for_checkout::function"),
        ]
        if booking_won is not None:
            async def _via_aborted():
                await booking_won.wait()
                return "__aborted__"
            tasks.append(
                asyncio.create_task(_via_aborted(), name="wait_for_checkout::aborted")
            )
        won_via: str | None = None
        try:
            for fut in asyncio.as_completed(tasks, timeout=total_wait):
                try:
                    label = await fut
                except Exception as e:
                    logger.debug(
                        f"[book] checkout waiter raised: {type(e).__name__}: {e}"
                    )
                    continue
                won_via = label
                break
        except asyncio.TimeoutError:
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Drain cancellations so Python doesn't warn about un-awaited
            # coroutines and so any stray Playwright cleanup runs.
            await asyncio.gather(*tasks, return_exceptions=True)

        if won_via == "__aborted__":
            logger.info(
                f"[book] Checkout wait aborted — another slot won "
                f"({slot.slot_date_str})"
            )
            return False
        if won_via:
            logger.info(
                f"[book] Checkout detected via {won_via} for {slot.slot_date_str}"
            )
            return True

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
            os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(_SCREENSHOT_DIR, f"booking_{ts}_{step}.png")
            await page.screenshot(path=path, full_page=True)
            logger.info(f"[book] Screenshot saved: {path}")
        except Exception as e:
            logger.debug(f"[book] Screenshot failed at step '{step}': {e}")

    async def _prepare_for_confirm(
        self, page: Page, slot: AvailableSlot,
        booking_won: asyncio.Event | None = None,
    ) -> bool:
        """Per-page idempotent preparation for the confirm click. Runs
        WITHOUT the lock so N racing tasks can prep concurrently
        (Phase B3.1).

        Steps:
          - payment detection
          - wait up to 9 min for the operator to add a card if missing
          - CVC fill on the saved card (if any)
          - wait for the confirm button to be visible

        Returns True iff the page is in a ready-to-click state. Returns
        False early if `booking_won` is set during the payment-card
        wait (Codex B3 review MEDIUM 1: avoids N*4 reloads/min when
        N racing tasks are all waiting for the same card to appear).
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
                # Codex B3 MEDIUM 1: short-circuit if another racing task
                # already won — no point reloading our page every 15s.
                if booking_won is not None and booking_won.is_set():
                    logger.info(
                        "[book] Another slot already booked while waiting "
                        "for payment card. Aborting this task's wait."
                    )
                    return False
                await asyncio.sleep(PAYMENT_POLL_INTERVAL_SEC)
                waited += PAYMENT_POLL_INTERVAL_SEC
                if booking_won is not None and booking_won.is_set():
                    logger.info(
                        "[book] booking_won fired during sleep; aborting wait."
                    )
                    return False
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

        # Fill CVC for saved card if configured (per-page; idempotent)
        if self.config.card_cvc:
            await self._fill_cvc(page)
        elif needs_payment and has_card:
            logger.warning(
                "[book] Saved card detected but TOCK_CARD_CVC is not set in .env — "
                "checkout may fail if CVC is required."
            )

        # Wait once for the confirm button to be visible. Done in prep so
        # the lock-protected click section is as short as possible.
        confirm_selector = sel.get("confirm_button")
        try:
            await page.wait_for_selector(confirm_selector, timeout=15000)
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='confirm_button'  selector={confirm_selector!r}\n"
                f"  Confirm button not found on page.\n"
                f"  Current URL: {page.url}\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
            return False
        return True

    async def _execute_confirm_click_and_verify(
        self, page: Page, slot: AvailableSlot
    ) -> bool:
        """Click the confirm button and verify Tock accepted the booking.
        MUST be called under `self._confirm_lock` (Phase B3.1).

        Returns True only if Tock's confirmation page rendered. Returns
        False if the click failed OR the confirmation could not be
        verified — the caller is responsible for soft-win bookkeeping.
        """
        confirm_selector = sel.get("confirm_button")
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
                        f"SELECTOR_FAILED: key='confirm_button'  "
                        f"selector={confirm_selector!r}\n"
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

    # Codex holistic review: the `_confirm_booking` shim was removed
    # because (a) no production code in src/ called it, (b) it was
    # semantically WEAKER than `_book_single` (no soft-win persistence,
    # no notify, no shared booking_won.set), so any future caller would
    # have silently lost the safety bookkeeping. Tests now patch the
    # split helpers `_prepare_for_confirm` and
    # `_execute_confirm_click_and_verify` directly.

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
            if el2 is not None:
                return True
            # Fallback: CVC input visible ⇒ saved card present ⇒ payment needed
            cvc_el = await self.browser.find_in_frames(page, sel.get("cvc_input"))
            return cvc_el is not None
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
        """True if a saved payment card widget is visible.

        Tock only renders the CVC re-entry field when a card is already on
        file, so CVC presence is used as a reliable fallback when the
        saved-card widget selector doesn't match.
        """
        try:
            el = await page.query_selector(sel.get("saved_payment_card"))
            if el is not None:
                return True
            # Fallback: CVC input visible ⇒ card is on file
            cvc_el = await self.browser.find_in_frames(page, sel.get("cvc_input"))
            return cvc_el is not None
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
