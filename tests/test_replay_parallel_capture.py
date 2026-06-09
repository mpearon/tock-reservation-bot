"""Tests for the post-release-sniper REPLAY-FIRST / DOM-FALLBACK detection
path and the replay-miss raw-body capture (fix/replay-parallel-capture).

Background — forensics from the 2026-05-29 20:00 fuhuihua release:
  A real slot release sold out within seconds. The bot polled ~7×/sec but
  detected 0 slots. Two compounding causes:
    1. parse_available_slots' ghost-slot guard (>=800 bytes / >=5 times per
       date section) was calibrated on benu (high-volume) and silently
       filters a real fuhuihua release (few seatings → sub-threshold).
    2. check_all early-returned the empty-but-valid replay result ([]) and
       NEVER fell back to the reliable DOM scan.

The FIRST fix attempt ran replay+DOM concurrently via asyncio.gather and
returned their union. Two reviews flagged a blocking HIGH: gather waits for
BOTH branches, so even when replay ALREADY found a slot (~160ms), booking
was blocked behind the multi-second DOM scan — fatal for a release that
sells out in seconds.

Fix under test (the redesign): during the POST-RELEASE sniper phase
(keep_pages=True AND sniper_window_age_sec >= 60.0) with replay enabled,
detection is SEQUENTIAL, replay-first:
  1. replay_slots = await _try_calendar_replay()  (~160ms)
  2. replay non-empty  → record + return IMMEDIATELY; DOM scan does NOT run.
  3. replay empty/None  → run the DOM scan (the safety net) and return it.
This is the ONLY path DOM runs on, so an empty/wrong replay can never
suppress DOM, AND a found replay slot is booked without waiting on DOM.

Capture: on the replay-empty-but-DOM-found path, dump the body the poll
already stashed (the exact bytes the parser just wrongly returned empty for)
SYNCHRONOUSLY — no network re-fetch on the booking path (codex HIGH: an extra
fetch_calendar there delays booking slots that sell out in seconds) — once
per window, for offline ghost-slot threshold calibration.

Scenario → test map:
  (a) replay non-empty returns immediately WITHOUT running DOM (fast path)
        → test_post_release_replay_nonempty_skips_dom
  (b) replay empty runs DOM and returns DOM slots
        → test_post_release_runs_dom_when_replay_empty
  (h) replay None (failure) runs DOM and returns DOM slots
        → test_post_release_replay_none_still_yields_dom
  (c) replay-miss dumps the poll's stashed body (no re-fetch) once per window
        → test_replay_miss_dumps_stashed_body_without_refetch
        → test_replay_miss_dumps_once_per_window
        → test_replay_miss_no_body_at_all_does_not_burn_budget
        → test_replay_miss_dump_resets_on_close_sniper_pages
        → test_replay_miss_dump_resets_on_close_replay_session
  (d) instrumentation log fires on the replay-empty path
        → test_replay_diag_logged_on_replay_empty_path
  (e) pre-release still skips both
        → test_pre_release_skips_both_detectors
  (f) normal mode unchanged
        → test_normal_mode_replay_success_early_returns
        → test_normal_mode_replay_none_falls_through
  (g) DOM-path counters authoritative on the replay-empty path
        → test_replay_empty_dom_path_counter_consistency
"""
import os
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config


# Age guaranteed to be in the POST-release sniper phase (>= 60s gate).
POST_RELEASE_AGE = 120.0
# Age guaranteed to be in the PRE-release sniper phase (< 60s gate).
PRE_RELEASE_AGE = 10.0


@pytest.fixture(autouse=True)
def _isolate_capture_dir(monkeypatch, tmp_path):
    """Redirect the replay-capture dir to a per-test tmp path so NO test
    ever writes a .bin into the repo's replay_captures/ (several tests
    trigger a real replay-miss dump as a side effect). Tests that assert on
    dump contents override this with their own tmp_path — that's fine, the
    last setattr wins."""
    import src.checker as checker_mod
    monkeypatch.setattr(checker_mod, "_REPLAY_CAPTURE_DIR", str(tmp_path / "_caps"))


@pytest.fixture(autouse=True)
def _no_real_replay_fetch(monkeypatch):
    """Stub the low-level replay fetch so tests that exercise the real
    _try_calendar_replay never touch a live browser. (The replay-miss capture
    no longer re-fetches — it dumps the poll's stashed body — but the replay
    fast path itself still calls fetch_calendar.)"""
    monkeypatch.setattr("src.calendar_replay.fetch_calendar", _async_none)


