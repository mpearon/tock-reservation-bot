#!/usr/bin/env python
"""
spikes/warm_vs_cold_repro.py  —  INVESTIGATION SPIKE (not production code)

Reproduce + measure the sniper-mode warm-page-reuse reliability question
raised on 2026-05-30: is reloading kept-open _sniper_pages (warm) actually
LESS reliable than a fresh page.goto (cold) for loading the DOM calendar,
because ~14 concurrent heavy-SPA reloads starve each other during hydration?

SAFETY (the production bot is running and holds bot.lock):
  * Does NOT acquire bot.lock — constructs Config/TockBrowser/AvailabilityChecker
    directly, never goes through main.py / monitor / process_lock.
  * Isolates session_cookies.json: works on a TEMP COPY and repoints
    src.browser.COOKIES_FILE at it, so nothing here can ever write the
    production cookie file. start() still restores the real cf_clearance
    (read-only) so benu calendars load.
  * Never calls login() / warm_session() (the only methods that persist cookies).
  * No-op tracker (no slot_tracker.* writes) and stubbed error-screenshot
    (no errors/*.png writes).
  * dry_run=True, use_calendar_replay=False (force the DOM scan under test),
    headless=True.

EXPERIMENTS
  PART A — exactly the task-requested call: check_all(concurrent=True,
    keep_pages=True, sniper_window_age_sec=120.0) for N polls. Poll 1 is COLD
    (fresh goto per date), polls 2..N are WARM (page.reload of the kept pages).
    Reports last_errors/last_checks + wall-clock per poll, and first-attempt
    calendar timeouts (warm vs cold, CF vs not) parsed from [cal-timeout-diag].

  PART B — mechanism / option comparison. Drives _check_date directly across
    the target dates with the slot-found abort DISABLED (abort_event=None) so
    every date does a full calendar load every poll. Three regimes:
      cold-fresh    : close_sniper_pages() before each poll -> fresh goto
      warm-uncapped : keep pages, reload ALL concurrently   -> current prod
      warm-cap-K    : keep pages, reload with <=K in flight  -> option 2/3
    Driving _check_date directly means check_all never ticks the warmup
    counter, so every regime/poll uses the SAME 12s warmup budget
    (_SNIPER_WARMUP_CAL_TIMEOUT_MS) — the budget under which the original
    2026-05-30 timeouts were seen — removing the ramp as a confound.

Usage:
    venv/bin/python spikes/warm_vs_cold_repro.py [--slug benu] [--a-polls 4]
        [--b-polls 2] [--cap 6] [--inter-poll-sec 2.0]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

# Repo root on path so `import src.*` works when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.browser as browser_mod  # noqa: E402
from src.browser import TockBrowser  # noqa: E402
from src.checker import AvailabilityChecker  # noqa: E402
from src.config import Config  # noqa: E402

logger = logging.getLogger("warm_vs_cold")

ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------

class DiagCapture(logging.Handler):
    """Capture src.checker [cal-timeout-diag] lines (one per first-attempt
    calendar timeout, tagged nav=warm-reload|cold-goto and cf_challenge=)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "[cal-timeout-diag]" in msg:
            self.records.append(msg)

    def snapshot(self) -> int:
        return len(self.records)

    def since(self, mark: int) -> list[str]:
        return self.records[mark:]


def classify(diag_lines: list[str]) -> dict:
    """Break a slice of [cal-timeout-diag] lines into warm/cold × cf/not."""
    out = {"total": len(diag_lines), "warm": 0, "cold": 0,
           "cf": 0, "noncf": 0}
    for m in diag_lines:
        if "nav=warm-reload" in m:
            out["warm"] += 1
        elif "nav=cold-goto" in m:
            out["cold"] += 1
        if "cf_challenge=True" in m:
            out["cf"] += 1
        else:
            out["noncf"] += 1
    return out


