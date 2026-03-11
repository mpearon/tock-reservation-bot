"""
Load and validate configuration from .env.
All settings live here — nothing else imports os.getenv directly.
"""

import os
from dataclasses import dataclass, field
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

    # Normal release-window polling (60s)
    release_window_days: list[str]   # e.g. ["Monday"]
    release_window_start: str        # "09:00" PT
    release_window_end: str          # "11:00" PT

    # Sniper mode (2-3s polling around specific release times)
    sniper_days: list[str]           # e.g. ["Wednesday", "Friday"]
    sniper_times: list[str]          # start times, e.g. ["16:59", "19:59"]
    sniper_duration_min: int         # how long each sniper window lasts
    sniper_interval_sec: int         # sleep between polls in sniper mode


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
    )
