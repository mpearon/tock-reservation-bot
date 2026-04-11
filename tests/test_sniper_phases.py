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
    """100% errors at sniper_age=90s MUST degrade to sequential mode."""
    monitor = _make_monitor()
    monitor._SNIPER_ERROR_THRESH = 0.0  # any error triggers switch
    monitor._apply_adaptive_switching(sniper_age=90.0)
    assert monitor._sniper_concurrent is False


def test_boundary_exactly_60s():
    """sniper_age=60.0 is post-release — errors should count."""
    monitor = _make_monitor()
    monitor._SNIPER_ERROR_THRESH = 0.0
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