class LatencyProbe:
    """Wrap checker._wait_for_calendar to time each calendar-load wait and
    record (date, reused, ok, elapsed_ms). Forwards to the true original so
    the retry/diagnostic logic still runs."""

    def __init__(self, checker: AvailabilityChecker):
        self.checker = checker
        self._true = checker._wait_for_calendar
        self.samples: list[dict] = []

    def install(self):
        true = self._true
        samples = self.samples

        async def timed(page, date_str, **kwargs):
            t0 = time.perf_counter()
            ok = await true(page, date_str, **kwargs)
            dt = (time.perf_counter() - t0) * 1000.0
            samples.append({
                "date": date_str,
                "reused": bool(kwargs.get("reused", False)),
                "ok": bool(ok),
                "ms": dt,
            })
            return ok

        self.checker._wait_for_calendar = timed  # type: ignore[assignment]

    def restore(self):
        self.checker._wait_for_calendar = self._true  # type: ignore[assignment]

    def reset(self):
        self.samples.clear()


def _pctile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[i]


def _latency_summary(samples: list[dict], reused: bool | None = None) -> dict:
    sub = [s for s in samples
           if (reused is None or s["reused"] == reused)]
    ok = [s for s in sub if s["ok"]]
    ms = [s["ms"] for s in ok]
    return {
        "n": len(sub),
        "ok": len(ok),
        "fail": len(sub) - len(ok),
        "p50_ms": round(_pctile(ms, 50), 0),
        "p90_ms": round(_pctile(ms, 90), 0),
        "max_ms": round(max(ms), 0) if ms else 0,
    }


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def _noop_screenshot(*_a, **_k) -> None:
    return None


def _build_config(slug: str) -> Config:
    """Minimal Config for a DOM-scan-only benu reproduction."""
    return Config(
        tock_email="repro@example.com",     # unused: we never login()
        tock_password="unused",
        card_cvc="",
        discord_webhook_url="",
        headless=True,
        dry_run=True,
        restaurant_slug=slug,
        party_size=2,
        preferred_days=list(ALL_DAYS),      # all 7 days × 2 weeks ≈ 14 dates
        fallback_days=[],                   # single concurrent phase
        preferred_time="17:00",
        scan_weeks=2,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=0,
        sniper_scan_weeks=2,
        use_calendar_replay=False,          # force the DOM scan under test
        page_pool_size=0,                   # pool not used by _check_date
        debug_screenshots=False,
    )


def _isolate_cookies() -> Path:
    """Copy the production cookie file to a temp path and repoint
    src.browser.COOKIES_FILE at it. Returns the temp path. After this, nothing
    in this process can write the real session_cookies.json."""
    real = Path("session_cookies.json")
    tmp = Path(tempfile.mkdtemp(prefix="repro_cookies_")) / "session_cookies.json"
    if real.exists():
        shutil.copy2(real, tmp)
        logger.info(f"Isolated cookies: copied {real} -> {tmp}")
    else:
        logger.warning("No production session_cookies.json found — "
                       "benu calendars may hit a Cloudflare wall.")
    browser_mod.COOKIES_FILE = tmp  # repoint the module-level constant
    return tmp


# ---------------------------------------------------------------------------
# PART A — task-requested check_all A/B
# ---------------------------------------------------------------------------

