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
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from playwright.async_api import Page

import src.selectors as sel
from src.config import Config, parse_time
from src.tracker import SlotTracker

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

            # Wait for day buttons to appear inside the calendar (selector-based,
            # no fixed sleep — moves on as soon as buttons are ready)
            try:
                await page.wait_for_selector(
                    sel.get("available_day_button"), timeout=5000
                )
            except Exception:
                pass  # no available days this month; _is_day_available will return False

            # Is our target day marked as available?
            if not await self._is_day_available(page, target_date):
                logger.debug(f"[check] {date_str} — day not available in calendar")
                return []

            # Click the day to reveal its time slots
            if not await self._click_day(page, target_date):
                return []

            # Wait for slot buttons to appear (selector-based, not a fixed sleep)
            try:
                await page.wait_for_selector(
                    sel.get("available_slot_button"), timeout=3000
                )
            except Exception:
                pass  # no slots visible yet; _collect_slots will return []

            # Collect and sort time slots
            slots = await self._collect_slots(page, target_date)

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
        """Click the calendar button for target_date. Returns True on success."""
        key = "available_day_button"
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
        """Scrape all visible available time slots after a day is clicked."""
        slot_key = "available_slot_button"
        slot_selector = sel.get(slot_key)
        time_key = "slot_time_text"
        time_selector = sel.get(time_key)

        try:
            slot_buttons = await page.query_selector_all(slot_selector)
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{slot_key}'  selector={slot_selector!r}\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
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

        if not slots:
            logger.debug(
                f"[check] {target_date.isoformat()} — day available but no time slots "
                f"found (SELECTOR_FAILED: key='{slot_key}'  selector={slot_selector!r})"
            )

        logger.debug(
            f"[check] {target_date.isoformat()} — {len(slots)} time slot(s): "
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
