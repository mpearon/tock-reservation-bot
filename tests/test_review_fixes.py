"""
Tests for bugs found during commit review:

1. Screenshot pruning/counting uses *.png glob — catches booking screenshots.
   Should use poll_*.png to only target normal poll screenshots.

2. release_detector.py has dead duplicate try/except with identical format strings.

3. _check_date general exception handler doesn't save error screenshot.
"""
import os
import pytest
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.checker import (
    AvailabilityChecker,
    _prune_screenshots,
    MAX_DEBUG_SCREENSHOTS,
)
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
        debug_screenshots=True,
    )
    defaults.update(overrides)
    return Config(**defaults)


# ---------------------------------------------------------------------------
# Bug 1: Screenshot pruning should only target poll_*.png, not all *.png
# ---------------------------------------------------------------------------

class TestScreenshotPruningScope:
    """_prune_screenshots must only delete poll_*.png, not booking or error pngs."""

    def test_prune_preserves_booking_screenshots(self, tmp_path):
        """Booking screenshots (booking_*.png) must survive pruning."""
        # Create 55 poll screenshots (over the 50 limit)
        for i in range(55):
            f = tmp_path / f"poll_{i:04d}.png"
            f.write_bytes(b"fake")
            os.utime(f, (i, i))

        # Create 3 booking screenshots
        for i in range(3):
            f = tmp_path / f"booking_{i:04d}.png"
            f.write_bytes(b"fake")
            os.utime(f, (100 + i, 100 + i))

        _prune_screenshots(str(tmp_path), max_count=50)

        # All 3 booking screenshots must survive
        booking_files = list(tmp_path.glob("booking_*.png"))
        assert len(booking_files) == 3, (
            f"Expected 3 booking screenshots to survive, got {len(booking_files)}"
        )

    def test_prune_only_deletes_poll_screenshots(self, tmp_path):
        """Only poll_*.png files should be pruned, never other patterns."""
        # Create 55 poll screenshots
        for i in range(55):
            f = tmp_path / f"poll_{i:04d}.png"
            f.write_bytes(b"fake")
            os.utime(f, (i, i))

        # Create other screenshot types that should survive
        other_files = [
            "booking_20260412_120000_01_start.png",
            "booking_20260412_120001_02_confirm.png",
            "custom_debug.png",
        ]
        for name in other_files:
            (tmp_path / name).write_bytes(b"fake")

        _prune_screenshots(str(tmp_path), max_count=50)

        # Exactly 50 poll screenshots remain
        poll_files = list(tmp_path.glob("poll_*.png"))
        assert len(poll_files) == 50

        # All other files survive
        for name in other_files:
            assert (tmp_path / name).exists(), f"{name} should not have been deleted"

    def test_prune_leaves_poll_files_under_limit(self, tmp_path):
        """With 40 poll files + 20 booking files, no pruning should happen."""
        for i in range(40):
            (tmp_path / f"poll_{i:04d}.png").write_bytes(b"fake")
        for i in range(20):
            (tmp_path / f"booking_{i:04d}.png").write_bytes(b"fake")

        _prune_screenshots(str(tmp_path), max_count=50)

        # Total count shouldn't change — 40 poll + 20 booking = 60 files
        assert len(list(tmp_path.glob("poll_*.png"))) == 40
        assert len(list(tmp_path.glob("booking_*.png"))) == 20


class TestRefreshScreenshotCount:
    """refresh_screenshot_count must only count poll_*.png, not booking screenshots."""

    def test_count_excludes_booking_screenshots(self, tmp_path):
        """Booking screenshots must not be included in the count."""
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())

        for i in range(10):
            (tmp_path / f"poll_{i:04d}.png").write_bytes(b"fake")
        for i in range(5):
            (tmp_path / f"booking_{i:04d}.png").write_bytes(b"fake")

        with patch("src.checker._SCREENSHOT_DIR", str(tmp_path)):
            checker.refresh_screenshot_count()

        # Should count only poll screenshots, not booking ones
        assert checker._screenshot_count == 10, (
            f"Expected 10 (poll only), got {checker._screenshot_count}"
        )


# ---------------------------------------------------------------------------
# Bug 2: release_detector duplicate try/except
# ---------------------------------------------------------------------------

