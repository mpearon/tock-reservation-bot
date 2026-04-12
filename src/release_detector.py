"""
Release time detector.

Scrapes the Fuhuihua Tock page and looks for text like:
  "All reservations sold out."
  "New reservations will be released on March 13, 2026 at 8:00 PM PDT."

If found, parses the release datetime and updates config.sniper_days /
config.sniper_times so the sniper fires 1 minute before the drop.

Called once on bot startup and then every CHECK_INTERVAL_MIN minutes so
the schedule stays accurate if Fuhuihua changes the release time.
"""

import logging
import re
from datetime import datetime, timedelta

import pytz

logger = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")

# How often (minutes) to re-check the release time while the bot is running
CHECK_INTERVAL_MIN = 60

# Regex that matches the release announcement.
# Examples this handles:
#   "New reservations will be released on March 13, 2026 at 8:00 PM PDT."
#   "New reservations will be released on March 13, 2026 at 8:00 PM PST."
#   "New reservations will be released on March 13, 2026 at 8:00 PM PT."
_RELEASE_RE = re.compile(
    r"New reservations will be released on\s+"
    r"([A-Za-z]+ \d{1,2},\s*\d{4})"   # group 1: "March 13, 2026"
    r"\s+at\s+"
    r"(\d{1,2}:\d{2}\s*[AP]M)"         # group 2: "8:00 PM"
    r"(?:\s+(\w+))?",                   # group 3: "PDT" / "PST" / "PT" (optional)
    re.IGNORECASE,
)

# Also match a "sold out" indicator so we know we're on the right page section
_SOLD_OUT_RE = re.compile(r"All reservations sold out", re.IGNORECASE)


async def detect_release_time(browser, config) -> datetime | None:
    """
    Load the restaurant's Tock page and look for a release announcement.

    Returns a timezone-aware datetime (America/Los_Angeles) for the
    announced release time, or None if no announcement is found.
    """
    url = f"https://www.exploretock.com/{config.restaurant_slug}"
    page = await browser.new_page()
    try:
        logger.info(f"[release-detect] Loading {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Give React a moment to render dynamic content
        await page.wait_for_timeout(4000)

        text = await page.evaluate("() => document.body.innerText")

        if not _SOLD_OUT_RE.search(text):
            logger.info("[release-detect] 'All reservations sold out' not found — slots may be available!")
            return None

        match = _RELEASE_RE.search(text)
        if not match:
            logger.info("[release-detect] Sold out but no release date announced yet.")
            return None

        date_str = match.group(1).strip()   # "March 13, 2026"
        time_str = match.group(2).strip()   # "8:00 PM"
        tz_str   = (match.group(3) or "PT").strip().upper()

        logger.info(
            f"[release-detect] Found: '{date_str} at {time_str} {tz_str}'"
        )

        # Parse naive datetime (%d handles both "3" and "03")
        naive_dt = datetime.strptime(f"{date_str} {time_str}", "%B %d, %Y %I:%M %p")

        # Localize to PT (handles PDT/PST automatically via America/Los_Angeles)
        release_dt = PT.localize(naive_dt)
        logger.info(f"[release-detect] Release datetime: {release_dt}")
        return release_dt

    except Exception as e:
        logger.error(f"[release-detect] Error scraping release time: {e}")
        return None
    finally:
        await page.close()


def apply_release_schedule(config, release_dt: datetime) -> bool:
    """
    Update config.sniper_days and config.sniper_times so the sniper fires
    1 minute before *release_dt*. Returns True if the schedule changed.

    Sniper window: 1 minute before release → release + SNIPER_DURATION_MIN.
    """
    sniper_start = release_dt - timedelta(minutes=1)
    sniper_day   = sniper_start.strftime("%A")          # e.g. "Friday"
    sniper_time  = sniper_start.strftime("%H:%M")       # e.g. "19:59"

    old_days  = config.sniper_days[:]
    old_times = config.sniper_times[:]

    config.sniper_days  = [sniper_day]
    config.sniper_times = [sniper_time]

    changed = (config.sniper_days != old_days or config.sniper_times != old_times)
    if changed:
        logger.info(
            f"[release-detect] Sniper schedule updated: "
            f"{sniper_day} @ {sniper_time} PT "
            f"(release at {release_dt.strftime('%B %d, %Y %I:%M %p %Z')})"
        )
    else:
        logger.info(
            f"[release-detect] Sniper schedule unchanged: "
            f"{sniper_day} @ {sniper_time} PT"
        )
    return changed
