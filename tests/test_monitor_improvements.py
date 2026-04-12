"""
Tests for monitor improvements:
  - Sniper hold countdown: logs every 10s during the hold guard wait
  - Discord alert on sniper end with 0 slots found
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from src.config import Config
from src.monitor import TockMonitor, _SNIPER_HOLD_SEC
from src.notifier import Notifier


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="test@example.com",
        tock_password="pass",
        card_cvc="123",
        discord_webhook_url="https://discord.com/api/webhooks/test",
        headless=True,
        dry_run=True,
        restaurant_slug="test-restaurant",
        party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=[],
        preferred_time="17:00",
        scan_weeks=2,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Wednesday", "Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_monitor(config=None):
    cfg = config or _make_config()
    browser = MagicMock()
    browser.warm_session = AsyncMock(return_value=True)
    checker = MagicMock()
    checker.check_all = AsyncMock(return_value=[])
    checker.close_sniper_pages = AsyncMock()
    checker.last_checks = 0
    checker.last_errors = 0
    checker.clear_normal_skip_cache = MagicMock()
    checker.refresh_screenshot_count = MagicMock()
    notifier = MagicMock()
    notifier.poll_start = MagicMock()
    notifier.no_slots_found = MagicMock()
    notifier.slots_found = MagicMock()
    notifier.sniper_mode_active = MagicMock()
    notifier.sniper_mode_ended = MagicMock()
    notifier.error = MagicMock()
    notifier.dry_run_would_book = MagicMock()
    tracker = MagicMock()
    with patch("src.monitor.TockBooker"):
        monitor = TockMonitor(cfg, browser, checker, notifier, tracker)
    return monitor


# ---------------------------------------------------------------------------
# Countdown hold tests
# ---------------------------------------------------------------------------

class TestSniperHoldCountdown:
    def test_monitor_has_countdown_hold_method(self):
        """TockMonitor must expose _countdown_hold coroutine."""
        monitor = _make_monitor()
        assert hasattr(monitor, "_countdown_hold")
        assert asyncio.iscoroutinefunction(monitor._countdown_hold)

    @pytest.mark.asyncio
    async def test_countdown_hold_sleeps_total_duration(self):
        """_countdown_hold should sleep approximately secs_to_sniper total."""
        monitor = _make_monitor()
        slept = []
        async def fake_sleep(s):
            slept.append(s)
        with patch("src.monitor.asyncio.sleep", side_effect=fake_sleep):
            await monitor._countdown_hold(25.0)
        assert abs(sum(slept) - 25.0) < 0.01

    @pytest.mark.asyncio
    async def test_countdown_hold_logs_every_10s(self):
        """_countdown_hold should emit log entries during wait."""
        monitor = _make_monitor()
        log_messages = []

        async def fake_sleep(s):
            pass

        with patch("src.monitor.asyncio.sleep", side_effect=fake_sleep), \
             patch("src.monitor.logger") as mock_logger:
            await monitor._countdown_hold(35.0)
            # Should have logged at least once for a 35s countdown
            assert mock_logger.info.called or mock_logger.debug.called

    @pytest.mark.asyncio
    async def test_countdown_hold_short_wait_single_sleep(self):
        """For waits under 10s, should do a single sleep with no extra log."""
        monitor = _make_monitor()
        slept = []
        async def fake_sleep(s):
            slept.append(s)
        with patch("src.monitor.asyncio.sleep", side_effect=fake_sleep):
            await monitor._countdown_hold(5.0)
        assert len(slept) == 1
        assert slept[0] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_countdown_hold_chunks_at_10s_intervals(self):
        """A 35s hold should sleep in chunks: 10, 10, 10, 5."""
        monitor = _make_monitor()
        slept = []
        async def fake_sleep(s):
            slept.append(s)
        with patch("src.monitor.asyncio.sleep", side_effect=fake_sleep), \
             patch("src.monitor.logger"):
            await monitor._countdown_hold(35.0)
        assert len(slept) == 4
        assert slept[0] == pytest.approx(10.0)
        assert slept[1] == pytest.approx(10.0)
        assert slept[2] == pytest.approx(10.0)
        assert slept[3] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Sniper end Discord alert tests
# ---------------------------------------------------------------------------

class TestSniperEndDiscordAlert:
    def test_sniper_mode_ended_fires_discord_on_zero_slots(self):
        """sniper_mode_ended(0) must call _fire to send a Discord embed."""
        config = _make_config()
        notifier = Notifier(config)

        with patch.object(notifier, "_fire") as mock_fire:
            notifier.sniper_mode_ended(slots_found=0)
            mock_fire.assert_called_once()

    def test_sniper_mode_ended_zero_slots_embed_mentions_zero(self):
        """The Discord embed should clearly state no slots were found."""
        config = _make_config()
        notifier = Notifier(config)

        with patch.object(notifier, "_fire") as mock_fire:
            notifier.sniper_mode_ended(slots_found=0)
            args, kwargs = mock_fire.call_args
            full_call = str(args) + str(kwargs)
            # Should mention 0 slots or "no slots" in the embed text
            assert "0" in full_call or "no slot" in full_call.lower()

    def test_sniper_mode_ended_with_slots_does_not_fire_discord(self):
        """sniper_mode_ended(N) where N > 0 should NOT trigger a Discord embed."""
        config = _make_config()
        notifier = Notifier(config)

        with patch.object(notifier, "_fire") as mock_fire:
            notifier.sniper_mode_ended(slots_found=3)
            mock_fire.assert_not_called()

    def test_sniper_mode_ended_always_logs_to_console(self):
        """Console log must fire regardless of Discord status."""
        config = _make_config()
        notifier = Notifier(config)

        with patch("src.notifier.logger") as mock_logger:
            notifier.sniper_mode_ended(slots_found=0)
            mock_logger.info.assert_called()

        with patch("src.notifier.logger") as mock_logger:
            notifier.sniper_mode_ended(slots_found=5)
            mock_logger.info.assert_called()

    def test_sniper_mode_ended_zero_uses_grey_or_red_color(self):
        """Zero-slot end embed should use a neutral/alert color, not green."""
        config = _make_config()
        notifier = Notifier(config)
        _GREEN = 0x2ECC71

        with patch.object(notifier, "_fire") as mock_fire:
            notifier.sniper_mode_ended(slots_found=0)
            _, kwargs = mock_fire.call_args
            color = kwargs.get("color", None)
            if color is None and mock_fire.call_args[0]:
                # Positional: title, description, color, ...
                color = mock_fire.call_args[0][2] if len(mock_fire.call_args[0]) > 2 else None
            assert color != _GREEN, "Zero-slot sniper end should not use green"


# ---------------------------------------------------------------------------
# Clear normal skip cache on sniper activation
# ---------------------------------------------------------------------------

class TestSniperActivationClearsNormalCache:
    @pytest.mark.asyncio
    async def test_clear_normal_skip_called_on_sniper_start(self):
        """When sniper mode activates, clear_normal_skip_cache() should be called."""
        monitor = _make_monitor()

        # Simulate sniper window just opened
        monitor._sniper_active = False
        with patch.object(monitor, "_sniper_window_info", return_value="20:10"), \
             patch.object(monitor, "_seconds_until_next_sniper", return_value=None):
            monitor._get_poll_interval()

        monitor.checker.clear_normal_skip_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_screenshot_count_called_on_sniper_start(self):
        """When sniper mode activates, refresh_screenshot_count() should be called."""
        monitor = _make_monitor()
        monitor._sniper_active = False

        with patch.object(monitor, "_sniper_window_info", return_value="20:10"), \
             patch.object(monitor, "_seconds_until_next_sniper", return_value=None):
            monitor._get_poll_interval()

        monitor.checker.refresh_screenshot_count.assert_called_once()
