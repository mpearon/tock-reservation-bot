"""Tests for normal-mode fast-path booking: stop scanning on first slot and
hand the live page off to the booker so it doesn't have to re-navigate.

Background
──────────
Before this change, normal-mode polling kept scanning all preferred dates
even after a slot was found, then closed the live page. The booker then
re-navigated to Tock to click the slot — by which time the slot was often
gone (observed 2026-05-09 17:56 incident: detected at :39, booker started
at :57:00, slot vanished).

Fix
───
- AvailabilityChecker.check_all gains stop_on_first_slot + retain_found_pages.
  In sequential mode (normal polling), it stops after the first date that
  yields slots, and parks that date's live page in self._handoff_pages.
- Monitor enables the fast path for booking-enabled normal mode (not
  sniper, not dry_run, not already secured) and drains _handoff_pages
  via pop_handoff_page() into book_best_slot_race(warm_pages=...).
- Booker's existing warm-page support reuses the handed-off page just
  like a sniper warm page; it falls back to a fresh page when none is
  provided or the page is closed.
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="t@e.com",
        tock_password="p",
        card_cvc="123",
        discord_webhook_url="",
        headless=True,
        dry_run=False,
        restaurant_slug="test",
        party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=["Monday", "Tuesday", "Wednesday", "Thursday"],
        preferred_time="17:00",
        scan_weeks=2,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_checker(**config_overrides) -> AvailabilityChecker:
    cfg = _make_config(**config_overrides)
    browser = MagicMock()
    tracker = MagicMock()
    tracker.record = MagicMock()
    tracker.record_deferred = MagicMock()
    return AvailabilityChecker(cfg, browser, tracker)


# ---------------------------------------------------------------------------
# pop_handoff_page / close_handoff_pages
# ---------------------------------------------------------------------------

class TestPopHandoffPage:
    """pop_handoff_page mirrors pop_warm_page for normal-mode handoff."""

    def test_returns_none_for_missing_date(self):
        checker = _make_checker()
        assert checker.pop_handoff_page("2026-04-17") is None

    def test_returns_page_and_removes_from_dict(self):
        checker = _make_checker()
        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=False)
        checker._handoff_pages["2026-04-17"] = mock_page

        result = checker.pop_handoff_page("2026-04-17")

        assert result is mock_page
        assert "2026-04-17" not in checker._handoff_pages

    def test_returns_none_for_closed_page(self):
        checker = _make_checker()
        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=True)
        checker._handoff_pages["2026-04-17"] = mock_page

        assert checker.pop_handoff_page("2026-04-17") is None
        assert "2026-04-17" not in checker._handoff_pages

    def test_handoff_pages_dict_starts_empty(self):
        checker = _make_checker()
        assert checker._handoff_pages == {}


class TestCloseHandoffPages:
    @pytest.mark.asyncio
    async def test_close_handoff_pages_closes_and_clears(self):
        checker = _make_checker()
        p1 = MagicMock()
        p1.is_closed = MagicMock(return_value=False)
        p1.close = AsyncMock()
        p2 = MagicMock()
        p2.is_closed = MagicMock(return_value=False)
        p2.close = AsyncMock()
        checker._handoff_pages["2026-05-08"] = p1
        checker._handoff_pages["2026-05-09"] = p2

        await checker.close_handoff_pages()

        p1.close.assert_called_once()
        p2.close.assert_called_once()
        assert checker._handoff_pages == {}

    @pytest.mark.asyncio
    async def test_close_handoff_pages_safe_when_close_raises(self):
        checker = _make_checker()
        p = MagicMock()
        p.is_closed = MagicMock(return_value=False)
        p.close = AsyncMock(side_effect=Exception("already closed"))
        checker._handoff_pages["2026-05-08"] = p

        # Must not raise
        await checker.close_handoff_pages()
        assert checker._handoff_pages == {}


# ---------------------------------------------------------------------------
# check_all stop_on_first_slot semantics
# ---------------------------------------------------------------------------

class TestCheckAllStopsOnFirstSlot:
    @pytest.mark.asyncio
    async def test_sequential_stops_on_first_found_slot(self):
        """Normal mode + stop_on_first_slot=True: scan halts after first hit."""
        checker = _make_checker()
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )

        call_log: list[date] = []

        async def fake_check_date(target_date, **kwargs):
            call_log.append(target_date)
            if target_date == date(2026, 5, 8):
                return [slot]
            return []

        def get_dates(days=None, sniper_mode=False):
            if days == checker.config.preferred_days:
                return [date(2026, 5, 8), date(2026, 5, 9), date(2026, 5, 10)]
            return []

        with patch.object(checker, "_check_date", side_effect=fake_check_date), \
             patch.object(checker, "_get_target_dates", side_effect=get_dates):
            result = await checker.check_all(
                concurrent=False,
                keep_pages=False,
                stop_on_first_slot=True,
            )

        assert len(result) == 1
        assert call_log == [date(2026, 5, 8)], (
            f"Sequential scan must stop after first slot found; saw {call_log}"
        )

    @pytest.mark.asyncio
    async def test_sequential_continues_when_first_date_empty(self):
        """If the first date has no slots, scanning continues to the next."""
        checker = _make_checker()
        slot = AvailableSlot(
            slot_date=date(2026, 5, 9),
            slot_time="6:00 PM",
            day_of_week="Saturday",
        )

        call_log: list[date] = []

        async def fake_check_date(target_date, **kwargs):
            call_log.append(target_date)
            if target_date == date(2026, 5, 9):
                return [slot]
            return []

        def get_dates(days=None, sniper_mode=False):
            if days == checker.config.preferred_days:
                return [date(2026, 5, 8), date(2026, 5, 9), date(2026, 5, 10)]
            return []

        with patch.object(checker, "_check_date", side_effect=fake_check_date), \
             patch.object(checker, "_get_target_dates", side_effect=get_dates):
            result = await checker.check_all(
                concurrent=False,
                keep_pages=False,
                stop_on_first_slot=True,
            )

        assert len(result) == 1
        assert call_log == [date(2026, 5, 8), date(2026, 5, 9)], (
            f"Empty first date must not abort; should scan until first hit. "
            f"saw {call_log}"
        )

    @pytest.mark.asyncio
    async def test_default_continues_full_scan(self):
        """Without stop_on_first_slot, all preferred dates are still scanned
        (existing behavior is preserved when the flag is False)."""
        checker = _make_checker()
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )

        call_log: list[date] = []

        async def fake_check_date(target_date, **kwargs):
            call_log.append(target_date)
            if target_date == date(2026, 5, 8):
                return [slot]
            return []

        def get_dates(days=None, sniper_mode=False):
            if days == checker.config.preferred_days:
                return [date(2026, 5, 8), date(2026, 5, 9), date(2026, 5, 10)]
            return []

        with patch.object(checker, "_check_date", side_effect=fake_check_date), \
             patch.object(checker, "_get_target_dates", side_effect=get_dates):
            await checker.check_all(
                concurrent=False,
                keep_pages=False,
                # stop_on_first_slot defaults to False
            )

        assert len(call_log) == 3, (
            f"Default behavior must scan all preferred dates; saw {call_log}"
        )


class TestCheckAllSkipsFallbackWhenPreferredFound:
    """Existing invariant — re-tested to ensure the new flag preserves it."""

    @pytest.mark.asyncio
    async def test_fallback_skipped_when_preferred_yields_slot(self):
        checker = _make_checker()
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )

        scanned_days: list[str] = []

        async def fake_check_date(target_date, **kwargs):
            scanned_days.append(target_date.strftime("%A"))
            if target_date == date(2026, 5, 8):
                return [slot]
            return []

        def get_dates(days=None, sniper_mode=False):
            if days == checker.config.preferred_days:
                return [date(2026, 5, 8)]
            return [date(2026, 5, 11), date(2026, 5, 12)]  # Mon, Tue

        with patch.object(checker, "_check_date", side_effect=fake_check_date), \
             patch.object(checker, "_get_target_dates", side_effect=get_dates):
            result = await checker.check_all(
                concurrent=False,
                keep_pages=False,
                stop_on_first_slot=True,
            )

        assert len(result) == 1
        assert "Monday" not in scanned_days
        assert "Tuesday" not in scanned_days


# ---------------------------------------------------------------------------
# Page handoff in _check_date
# ---------------------------------------------------------------------------

def _make_mock_page(date_str: str = "2026-05-08") -> AsyncMock:
    """Page that survives _check_date's full happy path."""
    page = AsyncMock()
    page.url = f"https://www.exploretock.com/test/search?date={date_str}"
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.reload = AsyncMock()
    page.close = AsyncMock()
    # B2.2: distinguish the CF DOM-detect JS (must return False so the
    # sniper-poll path doesn't treat the test page as challenged) from
    # the slot-detect JS (returns {index, count}).
    async def _eval(js, *args, **kwargs):
        if "challenges.cloudflare.com" in js or "cf-turnstile" in js:
            return False
        return {"index": 0, "count": 1}
    page.evaluate = AsyncMock(side_effect=_eval)
    page.wait_for_selector = AsyncMock()
    page.screenshot = AsyncMock()
    return page


