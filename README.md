# Tock Reservation Bot

Automated reservation sniper for restaurants on [Tock](https://www.exploretock.com). Monitors for available slots, fires a high-speed sniper mode around known release times, books the first available slot, and notifies you via Discord.

Built for [Fuhuihua SF](https://www.exploretock.com/fui-hui-hua-san-francisco) but works with any Tock restaurant — just change `RESTAURANT_SLUG` in `.env`. For a detailed write-up covering architecture, development timeline, bugs found and fixed, and lessons learned, see the [Project Report](https://charlieyang1557.github.io/tock-reservation-bot/report.html).

## Features

- **Sniper mode** — continuous polling (~3s intervals) during configurable release windows to catch slots the instant they drop
- **Release auto-detection** — scrapes the restaurant page for "New reservations will be released on..." text and auto-updates the sniper schedule
- **Session pre-warming** — navigates the restaurant page 15 minutes before a sniper window to refresh Cloudflare cookies
- **Adaptive concurrency** — polls all dates in parallel by default; if Cloudflare error rate exceeds 20%, falls back to sequential, then switches back after 3 clean polls
- **Concurrent booking race** — when multiple days have slots, attempts all simultaneously; `asyncio.Lock` ensures at most one booking completes
- **Smart polling schedule** — 0s sniper, 60s release window, 5min preferred evenings, 15min daytime, 60min overnight
- **Discord notifications** — rich embeds for confirmations, slot detections, sniper activations, and errors
- **Slot release tracker** — logs every detected slot to `slot_tracker.json`/`.csv` for release pattern analysis
- **Dry-run mode** — full flow (login, detect, select) without clicking confirm
- **Selector verification** — `--verify` tests every DOM selector against the live site
- **Payment-card pause** — if no saved card is detected at checkout, pauses up to 9 minutes and alerts you to add one
- **Cookie persistence** — saves session cookies after first login; subsequent runs skip re-authentication

## Requirements

- Python 3.11+
- A [Tock](https://www.exploretock.com) account

## Setup

```bash
git clone https://github.com/charlieyang1557/tock-reservation-bot.git
cd tock-reservation-bot

python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium

cp .env.example .env       # fill in TOCK_EMAIL and TOCK_PASSWORD
```

## Configuration

All settings live in `.env`. Copy from `.env.example` and edit.

| Variable | Default | Description |
|---|---|---|
| `TOCK_EMAIL` | *(required)* | Tock account email |
| `TOCK_PASSWORD` | *(required)* | Tock account password |
| `DISCORD_WEBHOOK_URL` | *(optional)* | Discord webhook URL for alerts |
| `HEADLESS` | `false` | `true` for background mode; `false` to see the browser |
| `DRY_RUN` | `false` | `true` to simulate without booking |
| `RESTAURANT_SLUG` | `fui-hui-hua-san-francisco` | Tock URL slug for the target restaurant |
| `PARTY_SIZE` | `2` | Number of guests |
| `PREFERRED_DAYS` | `Friday,Saturday,Sunday` | Target days (full English names, comma-separated) |
| `PREFERRED_TIME` | `17:00` | Preferred time (24-hour format) |
| `SCAN_WEEKS` | `2` | Weeks ahead to scan |
| `RELEASE_WINDOW_DAYS` | `Monday` | Day(s) for 60s polling |
| `RELEASE_WINDOW_START` | `09:00` | Release window start (PT) |
| `RELEASE_WINDOW_END` | `11:00` | Release window end (PT) |
| `SNIPER_DAYS` | `Wednesday,Friday` | Day(s) to enter sniper mode |
| `SNIPER_TIMES` | `16:59,19:59` | Sniper window start times (PT, comma-separated) |
| `SNIPER_DURATION_MIN` | `11` | Minutes each sniper window lasts |
| `SNIPER_INTERVAL_SEC` | `3` | Seconds between polls in sniper mode |

## Usage

```bash
source venv/bin/activate

# Verify selectors are current (recommended first run)
python main.py --verify

# Dry run — full flow, stops before confirm
python main.py --dry-run

# One-shot check and exit
python main.py --once

# Start continuous monitoring (runs until Ctrl+C)
python main.py

# Test Discord notification embeds
python main.py --test-notify
```

### Test commands

All test modes force `DRY_RUN=true` and never book anything. Default test restaurant is `benu`.

```bash
# Navigate to checkout, fill CVC, screenshot — stop before confirm
python main.py --test-booking-flow
python main.py --test-booking-flow --test-restaurant SLUG

# 3-part robustness test: pre-warm, page reuse, confirm retry
python main.py --test-sniper

# Full end-to-end: set sniper 2min from now, pre-warm, poll, Discord alerts
python main.py --test-sniper-integration
python main.py --test-sniper-integration --test-sniper-polls 5

# A/B benchmark: sequential vs concurrent scan speed and error rate
python main.py --test-sniper-benchmark
python main.py --test-sniper-benchmark --test-restaurant SLUG --test-sniper-polls 10

# Adaptive switching test (error threshold forced to 0%)
python main.py --test-adaptive-sniper
python main.py --test-adaptive-sniper --test-restaurant SLUG --test-sniper-polls 8
```

## How sniper mode works

1. **Auto-detect** — on startup (and every 60 min), `release_detector.py` scrapes the restaurant page for release announcements and updates the sniper schedule to 1 minute before the announced time
2. **Pre-warm** — 15 minutes before a window, the bot navigates the restaurant page to refresh Cloudflare cookies
3. **Sniper active** — at the window start time, polling switches to continuous (~3s intervals). Discord sends an orange "Sniper Mode Active" embed
4. **Adaptive concurrency** — all dates are polled in parallel. If the rolling error rate (last 3 polls) exceeds 20%, the bot switches to sequential. After 3 clean sequential polls, it switches back
5. **Book** — when a slot is found, the concurrent booking race kicks in. One booking fires; the rest abort
6. **Window end** — after `SNIPER_DURATION_MIN` minutes, sniper mode deactivates and normal scheduling resumes

## Discord notifications

| Event | Color |
|---|---|
| Reservation confirmed | Green |
| Slots detected (not yet booked) | Yellow |
| Sniper mode activated | Orange |
| Booking failed / error | Red |
| Dry-run would-have-booked | Blue |

**Setup:** Server Settings > Integrations > Webhooks > New Webhook. Copy the URL into `DISCORD_WEBHOOK_URL` in `.env`. Leave blank to disable — console logs always work.

## Slot release tracker

Every detected slot is logged to `slot_tracker.json` and `slot_tracker.csv`:

```json
{
  "recorded_at": "2025-03-11T09:02:15",
  "slot_date": "2025-03-15",
  "slot_time": "5:00 PM",
  "day_of_week": "Friday",
  "days_ahead": 4
}
```

Open the CSV and look at `recorded_at` to find the release pattern. Once you know it (e.g. every Wednesday at 5:02pm), tighten your sniper config:

```env
SNIPER_DAYS=Wednesday
SNIPER_TIMES=17:00
SNIPER_DURATION_MIN=5
```

## Project structure

```
tock-reservation-bot/
├── main.py                  # CLI entry point + all --test-* modes
├── requirements.txt
├── .env.example             # Template — copy to .env
└── src/
    ├── config.py            # Settings from .env (single Config dataclass)
    ├── selectors.py         # All DOM selectors + --verify logic
    ├── browser.py           # Playwright setup, stealth, login, cookie persistence
    ├── checker.py           # Date scanning, calendar interaction, page reuse
    ├── booker.py            # Checkout flow, CVC fill (Stripe iframe), confirm
    ├── monitor.py           # Polling loop, sniper scheduling, adaptive switching
    ├── notifier.py          # Console logs + Discord webhook embeds
    ├── release_detector.py  # Scrapes release announcements, auto-updates schedule
    └── tracker.py           # Slot history (JSON + CSV)
```

## Selector maintenance

Tock periodically updates their UI. When selectors break, the bot logs `SELECTOR_FAILED: key='...' selector='...'`.

```bash
python main.py --verify
```

Output shows which selectors pass and which need updating. Fix them in `src/selectors.py`, then re-verify.

## Cloudflare notes

- **First run:** use `HEADLESS=false` to manually solve any CAPTCHA, then switch to `HEADLESS=true`
- Session cookies (including `cf_clearance`) persist across runs via `session_cookies.json`
- If sniper mode starts failing, the adaptive logic switches to sequential automatically. If it fails completely, delete `session_cookies.json` and re-run headed

## Troubleshooting

| Symptom | Fix |
|---|---|
| Login fails | Check `.env` credentials; run `HEADLESS=false` to check for CAPTCHA |
| Calendar not loading | Run `--verify`; delete `session_cookies.json` and re-login headed |
| Bot books wrong time | Check `PREFERRED_TIME` — slots are sorted by proximity to it |
| Discord not working | Verify `DISCORD_WEBHOOK_URL`; console logs always work regardless |
| Sniper not activating | `SNIPER_DAYS` must be full names (`Wednesday` not `Wed`) |
| Cloudflare challenge | Run headed once, solve manually — clearance cookie is saved |
| Selectors broken after Tock update | Run `--verify`, inspect elements in DevTools, update `src/selectors.py` |

## Disclaimer

This bot is for personal use. Use responsibly and in accordance with Tock's Terms of Service.