def _make_config(use_replay: bool = True) -> Config:
    return Config(
        tock_email="t@t.com", tock_password="pw",
        restaurant_slug="fui-hui-hua-san-francisco",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="20:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        use_calendar_replay=use_replay,
    )


def _make_checker(use_replay: bool = True):
    cfg = _make_config(use_replay=use_replay)
    browser = MagicMock()
    tracker = MagicMock()
    return AvailabilityChecker(cfg, browser, tracker)


def _slot(d: date, t: str) -> AvailableSlot:
    return AvailableSlot(slot_date=d, slot_time=t, day_of_week=d.strftime("%A"))


def _single_target(checker, target: date):
    """Force _get_target_dates to yield exactly one preferred date (Friday)
    and no fallback dates."""
    checker._get_target_dates = (  # type: ignore[method-assign]
        lambda days, sniper_mode=False: [target] if "Friday" in days else []
    )


# ---------------------------------------------------------------------------
# (a) Post-release sniper: replay non-empty returns IMMEDIATELY, no DOM scan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_release_replay_nonempty_skips_dom(monkeypatch):
    """The HIGH fix. When replay finds a slot (~160ms), book it NOW: return
    the replay slots immediately and do NOT run the slow DOM scan. The whole
    point of replay is speed; waiting on DOM is fatal for a release that
    sells out in seconds.
    """
    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)

    replay_slot = _slot(target, "8:00 PM")
    async def fake_replay(*args, **kwargs):
        return [replay_slot]
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)

    dom_called = []
    async def fake_check_date(d, **kwargs):
        dom_called.append(d)
        return [_slot(target, "9:00 PM")]
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    result = await checker.check_all(
        concurrent=True, keep_pages=True,
        sniper_window_age_sec=POST_RELEASE_AGE,
    )
    assert result == [replay_slot], "must return exactly the replay slots"
    assert dom_called == [], (
        "DOM scan MUST NOT run on the replay fast path (the HIGH bug)"
    )
    # Replay fast path: one fetch represents the whole scan.
    assert checker.last_errors == 0
    assert checker.last_checks == 1


# ---------------------------------------------------------------------------
# (b) Post-release sniper: replay empty → DOM runs and wins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_release_runs_dom_when_replay_empty(monkeypatch):
    """The catastrophic-bug regression test. Replay returns [] (empty but
    valid — the ghost-slot guard filtered a real release), DOM finds the
    real slot. check_all MUST run DOM and return the DOM slot, not [].
    """
    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)

    async def fake_replay(*args, **kwargs):
        # Empty-but-valid parse (the over-tuned guard filtered everything)
        checker._last_replay_body = b"\x0a" + b"\x00" * 200
        return []
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)

    dom_slot = _slot(target, "8:00 PM")
    scan_dates_seen = []
    async def fake_check_date(d, **kwargs):
        scan_dates_seen.append(d)
        return [dom_slot]
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    result = await checker.check_all(
        concurrent=True, keep_pages=True,
        sniper_window_age_sec=POST_RELEASE_AGE,
    )
    assert scan_dates_seen, "DOM scan MUST run post-release when replay empty"
    assert result == [dom_slot], "DOM-found slot must survive an empty replay"


# ---------------------------------------------------------------------------
# (h) Post-release sniper: replay None (failure) → DOM runs and wins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_release_replay_none_still_yields_dom(monkeypatch):
    """When replay returns None (init/fetch/parse failed), the DOM scan
    still runs post-release and its slots are returned."""
    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)

    async def fake_replay(*args, **kwargs):
        return None  # total replay failure this poll
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)

    dom_slot = _slot(target, "8:00 PM")
    scan_seen = []
    async def fake_check_date(d, **kwargs):
        scan_seen.append(d)
        return [dom_slot]
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    result = await checker.check_all(
        concurrent=True, keep_pages=True,
        sniper_window_age_sec=POST_RELEASE_AGE,
    )
    assert scan_seen, "DOM must run when replay returns None post-release"
    assert result == [dom_slot]


