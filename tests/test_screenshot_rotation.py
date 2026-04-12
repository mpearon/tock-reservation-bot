"""
Tests for debug screenshot rotation and error separation.

Normal screenshots: keep last 50, delete oldest when over limit.
Error screenshots: saved to errors/ subfolder, NEVER deleted.
Before sniper mode: refresh count from disk so rotation stays accurate.
"""
import os
import pytest
from unittest.mock import MagicMock, patch

from src.checker import (
    AvailabilityChecker,
    MAX_DEBUG_SCREENSHOTS,
    _SCREENSHOT_DIR,
    _SCREENSHOT_ERROR_DIR,
    _prune_screenshots,
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


class TestScreenshotConstants:
    def test_max_screenshots_is_50(self):
        assert MAX_DEBUG_SCREENSHOTS == 50

    def test_error_dir_is_subdirectory_of_main(self):
        assert _SCREENSHOT_ERROR_DIR.startswith(_SCREENSHOT_DIR)
        assert _SCREENSHOT_ERROR_DIR != _SCREENSHOT_DIR

    def test_error_dir_name_contains_errors(self):
        assert "error" in _SCREENSHOT_ERROR_DIR.lower()


class TestPruneScreenshots:
    def test_prune_deletes_oldest_when_over_limit(self, tmp_path):
        """When 55 screenshots exist, prune should delete the 5 oldest."""
        files = []
        for i in range(55):
            f = tmp_path / f"poll_{i:04d}.png"
            f.write_bytes(b"fake")
            os.utime(f, (i, i))  # mtime = i seconds since epoch
            files.append(f)

        _prune_screenshots(str(tmp_path), max_count=50)

        remaining = list(tmp_path.glob("*.png"))
        assert len(remaining) == 50

    def test_prune_removes_oldest_files(self, tmp_path):
        """The 5 oldest files should be gone after pruning 55 → 50."""
        for i in range(55):
            f = tmp_path / f"poll_{i:04d}.png"
            f.write_bytes(b"fake")
            os.utime(f, (i, i))

        _prune_screenshots(str(tmp_path), max_count=50)

        # Oldest 5 (indices 0–4) should be deleted
        for i in range(5):
            assert not (tmp_path / f"poll_{i:04d}.png").exists(), \
                f"Expected poll_{i:04d}.png to be deleted"

    def test_prune_keeps_newest_files(self, tmp_path):
        """The 50 newest files should survive pruning."""
        for i in range(55):
            f = tmp_path / f"poll_{i:04d}.png"
            f.write_bytes(b"fake")
            os.utime(f, (i, i))

        _prune_screenshots(str(tmp_path), max_count=50)

        # Newest 50 (indices 5–54) should survive
        assert (tmp_path / "poll_0054.png").exists()
        assert (tmp_path / "poll_0005.png").exists()

    def test_prune_does_nothing_when_under_limit(self, tmp_path):
        """10 files with a limit of 50 → no deletions."""
        for i in range(10):
            (tmp_path / f"poll_{i:04d}.png").write_bytes(b"fake")

        _prune_screenshots(str(tmp_path), max_count=50)

        assert len(list(tmp_path.glob("*.png"))) == 10

    def test_prune_does_nothing_when_at_limit(self, tmp_path):
        """Exactly 50 files → no deletions."""
        for i in range(50):
            (tmp_path / f"poll_{i:04d}.png").write_bytes(b"fake")

        _prune_screenshots(str(tmp_path), max_count=50)

        assert len(list(tmp_path.glob("*.png"))) == 50

    def test_prune_empty_dir_is_safe(self, tmp_path):
        """Empty directory should not raise."""
        _prune_screenshots(str(tmp_path), max_count=50)  # should not raise

    def test_prune_nonexistent_dir_is_safe(self, tmp_path):
        """Non-existent directory should not raise."""
        _prune_screenshots(str(tmp_path / "nonexistent"), max_count=50)


class TestRefreshScreenshotCount:
    def test_refresh_updates_count_from_disk(self, tmp_path):
        """refresh_screenshot_count should count actual .png files in the dir."""
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())

        # Patch the screenshot dir to our tmp dir
        for i in range(30):
            (tmp_path / f"poll_{i:04d}.png").write_bytes(b"fake")

        with patch("src.checker._SCREENSHOT_DIR", str(tmp_path)):
            checker.refresh_screenshot_count()

        assert checker._screenshot_count == 30

    def test_refresh_count_zero_when_dir_empty(self, tmp_path):
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        with patch("src.checker._SCREENSHOT_DIR", str(tmp_path)):
            checker.refresh_screenshot_count()
        assert checker._screenshot_count == 0
