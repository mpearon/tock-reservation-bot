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
        self._critical_tasks: list[asyncio.Task] = []

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
        if slots_found == 0:
            # Alert via Discord when the sniper window closed with nothing found —
            # useful for knowing the release didn't happen / window needs adjustment.
            self._fire(
                title="😶 Sniper Window Ended — 0 Slots Found",
                description=(
                    f"The sniper window closed without finding any available slots.\n\n"
                    f"**What this might mean:**\n"
                    f"• Restaurant hasn't released yet — check for a new release date\n"
                    f"• Release happened but slots sold out instantly\n"
                    f"• Sniper window timing may need adjustment"
                ),
                color=_GREY,
            )

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def no_slots_found(self) -> None:
        logger.info("No available slots found this cycle.")

    def slots_found(self, slots: list, sniper_mode: bool = False) -> None:
        lines = [f"• {s.slot_date_str} ({s.day_of_week}) @ {s.slot_time}" for s in slots[:8]]
        extra = f"\n+{len(slots) - 8} more…" if len(slots) > 8 else ""
        summary = "\n".join(lines) + extra
        logger.info(f"[slots] {len(slots)} slot(s) found:\n{summary}")
        if sniper_mode:
            # In sniper mode the bot is already attempting to book — suppress
            # the Discord embed so the "slots found" and "booking confirmed"
            # don't arrive out of order or before booking completes.
            return
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
            critical=True,
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
            critical=True,
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
        critical: bool = False,
    ) -> None:
        """Schedule a Discord webhook call without blocking the caller.

        critical=True tasks are tracked so drain_pending() can await them
        before shutdown — prevents booking confirmations from being dropped.
        """
        if not self._discord_enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._send_discord(title, description, color, fields or [])
            )
            if critical:
                self._critical_tasks.append(task)
                task.add_done_callback(
                    lambda t: self._critical_tasks.remove(t)
                    if t in self._critical_tasks else None
                )
        except RuntimeError:
            # No running loop (e.g. called from a sync test context) — skip
            pass

    async def drain_pending(self, timeout: float = 10.0) -> None:
        """Wait for critical notifications to send. Call before shutdown."""
        tasks = list(self._critical_tasks)  # snapshot before await
        if not tasks:
            return
        logger.info(f"[notify] Draining {len(tasks)} critical notification(s)…")
        await asyncio.wait(tasks, timeout=timeout)

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


    # ------------------------------------------------------------------
    # Test helper
    # ------------------------------------------------------------------

    async def test_all_notifications(self) -> None:
        """
        Send one Discord message for each notification type and await each send.
        Used by --test-notify.  Console output is always printed regardless of
        whether a webhook URL is configured.
        """
        from types import SimpleNamespace

        fake_slot = SimpleNamespace(
            slot_date_str="2026-03-14",
            day_of_week="Saturday",
            slot_time="5:00 PM",
        )

        tests: list[tuple[str, str, int, list]] = [
            (
                "✅ Reservation Confirmed! [TEST]",
                f"{fake_slot.slot_date_str} ({fake_slot.day_of_week}) @ {fake_slot.slot_time}\n"
                f"Party of {self.config.party_size}",
                _GREEN,
                [
                    ("Date", f"{fake_slot.slot_date_str} ({fake_slot.day_of_week})", True),
                    ("Time", fake_slot.slot_time, True),
                    ("Party", str(self.config.party_size), True),
                    ("Restaurant", self.config.restaurant_slug, False),
                ],
            ),
            (
                "🟡 2 Slot(s) Available! [TEST]",
                f"• {fake_slot.slot_date_str} ({fake_slot.day_of_week}) @ {fake_slot.slot_time}\n"
                "• 2026-03-15 (Sunday) @ 5:30 PM",
                _YELLOW,
                [],
            ),
            (
                "🎯 Sniper Mode Active [TEST]",
                "Sniper mode ACTIVE on Friday — triggered at 16:59 PT, runs until 17:10 PT",
                _ORANGE,
                [
                    ("Day", "Friday", True),
                    ("Trigger", "16:59 PT", True),
                    ("Until", "17:10 PT", True),
                ],
            ),
            (
                "⚠️ Bot Error [TEST]",
                "**Checkout page not found**\nSelector checkout_container failed",
                _RED,
                [],
            ),
        ]

        if not self._discord_enabled:
            logger.warning(
                "[test-notify] DISCORD_WEBHOOK_URL is not set — "
                "printing console output only."
            )

        for title, description, color, fields in tests:
            logger.info(f"[test-notify] Sending: {title}")
            if self._discord_enabled:
                await self._send_discord(title, description, color, fields)
            else:
                logger.info(f"  (Discord skipped — no webhook URL)")

        logger.info(
            f"[test-notify] Done. {len(tests)} message type(s) tested."
            + (f"\n  Check your Discord channel to confirm delivery." if self._discord_enabled else "")
        )


def _fmt_interval(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"