# ---------------------------------------------------------------------------
# (c) Replay-miss capture: dump the poll's stashed body SYNCHRONOUSLY (no
#     network re-fetch on the booking path — see codex HIGH).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_miss_dumps_stashed_body_without_refetch(monkeypatch, tmp_path):
    """The booking path must NOT make a network call for calibration capture.
    On a replay-miss (DOM found slots, replay empty) the capture dumps the
    body the poll already stashed — the very body that wrongly parsed to empty
    — WITHOUT calling fetch_calendar again. (codex HIGH: an extra ~160ms
    fetch_calendar on the hottest poll delays booking slots that sell out in
    seconds.)"""
    import src.checker as checker_mod
    monkeypatch.setattr(checker_mod, "_REPLAY_CAPTURE_DIR", str(tmp_path))

    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)
    checker._replay_session = MagicMock()

    stashed_body = b"POST-RELEASE-BODY-THAT-PARSED-EMPTY" * 5
    async def fake_replay(*args, **kwargs):
        checker._last_replay_body = stashed_body
        return []  # replay missed
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)

    fetch_calls = []
    async def spy_fetch(session):
        fetch_calls.append(session)
        return b"SHOULD-NOT-BE-CALLED" * 5
    monkeypatch.setattr("src.calendar_replay.fetch_calendar", spy_fetch)

    async def fake_check_date(d, **kwargs):
        return [_slot(target, "8:00 PM")]  # DOM found it
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    result = await checker.check_all(concurrent=True, keep_pages=True,
                                     sniper_window_age_sec=POST_RELEASE_AGE)

    assert len(result) == 1, "DOM slots must be returned"
    assert fetch_calls == [], (
        "capture must NOT re-fetch on the booking path (no network call)"
    )
    dumps = os.listdir(tmp_path)
    assert len(dumps) == 1, f"expected exactly one capture, got {dumps}"
    assert (tmp_path / dumps[0]).read_bytes() == stashed_body, (
        "must dump the body the poll already stashed"
    )


@pytest.mark.asyncio
async def test_replay_miss_dumps_once_per_window(monkeypatch, tmp_path):
    """The dump is rate-limited to at most once per sniper window; a second
    miss in the same window must not dump again."""
    import src.checker as checker_mod
    monkeypatch.setattr(checker_mod, "_REPLAY_CAPTURE_DIR", str(tmp_path))

    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)
    checker._replay_session = MagicMock()

    async def fake_replay(*args, **kwargs):
        checker._last_replay_body = b"poll-body" * 5
        return []
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)

    async def fake_check_date(d, **kwargs):
        return [_slot(target, "8:00 PM")]
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    # Poll 1 → one dump
    await checker.check_all(concurrent=True, keep_pages=True,
                            sniper_window_age_sec=POST_RELEASE_AGE)
    assert len(os.listdir(tmp_path)) == 1

    # Poll 2 (same window) → no second dump
    await checker.check_all(concurrent=True, keep_pages=True,
                            sniper_window_age_sec=POST_RELEASE_AGE)
    assert len(os.listdir(tmp_path)) == 1, "must dump at most once per window"


@pytest.mark.asyncio
async def test_replay_miss_no_body_at_all_does_not_burn_budget(
    monkeypatch, tmp_path
):
    """When no body was stashed this poll (replay failed before producing
    one), there is nothing to dump — and the once-per-window budget must NOT
    be consumed, so a LATER poll in the same window that DOES stash a body can
    still dump it (don't crash, don't burn the budget).
    """
    import src.checker as checker_mod
    monkeypatch.setattr(checker_mod, "_REPLAY_CAPTURE_DIR", str(tmp_path))

    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)
    checker._replay_session = MagicMock()

    # Poll 1: replay None, no body stashed → nothing to dump.
    async def replay_none_no_body(*args, **kwargs):
        checker._last_replay_body = None
        return None
    monkeypatch.setattr(checker, "_try_calendar_replay", replay_none_no_body)
    async def fake_check_date(d, **kwargs):
        return [_slot(target, "8:00 PM")]
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    await checker.check_all(concurrent=True, keep_pages=True,
                            sniper_window_age_sec=POST_RELEASE_AGE)
    assert os.listdir(tmp_path) == [], "nothing to dump when no body anywhere"
    assert checker._replay_capture_dumped_this_window is False, (
        "a no-body miss must not consume the once-per-window dump budget"
    )

    # Poll 2 (same window): replay returns a body but still misses → dump.
    async def replay_with_body(*args, **kwargs):
        checker._last_replay_body = b"late-body" * 30
        return []
    monkeypatch.setattr(checker, "_try_calendar_replay", replay_with_body)

    await checker.check_all(concurrent=True, keep_pages=True,
                            sniper_window_age_sec=POST_RELEASE_AGE)
    assert len(os.listdir(tmp_path)) == 1, (
        "later poll with a body must still be allowed to dump"
    )


