"""Tests for sniper phase logic: pre-release error gating and two-phase scan."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.checker import AvailabilityChecker
from src.config import Config
from src.monitor import TockMonitor


def _make_monitor():
    """Minimal TockMonitor wired with mock dependencies."""
    config = Config(
        tock_email="test@test.com",
        tock_password="pw",
        restaurant_slug="test-slug",
        party_size=2,
        preferred_days=["Friday"],
        fallback_days=[],
        preferred_time="17:00",
        scan_weeks=4,
        dry_run=True,
        headless=True,
        sniper_days=["Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        debug_screenshots=False,
        discord_webhook_url="",
        card_cvc="",
    )
    browser = MagicMock()
    checker = MagicMock()
    checker.last_errors = 6
    checker.last_checks = 6
    notifier = MagicMock()
    tracker = MagicMock()
    monitor = TockMonitor(config, browser, checker, notifier, tracker)
    monitor._sniper_active = True
    monitor._sniper_concurrent = True
    return monitor


def _make_checker():
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    tracker = MagicMock()
    tracker.record_deferred = MagicMock()
    tracker.record = MagicMock()
    return AvailabilityChecker(config, browser, tracker)


# ---------------------------------------------------------------------------
# Task 1: adaptive degradation gating
# ---------------------------------------------------------------------------

def test_no_degradation_before_release():
    """100% errors at sniper_age=30s must NOT change concurrent mode."""
    monitor = _make_monitor()
    monitor._apply_adaptive_switching(sniper_age=30.0)
    assert monitor._sniper_concurrent is True


def test_degradation_after_release():
    """Sustained 100%-error polls at sniper_age >= 60s MUST degrade to
    sequential mode. Codex HIGH (5/22 incident): the flip requires a full
    rolling window, so we feed _SNIPER_WINDOW_SIZE samples to confirm the
    degradation path still works once the gate is satisfied."""
    monitor = _make_monitor()
    monitor._SNIPER_ERROR_THRESH = 0.0  # any error triggers switch
    for _ in range(monitor._SNIPER_WINDOW_SIZE):
        monitor._apply_adaptive_switching(sniper_age=90.0)
    assert monitor._sniper_concurrent is False


def test_boundary_exactly_60s():
    """sniper_age=60.0 is post-release — errors should count once the
    rolling window has filled."""
    monitor = _make_monitor()
    monitor._SNIPER_ERROR_THRESH = 0.0
    for _ in range(monitor._SNIPER_WINDOW_SIZE):
        monitor._apply_adaptive_switching(sniper_age=60.0)
    assert monitor._sniper_concurrent is False


def test_recovery_still_works_post_release():
    """After degradation, 3 clean polls restore concurrent mode."""
    monitor = _make_monitor()
    monitor._sniper_concurrent = False
    monitor._sniper_sequential_clean = 0
    monitor.checker.last_errors = 0
    monitor.checker.last_checks = 6
    for _ in range(monitor._SNIPER_RECOVER_POLLS):
        monitor._apply_adaptive_switching(sniper_age=120.0)
    assert monitor._sniper_concurrent is True


# ---------------------------------------------------------------------------
# Task 2: two-phase sniper pre-release skip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_release_skips_calendar_scan():
    """check_all with sniper_age < 60s returns [] without calling _check_date."""
    checker = _make_checker()
    with patch.object(checker, '_check_date', new_callable=AsyncMock) as mock_check:
        result = await checker.check_all(
            concurrent=True,
            keep_pages=True,
            sniper_window_age_sec=30.0,
        )
    assert result == []
    mock_check.assert_not_called()


@pytest.mark.asyncio
async def test_pre_release_resets_error_counters():
    """Pre-release return clears last_errors and last_checks (no phantom errors)."""
    checker = _make_checker()
    checker.last_errors = 99
    checker.last_checks = 99
    with patch.object(checker, '_check_date', new_callable=AsyncMock):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=10.0
        )
    assert checker.last_errors == 0
    assert checker.last_checks == 0


@pytest.mark.asyncio
async def test_post_release_proceeds_to_scan():
    """check_all with sniper_age >= 60s calls _check_date (normal aggressive mode)."""
    checker = _make_checker()
    with patch.object(checker, '_check_date', new_callable=AsyncMock, return_value=[]) as mock_check:
        await checker.check_all(
            concurrent=True,
            keep_pages=True,
            sniper_window_age_sec=61.0,
        )
    assert mock_check.call_count > 0


# ---------------------------------------------------------------------------
# Real two-phase DOM closure: Phase 1 (preferred) short-circuits Phase 2
# (fallback), and _wait_for_calendar is always restored.
#
# These drive the REAL _two_phase_dom_scan closure inside check_all (only
# _check_date is patched, not the closure itself) so the extracted-closure
# refactor is exercised end-to-end — the core replay tests stub _check_date
# but never prove the two-phase + finally-restore wiring.
# ---------------------------------------------------------------------------


def _make_two_phase_checker():
    """Checker with DISTINCT preferred (Friday) + fallback (Monday) days and
    replay OFF, so check_all takes the real `return await _two_phase_dom_scan()`
    branch. _get_target_dates is stubbed to yield EXACTLY one preferred (a
    Friday) and one fallback (a Monday) date so the two-phase assertions are
    fully deterministic regardless of today's weekday."""
    from datetime import date, timedelta

    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=["Monday"],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    checker = AvailabilityChecker(config, MagicMock(), MagicMock())
    # replay must be OFF so we hit the plain two-phase DOM branch.
    assert getattr(checker.config, "use_calendar_replay", False) is False

    base = date.today() + timedelta(days=14)
    friday = base + timedelta(days=(4 - base.weekday()) % 7)   # next Friday
    monday = base + timedelta(days=(0 - base.weekday()) % 7)   # next Monday

    def _targets(days, sniper_mode=False):
        if "Friday" in days:
            return [friday]
        if "Monday" in days:
            return [monday]
        return []

    checker._get_target_dates = _targets  # type: ignore[method-assign]
    return checker


