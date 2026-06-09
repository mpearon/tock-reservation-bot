"""Tests for the 2026-06-05 sniper fixes — PFR-round hardened.

Covers, on top of the original fixes:
  * Discord dedup: monitor suppresses repeat booking_failed embeds for the same
    slot-set (no per-poll spam during a contested window).
  * Dump sites guarded by booking_won (losing race tasks don't dump) and
    stage-labelled (click/checkout/prep) so artifacts aren't mislabelled.
  * Dump file written owner-only (0600), cap checked before page.content().
  * Tagged click uses no_wait_after; tag-miss re-checks booking_won before the
    strict rescan; _wait_for_checkout bails fast when another slot won.
  * Checker clears stale data-sniper-target tags and uses a button-index token
    consistent across the JS and PW paths.
"""
import asyncio
import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailableSlot
from src.notifier import _RED


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _make_config(**overrides):
    from src.config import Config
    kwargs = dict(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _make_notifier(**o):
    from src.notifier import Notifier
    return Notifier(_make_config(**o))


def _make_booker(**o):
    from src.booker import TockBooker
    return TockBooker(_make_config(**o), MagicMock(), MagicMock())


def _make_checker(**o):
    from src.checker import AvailabilityChecker
    return AvailabilityChecker(_make_config(**o), MagicMock(), MagicMock())


def _slot(d=date(2026, 5, 15), t="5:00 PM", dow="Friday"):
    return AvailableSlot(slot_date=d, slot_time=t, day_of_week=dow)


def _warm_page():
    p = AsyncMock()
    p.is_closed = MagicMock(return_value=False)
    p.close = AsyncMock()
    return p


# --------------------------------------------------------------------------- #
# Fix 1a — notifier.booking_failed
# --------------------------------------------------------------------------- #

def test_booking_failed_fires_critical_red_embed_for_all_slots():
    n = _make_notifier(); n._fire = MagicMock()
    slots = [_slot(date(2026, 6, 12), "5:00 PM", "Friday"),
             _slot(date(2026, 6, 13), "5:00 PM", "Saturday")]
    n.booking_failed(slots, "no clickable slot button found")
    n._fire.assert_called_once()
    kw = n._fire.call_args.kwargs
    assert "Failed" in kw.get("title", "")
    assert "2 slot" in kw.get("title", "") or "2 slot" in kw.get("description", "")
    desc = kw.get("description", "")
    assert "2026-06-12" in desc and "2026-06-13" in desc
    assert "no clickable slot button found" in desc
    assert kw.get("color") == _RED
    assert kw.get("critical") is True


def test_booking_failed_accepts_single_slot():
    n = _make_notifier(); n._fire = MagicMock()
    n.booking_failed(_slot(), "reason")
    assert "1 slot" in n._fire.call_args.kwargs.get("title", "")


# --------------------------------------------------------------------------- #
# Fix 1b + PFR-A — monitor wiring + dedup
# --------------------------------------------------------------------------- #

def _build_monitor(*, check_all_returns, sniper_active=False, dry_run=False):
    from src.monitor import TockMonitor
    cfg = _make_config(dry_run=dry_run)
    browser = MagicMock(); browser.warm_session = AsyncMock()
    checker = MagicMock()
    if isinstance(check_all_returns, list) and check_all_returns and isinstance(check_all_returns[0], list):
        checker.check_all = AsyncMock(side_effect=check_all_returns)
    else:
        checker.check_all = AsyncMock(return_value=check_all_returns)
    checker.last_checks = 1
    checker.last_errors = 0
    checker.close_sniper_pages = AsyncMock()
    checker.close_replay_session = AsyncMock()
    checker.close_handoff_pages = AsyncMock()
    checker.flush_deferred = MagicMock()
    checker.pop_handoff_page = MagicMock(return_value=None)
    checker.pop_warm_page = MagicMock(return_value=None)
    notifier = MagicMock()
    tracker = MagicMock(); tracker.flush_deferred = MagicMock()
    with patch("src.monitor.TockBooker"):
        monitor = TockMonitor(cfg, browser, checker, notifier, tracker)
    monitor._sniper_active = sniper_active
    monitor._sniper_concurrent = True
    monitor._booking_secured = False
    return monitor, checker, notifier


@pytest.mark.asyncio
async def test_poll_failed_booking_notifies_booking_failed():
    from src.booker import BookingOutcome
    slots = [_slot(date(2026, 6, 13), "5:00 PM", "Saturday")]
    monitor, _c, notifier = _build_monitor(check_all_returns=slots)
    monitor.booker.book_best_slot_race = AsyncMock(return_value=(BookingOutcome.FAILED, None))
    await monitor.poll()
    notifier.booking_failed.assert_called_once()
    assert list(notifier.booking_failed.call_args.args[0]) == slots


@pytest.mark.asyncio
async def test_poll_confirmed_booking_does_not_notify_failure():
    from src.booker import BookingOutcome
    slots = [_slot(date(2026, 6, 13), "5:00 PM", "Saturday")]
    monitor, _c, notifier = _build_monitor(check_all_returns=slots)
    monitor.booker.book_best_slot_race = AsyncMock(return_value=(BookingOutcome.CONFIRMED, slots[0]))
    await monitor.poll()
    notifier.booking_failed.assert_not_called()


@pytest.mark.asyncio
async def test_poll_dedups_repeated_identical_failure():
    """Same unbookable slot-set across consecutive polls → ONE embed, not spam."""
    from src.booker import BookingOutcome
    slots = [_slot(date(2026, 6, 13), "5:00 PM", "Saturday")]
    monitor, _c, notifier = _build_monitor(check_all_returns=slots)
    monitor.booker.book_best_slot_race = AsyncMock(return_value=(BookingOutcome.FAILED, None))
    await monitor.poll()
    await monitor.poll()
    await monitor.poll()
    assert notifier.booking_failed.call_count == 1


@pytest.mark.asyncio
async def test_poll_realerts_on_new_failed_slotset():
    """A different failed slot-set re-alerts."""
    from src.booker import BookingOutcome
    a = [_slot(date(2026, 6, 13), "5:00 PM", "Saturday")]
    b = [_slot(date(2026, 6, 12), "8:00 PM", "Friday")]
    monitor, _c, notifier = _build_monitor(check_all_returns=[a, b])
    monitor.booker.book_best_slot_race = AsyncMock(return_value=(BookingOutcome.FAILED, None))
    await monitor.poll()
    await monitor.poll()
    assert notifier.booking_failed.call_count == 2


# --------------------------------------------------------------------------- #
# Fix 3a — AvailableSlot.target_selector
# --------------------------------------------------------------------------- #

def test_available_slot_target_selector_defaults_to_none():
    assert _slot().target_selector is None


def test_available_slot_target_selector_excluded_from_equality():
    a = _slot(); b = _slot()
    b.target_selector = '[data-sniper-target="2026-05-15#0"]'
    assert a == b


# --------------------------------------------------------------------------- #
# Fix 3b + PFR-F — checker tagging (clears stale tags, indexed token)
# --------------------------------------------------------------------------- #

def _pw_page_with_buttons(btns):
    page = AsyncMock()
    page.evaluate = AsyncMock()  # allowed: used to CLEAR stale tags
    button_locator = MagicMock()
    button_locator.count = AsyncMock(return_value=len(btns))
    button_locator.nth = MagicMock(side_effect=lambda i: btns[i])
    container_locator = MagicMock(); container_locator.count = AsyncMock(return_value=0)
    pw_target = 'button:visible:has-text("Book")'

    def page_locator(selector):
        from src.selectors import SELECTORS
        if selector == SELECTORS["slots_container"]:
            return container_locator
        if selector == pw_target:
            return button_locator
        z = MagicMock(); z.count = AsyncMock(return_value=0)
        return z
    page.locator = MagicMock(side_effect=page_locator)
    return page, pw_target


def _pw_btn(parent_text="5:00 PM table for 2"):
    b = AsyncMock()
    b.text_content = AsyncMock(return_value="Book")
    b.get_attribute = AsyncMock(return_value=None)
    b.evaluate = AsyncMock()
    par = AsyncMock(); par.text_content = AsyncMock(return_value=parent_text)
    es = MagicMock(); es.count = AsyncMock(return_value=0)
    b.locator = MagicMock(side_effect=lambda s, par=par, es=es: par if s == ".." else es)
    return b


@pytest.mark.asyncio
async def test_pw_collect_tags_button_with_indexed_token():
    checker = _make_checker()
    btn = _pw_btn()
    page, pw_target = _pw_page_with_buttons([btn])
    slots = await checker._collect_slots_multi(page, date(2026, 6, 13), pw_target)
    assert len(slots) == 1
    assert slots[0].slot_time == "5:00 PM"
    assert slots[0].target_selector == '[data-sniper-target="2026-06-13#0"]'
    btn.evaluate.assert_awaited_once()
    assert btn.evaluate.call_args.args[1] == "2026-06-13#0"


@pytest.mark.asyncio
async def test_pw_collect_clears_stale_tags_before_tagging():
    checker = _make_checker()
    page, pw_target = _pw_page_with_buttons([_pw_btn()])
    await checker._collect_slots_multi(page, date(2026, 6, 13), pw_target)
    # A page-wide clear of data-sniper-target must have been attempted.
    assert page.evaluate.await_count >= 1
    cleared = any("data-sniper-target" in str(c.args) for c in page.evaluate.await_args_list)
    assert cleared


@pytest.mark.asyncio
async def test_pw_collect_two_same_time_buttons_get_distinct_tokens():
    checker = _make_checker()
    b0, b1 = _pw_btn("5:00 PM"), _pw_btn("5:00 PM")
    page, pw_target = _pw_page_with_buttons([b0, b1])
    slots = await checker._collect_slots_multi(page, date(2026, 6, 13), pw_target)
    assert {s.target_selector for s in slots} == {
        '[data-sniper-target="2026-06-13#0"]',
        '[data-sniper-target="2026-06-13#1"]',
    }


@pytest.mark.asyncio
async def test_js_collect_sets_target_selector_from_token():
    checker = _make_checker()
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "container_used": True, "button_count": 1,
        "slots": [{"time": "5:00 PM", "source": 1, "target": "2026-06-13#0"}],
    })
    slots = await checker._collect_slots_multi(
        page, date(2026, 6, 13), "button.Consumer-resultsListItem.is-available")
    assert slots[0].target_selector == '[data-sniper-target="2026-06-13#0"]'
    assert page.evaluate.call_args.args[1].get("dateStr") == "2026-06-13"