class TestPageHandoffOnSlotFound:
    """When retain_found_page=True and slots are found, the page is parked
    in _handoff_pages instead of being closed in the finally block."""

    @pytest.mark.asyncio
    async def test_page_retained_when_slots_found(self):
        checker = _make_checker()
        page = _make_mock_page()
        checker.browser.new_page = AsyncMock(return_value=page)

        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )

        with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
             patch.object(checker, "_click_day", AsyncMock(return_value=True)), \
             patch.object(checker, "_collect_slots_multi",
                          AsyncMock(return_value=[slot])):
            result = await checker._check_date(
                date(2026, 5, 8),
                keep_page=False,
                retain_found_page=True,
            )

        assert len(result) == 1
        page.close.assert_not_called(), (
            "Page must NOT be closed — it is handed off to the booker"
        )
        assert checker._handoff_pages.get("2026-05-08") is page

    @pytest.mark.asyncio
    async def test_page_closed_when_no_slots_found(self):
        """retain_found_page=True only retains pages that ACTUALLY found slots."""
        checker = _make_checker()
        page = _make_mock_page()
        # No slots → evaluate returns index=-1
        page.evaluate = AsyncMock(return_value={"index": -1, "count": 0})
        # Slow path: page.locator(...).count() returns 0 — make this a sync
        # MagicMock so we don't leave an un-awaited coroutine behind.
        empty_locator = MagicMock()
        empty_locator.count = AsyncMock(return_value=0)
        page.locator = MagicMock(return_value=empty_locator)
        checker.browser.new_page = AsyncMock(return_value=page)

        with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
             patch.object(checker, "_click_day", AsyncMock(return_value=True)):
            result = await checker._check_date(
                date(2026, 5, 8),
                keep_page=False,
                retain_found_page=True,
            )

        assert result == []
        page.close.assert_called_once(), (
            "Page must be closed when no slots — nothing to hand off"
        )
        assert "2026-05-08" not in checker._handoff_pages

    @pytest.mark.asyncio
    async def test_default_retain_false_closes_page_even_with_slots(self):
        """retain_found_page defaults to False — old behavior preserved."""
        checker = _make_checker()
        page = _make_mock_page()
        checker.browser.new_page = AsyncMock(return_value=page)

        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )

        with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
             patch.object(checker, "_click_day", AsyncMock(return_value=True)), \
             patch.object(checker, "_collect_slots_multi",
                          AsyncMock(return_value=[slot])):
            result = await checker._check_date(
                date(2026, 5, 8),
                keep_page=False,
                # retain_found_page=False (default)
            )

        assert len(result) == 1
        page.close.assert_called_once(), (
            "Default behavior must close the page even when slots are found"
        )
        assert "2026-05-08" not in checker._handoff_pages