@pytest.mark.asyncio
async def test_replay_miss_dump_resets_on_close_sniper_pages(monkeypatch, tmp_path):
    """close_sniper_pages() ends the window → the once-per-window dump flag
    resets so the NEXT window can capture again."""
    import src.checker as checker_mod
    monkeypatch.setattr(checker_mod, "_REPLAY_CAPTURE_DIR", str(tmp_path))

    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)
    checker._replay_session = MagicMock()

    async def fake_replay(*args, **kwargs):
        checker._last_replay_body = b"raw" * 50
        return []
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    async def fake_check_date(d, **kwargs):
        return [_slot(target, "8:00 PM")]
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    await checker.check_all(concurrent=True, keep_pages=True,
                            sniper_window_age_sec=POST_RELEASE_AGE)
    assert len(os.listdir(tmp_path)) == 1

    # Window ends
    await checker.close_sniper_pages()
    assert checker._replay_capture_dumped_this_window is False

    # New window → a fresh capture is allowed
    await checker.check_all(concurrent=True, keep_pages=True,
                            sniper_window_age_sec=POST_RELEASE_AGE)
    assert len(os.listdir(tmp_path)) == 2


@pytest.mark.asyncio
async def test_replay_miss_dump_resets_on_close_replay_session(monkeypatch, tmp_path):
    """close_replay_session() also marks the window boundary → reset flag
    AND reset the per-window diag poll counter."""
    import src.checker as checker_mod
    monkeypatch.setattr(checker_mod, "_REPLAY_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setattr("src.calendar_replay.close_session", _async_none)

    checker = _make_checker(use_replay=True)
    checker._replay_capture_dumped_this_window = True
    checker._replay_diag_poll_count = 5
    await checker.close_replay_session()
    assert checker._replay_capture_dumped_this_window is False
    assert checker._replay_diag_poll_count == 0


@pytest.mark.asyncio
async def test_close_replay_session_idempotent_with_no_session(monkeypatch):
    """close_replay_session() must be safe to call when no session exists
    (so wiring it into the window-end transition can't crash the loop)."""
    monkeypatch.setattr("src.calendar_replay.close_session", _async_none)
    checker = _make_checker(use_replay=True)
    assert checker._replay_session is None
    # Should not raise, even with no session and no prior state.
    await checker.close_replay_session()
    await checker.close_replay_session()  # twice — still fine
    assert checker._replay_session is None


# ---------------------------------------------------------------------------
# (d) Instrumentation log fires on the replay-empty path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_diag_logged_on_replay_empty_path(monkeypatch, caplog):
    """The case we care about: replay returned empty, so DOM ran. The
    replay diagnostics (body length, date hits, sections passed/filtered)
    must still be logged at INFO so we can recalibrate the guards."""
    import logging
    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)

    async def fake_replay(*args, **kwargs):
        checker._last_replay_body = b"\x0a\x0a2026-05-15" + b"\x00" * 300
        checker._last_replay_diag = checker_diag(body_len=312, date_hits=3,
                                                 sections_passed=0,
                                                 sections_filtered=3)
        return []
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    async def fake_check_date(d, **kwargs):
        return []
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    with caplog.at_level(logging.INFO, logger="src.checker"):
        await checker.check_all(concurrent=True, keep_pages=True,
                                sniper_window_age_sec=POST_RELEASE_AGE)

    diag_lines = [r.message for r in caplog.records
                  if "replay-diag" in r.message]
    assert diag_lines, "expected a [replay-diag] INFO line on the replay-empty path"
    joined = " ".join(diag_lines)
    assert "312" in joined and "filtered" in joined


# ---------------------------------------------------------------------------
# (e) Pre-release skips BOTH detectors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_release_skips_both_detectors(monkeypatch):
    """During the pre-release window (age < 60s), neither replay nor DOM
    runs — check_all returns [] immediately (unchanged behavior)."""
    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)

    replay_called = []
    async def fake_replay(*args, **kwargs):
        replay_called.append(True)
        return []
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    dom_called = []
    async def fake_check_date(d, **kwargs):
        dom_called.append(d)
        return []
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    result = await checker.check_all(
        concurrent=True, keep_pages=True,
        sniper_window_age_sec=PRE_RELEASE_AGE,
    )
    assert result == []
    assert replay_called == [], "replay must not run pre-release"
    assert dom_called == [], "DOM must not run pre-release"


# ---------------------------------------------------------------------------
# (f) Normal mode unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normal_mode_replay_success_early_returns(monkeypatch):
    """Normal (non-sniper) mode: a non-None replay result short-circuits
    and the DOM scan does NOT run (existing replay-if-on-else-DOM behavior).
    """
    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)

    replay_slot = _slot(target, "8:00 PM")
    async def fake_replay(*args, **kwargs):
        return [replay_slot]
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)

    dom_called = []
    async def fake_check_date(d, **kwargs):
        dom_called.append(d)
        return [_slot(target, "9:00 PM")]
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    result = await checker.check_all(concurrent=True, keep_pages=False,
                                     sniper_window_age_sec=0.0)
    assert result == [replay_slot]
    assert dom_called == [], "normal mode must NOT run DOM when replay succeeds"


