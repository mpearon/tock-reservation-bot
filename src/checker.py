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
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from typing import TYPE_CHECKING

from playwright.async_api import Locator, Page

import src.selectors as sel
from src import selector_metrics
from src.selectors import is_playwright_selector
from src.config import Config, parse_time
from src.tracker import SlotTracker

if TYPE_CHECKING:
    from src.notifier import Notifier

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

# Cloudflare challenge alert threshold. Strictly greater-than: a rate of
# exactly 5% (e.g. 1-in-20) does NOT alert; 6%+ does. In a production
# 7-page prewarm window the smallest non-zero rate is 1/7 ≈ 14% so the
# strict-vs-equal distinction is academic, but the constant clarifies
# intent for future maintainers.
_CF_CHALLENGE_ALERT_THRESHOLD = 0.05

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


# Phase B2.2: DOM-based Cloudflare challenge detection. Returns true when
# the page contains any of the documented CF challenge markers — used as a
# second signal alongside URL-based detection so challenges that don't
# change the URL still get caught.
_CF_DOM_DETECT_JS = r"""
() => {
  // Marker iframes / widgets / interstitial spinners
  const markers = document.querySelector(
    'iframe[src*="challenges.cloudflare.com"], '
    + '.cf-turnstile, '
    + '#cf-please-wait, '
    + '#cf-spinner-please-wait'
  );
  if (markers) return true;
  // Interstitial text — must be visible (innerText reflects rendered text)
  const phraseRe = /verify you are human|just a moment|checking your browser/i;
  const candidates = document.querySelectorAll('h1, h2, p, div, span');
  for (const el of candidates) {
    const t = (el.innerText || '');
    if (phraseRe.test(t)) return true;
  }
  return false;
}
"""


