"""Tests for booking click flow fixes."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date

from src.checker import AvailableSlot


def _make_booker():
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    notifier = MagicMock()
    return TockBooker(config, browser, notifier)


def _make_slot(slot_time="5:00 PM"):
    return AvailableSlot(
        slot_date=date(2026, 4, 17),
        slot_time=slot_time,
        day_of_week="Friday",
    )


@pytest.mark.asyncio
async def test_generic_book_button_skipped_when_no_time_in_parent():
    """A generic 'Book' button whose parent has no time text must NOT be clicked."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")

    page = AsyncMock()
    # wait_for_selector times out (no specific slot buttons)
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))

    # Capture the generic button mock so we can assert it was never clicked
    captured_btn: AsyncMock | None = None

    def make_locator(selector):
        nonlocal captured_btn
        loc = MagicMock()
        if 'has-text("Book")' in selector or 'book_now' in selector.lower():
            loc.count = AsyncMock(return_value=1)
            btn = AsyncMock()
            btn.text_content = AsyncMock(return_value="Book")
            parent = AsyncMock()
            # Parent has NO time text — should skip this button
            parent.text_content = AsyncMock(return_value="Restaurant details")
            btn.locator = MagicMock(return_value=parent)
            loc.nth = MagicMock(return_value=btn)
            captured_btn = btn
        else:
            loc.count = AsyncMock(return_value=0)
        return loc

    page.locator = MagicMock(side_effect=make_locator)

    result = await booker._click_time_slot(page, slot)

    assert result is False
    # The button's .click() must never have been called
    if captured_btn is not None:
        captured_btn.click.assert_not_called()


@pytest.mark.asyncio
async def test_generic_book_button_clicked_when_time_in_parent():
    """A generic 'Book' button whose parent contains the target time MUST be clicked."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")

    page = AsyncMock()
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))

    def make_locator(selector):
        loc = MagicMock()
        if 'has-text("Book")' in selector:
            loc.count = AsyncMock(return_value=1)
            btn = AsyncMock()
            btn.text_content = AsyncMock(return_value="Book")
            parent = AsyncMock()
            # Parent DOES contain the target time
            parent.text_content = AsyncMock(return_value="5:00 PM  Book  2 guests")
            btn.locator = MagicMock(return_value=parent)
            loc.nth = MagicMock(return_value=btn)
        else:
            loc.count = AsyncMock(return_value=0)
        return loc

    page.locator = MagicMock(side_effect=make_locator)

    result = await booker._click_time_slot(page, slot)

    assert result is True


@pytest.mark.asyncio
async def test_checkout_detection_polls_payment_element():
    """_wait_for_checkout falls back to payment-element detection within 30s."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"

    # selector wait always times out
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))

    # payment indicator found on 3rd attempt (any non-None return counts)
    call_count = [0]
    async def mock_query_selector(selector):
        call_count[0] += 1
        if call_count[0] >= 3:
            return MagicMock()  # payment element found
        return None

    page.query_selector = AsyncMock(side_effect=mock_query_selector)

    result = await booker._wait_for_checkout(page, slot)

    assert result is True


@pytest.mark.asyncio
async def test_checkout_detection_respects_url_change():
    """_wait_for_checkout detects checkout via URL containing '/checkout'."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()

    # Simulate URL change to checkout after 2s
    call_count = [0]
    async def mock_wait_for_selector(selector, timeout=None):
        call_count[0] += 1
        if call_count[0] >= 2:
            page.url = "https://www.exploretock.com/test/checkout/abc123"
        raise Exception("timeout")

    page.wait_for_selector = AsyncMock(side_effect=mock_wait_for_selector)
    page.url = "https://www.exploretock.com/test/search"
    page.query_selector = AsyncMock(return_value=None)

    result = await booker._wait_for_checkout(page, slot)

    assert result is True


@pytest.mark.asyncio
async def test_screenshot_taken_on_checkout_timeout(tmp_path):
    """When debug_screenshots=True and checkout times out, a screenshot is saved."""
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=True,  # enabled
        discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    notifier = MagicMock()
    booker = TockBooker(config, browser, notifier)

    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))
    page.query_selector = AsyncMock(return_value=None)
    screenshot_paths = []

    async def mock_screenshot(path=None, **kwargs):
        if path:
            screenshot_paths.append(path)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"PNG")

    page.screenshot = AsyncMock(side_effect=mock_screenshot)

    slot = _make_slot()
    with patch("src.booker._SCREENSHOT_DIR", str(tmp_path)):
        await booker._wait_for_checkout(page, slot)

    # At least one screenshot should have been taken
    assert len(screenshot_paths) >= 1
    assert all("booking_" in p for p in screenshot_paths)


@pytest.mark.asyncio
async def test_no_screenshot_when_debug_disabled(tmp_path):
    """When debug_screenshots=False, no screenshots during booking."""
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False,  # disabled
        discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    notifier = MagicMock()
    booker = TockBooker(config, browser, notifier)

    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock()

    slot = _make_slot()
    with patch("src.booker._SCREENSHOT_DIR", str(tmp_path)):
        await booker._wait_for_checkout(page, slot)

    page.screenshot.assert_not_called()
