"""Pin _SNIPER_ERROR_THRESH at 30%.

5/22 lesson: 20% means 3-in-14 timeouts flips the mode. Cloudflare /
Tock-CDN noise commonly produces 3/14 = 21% during peak release traffic
even on healthy sessions, causing the adaptive logic to flap between
concurrent and sequential every few polls (observed: 4 switches in the
17:00 window, 3 in the 20:00 window, both Fri 5/22). 30% requires
5-in-14 = 36% sustained over 3 polls before flipping, which matches the
actual Tock-side outage signal.
"""
from unittest.mock import MagicMock

from src.config import Config
from src.monitor import TockMonitor


def _config() -> Config:
    return Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )


def _make_monitor() -> TockMonitor:
    return TockMonitor(
        _config(), MagicMock(), MagicMock(), MagicMock(), MagicMock(),
    )


def test_default_threshold_is_30_percent():
    """Production default must be 0.30 — 5+/14 errors sustained over the
    3-poll window before flipping. 20% (legacy) flipped on 3/14 which
    is healthy Tock-CDN noise at release time."""
    monitor = _make_monitor()
    assert monitor._SNIPER_ERROR_THRESH == 0.30


def test_3_errors_in_14_does_not_flip():
    """At 3/14 = 21% sustained, mode must stay concurrent under the new
    threshold (was a false-positive under the old 20% rule)."""
    monitor = _make_monitor()
    monitor._sniper_active = True
    monitor.checker.last_errors = 3
    monitor.checker.last_checks = 14
    for _ in range(monitor._SNIPER_WINDOW_SIZE):
        monitor._apply_adaptive_switching(sniper_age=120.0)
    assert monitor._sniper_concurrent is True


def test_5_errors_in_14_does_flip():
    """At 5/14 = 36% sustained, mode must flip to sequential — this is the
    'real Cloudflare problem' regime worth degrading for."""
    monitor = _make_monitor()
    monitor._sniper_active = True
    monitor.checker.last_errors = 5
    monitor.checker.last_checks = 14
    for _ in range(monitor._SNIPER_WINDOW_SIZE):
        monitor._apply_adaptive_switching(sniper_age=120.0)
    assert monitor._sniper_concurrent is False


# ---------------------------------------------------------------------------
# Codex HIGH: an isolated bad first poll must not flip modes
# ---------------------------------------------------------------------------

def test_single_over_threshold_poll_does_not_flip():
    """One isolated 5/14 (36%) poll must NOT switch to sequential — the
    threshold semantics promise 'sustained over the rolling window'. The
    5/22 mode-flap pattern came from acting on 1-sample averages on the
    first post-release poll. Codex HIGH finding."""
    monitor = _make_monitor()
    monitor._sniper_active = True
    monitor.checker.last_errors = 5
    monitor.checker.last_checks = 14
    monitor._apply_adaptive_switching(sniper_age=120.0)
    assert monitor._sniper_concurrent is True, (
        "Single over-threshold poll flipped modes; must wait for a full "
        "rolling window before degrading"
    )


def test_two_over_threshold_polls_does_not_flip():
    """Two over-threshold polls is still below the full 3-sample window —
    don't flip yet. Confirms the gating is exactly N samples, not N-1."""
    monitor = _make_monitor()
    monitor._sniper_active = True
    monitor.checker.last_errors = 5
    monitor.checker.last_checks = 14
    monitor._apply_adaptive_switching(sniper_age=120.0)
    monitor._apply_adaptive_switching(sniper_age=120.0)
    assert monitor._sniper_concurrent is True


def test_three_over_threshold_polls_flips():
    """Third over-threshold poll completes the window and triggers the flip."""
    monitor = _make_monitor()
    monitor._sniper_active = True
    monitor.checker.last_errors = 5
    monitor.checker.last_checks = 14
    for _ in range(3):
        monitor._apply_adaptive_switching(sniper_age=120.0)
    assert monitor._sniper_concurrent is False