@pytest.mark.asyncio
async def test_two_phase_preferred_short_circuits_fallback():
    """Real two-phase closure: when Phase 1 (preferred=Friday) yields a slot,
    Phase 2 (fallback=Monday) must NOT be scanned. Asserts on which weekdays
    _check_date was actually invoked for."""
    from src.checker import AvailableSlot

    checker = _make_two_phase_checker()

    weekdays_checked = []

    async def fake_check_date(target_date, **kwargs):
        weekdays_checked.append(target_date.weekday())  # Mon=0 .. Sun=6
        if target_date.weekday() == 4:  # Friday — preferred has a slot
            return [AvailableSlot(
                slot_date=target_date, slot_time="5:00 PM",
                day_of_week="Friday",
            )]
        return []

    with patch.object(checker, "_check_date", side_effect=fake_check_date):
        result = await checker.check_all(
            concurrent=False, keep_pages=True, sniper_window_age_sec=120.0,
        )

    assert result, "Phase 1 should have returned the Friday slot"
    assert 4 in weekdays_checked, "Friday (preferred) must have been scanned"
    assert 0 not in weekdays_checked, (
        "Monday (fallback) must NOT be scanned once preferred has a slot "
        f"(weekdays checked: {sorted(set(weekdays_checked))})"
    )
    # last_checks reflects ONLY the preferred dates (Phase 2 skipped).
    assert checker.last_checks == sum(1 for w in weekdays_checked if w == 4)


@pytest.mark.asyncio
async def test_two_phase_empty_preferred_runs_fallback():
    """Real two-phase closure: when Phase 1 (preferred) is empty, Phase 2
    (fallback=Monday) IS scanned and its slots are returned."""
    from src.checker import AvailableSlot

    checker = _make_two_phase_checker()
    weekdays_checked = []

    async def fake_check_date(target_date, **kwargs):
        weekdays_checked.append(target_date.weekday())
        if target_date.weekday() == 0:  # Monday — fallback has a slot
            return [AvailableSlot(
                slot_date=target_date, slot_time="7:00 PM",
                day_of_week="Monday",
            )]
        return []  # Friday (preferred) empty

    with patch.object(checker, "_check_date", side_effect=fake_check_date):
        result = await checker.check_all(
            concurrent=False, keep_pages=True, sniper_window_age_sec=120.0,
        )

    assert 4 in weekdays_checked, "Friday (preferred) must have been scanned"
    assert 0 in weekdays_checked, (
        "Monday (fallback) MUST be scanned when preferred is empty"
    )
    assert result, "fallback slot must be returned"
    assert all(s.slot_date.weekday() == 0 for s in result)