@pytest.mark.asyncio
async def test_js_collect_without_token_leaves_target_selector_none():
    checker = _make_checker()
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "container_used": True, "button_count": 1,
        "slots": [{"time": "5:00 PM", "source": 1}],
    })
    slots = await checker._collect_slots_multi(
        page, date(2026, 6, 13), "button.Consumer-resultsListItem.is-available")
    assert slots[0].target_selector is None


# --------------------------------------------------------------------------- #
# Fix 3c + PFR-D — _click_tagged_slot
# --------------------------------------------------------------------------- #

def _tagged_page(count=1):
    btn = AsyncMock(); btn.click = AsyncMock()
    loc = MagicMock(); loc.count = AsyncMock(return_value=count); loc.first = btn
    page = AsyncMock(); page.locator = MagicMock(return_value=loc)
    return page, btn


@pytest.mark.asyncio
async def test_click_tagged_slot_clicks_when_tag_present():
    booker = _make_booker()
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    page, btn = _tagged_page(1)
    assert await booker._click_tagged_slot(page, slot) is True
    btn.click.assert_awaited_once()
    page.locator.assert_called_once_with('[data-sniper-target="2026-05-15#0"]')


@pytest.mark.asyncio
async def test_click_tagged_slot_uses_bounded_timeout_and_no_wait_after():
    booker = _make_booker()
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    page, btn = _tagged_page(1)
    await booker._click_tagged_slot(page, slot)
    kw = btn.click.call_args.kwargs
    assert kw.get("timeout") is not None and 0 < kw["timeout"] <= 5000
    assert kw.get("no_wait_after") is True