class TestSniperPathUnaffectedByRetain:
    """Sniper mode (keep_page=True) is independent of the new retain flag.
    The page goes into _sniper_pages, not _handoff_pages, regardless."""

    @pytest.mark.asyncio
    async def test_sniper_keeps_page_in_sniper_pages_dict(self):
        checker = _make_checker(sniper_reuse_pages=True)  # reuse keeps the page
        page = _make_mock_page()
        checker.browser.new_page = AsyncMock(return_value=page)

        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )

        with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
             patch.object(checker, "_click_day", AsyncMock(return_value=True)), \
             patch.object(checker, "_collect_slots_multi",
                          AsyncMock(return_value=[slot])):
            await checker._check_date(
                date(2026, 5, 8),
                keep_page=True,           # sniper mode
                retain_found_page=False,  # explicitly off — should be no-op for sniper
            )

        assert checker._sniper_pages.get("2026-05-08") is page
        assert "2026-05-08" not in checker._handoff_pages
        page.close.assert_not_called()


# ---------------------------------------------------------------------------
# Monitor wiring: poll() enables the fast path appropriately
# ---------------------------------------------------------------------------

def _build_monitor(*, dry_run: bool, slots: list[AvailableSlot],
                   sniper_active: bool = False,
                   pop_handoff_results: list = None,
                   pop_warm_results: list = None):
    """Build a TockMonitor with mocked checker/booker/notifier suitable for
    asserting how poll() wires data through."""
    from src.monitor import TockMonitor

    cfg = _make_config(dry_run=dry_run)
    browser = MagicMock()
    browser.warm_session = AsyncMock()

    checker = MagicMock()
    checker.check_all = AsyncMock(return_value=slots)
    checker.last_checks = max(1, len(slots))
    checker.last_errors = 0
    checker.close_sniper_pages = AsyncMock()
    checker.close_replay_session = AsyncMock()
    checker.close_handoff_pages = AsyncMock()
    checker.flush_deferred = MagicMock()
    handoff_iter = iter(pop_handoff_results or [])
    warm_iter = iter(pop_warm_results or [])

    def pop_handoff(date_str):
        try:
            return next(handoff_iter)
        except StopIteration:
            return None

    def pop_warm(date_str):
        try:
            return next(warm_iter)
        except StopIteration:
            return None

    checker.pop_handoff_page = MagicMock(side_effect=pop_handoff)
    checker.pop_warm_page = MagicMock(side_effect=pop_warm)

    notifier = MagicMock()
    for name in [
        "poll_start", "no_slots_found", "slots_found", "error",
        "dry_run_would_book", "booking_attempting", "booking_aborted",
        "booking_confirmed",
    ]:
        setattr(notifier, name, MagicMock())

    tracker = MagicMock()
    tracker.flush_deferred = MagicMock()

    with patch("src.monitor.TockBooker"):
        monitor = TockMonitor(cfg, browser, checker, notifier, tracker)
    monitor._sniper_active = sniper_active
    monitor._sniper_concurrent = True
    monitor._booking_secured = False
    return monitor, checker, notifier


