"""
Tests for skip-date cache during normal (non-sniper) polling.

The normal skip cache persists across polls for dates whose calendar day
was not visible (likely beyond the booking window). Entries expire after
NORMAL_SKIP_TTL_SEC so dates are eventually retried. The cache is also
cleared when sniper mode activates so all dates are retried fresh.
"""
import time
import pytest
from unittest.mock import MagicMock

from src.checker import AvailabilityChecker, NORMAL_SKIP_TTL_SEC
from src.config import Config


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="t@e.com", tock_password="p", card_cvc="123",
        discord_webhook_url="", headless=True, dry_run=True,
        restaurant_slug="test", party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2,
        release_window_days=["Monday"], release_window_start="09:00",
        release_window_end="11:00", sniper_days=["Friday"],
        sniper_times=["19:59"], sniper_duration_min=11, sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestNormalSkipCache:
    def test_constant_ttl_value(self):
        """NORMAL_SKIP_TTL_SEC should be a positive integer (20 minutes)."""
        assert NORMAL_SKIP_TTL_SEC > 0
        assert isinstance(NORMAL_SKIP_TTL_SEC, int)

    def test_normal_skip_cache_initially_empty(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        assert len(checker._normal_skip_dates) == 0

    def test_add_date_to_normal_skip_cache(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        checker._add_to_normal_skip("2026-04-18")
        assert "2026-04-18" in checker._normal_skip_dates

    def test_should_skip_date_within_ttl(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        checker._add_to_normal_skip("2026-04-18")
        assert checker._should_skip_normal("2026-04-18") is True

    def test_should_not_skip_unknown_date(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        assert checker._should_skip_normal("2026-04-18") is False

    def test_skip_expires_after_ttl(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        # Backdate entry to simulate expired TTL
        checker._normal_skip_dates["2026-04-18"] = (
            time.monotonic() - NORMAL_SKIP_TTL_SEC - 1
        )
        assert checker._should_skip_normal("2026-04-18") is False

    def test_expired_entry_is_removed_on_check(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        checker._normal_skip_dates["2026-04-18"] = (
            time.monotonic() - NORMAL_SKIP_TTL_SEC - 1
        )
        checker._should_skip_normal("2026-04-18")
        assert "2026-04-18" not in checker._normal_skip_dates

    def test_skip_still_valid_just_before_ttl(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        # Entry added 1 second ago — still valid
        checker._normal_skip_dates["2026-04-18"] = (
            time.monotonic() - 1
        )
        assert checker._should_skip_normal("2026-04-18") is True

    def test_clear_normal_skip_cache(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        checker._add_to_normal_skip("2026-04-18")
        checker._add_to_normal_skip("2026-04-19")
        checker.clear_normal_skip_cache()
        assert len(checker._normal_skip_dates) == 0

    def test_multiple_dates_independently_cached(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        checker._add_to_normal_skip("2026-04-18")
        checker._add_to_normal_skip("2026-04-25")
        assert checker._should_skip_normal("2026-04-18") is True
        assert checker._should_skip_normal("2026-04-25") is True
        assert checker._should_skip_normal("2026-04-19") is False

    def test_re_add_refreshes_ttl(self):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        # Add then immediately expire it
        checker._normal_skip_dates["2026-04-18"] = (
            time.monotonic() - NORMAL_SKIP_TTL_SEC - 1
        )
        # Re-add should refresh the TTL
        checker._add_to_normal_skip("2026-04-18")
        assert checker._should_skip_normal("2026-04-18") is True