async def part_a(checker: AvailabilityChecker, diag: DiagCapture,
                 probe: LatencyProbe, polls: int, inter_poll_sec: float,
                 fresh_each_poll: bool = False):
    label = "FRESH-each-poll (close before every poll → all cold)" \
        if fresh_each_poll else "WARM-continuous (poll 1 cold, polls 2+ reload)"
    print("\n" + "=" * 72)
    print(f"PART A [{label}] — check_all(concurrent=True, keep_pages=True, "
          f"sniper_window_age_sec=120.0) × {polls} polls")
    print("=" * 72)

    await checker.close_sniper_pages()  # clean slate
    probe.reset()
    rows = []
    for i in range(1, polls + 1):
        if fresh_each_poll:
            await checker.close_sniper_pages()  # force every poll cold
        mark = diag.snapshot()
        probe.reset()
        t0 = time.perf_counter()
        slots = await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0,
        )
        wall = (time.perf_counter() - t0) * 1000.0
        new_diag = classify(diag.since(mark))
        kind = "COLD" if (fresh_each_poll or i == 1) else "WARM"
        rows.append({
            "poll": i, "kind": kind,
            "last_errors": checker.last_errors,
            "last_checks": checker.last_checks,
            "first_attempt_timeouts": new_diag["total"],
            "fa_warm": new_diag["warm"], "fa_cold": new_diag["cold"],
            "fa_cf": new_diag["cf"],
            "slots": len(slots),
            "wall_ms": round(wall, 0),
            "cal_p50": _latency_summary(probe.samples)["p50_ms"],
            "cal_max": _latency_summary(probe.samples)["max_ms"],
        })
        print(f"  poll {i} [{kind}]: last_errors={checker.last_errors}/"
              f"{checker.last_checks}  first_attempt_timeouts="
              f"{new_diag['total']} (warm={new_diag['warm']} cold={new_diag['cold']} "
              f"cf={new_diag['cf']})  slots={len(slots)}  wall={wall:.0f}ms  "
              f"cal_p50={rows[-1]['cal_p50']:.0f}ms cal_max={rows[-1]['cal_max']:.0f}ms")
        if i < polls and inter_poll_sec > 0:
            await asyncio.sleep(inter_poll_sec)
    return rows


# ---------------------------------------------------------------------------
# PART B — mechanism / option comparison (direct _check_date drive)
# ---------------------------------------------------------------------------

async def _drive_dates(checker: AvailabilityChecker, dates: list[date],
                       cap: int | None) -> None:
    """Run _check_date for every date concurrently (abort disabled), optionally
    capped to <=cap in flight via a semaphore."""
    sem = asyncio.Semaphore(cap) if cap else None

    async def one(d: date):
        if sem is not None:
            async with sem:
                return await checker._check_date(d, keep_page=True,
                                                 abort_event=None)
        return await checker._check_date(d, keep_page=True, abort_event=None)

    await asyncio.gather(*(one(d) for d in dates), return_exceptions=True)


async def _run_regime(name: str, checker: AvailabilityChecker, diag: DiagCapture,
                      probe: LatencyProbe, dates: list[date], polls: int,
                      *, fresh_each_poll: bool, cap: int | None,
                      inter_poll_sec: float) -> dict:
    await checker.close_sniper_pages()
    agg_warm: list[dict] = []
    agg_cold: list[dict] = []
    fa = {"warm": 0, "cold": 0, "cf": 0, "total": 0}
    print(f"\n-- regime: {name}  (dates={len(dates)}, polls={polls}, "
          f"cap={cap or 'none'}, fresh_each_poll={fresh_each_poll}) --")
    for i in range(1, polls + 1):
        if fresh_each_poll:
            await checker.close_sniper_pages()  # force cold every poll
        mark = diag.snapshot()
        probe.reset()
        t0 = time.perf_counter()
        await _drive_dates(checker, dates, cap)
        wall = (time.perf_counter() - t0) * 1000.0
        d = classify(diag.since(mark))
        for k in fa:
            fa[k] += d[k]
        warm_s = _latency_summary(probe.samples, reused=True)
        cold_s = _latency_summary(probe.samples, reused=False)
        agg_warm += [s for s in probe.samples if s["reused"]]
        agg_cold += [s for s in probe.samples if not s["reused"]]
        kind = "cold(fresh)" if not probe.samples or all(
            not s["reused"] for s in probe.samples) else "mixed/warm"
        print(f"   poll {i}: wall={wall:.0f}ms  first_attempt_timeouts="
              f"{d['total']} (warm={d['warm']} cold={d['cold']} cf={d['cf']})  "
              f"cold_ok={cold_s['ok']}/{cold_s['n']} p50={cold_s['p50_ms']:.0f} "
              f"warm_ok={warm_s['ok']}/{warm_s['n']} p50={warm_s['p50_ms']:.0f}")
        if i < polls and inter_poll_sec > 0:
            await asyncio.sleep(inter_poll_sec)
    return {
        "regime": name, "cap": cap, "fresh_each_poll": fresh_each_poll,
        "first_attempt_timeouts": fa,
        "warm_latency": _latency_summary(agg_warm, reused=None),
        "cold_latency": _latency_summary(agg_cold, reused=None),
    }


