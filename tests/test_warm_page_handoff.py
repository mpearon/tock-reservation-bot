"""Tests for warm page handoff from checker to booker."""
import pytest
from datetime import date
from unittest.mock import MagicMock
from src.checker import AvailabilityChecker
from src.config import Config


def _make_config() -> Config:
    return Config(
        tock_email="t@e.com", tock_password="p", card_cvc="123",
        discord_webhook_url="", headless=True, dry_run=True,
        restaurant_slug="test", party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2,
        release_window_days=["Monday"], release_window_start="09:00",
        release_window_end="11:00", sniper_days=["Friday"],
        sniper_times=["19:59"], sniper_duration_min=11, sniper_interval_sec=3,
    )


class TestWarmPageHandoff:
    def test_get_warm_page_returns_open_page(self) -> None:
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=False)
        checker._sniper_pages["2026-04-17"] = mock_page
        assert checker.get_warm_page("2026-04-17") is mock_page

    def test_get_warm_page_returns_none_for_closed(self) -> None:
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=True)
        checker._sniper_pages["2026-04-17"] = mock_page
        assert checker.get_warm_page("2026-04-17") is None

    def test_get_warm_page_returns_none_for_missing(self) -> None:
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        assert checker.get_warm_page("2026-04-17") is None

    def test_sniper_pages_dict_exists(self) -> None:
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        assert isinstance(checker._sniper_pages, dict)
