"""Tests for target-date page prewarm (Phase A+2 Task 2).

Also covers Codex adversarial review fixes:
  Fix 2: split prewarm timers (cookies T-15, pages T-5)
  Fix 3: _get_prewarm_dates skips prior-release-cycle dates
  Fix 4: close old page before overwriting in _sniper_pages
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.checker import AvailabilityChecker


def _make_checker():
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        # This file exercises prewarm's page-PARKING behavior, which only
        # happens when reuse is enabled (the default is now off — fresh pages).
        sniper_reuse_pages=True,
    )
    browser = MagicMock()
    browser.new_page = AsyncMock()
    return AvailabilityChecker(config, browser, MagicMock())


@pytest.mark.asyncio
async def test_prewarm_opens_one_page_per_date():
    """prewarm_target_dates opens exactly N pages and stores them in _sniper_pages."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    pages = []
    async def make_page():
        p = AsyncMock()
        p.is_closed = MagicMock(return_value=False)
        p.evaluate = AsyncMock(return_value=False)
        p.goto = AsyncMock()
        p.wait_for_selector = AsyncMock()
        pages.append(p)
        return p
    checker.browser.new_page = make_page

    # Use stagger=0 to keep the test fast
    await checker.prewarm_target_dates(dates, stagger_sec=0)

    assert len(pages) == 3
    assert set(checker._sniper_pages.keys()) == {
        "2026-05-01", "2026-05-02", "2026-05-03"
    }


@pytest.mark.asyncio
async def test_prewarm_navigates_to_correct_url():
    """Each prewarmed page navigates to the per-date Tock search URL."""
    checker = _make_checker()
    dates = [date(2026, 5, 1)]
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    page.goto.assert_called_once()
    args = page.goto.call_args
    url = args.args[0] if args.args else args.kwargs.get("url")
    assert "date=2026-05-01" in url
    assert "size=2" in url


@pytest.mark.asyncio
async def test_prewarm_waits_for_calendar_container():
    """After goto, prewarm waits for the calendar to render (parks at CALENDAR_LOADED)."""
    checker = _make_checker()
    dates = [date(2026, 5, 1)]
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    # Confirm wait_for_selector was called with the calendar_container
    # selector — not just any selector. Tight assertion catches a future
    # refactor that swaps in a different (incorrect) wait target.
    import src.selectors as sel_mod
    page.wait_for_selector.assert_called_once()
    called_selector = page.wait_for_selector.call_args.args[0]
    assert called_selector == sel_mod.SELECTORS["calendar_container"], (
        f"Expected wait_for_selector to receive calendar_container "
        f"({sel_mod.SELECTORS['calendar_container']!r}); got {called_selector!r}"
    )


@pytest.mark.asyncio
async def test_prewarm_failure_does_not_break_other_dates():
    """If one prewarm fails, others still complete."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    page_count = [0]
    pages = []
    async def make_page():
        page_count[0] += 1
        p = AsyncMock()
        p.is_closed = MagicMock(return_value=False)
        p.evaluate = AsyncMock(return_value=False)
        p.wait_for_selector = AsyncMock()
        if page_count[0] == 2:
            p.goto = AsyncMock(side_effect=Exception("fake CF error"))
        else:
            p.goto = AsyncMock()
        pages.append(p)
        return p
    checker.browser.new_page = make_page

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    # Two pages successfully prewarmed (1st and 3rd); 2nd failed but didn't kill the others
    assert len(checker._sniper_pages) == 2
    assert "2026-05-01" in checker._sniper_pages
    assert "2026-05-03" in checker._sniper_pages
    assert "2026-05-02" not in checker._sniper_pages


@pytest.mark.asyncio
async def test_prewarm_respects_stagger():
    """Pages open spread across `stagger_sec` intervals."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    checker.browser.new_page = AsyncMock(return_value=page)

    sleep_calls = []
    real_sleep = asyncio.sleep
    async def fake_sleep(secs):
        sleep_calls.append(secs)
        # Don't actually sleep — keep the test fast
        await real_sleep(0)

    with patch("src.checker.asyncio.sleep", new=fake_sleep):
        await checker.prewarm_target_dates(dates, stagger_sec=30)

    # 3 dates → exactly 2 stagger sleeps (no trailing sleep after last date).
    # Tight assertion catches both off-by-one regressions and missing-stagger
    # regressions (a single `any()` would mask either of those).
    stagger_30_count = sum(1 for s in sleep_calls if s == 30)
    assert stagger_30_count == len(dates) - 1, (
        f"Expected exactly {len(dates) - 1} stagger sleeps of 30s; "
        f"got {stagger_30_count} (sleep_calls={sleep_calls})"
    )