class TestMonitorEnablesFastHandoffInNormalMode:
    @pytest.mark.asyncio
    async def test_normal_mode_passes_handoff_pages_to_booker(self):
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        warm_page = MagicMock()
        warm_page.is_closed = MagicMock(return_value=False)
        warm_page.close = AsyncMock()

        monitor, checker, _ = _build_monitor(
            dry_run=False,
            slots=[slot],
            sniper_active=False,
            pop_handoff_results=[warm_page],
        )

        booker_calls = []
        from src.booker import BookingOutcome

        async def fake_book_race(slots, warm_pages=None):
            booker_calls.append({"slots": slots, "warm_pages": warm_pages})
            return BookingOutcome.CONFIRMED, slots[0]

        monitor.booker.book_best_slot_race = AsyncMock(side_effect=fake_book_race)

        await monitor.poll()

        # check_all called with the new fast-path flags
        kwargs = checker.check_all.call_args.kwargs
        assert kwargs.get("stop_on_first_slot") is True
        assert kwargs.get("retain_found_pages") is True
        # Booker received the handoff page
        assert len(booker_calls) == 1
        wp = booker_calls[0]["warm_pages"]
        assert wp is not None
        assert wp.get("2026-05-08") is warm_page
        # Sniper pop should NOT have been used in normal mode
        checker.pop_warm_page.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_enable_fast_handoff(self):
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        monitor, checker, _ = _build_monitor(
            dry_run=True, slots=[slot], sniper_active=False,
        )

        await monitor.poll()

        kwargs = checker.check_all.call_args.kwargs
        assert kwargs.get("retain_found_pages") is False, (
            "dry_run must not retain pages — they have nowhere to go"
        )

    @pytest.mark.asyncio
    async def test_sniper_mode_uses_warm_pages_not_handoff(self):
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        sniper_page = MagicMock()
        sniper_page.is_closed = MagicMock(return_value=False)
        sniper_page.close = AsyncMock()
        monitor, checker, _ = _build_monitor(
            dry_run=False,
            slots=[slot],
            sniper_active=True,
            pop_warm_results=[sniper_page],
        )

        from src.booker import BookingOutcome
        monitor.booker.book_best_slot_race = AsyncMock(
            return_value=(BookingOutcome.FAILED, None)
        )

        await monitor.poll()

        kwargs = checker.check_all.call_args.kwargs
        # Sniper does its own race; fast-path should be off
        assert kwargs.get("retain_found_pages") is False
        assert kwargs.get("stop_on_first_slot") is False
        # The sniper warm-page accessor was used; handoff was not
        checker.pop_warm_page.assert_called()
        checker.pop_handoff_page.assert_not_called()

    @pytest.mark.asyncio
    async def test_sniper_reuse_off_falls_back_to_handoff_page(self):
        """Sniper mode with reuse OFF (default): no kept warm page exists, but
        the found-slot page was retained in _handoff_pages. The monitor must
        try pop_warm_page (→ None) then fall back to pop_handoff_page and pass
        that page to the booker."""
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        handoff_page = MagicMock()
        handoff_page.is_closed = MagicMock(return_value=False)
        handoff_page.close = AsyncMock()
        monitor, checker, _ = _build_monitor(
            dry_run=False,
            slots=[slot],
            sniper_active=True,
            pop_warm_results=[],                 # reuse off → no kept warm page
            pop_handoff_results=[handoff_page],  # but the found page was retained
        )

        booker_calls = []
        from src.booker import BookingOutcome

        async def fake_book_race(slots, warm_pages=None):
            booker_calls.append(warm_pages)
            return BookingOutcome.CONFIRMED, slots[0]

        monitor.booker.book_best_slot_race = AsyncMock(side_effect=fake_book_race)

        await monitor.poll()

        checker.pop_warm_page.assert_called()       # tried the reuse dict first
        checker.pop_handoff_page.assert_called()    # fell back to the retained page
        wp = booker_calls[0]
        assert wp is not None and wp.get("2026-05-08") is handoff_page

    @pytest.mark.asyncio
    async def test_one_handoff_page_for_multiple_slots_same_date(self):
        slot1 = AvailableSlot(
            slot_date=date(2026, 5, 8), slot_time="5:00 PM", day_of_week="Friday",
        )
        slot2 = AvailableSlot(
            slot_date=date(2026, 5, 8), slot_time="8:00 PM", day_of_week="Friday",
        )
        warm_page = MagicMock()
        warm_page.is_closed = MagicMock(return_value=False)
        warm_page.close = AsyncMock()

        monitor, checker, _ = _build_monitor(
            dry_run=False,
            slots=[slot1, slot2],
            sniper_active=False,
            pop_handoff_results=[warm_page],
        )

        booker_calls = []
        from src.booker import BookingOutcome

        async def fake_book_race(slots, warm_pages=None):
            booker_calls.append(warm_pages)
            return BookingOutcome.CONFIRMED, slots[0]

        monitor.booker.book_best_slot_race = AsyncMock(side_effect=fake_book_race)

        await monitor.poll()

        wp = booker_calls[0]
        assert wp is not None
        assert len(wp) == 1, (
            f"Two slots on same date must dedup to one warm page; got {wp}"
        )
        assert wp.get("2026-05-08") is warm_page
        # pop_handoff_page called exactly once (deduped)
        assert checker.pop_handoff_page.call_count == 1