@pytest.mark.asyncio
async def test_click_tagged_slot_false_when_tag_absent():
    booker = _make_booker()
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    page, _ = _tagged_page(0)
    assert await booker._click_tagged_slot(page, slot) is False


@pytest.mark.asyncio
async def test_click_tagged_slot_false_on_exception():
    booker = _make_booker()
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    page = AsyncMock(); page.locator = MagicMock(side_effect=RuntimeError("detached"))
    assert await booker._click_tagged_slot(page, slot) is False


@pytest.mark.asyncio
async def test_click_tagged_slot_false_without_target_selector():
    booker = _make_booker()
    page = AsyncMock(); page.locator = MagicMock()
    assert await booker._click_tagged_slot(page, _slot()) is False
    page.locator.assert_not_called()


# --------------------------------------------------------------------------- #
# Fix 3d + PFR — _book_single routing, fallback, booking_won guards
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_book_single_warm_tag_hit_skips_time_scan():
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    booker._click_tagged_slot = AsyncMock(return_value=True)
    booker._click_time_slot = AsyncMock(return_value=True)
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    booker._wait_for_checkout = AsyncMock(return_value=False)
    ok = await booker._book_single(slot, asyncio.Event(), warm_page=_warm_page())
    assert ok is False
    booker._click_tagged_slot.assert_awaited_once()
    booker._click_time_slot.assert_not_awaited()
    booker._wait_for_checkout.assert_awaited_once()


