"""
Notifications — structured console logs + Discord webhook embeds.

Discord embeds use color codes:
  Green  (0x2ECC71) — booking confirmed
  Yellow (0xF1C40F) — slots detected
  Orange (0xE67E22) — sniper mode activated
  Red    (0xE74C3C) — error / booking failed
  Blue   (0x3498DB) — informational (poll start, dry run)

Set DISCORD_WEBHOOK_URL in .env to enable Discord alerts.
Leave it blank (or omit) to use console-only mode.

Discord calls are fire-and-forget asyncio tasks — they never block the bot.
"""

import asyncio
import logging
from datetime import datetime

import pytz

logger = logging.getLogger(__name__)

PT = pytz.timezone("America/Los_Angeles")

# Discord embed colors
_GREEN  = 0x2ECC71
_YELLOW = 0xF1C40F
_ORANGE = 0xE67E22
_RED    = 0xE74C3C
_BLUE   = 0x3498DB
_GREY   = 0x95A5A6


class Notifier:
    def __init__(self, config):
        self.config = config
        self._webhook_url: str = config.discord_webhook_url
        self._discord_enabled = bool(self._webhook_url)

        if self._discord_enabled:
            logger.info("[notify] Discord webhook configured ✓")
        else:
            logger.info("[notify] Discord webhook not configured — console only")

    # ------------------------------------------------------------------
    # Bot lifecycle
    # ------------------------------------------------------------------

    def poll_start(self, poll_num: int, next_interval_sec: int) -> None:
        logger.info(
            f"━━━ Poll #{poll_num} "
            f"(next in {_fmt_interval(next_interval_sec)}) ━━━"
        )

    def sniper_mode_active(self, day: str, trigger_time: str, until: str) -> None:
        msg = f"Sniper mode ACTIVE on {day} — triggered at {trigger_time} PT, runs until {until} PT"
        logger.info(f"[sniper] 🎯 {msg}")
        self._fire(
            title="🎯 Sniper Mode Active",
            description=msg,
            color=_ORANGE,
            fields=[
                ("Day", day, True),
                ("Trigger", trigger_time + " PT", True),
                ("Until", until + " PT", True),
            ],
        )

    def sniper_mode_ended(self, slots_found: int) -> None:
        msg = f"Sniper window ended. {slots_found} slot(s) detected during window."
        logger.info(f"[sniper] {msg}")

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def no_slots_found(self) -> None:
        logger.info("No available slots found this cycle.")

    def slots_found(self, slots: list) -> None:
        lines = [f"• {s.slot_date_str} ({s.day_of_week}) @ {s.slot_time}" for s in slots[:8]]
        extra = f"\n+{len(slots) - 8} more…" if len(slots) > 8 else ""
        summary = "\n".join(lines) + extra
        logger.info(f"[slots] {len(slots)} slot(s) found:\n{summary}")
        self._fire(
            title=f"🟡 {len(slots)} Slot(s) Available!",
            description=summary,
            color=_YELLOW,
        )

    # ------------------------------------------------------------------
    # Booking
    # ------------------------------------------------------------------

    def booking_attempting(self, slot) -> None:
        msg = (
            f"{slot.slot_date_str} ({slot.day_of_week}) @ {slot.slot_time} "
            f"— party of {self.config.party_size}"
        )
        logger.info(f"[book] Attempting: {msg}")

    def booking_confirmed(self, slot) -> None:
        msg = (
            f"{slot.slot_date_str} ({slot.day_of_week}) @ {slot.slot_time}\n"
            f"Party of {self.config.party_size}"
        )
        logger.info(f"\n{'=' * 60}\n[book] *** BOOKING CONFIRMED ***\n{msg}\n{'=' * 60}")
        self._fire(
            title="✅ Reservation Confirmed!",
            description=msg,
            color=_GREEN,
            fields=[
                ("Date", f"{slot.slot_date_str} ({slot.day_of_week})", True),
                ("Time", slot.slot_time, True),
                ("Party", str(self.config.party_size), True),
                ("Restaurant", self.config.restaurant_slug, False),
            ],
        )

    def booking_aborted(self, slot, reason: str) -> None:
        logger.info(f"[book] Aborted {slot}: {reason}")

    def booking_failed(self, slot, reason: str) -> None:
        msg = f"{slot} — {reason}"
        logger.warning(f"[book] FAILED: {msg}")
        self._fire(
            title="❌ Booking Failed",
            description=msg,
            color=_RED,
        )

    def no_payment_method(self, slot) -> None:
        msg = (
            f"No payment card found while trying to book:\n"
            f"**{slot.slot_date_str} ({slot.day_of_week}) @ {slot.slot_time}**\n\n"
            f"Add a card at https://www.exploretock.com/account/payment\n"
            f"The bot will retry automatically for up to 9 minutes."
        )
        logger.warning(f"\n{'!' * 60}\n[PAYMENT REQUIRED]\n{msg}\n{'!' * 60}")
        self._fire(
            title="💳 Add Payment Card to Tock!",
            description=msg,
            color=_RED,
        )

    def dry_run_would_book(self, slot) -> None:
        msg = (
            f"[DRY RUN] Would book: {slot.slot_date_str} ({slot.day_of_week}) "
            f"@ {slot.slot_time}  party={self.config.party_size}"
        )
        logger.info(msg)
        self._fire(
            title="🔵 Dry Run — Would Have Booked",
            description=msg,
            color=_BLUE,
        )

    def error(self, context: str, detail: str) -> None:
        msg = f"**{context}**\n{detail}"
        logger.error(f"[error] {context}: {detail}")
        self._fire(title="⚠️ Bot Error", description=msg, color=_RED)

    # ------------------------------------------------------------------
    # Discord delivery (fire-and-forget)
    # ------------------------------------------------------------------

    def _fire(
        self,
        title: str,
        description: str,
        color: int,
        fields: list[tuple[str, str, bool]] | None = None,
    ) -> None:
        """Schedule a Discord webhook call without blocking the caller."""
        if not self._discord_enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._send_discord(title, description, color, fields or [])
            )
        except RuntimeError:
            # No running loop (e.g. called from a sync test context) — skip
            pass

    async def _send_discord(
        self,
        title: str,
        description: str,
        color: int,
        fields: list[tuple[str, str, bool]],
    ) -> None:
        try:
            import aiohttp
        except ImportError:
            logger.warning(
                "aiohttp not installed — Discord notifications disabled.\n"
                "  Fix: pip install aiohttp"
            )
            return

        now_pt = datetime.now(PT).strftime("%Y-%m-%d %H:%M PT")
        embed: dict = {
            "title": title,
            "description": description,
            "color": color,
            "footer": {"text": f"Tock Bot • {now_pt}"},
        }
        if fields:
            embed["fields"] = [
                {"name": n, "value": v, "inline": inline}
                for n, v, inline in fields
            ]

        payload = {"embeds": [embed]}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 204):
                        text = await resp.text()
                        logger.warning(
                            f"[discord] Webhook returned {resp.status}: {text[:200]}"
                        )
        except Exception as e:
            logger.warning(f"[discord] Failed to send notification: {e}")


def _fmt_interval(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"