# Single-evaluate slot-collection JS (Phase B1.3). Called from
# _collect_slots_multi. Container scoping + 5-source extraction in one
# round-trip, replacing the per-button locator chain. Returns
#   { container_used, button_count, slots: [{time, source}, ...] }
# where time is null when no source produced a parseable time string —
# the wrapper drops null entries (Apr 17 lesson: never fabricate "Slot N").
_COLLECT_SLOTS_JS = r"""
(args) => {
  const {
    containerSelector,
    matchedSelector,
    slotTimeTextSelector,
    timeRegex,
    timeFlags,
  } = args;
  const re = new RegExp(timeRegex, timeFlags || 'i');

  let root = document;
  let containerUsed = false;
  if (containerSelector) {
    const containerEl = document.querySelector(containerSelector);
    if (containerEl) {
      root = containerEl;
      containerUsed = true;
    }
  }

  const buttons = Array.from(root.querySelectorAll(matchedSelector));
  const slots = [];

  const cleanTime = (s) => s.replace(/\s+/g, ' ').trim();

  for (const btn of buttons) {
    let time = null;
    let source = -1;

    // Source 1: child span matching slot_time_text selector
    if (slotTimeTextSelector) {
      try {
        const span = btn.querySelector(slotTimeTextSelector);
        if (span) {
          const t = (span.textContent || span.innerText || '').trim();
          if (t) { time = t; source = 1; }
        }
      } catch (_) { /* invalid selector → skip */ }
    }

    // Source 2: parent.text_content
    if (time === null) {
      const parent = btn.parentElement;
      if (parent) {
        const ptext = (parent.textContent || parent.innerText || '').trim();
        const m = ptext.match(re);
        if (m) { time = cleanTime(m[1]); source = 2; }
      }
    }

    // Source 3: ancestors up to 3 levels above the parent
    if (time === null) {
      let anc = btn.parentElement;
      for (let i = 0; i < 3 && anc; i++) {
        anc = anc.parentElement;
        if (!anc) break;
        const atext = (anc.textContent || anc.innerText || '').trim();
        const m = atext.match(re);
        if (m) { time = cleanTime(m[1]); source = 3; break; }
      }
    }

    // Source 4: aria-label / title attributes
    if (time === null) {
      for (const attr of ['aria-label', 'title']) {
        const v = btn.getAttribute(attr);
        if (!v) continue;
        const m = v.match(re);
        if (m) { time = cleanTime(m[1]); source = 4; break; }
      }
    }

    // Source 5: button's own text content (only if not a bare 'Book')
    if (time === null) {
      const btext = (btn.textContent || btn.innerText || '').trim();
      const lower = btext.toLowerCase();
      if (btext && lower !== 'book' && lower !== 'book now') {
        const m = btext.match(re);
        if (m) { time = cleanTime(m[1]); source = 5; }
      }
    }

    slots.push({ time, source });
  }

  return {
    container_used: containerUsed,
    button_count: buttons.length,
    slots,
  };
}
"""


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
        # Normal-mode fast-path handoff: when retain_found_pages=True, the page
        # that found slots is parked here for one cycle so the booker can click
        # the already-visible slot button instead of re-navigating. Drained by
        # the monitor immediately after check_all and before book_best_slot_race.
        self._handoff_pages: dict[str, "Page"] = {}  # date_str -> open Page
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
        # Telemetry from the most recent prewarm_target_dates() call.
        # Set to 0 here so they're always present regardless of whether
        # prewarm has run yet.
        self._last_prewarm_cf_challenges: int = 0
        self._last_prewarm_attempts: int = 0
        # Sniper-phase CF challenge counters. Reset per sniper window via
        # close_sniper_pages(). Tracked separately from prewarm counters
        # because they signal different operational risks.
        self._sniper_cf_challenges: int = 0
        self._sniper_cf_attempts: int = 0
        # B3.2 fast-path (USE_CALENDAR_REPLAY=true). Lazy-initialized on
        # first call to check_all when the flag is enabled. Auto-invalidated
        # on fetch failure so the next poll re-initializes.
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from src.calendar_replay import CalendarReplaySession  # noqa: F401
        self._replay_session = None  # type: ignore[var-annotated]
        # Codex MEDIUM 4: circuit breaker. Track replay failures; once we
        # exceed _REPLAY_FAILURE_THRESHOLD consecutive failures, stop
        # attempting replay until the next sniper window starts (i.e.,
        # close_replay_session() resets us). Avoids hammering Tock with
        # known-broken auth/headers while the legacy path runs each poll.
        self._replay_failure_count: int = 0
        self._replay_circuit_open: bool = False

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    # Circuit-breaker threshold: after this many consecutive replay
    # failures, stop attempting replay until close_replay_session()
    # resets us (typically at sniper-window end). 3 = "give it a few
    # tries in case of transient network blip" without "every poll
    # incurs init+fetch cost on a known-broken auth path".
    _REPLAY_FAILURE_THRESHOLD: int = 3

    async def _try_calendar_replay(
        self, sniper_mode: bool
    ) -> "list[AvailableSlot] | None":
        """B3.2 fast-path: fetch + parse Tock's calendar protobuf via
        in-browser fetch() with replayed SPA headers.

        Returns the list of available slots filtered to preferred + fallback
        dates, OR None if the path cannot be used this poll (session not yet
        initialized AND init failed, or fetch returned None, or parse failed).
        Caller falls back to the legacy per-date scan on None.

        Lazy-initializes `self._replay_session` on first call OR after a
        prior fetch failed (consecutive_failures > 0). Re-uses the session
        across polls otherwise — the captured headers are reused indefinitely
        until they expire (JWT exp is 2027 per recon trace; sniper window
        is ~11 min, so expiry is not a real concern within a window).
        """
        from src.calendar_replay import (
            initialize_replay_session, fetch_calendar,
            parse_available_slots, close_session, cap_slots_per_date,
        )

        # Codex MEDIUM 4: circuit breaker — if we've failed enough times
        # this sniper window, stop trying replay until close_replay_session
        # resets the breaker (typically at window end).
        if self._replay_circuit_open:
            return None

        # Compute the union of dates we care about (preferred + fallback)
        target_dates = list(self._get_target_dates(
            self.config.preferred_days, sniper_mode=sniper_mode
        ))
        target_dates += list(self._get_target_dates(
            self.config.fallback_days, sniper_mode=sniper_mode
        ))
        if not target_dates:
            return []

        # Ensure session is alive
        sess = self._replay_session
        if sess is None or sess.consecutive_failures >= 1:
            if sess is not None:
                # Stale; close and re-init
                await close_session(sess)
                self._replay_session = None
            # Use the first preferred date for navigation; the URL only
            # affects the initial page load — the captured calendar/full
            # call returns ALL available dates regardless.
            init_date = target_dates[0]
            preferred_time = getattr(self.config, "preferred_time", "17:00")
            party_size = getattr(self.config, "party_size", 2)
            sess = await initialize_replay_session(
                self.browser,
                self.config.restaurant_slug,
                init_date,
                party_size=party_size,
                preferred_time=preferred_time,
            )
            if sess is None:
                logger.warning(
                    "[check-replay] initialize_replay_session returned None — "
                    "calendar/full XHR did not fire within timeout (likely CF "
                    "challenge or login expired)"
                )
                self._replay_failure_count += 1
                if self._replay_failure_count >= self._REPLAY_FAILURE_THRESHOLD:
                    self._replay_circuit_open = True
                    logger.error(
                        f"[check-replay] CIRCUIT OPEN after "
                        f"{self._replay_failure_count} consecutive failures. "
                        "Disabling replay path until close_replay_session() "
                        "is called (typically at sniper window end)."
                    )
                return None
            self._replay_session = sess

        # Fetch + parse
        body = await fetch_calendar(sess)
        if body is None:
            self._replay_failure_count += 1
            if self._replay_failure_count >= self._REPLAY_FAILURE_THRESHOLD:
                self._replay_circuit_open = True
                logger.error(
                    f"[check-replay] CIRCUIT OPEN after "
                    f"{self._replay_failure_count} consecutive failures."
                )
            return None
        try:
            slots = parse_available_slots(body, target_dates)
        except Exception as e:
            logger.error(
                f"[check-replay] parse_available_slots raised: "
                f"{type(e).__name__}: {e}"
            )
            self._replay_failure_count += 1
            return None
        # Codex HIGH 1: cap slots before returning so book_best_slot_race
        # doesn't fan out into 100+ concurrent booking pages on a calendar
        # response that includes the full 14-day calendar.
        # Default cap=5 covers the typical 5-6 anchor times per date
        # (Tock's protobuf only encodes anchors; UI interpolates 15-min
        # variants). Operator can override via REPLAY_PER_DATE_CAP env.
        slots = cap_slots_per_date(
            slots,
            preferred_time=getattr(self.config, "preferred_time", "17:00"),
            per_date_cap=getattr(self.config, "replay_per_date_cap", 5),
        )
        # Success — reset failure count
        self._replay_failure_count = 0
        return slots

    async def close_replay_session(self) -> None:
        """Close the replay session (best-effort). Called on shutdown
        OR when sniper window ends (so the next window starts fresh).

        Also resets the circuit-breaker so the next window can attempt
        replay again (Codex MEDIUM 4)."""
        from src.calendar_replay import close_session
        sess = self._replay_session
        self._replay_session = None
        # Reset circuit-breaker state for the next sniper window
        self._replay_failure_count = 0
        self._replay_circuit_open = False
        await close_session(sess)

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

    def pop_warm_page(self, date_str: str) -> "Page | None":
        """Remove and return the warm page for a date — ownership transfers
        to the caller.

        After Phase A+2's race-all-slots change, the booker may navigate
        the warm page to a checkout/error URL. If checker still owned it,
        the next sniper poll's reload() would reload that wrong URL and
        poison the date. Codex pass 2 caught this; pop_warm_page transfers
        ownership atomically so the booker is responsible for closing or
        re-parking the page.
        """
        page = self._sniper_pages.pop(date_str, None)
        if page and page.is_closed():
            return None
        return page

    async def close_sniper_pages(self) -> None:
        """Close all pages kept open during sniper mode. Call when window ends."""
        for page in list(self._sniper_pages.values()):
            try:
                await page.close()
            except Exception:
                pass
        self._sniper_pages.clear()
        self._skip_dates.clear()
        self._sniper_cf_challenges = 0
        self._sniper_cf_attempts = 0
        logger.debug("[check] Sniper pages closed.")

    def pop_handoff_page(self, date_str: str) -> "Page | None":
        """Remove and return the normal-mode handoff page for *date_str*.

        Mirrors pop_warm_page: ownership transfers to the caller. Closed pages
        are dropped (returned as None) so a stale entry can't poison the next
        poll cycle. Used by the monitor to drain the booker's warm_pages dict
        in normal-mode fast-path booking.
        """
        page = self._handoff_pages.pop(date_str, None)
        if page is None:
            return None
        try:
            if page.is_closed():
                return None
        except Exception:
            return None
        return page

    def _maybe_create_xhr_recorder(self, page, target_date: date):
        """Phase B3.2 telemetry first pass. When event_driven_detection
        is enabled, return a fresh XhrTelemetryRecorder attached to
        `page` so the caller can observe matching XHRs during the
        day-click. Returns None when disabled."""
        if not getattr(self.config, "event_driven_detection", False):
            return None
        from src.xhr_telemetry import XhrTelemetryRecorder, DEFAULT_LOG_PATH
        recorder = XhrTelemetryRecorder(
            url_pattern=getattr(self.config, "event_driven_url_pattern", "") or "",
            log_path=DEFAULT_LOG_PATH,
            target_date=target_date,
        )
        try:
            recorder.attach(page)
        except Exception as e:
            logger.debug(
                f"[xhr-telemetry] attach failed: {type(e).__name__}: {e}"
            )
            return None
        return recorder

    async def close_handoff_pages(self) -> None:
        """Close any remaining normal-mode handoff pages and clear the dict.

        Defensive cleanup — under normal flow the monitor drains the dict via
        pop_handoff_page() and the booker closes each page. This method handles
        the edge cases (early returns, exceptions) so pages never leak across
        polls.
        """
        for page in list(self._handoff_pages.values()):
            try:
                await page.close()
            except Exception:
                pass
        self._handoff_pages.clear()

    @staticmethod
    def _is_cloudflare_challenge_page(page: Page) -> bool:
        """Sync URL-only Cloudflare check (cheap, non-blocking).

        Detection signals (any one is sufficient):
          - URL contains 'challenge' (typical CF redirect path)
          - URL contains '__cf_chl' (CF challenge query param)

        Returns False on any error (including non-string page.url in tests).
        Most callers should prefer `is_cloudflare_challenge_page` (async)
        which adds a DOM check for challenges that don't change the URL
        — Phase B2.2.
        """
        try:
            raw = page.url
            if not isinstance(raw, str):
                return False
            url = raw.lower()
        except Exception:
            return False
        if "challenge" in url:
            return True
        if "__cf_chl" in url:
            return True
        return False

    async def is_cloudflare_challenge_page(self, page: Page) -> bool:
        """Combined URL + DOM Cloudflare check (Phase B2.2).

        Returns True if either:
          - the URL signals a CF challenge (sync helper above), OR
          - the page DOM contains a CF iframe / Turnstile widget /
            "verify you are human" interstitial text

        DOM check is one `page.evaluate` call (cheap). On any DOM-check
        exception (page closed, exec context destroyed), falls back to
        the URL signal — never raises out of detection.
        """
        if self._is_cloudflare_challenge_page(page):
            return True
        try:
            return bool(await page.evaluate(_CF_DOM_DETECT_JS))
        except Exception as e:
            logger.debug(
                f"[cf-detect] DOM check raised "
                f"{type(e).__name__}: {e}; falling back to URL-only result"
            )
            return False

    async def prewarm_target_dates(
        self,
        target_dates: list[date],
        stagger_sec: float = 30.0,
        notifier: "Notifier | None" = None,
    ) -> None:
        """Open one Playwright page per target_date, parked at CALENDAR_LOADED.

        Pages are stored in self._sniper_pages so the existing sniper-poll path
        picks them up. The first poll after the sniper window opens uses
        page.reload() (cached), saving ~1-2s vs. a fresh page.goto().

        Pages are opened ONE AT A TIME with stagger_sec between starts to
        keep the prewarm gentle on Cloudflare/Turnstile (vs. opening all
        N pages concurrently). Failures on individual dates are logged but
        do not abort the rest.

        CF challenge detection: if a navigated page's URL looks like a
        Cloudflare/Turnstile challenge, the page is NOT parked in
        _sniper_pages — the sniper-poll falls back to a fresh goto() for
        that date. Challenge count is recorded in _last_prewarm_cf_challenges;
        if the rate exceeds 5%, a Discord alert fires via notifier.
        """
        cf_challenges = 0
        attempted = 0

        for i, target_date in enumerate(target_dates):
            date_str = target_date.isoformat()
            url = (
                f"{BASE_URL}/{self.config.restaurant_slug}/search"
                f"?date={date_str}"
                f"&size={self.config.party_size}"
                f"&time={self.config.preferred_time}"
            )
            attempted += 1
            page = None
            try:
                page = await self.browser.new_page()
                logger.info(f"[prewarm] {date_str} → {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)

                # Detect CF challenge BEFORE waiting for calendar — a challenged
                # page never renders calendar_container, so the wait would just
                # time out wastefully. Skip parking; sniper-poll will fall back
                # to a fresh goto() at release time.
                # B2.2: combined URL+DOM check catches challenges that don't
                # change the URL (interstitial overlays, Turnstile widgets).
                if await self.is_cloudflare_challenge_page(page):
                    cf_challenges += 1
                    logger.warning(
                        f"[prewarm] {date_str} hit CF challenge "
                        f"(url={page.url}); not parking this page"
                    )
                    continue  # finally block closes the page

                # Park at CALENDAR_LOADED (calendar widget rendered)
                await page.wait_for_selector(
                    sel.get("calendar_container"), timeout=10000
                )
                # If a previous prewarm parked a page for this date (e.g. a
                # schedule re-aim or a second sniper window in the same
                # process), close it before overwriting. Otherwise the old
                # Page object leaks — never reachable for cleanup since
                # close_sniper_pages() iterates the current dict.
                old = self._sniper_pages.pop(date_str, None)
                if old is not None:
                    try:
                        await old.close()
                    except Exception:
                        pass
                self._sniper_pages[date_str] = page
                page = None  # ownership transferred to _sniper_pages — don't close in finally
                logger.info(
                    f"[prewarm] {date_str} parked at CALENDAR_LOADED"
                )
            except Exception as e:
                logger.warning(
                    f"[prewarm] {date_str} failed: {type(e).__name__}: {e}"
                )
                # Don't store the failed page — sniper-poll will fall back to
                # fresh goto() for this date
            finally:
                # Close any page we still own (failure path or CF challenge path)
                # so it doesn't leak across release windows. On the success path
                # `page` was set to None after handing ownership to _sniper_pages.
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass

            # Stagger between page opens (skip after the last one)
            if i < len(target_dates) - 1 and stagger_sec > 0:
                await asyncio.sleep(stagger_sec)

        # Expose telemetry on the instance for tests / introspection
        self._last_prewarm_cf_challenges = cf_challenges
        self._last_prewarm_attempts = attempted

        # Alert via Discord if rate exceeded threshold
        if attempted > 0 and notifier is not None:
            rate = cf_challenges / attempted
            if rate > _CF_CHALLENGE_ALERT_THRESHOLD:
                try:
                    notifier.cf_challenge_warning(
                        rate=rate, count=cf_challenges, phase="prewarm"
                    )
                except Exception as e:
                    logger.warning(
                        f"[prewarm] cf_challenge_warning failed (alert lost): {e}"
                    )

    async def check_all(
        self,
        concurrent: bool = False,
        keep_pages: bool = False,
        sniper_window_age_sec: float = 0,
        bypass_normal_skip: bool = False,
        notifier: "Notifier | None" = None,
        stop_on_first_slot: bool = False,
        retain_found_pages: bool = False,
    ) -> list[AvailableSlot]:
        """
        Scan for available slots in two phases:

        Phase 1 — preferred_days (e.g. Fri/Sat/Sun): checked first. If any
          slots found, return them immediately without scanning fallback days.

        Phase 2 — fallback_days (e.g. Mon–Thu): only scanned when Phase 1
          finds nothing.

        concurrent=False (default): sequential per date — safe from Cloudflare.
        concurrent=True: parallel per date — ~4× faster, 1% error rate at 14 dates.

        stop_on_first_slot=True (normal-mode fast path): in sequential mode,
          break out of the date loop the moment any slot is found. Combined
          with retain_found_pages, lets the booker click the already-visible
          slot button without re-navigating — closes the latency window between
          detection and booking that was costing slots in normal-mode polls.

        retain_found_pages=True (normal-mode fast path): when keep_pages is
          False AND a date yields slots, that date's live page is parked in
          self._handoff_pages instead of being closed. The monitor drains it
          via pop_handoff_page() and hands it to the booker as warm_pages.
          Independent of keep_pages so this fast path doesn't reuse the sniper
          scan-horizon, skip-cache, or pre-release scaffolding.

        After each call, self.last_errors / self.last_checks reflects the
        calendar load error rate for this poll — monitor.py uses this to
        adaptively switch between concurrent and sequential modes.
        """
        import asyncio as _asyncio

        # Defensive cleanup: any stale handoff pages from a prior cycle (e.g.
        # an exception bypassed the monitor's drain) get closed before we open
        # new ones. Costs a single dict iteration per poll.
        if self._handoff_pages:
            await self.close_handoff_pages()

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
            # B3.2 fast-path: SPA-header replay (76× detection speedup).
            # When enabled, fetch the entire calendar via one in-browser
            # fetch() call (~160ms) instead of N concurrent page reloads.
            # On any failure (None body, parse exception, session not
            # initialized): set last_errors=1 to mark the path failed and
            # fall through to the existing per-date scan.
            if getattr(self.config, "use_calendar_replay", False):
                replay_slots = await self._try_calendar_replay(
                    sniper_mode=keep_pages
                )
                if replay_slots is not None:
                    # Success — track + return without running the
                    # per-date scan.
                    for slot in replay_slots:
                        try:
                            if keep_pages:
                                self.tracker.record_deferred(
                                    slot.slot_date, slot.slot_time
                                )
                            else:
                                self.tracker.record(
                                    slot.slot_date, slot.slot_time
                                )
                        except Exception as e:
                            logger.debug(
                                f"[check] tracker.record failed: {e}"
                            )
                    self.last_errors = 0
                    self.last_checks = 1  # one fetch represents the whole scan
                    logger.info(
                        f"[check-replay] {len(replay_slots)} slot(s) via "
                        f"calendar-replay fast-path"
                    )
                    return replay_slots
                # Replay returned None: log and fall through to legacy path
                logger.warning(
                    "[check-replay] fast-path failed this poll; falling "
                    "back to per-date page-reload scan"
                )

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
                    check_calls = [
                        self._check_date(
                            d,
                            keep_page=keep_pages,
                            abort_event=abort_evt,
                            bypass_normal_skip=bypass_normal_skip,
                            retain_found_page=retain_found_pages,
                        )
                        for d in dates
                    ]
                    results = await _asyncio.gather(
                        *check_calls,
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
                        result = await self._check_date(
                            d,
                            keep_page=keep_pages,
                            bypass_normal_skip=bypass_normal_skip,
                            retain_found_page=retain_found_pages,
                        )
                        slots.extend(result)
                        if result and (keep_pages or stop_on_first_slot):
                            reason = (
                                "sniper page-reuse keeps following dates warm"
                                if keep_pages
                                else "fast path — booker takes the live page"
                            )
                            logger.info(
                                f"[check] First slot found on {d.isoformat()} — "
                                f"stopping sequential scan early ({reason})"
                            )
                            break
                    return slots

            # Sniper mode implies the tighter scan window (Tock releases ≤2 wks).
            # `keep_pages` is the existing flag that signals sniper mode in this
            # method — alias it explicitly so future readers don't have to trace
            # the coupling.
            sniper_horizon = keep_pages
            preferred_dates = self._get_target_dates(
                self.config.preferred_days, sniper_mode=sniper_horizon
            )
            preferred_slots = await _scan_dates(preferred_dates)

            fallback_dates = self._get_target_dates(
                self.config.fallback_days, sniper_mode=sniper_horizon
            )
            total_dates = len(preferred_dates) + len(fallback_dates)

            if preferred_slots:
                self.last_errors = errors[0]
                self.last_checks = len(preferred_dates)
                logger.info(
                    f"Scan complete — {len(preferred_slots)} slot(s) found "
                    f"across {len(preferred_dates)} preferred date(s)"
                )
                result_slots = preferred_slots
            elif not fallback_dates:
                self.last_errors = errors[0]
                self.last_checks = len(preferred_dates)
                logger.info(
                    f"Scan complete — 0 slot(s) found across "
                    f"{len(preferred_dates)} date(s) (no fallback days configured)"
                )
                result_slots = []
            else:
                fallback_slots = await _scan_dates(fallback_dates)
                self.last_errors = errors[0]
                self.last_checks = total_dates
                logger.info(
                    f"Scan complete — {len(fallback_slots)} fallback slot(s) found "
                    f"across {total_dates} date(s) total "
                    f"(0 preferred + {len(fallback_slots)} fallback)"
                )
                result_slots = fallback_slots

            # Sniper-phase CF alerting (Codex pass 2). Independent of prewarm CF.
            if (
                keep_pages
                and self._sniper_cf_attempts > 0
                and notifier is not None
            ):
                sniper_rate = self._sniper_cf_challenges / max(1, self._sniper_cf_attempts)
                if sniper_rate > _CF_CHALLENGE_ALERT_THRESHOLD:
                    try:
                        notifier.cf_challenge_warning(
                            rate=sniper_rate,
                            count=self._sniper_cf_challenges,
                            phase="sniper",
                        )
                    except Exception as e:
                        logger.warning(
                            f"[check] sniper cf_challenge_warning failed: {e}"
                        )

            return result_slots

        finally:
            self._wait_for_calendar = original_wait  # type: ignore[method-assign]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_target_dates(
        self, days: list[str] | None = None, sniper_mode: bool = False
    ) -> list[date]:
        """Dates from tomorrow through the active scan horizon that fall on *days*.

        Defaults to config.preferred_days when days is None.
        When sniper_mode=True, the horizon is capped at config.sniper_scan_weeks
        (Tock releases at most that many weeks of slots; scanning further out is
        wasted effort that contributes only to error counts).
        """
        if days is None:
            days = self.config.preferred_days
        weeks = self.config.sniper_scan_weeks if sniper_mode else self.config.scan_weeks
        weeksOffset = self.config.scan_weeks_lookahead
        today = date.today()
        
        offset = today + timedelta(weeks=weeksOffset)
        start = offset if weeksOffset > 0 else date.today()
        
        end = today + timedelta(weeks=weeks)
        result = []
        
        #current = today + timedelta(days=1)
        current = start + timedelta(days=1)
        while current <= end:
            if current.strftime("%A") in days:
                result.append(current)
            current += timedelta(days=1)
        return result

    async def _check_date(
        self, target_date: date, keep_page: bool = False,
        abort_event: asyncio.Event | None = None,
        bypass_normal_skip: bool = False,
        retain_found_page: bool = False,
    ) -> list[AvailableSlot]:
        """
        Load the Tock search page for target_date, verify the day is
        available in the calendar, click it, then collect time slots.

        keep_page=True (sniper mode): reuses the existing page for this date
        (reload instead of full navigate) for speed and Cloudflare friendliness.

        retain_found_page=True (normal-mode fast path): when slots ARE found
        AND keep_page is False, the page is parked in self._handoff_pages
        instead of being closed in the finally block. The booker reuses it via
        warm_pages so it doesn't have to re-navigate. No effect when keep_page
        is True (the sniper path already retains pages in _sniper_pages).
        """
        date_str = target_date.isoformat()

        # Sniper-mode skip: date failed last poll — skip until pages close.
        if keep_page and self._should_skip_date(date_str, skip_cache_enabled=self._skip_cache_enabled):
            logger.debug(f"[check] {date_str} — skipped (sniper cache: not in calendar last poll)")
            return []

        # Normal-mode skip: date was not visible in calendar on a recent poll.
        # Skip for NORMAL_SKIP_TTL_SEC (20 min) to avoid ~15s calendar timeout
        # per date per poll cycle when dates are beyond the booking window.
        if not keep_page and not bypass_normal_skip and self._should_skip_normal(date_str):
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
        # Set True only when we hand the page off to the booker (normal-mode
        # fast path with slots found). The finally block reads this to decide
        # whether to close the page.
        handoff_to_booker = False
        try:
            if reusing:
                # Defensive: verify the page is still on the search URL
                # before reload. Codex pass 2: if the booker navigated the
                # page to checkout (or an error page) and returned it to
                # checker ownership in some future code path, reload()
                # would reload the wrong URL. Fall through to goto() if
                # the URL has drifted.
                current_url = ""
                try:
                    current_url = page.url or ""
                except Exception:
                    current_url = ""
                if f"date={date_str}" in current_url and "/search" in current_url:
                    logger.debug(f"[check] {date_str} → reload (sniper page reuse)")
                    await page.reload(wait_until="domcontentloaded", timeout=nav_timeout)
                else:
                    logger.warning(
                        f"[check] {date_str} — warm page drifted from search URL "
                        f"(now: {current_url[:80]!r}); forcing fresh goto()"
                    )
                    await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
            else:
                logger.debug(f"[check] {date_str} → {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)

            # Sniper-phase CF detection (Codex pass 2): a challenge during
            # sniper polling is more critical than during prewarm — it's
            # consuming critical-path time during a release window.
            # B2.2: combined URL+DOM check catches challenges that don't
            # change the URL.
            if keep_page:
                self._sniper_cf_attempts += 1
                if await self.is_cloudflare_challenge_page(page):
                    self._sniper_cf_challenges += 1
                    logger.warning(
                        f"[check] {date_str} hit CF challenge during sniper "
                        f"poll (url={page.url}); date will fall back next poll"
                    )
                    # Drop the page so next poll opens a fresh one
                    if date_str in self._sniper_pages:
                        del self._sniper_pages[date_str]
                    try:
                        await page.close()
                    except Exception:
                        pass
                    return []

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
            #
            # B1.5: when skip_day_click_check is True, defer the calendar-day
            # click until after the first slot detection — Tock's
            # `?date=YYYY-MM-DD` URL may already select the date in the SPA
            # so the click is redundant. If post-skip detection finds 0
            # slots, we fall back to clicking the day and detecting once
            # more. The fallback is bounded to a single retry so a SPA quirk
            # doesn't silently miss slots.

            if abort_event is not None and abort_event.is_set():
                return []

            # Phase B3.2: optional XHR telemetry. When event_driven_detection
            # is on, register a `response` listener around the day-click
            # and slot collection so the operator can identify Tock's
            # slot-availability XHR via `xhr_telemetry.jsonl`. Default OFF
            # — production-safe.
            xhr_recorder = self._maybe_create_xhr_recorder(page, target_date)

            skip_click = self.config.skip_day_click_check
            day_clicked = False
            if not skip_click:
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
                day_clicked = True

            # Try multiple selectors for slot/booking buttons.
            # Centralized in selectors.py so checker and booker stay in sync.
            from src.selectors import get_slot_button_selectors
            slot_selectors = get_slot_button_selectors()

            # Split selectors: CSS-compatible ones go through fast page.evaluate(),
            # Playwright-specific ones (:has-text, :text, :visible) fall back to locator API
            css_selectors = []
            pw_selectors = []
            for s in slot_selectors:
                if any(pw in s for pw in [':has-text', ':text(', ':visible']):
                    pw_selectors.append(s)
                else:
                    css_selectors.append(s)

            slot_timeout = 500 if keep_page else 2500
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

            async def _detect_and_collect() -> list[AvailableSlot]:
                # Wait reactively for any slot-like element instead of blind sleep.
                # Short timeout (500ms sniper, 2500ms normal) — move on if nothing appears.
                try:
                    await page.wait_for_selector(slot_selectors[0], timeout=slot_timeout)
                except Exception:
                    pass  # no slots visible yet — proceed to multi-selector check

                found = None
                count = 0
                # Fast path: batch CSS selectors in one evaluate() call
                if css_selectors:
                    res = await page.evaluate(detect_js, css_selectors)
                    if res["index"] >= 0:
                        found = css_selectors[res["index"]]
                        count = res["count"]
                # Slow path: Playwright-specific selectors (only if fast path missed)
                if not found:
                    for try_sel in pw_selectors:
                        try:
                            c = await page.locator(try_sel).count()
                            if c > 0:
                                found = try_sel
                                count = c
                                break
                        except Exception:
                            continue
                if not found:
                    logger.debug(
                        f"[check] {date_str} — no slots found with any selector"
                    )
                    return []
                logger.info(
                    f"[check] {date_str} — {count} slot(s) found via {found!r}"
                )
                # B2.4: telemetry for selector hit-rate analysis. Best-effort —
                # never let a metrics bug break the booking flow.
                try:
                    selector_metrics.record_match("slot_button_check", found)
                except Exception as exc:
                    logger.debug(f"[check] selector_metrics.record_match failed: {exc}")
                return await self._collect_slots_multi(page, target_date, found)

            slots = await _detect_and_collect()

            # B1.5 fallback: if we skipped the day click and the SPA URL alone
            # didn't surface any slots, click the day and retry once. Bounded
            # to a single retry so an SPA quirk doesn't silently miss slots.
            if skip_click and not day_clicked and not slots:
                logger.debug(
                    f"[check] {date_str} — skip-mode produced 0 slots; "
                    "falling back to click_day + re-detect"
                )
                fallback_clicked = await self._click_day(page, target_date)
                if fallback_clicked:
                    day_clicked = True
                    slots = await _detect_and_collect()
                else:
                    if keep_page:
                        self._skip_dates.add(date_str)
                    else:
                        self._add_to_normal_skip(date_str)
                        logger.debug(
                            f"[check] {date_str} cached in normal skip "
                            f"(TTL {NORMAL_SKIP_TTL_SEC}s, skip-mode fallback)"
                        )

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
            # Normal-mode fast path: park the live page so the booker can
            # click the already-visible slot button instead of re-navigating.
            # No effect for keep_page=True (sniper already retains via
            # _sniper_pages) or when no slots were extracted.
            if sorted_slots and retain_found_page and not keep_page:
                # Close any pre-existing handoff page for this date before
                # overwriting — otherwise the old page leaks (Codex review).
                # In current monitor wiring this slot is always empty by the
                # time we get here (defensive close_handoff_pages at start of
                # check_all), but a future concurrent caller could trip it.
                old = self._handoff_pages.get(date_str)
                if old is not None and old is not page:
                    try:
                        if not old.is_closed():
                            await old.close()
                    except Exception:
                        pass
                self._handoff_pages[date_str] = page
                handoff_to_booker = True
                logger.info(
                    f"[check] {date_str} — handing live page to booker "
                    "(normal-mode fast path)"
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
            # Phase B3.2: detach + flush XHR telemetry (best-effort).
            recorder = locals().get("xhr_recorder", None)
            if recorder is not None:
                try:
                    recorder.detach(page)
                    await recorder.flush()
                except Exception as e:
                    logger.debug(
                        f"[xhr-telemetry] detach/flush failed for {date_str}: "
                        f"{type(e).__name__}: {e}"
                    )

            # Close the page UNLESS:
            #   1. keep_page=True (sniper mode keeps it across polls), OR
            #   2. handoff_to_booker=True (booker now owns it; will close
            #      after booking succeeds or fails — see TockBooker._book_single).
            if not keep_page and not handoff_to_booker:
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

        Single browser-side pass: container scope + 5-source extraction
        run inside one page.evaluate, replacing the per-button locator
        chain (~5N round-trips → 1).

        Time-extraction priority order (in JS):
          1. Child span matching slot_time_text selector
          2. Time pattern in parent.text_content()
          3. Time pattern in any ancestor up to 3 levels deep
          4. Button's aria-label or title attribute
          5. Button's own text_content (when not a bare 'Book' / 'Book now')

        If NO source yields a parseable time, the slot is NOT emitted —
        the 'Slot N' fallback is forbidden because the booker cannot match
        a slot without a real time string (Apr 17 root cause).
        """
        container_selector = sel.get("slots_container")
        slot_time_text_selector = sel.get("slot_time_text")
        # Pattern is split into source + flags to construct RegExp in JS.
        time_pattern = r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b"
        time_flags = "i"

        # Codex HIGH 1: Playwright-only selectors (`:has-text`, `:text`,
        # `:visible`) cannot be passed to document.querySelectorAll —
        # fall back to a Python locator iteration for those, preserving
        # the JS fast path for plain CSS selectors.
        if is_playwright_selector(matched_selector):
            return await self._collect_slots_multi_locator_loop(
                page, target_date, matched_selector
            )

        try:
            result = await page.evaluate(
                _COLLECT_SLOTS_JS,
                {
                    "containerSelector": container_selector,
                    "matchedSelector": matched_selector,
                    "slotTimeTextSelector": slot_time_text_selector,
                    "timeRegex": time_pattern,
                    "timeFlags": time_flags,
                },
            )
        except Exception as e:
            logger.error(
                f"[check] {target_date.isoformat()} — "
                f"slot collection evaluate failed: {type(e).__name__}: {e}"
            )
            return []

        if not isinstance(result, dict):
            logger.error(
                f"[check] {target_date.isoformat()} — "
                f"slot collection returned unexpected shape: {result!r}"
            )
            return []

        if not result.get("container_used"):
            logger.debug(
                f"[check] {target_date.isoformat()} — "
                f"slots_container not found; falling back to page-wide "
                f"collection (selector key: 'slots_container')"
            )

        slots: list[AvailableSlot] = []
        raw = result.get("slots") or []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            time_text = item.get("time")
            if time_text is None:
                # No parseable time — drop this slot (do NOT fabricate).
                # Apr 17 lesson: a slot the booker can't book is worse
                # than no slot at all.
                logger.warning(
                    f"[check] {target_date.isoformat()} — "
                    f"slot at index {i} has no extractable time; "
                    f"skipping (was 'Slot {i + 1}' under old fallback)"
                )
                if self.config.debug_screenshots:
                    try:
                        await self._save_error_screenshot(
                            page, target_date.isoformat(),
                            f"slot_no_time_idx{i}"
                        )
                    except Exception:
                        pass
                continue

            slots.append(
                AvailableSlot(
                    slot_date=target_date,
                    slot_time=time_text,
                    day_of_week=target_date.strftime("%A"),
                )
            )

        if slots:
            logger.info(
                f"[check] {target_date.isoformat()} — {len(slots)} slot(s): "
                + ", ".join(s.slot_time for s in slots)
            )
        return slots

    async def _collect_slots_multi_locator_loop(
        self, page: Page, target_date: date, matched_selector: str
    ) -> list[AvailableSlot]:
        """Locator-based fallback for Playwright-only matched selectors
        (`:has-text`, `:text(`, `:visible`) which can't be passed to
        document.querySelectorAll. Mirrors the pre-B1.3 algorithm but
        runs only when the JS fast path is unsafe.
        """
        time_re = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))\b")
        slots: list[AvailableSlot] = []
        date_str = target_date.isoformat()

        try:
            # Container scope: when slots_container exists, scope to it;
            # otherwise fall back to page-wide collection.
            container_selector = sel.get("slots_container")
            container_finder = page.locator(container_selector)
            scoped_root = None
            try:
                if await container_finder.count() > 0:
                    scoped_root = container_finder.first
            except Exception as exc:
                logger.debug(
                    f"[check] {date_str} — container count() raised "
                    f"{type(exc).__name__}: {exc}; falling back to page-wide"
                )
                scoped_root = None

            if scoped_root is None:
                logger.debug(
                    f"[check] {date_str} — slots_container not found; "
                    f"falling back to page-wide collection (PW selector path)"
                )
                locator = page.locator(matched_selector)
            else:
                locator = scoped_root.locator(matched_selector)

            count = await locator.count()
            for i in range(count):
                el = locator.nth(i)
                try:
                    time_text = await self._extract_slot_time_locator(el, time_re)
                    if time_text is None:
                        logger.warning(
                            f"[check] {date_str} — slot at index {i} "
                            "has no extractable time; skipping (PW fallback path)"
                        )
                        continue
                    slots.append(AvailableSlot(
                        slot_date=target_date,
                        slot_time=time_text,
                        day_of_week=target_date.strftime("%A"),
                    ))
                except Exception:
                    continue
        except Exception as e:
            logger.error(
                f"[check] {date_str} — PW-locator collection failed: {e}"
            )

        if slots:
            logger.info(
                f"[check] {date_str} — {len(slots)} slot(s) (PW fallback): "
                + ", ".join(s.slot_time for s in slots)
            )
        return slots

    async def _extract_slot_time_locator(
        self, element, time_re: re.Pattern[str]
    ) -> str | None:
        """5-source extraction via Playwright Locator API. Used by the
        PW-selector fallback in _collect_slots_multi_locator_loop."""
        # Source 1: child span matching slot_time_text selector
        try:
            time_selector = sel.get("slot_time_text")
            time_span = element.locator(time_selector)
            if await time_span.count() > 0:
                t = (await time_span.first.text_content() or "").strip()
                if t:
                    return t
        except Exception:
            pass
        # Source 2: parent text
        try:
            parent = element.locator("..")
            parent_text = (await parent.text_content() or "").strip()
            m = time_re.search(parent_text)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
        except Exception:
            pass
        # Source 3: ancestors up to 3 levels above parent
        try:
            ancestor = element
            for _ in range(3):
                ancestor = ancestor.locator("..")
                anc_text = (await ancestor.text_content() or "").strip()
                m = time_re.search(anc_text)
                if m:
                    return re.sub(r"\s+", " ", m.group(1)).strip()
        except Exception:
            pass
        # Source 4: aria-label / title attributes
        for attr in ("aria-label", "title"):
            try:
                val = await element.get_attribute(attr)
                if val:
                    m = time_re.search(val)
                    if m:
                        return re.sub(r"\s+", " ", m.group(1)).strip()
            except Exception:
                continue
        # Source 5: button's own text content (only if not bare 'Book')
        try:
            btn_text = (await element.text_content() or "").strip()
            if btn_text and btn_text.lower() not in ("book", "book now"):
                m = time_re.search(btn_text)
                if m:
                    return re.sub(r"\s+", " ", m.group(1)).strip()
        except Exception:
            pass
        return None

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