class TestNoPageLeak:
    @pytest.mark.asyncio
    async def test_unclaimed_handoff_pages_closed_after_book(self):
        """If the booker refuses to claim the warm page (e.g. uncertain disk
        file refusal returns early), the page must still be closed by the
        monitor — not leaked."""
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        unclaimed = MagicMock()
        unclaimed.is_closed = MagicMock(return_value=False)
        unclaimed.close = AsyncMock()

        monitor, _, _ = _build_monitor(
            dry_run=False,
            slots=[slot],
            sniper_active=False,
            pop_handoff_results=[unclaimed],
        )

        # Booker returns early without popping warm_pages (simulates the
        # uncertain-disk-file refusal path inside book_best_slot_race)
        from src.booker import BookingOutcome

        async def fake_book_race(slots, warm_pages=None):
            return BookingOutcome.UNVERIFIED_CONFIRM, slots[0]

        monitor.booker.book_best_slot_race = AsyncMock(side_effect=fake_book_race)

        await monitor.poll()

        unclaimed.close.assert_called_once(), (
            "Unclaimed handoff page must be closed by monitor cleanup"
        )


# ---------------------------------------------------------------------------
# Booker: warm-page handoff fallback to fresh navigation
# ---------------------------------------------------------------------------

class TestBookerHandoffFallback:
    """Booker already supports warm pages; this test pins down that a
    closed/None warm_page falls through to a fresh page.goto()."""

    @pytest.mark.asyncio
    async def test_falls_back_to_fresh_page_when_warm_closed(self):
        from src.booker import TockBooker

        cfg = _make_config(dry_run=False)
        browser = MagicMock()
        notifier = MagicMock()
        for name in [
            "booking_attempting", "booking_confirmed", "booking_aborted",
            "no_payment_method", "error",
        ]:
            setattr(notifier, name, MagicMock())
        booker = TockBooker(cfg, browser, notifier)

        # Warm page that reports as closed — booker must fall through to fresh
        warm_page = MagicMock()
        warm_page.is_closed = MagicMock(return_value=True)

        fresh_page = AsyncMock()
        fresh_page.url = "https://www.exploretock.com/test/search?date=2026-05-08"
        fresh_page.is_closed = MagicMock(return_value=False)
        fresh_page.close = AsyncMock()
        fresh_page.goto = AsyncMock()
        fresh_page.evaluate = AsyncMock()
        fresh_page.wait_for_selector = AsyncMock()
        browser.new_page = AsyncMock(return_value=fresh_page)

        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        booking_won = asyncio.Event()

        with patch.object(booker, "_wait_for_selector",
                          AsyncMock(return_value=True)), \
             patch.object(booker, "_click_calendar_day",
                          AsyncMock(return_value=True)), \
             patch.object(booker, "_click_time_slot",
                          AsyncMock(return_value=False)), \
             patch.object(booker, "_booking_screenshot", AsyncMock()):
            await booker._book_single(slot, booking_won, warm_page=warm_page)

        # A fresh page was created because warm_page was closed
        browser.new_page.assert_called_once()
        # The fresh page was navigated (Step 1 path)
        fresh_page.goto.assert_called_once()
        # And it was closed by _book_single's finally
        fresh_page.close.assert_called_once()


