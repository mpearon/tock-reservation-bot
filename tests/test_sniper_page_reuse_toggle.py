"""Sniper page-reuse toggle (config.sniper_reuse_pages).

Investigation 2026-05-31 (spikes/warm_vs_cold_repro.py) measured warm
cross-poll page-reuse (page.reload of the kept-open _sniper_pages) as
materially LESS reliable than fresh navigation under concurrent sniper load:
~19% first-attempt calendar-load timeouts with UNRECOVERABLE bursts (6/14,
5/14 — enough to flip the adaptive switch) vs ~3.6% all-recovered for fresh
pages, and reuse was not faster. Repeatedly reloading ~14 kept-open SPA pages
accumulates renderer state and starves hydration.

So sniper page-reuse is gated behind config.sniper_reuse_pages, DEFAULT FALSE
(fresh page every poll — the detection scan is the load-bearing post-release
safety net). This file pins the toggle in both states:

  reuse OFF (default):
    - _check_date(keep_page=True) opens a FRESH page, never reloads a kept
      page, never parks the fresh page in _sniper_pages, and CLOSES it at poll
      end (so no page survives to be reused or to reach the booker stale).
    - prewarm_target_dates() still navigates (warms the Cloudflare session) but
      does NOT park pages → _sniper_pages stays empty → pop_warm_page() returns
      None → booker falls back to a fresh page.

  reuse ON (opt-in): the historical behavior — reload kept pages, park prewarm
    pages, keep them open across polls.

  Normal mode (keep_page=False) and the flag are independent.
"""
import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config, load_config

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except Exception:  # pragma: no cover
    class PlaywrightTimeoutError(Exception):
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_checker(**overrides) -> AvailabilityChecker:
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="benu",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    for k, v in overrides.items():
        setattr(config, k, v)
    browser = MagicMock()
    return AvailabilityChecker(config, browser, MagicMock())


def _search_url(date_str: str, slug: str = "benu") -> str:
    return (f"https://www.exploretock.com/{slug}/search"
            f"?date={date_str}&size=2&time=17:00")


def _make_page(url: str, *, cf_dom: bool = False) -> AsyncMock:
    """A Playwright Page mock that loads cleanly (no CF, calendar present)."""
    page = AsyncMock()
    page.url = url
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.reload = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=MagicMock())
    page.evaluate = AsyncMock(return_value=cf_dom)  # CF DOM probe
    page.close = AsyncMock()
    page.query_selector = AsyncMock(return_value=None)
    page.query_selector_all = AsyncMock(return_value=[])
    return page


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------

def test_default_is_reuse_off():
    """The dataclass default is reuse OFF (fresh pages)."""
    checker = _make_checker()
    assert checker.config.sniper_reuse_pages is False


def test_env_true_enables_reuse(monkeypatch):
    monkeypatch.setenv("TOCK_EMAIL", "t@t.com")
    monkeypatch.setenv("TOCK_PASSWORD", "pw")
    monkeypatch.setenv("SNIPER_REUSE_PAGES", "true")
    assert load_config().sniper_reuse_pages is True


def test_env_absent_defaults_off(monkeypatch):
    monkeypatch.setenv("TOCK_EMAIL", "t@t.com")
    monkeypatch.setenv("TOCK_PASSWORD", "pw")
    monkeypatch.delenv("SNIPER_REUSE_PAGES", raising=False)
    assert load_config().sniper_reuse_pages is False


# ---------------------------------------------------------------------------
# _check_date: reuse OFF (default) → fresh page, no reload, not kept
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reuse_off_opens_fresh_page_does_not_reload_parked():
    """With reuse OFF, a kept page from a prior poll is IGNORED — _check_date
    opens a fresh page via browser.new_page() and never calls warm.reload()."""
    checker = _make_checker(sniper_reuse_pages=False)
    date_str = "2026-06-05"
    warm = _make_page(_search_url(date_str))          # a stale parked page
    checker._sniper_pages[date_str] = warm
    fresh = _make_page(_search_url(date_str))
    checker.browser.new_page = AsyncMock(return_value=fresh)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=False)):
        await checker._check_date(date(2026, 6, 5), keep_page=True)

    checker.browser.new_page.assert_awaited_once()   # fresh page opened
    warm.reload.assert_not_called()                  # parked page NOT reused
    fresh.goto.assert_awaited()                      # fresh navigation


