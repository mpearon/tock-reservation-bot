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
from src.config import Config
from src.notifier import Notifier
from src.tracker import SlotTracker

logger = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")


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

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop — runs until interrupted (Ctrl+C)."""
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
            # Determine interval BEFORE poll so it's logged up front
            interval = self._get_poll_interval()
            was_sniper = self._sniper_active

            self.notifier.poll_start(self._poll_count + 1, interval)
            await self.poll()

            # If sniper mode just ended (we left the window), log it
            now_sniper = self._is_sniper_window()
            if was_sniper and not now_sniper:
                self.notifier.sniper_mode_ended(self._poll_count)

            if interval > 0:
                logger.info(f"Sleeping {interval}s…")
                await asyncio.sleep(interval)
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
        # Sniper mode checks all dates concurrently for maximum speed
        try:
            slots = await self.checker.check_all(concurrent=self._sniper_active)
        except Exception as e:
            logger.error(f"[monitor] Availability check error: {e}")
            self.notifier.error("Availability check error", str(e))
            return

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
        release_start = _parse_time(self.config.release_window_start)
        release_end = _parse_time(self.config.release_window_end)
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
            window_start = _parse_time(start_str)
            # Compute window end as a naive datetime then extract time
            start_dt = now.replace(
                hour=window_start.hour,
                minute=window_start.minute,
                second=0,
                microsecond=0,
            )
            end_dt = start_dt + duration
            window_end = end_dt.time()

            if window_start <= t <= window_end:
                return end_dt.strftime("%H:%M")

        return None


def _parse_time(s: str) -> time:
    """Parse 'HH:MM' into datetime.time."""
    h, m = map(int, s.split(":"))
    return time(h, m)