# ---------------------------------------------------------------------------
# Codex adversarial review fixes
# ---------------------------------------------------------------------------

class TestStrictTimeMatchOnWarmPage:
    """Codex HIGH: when booker uses a warm page, it must NOT click the
    'first specific button' fallback if the exact target time isn't present.
    Otherwise a vanished slot causes the booker to book a different time.
    """

    @pytest.mark.asyncio
    async def test_warm_page_refuses_first_button_fallback(self):
        """slot.slot_time='5:00 PM' but only an '8:00 PM' button is on the
        page → must NOT click '8:00 PM'; return False so race tries another."""
        from src.booker import TockBooker

        cfg = _make_config(dry_run=False)
        booker = TockBooker(cfg, MagicMock(), MagicMock())

        page = AsyncMock()
        page.url = "https://www.exploretock.com/test/search?date=2026-05-08"
        page.is_closed = MagicMock(return_value=False)
        page.wait_for_selector = AsyncMock()

        # One button, time text doesn't match target
        wrong_btn = AsyncMock()
        wrong_btn.text_content = AsyncMock(return_value="8:00 PM")
        wrong_btn.click = AsyncMock()

        button_locator = MagicMock()
        button_locator.count = AsyncMock(return_value=1)
        button_locator.nth = MagicMock(return_value=wrong_btn)
        page.locator = MagicMock(return_value=button_locator)

        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )

        result = await booker._click_time_slot(page, slot, strict_time_match=True)

        assert result is False, (
            "Strict mode must return False when target time absent — "
            "fallback to the first button would book the wrong slot"
        )
        wrong_btn.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_strict_default_keeps_existing_fallback(self):
        """Default (no warm page) preserves the existing fallback so
        fresh-navigation booking continues to work.

        Post-B1.2: the fallback decision is in the JS — when the JS reports
        a `first-fallback` click, the wrapper returns True and propagates
        strict_time_match=False to the JS so it knows the fallback is
        permitted."""
        from src.booker import TockBooker

        cfg = _make_config(dry_run=False)
        booker = TockBooker(cfg, MagicMock(), MagicMock())

        page = AsyncMock()
        page.url = "https://www.exploretock.com/test/search?date=2026-05-08"
        page.is_closed = MagicMock(return_value=False)
        page.wait_for_selector = AsyncMock()
        page.evaluate = AsyncMock(return_value={
            "clicked": True,
            "text": "8:00 PM",
            "reason": "first-fallback",
        })

        button_locator = MagicMock()
        button_locator.count = AsyncMock(return_value=1)
        page.locator = MagicMock(return_value=button_locator)

        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )

        result = await booker._click_time_slot(page, slot)

        # Default behavior: JS-side fallback fires, wrapper accepts it
        assert result is True
        # And the JS was told strict mode is off so the fallback was permitted
        args, _ = page.evaluate.call_args
        js_arg = args[1] if len(args) > 1 else args[0]
        assert js_arg["strictTimeMatch"] is False

    @pytest.mark.asyncio
    async def test_warm_page_passes_strict_match_to_click_time_slot(self):
        """_book_single must propagate strict_time_match=True when given a
        warm page."""
        from src.booker import TockBooker

        cfg = _make_config(dry_run=False)
        booker = TockBooker(cfg, MagicMock(), MagicMock())

        warm_page = AsyncMock()
        warm_page.url = "https://www.exploretock.com/test/search?date=2026-05-08"
        warm_page.is_closed = MagicMock(return_value=False)
        warm_page.close = AsyncMock()
        warm_page.evaluate = AsyncMock()

        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        booking_won = asyncio.Event()

        click_calls = []

        async def fake_click_time_slot(page, slot_arg, strict_time_match=False):
            click_calls.append({"strict": strict_time_match})
            return False  # let _book_single bail out

        with patch.object(booker, "_click_time_slot",
                          side_effect=fake_click_time_slot), \
             patch.object(booker, "_booking_screenshot", AsyncMock()):
            await booker._book_single(slot, booking_won, warm_page=warm_page)

        assert click_calls and click_calls[0]["strict"] is True, (
            "When using a warm page, _click_time_slot must be called with "
            "strict_time_match=True"
        )

    @pytest.mark.asyncio
    async def test_fresh_page_does_not_request_strict_match(self):
        """When booker takes the fresh-navigation path, strict_time_match
        defaults to False so existing booking flows continue to work."""
        from src.booker import TockBooker

        cfg = _make_config(dry_run=False)
        booker = TockBooker(cfg, MagicMock(), MagicMock())

        fresh_page = AsyncMock()
        fresh_page.url = "https://www.exploretock.com/test/search?date=2026-05-08"
        fresh_page.is_closed = MagicMock(return_value=False)
        fresh_page.close = AsyncMock()
        fresh_page.goto = AsyncMock()
        fresh_page.evaluate = AsyncMock()
        fresh_page.wait_for_selector = AsyncMock()
        booker.browser = MagicMock()
        booker.browser.new_page = AsyncMock(return_value=fresh_page)

        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        booking_won = asyncio.Event()

        click_calls = []

        async def fake_click_time_slot(page, slot_arg, strict_time_match=False):
            click_calls.append({"strict": strict_time_match})
            return False

        with patch.object(booker, "_wait_for_selector",
                          AsyncMock(return_value=True)), \
             patch.object(booker, "_click_calendar_day",
                          AsyncMock(return_value=True)), \
             patch.object(booker, "_click_time_slot",
                          side_effect=fake_click_time_slot), \
             patch.object(booker, "_booking_screenshot", AsyncMock()):
            await booker._book_single(slot, booking_won, warm_page=None)

        assert click_calls and click_calls[0]["strict"] is False


