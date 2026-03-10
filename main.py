#!/usr/bin/env python3
"""
Tock Reservation Bot — entry point.

Usage:
  python main.py               Start the monitoring loop (runs indefinitely)
  python main.py --once        Run one availability check then exit
  python main.py --verify      Verify DOM selectors against the live site
  python main.py --dry-run     Override DRY_RUN=true for this session only
"""

import asyncio
import logging
import sys
from argparse import ArgumentParser
from pathlib import Path


def _setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt, handlers=handlers)
    # Suppress noisy third-party loggers
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def main() -> None:
    parser = ArgumentParser(description="Tock reservation bot for Fuhuihua SF")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one availability check and exit (useful for cron / testing)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify DOM selectors against the live Tock site and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Override DRY_RUN=true (go through full flow but skip the confirm click)",
    )
    args = parser.parse_args()

    _setup_logging()
    logger = logging.getLogger("main")

    # --- Config ---
    from src.config import load_config
    try:
        config = load_config()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    if args.dry_run:
        config.dry_run = True

    # --- Banner ---
    mode_flags = []
    if config.dry_run:
        mode_flags.append("DRY-RUN")
    mode_flags.append("HEADLESS" if config.headless else "HEADED")

    logger.info("=" * 60)
    logger.info(f"  Tock Reservation Bot  [{', '.join(mode_flags)}]")
    logger.info(f"  Restaurant : {config.restaurant_slug}")
    logger.info(f"  Party size : {config.party_size}")
    logger.info(f"  Prefer days: {', '.join(config.preferred_days)}")
    logger.info(f"  Prefer time: {config.preferred_time}")
    logger.info(f"  Scan range : {config.scan_weeks} weeks")
    logger.info(f"  Release win: {config.release_window_days} "
                f"{config.release_window_start}–{config.release_window_end} PT")
    logger.info("=" * 60)

    # --- Imports (deferred so logging is set up first) ---
    from src.browser import TockBrowser
    from src.checker import AvailabilityChecker
    from src.monitor import TockMonitor
    from src.notifier import Notifier
    from src.tracker import SlotTracker
    import src.selectors as selectors_mod

    browser = TockBrowser(config)
    notifier = Notifier(config)
    tracker = SlotTracker()
    checker = AvailabilityChecker(config, browser, tracker)

    try:
        await browser.start()

        # ── Mode: --verify ────────────────────────────────────────────
        if args.verify:
            logger.info("Running selector verification (no booking will occur)…")
            # Login not strictly required for verify but helps test authenticated selectors
            await browser.login()
            await selectors_mod.verify_selectors(browser, config)
            return

        # ── Login ─────────────────────────────────────────────────────
        if not await browser.login():
            logger.error(
                "Login failed. Possible fixes:\n"
                "  • Check TOCK_EMAIL and TOCK_PASSWORD in .env\n"
                "  • Run with HEADLESS=false to see the browser and solve any CAPTCHA\n"
                "  • Delete session_cookies.json and retry"
            )
            sys.exit(1)

        monitor = TockMonitor(config, browser, checker, notifier, tracker)

        # ── Mode: --once ──────────────────────────────────────────────
        if args.once:
            logger.info("Running single poll (--once mode)…")
            await monitor.poll()
            return

        # ── Mode: continuous loop ─────────────────────────────────────
        await monitor.run()

    except KeyboardInterrupt:
        logger.info("\nBot stopped by user (Ctrl+C).")
    finally:
        tracker.save()
        await browser.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