class TestReleaseDetectorParsing:
    """The release detector must handle various date formats without duplicate code."""

    def test_parses_standard_date_format(self):
        """Standard format like 'March 13, 2026 at 8:00 PM' must parse."""
        from src.release_detector import _RELEASE_RE
        import pytz

        text = "All reservations sold out. New reservations will be released on March 13, 2026 at 8:00 PM PDT."
        match = _RELEASE_RE.search(text)
        assert match is not None
        date_str = match.group(1).strip()
        time_str = match.group(2).strip()
        # This must not raise — if it does, the duplicate except was masking it
        naive_dt = datetime.strptime(f"{date_str} {time_str}", "%B %d, %Y %I:%M %p")
        assert naive_dt.month == 3
        assert naive_dt.day == 13

    def test_parses_single_digit_day(self):
        """Single-digit day like 'March 3, 2026' must parse without fallback."""
        from src.release_detector import _RELEASE_RE

        text = "New reservations will be released on March 3, 2026 at 5:00 PM PT."
        match = _RELEASE_RE.search(text)
        assert match is not None
        date_str = match.group(1).strip()
        time_str = match.group(2).strip()
        naive_dt = datetime.strptime(f"{date_str} {time_str}", "%B %d, %Y %I:%M %p")
        assert naive_dt.day == 3

    def test_invalid_date_raises_cleanly(self):
        """An unparseable date string must raise ValueError, not silently retry."""
        with pytest.raises(ValueError):
            datetime.strptime("Marchtember 45, 2026 8:00 PM", "%B %d, %Y %I:%M %p")


# ---------------------------------------------------------------------------
# Bug 3: _check_date general exception should save error screenshot
# ---------------------------------------------------------------------------

class TestCheckDateErrorScreenshot:
    """_check_date must call _save_error_screenshot on unexpected exceptions."""

    @pytest.mark.asyncio
    async def test_error_screenshot_on_unexpected_exception(self):
        """When _check_date hits an unexpected exception, an error screenshot
        should be saved if debug_screenshots is enabled."""
        config = _make_config(debug_screenshots=True)
        browser = MagicMock()
        tracker = MagicMock()
        checker = AvailabilityChecker(config, browser, tracker)

        # Create a mock page that throws during navigation
        mock_page = AsyncMock()
        mock_page.is_closed.return_value = False
        mock_page.goto = AsyncMock(side_effect=RuntimeError("network failure"))
        mock_page.close = AsyncMock()
        browser.new_page = AsyncMock(return_value=mock_page)

        error_screenshots = []
        original_save = checker._save_error_screenshot

        async def tracking_save(page, date_str, label):
            error_screenshots.append((date_str, label))

        checker._save_error_screenshot = tracking_save

        result = await checker._check_date(date(2026, 4, 17), keep_page=False)

        assert result == []
        assert len(error_screenshots) >= 1, (
            "Expected _save_error_screenshot to be called on unexpected exception"
        )
        # The label should indicate it was an unexpected error
        assert any("unexpected" in label or "error" in label
                   for _, label in error_screenshots)

    @pytest.mark.asyncio
    async def test_no_error_screenshot_when_debug_disabled(self):
        """When debug_screenshots=False, no error screenshot on exception."""
        config = _make_config(debug_screenshots=False)
        browser = MagicMock()
        tracker = MagicMock()
        checker = AvailabilityChecker(config, browser, tracker)

        mock_page = AsyncMock()
        mock_page.is_closed.return_value = False
        mock_page.goto = AsyncMock(side_effect=RuntimeError("network failure"))
        mock_page.close = AsyncMock()
        browser.new_page = AsyncMock(return_value=mock_page)

        screenshot_called = []
        async def tracking_save(page, date_str, label):
            screenshot_called.append(True)

        checker._save_error_screenshot = tracking_save

        await checker._check_date(date(2026, 4, 17), keep_page=False)

        assert len(screenshot_called) == 0, (
            "Error screenshot should not be taken when debug_screenshots=False"
        )

    @pytest.mark.asyncio
    async def test_error_screenshot_on_sniper_page_failure(self):
        """In sniper mode, error screenshot is saved and broken page is cleaned up."""
        config = _make_config(debug_screenshots=True)
        browser = MagicMock()
        tracker = MagicMock()
        checker = AvailabilityChecker(config, browser, tracker)

        # Use a non-async mock for is_closed (it's a sync method on Page)
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.reload = AsyncMock(side_effect=RuntimeError("page crashed"))
        mock_page.close = AsyncMock()

        date_str = "2026-04-17"
        checker._sniper_pages[date_str] = mock_page

        error_screenshots = []
        async def tracking_save(page, ds, label):
            error_screenshots.append((ds, label))

        checker._save_error_screenshot = tracking_save

        result = await checker._check_date(date(2026, 4, 17), keep_page=True)

        assert result == []
        assert len(error_screenshots) >= 1
        # Broken page should have been removed from sniper_pages
        assert date_str not in checker._sniper_pages
