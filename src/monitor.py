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
from datetime import date, datetime, time, timedelta

import pytz

from src import selector_metrics
from src.booker import TockBooker
from src.checker import AvailabilityChecker
from src.config import Config, parse_time
from src.notifier import Notifier
from src.poll_watchdog import PollWatchdog, WatchdogTrip
from src.release_detector import CHECK_INTERVAL_MIN, apply_release_schedule, detect_release_time
from src.tracker import SlotTracker

logger = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")

# How many minutes before a sniper window to pre-warm the session.
# Must be >= the longest non-sniper poll interval (15 min default) to guarantee
# the pre-warm fires at the poll just before the window opens.
PREWARM_BEFORE_MIN = 15

# Date-page prewarm fires CLOSER to release than session-cookie prewarm
# so prewarmed pages are at most ~6.5 minutes idle before the sniper
# window opens (5 min stagger + 1.5 min slack + window duration).
# Session-cookie prewarm at T-15min remains independent.
PREWARM_DATES_BEFORE_MIN = 5

# Target-date prewarm: how far ahead to look for prewarm candidates and how
# many pages to open at most. 10 days covers next-week's release targets
# (Wed-Sun) when prewarm fires from a Friday; 7 dates fits in ~3.5 min at
# 30s stagger, well under the 11.5-min idle budget before the window opens.
PREWARM_LOOKAHEAD_DAYS = 10
PREWARM_DATE_CAP = 7

# If a sniper window opens within this many seconds, skip the regular poll and
# wait so the first sniper poll fires right at the window start.  A full scan
# takes ~100-120s, so holding when we're within 120s prevents a slow regular
# poll from burning through the critical first seconds of the window.
_SNIPER_HOLD_SEC = 120

# The sniper window opens this many seconds before actual release.  During
# that pre-release period checker.check_all() returns immediately with no I/O
# (see checker.py: "Pre-release phase — skipping calendar scan until release"),
# so the run loop must sleep instead of spinning at sleep(0) — otherwise the
# poll-rate watchdog escalates within seconds (2026-05-01 19:44 incident:
# 120 polls in 60s, 4 watchdog trips, PM2 restart loop).  Must match the
# threshold checker.check_all() applies (the `keep_pages and
# sniper_window_age_sec < 60.0` gate) — same release boundary on both sides.
_SNIPER_PRE_RELEASE_SEC = 60.0
# Cap a single pre-release sleep so the loop wakes promptly to cross the
# release boundary and so tests stay responsive.  0.5s gives ~120 wakeups
# across the 60s pre-release period.  Pre-release polls no longer tick the
# watchdog (see poll() — _is_pre_release_skip_age gate), so the wake-up count
# is bounded by sleep duration, not the watchdog ceiling.
_SNIPER_PRE_RELEASE_TICK_SEC = 0.5

