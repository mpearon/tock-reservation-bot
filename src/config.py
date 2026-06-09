"""
Load and validate configuration from .env.
All settings live here — nothing else imports os.getenv directly.
"""

import os
from dataclasses import dataclass, field
from datetime import time
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Credentials
    tock_email: str
    tock_password: str
    card_cvc: str          # CVC for saved payment card; "" = not set

    # Notifications
    discord_webhook_url: str   # empty string = disabled

    # Browser
    headless: bool

    # Mode
    dry_run: bool

    # Restaurant
    restaurant_slug: str

    # Booking preferences
    party_size: int
    preferred_days: list[str]   # e.g. ["Friday", "Saturday", "Sunday"] — booked first
    fallback_days: list[str]    # e.g. ["Monday", "Tuesday"] — only if no preferred slots found
    preferred_time: str         # e.g. "17:00" (24-hour)
    scan_weeks: int
    scan_weeks_lookahead: int

    # Normal release-window polling (60s)
    release_window_days: list[str]   # e.g. ["Monday"]
    release_window_start: str        # "09:00" PT
    release_window_end: str          # "11:00" PT

    # Sniper mode (2-3s polling around specific release times)
    sniper_days: list[str]           # e.g. ["Wednesday", "Friday"]
    sniper_times: list[str]          # start times, e.g. ["16:59", "19:59"]
    sniper_duration_min: int         # how long each sniper window lasts
    sniper_interval_sec: int         # sleep between polls in sniper mode
    sniper_scan_weeks: int = 2       # scan-range cap during sniper mode (Tock releases ≤2 wks)
    prewarm_min_days_out: int = 5    # skip dates closer than this during target-date prewarm

    # Sniper page-reuse. When True, sniper mode keeps one Playwright page open
    # per target date across polls and reloads them (page.reload) instead of
    # navigating fresh; prewarm parks pages for the first poll to reuse.
    # DEFAULT FALSE after the 2026-05-31 investigation
    # (spikes/warm_vs_cold_repro.py): repeatedly reloading ~14 kept-open SPA
    # pages under concurrent sniper load accumulates renderer state and starves
    # hydration — ~19% first-attempt calendar-load timeouts with unrecoverable
    # bursts (6/14, 5/14) vs ~3.6% all-recovered for fresh pages, and it was
    # not faster. Fresh-page-per-poll is the reliable default; the detection
    # scan is the load-bearing post-release safety net. Set
    # SNIPER_REUSE_PAGES=true to opt back into reuse (also keeps the warm
    # page→booker handoff).
    sniper_reuse_pages: bool = False

    # B1.5 — skip _click_day when the URL date is authoritative.
    # When True, checker tries _collect_slots_multi without clicking the
    # calendar day first; falls back to clicking + retrying once if 0 slots
    # were found. Booker mirrors this on the owns-page branch.
    # Default False — flipped to True only after a real release window
    # confirms the SPA URL alone is enough.
    skip_day_click_check: bool = False

    # B3.2 — event-driven slot detection (telemetry first pass).
    # When True, checker registers a Playwright `response` listener
    # during _check_date and logs every matching XHR to
    # xhr_telemetry.jsonl. Operator inspects the file to identify the
    # actual slot-availability XHR pattern, then sets
    # event_driven_url_pattern to narrow recording. A future commit
    # will plug a JSON parser into the listener to skip the DOM scan
    # entirely. Default False — telemetry only.
    event_driven_detection: bool = False
    event_driven_url_pattern: str = ""

    # Page pool (Phase B3.3) — pre-warmed Playwright pages reduce cold-task
    # latency in races. 0 disables the pool entirely (operator side switch).
    page_pool_size: int = 4

    # B3.2 fast-path — SPA-header replay (76× detection speedup, empirical).
    # When True, AvailabilityChecker.check_all uses src/calendar_replay.py
    # to fetch slot availability via in-browser fetch() with replayed Tock
    # SPA headers (~160ms per poll vs ~12s today). Falls back to the
    # existing per-date page-reload path on any failure.
    # Default OFF — opt in after verifying against benu via
    # spikes/http_replay/validate_replay.py.
    use_calendar_replay: bool = False

    # Per-date cap for replay-mode slot output. The protobuf body
    # contains anchor times only (~5-6 per date for benu/fuhuihua-style
    # dinner restaurants), so cap=5 effectively keeps everything Tock
    # offers while bounding worst-case booker fanout to ~30 race
    # candidates (5 dates × ~6 anchors). Lower this if you see Tock
    # rate-limiting under high fanout; raise it if you want to race
    # more options per date.
    replay_per_date_cap: int = 5

    # Debug
    debug_screenshots: bool = False  # save screenshots each poll (slow, skip in sniper)