@pytest.mark.asyncio
async def test_reuse_off_does_not_park_fresh_page_and_closes_it():
    """With reuse OFF, the fresh sniper page is NOT stored in _sniper_pages and
    IS closed at the end of the poll (nothing survives to be reused)."""
    checker = _make_checker(sniper_reuse_pages=False)
    fresh = _make_page(_search_url("2026-06-05"))
    checker.browser.new_page = AsyncMock(return_value=fresh)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=False)):
        await checker._check_date(date(2026, 6, 5), keep_page=True)

    assert "2026-06-05" not in checker._sniper_pages   # not parked
    fresh.close.assert_awaited()                        # closed at poll end


@pytest.mark.asyncio
async def test_reuse_off_leaves_no_warm_page_for_booker():
    """Safety property: after a reuse-OFF sniper poll, pop_warm_page() returns
    None so the booker never gets a stale page (it falls back to new_page)."""
    checker = _make_checker(sniper_reuse_pages=False)
    fresh = _make_page(_search_url("2026-06-05"))
    checker.browser.new_page = AsyncMock(return_value=fresh)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=False)):
        await checker._check_date(date(2026, 6, 5), keep_page=True)

    assert checker.pop_warm_page("2026-06-05") is None


# ---------------------------------------------------------------------------
# _check_date: reuse ON (opt-in) → historical behavior preserved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reuse_on_reloads_parked_page():
    """With reuse ON, a kept page on the right URL is reloaded (reused), and
    browser.new_page() is NOT called."""
    checker = _make_checker(sniper_reuse_pages=True)
    date_str = "2026-06-05"
    warm = _make_page(_search_url(date_str))
    checker._sniper_pages[date_str] = warm
    checker.browser.new_page = AsyncMock(return_value=_make_page(_search_url(date_str)))

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=False)):
        await checker._check_date(date(2026, 6, 5), keep_page=True)

    warm.reload.assert_awaited_once()
    checker.browser.new_page.assert_not_called()


@pytest.mark.asyncio
async def test_reuse_on_parks_and_keeps_fresh_page_open():
    """With reuse ON and no parked page, the fresh sniper page is stored in
    _sniper_pages and NOT closed at poll end (kept for the next poll)."""
    checker = _make_checker(sniper_reuse_pages=True)
    fresh = _make_page(_search_url("2026-06-05"))
    checker.browser.new_page = AsyncMock(return_value=fresh)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=False)):
        await checker._check_date(date(2026, 6, 5), keep_page=True)

    assert checker._sniper_pages.get("2026-06-05") is fresh   # parked
    fresh.close.assert_not_called()                            # kept open


# ---------------------------------------------------------------------------
# Normal mode (keep_page=False) is independent of the flag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", [False, True])
async def test_normal_mode_unaffected_by_flag(reuse):
    """keep_page=False always opens a fresh page and closes it, regardless of
    the sniper flag (the flag only governs sniper mode)."""
    checker = _make_checker(sniper_reuse_pages=reuse)
    fresh = _make_page(_search_url("2026-06-05"))
    checker.browser.new_page = AsyncMock(return_value=fresh)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=False)):
        await checker._check_date(
            date(2026, 6, 5), keep_page=False, bypass_normal_skip=True
        )

    assert "2026-06-05" not in checker._sniper_pages
    fresh.close.assert_awaited()


# ---------------------------------------------------------------------------
# prewarm_target_dates honors the flag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prewarm_off_warms_but_does_not_park():
    """With reuse OFF, prewarm navigates (warming the CF session) but does NOT
    park the page — _sniper_pages stays empty and the page is closed."""
    checker = _make_checker(sniper_reuse_pages=False)
    page = _make_page(_search_url("2026-06-05"))
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates([date(2026, 6, 5)], stagger_sec=0)

    page.goto.assert_awaited()                       # session still warmed
    assert checker._sniper_pages == {}               # nothing parked
    page.close.assert_awaited()                       # page closed, not kept


