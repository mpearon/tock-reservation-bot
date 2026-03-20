# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Automates snagging a reservation at Fuhuihua SF (`fui-hui-hua-san-francisco`) on Tock. It polls for available slots, and when the restaurant drops new reservations (Friday ~8 PM PT), it fires a "sniper mode" that hammers the search page continuously until a slot appears, then books it automatically and notifies via Discord.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # fill in credentials
```

## Running

```bash
# Production (runs forever)
python main.py

# One-shot check and exit
python main.py --once

# Dry-run — goes through full flow, skips the final confirm click
python main.py --dry-run

# Verify all DOM selectors are still valid against the live site
python main.py --verify

# Test Discord notifications for every embed type
python main.py --test-notify
```

## Test commands

All test modes force `DRY_RUN=true` and never book anything. The default `--test-restaurant` is `benu`.

```bash
# Navigate to Benu checkout, fill CVC, screenshot — STOP before confirm
python main.py --test-booking-flow
python main.py --test-booking-flow --test-restaurant SLUG

# 3-part robustness test: pre-warm cookies, page-reuse timing, confirm-retry
python main.py --test-sniper
python main.py --test-sniper --test-restaurant SLUG

# Full end-to-end chain: sets sniper 2 min from now, watches pre-warm fire,
# waits for window, runs N rapid polls, sends real Discord notifications
python main.py --test-sniper-integration
python main.py --test-sniper-integration --test-sniper-polls 5

# A/B benchmark: sequential vs concurrent scan speed and Cloudflare error rate
python main.py --test-sniper-benchmark
python main.py --test-sniper-benchmark --test-restaurant SLUG --test-sniper-polls 10

# Test adaptive concurrent→sequential→concurrent switching (error threshold forced to 0%)
python main.py --test-adaptive-sniper
python main.py --test-adaptive-sniper --test-restaurant SLUG --test-sniper-polls 8
```

## Architecture

### Data flow during a sniper poll cycle

```
monitor.run()
  │
  ├─ _get_prewarm_target()  →  warm_session()  [fires 15 min before window]
  │    browser.warm_session(): navigate restaurant main page, save fresh CF cookies
  │
  ├─ _get_poll_interval()   →  0s when in sniper window (sets _sniper_active=True)
  │    Also fires notifier.sniper_mode_active() Discord embed on window entry.
  │
  └─ poll()
       │
       ├─ checker.check_all(concurrent=True, keep_pages=True)
       │    Phase 1: preferred_days (Fri/Sat/Sun) — if slots found, skip Phase 2
       │    Phase 2: fallback_days (Mon-Thu) — only if Phase 1 empty
       │    keep_pages=True: reuses open Playwright pages via page.reload() (faster)
       │    concurrent=True: all dates loaded in parallel via asyncio.gather()
       │    Adaptive: if rolling calendar error rate >20%, falls back to sequential
       │
       ├─ if no slots → notifier.no_slots_found() [console only, no Discord]
       │
       └─ if slots found (DRY_RUN=false) → booker.book_best_slot_race(slots)
            One asyncio task per unique calendar date, all run concurrently.
            asyncio.Lock() ensures only one task executes the confirm click.
            asyncio.Event() signals the winner → others abort immediately.
