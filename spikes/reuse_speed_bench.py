#!/usr/bin/env python
"""
spikes/reuse_speed_bench.py  —  INVESTIGATION SPIKE (not production code)

Answers "how much faster is warm page-reuse (page.reload) than fresh
navigation (page.goto)?" — the speed side of the sniper_reuse_pages decision.

Measures TIME-TO-CALENDAR-READY (navigation + wait_for_selector of
div.ConsumerCalendar-month), i.e. the moment a page is usable for
detection/booking. Two benches:

  BENCH 1 — intrinsic, concurrency=1: one page, cold goto vs N warm reloads.
    Isolates the per-page reload-vs-goto cost with NO event-loop contention.
    This is the BEST case for reuse and also proxies the booking re-nav cost
    (with reuse off, the booker opens a fresh page → pays one cold goto).

  BENCH 2 — realistic, concurrency=14: a full sniper poll. One COLD round
    (14 fresh goto) vs several WARM rounds (14 reload of kept pages). Reports
    ROUND wall-clock (time until ALL 14 calendars ready — the real poll
    latency) and per-page p50.

Safe: isolates session_cookies.json (temp copy, never writes prod), no
bot.lock, no login/warm_session, dry-run.

Usage: venv/bin/python spikes/reuse_speed_bench.py [--warm-iters 6] [--warm-rounds 3]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib.util  # noqa: E402

# Reuse the main harness's cookie-isolation + config helpers.
_spec = importlib.util.spec_from_file_location(
    "repro", str(Path(__file__).resolve().parent / "warm_vs_cold_repro.py"))
_repro = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repro)

from src.browser import TockBrowser  # noqa: E402
from src.checker import AvailabilityChecker  # noqa: E402
import src.selectors as _sel  # noqa: E402

CAL = _sel.SELECTORS["calendar_container"]
NAV_TIMEOUT = 10000
CAL_TIMEOUT = 12000


def _url(cfg, date_str: str) -> str:
    return (f"https://www.exploretock.com/{cfg.restaurant_slug}/search"
            f"?date={date_str}&size={cfg.party_size}&time={cfg.preferred_time}")


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def _stats(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    s = sorted(xs)
    p50 = s[len(s) // 2]
    p90 = s[min(len(s) - 1, int(0.9 * (len(s) - 1)))]
    return (f"n={len(s)} p50={p50:.0f}ms p90={p90:.0f}ms "
            f"min={s[0]:.0f} max={s[-1]:.0f} mean={sum(s)/len(s):.0f}")


async def _ready_after_goto(page, url) -> float:
    t0 = time.perf_counter()
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_selector(CAL, timeout=CAL_TIMEOUT)
    return _ms(t0)


async def _ready_after_reload(page) -> float:
    t0 = time.perf_counter()
    await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_selector(CAL, timeout=CAL_TIMEOUT)
    return _ms(t0)


async def bench1(bro, cfg, warm_iters: int):
    print("\n" + "=" * 72)
    print(f"BENCH 1 — intrinsic (concurrency=1): cold goto vs {warm_iters} warm reloads")
    print("=" * 72)
    date_str = (__import__("datetime").date.today()
                + __import__("datetime").timedelta(days=7)).isoformat()
    url = _url(cfg, date_str)
    # COLD: a FRESH page (new_page + goto) each iteration — the booker's path
    # when reuse is off. WARM: prime one page, then reload it repeatedly.
    cold = []
    for _ in range(warm_iters):
        p = await bro.new_page()
        try:
            cold.append(await _ready_after_goto(p, url))
        finally:
            await p.close()
    print(f"  COLD goto→ready:   {_stats(cold)}")

    page = await bro.new_page()
    try:
        await _ready_after_goto(page, url)  # prime (not measured)
        warm = []
        for _ in range(warm_iters):
            warm.append(await _ready_after_reload(page))
        print(f"  WARM reload→ready: {_stats(warm)}")
    finally:
        await page.close()

    if cold and warm:
        cp50 = sorted(cold)[len(cold) // 2]
        wp50 = sorted(warm)[len(warm) // 2]
        delta = cp50 - wp50
        faster = "FASTER" if delta > 0 else "SLOWER"
        print(f"  → reload is ~{abs(delta):.0f}ms {faster} than goto "
              f"(cold-p50 {cp50:.0f} vs warm-p50 {wp50:.0f})")


async def _round(bro, cfg, dates, pages, mode: str):
    """One concurrent round across all dates. mode='cold' opens fresh pages
    (returns them); mode='warm' reloads the supplied pages."""
    if mode == "cold":
        pages = [await bro.new_page() for _ in dates]

    per_page: list[float] = []

    async def one(idx, d):
        p = pages[idx]
        if mode == "cold":
            dt = await _ready_after_goto(p, _url(cfg, d))
        else:
            dt = await _ready_after_reload(p)
        per_page.append(dt)
        return dt

    t0 = time.perf_counter()
    await asyncio.gather(*(one(i, d) for i, d in enumerate(dates)),
                         return_exceptions=True)
    wall = _ms(t0)
    return wall, per_page, pages


async def bench2(bro, cfg, warm_rounds: int):
    print("\n" + "=" * 72)
    print(f"BENCH 2 — realistic (concurrency=14): 1 cold round vs {warm_rounds} warm rounds")
    print("=" * 72)
    checker = AvailabilityChecker(cfg, bro, None)
    dates = [d.isoformat() for d in
             checker._get_target_dates(cfg.preferred_days, sniper_mode=True)]
    print(f"  {len(dates)} dates")

    # COLD round: 14 fresh goto. Keep the pages to reload as the warm prime.
    wall, pp, pages = await _round(bro, cfg, dates, None, "cold")
    print(f"  COLD round (14× fresh goto):   round_wall={wall:.0f}ms  per-page {_stats(pp)}")

    try:
        for r in range(1, warm_rounds + 1):
            wall, pp, _ = await _round(bro, cfg, dates, pages, "warm")
            print(f"  WARM round {r} (14× reload):     round_wall={wall:.0f}ms  per-page {_stats(pp)}")
    finally:
        for p in pages:
            try:
                await p.close()
            except Exception:
                pass


async def main_async(args):
    _repro._isolate_cookies()
    cfg = _repro._build_config(args.slug)
    bro = TockBrowser(cfg)
    await bro.start()
    try:
        await bench1(bro, cfg, args.warm_iters)
        if args.warm_rounds > 0:
            await bench2(bro, cfg, args.warm_rounds)
    finally:
        await bro.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="benu")
    ap.add_argument("--warm-iters", type=int, default=6)
    ap.add_argument("--warm-rounds", type=int, default=3)
    args = ap.parse_args()
    import logging
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
