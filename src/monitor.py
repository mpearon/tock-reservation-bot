"""
Monitoring loop with smart scheduling and sniper mode.

Polling schedule (PT timezone):
────────────────────────────────────────────────────────────────────────────
  SNIPER MODE  Wednesday/Friday @ 16:59 → polls every ~3s until 17:10
               Wednesday/Friday @ 19:59 → polls every ~3s until 20:10
               (Days and times configurable via .env)

  RELEASE WIN  e.g. Monday 9–11am         → every 60s

  PREF EVENING e.g. Fri/Sat/Sun 5–11pm    → every 5 min

  OVERNIGHT    any day midnight–7am        → every 60 min

  DEFAULT      all other times             → every 15 min
────────────────────────────────────────────────────────────────────────────

SNIPER MODE DETAILS
───────────────────
Each SNIPER_TIMES entry (e.g. "16:59") defines a window start.
The window lasts SNIPER_DURATION_MIN minutes (default 11).
During the window the bot sleeps only SNIPER_INTERVAL_SEC seconds (default 3)
between polls. Because each poll (multiple page loads) takes longer than 3s,
the effective rate is "as fast as possible".

All times are evaluated in America/Los_Angeles (PT/PDT automatically).
"""

import asyncio
import logging
from datetime import datetime, time, timedelta

import pytz

from src.booker import TockBooker
from src.checker import AvailabilityChecker
from src.config import Config, parse_time
from src.notifier import Notifier
from src.release_detector import CHECK_INTERVAL_MIN, apply_release_schedule, detect_release_time
from src.tracker import SlotTracker

logger = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")

# How many minutes before a sniper window to pre-warm the session.
# Must be >= the longest non-sniper poll interval (15 min default) to guarantee
# the pre-warm fires at the poll just before the window opens.
PREWARM_BEFORE_MIN = 15

# If a sniper window opens within this many seconds, skip the regular poll and
# wait so the first sniper poll fires right at the window start.  A full scan
# takes ~100-120s, so holding when we're within 120s prevents a slow regular
# poll from burning through the critical first seconds of the window.
_SNIPER_HOLD_SEC = 120