async def part_b(checker: AvailabilityChecker, diag: DiagCapture,
                 probe: LatencyProbe, polls: int, cap: int,
                 inter_poll_sec: float):
    print("\n" + "=" * 72)
    print("PART B — mechanism / option comparison (direct _check_date, "
          "abort disabled, uniform 12s budget)")
    print("=" * 72)
    dates = checker._get_target_dates(checker.config.preferred_days,
                                      sniper_mode=True)
    print(f"target dates ({len(dates)}): "
          f"{', '.join(d.isoformat() for d in dates)}")

    results = []
    # cold-fresh: every poll opens fresh pages (option 1 — drop reuse)
    results.append(await _run_regime(
        "cold-fresh (option 1: no reuse)", checker, diag, probe, dates, polls,
        fresh_each_poll=True, cap=None, inter_poll_sec=inter_poll_sec))
    # warm-uncapped: poll1 cold then reload all concurrently (current prod)
    results.append(await _run_regime(
        "warm-uncapped (current prod)", checker, diag, probe, dates, polls + 1,
        fresh_each_poll=False, cap=None, inter_poll_sec=inter_poll_sec))
    # warm-capped: poll1 cold then reload with <=cap in flight (option 2/3)
    results.append(await _run_regime(
        f"warm-cap-{cap} (option 2/3: throttle)", checker, diag, probe, dates,
        polls + 1, fresh_each_poll=False, cap=cap,
        inter_poll_sec=inter_poll_sec))
    return results


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main_async(args):
    _isolate_cookies()
    config = _build_config(args.slug)
    bro = TockBrowser(config)

    class _NullTracker:
        def record(self, *_a, **_k): pass
        def record_deferred(self, *_a, **_k): pass
        def flush_deferred(self, *_a, **_k): pass

    await bro.start()
    checker = AvailabilityChecker(config, bro, _NullTracker())
    checker._save_error_screenshot = _noop_screenshot  # type: ignore[assignment]

    diag = DiagCapture()
    logging.getLogger("src.checker").addHandler(diag)

    probe = LatencyProbe(checker)
    probe.install()

    summary = {}
    try:
        if args.a_polls > 0:
            summary["part_a"] = await part_a(
                checker, diag, probe, args.a_polls, args.inter_poll_sec,
                fresh_each_poll=args.fresh_each_poll)
        if args.b_polls > 0:
            summary["part_b"] = await part_b(
                checker, diag, probe, args.b_polls, args.cap,
                args.inter_poll_sec)
    finally:
        probe.restore()
        await checker.close_sniper_pages()
        await bro.close()

    print("\n" + "=" * 72)
    print("MACHINE-READABLE SUMMARY")
    print("=" * 72)
    print(json.dumps(summary, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="benu")
    ap.add_argument("--a-polls", type=int, default=4)
    ap.add_argument("--b-polls", type=int, default=2)
    ap.add_argument("--cap", type=int, default=6)
    ap.add_argument("--inter-poll-sec", type=float, default=2.0)
    ap.add_argument("--fresh-each-poll", action="store_true",
                    help="Part A: close pages before every poll (all cold) — "
                         "the fresh-continuous arm of the degradation A/B.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING,  # quiet src.checker INFO noise; keep WARN+ (diag)
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("warm_vs_cold").setLevel(logging.INFO)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