@pytest.mark.asyncio
async def test_normal_mode_replay_none_falls_through(monkeypatch):
    """Normal mode: replay None falls through to DOM (existing behavior)."""
    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)

    async def fake_replay(*args, **kwargs):
        return None
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    dom_called = []
    async def fake_check_date(d, **kwargs):
        dom_called.append(d)
        return []
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    await checker.check_all(concurrent=True, keep_pages=False,
                            sniper_window_age_sec=0.0)
    assert dom_called, "normal mode must fall through to DOM on replay None"


# ---------------------------------------------------------------------------
# (g) DOM-path counters authoritative on the replay-empty path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_empty_dom_path_counter_consistency(monkeypatch):
    """After a post-release replay-empty poll (DOM ran), last_errors/
    last_checks must reflect the DOM scan's authoritative counts — NOT
    replay's "1 fetch = whole scan" accounting."""
    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)

    async def fake_replay(*args, **kwargs):
        # Replay mutates ONLY its own state; it must not touch last_errors.
        checker._replay_failure_count += 1
        checker._last_replay_body = b"\x0a" * 100
        return []
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)

    dom_slot = _slot(target, "8:00 PM")
    dom_ran = []
    async def fake_check_date(d, **kwargs):
        dom_ran.append(d)
        return [dom_slot]
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    result = await checker.check_all(concurrent=True, keep_pages=True,
                                     sniper_window_age_sec=POST_RELEASE_AGE)

    assert dom_ran, "DOM scan must have run (proves replay-empty → DOM path)"
    assert result == [dom_slot]
    # The DOM scan checked exactly one preferred date with zero errors.
    assert checker.last_checks == 1
    assert checker.last_errors == 0
    # Replay's own bookkeeping is untouched by the DOM scan's counters.
    assert checker._replay_failure_count == 1


# ---------------------------------------------------------------------------
# parse_with_diagnostics — the calibration instrumentation foundation
# ---------------------------------------------------------------------------

def test_parse_with_diagnostics_counts_filtered_sections():
    """parse_with_diagnostics returns the same slots as parse_available_slots
    PLUS a diag object counting body length, date hits, and how many date
    sections passed vs were filtered by the size/time ghost-slot guards."""
    from src.calendar_replay import (
        parse_with_diagnostics, parse_available_slots,
    )

    target = date(2026, 5, 15)
    # A single date marker followed by a tiny section (sold-out shape) →
    # below the byte threshold → filtered.
    body = b"\x0a\x0a2026-05-15\x12\x05\x1a\x05"  # ~16 bytes, no real metadata
    slots, diag = parse_with_diagnostics(body, [target])

    assert slots == parse_available_slots(body, [target])
    assert diag.body_len == len(body)
    assert diag.date_hits >= 1
    # The only section is sub-threshold → filtered, none passed.
    assert diag.sections_filtered >= 1
    assert diag.sections_passed == 0


def test_parse_with_diagnostics_counts_passed_section():
    """A fat section with many times passes both guards → counted as passed
    and its slots returned."""
    from src.calendar_replay import parse_with_diagnostics

    target = date(2026, 5, 15)
    # Big section with >= 5 plausible booking times (17:00..21:00) and
    # >= 800 bytes so it clears both thresholds. Times are separated by a
    # NUL framing byte so each clears the \b word boundary in _TIME_RE
    # (mirrors how anchors sit between protobuf field separators).
    good_times = b"\x0017:00\x0018:00\x0019:00\x0020:00\x0021:00\x00"
    padding = b"\x00" * 900
    body = b"\x0a\x0a2026-05-15" + good_times + padding
    slots, diag = parse_with_diagnostics(body, [target])

    assert diag.sections_passed == 1
    assert any(s.slot_date == target for s in slots)


# ---------------------------------------------------------------------------
# Helpers used by tests above
# ---------------------------------------------------------------------------

async def _async_none(*args, **kwargs):
    return None


def checker_diag(body_len, date_hits, sections_passed, sections_filtered):
    """Build a ReplayParseDiag instance."""
    from src.calendar_replay import ReplayParseDiag
    return ReplayParseDiag(
        body_len=body_len, date_hits=date_hits,
        sections_passed=sections_passed, sections_filtered=sections_filtered,
    )