@pytest.mark.asyncio
async def test_two_phase_restores_wait_for_calendar_on_success():
    """check_all monkeypatches self._wait_for_calendar to a counting wrapper
    inside the method; it MUST be restored (finally) so subsequent polls
    don't stack wrappers. (Bound methods aren't identity-stable across
    attribute reads, so compare the underlying function and assert the
    in-method `_counting_wait` wrapper is no longer installed.)"""
    checker = _make_two_phase_checker()

    async def fake_check_date(target_date, **kwargs):
        return []

    with patch.object(checker, "_check_date", side_effect=fake_check_date):
        await checker.check_all(
            concurrent=False, keep_pages=True, sniper_window_age_sec=120.0,
        )

    restored = checker._wait_for_calendar
    assert getattr(restored, "__func__", restored) is (
        AvailabilityChecker._wait_for_calendar
    ), "_wait_for_calendar must be restored to the original method after scan"
    assert getattr(restored, "__name__", "") != "_counting_wait", (
        "the in-method counting wrapper must NOT remain installed"
    )


@pytest.mark.asyncio
async def test_two_phase_restores_wait_for_calendar_on_exception():
    """Even when the DOM scan raises, the _wait_for_calendar counting wrapper
    must be restored in the finally block (otherwise every crashed poll
    leaves a stacked wrapper that double-counts errors forever)."""
    checker = _make_two_phase_checker()

    class _Boom(RuntimeError):
        pass

    async def crashing_check_date(target_date, **kwargs):
        raise _Boom("simulated browser disconnect")

    with patch.object(checker, "_check_date", side_effect=crashing_check_date):
        with pytest.raises(_Boom):
            # sequential so the exception propagates out of _scan_dates.
            await checker.check_all(
                concurrent=False, keep_pages=True, sniper_window_age_sec=120.0,
            )

    restored = checker._wait_for_calendar
    assert getattr(restored, "__func__", restored) is (
        AvailabilityChecker._wait_for_calendar
    ), "_wait_for_calendar must be restored even when the scan raises"
    assert getattr(restored, "__name__", "") != "_counting_wait", (
        "the in-method counting wrapper must NOT remain installed after a crash"
    )


def test_slots_found_discord_suppressed_in_sniper(caplog):
    """slots_found(sniper_mode=True) must not call _fire() (Discord)."""
    from src.notifier import Notifier
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False,
        discord_webhook_url="https://discord.example.com/webhook",
        card_cvc="",
    )
    notifier = Notifier(config)
    fire_calls = []
    notifier._fire = lambda *a, **kw: fire_calls.append((a, kw))

    from src.checker import AvailableSlot
    from datetime import date
    slots = [AvailableSlot(
        slot_date=date(2026, 4, 17), slot_time="5:00 PM", day_of_week="Friday"
    )]
    notifier.slots_found(slots, sniper_mode=True)

    assert fire_calls == [], "Discord _fire must not be called in sniper mode"


def test_slots_found_discord_sent_outside_sniper():
    """slots_found(sniper_mode=False) MUST call _fire() (Discord notification)."""
    from src.notifier import Notifier
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False,
        discord_webhook_url="https://discord.example.com/webhook",
        card_cvc="",
    )
    notifier = Notifier(config)
    fire_calls = []
    notifier._fire = lambda *a, **kw: fire_calls.append((a, kw))

    from src.checker import AvailableSlot
    from datetime import date
    slots = [AvailableSlot(
        slot_date=date(2026, 4, 17), slot_time="5:00 PM", day_of_week="Friday"
    )]
    notifier.slots_found(slots, sniper_mode=False)

    assert len(fire_calls) == 1, "Discord _fire must be called outside sniper mode"
