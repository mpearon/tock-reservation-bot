"""
Slot release tracker.

Every time the bot detects a new available slot it records:
  - When we saw it (timestamp)
  - What date/time the slot is for
  - Day of week
  - How many days ahead it is from today

Data is written to:
  slot_tracker.json  — machine-readable, full history
  slot_tracker.csv   — open in Excel/Sheets to spot release patterns

Use these logs to figure out WHEN Fuhuihua actually drops new reservations,
then tune RELEASE_WINDOW_DAYS / RELEASE_WINDOW_START / RELEASE_WINDOW_END in .env.
"""

import csv
import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TRACKER_JSON = Path("slot_tracker.json")
TRACKER_CSV = Path("slot_tracker.csv")

CSV_FIELDS = ["recorded_at", "slot_date", "slot_time", "day_of_week", "days_ahead"]


@dataclass
class SlotEvent:
    recorded_at: str   # ISO datetime string, e.g. "2024-03-10T09:15:00"
    slot_date: str     # "2024-03-15"
    slot_time: str     # "5:00 PM"
    day_of_week: str   # "Friday"
    days_ahead: int    # slot_date - today in days


class SlotTracker:
    def __init__(self):
        self._events: list[SlotEvent] = []
        # Track (slot_date, slot_time) pairs already recorded THIS session
        # so we don't spam the log with the same slot every poll cycle.
        self._seen_this_session: set[str] = set()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_deferred(self, slot_date: date, slot_time: str) -> bool:
        """Record a slot without flushing to disk. Call flush_deferred() later.

        Same dedup logic as record() but skips the save() call.
        Use during sniper mode to avoid ~50-100ms of blocking disk I/O.
        """
        key = f"{slot_date.isoformat()}|{slot_time}"

        if key in self._seen_this_session:
            return False

        self._seen_this_session.add(key)
        existing_keys = {f"{e.slot_date}|{e.slot_time}" for e in self._events}
        is_new = key not in existing_keys

        event = SlotEvent(
            recorded_at=datetime.now().isoformat(timespec="seconds"),
            slot_date=slot_date.isoformat(),
            slot_time=slot_time,
            day_of_week=slot_date.strftime("%A"),
            days_ahead=(slot_date - date.today()).days,
        )
        self._events.append(event)
        self._pending_flush = True

        if is_new:
            logger.info(
                f"[tracker] NEW slot: {event.slot_date} ({event.day_of_week}) "
                f"at {event.slot_time} — {event.days_ahead} days ahead"
            )
        return is_new

    def flush_deferred(self) -> None:
        """Flush any pending deferred records to disk."""
        if getattr(self, '_pending_flush', False):
            self.save()
            self._pending_flush = False

    def record(self, slot_date: date, slot_time: str) -> bool:
        """
        Record a detected slot.

        Returns True if this is the first time we've seen this slot
        (either ever, or at least in this session).
        Deduplicates within the session; historical duplicates are also skipped.
        """
        key = f"{slot_date.isoformat()}|{slot_time}"

        if key in self._seen_this_session:
            return False  # already noted this cycle

        self._seen_this_session.add(key)

        # Also skip if this exact event was loaded from a previous session
        existing_keys = {
            f"{e.slot_date}|{e.slot_time}" for e in self._events
        }
        is_new = key not in existing_keys

        event = SlotEvent(
            recorded_at=datetime.now().isoformat(timespec="seconds"),
            slot_date=slot_date.isoformat(),
            slot_time=slot_time,
            day_of_week=slot_date.strftime("%A"),
            days_ahead=(slot_date - date.today()).days,
        )
        self._events.append(event)

        if is_new:
            logger.info(
                f"[tracker] NEW slot: {event.slot_date} ({event.day_of_week}) "
                f"at {event.slot_time} — {event.days_ahead} days ahead"
            )
        else:
            logger.debug(
                f"[tracker] Slot still available (seen before): "
                f"{event.slot_date} {event.slot_time}"
            )

        self.save()
        return is_new

    def save(self) -> None:
        """Flush all events to JSON and CSV."""
        data = [asdict(e) for e in self._events]

        TRACKER_JSON.write_text(json.dumps(data, indent=2))

        with TRACKER_CSV.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(data)

        logger.debug(
            f"[tracker] Saved {len(self._events)} events "
            f"→ {TRACKER_JSON}, {TRACKER_CSV}"
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not TRACKER_JSON.exists():
            return
        try:
            raw = json.loads(TRACKER_JSON.read_text())
            self._events = [SlotEvent(**r) for r in raw]
            logger.info(
                f"[tracker] Loaded {len(self._events)} historical events "
                f"from {TRACKER_JSON}"
            )
        except Exception as e:
            logger.warning(f"[tracker] Could not load existing tracker data: {e}")
