"""
Availability checker.

For each preferred-day date within the scan window, opens the Tock search
page for that date and collects all available time slots.

Each check opens its own Playwright page and closes it when done, so state
never bleeds between date checks.

Selector failures are logged with the exact key and selector string so
updates to src/selectors.py are straightforward.
"""

import asyncio
import glob as _glob
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from playwright.async_api import Page

import src.selectors as sel
from src.config import Config, parse_time
from src.tracker import SlotTracker

# Debug screenshot directories
_SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_screenshots")
_SCREENSHOT_ERROR_DIR = os.path.join(_SCREENSHOT_DIR, "errors")
os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
os.makedirs(_SCREENSHOT_ERROR_DIR, exist_ok=True)

# Normal screenshots: keep the most recent N, delete oldest when over limit.
# Error screenshots (saved to _SCREENSHOT_ERROR_DIR) are NEVER deleted.
MAX_DEBUG_SCREENSHOTS = 50

# Non-sniper skip cache TTL: 20 minutes.  Dates whose calendar day was not
# visible are skipped for this long before being retried.  This avoids
# spending ~15s per "day not visible" date on every normal poll cycle.
NORMAL_SKIP_TTL_SEC = 1200  # 20 minutes

logger = logging.getLogger(__name__)


def _prune_screenshots(directory: str, max_count: int = MAX_DEBUG_SCREENSHOTS) -> None:
    """Delete the oldest .png files in *directory* until at most max_count remain.

    Never touches subdirectories (i.e. the errors/ subfolder is untouched).
    Safe to call on non-existent or empty directories.
    """
    try:
        pattern = os.path.join(directory, "poll_*.png")
        files = sorted(_glob.glob(pattern), key=os.path.getmtime)
        excess = len(files) - max_count
        if excess > 0:
            for path in files[:excess]:
                try:
                    os.remove(path)
                    logger.debug(f"[check] Pruned old screenshot: {os.path.basename(path)}")
                except OSError:
                    pass
    except Exception as e:
        logger.debug(f"[check] Screenshot prune failed: {e}")

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
        # Sniper-mode skip cache: dates that failed to show target day in calendar.
        # Cleared when sniper pages are closed (new window).
        self._skip_dates: set[str] = set()
        self._skip_cache_enabled: bool = True
        # Normal-mode skip cache: date_str → monotonic timestamp when cached.
        # Persists across polls (TTL=NORMAL_SKIP_TTL_SEC) to avoid re-hitting
        # dates that are beyond the booking window every poll cycle.
        # Cleared when sniper mode activates (via clear_normal_skip_cache()).
        self._normal_skip_dates: dict[str, float] = {}
        # Track count of existing normal screenshots for rotation.
        # Populated from disk by refresh_screenshot_count() before sniper.
        self._screenshot_count: int = 0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def clear_skip_cache(self) -> None:
        """Clear the sniper-mode skip-date cache. Call at the start of each sniper poll."""
        self._skip_dates.clear()

    def _should_skip_date(self, date_str: str, skip_cache_enabled: bool) -> bool:
        """Return True if this date should be skipped based on sniper cache."""
        if not skip_cache_enabled:
            return False
        return date_str in self._skip_dates

    # ------------------------------------------------------------------
    # Normal-mode skip cache
    # ------------------------------------------------------------------

    def _add_to_normal_skip(self, date_str: str) -> None:
        """Cache *date_str* as not-visible-in-calendar for NORMAL_SKIP_TTL_SEC."""
        self._normal_skip_dates[date_str] = time.monotonic()

    def _should_skip_normal(self, date_str: str) -> bool:
        """Return True if *date_str* is in the normal skip cache and still fresh."""
        ts = self._normal_skip_dates.get(date_str)
        if ts is None:
            return False
        if time.monotonic() - ts > NORMAL_SKIP_TTL_SEC:
            # Expired — evict and retry
            del self._normal_skip_dates[date_str]
            return False
        return True

    def clear_normal_skip_cache(self) -> None:
        """Clear the normal-mode skip cache. Called when sniper mode activates."""
        self._normal_skip_dates.clear()
        logger.debug("[check] Normal skip cache cleared (sniper mode starting).")

    # ------------------------------------------------------------------
    # Screenshot count management
    # ------------------------------------------------------------------

    def refresh_screenshot_count(self) -> None:
        """Count existing normal screenshots from disk.

        Call before sniper mode so rotation stays accurate even when old
        screenshots from previous bot runs are already on disk.
        """
        try:
            pattern = os.path.join(_SCREENSHOT_DIR, "poll_*.png")
            self._screenshot_count = len(_glob.glob(pattern))
            logger.debug(
                f"[check] Screenshot count refreshed: {self._screenshot_count} "
                f"existing file(s) in debug_screenshots/"
            )
        except Exception as e:
            logger.debug(f"[check] Screenshot count refresh failed: {e}")

    def get_warm_page(self, date_str: str) -> "Page | None":
        """Return the warm sniper page for a date, or None if unavailable."""
        page = self._sniper_pages.get(date_str)
        if page and not page.is_closed():
            return page
        return None

    async def close_sniper_pages(self) -> None:
        """Close all pages kept open during sniper mode. Call when window ends."""
        for page in list(self._sniper_pages.values()):
            try:
                await page.close()
            except Exception:
                pass
        self._sniper_pages.clear()
        self._skip_dates.clear()
        logger.debug("[check] Sniper pages closed.")

    async def check_all(
        self,
        concurrent: bool = False,
        keep_pages: bool = False,
        sniper_window_age_sec: float = 0,
    ) -> list[AvailableSlot]:
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

        # Skip cache only active during sniper mode, and only after first 5 min
        # (release may happen mid-window, so don't cache "not visible" early)
        self._skip_cache_enabled = keep_pages and sniper_window_age_sec > 300
        # Always clear stale entries at poll start so we retry dates that
        # failed last poll
        self._skip_dates.clear()

        # ── Two-phase sniper: Phase 1 (pre-release) ──────────────────────────
        # The sniper window starts 60s before the actual release time.
        # Scanning calendars before release produces only timeouts and error
        # counts. Return immediately; Phase 2 (aggressive scan) begins at 60s.
        if keep_pages and sniper_window_age_sec < 60.0:
            self.last_errors = 0
            self.last_checks = 0
            logger.debug(
                f"[check] Pre-release phase (age={sniper_window_age_sec:.1f}s) — "
                "skipping calendar scan until release"
            )
            return []

        errors: list[int] = [0]   # mutable counter accessible in closure

        # Patch _wait_for_calendar to count failures for this poll
        original_wait = self._wait_for_calendar
        async def _counting_wait(page, date_str: str, **kwargs) -> bool:
            ok = await original_wait(page, date_str, **kwargs)
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
                    # In sniper mode, create an abort event: the first date to find
                    # slots signals others to stop early via abort_event.set().
                    abort_evt = _asyncio.Event() if keep_pages else None
                    results = await _asyncio.gather(
                        *[
                            self._check_date(d, keep_page=keep_pages, abort_event=abort_evt)
                            for d in dates
                        ],
                        return_exceptions=True,
                    )
                    slots: list[AvailableSlot] = []
                    for i, r in enumerate(results):
                        if isinstance(r, BaseException):
                            errors[0] += 1
                            logger.error(
                                f"[check] Concurrent check failed for "
                                f"{dates[i].isoformat()}: {r}"
                            )
                        elif isinstance(r, list):
                            slots.extend(r)
                    return slots
                else:
                    slots = []
                    for d in dates:
                        result = await self._check_date(d, keep_page=keep_pages)
                        slots.extend(result)
                        if result and keep_pages:
                            logger.info(
                                "[check] First slot found — stopping sequential scan early"
                            )
                            break
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
        self, target_date: date, keep_page: bool = False,
        abort_event: asyncio.Event | None = None,
    ) -> list[AvailableSlot]:
        """
        Load the Tock search page for target_date, verify the day is
        available in the calendar, click it, then collect time slots.

        keep_page=True (sniper mode): reuses the existing page for this date
        (reload instead of full navigate) for speed and Cloudflare friendliness.
        """
        date_str = target_date.isoformat()

        # Sniper-mode skip: date failed last poll — skip until pages close.
        if keep_page and self._should_skip_date(date_str, skip_cache_enabled=self._skip_cache_enabled):
            logger.debug(f"[check] {date_str} — skipped (sniper cache: not in calendar last poll)")
            return []

        # Normal-mode skip: date was not visible in calendar on a recent poll.
        # Skip for NORMAL_SKIP_TTL_SEC (20 min) to avoid ~15s calendar timeout
        # per date per poll cycle when dates are beyond the booking window.
        if not keep_page and self._should_skip_normal(date_str):
            logger.debug(f"[check] {date_str} — skipped (normal cache: not in calendar recently)")
            return []

        # Sniper interrupt: another date already found slots — skip immediately
        if abort_event is not None and abort_event.is_set():
            logger.debug(
                f"[check] {date_str} — skipped "
                "(first slot already found on another date)"
            )
            return []

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

        nav_timeout = 10000 if keep_page else 30000
        try:
            if reusing:
                logger.debug(f"[check] {date_str} → reload (sniper page reuse)")
                await page.reload(wait_until="domcontentloaded", timeout=nav_timeout)
            else:
                logger.debug(f"[check] {date_str} → {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)

            # Check abort before expensive calendar work
            if abort_event is not None and abort_event.is_set():
                return []

            # Wait for calendar to render (shorter timeout in sniper mode)
            cal_timeout = 5000 if keep_page else 15000
            if not await self._wait_for_calendar(page, date_str, timeout=cal_timeout):
                return []

            # Debug screenshot: only when enabled and not in sniper mode (too slow)
            if (
                self.config.debug_screenshots
                and not keep_page  # skip during sniper — ~200ms overhead per poll
                and not self._screenshot_taken_this_poll
            ):
                self._screenshot_taken_this_poll = True
                try:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = os.path.join(_SCREENSHOT_DIR, f"poll_{ts}_{date_str}.png")
                    await page.screenshot(path=path, full_page=True)
                    self._screenshot_count += 1
                    logger.info(f"[check] Debug screenshot saved: {path}")
                    # Rotate: keep only the most recent MAX_DEBUG_SCREENSHOTS
                    if self._screenshot_count > MAX_DEBUG_SCREENSHOTS:
                        _prune_screenshots(_SCREENSHOT_DIR, MAX_DEBUG_SCREENSHOTS)
                        self._screenshot_count = MAX_DEBUG_SCREENSHOTS
                except Exception as e:
                    logger.debug(f"[check] Screenshot failed: {e}")

            # STRATEGY: Click the target day by number, then find slot buttons
            # using the same multi-selector fallback as --test-booking-flow.
            #
            # The Tock UI varies by restaurant — some use is-available class on
            # calendar days, others don't. Slot buttons may be Consumer-resultsListItem
            # or plain "Book" buttons with hashed CSS classes. We try all known
            # patterns and use the first that matches.

            if abort_event is not None and abort_event.is_set():
                return []

            # Click the target day in the calendar
            clicked = await self._click_day(page, target_date)
            if not clicked:
                logger.info(f"[check] {date_str} — could not click day in calendar")
                if keep_page:
                    # Sniper mode: add to per-window skip set
                    self._skip_dates.add(date_str)
                else:
                    # Normal mode: cache for NORMAL_SKIP_TTL_SEC to avoid
                    # wasting time re-checking dates beyond the booking window
                    self._add_to_normal_skip(date_str)
                    logger.debug(
                        f"[check] {date_str} cached in normal skip (TTL {NORMAL_SKIP_TTL_SEC}s)"
                    )
                return []

            # Try multiple selectors for slot/booking buttons.
            # Centralized in selectors.py so checker and booker stay in sync.
            from src.selectors import get_slot_button_selectors
            slot_selectors = get_slot_button_selectors()

            # Wait reactively for any slot-like element instead of blind sleep.
            # Short timeout (500ms sniper, 2500ms normal) — move on if nothing appears.
            slot_timeout = 500 if keep_page else 2500
            try:
                await page.wait_for_selector(slot_selectors[0], timeout=slot_timeout)
            except Exception:
                pass  # no slots visible yet — proceed to multi-selector check

            # Split selectors: CSS-compatible ones go through fast page.evaluate(),
            # Playwright-specific ones (:has-text, :text, :visible) fall back to locator API
            css_selectors = []
            pw_selectors = []
            for s in slot_selectors:
                if any(pw in s for pw in [':has-text', ':text(', ':visible']):
                    pw_selectors.append(s)
                else:
                    css_selectors.append(s)

            found_selector = None
            slot_count = 0

            # Fast path: batch CSS selectors in one evaluate() call
            if css_selectors:
                detect_js = """
                (selectors) => {
                    for (let i = 0; i < selectors.length; i++) {
                        try {
                            const els = document.querySelectorAll(selectors[i]);
                            if (els.length > 0) return { index: i, count: els.length };
                        } catch(e) { continue; }
                    }
                    return { index: -1, count: 0 };
                }
                """
                detect_result = await page.evaluate(detect_js, css_selectors)
                if detect_result["index"] >= 0:
                    found_selector = css_selectors[detect_result["index"]]
                    slot_count = detect_result["count"]

            # Slow path: Playwright-specific selectors (only if fast path missed)
            if not found_selector:
                for try_sel in pw_selectors:
                    try:
                        count = await page.locator(try_sel).count()
                        if count > 0:
                            found_selector = try_sel
                            slot_count = count
                            break
                    except Exception:
                        continue

            if found_selector:
                logger.info(
                    f"[check] {date_str} — {slot_count} slot(s) found via {found_selector!r}"
                )

            if not found_selector:
                logger.debug(f"[check] {date_str} — no slots found with any selector")
                return []

            # Collect slots from the matched selector
            slots = await self._collect_slots_multi(page, target_date, found_selector)

            # Record each new slot in the tracker
            # Sniper mode defers disk I/O; monitor.poll() calls flush_deferred() after
            for slot in slots:
                if keep_page:
                    self.tracker.record_deferred(slot.slot_date, slot.slot_time)
                else:
                    self.tracker.record(slot.slot_date, slot.slot_time)

            sorted_slots = self._sort_by_preferred_time(slots)
            if sorted_slots and abort_event is not None:
                abort_event.set()
                logger.info(
                    f"[check] {date_str} — first slot found, "
                    "abort signaled to remaining tasks"
                )
            return sorted_slots

        except Exception as e:
            logger.error(f"[check] Unexpected error for {date_str}: {e}")
            if self.config.debug_screenshots:
                await self._save_error_screenshot(page, date_str, "unexpected_error")
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

    async def _save_error_screenshot(self, page: Page, date_str: str, label: str) -> None:
        """Save a screenshot to the errors/ subfolder. Never deleted automatically."""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"error_{ts}_{label}_{date_str}.png"
            path = os.path.join(_SCREENSHOT_ERROR_DIR, filename)
            await page.screenshot(path=path, full_page=True)
            logger.info(f"[check] Error screenshot saved: errors/{filename}")
        except Exception as e:
            logger.debug(f"[check] Error screenshot failed: {e}")

    async def _wait_for_calendar(self, page: Page, date_str: str, timeout: int = 15000) -> bool:
        """Wait for the calendar container to appear. Logs selector failures."""
        key = "calendar_container"
        selector = sel.get(key)
        try:
            await page.wait_for_selector(selector, timeout=timeout)
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
            # Save error screenshot for diagnosis (never rotated/deleted)
            if self.config.debug_screenshots:
                await self._save_error_screenshot(page, date_str, "cal_load_fail")
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
        """Click the calendar button for target_date using a single evaluate() call.

        Uses all_day_button (any in-month day) — NOT available_day_button —
        so we click days even when they lack the is-available class (e.g.
        Fuhuihua shows is-sold/is-disabled until the exact release moment).

        No pagination — if the day isn't in the visible calendar, it's
        beyond the booking window and we skip it instantly.
        """
        selector = sel.get("all_day_button")
        target_num = str(target_date.day)

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
            logger.info(
                f"[check] Clicked day {target_num} for {target_date.isoformat()}"
            )
            return True

        logger.info(
            f"[check] Day {target_num} not visible in calendar for "
            f"{target_date.isoformat()} (likely not yet released)"
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