@pytest.mark.asyncio
async def test_book_single_warm_tag_miss_falls_back_to_strict_then_dumps():
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    booker._click_tagged_slot = AsyncMock(return_value=False)
    booker._click_time_slot = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    booker._wait_for_checkout = AsyncMock(return_value=False)
    ok = await booker._book_single(slot, asyncio.Event(), warm_page=_warm_page())
    assert ok is False
    booker._click_time_slot.assert_awaited_once()
    assert booker._click_time_slot.call_args.kwargs.get("strict_time_match") is True
    booker._dump_click_failure.assert_awaited_once()
    assert booker._dump_click_failure.call_args.kwargs.get("stage", "click") == "click"
    booker._wait_for_checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_single_warm_tag_miss_aborts_without_dump_when_won():
    """If another slot already won, a tag miss must NOT rescan or dump."""
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    won = asyncio.Event(); won.set()
    booker._click_tagged_slot = AsyncMock(return_value=False)
    booker._click_time_slot = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    ok = await booker._book_single(slot, won, warm_page=_warm_page())
    assert ok is False
    booker._click_time_slot.assert_not_awaited()
    booker._dump_click_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_single_warm_tag_miss_fallback_succeeds_proceeds():
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    booker._click_tagged_slot = AsyncMock(return_value=False)
    booker._click_time_slot = AsyncMock(return_value=True)
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    booker._wait_for_checkout = AsyncMock(return_value=False)
    await booker._book_single(slot, asyncio.Event(), warm_page=_warm_page())
    booker._click_time_slot.assert_awaited_once()
    assert booker._click_time_slot.call_args.kwargs.get("strict_time_match") is True
    booker._wait_for_checkout.assert_awaited_once()


@pytest.mark.asyncio
async def test_book_single_checkout_fail_dumps_with_checkout_stage():
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    booker._click_tagged_slot = AsyncMock(return_value=True)
    booker._click_time_slot = AsyncMock(return_value=True)
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    booker._wait_for_checkout = AsyncMock(return_value=False)
    await booker._book_single(slot, asyncio.Event(), warm_page=_warm_page())
    booker._dump_click_failure.assert_awaited_once()
    assert booker._dump_click_failure.call_args.kwargs.get("stage") == "checkout"