@pytest.mark.asyncio
async def test_prewarm_on_parks_page():
    """With reuse ON, prewarm parks the page in _sniper_pages (historical)."""
    checker = _make_checker(sniper_reuse_pages=True)
    page = _make_page(_search_url("2026-06-05"))
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates([date(2026, 6, 5)], stagger_sec=0)

    assert checker._sniper_pages.get("2026-06-05") is page
    page.close.assert_not_called()


# ---------------------------------------------------------------------------
# Found-slot page retention (sniper reuse-off keeps the booking-speed edge)
#
# Reuse-off makes the SCAN reliable (fresh pages). But closing every page also
# throws away the ~0.85s warm-page→booker handoff for the date that found a
# slot. So when reuse is off and a slot IS found, we retain ONLY that page
# (single-cycle, in _handoff_pages) for the booker — one warm page, not 14, so
# there's no reload contention. This is the existing normal-mode
# retain_found_pages handoff, extended to the sniper path.
# ---------------------------------------------------------------------------

def _make_loaded_page(date_str: str = "2026-06-05") -> AsyncMock:
    """Page that survives _check_date's full happy path (calendar + slot
    detection). page.evaluate distinguishes the CF DOM probe (→ False) from
    the slot-detect probe (→ a hit)."""
    page = AsyncMock()
    page.url = _search_url(date_str)
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.reload = AsyncMock()
    page.close = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.screenshot = AsyncMock()

    async def _eval(js, *a, **k):
        if "challenges.cloudflare.com" in js or "cf-turnstile" in js:
            return False          # not a Cloudflare challenge
        return {"index": 0, "count": 1}   # a slot is present
    page.evaluate = AsyncMock(side_effect=_eval)
    return page


def _slot(date_str: str = "2026-06-05") -> AvailableSlot:
    y, m, d = (int(x) for x in date_str.split("-"))
    return AvailableSlot(slot_date=date(y, m, d), slot_time="5:00 PM",
                         day_of_week=date(y, m, d).strftime("%A"))


@pytest.mark.asyncio
async def test_sniper_reuse_off_retains_found_slot_page_for_booker():
    """Reuse OFF + slot found in sniper mode → the page is parked in
    _handoff_pages (NOT closed, NOT in _sniper_pages) for the booker."""
    checker = _make_checker(sniper_reuse_pages=False)
    page = _make_loaded_page("2026-06-05")
    checker.browser.new_page = AsyncMock(return_value=page)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
         patch.object(checker, "_click_day", AsyncMock(return_value=True)), \
         patch.object(checker, "_collect_slots_multi",
                      AsyncMock(return_value=[_slot("2026-06-05")])):
        result = await checker._check_date(date(2026, 6, 5), keep_page=True)

    assert len(result) == 1
    page.close.assert_not_called()                       # retained for booker
    assert checker._handoff_pages.get("2026-06-05") is page
    assert "2026-06-05" not in checker._sniper_pages     # not the reuse dict


@pytest.mark.asyncio
async def test_sniper_reuse_off_found_page_drainable_via_pop_handoff():
    """The retained page is reachable by the booker's drain (pop_handoff_page)."""
    checker = _make_checker(sniper_reuse_pages=False)
    page = _make_loaded_page("2026-06-05")
    checker.browser.new_page = AsyncMock(return_value=page)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
         patch.object(checker, "_click_day", AsyncMock(return_value=True)), \
         patch.object(checker, "_collect_slots_multi",
                      AsyncMock(return_value=[_slot("2026-06-05")])):
        await checker._check_date(date(2026, 6, 5), keep_page=True)

    assert checker.pop_handoff_page("2026-06-05") is page