def load_config() -> Config:
    email = os.getenv("TOCK_EMAIL", "").strip()
    password = os.getenv("TOCK_PASSWORD", "").strip()

    if not email or not password:
        raise ValueError(
            "TOCK_EMAIL and TOCK_PASSWORD must be set in .env\n"
            "Copy .env.example → .env and fill in your credentials."
        )

    return Config(
        tock_email=email,
        tock_password=password,
        card_cvc=os.getenv("TOCK_CARD_CVC", "").strip(),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        headless=os.getenv("HEADLESS", "false").lower() == "true",
        dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        restaurant_slug=os.getenv("RESTAURANT_SLUG", "fui-hui-hua-san-francisco"),
        party_size=int(os.getenv("PARTY_SIZE", "2")),
        preferred_days=[
            d.strip()
            for d in os.getenv("PREFERRED_DAYS", "Friday,Saturday,Sunday").split(",")
        ],
        fallback_days=[
            d.strip()
            for d in os.getenv("FALLBACK_DAYS", "").split(",")
            if d.strip()
        ],
        preferred_time=os.getenv("PREFERRED_TIME", "17:00"),
        scan_weeks_lookahead=int(os.getenv("SCAN_WEEKS_LOOKAHEAD", "2")),
        scan_weeks=int(os.getenv("SCAN_WEEKS", "2")),
        release_window_days=[
            d.strip()
            for d in os.getenv("RELEASE_WINDOW_DAYS", "Monday").split(",")
        ],
        release_window_start=os.getenv("RELEASE_WINDOW_START", "09:00"),
        release_window_end=os.getenv("RELEASE_WINDOW_END", "11:00"),
        sniper_days=[
            d.strip()
            for d in os.getenv("SNIPER_DAYS", "Wednesday,Friday").split(",")
        ],
        sniper_times=[
            t.strip()
            for t in os.getenv("SNIPER_TIMES", "16:59,19:59").split(",")
        ],
        sniper_duration_min=int(os.getenv("SNIPER_DURATION_MIN", "11")),
        sniper_interval_sec=int(os.getenv("SNIPER_INTERVAL_SEC", "3")),
        sniper_scan_weeks=int(os.getenv("SNIPER_SCAN_WEEKS", "2")),
        prewarm_min_days_out=int(os.getenv("PREWARM_MIN_DAYS_OUT", "5")),
        sniper_reuse_pages=os.getenv("SNIPER_REUSE_PAGES", "false").lower() == "true",
        skip_day_click_check=os.getenv("SKIP_DAY_CLICK_CHECK", "false").lower() == "true",
        event_driven_detection=os.getenv("EVENT_DRIVEN_DETECTION", "false").lower() == "true",
        event_driven_url_pattern=os.getenv("EVENT_DRIVEN_URL_PATTERN", "").strip(),
        page_pool_size=int(os.getenv("PAGE_POOL_SIZE", "4")),
        use_calendar_replay=os.getenv("USE_CALENDAR_REPLAY", "false").lower() == "true",
        replay_per_date_cap=int(os.getenv("REPLAY_PER_DATE_CAP", "5")),
        debug_screenshots=os.getenv("DEBUG_SCREENSHOTS", "false").lower() == "true",
    )


def parse_time(s: str) -> time:
    """Parse 'HH:MM' into datetime.time. Used by monitor and checker."""
    h, m = map(int, s.split(":"))
    return time(h, m)
