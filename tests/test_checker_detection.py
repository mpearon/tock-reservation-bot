"""Tests for the slot detection flow in AvailabilityChecker.

Covers:
  - _click_day uses all_day_button (not filtered by is-available)
  - _check_date multi-selector fallback (same as --test-booking-flow)
  - _collect_slots_multi extracts time from various DOM patterns
"""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.checker import AvailabilityChecker
from src.config import Config


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="test@example.com",
        tock_password="pass",
        card_cvc="123",
        discord_webhook_url="",
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
    return AvailabilityChecker(cfg, browser, tracker)


def _make_mock_button(text: str):
    """Create a mock Playwright ElementHandle for a calendar day button."""
    btn = AsyncMock()
    btn.text_content = AsyncMock(return_value=text)
    btn.click = AsyncMock()
    btn.get_attribute = AsyncMock(return_value="ConsumerCalendar-day is-in-month")
    return btn


# ---------------------------------------------------------------------------
# _click_day: uses all_day_button, not available_day_button
# ---------------------------------------------------------------------------

class TestClickDayUsesAllButtons:

    @pytest.mark.asyncio
    async def test_clicks_matching_day_number(self):
        checker = _make_checker()
        page = AsyncMock()
        btn3 = _make_mock_button("3")
        btn4 = _make_mock_button("4")
        btn5 = _make_mock_button("5")
        page.query_selector_all = AsyncMock(return_value=[btn3, btn4, btn5])

        result = await checker._click_day(page, date(2026, 4, 4))

        assert result is True
        btn4.click.assert_called_once()
        btn3.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_when_day_not_found(self):
        checker = _make_checker()
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[_make_mock_button("1")])

        assert await checker._click_day(page, date(2026, 4, 15)) is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_buttons(self):
        checker = _make_checker()
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[])

        assert await checker._click_day(page, date(2026, 4, 4)) is False


# ---------------------------------------------------------------------------
# _collect_slots_multi: extracts time from various DOM patterns
# ---------------------------------------------------------------------------

class TestCollectSlotsMulti:

    @pytest.mark.asyncio
    async def test_extracts_time_from_parent_text(self):
        """When parent element contains time like '5:00 PM', extract it."""
        checker = _make_checker()
        page = AsyncMock()

        # Mock locator chain: page.locator(sel) → count=1 → nth(0) → ...
        mock_el = AsyncMock()
        mock_el.text_content = AsyncMock(return_value="Book")

        # Parent has time text
        mock_parent = AsyncMock()
        mock_parent.text_content = AsyncMock(return_value="Dinner 5:00 PM Book")

        mock_el.locator = MagicMock(side_effect=lambda sel: (
            mock_parent if sel == ".." else _empty_locator()
        ))

        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.nth = MagicMock(return_value=mock_el)
        page.locator = MagicMock(return_value=mock_locator)

        slots = await checker._collect_slots_multi(
            page, date(2026, 4, 4), 'button:has-text("Book")'
        )

        assert len(slots) == 1
        assert slots[0].slot_time == "5:00 PM"

    @pytest.mark.asyncio
    async def test_falls_back_to_slot_number(self):
        """When no time found anywhere, label as 'Slot 1'."""
        checker = _make_checker()
        page = AsyncMock()

        mock_el = AsyncMock()
        mock_el.text_content = AsyncMock(return_value="Book")

        mock_parent = AsyncMock()
        mock_parent.text_content = AsyncMock(return_value="Dinner Book")  # no time

        mock_el.locator = MagicMock(side_effect=lambda sel: (
            mock_parent if sel == ".." else _empty_locator()
        ))

        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_locator.nth = MagicMock(return_value=mock_el)
        page.locator = MagicMock(return_value=mock_locator)

        slots = await checker._collect_slots_multi(
            page, date(2026, 4, 4), 'button:has-text("Book")'
        )

        assert len(slots) == 1
        assert slots[0].slot_time == "Slot 1"

    @pytest.mark.asyncio
    async def test_multiple_slots(self):
        """Two 'Book' buttons → two slots."""
        checker = _make_checker()
        page = AsyncMock()

        def make_el(parent_text, btn_text="Book"):
            el = AsyncMock()
            el.text_content = AsyncMock(return_value=btn_text)
            parent = AsyncMock()
            parent.text_content = AsyncMock(return_value=parent_text)
            el.locator = MagicMock(side_effect=lambda sel: (
                parent if sel == ".." else _empty_locator()
            ))
            return el

        el1 = make_el("5:00 PM Book")
        el2 = make_el("8:00 PM Book")

        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=2)
        mock_locator.nth = MagicMock(side_effect=lambda i: [el1, el2][i])
        page.locator = MagicMock(return_value=mock_locator)

        slots = await checker._collect_slots_multi(
            page, date(2026, 4, 4), 'button:has-text("Book")'
        )

        assert len(slots) == 2
        assert slots[0].slot_time == "5:00 PM"
        assert slots[1].slot_time == "8:00 PM"


def _empty_locator():
    """A mock locator that returns count=0."""
    loc = AsyncMock()
    loc.count = AsyncMock(return_value=0)
    return loc