```

### Module responsibilities

| File | Responsibility |
|------|---------------|
| `main.py` | CLI entry point, all `--test-*` implementations |
| `src/config.py` | Single `Config` dataclass; only file that reads `os.getenv` |
| `src/selectors.py` | All DOM selectors in one `SELECTORS` dict + `--verify` logic |
| `src/browser.py` | Launch Chromium with stealth, login, cookie persistence, `warm_session()` |
| `src/checker.py` | Date scanning, calendar interaction, page reuse (`_sniper_pages`) |
| `src/booker.py` | Slot click → checkout → CVC fill → confirm, with retry logic |
| `src/monitor.py` | Polling loop, sniper scheduling, adaptive mode switching, pre-warm timing |
| `src/notifier.py` | Console logs + Discord webhook embeds (fire-and-forget async tasks) |
| `src/release_detector.py` | Scrapes restaurant page for "New reservations will be released on…" text; auto-updates sniper schedule |
| `src/tracker.py` | Appends detected slots to `slot_tracker.json`/`.csv` for pattern analysis |

### Key design decisions to understand before modifying

**Selectors** — all in `src/selectors.py`. When Tock redesigns their UI, the bot logs `SELECTOR_FAILED: key='...' selector='...'` with the exact key. Update the dict in `selectors.py` and run `--verify`.

**Page reuse in sniper mode** — `AvailabilityChecker._sniper_pages` is a `dict[date_str, Page]` that keeps Playwright pages alive across rapid polls. Second+ polls call `page.reload()` instead of `page.goto()`, which is faster and looks less bot-like. Pages are closed by `close_sniper_pages()` when the sniper window ends.

**Adaptive concurrent/sequential** — `monitor._sniper_concurrent` starts True. After each sniper poll, the rolling error rate (last 3 polls' `last_errors/last_checks`) is checked. Above 20% → switch to sequential. After 3 clean sequential polls → switch back to concurrent. Test with `--test-adaptive-sniper`.

**Sniper pre-warm timing** — `PREWARM_BEFORE_MIN = 15` in `monitor.py`. `_get_prewarm_target()` returns a `"DayName@HH:MM"` string when any sniper window starts within the next 15 minutes. `_session_prewarmed_for` tracks which window was already warmed so it only fires once per window.

**Release auto-detection** — `src/release_detector.py` scrapes the restaurant's main Tock page for the announcement text and calls `apply_release_schedule()` to update `config.sniper_days`/`config.sniper_times` to 1 minute before the announced release. Runs on startup and every 60 minutes.

**CVC in iframes** — Tock embeds the CVC field in a Stripe iframe. `booker._fill_cvc()` iterates `page.main_frame` plus all child frames to find it.

**No booking in test modes** — All `--test-*` flags check for or force `config.dry_run = True`. The `_book_single()` method returns immediately if `dry_run` is set. `--test-booking-flow` explicitly stops before the confirm click via early return.

### Polling schedule (America/Los_Angeles)

Priority order (highest first):
1. **Sniper window** — 0s sleep (continuous) during configured windows (default Wed/Fri @ 16:59, 19:59)
2. **Release window** — 60s during configured release days/times (default Mon 9–11am)
3. **Overnight** — 3600s midnight–7am any day
4. **Preferred evening** — 300s on preferred days (Fri/Sat/Sun) 5–11pm
5. **Default** — 900s all other times

### Runtime files (gitignored)

- `session_cookies.json` — Playwright context cookies including Cloudflare `cf_clearance`
- `slot_tracker.json` / `slot_tracker.csv` — every detected slot with timestamp
- `bot.log` — full log history
- `.env` — credentials and config (never commit)

### Cloudflare notes

- First run: use `HEADLESS=false` to manually solve any CAPTCHA, then switch to `HEADLESS=true`
- Session cookies (especially `cf_clearance`) persist across runs via `session_cookies.json`
- If the bot starts failing calendar loads in sniper mode, the adaptive logic will switch to sequential automatically; if it fails completely, delete `session_cookies.json` and re-run headed to re-authenticate

## MANDATORY: Test-Driven Development (TDD)

For ALL new code in this project:

1. Write tests FIRST — including edge cases and real API data formats
2. Run tests — verify they FAIL (red)
3. Write implementation code
4. Run tests — verify they PASS (green)
5. Only then commit

Tests must include:
- Happy path
- Edge cases (empty data, null fields, boundary values)
- Real response formats (not mocked/assumed schemas)
- Error recovery (what happens after a failure?)

NEVER write implementation before tests exist and fail.