class TockMonitor:
    def __init__(
        self,
        config: Config,
        browser,
        checker: AvailabilityChecker,
        notifier: Notifier,
        tracker: SlotTracker,
    ):
        self.config = config
        self.browser = browser
        self.checker = checker
        self.notifier = notifier
        self.tracker = tracker
        self.booker = TockBooker(config, browser, notifier)
        self._poll_count = 0
        self._booking_secured = False
        self._sniper_active = False  # tracks whether we're in a sniper window

        # Adaptive sniper mode: start concurrent, fall back to sequential if
        # Cloudflare error rate gets too high, retry concurrent after recovery.
        self._sniper_concurrent = True          # current mode for sniper polls
        self._sniper_error_window: list[float] = []  # rolling error rates (last N polls)
        self._SNIPER_WINDOW_SIZE   = 3    # look at last 3 polls to decide
        self._SNIPER_ERROR_THRESH  = 0.20 # >20% errors → switch to sequential
        self._SNIPER_RECOVER_POLLS = 3    # consecutive clean sequential polls → try concurrent again
        self._sniper_sequential_clean = 0  # consecutive clean polls in sequential mode

        # Release-time auto-detection: re-check every CHECK_INTERVAL_MIN
        self._last_release_check: datetime | None = None

        # Session pre-warm: fire PREWARM_BEFORE_MIN minutes before each sniper window.
        # Track which window we've already warmed for to avoid repeated calls.
        self._session_prewarmed_for: str | None = None  # "DayName@HH:MM"

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run_adaptive_test(self, num_polls: int) -> None:
        """
        Test the adaptive concurrent↔sequential switching logic end-to-end.

        Forces sniper mode active, lowers the error threshold to 0% (any
        calendar failure triggers a switch), and runs num_polls consecutive
        polls through the real TockMonitor.poll() path — the same code that
        runs during a live sniper window.

        DRY_RUN is forced so no booking ever fires.
        """
        self.config.dry_run = True

        # Force sniper mode on and use a hair-trigger threshold (0%) so even
        # 1 Cloudflare blip triggers the concurrent→sequential transition
        self._sniper_active = True
        self._sniper_concurrent = True
        self._sniper_error_window.clear()
        self._sniper_sequential_clean = 0
        saved_thresh = self._SNIPER_ERROR_THRESH
        self._SNIPER_ERROR_THRESH = 0.0  # any error triggers switch

        logger.info(
            f"\n{'='*60}\n"
            f"[test-adaptive] Adaptive sniper switching test\n"
            f"  Polls       : {num_polls}\n"
            f"  Error thresh: 0% (hair-trigger — any error switches mode)\n"
            f"  Booking     : DISABLED (DRY_RUN forced)\n"
            f"  Expected    : concurrent → sequential on first CF error,\n"
            f"                sequential → concurrent after "
            f"{self._SNIPER_RECOVER_POLLS} clean polls\n"
            f"{'='*60}"
        )

        for i in range(1, num_polls + 1):
            mode = "CONCURRENT" if self._sniper_concurrent else "sequential"
            logger.info(f"[test-adaptive] ── Poll {i}/{num_polls}  [{mode}] ──")
            await self.poll()
            await asyncio.sleep(0)

        self._SNIPER_ERROR_THRESH = saved_thresh
        self._sniper_active = False
        logger.info(
            f"\n{'='*60}\n"
            f"[test-adaptive] Done. Review log above for mode-switch events:\n"
            f"  switching to SEQUENTIAL  → threshold triggered\n"
            f"  switching back to CONCURRENT → recovery confirmed\n"
            f"{'='*60}"
        )

    async def _refresh_release_schedule(self) -> None:
        """
        Scrape the restaurant page for a release announcement and update the
        sniper schedule if a new date/time is found. Runs on startup and then
        every CHECK_INTERVAL_MIN minutes.
        """
        now = datetime.now(PT)
        if (
            self._last_release_check is not None
            and (now - self._last_release_check).total_seconds() < CHECK_INTERVAL_MIN * 60
        ):
            return  # Not time to check yet

        self._last_release_check = now
        release_dt = await detect_release_time(self.browser, self.config)
        if release_dt:
            changed = apply_release_schedule(self.config, release_dt)
            if changed:
                logger.info(
                    f"[release-detect] Sniper re-aimed at "
                    f"{self.config.sniper_days} @ {self.config.sniper_times} PT"
                )

    async def run(self) -> None:
        """Main loop — runs until interrupted (Ctrl+C)."""
        # Detect release time before first poll so sniper is correctly aimed
        await self._refresh_release_schedule()

        logger.info(
            "Monitor running.\n"
            f"  Preferred days : {', '.join(self.config.preferred_days)}\n"
            f"  Preferred time : {self.config.preferred_time}\n"
            f"  Scan window    : {self.config.scan_weeks} weeks\n"
            f"  Sniper mode    : {', '.join(self.config.sniper_days)} "
            f"@ {', '.join(self.config.sniper_times)} PT "
            f"({self.config.sniper_duration_min} min each)\n"
            "Press Ctrl+C to stop."
        )
        while True:
            # Pre-warm session BEFORE the window opens (not at window entry).
            # Fires when we're within PREWARM_BEFORE_MIN minutes of the next
            # sniper window — giving time to solve CAPTCHA in headed mode.
            prewarm_target = self._get_prewarm_target()
            if prewarm_target and prewarm_target != self._session_prewarmed_for:
                logger.info(
                    f"[monitor] Sniper window within {PREWARM_BEFORE_MIN} min "
                    f"({prewarm_target}) — pre-warming session cookies…"
                )
                await self.browser.warm_session()
                self._session_prewarmed_for = prewarm_target

            # If a sniper window is about to open (within SNIPER_HOLD_SEC),
            # do NOT start a slow regular poll that would burn through the
            # first minutes of the window. Instead, wait and fire the first
            # sniper poll right when the window opens.
            secs_to_sniper = self._seconds_until_next_sniper()
            if secs_to_sniper is not None and 0 < secs_to_sniper <= _SNIPER_HOLD_SEC:
                logger.info(
                    f"[monitor] Sniper window in {secs_to_sniper}s — "
                    f"holding for window (skipping regular poll)"
                )
                await asyncio.sleep(secs_to_sniper)
                continue  # re-enter loop; _get_poll_interval will now see sniper mode

            interval = self._get_poll_interval()
            was_sniper = self._sniper_active

            self.notifier.poll_start(self._poll_count + 1, interval)
            await self.poll()

            # Re-check interval AFTER the poll — the poll may have taken long
            # enough that we've entered (or left) a sniper window since the
            # pre-poll check.
            interval = self._get_poll_interval()

            # If sniper mode just ended (we left the window), log it and
            # close the reused search pages held open during sniper.
            now_sniper = self._is_sniper_window()
            if was_sniper and not now_sniper:
                self.notifier.sniper_mode_ended(self._poll_count)
                await self.checker.close_sniper_pages()
                # Reset so pre-warm fires again if sniper re-arms (new window same day)
                self._session_prewarmed_for = None

            if interval > 0:
                # Cap sleep so the bot wakes exactly when a sniper window opens
                secs_to_sniper = self._seconds_until_next_sniper()
                if secs_to_sniper is not None and secs_to_sniper < interval:
                    logger.info(
                        f"Sleeping {secs_to_sniper}s (sniper window in {secs_to_sniper}s, "
                        f"not the full {interval}s)"
                    )
                    await asyncio.sleep(secs_to_sniper)
                else:
                    logger.info(f"Sleeping {interval}s…")
                    await asyncio.sleep(interval)
                # Re-check release page periodically between polls (not during sniper)
                await self._refresh_release_schedule()
            else:
                # Sniper mode: zero sleep — yield control briefly then immediately re-poll
                await asyncio.sleep(0)

    async def poll(self) -> None:
        """One full check-and-book cycle."""
        self._poll_count += 1

        if self._booking_secured:
            logger.info(
                "[monitor] Booking secured this session — idling. "
                "Restart the bot if you need another reservation."
            )
            return

        # --- Availability check ---
        # Sniper mode uses concurrent by default (~34s vs ~133s sequential).
        # If error rate spikes, adaptive logic switches to sequential automatically.
        use_concurrent = self._sniper_active and self._sniper_concurrent
        try:
            slots = await self.checker.check_all(
                concurrent=use_concurrent,
                keep_pages=self._sniper_active,
            )
        except Exception as e:
            logger.error(f"[monitor] Availability check error: {e}")
            self.notifier.error("Availability check error", str(e))
            return

        # --- Adaptive sniper mode switching ---
        if self._sniper_active and self.checker.last_checks > 0:
            rate = self.checker.last_errors / self.checker.last_checks
            self._sniper_error_window.append(rate)
            if len(self._sniper_error_window) > self._SNIPER_WINDOW_SIZE:
                self._sniper_error_window.pop(0)
            rolling_rate = sum(self._sniper_error_window) / len(self._sniper_error_window)

            if self._sniper_concurrent and rolling_rate > self._SNIPER_ERROR_THRESH:
                self._sniper_concurrent = False
                self._sniper_sequential_clean = 0
                logger.warning(
                    f"[sniper] Concurrent error rate {rolling_rate:.0%} > "
                    f"{self._SNIPER_ERROR_THRESH:.0%} threshold — "
                    f"switching to SEQUENTIAL mode"
                )
            elif not self._sniper_concurrent:
                if rate == 0.0:
                    self._sniper_sequential_clean += 1
                else:
                    self._sniper_sequential_clean = 0
                if self._sniper_sequential_clean >= self._SNIPER_RECOVER_POLLS:
                    self._sniper_concurrent = True
                    self._sniper_error_window.clear()
                    self._sniper_sequential_clean = 0
                    logger.info(
                        f"[sniper] {self._SNIPER_RECOVER_POLLS} clean sequential polls "
                        f"— switching back to CONCURRENT mode"
                    )
            else:
                logger.debug(
                    f"[sniper] {'concurrent' if self._sniper_concurrent else 'sequential'} "
                    f"error rate this poll: {rate:.0%} "
                    f"(rolling {rolling_rate:.0%})"
                )

        if not slots:
            self.notifier.no_slots_found()
            return

        self.notifier.slots_found(slots)

        # --- Dry-run: log and stop ---
        if self.config.dry_run:
            for slot in slots:
                self.notifier.dry_run_would_book(slot)
            return

        # --- Attempt booking ---
        booked = await self.booker.book_best_slot_race(slots)
        if booked:
            self._booking_secured = True
            logger.info(
                f"[monitor] *** Booking secured: {booked} ***\n"
                "Bot will idle from now on. Safe to Ctrl+C."
            )
        else:
            logger.warning(
                "[monitor] All booking attempts failed this cycle. Will retry."
            )

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _get_poll_interval(self) -> int:
        """
        Return the number of seconds to sleep before the next poll.
        Checks schedule priority (highest first):
          1. Sniper window  → SNIPER_INTERVAL_SEC (default 3s)
          2. Release window → 60s
          3. Overnight      → 3600s
          4. Preferred eve  → 300s
          5. Default        → 900s
        """
        now = datetime.now(PT)
        day_name = now.strftime("%A")
        t = now.time()

        # 1. Sniper mode — zero sleep between polls; the page load is the rate limiter
        sniper_info = self._sniper_window_info(now)
        if sniper_info is not None:
            until_str = sniper_info
            if not self._sniper_active:
                self._sniper_active = True
                # Reset adaptive state for each new sniper window
                self._sniper_concurrent = True
                self._sniper_error_window.clear()
                self._sniper_sequential_clean = 0
                self.notifier.sniper_mode_active(
                    day=day_name,
                    trigger_time=t.strftime("%H:%M"),
                    until=until_str,
                )
            logger.debug(
                f"[schedule] SNIPER MODE ({day_name} {t.strftime('%H:%M')} PT, "
                f"until {until_str}) → 0s (continuous)"
            )
            return 0
        else:
            self._sniper_active = False

        # 2. Normal release window
        release_start = parse_time(self.config.release_window_start)
        release_end = parse_time(self.config.release_window_end)
        if (
            day_name in self.config.release_window_days
            and release_start <= t <= release_end
        ):
            logger.debug(
                f"[schedule] Release window ({day_name} {t.strftime('%H:%M')} PT) → 60s"
            )
            return 60

        # 3. Overnight (midnight–7am)
        if t < time(7, 0):
            logger.debug(f"[schedule] Overnight ({t.strftime('%H:%M')} PT) → 3600s")
            return 3600

        # 4. Preferred day evenings (5pm–11pm)
        if day_name in self.config.preferred_days and time(17, 0) <= t <= time(23, 0):
            logger.debug(
                f"[schedule] Preferred evening ({day_name} {t.strftime('%H:%M')} PT) → 300s"
            )
            return 300

        # 5. Default
        logger.debug(f"[schedule] Default ({t.strftime('%H:%M')} PT) → 900s")
        return 900

    def _is_sniper_window(self) -> bool:
        """True if the current PT time falls within any configured sniper window."""
        return self._sniper_window_info(datetime.now(PT)) is not None

    def _sniper_window_info(self, now: datetime) -> str | None:
        """
        If *now* is within a sniper window, return the window-end time as a
        'HH:MM' string. Otherwise return None.
        """
        day_name = now.strftime("%A")
        if day_name not in self.config.sniper_days:
            return None

        t = now.time()
        duration = timedelta(minutes=self.config.sniper_duration_min)

        for start_str in self.config.sniper_times:
            start_dt = _sniper_start_dt(now, start_str)
            end_dt = start_dt + duration
            if parse_time(start_str) <= t <= end_dt.time():
                return end_dt.strftime("%H:%M")

        return None

    def _seconds_until_next_sniper(self) -> int | None:
        """
        Return seconds until the next sniper window starts today, or None
        if no future window exists today.  Used to cap sleep duration so the
        bot wakes up exactly when the window opens.
        """
        now = datetime.now(PT)
        day_name = now.strftime("%A")
        if day_name not in self.config.sniper_days:
            return None

        best = None
        for start_str in self.config.sniper_times:
            start_dt = _sniper_start_dt(now, start_str)
            delta = (start_dt - now).total_seconds()
            if delta > 0 and (best is None or delta < best):
                best = delta

        return int(best) if best is not None else None

    def _get_prewarm_target(self) -> str | None:
        """
        Return a 'DayName@HH:MM' string if any configured sniper window starts
        within the next PREWARM_BEFORE_MIN minutes. Otherwise return None.

        Used to fire session warm-up BEFORE the window opens (not at entry).
        """
        now = datetime.now(PT)
        day_name = now.strftime("%A")
        if day_name not in self.config.sniper_days:
            return None

        for start_str in self.config.sniper_times:
            start_dt = _sniper_start_dt(now, start_str)
            delta_sec = (start_dt - now).total_seconds()
            if 0 < delta_sec <= PREWARM_BEFORE_MIN * 60:
                return f"{day_name}@{start_str}"

        return None


def _sniper_start_dt(now: datetime, start_str: str) -> datetime:
    """Return the sniper window start as a datetime in the same tz as *now*."""
    t = parse_time(start_str)
    return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