@pytest.mark.asyncio
async def test_sniper_reuse_off_no_slot_closes_page_no_retention():
    """Reuse OFF + NO slot → page closed, nothing retained (only pages that
    actually found a slot are worth handing off)."""
    checker = _make_checker(sniper_reuse_pages=False)
    page = _make_loaded_page("2026-06-05")
    checker.browser.new_page = AsyncMock(return_value=page)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
         patch.object(checker, "_click_day", AsyncMock(return_value=True)), \
         patch.object(checker, "_collect_slots_multi", AsyncMock(return_value=[])):
        result = await checker._check_date(date(2026, 6, 5), keep_page=True)

    assert result == []
    page.close.assert_awaited()                          # closed, nothing to keep
    assert checker._handoff_pages == {}


@pytest.mark.asyncio
async def test_sniper_reuse_on_found_slot_uses_sniper_pages_not_handoff():
    """Reuse ON + slot found → the page lives in _sniper_pages (the reuse dict),
    is kept open, and is NOT double-parked in _handoff_pages."""
    checker = _make_checker(sniper_reuse_pages=True)
    page = _make_loaded_page("2026-06-05")
    checker.browser.new_page = AsyncMock(return_value=page)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
         patch.object(checker, "_click_day", AsyncMock(return_value=True)), \
         patch.object(checker, "_collect_slots_multi",
                      AsyncMock(return_value=[_slot("2026-06-05")])):
        result = await checker._check_date(date(2026, 6, 5), keep_page=True)

    assert len(result) == 1
    assert checker._sniper_pages.get("2026-06-05") is page   # reuse path keeps it
    assert "2026-06-05" not in checker._handoff_pages        # no double-park
    page.close.assert_not_called()                            # kept open


@pytest.mark.asyncio
async def test_normal_mode_no_retain_still_closes_with_slots():
    """Regression guard: normal mode (keep_page=False) WITHOUT retain_found_page
    still closes the page even when slots are found (unchanged behavior)."""
    checker = _make_checker(sniper_reuse_pages=False)
    page = _make_loaded_page("2026-06-05")
    checker.browser.new_page = AsyncMock(return_value=page)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=True)), \
         patch.object(checker, "_click_day", AsyncMock(return_value=True)), \
         patch.object(checker, "_collect_slots_multi",
                      AsyncMock(return_value=[_slot("2026-06-05")])):
        result = await checker._check_date(
            date(2026, 6, 5), keep_page=False, bypass_normal_skip=True,
        )

    assert len(result) == 1
    page.close.assert_awaited()
    assert checker._handoff_pages == {}


# ---------------------------------------------------------------------------
# Review hardening (code-review + codex adversarial pass)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reuse_off_evicts_stale_sniper_page():
    """Codex HIGH (defense-in-depth): with reuse off, a stale page lingering in
    _sniper_pages (e.g. left by a prior reuse-on phase) must be closed+evicted
    when its date is scanned, so it can't shadow the fresh found-slot handoff
    page via the monitor's pop_warm_page()-first drain."""
    checker = _make_checker(sniper_reuse_pages=False)
    stale = _make_loaded_page("2026-06-05")
    checker._sniper_pages["2026-06-05"] = stale
    fresh = _make_loaded_page("2026-06-05")
    checker.browser.new_page = AsyncMock(return_value=fresh)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=False)):
        await checker._check_date(date(2026, 6, 5), keep_page=True)

    assert "2026-06-05" not in checker._sniper_pages   # stale evicted
    stale.close.assert_awaited()                        # and closed
    checker.browser.new_page.assert_awaited()           # a fresh page was opened


@pytest.mark.asyncio
async def test_reuse_off_close_failure_does_not_propagate():
    """Code-review Medium: the finally's page.close() is now the primary close
    path for every fresh sniper page. If it raises (CDP drop mid-release), it
    must be swallowed — otherwise the coroutine raises, check_all counts it as
    an error, and that date is silently unchecked."""
    checker = _make_checker(sniper_reuse_pages=False)
    page = _make_page(_search_url("2026-06-05"))
    page.close = AsyncMock(side_effect=Exception("CDP connection closed"))
    checker.browser.new_page = AsyncMock(return_value=page)

    with patch.object(checker, "_wait_for_calendar", AsyncMock(return_value=False)):
        result = await checker._check_date(date(2026, 6, 5), keep_page=True)

    assert result == []          # no exception propagated out of _check_date
    page.close.assert_awaited()  # close was attempted