@pytest.mark.asyncio
async def test_book_single_fresh_page_ignores_target_selector():
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    page = AsyncMock(); page.is_closed = MagicMock(return_value=False); page.close = AsyncMock()
    booker.browser.new_page = AsyncMock(return_value=page)
    booker.browser.page_pool = None
    booker._wait_for_selector = AsyncMock(return_value=True)
    booker._click_calendar_day = AsyncMock(return_value=True)
    booker._click_tagged_slot = AsyncMock(return_value=True)
    booker._click_time_slot = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    ok = await booker._book_single(slot, asyncio.Event(), warm_page=None)
    assert ok is False
    booker._click_tagged_slot.assert_not_awaited()
    booker._click_time_slot.assert_awaited()
    assert booker._click_time_slot.call_args.kwargs.get("strict_time_match") is False


# --------------------------------------------------------------------------- #
# PFR-E — _wait_for_checkout bails fast when another slot won
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_wait_for_checkout_aborts_fast_when_booking_won_set():
    booker = _make_booker()

    async def _hang(*a, **k):
        await asyncio.sleep(30)
    page = AsyncMock()
    page.wait_for_selector = AsyncMock(side_effect=_hang)
    page.wait_for_url = AsyncMock(side_effect=_hang)
    page.wait_for_function = AsyncMock(side_effect=_hang)
    booker._booking_screenshot = AsyncMock()
    won = asyncio.Event(); won.set()

    result = await asyncio.wait_for(
        booker._wait_for_checkout(page, _slot(), booking_won=won), timeout=3
    )
    assert result is False


# --------------------------------------------------------------------------- #
# Fix 4 + PFR — dump: stage label, 0600 perms, cap before content
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_dump_writes_html_with_stage_and_strips_query(tmp_path, monkeypatch):
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(return_value="<html>SNIPER_DOM_MARKER</html>")
    page.url = "https://www.exploretock.com/test/search?date=2026-06-13&size=2"
    monkeypatch.setattr("src.booker._BOOKING_FAILURE_DIR", str(tmp_path))
    await booker._dump_click_failure(
        page, _slot(date(2026, 6, 13), "5:00 PM", "Saturday"), stage="checkout")
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    name = files[0].name
    assert name.startswith("faildom_checkout_")
    assert "2026-06-13" in name
    body = files[0].read_text()
    assert "SNIPER_DOM_MARKER" in body
    assert "size=2" not in body


@pytest.mark.asyncio
async def test_dump_file_is_owner_only(tmp_path, monkeypatch):
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(return_value="<html>x</html>")
    page.url = "https://x/search"
    monkeypatch.setattr("src.booker._BOOKING_FAILURE_DIR", str(tmp_path))
    await booker._dump_click_failure(page, _slot(), stage="click")
    f = next(tmp_path.iterdir())
    assert (os.stat(f).st_mode & 0o077) == 0  # no group/other permissions


@pytest.mark.asyncio
async def test_dump_respects_cap_without_serializing_page(tmp_path, monkeypatch):
    booker = _make_booker()
    monkeypatch.setattr("src.booker._BOOKING_FAILURE_DIR", str(tmp_path))
    monkeypatch.setattr("src.booker._MAX_FAILURE_DUMPS", 2)
    for i in range(2):
        (tmp_path / f"faildom_click_2026010{i}_000000_000000_2026-06-13.html").write_text("x")
    page = AsyncMock()
    page.content = AsyncMock(return_value="<html>OVERFLOW</html>")
    page.url = "https://x/search"
    await booker._dump_click_failure(page, _slot(), stage="click")
    assert len(list(tmp_path.iterdir())) == 2
    # Cap reached → must not have paid for full-page serialization.
    page.content.assert_not_awaited()


@pytest.mark.asyncio
async def test_dump_swallows_content_errors(tmp_path, monkeypatch):
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(side_effect=RuntimeError("page closed"))
    monkeypatch.setattr("src.booker._BOOKING_FAILURE_DIR", str(tmp_path))
    await booker._dump_click_failure(page, _slot(), stage="click")
    assert list(tmp_path.iterdir()) == []
