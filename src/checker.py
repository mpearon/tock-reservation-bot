"""
Availability checker.

For each preferred-day date within the scan window, opens the Tock search
page for that date and collects all available time slots.

Each check opens its own Playwright page and closes it when done, so state
never bleeds between date checks.

Selector failures are logged with the exact key and selector string so
updates to src/selectors.py are straightforward.
"""

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from playwright.async_api import Page

import src.selectors as sel
from src.config import Config, parse_time
from src.tracker import SlotTracker

# Debug screenshot: capture one screenshot per poll cycle on the first date
# checked. Overwrites each time so disk doesn't fill up.
_SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_screenshots")
os.makedirs(_SCREENSHOT_DIR, exist_ok=True)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.exploretock.com"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AvailableSlot:
    slot_date: date
    slot_time: str    # e.g. "5:00 PM"
    day_of_week: str  # e.g. "Friday"

    @property
    def slot_date_str(self) -> str:
        return self.slot_date.isoformat()

    def __str__(self) -> str:
        return f"{self.slot_date_str} ({self.day_of_week}) @ {self.slot_time}"


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

class AvailabilityChecker:
    def __init__(self, config: Config, browser, tracker: SlotTracker):
        self.config = config
        self.browser = browser
        self.tracker = tracker
        # Error stats from the most recent check_all() call.
        # monitor.py reads these to decide whether to switch concurrent↔sequential.
        self.last_errors: int = 0   # calendar_container failures in last poll
        self.last_checks: int = 0   # total date checks attempted in last poll
        # Sniper mode: keep pages open across polls and reload them instead of
        # opening fresh — faster (no DNS/TCP overhead) and looks more human.
        self._sniper_pages: dict[str, "Page"] = {}  # date_str -> open Page
        self._screenshot_taken_this_poll = False  # reset each poll cycle

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def close_sniper_pages(self) -> None:
        """Close all pages kept open during sniper mode. Call when window ends."""
        for page in list(self._sniper_pages.values()):
            try:
                await page.close()
            except Exception:
                pass
        self._sniper_pages.clear()
        logger.debug("[check] Sniper pages closed.")

    async def check_all(self, concurrent: bool = False, keep_pages: bool = False) -> list[AvailableSlot]:
        """
        Scan for available slots in two phases:

        Phase 1 — preferred_days (e.g. Fri/Sat/Sun): checked first. If any
          slots found, return them immediately without scanning fallback days.

        Phase 2 — fallback_days (e.g. Mon–Thu): only scanned when Phase 1
          finds nothing.

        concurrent=False (default): sequential per date — safe from Cloudflare.
        concurrent=True: parallel per date — ~4× faster, 1% error rate at 14 dates.

        After each call, self.last_errors / self.last_checks reflects the
        calendar load error rate for this poll — monitor.py uses this to
        adaptively switch between concurrent and sequential modes.
        """
        import asyncio as _asyncio

        self._screenshot_taken_this_poll = False
        errors: list[int] = [0]   # mutable counter accessible in closure

        async def _check_date_tracked(d: date) -> list[AvailableSlot]:
            result = await self._check_date(d)
            # _check_date returns [] on calendar failure; we detect it by
            # checking whether _wait_for_calendar logged a SELECTOR_FAILED.
            # Simpler proxy: if result is [] AND the date is in a phase where
            # we'd expect the calendar to load, count it as a potential error.
            # The real signal comes from _wait_for_calendar's log, so we use
            # a hook: override to count failures directly.
            return result

        # Patch _wait_for_calendar to count failures for this poll
        original_wait = self._wait_for_calendar
        async def _counting_wait(page, date_str: str) -> bool:
            ok = await original_wait(page, date_str)
            if not ok:
                errors[0] += 1
            return ok
        self._wait_for_calendar = _counting_wait  # type: ignore[method-assign]

        try:
            async def _scan_dates(dates: list[date]) -> list[AvailableSlot]:
                if not dates:
                    return []
                logger.debug(
                    f"Scanning {len(dates)} date(s) [{'concurrent' if concurrent else 'sequential'}]: "
                    + ", ".join(d.isoformat() for d in dates)
                )
                if concurrent:
                    results = await _asyncio.gather(
                        *[self._check_date(d, keep_page=keep_pages) for d in dates],
                        return_exceptions=True,
                    )
                    slots: list[AvailableSlot] = []
                    for r in results:
                        if isinstance(r, list):
                            slots.extend(r)
                    return slots
                else:
                    slots = []
                    for d in dates:
                        slots.extend(await self._check_date(d, keep_page=keep_pages))
                    return slots

            preferred_dates = self._get_target_dates(self.config.preferred_days)
            preferred_slots = await _scan_dates(preferred_dates)

            fallback_dates = self._get_target_dates(self.config.fallback_days)
            total_dates = len(preferred_dates) + len(fallback_dates)

            if preferred_slots:
                self.last_errors = errors[0]
                self.last_checks = len(preferred_dates)
                logger.info(
                    f"Scan complete — {len(preferred_slots)} slot(s) found "
                    f"across {len(preferred_dates)} preferred date(s)"
                )
                return preferred_slots

            if not fallback_dates:
                self.last_errors = errors[0]
                self.last_checks = len(preferred_dates)
                logger.info(
                    f"Scan complete — 0 slot(s) found across "
                    f"{len(preferred_dates)} date(s) (no fallback days configured)"
                )
                return []

            fallback_slots = await _scan_dates(fallback_dates)
            self.last_errors = errors[0]
            self.last_checks = total_dates
            logger.info(
                f"Scan complete — {len(fallback_slots)} fallback slot(s) found "
                f"across {total_dates} date(s) total "
                f"(0 preferred + {len(fallback_slots)} fallback)"
            )
            return fallback_slots

        finally:
            self._wait_for_calendar = original_wait  # type: ignore[method-assign]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_target_dates(self, days: list[str] | None = None) -> list[date]:
        """Dates from tomorrow through SCAN_WEEKS weeks that fall on *days*.
        Defaults to config.preferred_days when days is None."""
        if days is None:
            days = self.config.preferred_days
        today = date.today()
        end = today + timedelta(weeks=self.config.scan_weeks)
        result = []
        current = today + timedelta(days=1)
        while current <= end:
            if current.strftime("%A") in days:
                result.append(current)
            current += timedelta(days=1)
        return result

    async def _check_date(
        self, target_date: date, keep_page: bool = False
    ) -> list[AvailableSlot]:
        """
        Load the Tock search page for target_date, verify the day is
        available in the calendar, click it, then collect time slots.

        keep_page=True (sniper mode): reuses the existing page for this date
        (reload instead of full navigate) for speed and Cloudflare friendliness.
        """
        date_str = target_date.isoformat()
        url = (
            f"{BASE_URL}/{self.config.restaurant_slug}/search"
            f"?date={date_str}"
            f"&size={self.config.party_size}"
            f"&time={self.config.preferred_time}"
        )

        # Resolve page: reuse if keep_page and page is still open
        existing = self._sniper_pages.get(date_str) if keep_page else None
        if existing and not existing.is_closed():
            page = existing
            reusing = True
        else:
            page = await self.browser.new_page()
            if keep_page:
                self._sniper_pages[date_str] = page
            reusing = False

        try:
            if reusing:
                logger.debug(f"[check] {date_str} → reload (sniper page reuse)")
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            else:
                logger.debug(f"[check] {date_str} → {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Wait for calendar to render
            if not await self._wait_for_calendar(page, date_str):
                return []

            # Debug screenshot: capture once per poll cycle (first date only)
            if not self._screenshot_taken_this_poll:
                self._screenshot_taken_this_poll = True
                try:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = os.path.join(_SCREENSHOT_DIR, f"poll_{ts}_{date_str}.png")
                    await page.screenshot(path=path, full_page=True)
                    logger.info(f"[check] Debug screenshot saved: {path}")
                except Exception as e:
                    logger.debug(f"[check] Screenshot failed: {e}")

            # STRATEGY: Click the target day by number, then find slot buttons
            # using the same multi-selector fallback as --test-booking-flow.
            #
            # The Tock UI varies by restaurant — some use is-available class on
            # calendar days, others don't. Slot buttons may be Consumer-resultsListItem
            # or plain "Book" buttons with hashed CSS classes. We try all known
            # patterns and use the first that matches.

            # Click the target day in the calendar
            clicked = await self._click_day(page, target_date)
            if not clicked:
                logger.info(f"[check] {date_str} — could not click day in calendar")
                return []

            # Wait for the slot panel to render after clicking the day
            # (Tock needs time to load the slot results via React)
            await page.wait_for_timeout(2500)

            # Try multiple selectors for slot/booking buttons (same order as
            # --test-booking-flow). The first selector that matches wins.
            slot_selectors = [
                sel.get("available_slot_button"),          # button.Consumer-resultsListItem.is-available
                "button.Consumer-resultsListItem",         # without is-available class
                'button:visible:has-text("Book")',         # "Book" CTA (e.g. Benu css-dr2rn7)
                sel.get("book_now_button"),                # "Book now" button
                "button.SearchExperience-bookButton",      # alternative booking button
                "[data-testid='book-button']",             # test ID variant
            ]

            found_selector = None
            slot_count = 0
            for try_sel in slot_selectors:
                try:
                    count = await page.locator(try_sel).count()
                    if count > 0:
                        found_selector = try_sel
                        slot_count = count
                        logger.info(
                            f"[check] {date_str} — {count} slot(s) found via {try_sel!r}"
                        )
                        break
                except Exception:
                    continue

            if not found_selector:
                logger.debug(f"[check] {date_str} — no slots found with any selector")
                return []

            # Collect slots from the matched selector
            slots = await self._collect_slots_multi(page, target_date, found_selector)

            # Record each new slot in the tracker
            for slot in slots:
                self.tracker.record(slot.slot_date, slot.slot_time)

            return self._sort_by_preferred_time(slots)

        except Exception as e:
            logger.error(f"[check] Unexpected error for {date_str}: {e}")
            if keep_page and date_str in self._sniper_pages:
                # Drop broken page so next poll creates a fresh one
                del self._sniper_pages[date_str]
                try:
                    await page.close()
                except Exception:
                    pass
            return []
        finally:
            # Only close if we're not keeping this page across polls
            if not keep_page:
                await page.close()

    async def _wait_for_calendar(self, page: Page, date_str: str) -> bool:
        """Wait for the calendar container to appear. Logs selector failures."""
        key = "calendar_container"
        selector = sel.get(key)
        try:
            await page.wait_for_selector(selector, timeout=15000)
            return True
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
                f"  The calendar did not load for {date_str}.\n"
                f"  Possible causes:\n"
                f"    • Not logged in (session expired)\n"
                f"    • Tock redesigned the page — update src/selectors.py\n"
                f"    • Bot detection triggered — try HEADLESS=false\n"
                f"  Error: {e}"
            )
            return False

    async def _is_day_available(self, page: Page, target_date: date) -> bool:
        """Return True if target_date appears among the available day buttons."""
        key = "available_day_button"
        selector = sel.get(key)
        num_key = "day_number_span"
        num_selector = sel.get(num_key)

        # Debug: dump raw classes of ALL calendar day buttons so we can see
        # exactly what Tock renders (not just is-available ones).
        try:
            all_day_btns = await page.query_selector_all(
                "button.ConsumerCalendar-day.is-in-month"
            )
            if all_day_btns:
                class_samples = []
                for btn in all_day_btns[:5]:  # first 5 to keep logs manageable
                    cls = await btn.get_attribute("class") or ""
                    text = (await btn.text_content() or "").strip()
                    class_samples.append(f"day={text} classes=[{cls}]")
                logger.info(
                    f"[check] {target_date.isoformat()} calendar day button classes "
                    f"(first {len(class_samples)}):\n  "
                    + "\n  ".join(class_samples)
                )
            else:
                logger.info(
                    f"[check] {target_date.isoformat()} — no "
                    f"button.ConsumerCalendar-day.is-in-month found at all"
                )
        except Exception as e:
            logger.info(f"[check] {target_date.isoformat()} — class dump failed: {e}")

        try:
            day_buttons = await page.query_selector_all(selector)
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
            return False

        if not day_buttons:
            return False

        target_num = str(target_date.day)
        for btn in day_buttons:
            try:
                # Read the button's text content directly — the day number is the
                # button's full text. (Old approach used a child span.B2 which
                # changed to span.MuiTypography-root; text_content() is span-agnostic.)
                text = (await btn.text_content() or "").strip()
                if text == target_num:
                    return True
            except Exception:
                continue

        return False

    async def _click_day(self, page: Page, target_date: date) -> bool:
        """Click the calendar button for target_date. Returns True on success.

        Uses all_day_button (any in-month day) — NOT available_day_button —
        so we click days even when they lack the is-available class (e.g.
        Fuhuihua shows is-sold/is-disabled until the exact release moment).
        """
        key = "all_day_button"
        selector = sel.get(key)
        target_num = str(target_date.day)

        day_buttons = await page.query_selector_all(selector)
        for btn in day_buttons:
            try:
                text = (await btn.text_content() or "").strip()
                if text == target_num:
                    await btn.click()
                    logger.debug(
                        f"[check] Clicked day {target_num} for {target_date.isoformat()}"
                    )
                    return True
            except Exception:
                continue

        logger.warning(
            f"[check] Could not click day {target_num} for {target_date.isoformat()}\n"
            f"  SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
            f"  → Update src/selectors.py"
        )
        return False

    async def _collect_slots(
        self, page: Page, target_date: date
    ) -> list[AvailableSlot]:
        """Scrape all visible available time slots after a day is clicked.
        Uses the legacy Consumer-resultsListItem selector."""
        slot_selector = sel.get("available_slot_button")
        time_selector = sel.get("slot_time_text")

        try:
            slot_buttons = await page.query_selector_all(slot_selector)
        except Exception:
            return []

        slots: list[AvailableSlot] = []
        for btn in slot_buttons:
            try:
                span = await btn.query_selector(time_selector)
                if span:
                    time_text = (await span.text_content() or "").strip()
                    if time_text:
                        slots.append(
                            AvailableSlot(
                                slot_date=target_date,
                                slot_time=time_text,
                                day_of_week=target_date.strftime("%A"),
                            )
                        )
            except Exception:
                continue
        return slots

    async def _collect_slots_multi(
        self, page: Page, target_date: date, matched_selector: str
    ) -> list[AvailableSlot]:
        """Collect slots using whichever selector matched during detection.

        For Consumer-resultsListItem selectors, extracts time from a child span.
        For "Book" button selectors, extracts context from surrounding elements
        or falls back to a generic slot label.
        """
        import re

        slots: list[AvailableSlot] = []
        try:
            locator = page.locator(matched_selector)
            count = await locator.count()

            for i in range(count):
                el = locator.nth(i)
                try:
                    # Try to get time text from various sources
                    time_text = None

                    # Source 1: Child span with time class (legacy Tock)
                    time_selector = sel.get("slot_time_text")
                    time_span = el.locator(time_selector)
                    if await time_span.count() > 0:
                        time_text = (await time_span.first.text_content() or "").strip()

                    # Source 2: Look for time pattern in nearby text (parent/sibling)
                    if not time_text:
                        # Get the parent container's text for time context
                        parent = el.locator("..")
                        parent_text = (await parent.text_content() or "").strip()
                        # Match common time patterns: "5:00 PM", "8:00 PM", "17:00"
                        time_match = re.search(
                            r'\b(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))\b', parent_text
                        )
                        if time_match:
                            time_text = time_match.group(1)

                    # Source 3: Fall back to button text or slot number
                    if not time_text:
                        btn_text = (await el.text_content() or "").strip()
                        if btn_text and btn_text.lower() not in ("book", "book now"):
                            time_text = btn_text
                        else:
                            time_text = f"Slot {i + 1}"

                    slots.append(
                        AvailableSlot(
                            slot_date=target_date,
                            slot_time=time_text,
                            day_of_week=target_date.strftime("%A"),
                        )
                    )
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"[check] {target_date.isoformat()} — slot collection failed: {e}")

        if slots:
            logger.info(
                f"[check] {target_date.isoformat()} — {len(slots)} slot(s): "
                + ", ".join(s.slot_time for s in slots)
            )
        return slots

    def _sort_by_preferred_time(
        self, slots: list[AvailableSlot]
    ) -> list[AvailableSlot]:
        """Sort slots by absolute distance from config.preferred_time (closest first)."""
        try:
            pt = parse_time(self.config.preferred_time)
            pref_minutes = pt.hour * 60 + pt.minute
        except Exception:
            return slots

        def distance(slot: AvailableSlot) -> int:
            for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
                try:
                    t = datetime.strptime(slot.slot_time.strip().upper(), fmt)
                    return abs(t.hour * 60 + t.minute - pref_minutes)
                except ValueError:
                    continue
            return 9999

        return sorted(slots, key=distance)
