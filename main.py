#!/usr/bin/env python3
"""
Tock Reservation Bot — entry point.

Usage:
  python main.py                              Start the monitoring loop (runs indefinitely)
  python main.py --once                       Run one availability check then exit
  python main.py --verify                     Verify DOM selectors against the live site
  python main.py --dry-run                    Override DRY_RUN=true for this session only
  python main.py --test-notify                Send a test Discord message for each alert type
  python main.py --test-booking-flow          Navigate to checkout on a test restaurant and screenshot
  python main.py --test-booking-flow --test-restaurant SLUG
  python main.py --test-sniper                Run 3 robustness tests: pre-warm, page-reuse, confirm-retry
  python main.py --test-sniper --test-restaurant SLUG
  python main.py --test-sniper-benchmark      A/B benchmark: sequential vs concurrent poll speed/error rate
  python main.py --test-sniper-benchmark --test-restaurant SLUG --test-sniper-polls N
  python main.py --test-adaptive-sniper       Test concurrent↔sequential auto-switching (threshold forced to 0%)
"""

import asyncio
import logging
import sys
from argparse import ArgumentParser

from src.testing.booking_flow import test_booking_flow
from src.testing.sniper_tests import (
    test_sniper_benchmark,
    test_sniper_integration,
    test_sniper_phases,
    test_sniper_robustness,
)


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
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="Send a test Discord message for each alert type (confirmed, slots, sniper, error) then exit",
    )
    parser.add_argument(
        "--test-booking-flow",
        action="store_true",
        help=(
            "Navigate to a test restaurant's checkout page all the way to the confirm button, "
            "take a screenshot, then exit WITHOUT confirming"
        ),
    )
    parser.add_argument(
        "--test-restaurant",
        default="benu",
        metavar="SLUG",
        help="Tock restaurant slug used for --test-booking-flow / --test-sniper (default: benu)",
    )
    parser.add_argument(
        "--test-sniper",
        action="store_true",
        help=(
            "Run 3 robustness tests for sniper mode: "
            "(1) session pre-warm with cf_clearance expiry logging, "
            "(2) page reuse reload vs fresh navigate timing, "
            "(3) confirm retry with broken selector — never books."
        ),
    )
    parser.add_argument(
        "--test-sniper-benchmark",
        action="store_true",
        help=(
            "A/B benchmark: run --test-sniper-polls polls sequentially then concurrently "
            "on a test restaurant and compare cycle time and Cloudflare error rate."
        ),
    )
    parser.add_argument(
        "--test-sniper-polls",
        type=int,
        default=10,
        metavar="N",
        help="Number of consecutive sniper polls to run in --test-sniper-benchmark mode (default: 10)",
    )
    parser.add_argument(
        "--test-sniper-integration",
        action="store_true",
        help=(
            "Full end-to-end integration test: sets sniper to fire in 2 min, "
            "triggers pre-warm immediately, waits for window to open, runs "
            "--test-sniper-polls rapid DRY_RUN polls, sends Discord notifications. "
            "Targets the real restaurant (no --test-restaurant override needed)."
        ),
    )
    parser.add_argument(
        "--test-adaptive-sniper",
        action="store_true",
        help=(
            "Test adaptive concurrent↔sequential switching through the real monitor "
            "poll() path. Forces sniper active, threshold=0%% (any error triggers switch), "
            "DRY_RUN forced. Use --test-sniper-polls N to control poll count."
        ),
    )
    parser.add_argument(
        "--test-sniper-phases",
        action="store_true",
        help=(
            "Test two-phase sniper: sets window 30s from now, runs pre-release "
            "Phase 1 no-ops then Phase 2 aggressive scans. DRY_RUN forced. "
            "Use --test-sniper-polls N to control poll count (default: 20)."
        ),
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
    if config.fallback_days:
        logger.info(f"  Fallback   : {', '.join(config.fallback_days)} (if no preferred slots)")
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

        # ── Mode: --test-notify ───────────────────────────────────────
        if args.test_notify:
            logger.info("Sending test notifications for each alert type…")
            await notifier.test_all_notifications()
            return

        # ── Mode: --test-booking-flow ─────────────────────────────────
        if args.test_booking_flow:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-booking-flow.")
                sys.exit(1)
            await test_booking_flow(browser, config, args.test_restaurant)
            return

        # ── Mode: --test-sniper (3 robustness tests) ─────────────────
        if args.test_sniper:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-sniper.")
                sys.exit(1)
            await test_sniper_robustness(browser, config, args.test_restaurant)
            return

        # ── Mode: --test-sniper-benchmark (A/B concurrent vs sequential) ─
        if args.test_sniper_benchmark:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-sniper-benchmark.")
                sys.exit(1)
            await test_sniper_benchmark(
                browser, config, args.test_restaurant,
                args.test_sniper_polls,
            )
            return

        # ── Mode: --test-sniper-integration ──────────────────────────
        if args.test_sniper_integration:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-sniper-integration.")
                sys.exit(1)
            await test_sniper_integration(
                browser, config, notifier, checker, tracker,
                num_polls=args.test_sniper_polls,
            )
            return

        # ── Mode: --test-adaptive-sniper ──────────────────────────────
        if args.test_adaptive_sniper:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-adaptive-sniper.")
                sys.exit(1)
            # Point config at test restaurant, keep all other settings identical
            config.restaurant_slug = args.test_restaurant
            monitor = TockMonitor(config, browser, checker, notifier, tracker)
            await monitor.run_adaptive_test(args.test_sniper_polls)
            return

        # ── Mode: --test-sniper-phases ────────────────────────────────────
        if args.test_sniper_phases:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-sniper-phases.")
                sys.exit(1)
            await test_sniper_phases(
                browser, config, notifier, checker, tracker,
                num_polls=args.test_sniper_polls,
            )
            return

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

        # ── Mode: continuous loop (with auto-restart) ─────────────────
        max_backoff = 300  # cap at 5 minutes
        backoff = 10       # start at 10 seconds
        while True:
            try:
                await monitor.run()
                break  # clean exit (shouldn't happen, run() loops forever)
            except KeyboardInterrupt:
                raise  # let outer handler deal with it
            except Exception as e:
                logger.error(f"Bot crashed: {e}")
                notifier.error(
                    "Bot crashed — auto-restarting",
                    f"{type(e).__name__}: {e}\nRestarting in {backoff}s…",
                )
                logger.info(f"Auto-restarting in {backoff}s…")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

                # Tear down and reinitialize browser
                try:
                    await browser.close()
                except Exception:
                    pass
                browser = TockBrowser(config)
                await browser.start()
                checker = AvailabilityChecker(config, browser, tracker)

                if not await browser.login():
                    logger.error("Login failed on restart — will retry after backoff.")
                    continue

                monitor = TockMonitor(config, browser, checker, notifier, tracker)
                logger.info("Bot restarted successfully.")
                backoff = 10  # reset backoff on successful restart

    except KeyboardInterrupt:
        logger.info("\nBot stopped by user (Ctrl+C).")
    finally:
        tracker.save()
        await browser.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
