# tock-reservation-bot

Automated reservation bot for **Fuhuihua (Fui Hui Hua)** in San Francisco on [Tock](https://www.exploretock.com/fui-hui-hua-san-francisco).

Monitors for available Friday/Saturday/Sunday evening slots, books the best one automatically, and tracks release patterns so you can tune the polling schedule over time.

---

## Features

- **Sniper mode** — polls every ~3 seconds around configured release times (e.g. Wed/Fri 5pm and 8pm PT) to catch slots the instant they drop
- **Smart polling** — 60s during normal release windows, 5min on preferred evenings, 15min daytime, 60min overnight
- **Concurrent booking race** — when multiple preferred days have slots, attempts all simultaneously; cancels the rest the moment one succeeds
- **Discord notifications** — rich embeds for booking confirmations, slot detections, sniper mode activations, and errors
- **Slot release tracker** — logs every detected slot to `slot_tracker.json` / `.csv` so you can reverse-engineer Fuhuihua's release schedule
- **Dry-run mode** — runs the full flow (login → detect → select slot) but stops before clicking confirm
- **Selector verification** — `--verify` flag tests every DOM selector against the live site and tells you which ones need updating
- **Payment-card pause** — if no card is saved, the bot pauses and notifies you so you can add one, then resumes automatically
- **Session persistence** — saves cookies after first login; subsequent runs skip re-authentication

---

## Requirements

- Python 3.11+
- A [Tock](https://www.exploretock.com) account

---

## Setup

```bash
# 1. Clone / enter the project
cd tock-reservation-bot

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright browser (Chromium)
playwright install chromium

# 5. Create your .env file
cp .env.example .env
# Then open .env and fill in TOCK_EMAIL and TOCK_PASSWORD
```

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `TOCK_EMAIL` | *(required)* | Your Tock account email |
| `TOCK_PASSWORD` | *(required)* | Your Tock account password |
| `DISCORD_WEBHOOK_URL` | *(optional)* | Discord webhook for booking alerts |
| `HEADLESS` | `false` | `false` = browser visible (debug); `true` = silent background mode |
| `DRY_RUN` | `false` | `true` = simulate without booking |
| `RESTAURANT_SLUG` | `fui-hui-hua-san-francisco` | Tock URL slug for the restaurant |
| `PARTY_SIZE` | `2` | Number of guests |
| `PREFERRED_DAYS` | `Friday,Saturday,Sunday` | Days to target (full English names) |
| `PREFERRED_TIME` | `17:00` | Preferred time in 24-hour format |
| `SCAN_WEEKS` | `4` | How many weeks ahead to scan |
| `RELEASE_WINDOW_DAYS` | `Monday` | Day(s) for 60-second polling (normal release window) |
| `RELEASE_WINDOW_START` | `09:00` | Start of release window (PT, 24-hr) |
| `RELEASE_WINDOW_END` | `11:00` | End of release window (PT, 24-hr) |
| `SNIPER_DAYS` | `Wednesday,Friday` | Day(s) to enter sniper mode |
| `SNIPER_TIMES` | `16:59,19:59` | Sniper window start times (PT, 24-hr, comma-separated) |
| `SNIPER_DURATION_MIN` | `11` | How many minutes each sniper window lasts |
| `SNIPER_INTERVAL_SEC` | `3` | Sleep between polls in sniper mode (seconds) |

---

## Usage

```bash
# Activate virtual environment first
source venv/bin/activate

# Recommended first run: verify selectors are current
python main.py --verify

# Test the full flow without booking anything
python main.py --dry-run

# Run one check and exit (good for cron)
python main.py --once

# Start continuous monitoring (runs until Ctrl+C)
python main.py
```

---

## Discord Notifications

The bot sends rich Discord embeds for key events:

| Event | Color |
|---|---|
| Reservation confirmed | 🟢 Green |
| Slots detected (not yet booked) | 🟡 Yellow |
| Sniper mode activated | 🟠 Orange |
| Booking failed / error | 🔴 Red |
| Dry-run would-have-booked | 🔵 Blue |

**Setup:**
1. Open your Discord server → Settings → Integrations → Webhooks → **New Webhook**
2. Choose a channel (e.g. `#tock-bot`)
3. Copy the webhook URL
4. Paste it into `.env` as `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`

Leave the variable blank or omit it to disable Discord — console logs always work.

---

## Sniper Mode

Sniper mode activates around the times Fuhuihua is likely to release new reservations, polling as fast as possible to catch slots the instant they drop.

**How it works:**

1. At the configured start time (e.g. `16:59` = 4:59pm PT), the bot enters sniper mode
2. Between every poll it sleeps only `SNIPER_INTERVAL_SEC` seconds (default 3s)
3. Each poll still takes 15–60s (browser page loads) — the 3s interval means zero idle time between checks
4. After `SNIPER_DURATION_MIN` minutes (default 11), sniper mode ends and the bot returns to normal scheduling
5. Discord sends an orange "Sniper Mode Active" embed when each window begins

**Current default windows** (update once you know Fuhuihua's actual schedule):

| Day | Window |
|---|---|
| Wednesday | 4:59pm → 5:10pm PT |
| Friday | 4:59pm → 5:10pm PT |
| Wednesday | 7:59pm → 8:10pm PT |
| Friday | 7:59pm → 8:10pm PT |

**Tuning with the slot tracker:**

Once `slot_tracker.csv` has a few entries, look at the `recorded_at` column. If all new slots appear Wednesday at 17:02, update `.env`:

```env
SNIPER_DAYS=Wednesday
SNIPER_TIMES=17:00
SNIPER_DURATION_MIN=5
```

---

## Payment Card — Important

Tock requires a saved payment card on your account to complete bookings for many restaurants. Fuhuihua may or may not require one.

**If no card is detected**, the bot will:
1. Pause before clicking "Complete reservation"
2. Send a desktop notification: *"Tock Bot: Add a Payment Card!"*
3. Log a prominent warning to the console
4. Poll every 15 seconds for up to **9 minutes** waiting for you to add a card
5. Once a card appears, it clicks confirm automatically

**To add a card to your Tock account:**
1. Go to https://www.exploretock.com/account/payment
2. Add a credit card
3. Return — the bot will detect it and complete the booking within 15 seconds

> **Note:** Tock holds a selected time slot for approximately 10 minutes. Adding your card and returning within that window is enough time.

---

## Slot Release Tracker

Every time the bot detects an available slot, it writes a record to:

- `slot_tracker.json` — full JSON array, machine-readable
- `slot_tracker.csv` — open in Excel or Google Sheets

Each record contains:

```json
{
  "recorded_at": "2024-03-11T09:02:15",
  "slot_date": "2024-03-15",
  "slot_time": "5:00 PM",
  "day_of_week": "Friday",
  "days_ahead": 4
}
```

**Analyzing the pattern:**

Open `slot_tracker.csv` and look at `recorded_at` for each `slot_date`. If slots always appear Monday at 10am, update your `.env`:

```env
RELEASE_WINDOW_DAYS=Monday
RELEASE_WINDOW_START=09:45
RELEASE_WINDOW_END=10:15
```

This narrows the aggressive polling window and reduces unnecessary load on Tock's servers.

---

## Selector Maintenance

Tock periodically redesigns their UI. If the bot stops detecting slots or fails to book, run:

```bash
python main.py --verify
```

This loads each relevant Tock page and tests every DOM selector. Output looks like:

```
✓ PASS   login_email
✓ PASS   calendar_container
✗ FAIL   available_slot_button
         selector : button.Consumer-resultsListItem.is-available
         → Update src/selectors.py
```

**To fix a failing selector:**
1. Open the restaurant page in Chrome
2. Open DevTools → right-click the element → "Inspect"
3. Find a stable CSS class or attribute to target
4. Update the value in `src/selectors.py` under the failing key
5. Re-run `python main.py --verify`

All selector keys and what they match are documented in `src/selectors.py`.

---

## Concurrent Booking Architecture

When slots are found on multiple preferred days (e.g. Friday AND Saturday), the bot:

1. Picks the best time slot per day (closest to 5pm)
2. Opens one Playwright page per day simultaneously
3. Navigates each page through the full checkout flow in parallel
4. Uses an `asyncio.Lock` around the final confirm click — only one task can confirm
5. Once one booking succeeds, the shared `asyncio.Event` is set and all other tasks abort before clicking confirm

This means **at most one booking is made**, even if multiple slots were available simultaneously.

---

## Logs

- Console output: all INFO+ messages with timestamps
- `bot.log`: same output persisted to disk (appended each run)
- `slot_tracker.json` / `.csv`: availability history

---

## Running in the Background

```bash
# Start in background, log to file
nohup python main.py > /dev/null 2>&1 &

# Or use screen
screen -S tockbot
python main.py
# Ctrl+A, D to detach; screen -r tockbot to reattach
```

For production, set `HEADLESS=true` in `.env` first.

---

## Project Structure

```
tock-reservation-bot/
├── .env                   # Your secrets (gitignored)
├── .env.example           # Template — commit this, not .env
├── .gitignore
├── README.md
├── requirements.txt
├── main.py                # Entry point + CLI
└── src/
    ├── config.py          # Load & validate .env settings
    ├── selectors.py       # All DOM selectors + --verify logic
    ├── browser.py         # Playwright setup, login, session cookies
    ├── checker.py         # Scan dates for available slots
    ├── booker.py          # Navigate checkout + confirm booking
    ├── monitor.py         # Polling loop + smart schedule
    ├── notifier.py        # Console logs + desktop notifications
    └── tracker.py         # Slot release history (JSON + CSV)
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Login fails immediately | Check `.env` credentials; run `HEADLESS=false` to see if a CAPTCHA appears |
| Calendar not loading | Run `--verify`; delete `session_cookies.json` and re-login |
| Bot books wrong time | Slots are sorted by proximity to `PREFERRED_TIME`; confirm that value is correct |
| Discord notifications not arriving | Check `DISCORD_WEBHOOK_URL` is correct; verify channel permissions; console logs always work |
| Sniper mode not activating | Confirm `SNIPER_DAYS` matches the current weekday name exactly (e.g. `Wednesday` not `Wed`) |
| Cloudflare challenge appears | Run headed (`HEADLESS=false`), solve once manually — the clearance cookie is saved |
| Selector failures after Tock update | Run `--verify`, find new selectors in DevTools, update `src/selectors.py` |

---

## Disclaimer

This bot is for personal use. Use it responsibly and in accordance with Tock's Terms of Service. Do not run it at aggressive polling intervals unnecessarily.