# B2.4: selector-hit telemetry flush cadence inside sniper mode. Sniper polls
# fire ~every 3s; flushing every poll would mean ~20 file writes per minute.
# Flushing every Nth poll keeps the I/O cost negligible while still landing
# fresh data on disk well before the window ends. Outside sniper mode polls
# are 5+ min apart so we always flush.
_SNIPER_METRICS_FLUSH_EVERY_N = 5


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
        self._sniper_slots_found = 0  # slots detected during current sniper window
        # Dedup key for booking_failed embeds — suppresses per-poll spam when
        # the same slot-set stays unbookable across a contested window.
        self._last_booking_failed_key: tuple | None = None

        # Adaptive sniper mode: start concurrent, fall back to sequential if
        # Cloudflare error rate gets too high, retry concurrent after recovery.
        self._sniper_concurrent = True          # current mode for sniper polls
        self._sniper_error_window: list[float] = []  # rolling error rates (last N polls)
        self._SNIPER_WINDOW_SIZE   = 3    # look at last 3 polls to decide
        # >30% errors → switch to sequential. Bumped from 0.20 after the
        # 5/22 Fri Fuhuihua release windows showed 3-poll mode-flapping
        # caused by routine 3/14 (21%) timeout bursts that look like CDN
        # noise but are not the failure mode we should degrade for.
        # 30% requires 5/14 sustained, which matches real Cloudflare-side
        # problems worth giving up concurrency for.
        self._SNIPER_ERROR_THRESH  = 0.30
        self._SNIPER_RECOVER_POLLS = 3    # consecutive clean sequential polls → try concurrent again
        self._sniper_sequential_clean = 0  # consecutive clean polls in sequential mode

        # Release-time auto-detection: re-check every CHECK_INTERVAL_MIN
        self._last_release_check: datetime | None = None

        # Session pre-warm: fire PREWARM_BEFORE_MIN minutes before each sniper window.
        # Track which window we've already warmed for to avoid repeated calls.
        self._session_prewarmed_for: str | None = None  # "DayName@HH:MM"
        # Date-page prewarm: fires at T-5min (closer than cookie prewarm at T-15min).
        # Tracked separately so the two timers fire independently.
        self._session_dates_prewarmed_for: str | None = None  # "DayName@HH:MM"

        # Watchdog: detect pathological poll bursts (see Apr 14 20:14 incident).
        # Normal-mode threshold (10/5s) is generous around legacy 1-poll/3-4s.
        # Sniper-mode threshold (60/5s) accommodates calendar-replay polls
        # (~150 ms each, so 5-10/sec is healthy). enter_sniper_mode()/
        # exit_sniper_mode() are called when _sniper_active transitions.
        self._watchdog = PollWatchdog(
            burst_threshold=10, window_sec=5.0, sniper_burst_threshold=60,
        )

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
        # 1 Cloudflare blip triggers the concurrent→sequential transition.
        # The watchdog must also flip — without enter_sniper_mode() this test
        # path runs the legacy 10/5s ceiling and trips on calendar-replay rates.
        self._sniper_active = True
        self._watchdog.enter_sniper_mode()
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

        try:
            for i in range(1, num_polls + 1):
                mode = "CONCURRENT" if self._sniper_concurrent else "sequential"
                logger.info(f"[test-adaptive] ── Poll {i}/{num_polls}  [{mode}] ──")
                await self.poll()
                await asyncio.sleep(0)
        finally:
            # Always tear down — otherwise an exception mid-loop leaves the
            # watchdog in sniper mode (suppressed sys.exit, 60/5s threshold)
            # for the remainder of the process.
            self._SNIPER_ERROR_THRESH = saved_thresh
            self._sniper_active = False
            self._watchdog.exit_sniper_mode()

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
            # Cookie prewarm: 15 min before window.
            # Fires when we're within PREWARM_BEFORE_MIN minutes of the next
            # sniper window — giving time to solve CAPTCHA in headed mode.
            cookie_prewarm_target = self._get_prewarm_target()
            if cookie_prewarm_target and cookie_prewarm_target != self._session_prewarmed_for:
                logger.info(
                    f"[monitor] Sniper window within {PREWARM_BEFORE_MIN} min "
                    f"({cookie_prewarm_target}) — pre-warming session cookies…"
                )
                success = await self.browser.warm_session()
                if success:
                    self._session_prewarmed_for = cookie_prewarm_target
                else:
                    logger.error(
                        f"[monitor] Pre-warm failed for {cookie_prewarm_target} — "
                        "will retry next poll cycle"
                    )
                    self.notifier.error(
                        "Pre-warm failed",
                        f"Session warm-up for {cookie_prewarm_target} failed. "
                        "Bot will retry but sniper accuracy may be reduced.",
                    )

            # Date-page prewarm: 5 min before window (closer than cookie prewarm
            # so pages don't sit idle for 15+ min before reload). Gated by
            # _should_prewarm_dates() — only fires when sniper reuses pages
            # (reuse off discards prewarmed pages; warm_session still warms CF).
            dates_prewarm_target = self._should_prewarm_dates()
            if dates_prewarm_target:
                prewarm_failed = False
                try:
                    prewarm_dates = self._get_prewarm_dates()
                    if prewarm_dates:
                        logger.info(
                            f"[monitor] Pre-opening {len(prewarm_dates)} "
                            f"target-date page(s) for {dates_prewarm_target}"
                        )
                        await self.checker.prewarm_target_dates(
                            prewarm_dates, stagger_sec=30.0,
                            notifier=self.notifier,
                        )
                except Exception as e:
                    prewarm_failed = True
                    logger.warning(
                        f"[monitor] Target-date prewarm failed (non-critical): {e}"
                    )
                self._session_dates_prewarmed_for = dates_prewarm_target
                if prewarm_failed:
                    logger.info(
                        f"[monitor] {dates_prewarm_target} marked as date-prewarmed; "
                        "page-prewarm degraded — first sniper poll will use cold goto()"
                    )

            # If a sniper window is about to open (within SNIPER_HOLD_SEC),
            # do NOT start a slow regular poll that would burn through the
            # first minutes of the window. Instead, wait and fire the first
            # sniper poll right when the window opens.
            secs_to_sniper = self._seconds_until_next_sniper()
            if secs_to_sniper is not None and 0 < secs_to_sniper <= _SNIPER_HOLD_SEC:
                logger.info(
                    f"[monitor] Sniper window in {secs_to_sniper:.1f}s — "
                    f"holding for window (skipping regular poll)"
                )
                await self._countdown_hold(secs_to_sniper)
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
                self.notifier.sniper_mode_ended(self._sniper_slots_found)
                await self.checker.close_sniper_pages()
                # Also tear down the replay session at the window boundary so
                # the next window starts fresh: this resets the replay
                # circuit-breaker, the once-per-window capture-dumped flag, and
                # the replay-diag poll counter. Without it, a tripped breaker
                # would persist and capture would fire once EVER (not once per
                # window). Idempotent — safe even if no replay session exists.
                await self.checker.close_replay_session()
                # Reset so pre-warm fires again if sniper re-arms (new window same day)
                self._session_prewarmed_for = None
                self._session_dates_prewarmed_for = None

            if interval > 0:
                # Cap sleep so the bot wakes exactly when a sniper window opens
                secs_to_sniper = self._seconds_until_next_sniper()
                if secs_to_sniper is not None and secs_to_sniper < interval:
                    logger.info(
                        f"Sleeping {secs_to_sniper:.1f}s (sniper window in "
                        f"{secs_to_sniper:.1f}s, not the full {interval}s)"
                    )
                    await asyncio.sleep(secs_to_sniper)
                else:
                    logger.info(f"Sleeping {interval}s…")
                    await asyncio.sleep(interval)
                # Re-check release page periodically between polls (not during sniper)
                await self._refresh_release_schedule()
            else:
                # Sniper mode. During the pre-release window the checker
                # returns instantly with no I/O, so sleep(0) here would spin
                # the loop and trip the poll-rate watchdog (see 2026-05-01
                # 19:44 incident: 120 polls in 60s, 4 watchdog trips).
                # Sleep toward the release boundary; once past it, the
                # page-load latency rate-limits naturally.
                pre_sleep = self._pre_release_sleep_sec(datetime.now(PT))
                if pre_sleep > 0:
                    await asyncio.sleep(pre_sleep)
                else:
                    await asyncio.sleep(0)

    async def poll(self) -> None:
        """One full check-and-book cycle."""
        # Cache the sniper age at poll entry. Reading it again in _poll_inner
        # could land on the other side of the 60s boundary, so the watchdog
        # skip decision and check_all's pre-release gate would disagree (one
        # path does real I/O while the other thinks it's a no-op).
        sniper_age = None
        if self._sniper_active:
            sniper_age = self._current_sniper_age_sec(datetime.now(PT))

        # Pre-release sniper polls short-circuit to no I/O. They must NOT
        # count toward the watchdog — otherwise the ~120 wake-ups across the
        # 60s pre-release period burn the 3-trip budget while doing zero work.
        if not self._is_pre_release_skip_age(sniper_age):
            try:
                self._watchdog.tick()
            except WatchdogTrip as e:
                logger.warning(f"[monitor] Watchdog throttled this poll: {e}")
                return  # skip this cycle; throttle already slept
        self._poll_count += 1

        if self._booking_secured:
            logger.info(
                "[monitor] Booking secured this session — idling. "
                "Restart the bot if you need another reservation."
            )
            return

        try:
            await self._poll_inner(sniper_age=sniper_age)
        finally:
            # B2.4: persist selector-hit telemetry. Outside sniper mode polls
            # are 5+ min apart so we always flush; inside sniper mode (~3s
            # polls) we gate to every Nth poll to keep the I/O footprint
            # tiny. Best-effort — never let a metrics bug abort a poll cycle.
            try:
                should_flush = (
                    not self._sniper_active
                    or (self._poll_count % _SNIPER_METRICS_FLUSH_EVERY_N == 0)
                )
                if should_flush:
                    selector_metrics.flush()
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug(f"[monitor] selector_metrics.flush failed: {exc}")

    async def _poll_inner(self, sniper_age: float | None) -> None:
        """Body of poll() — kept separate so finally can run flushes.

        ``sniper_age`` is cached by ``poll()`` at entry (None outside a sniper
        window). Threading it through here keeps the watchdog skip and the
        checker's pre-release gate on the same clock read so they can't
        disagree across the 60s boundary."""
        # --- Availability check ---
        # Sniper mode uses concurrent by default (~34s vs ~133s sequential).
        # If error rate spikes, adaptive logic switches to sequential automatically.
        use_concurrent = self._sniper_active and self._sniper_concurrent
        sniper_age_f = sniper_age if sniper_age is not None else 0.0

        # Normal-mode fast path: stop scanning at first slot and hand the
        # live page off to the booker so it can click the already-visible
        # slot button without re-navigating. Disabled for sniper (which has
        # its own page-reuse via _sniper_pages and races multiple slots),
        # for dry-run (no booking, so nothing to hand off to), and after
        # booking has been secured (poll returns early above anyway, but
        # belt-and-suspenders).
        enable_fast_handoff = (
            not self._sniper_active
            and not self.config.dry_run
            and not self._booking_secured
        )

        try:
            bypass_normal_skip = self._sniper_active or self._in_release_window()
            slots = await self.checker.check_all(
                concurrent=use_concurrent,
                keep_pages=self._sniper_active,
                sniper_window_age_sec=sniper_age_f,
                bypass_normal_skip=bypass_normal_skip,
                notifier=self.notifier,
                stop_on_first_slot=enable_fast_handoff,
                retain_found_pages=enable_fast_handoff,
            )
        except Exception as e:
            logger.error(f"[monitor] Availability check error: {e}")
            self.notifier.error("Availability check error", str(e))
            return

        # --- Adaptive sniper mode switching ---
        self._apply_adaptive_switching(sniper_age_f)

        if not slots:
            self.notifier.no_slots_found()
            # Slots cleared — re-arm the failure-dedup so a later failure of the
            # same slot-set alerts again rather than being suppressed.
            self._last_booking_failed_key = None
            return

        # Track slots found during sniper window for end-of-window summary
        if self._sniper_active:
            self._sniper_slots_found += len(slots)

        # The post-slots section runs under try/finally so that any exception
        # from notifier.slots_found, the warm-pages drain, or book_best_slot_race
        # cannot leave handoff pages open. Cleanup runs in two layers:
        #   1. Pages already popped into local `warm_pages`        — close here
        #   2. Pages still parked in checker._handoff_pages         — close via
        #      checker.close_handoff_pages() in case we never drained them
        # (Codex adversarial review MEDIUM finding.)
        warm_pages: dict[str, "Page"] | None = None
        try:
            self.notifier.slots_found(slots, sniper_mode=self._sniper_active)

            # --- Dry-run: log and stop ---
            if self.config.dry_run:
                for slot in slots:
                    self.notifier.dry_run_would_book(slot)
                return

            # --- Attempt booking ---
            # Pass warm pages to booker (avoids re-navigation in either mode).
            # Two page sources, one drain loop:
            #   • sniper: reuse-on keeps the page in _sniper_pages (pop_warm_page);
            #     reuse-off retains the found-slot page in _handoff_pages. Try
            #     warm first, fall back to handoff.
            #   • normal fast-path: only _handoff_pages.
            # The booker treats them identically (pop()-and-own; close after
            # success/failure). The two sources stay in separate dicts so the
            # sniper warm-page lifetime (across polls) doesn't leak into the
            # one-shot fast-path lifetime (single poll).
            if self._sniper_active or enable_fast_handoff:
                if self._sniper_active:
                    def _drain(date_str: str):
                        return (self.checker.pop_warm_page(date_str)
                                or self.checker.pop_handoff_page(date_str))
                    source = "warm"
                else:
                    _drain = self.checker.pop_handoff_page
                    source = "handoff"
                warm_pages = {}
                seen: set[str] = set()
                for s in slots:
                    if s.slot_date_str in seen:
                        continue  # multiple slots per date share one page
                    seen.add(s.slot_date_str)
                    p = _drain(s.slot_date_str)
                    if p is not None:
                        warm_pages[s.slot_date_str] = p
                if warm_pages:
                    logger.info(
                        f"[monitor] Passing {len(warm_pages)} {source} page(s) to booker"
                    )
                else:
                    warm_pages = None
                    if enable_fast_handoff:
                        logger.info(
                            "[monitor] No handoff page available — booker will "
                            "fall back to fresh navigation"
                        )

            from src.booker import BookingOutcome
            outcome, booked_slot = await self.booker.book_best_slot_race(
                slots, warm_pages=warm_pages
            )

            if outcome == BookingOutcome.CONFIRMED:
                self._booking_secured = True
                self._last_booking_failed_key = None
                logger.info(
                    f"[monitor] *** Booking secured: {booked_slot} ***\n"
                    "Bot will idle from now on. Safe to Ctrl+C."
                )
            elif outcome == BookingOutcome.UNVERIFIED_CONFIRM:
                # Codex pass 2 HIGH: confirm click happened but verification
                # failed. Tock MAY have accepted. We MUST idle this session — do
                # not try another slot or another release window. Operator must
                # restart after verifying manually on Tock's website.
                self._booking_secured = True
                logger.error(
                    f"[monitor] *** Unverified confirm for {booked_slot} ***\n"
                    "Bot will idle until restart. Verify manually at "
                    "https://www.exploretock.com/account/reservations."
                )
            else:  # FAILED
                logger.warning(
                    "[monitor] All booking attempts failed this cycle. Will retry."
                )
                # Wire the (formerly dead) booking_failed embed so a failed
                # cycle alerts the operator. Dedup by slot-set so a contested
                # sniper window (continuous polling, same slots unbookable each
                # poll) fires ONE embed, not dozens — which would hit Discord's
                # rate limit and bury the signal. A new/changed failed slot-set
                # re-alerts. 2026-06-05 post-mortem + PFR hardening.
                failed_key = tuple(sorted(
                    f"{s.slot_date_str}@{s.slot_time}" for s in slots
                ))
                if failed_key != self._last_booking_failed_key:
                    self._last_booking_failed_key = failed_key
                    self.notifier.booking_failed(
                        slots,
                        "all booking attempts failed (slot vanished, checkout "
                        "never loaded, or payment prep failed) — see "
                        "booking_failures/ for the captured page DOM (if any)",
                    )
                else:
                    logger.debug(
                        "[monitor] booking_failed embed suppressed — same "
                        "slot-set already alerted this window"
                    )

            # Flush any deferred tracker writes (sniper mode defers disk I/O)
            self.tracker.flush_deferred()
        finally:
            # Layer 1: close pages popped into warm_pages but never claimed
            # by the booker (early-return paths inside book_best_slot_race).
            if warm_pages:
                for _date_str, p in list(warm_pages.items()):
                    try:
                        if not p.is_closed():
                            await p.close()
                    except Exception:
                        pass
            # Layer 2: any handoff pages still parked in the checker (e.g. an
            # exception fired before we drained them, or a dry-run early-return).
            # _handoff_pages is single-cycle in BOTH modes — normal-mode fast
            # path AND sniper reuse-off found-slot retention — so always drain
            # it here. Sniper-WARM pages (_sniper_pages, reuse ON) are NOT
            # touched: they live across polls by design and close_sniper_pages()
            # handles them at window end.
            try:
                await self.checker.close_handoff_pages()
            except Exception:
                pass

    def _apply_adaptive_switching(self, sniper_age: float) -> None:
        """Update concurrent/sequential mode based on rolling error rate.

        Only applies AFTER the release time has passed (sniper_age >= 60s).
        The sniper window starts 1 minute before release; errors in the first
        60s are expected (sold-out state) and must not count toward degradation.
        """
        if not self._sniper_active:
            return
        if self.checker.last_checks <= 0:
            return
        if sniper_age < 60.0:
            logger.debug(
                f"[sniper] Pre-release (age={sniper_age:.1f}s) — "
                "ignoring errors for adaptive switching"
            )
            return

        rate = self.checker.last_errors / self.checker.last_checks
        self._sniper_error_window.append(rate)
        if len(self._sniper_error_window) > self._SNIPER_WINDOW_SIZE:
            self._sniper_error_window.pop(0)
        rolling_rate = sum(self._sniper_error_window) / len(self._sniper_error_window)
        # Codex HIGH: require a full sample window before any flip. With
        # only 1-2 samples the rolling average is the single (or pair of)
        # post-release polls — exactly the spike pattern the 5/22 incident
        # showed, where one bad poll out of one immediately degraded the
        # bot. Wait until we have N samples so 'rolling' actually means
        # rolling.
        window_full = len(self._sniper_error_window) >= self._SNIPER_WINDOW_SIZE

        if self._sniper_concurrent and window_full and rolling_rate > self._SNIPER_ERROR_THRESH:
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

    async def _countdown_hold(self, secs: float) -> None:
        """Sleep for *secs* seconds, logging a countdown every 10 seconds.

        Used during the sniper hold guard so the bot shows it's alive rather
        than going silent for up to 2 minutes before a sniper window opens.
        """
        remaining = secs
        while remaining > 0:
            chunk = min(10.0, remaining)
            if remaining > 10:
                logger.info(
                    f"[monitor] ⏳ Sniper window in {remaining:.0f}s — holding…"
                )
            await asyncio.sleep(chunk)
            remaining -= chunk

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _in_release_window(self) -> bool:
        now = datetime.now(PT)
        day_name = now.strftime("%A")
        t = now.time()
        release_start = parse_time(self.config.release_window_start)
        release_end = parse_time(self.config.release_window_end)
        return (
            day_name in self.config.release_window_days
            and release_start <= t <= release_end
        )

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
                self._sniper_slots_found = 0
                self._last_booking_failed_key = None  # re-arm failure dedup
                # Reset adaptive state for each new sniper window
                self._sniper_concurrent = True
                self._sniper_error_window.clear()
                self._sniper_sequential_clean = 0
                # Clear normal-mode skip cache so all dates are retried fresh
                # during the sniper window (release may open any date)
                self.checker.clear_normal_skip_cache()
                # Refresh screenshot count from disk so rotation stays accurate
                # even when old screenshots from previous runs are on disk
                self.checker.refresh_screenshot_count()
                # Lift the watchdog's burst threshold (calendar-replay polls
                # run 5-10/sec; the normal 10/5s ceiling trips every cycle)
                # and suppress sys.exit while we're in the window.
                self._watchdog.enter_sniper_mode()
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
            if self._sniper_active:
                # Restore the tighter normal-mode threshold and exit policy
                self._watchdog.exit_sniper_mode()
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

    def _current_sniper_age_sec(self, now: datetime) -> float | None:
        """Seconds since the currently active sniper window started, or None
        if *now* is not within any sniper window today."""
        day_name = now.strftime("%A")
        if day_name not in self.config.sniper_days:
            return None
        duration = timedelta(minutes=self.config.sniper_duration_min)
        for start_str in self.config.sniper_times:
            start_dt = _sniper_start_dt(now, start_str)
            end_dt = start_dt + duration
            if start_dt <= now <= end_dt:
                return (now - start_dt).total_seconds()
        return None

    def _pre_release_sleep_sec(self, now: datetime) -> float:
        """How long the sniper run loop should sleep when it's still in the
        pre-release phase. Returns 0 outside sniper or once release has passed
        — the page-load latency then rate-limits naturally."""
        age = self._current_sniper_age_sec(now)
        if age is None or age >= _SNIPER_PRE_RELEASE_SEC:
            return 0.0
        return min(_SNIPER_PRE_RELEASE_SEC - age, _SNIPER_PRE_RELEASE_TICK_SEC)

    def _is_pre_release_skip_age(self, age: float | None) -> bool:
        """True if a poll with this cached age would short-circuit in
        checker.check_all() (sniper active, age < _SNIPER_PRE_RELEASE_SEC).
        Caller passes the age it read at poll entry so the watchdog skip
        decision and check_all's pre-release gate agree on the same clock."""
        if not self._sniper_active:
            return False
        return age is not None and age < _SNIPER_PRE_RELEASE_SEC

    def _seconds_until_next_sniper(self) -> float | None:
        """
        Return seconds until the next sniper window starts today, or None
        if no future window exists today.  Used to cap sleep duration so the
        bot wakes at the exact moment the window opens (sub-second precision).
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

        return best

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

    def _get_dates_prewarm_target(self) -> str | None:
        """Return a 'DayName@HH:MM' string if any sniper window starts within
        the next PREWARM_DATES_BEFORE_MIN minutes. None otherwise.

        Used to fire the target-date PAGE prewarm closer to release than the
        session-cookie prewarm (PREWARM_BEFORE_MIN), so pages don't sit idle
        long enough for Cloudflare/SPA state to drift.
        """
        now = datetime.now(PT)
        day_name = now.strftime("%A")
        if day_name not in self.config.sniper_days:
            return None
        for start_str in self.config.sniper_times:
            start_dt = _sniper_start_dt(now, start_str)
            delta_sec = (start_dt - now).total_seconds()
            if 0 < delta_sec <= PREWARM_DATES_BEFORE_MIN * 60:
                return f"{day_name}@{start_str}"
        return None

    def _should_prewarm_dates(self) -> str | None:
        """Target ('DayName@HH:MM') if date-page prewarm should fire now, else None.

        Date-page prewarm pre-opens one page per target date so the first sniper
        poll can reload them — but that only pays off when sniper REUSES pages.
        With reuse off (the default) the pages are discarded, so prewarm would
        just burn pre-window time (and can delay the first poll) for nothing; the
        cookie prewarm (warm_session) already refreshes the Cloudflare session.
        Returns None unless reuse is enabled AND a window is in range AND we have
        not already prewarmed for it this cycle.
        """
        if not getattr(self.config, "sniper_reuse_pages", False):
            return None
        target = self._get_dates_prewarm_target()
        if target is None or target == self._session_dates_prewarmed_for:
            return None
        return target

    def _get_prewarm_dates(self) -> list[date]:
        """Return up to 7 target dates within the next ~10 days to prewarm.

        A single Fuhuihua Friday release covers the upcoming calendar week's
        active days. If today is Friday T, those are roughly T+5 (Wed) through
        T+9 (Sun). We extend the lookahead to 10 days so the targeted Fri/Sat/Sun
        are captured even when the prewarm fires from a non-Friday day.

        Combines preferred_days + fallback_days. Sorted by proximity (closest
        first) so when the cap of 7 is hit, we naturally take next-week's
        slots before two-weeks-out slots — those belong to the FOLLOWING
        Friday's release window, not this one.
        """
        days = list(self.config.preferred_days) + list(self.config.fallback_days)
        if not days:
            return []
        today = date.today()
        # PREWARM_LOOKAHEAD_DAYS horizon: covers the upcoming calendar week's
        # release targets (Wed T+5 through Sun T+9 when prewarm fires on
        # Friday at T-5min, per config.prewarm_min_days_out=5).
        # Beyond this = next release's territory.
        end = today + timedelta(days=PREWARM_LOOKAHEAD_DAYS)
        result: list[date] = []
        # Start at config.prewarm_min_days_out to skip dates from the prior
        # release. On a Fuhuihua Friday release, today+5 = next Wednesday
        # (the earliest date in the new release window).
        current = today + timedelta(days=self.config.prewarm_min_days_out)
        while current <= end:
            if current.strftime("%A") in days:
                result.append(current)
            current += timedelta(days=1)
        # Cap at PREWARM_DATE_CAP (closest first — list is already chronologically sorted)
        return result[:PREWARM_DATE_CAP]


def _sniper_start_dt(now: datetime, start_str: str) -> datetime:
    """Return the sniper window start as a datetime in the same tz as *now*."""
    t = parse_time(start_str)
    return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