class TestPostCheckCleanupSurvivesException:
    """Codex MEDIUM: an exception during slots_found() or
    book_best_slot_race() must NOT leak the handoff page."""

    @pytest.mark.asyncio
    async def test_handoff_page_closed_when_booker_raises(self):
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        unclaimed = MagicMock()
        unclaimed.is_closed = MagicMock(return_value=False)
        unclaimed.close = AsyncMock()

        monitor, _, _ = _build_monitor(
            dry_run=False,
            slots=[slot],
            sniper_active=False,
            pop_handoff_results=[unclaimed],
        )

        async def boom(*args, **kwargs):
            raise RuntimeError("simulated booker crash")

        monitor.booker.book_best_slot_race = AsyncMock(side_effect=boom)

        # poll() should swallow the exception or re-raise — either way the
        # cleanup loop must run before unwinding
        try:
            await monitor.poll()
        except RuntimeError:
            pass

        unclaimed.close.assert_called_once(), (
            "Handoff page must be closed even when book_best_slot_race raises"
        )

    @pytest.mark.asyncio
    async def test_parked_page_closed_when_slots_found_raises(self):
        """If slots_found raises BEFORE warm_pages is drained, any pages
        parked in checker._handoff_pages must be closed via close_handoff_pages.
        Uses a real parked page (not just an assert_called check) so a future
        bug that drops the await would also be caught."""
        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )
        parked = MagicMock()
        parked.is_closed = MagicMock(return_value=False)
        parked.close = AsyncMock()

        monitor, checker, notifier = _build_monitor(
            dry_run=False,
            slots=[slot],
            sniper_active=False,
            pop_handoff_results=[],  # exception fires before drain
        )

        # Real close_handoff_pages-like behavior: close the parked page.
        # This catches a future bug where the call is fire-and-forget
        # (no await) — the AsyncMock would record the call but never run
        # the simulated close.
        async def fake_close_handoff_pages():
            if not parked.is_closed():
                await parked.close()

        checker.close_handoff_pages = AsyncMock(side_effect=fake_close_handoff_pages)
        notifier.slots_found.side_effect = RuntimeError("notifier crash")

        try:
            await monitor.poll()
        except RuntimeError:
            pass

        # If the await was dropped, the side_effect coroutine wouldn't run
        # and parked.close wouldn't have been awaited.
        checker.close_handoff_pages.assert_awaited_once()
        parked.close.assert_awaited_once(), (
            "Parked handoff page must be closed when slots_found raises"
        )


class TestHandoffOverwriteCloseOldEntry:
    """Codex MEDIUM: parking a page in _handoff_pages[date_str] when an entry
    already exists must close the old page, not silently leak it."""

    @pytest.mark.asyncio
    async def test_old_page_closed_when_overwritten(self):
        checker = _make_checker()
        old_page = MagicMock()
        old_page.is_closed = MagicMock(return_value=False)
        old_page.close = AsyncMock()
        checker._handoff_pages["2026-05-08"] = old_page

        new_page = _make_mock_page()
        checker.browser.new_page = AsyncMock(return_value=new_page)

        slot = AvailableSlot(
            slot_date=date(2026, 5, 8),
            slot_time="5:00 PM",
            day_of_week="Friday",
        )

        with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
             patch.object(checker, "_click_day", AsyncMock(return_value=True)), \
             patch.object(checker, "_collect_slots_multi",
                          AsyncMock(return_value=[slot])):
            await checker._check_date(
                date(2026, 5, 8),
                keep_page=False,
                retain_found_page=True,
            )

        old_page.close.assert_called_once(), (
            "Old handoff page for the same date must be closed before being "
            "replaced — otherwise it leaks"
        )
        assert checker._handoff_pages.get("2026-05-08") is new_page