@pytest.mark.asyncio
async def test_prewarm_all_dates_fail_returns_cleanly():
    """If EVERY date's prewarm fails, _sniper_pages stays empty and no
    exception propagates. Sniper-poll falls back to cold goto() for all."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    pages = []
    async def make_page():
        p = AsyncMock()
        p.is_closed = MagicMock(return_value=False)
        p.evaluate = AsyncMock(return_value=False)
        p.goto = AsyncMock(side_effect=Exception("simulated CF challenge"))
        p.wait_for_selector = AsyncMock()
        p.close = AsyncMock()
        pages.append(p)
        return p
    checker.browser.new_page = make_page

    # Must not raise even when every date fails
    await checker.prewarm_target_dates(dates, stagger_sec=0)

    assert checker._sniper_pages == {}, (
        f"All-fail must leave _sniper_pages empty; got {checker._sniper_pages}"
    )
    # All 3 leaked pages should have been closed by the new finally block (Fix 1)
    assert all(p.close.called for p in pages), (
        "Failed pages must be closed to prevent leak across release windows"
    )


# ---------------------------------------------------------------------------
# Fix 2: Split prewarm timers — _get_dates_prewarm_target (T-5min)
# ---------------------------------------------------------------------------

def _make_monitor_with_friday_sniper():
    """Helper: TockMonitor configured with Friday@19:59 sniper."""
    from src.monitor import TockMonitor
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    return TockMonitor(config, MagicMock(), MagicMock(), MagicMock(), MagicMock())


def test_dates_prewarm_target_returns_none_outside_window(monkeypatch):
    """_get_dates_prewarm_target returns None when no window is in the next 5 min."""
    import pytz
    from datetime import datetime
    import src.monitor as monitor_mod

    mon = _make_monitor_with_friday_sniper()

    # Mock "now" to a Friday at 18:00 PT — sniper at 19:59, 119 minutes away
    PT = pytz.timezone("America/Los_Angeles")
    fake_now = PT.localize(datetime(2026, 5, 1, 18, 0))

    fake_dt = MagicMock()
    fake_dt.now = MagicMock(return_value=fake_now)
    monkeypatch.setattr(monitor_mod, "datetime", fake_dt)

    assert mon._get_dates_prewarm_target() is None


def test_dates_prewarm_target_fires_at_5min_window(monkeypatch):
    """Within 5 min of sniper window opening, _get_dates_prewarm_target fires."""
    import pytz
    from datetime import datetime
    import src.monitor as monitor_mod

    mon = _make_monitor_with_friday_sniper()

    # Mock "now" to Friday 19:55 PT — sniper at 19:59, 4 minutes away (<=5)
    PT = pytz.timezone("America/Los_Angeles")
    fake_now = PT.localize(datetime(2026, 5, 1, 19, 55))

    fake_dt = MagicMock()
    fake_dt.now = MagicMock(return_value=fake_now)
    monkeypatch.setattr(monitor_mod, "datetime", fake_dt)

    target = mon._get_dates_prewarm_target()
    assert target == "Friday@19:59", f"Expected Friday@19:59; got {target}"


def test_dates_prewarm_target_none_on_non_sniper_day(monkeypatch):
    """_get_dates_prewarm_target returns None on a non-sniper day regardless of time."""
    import pytz
    from datetime import datetime
    import src.monitor as monitor_mod

    mon = _make_monitor_with_friday_sniper()

    # Mock "now" to Thursday 19:55 PT — not a sniper day
    PT = pytz.timezone("America/Los_Angeles")
    fake_now = PT.localize(datetime(2026, 4, 30, 19, 55))  # Thursday

    fake_dt = MagicMock()
    fake_dt.now = MagicMock(return_value=fake_now)
    monkeypatch.setattr(monitor_mod, "datetime", fake_dt)

    assert mon._get_dates_prewarm_target() is None


def test_session_dates_prewarmed_for_field_exists():
    """TockMonitor has _session_dates_prewarmed_for field initialized to None."""
    mon = _make_monitor_with_friday_sniper()
    assert hasattr(mon, "_session_dates_prewarmed_for"), (
        "TockMonitor must have _session_dates_prewarmed_for attribute (Fix 2)"
    )
    assert mon._session_dates_prewarmed_for is None


# ---------------------------------------------------------------------------
# Fix 3: _get_prewarm_dates skips prior-release-cycle dates
# ---------------------------------------------------------------------------

def test_get_prewarm_dates_skips_current_release_dates(monkeypatch):
    """On a Friday release, today+1 (Sat) and today+2 (Sun) belong to the
    PRIOR release and must NOT be prewarmed. Only dates >=5 days out qualify."""
    from datetime import date as _date
    import src.monitor as monitor_mod

    mon = _make_monitor_with_friday_sniper()
    # Use preferred_days with Sat and Sun so they would appear without the fix
    mon.config.preferred_days = ["Friday", "Saturday", "Sunday"]
    mon.config.fallback_days = []

    # Mock today to Friday 2026-05-01
    fake_today = _date(2026, 5, 1)
    fake_date = MagicMock(wraps=_date)
    fake_date.today = MagicMock(return_value=fake_today)
    monkeypatch.setattr(monitor_mod, "date", fake_date)

    dates = mon._get_prewarm_dates()
    # Skip Sat 2026-05-02 (T+1) and Sun 2026-05-03 (T+2) — prior release
    assert _date(2026, 5, 2) not in dates, (
        f"Sat 2026-05-02 (T+1) belongs to PRIOR release; should be skipped. Got: {dates}"
    )
    assert _date(2026, 5, 3) not in dates, (
        f"Sun 2026-05-03 (T+2) belongs to PRIOR release; should be skipped. Got: {dates}"
    )
    # Include next-week targets (T+7 and beyond)
    assert _date(2026, 5, 8) in dates, f"Fri 2026-05-08 (T+7) must be prewarmed. Got: {dates}"
    assert _date(2026, 5, 9) in dates, f"Sat 2026-05-09 (T+8) must be prewarmed. Got: {dates}"
    assert _date(2026, 5, 10) in dates, f"Sun 2026-05-10 (T+9) must be prewarmed. Got: {dates}"


def test_default_prewarm_min_days_out_is_5():
    """Default config value still matches Fuhuihua's Friday release cadence.

    PREWARM_MIN_DAYS_OUT was promoted from a monitor module constant to
    Config.prewarm_min_days_out (Codex pass 2 MEDIUM). The dataclass default
    must remain 5 so existing deployments without PREWARM_MIN_DAYS_OUT in
    .env continue to behave correctly.
    """
    from dataclasses import fields
    from src.config import Config
    for f in fields(Config):
        if f.name == "prewarm_min_days_out":
            assert f.default == 5, (
                f"Config.prewarm_min_days_out default must be 5; got {f.default}"
            )
            return
    raise AssertionError("Config has no prewarm_min_days_out field")


# ---------------------------------------------------------------------------
# Fix 4: close old page before overwriting in _sniper_pages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prewarm_closes_old_page_before_overwriting():
    """When prewarm fires twice for the same date in one process, the
    OLD page is closed before being overwritten. Otherwise it leaks
    (close_sniper_pages can only see what's currently in the dict)."""
    checker = _make_checker()
    dates = [date(2026, 5, 1)]

    pages = []
    async def make_page():
        p = AsyncMock()
        p.is_closed = MagicMock(return_value=False)
        p.evaluate = AsyncMock(return_value=False)
        p.goto = AsyncMock()
        p.wait_for_selector = AsyncMock()
        p.close = AsyncMock()
        pages.append(p)
        return p
    checker.browser.new_page = make_page

    # First prewarm
    await checker.prewarm_target_dates(dates, stagger_sec=0)
    assert "2026-05-01" in checker._sniper_pages
    first_page = pages[0]

    # Second prewarm — must close the first page before storing the second
    await checker.prewarm_target_dates(dates, stagger_sec=0)
    assert "2026-05-01" in checker._sniper_pages

    # The first page must have been closed during the second prewarm
    assert first_page.close.called, (
        "Old prewarmed page must be closed before overwrite — otherwise it leaks"
    )
    # The dict now contains the SECOND page, not the first
    assert checker._sniper_pages["2026-05-01"] is pages[1], (
        "Dict must hold the new page after overwrite"
    )


# ---------------------------------------------------------------------------
# Codex pass 2: pop_warm_page transfers ownership
# ---------------------------------------------------------------------------

def test_pop_warm_page_removes_from_sniper_pages():
    """pop_warm_page transfers ownership: the page leaves _sniper_pages."""
    checker = _make_checker()
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(return_value=False)
    checker._sniper_pages["2026-05-01"] = page

    popped = checker.pop_warm_page("2026-05-01")

    assert popped is page
    assert "2026-05-01" not in checker._sniper_pages, (
        "pop_warm_page must remove the entry — checker no longer owns this page"
    )


def test_pop_warm_page_returns_none_if_closed():
    """A closed page returns None (defensive against booker side-effects)."""
    checker = _make_checker()
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=True)
    checker._sniper_pages["2026-05-01"] = page

    popped = checker.pop_warm_page("2026-05-01")

    assert popped is None


def test_pop_warm_page_returns_none_if_missing():
    """Missing date returns None without raising."""
    checker = _make_checker()
    assert checker.pop_warm_page("2026-05-01") is None
