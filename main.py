#!/usr/bin/env python3
"""
Tock Reservation Bot — entry point.

Usage:
  python main.py                         Start the monitoring loop (runs indefinitely)
  python main.py --once                  Run one availability check then exit
  python main.py --verify                Verify DOM selectors against the live site
  python main.py --dry-run               Override DRY_RUN=true for this session only
  python main.py --test-notify           Send a test Discord message for each alert type
  python main.py --test-booking-flow     Navigate to checkout on a test restaurant and screenshot
  python main.py --test-booking-flow --test-restaurant SLUG  (specify which restaurant)
"""

import asyncio
import logging
import sys
from argparse import ArgumentParser
from pathlib import Path


async def _test_booking_flow(browser, config, test_slug: str, logger) -> None:
    """
    Navigate through the full booking flow on *test_slug* up to (but NOT including)
    clicking the confirm button.

    Steps:
      1. Scan the next 4 weeks for any available day on the test restaurant.
      2. Click that day, then click the first available time slot.
      3. Wait for the checkout page to load.
      4. Detect whether a saved card / confirm button is present.
      5. Take a screenshot → test_booking_flow.png.
      6. Exit WITHOUT clicking confirm.
    """
    from datetime import date, timedelta
    import src.selectors as sel

    BASE_URL = "https://www.exploretock.com"
    SCREENSHOT_PATH = Path("test_booking_flow.png")
    PARTY = config.party_size

    logger.info(
        f"[test-flow] Starting booking-flow test on restaurant: {test_slug!r}"
        f"  (party={PARTY}, will NOT confirm)"
    )

    page = await browser.new_page()
    try:
        # ── Step 1: Find a date with available time slots ──────────────
        # Navigate directly to each date's search URL (same approach as the
        # main bot's checker). This bypasses the calendar click/search issue
        # where selecting a day doesn't auto-submit the search on some restaurants.
        found_date = None
        today = date.today()
        slot_selector = sel.get("available_slot_button")
        time_selector = sel.get("slot_time_text")
        day_selector  = sel.get("available_day_button")
        cal_selector  = sel.get("calendar_container")

        for delta in range(1, 29):
            check_date = today + timedelta(days=delta)
            url = (
                f"{BASE_URL}/{test_slug}/search"
                f"?date={check_date.isoformat()}&size={PARTY}&time=17:00"
            )
            logger.info(f"[test-flow] {check_date.isoformat()} → {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(6000)  # let the page + slots render

            # Wait for calendar, then click the specific day to open the slot panel.
            # On Tock, "Book now" buttons only appear AFTER you click a calendar day.
            try:
                await page.wait_for_selector(cal_selector, timeout=10000)
            except Exception:
                logger.warning(f"[test-flow] Calendar did not appear for {check_date.isoformat()}")
                continue

            day_num = str(check_date.day)
            day_buttons = await page.query_selector_all(day_selector)
            clicked_day = False
            for day_btn in day_buttons:
                if (await day_btn.text_content() or "").strip() == day_num:
                    logger.info(f"[test-flow] Clicking calendar day {day_num}…")
                    await day_btn.click()
                    await page.wait_for_timeout(2500)
                    clicked_day = True
                    break
            if not clicked_day:
                logger.info(f"[test-flow] Day {day_num} not available in calendar — skipping")
                continue

            # ── Step 2: Dump buttons + find a time slot button ──────────
            btn_info = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('button, a[role="button"]'))
                    .map(b => ({
                        text: b.textContent.trim().slice(0, 60),
                        cls:  b.className.slice(0, 80),
                        vis:  b.offsetParent !== null
                    }))
                    .filter(b => b.text.length > 0 &&
                                 !b.cls.includes('ConsumerCalendar') &&
                                 !['Search','More','Reservations','Your Privacy Choices'].includes(b.text))
                    .slice(0, 20);
            }""")
            if btn_info:
                logger.info(f"[test-flow] {check_date.isoformat()} — non-calendar buttons:")
                for b in btn_info:
                    vis = "V" if b["vis"] else " "
                    logger.info(f"  [{vis}] text={b['text']!r:45s}  cls={b['cls']!r}")

            found_selector = None
            for try_selector in [
                slot_selector,                        # button.Consumer-resultsListItem.is-available
                "button.Consumer-resultsListItem",
                'button:visible:has-text("Book")',    # "Book" CTA (e.g. Benu css-dr2rn7)
                "button.SearchExperience-bookButton",
                "[data-testid='book-button']",
                # NOTE: Consumer-reservationLink is the EXPERIENCE ROW (opens date picker),
                # not the Book button — keep it last as a fallback only.
                ".Consumer-reservationLink",
            ]:
                count = await page.locator(try_selector).count()
                if count:
                    found_selector = try_selector
                    logger.info(f"[test-flow] {check_date.isoformat()}: {count} slot(s) — {try_selector!r}")
                    break

            if not found_selector:
                logger.info(f"[test-flow] {check_date.isoformat()}: no slots found")
                continue

            first_slot = page.locator(found_selector).first
            slot_text = (await first_slot.text_content() or "?").strip()[:40]
            logger.info(f"[test-flow] Force-clicking slot: {slot_text!r}")
            await first_slot.click(force=True)
            await page.wait_for_timeout(3000)
            found_date = check_date
            break

        if not found_date:
            logger.error(
                f"[test-flow] No available slots found on {test_slug!r} "
                f"in the next 28 days.\n"
                f"  Try a different restaurant with --test-restaurant SLUG"
            )
            await page.screenshot(path=str(SCREENSHOT_PATH))
            logger.info(f"[test-flow] Screenshot of final state saved → {SCREENSHOT_PATH}")
            return

        # ── Step 3: Wait for checkout page ─────────────────────────────
        checkout_selector = sel.get("checkout_container")
        checkout_loaded = False
        try:
            await page.wait_for_selector(checkout_selector, timeout=20000)
            checkout_loaded = True
        except Exception:
            # URL-based fallback
            if any(p in page.url for p in ("/checkout", "/reservation", "/book")):
                checkout_loaded = True

        if not checkout_loaded:
            logger.error(
                f"[test-flow] Checkout page did not load (URL: {page.url}).\n"
                f"  selector={checkout_selector!r}\n"
                f"  → Update checkout_container in src/selectors.py"
            )
        else:
            logger.info(f"[test-flow] Checkout page loaded. URL: {page.url}")

        # Scroll to the bottom of the checkout page to reveal CVC + confirm button
        await page.wait_for_timeout(1500)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)

        # Fill CVC if configured — search main frame AND all iframes (Stripe embeds in iframe)
        cvc_sel_str = sel.get("cvc_input")
        all_frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
        cvc_el = None
        for _frame in all_frames:
            try:
                cvc_el = await _frame.query_selector(cvc_sel_str)
                if cvc_el:
                    break
            except Exception:
                continue

        if cvc_el and config.card_cvc:
            await cvc_el.fill(config.card_cvc)
            logger.info("[test-flow] CVC field found and filled.")
        elif cvc_el and not config.card_cvc:
            logger.warning("[test-flow] CVC field visible but TOCK_CARD_CVC not set — leaving blank.")

        # ── Step 4: Detect card / CVC field / confirm button ───────────
        # Search across main frame + iframes for each indicator
        async def _find_in_any_frame(selector: str) -> bool:
            for _f in all_frames:
                try:
                    el = await _f.query_selector(selector)
                    if el:
                        return True
                except Exception:
                    continue
            return False

        saved_card_sel = sel.get("saved_payment_card")
        no_payment_sel = sel.get("no_payment_indicator")
        confirm_sel    = sel.get("confirm_button")
        cvc_sel        = sel.get("cvc_input")

        has_card    = await _find_in_any_frame(saved_card_sel)
        needs_add   = await _find_in_any_frame(no_payment_sel)
        has_confirm = await _find_in_any_frame(confirm_sel)
        has_cvc     = cvc_el is not None
        cvc_configured = bool(config.card_cvc)

        logger.info(
            f"\n"
            f"{'='*60}\n"
            f"[test-flow] CHECKOUT STATE\n"
            f"  URL              : {page.url}\n"
            f"  Checkout loaded  : {checkout_loaded}\n"
            f"  Saved card       : {'YES ✓' if has_card else 'NO — add one at /account/payment'}\n"
            f"  Add-card prompt  : {'YES (no card on file)' if needs_add else 'no'}\n"
            f"  CVC field visible: {'YES ✓' if has_cvc else 'not found (may appear after card select)'}\n"
            f"  CVC configured   : {'YES ✓ (TOCK_CARD_CVC set)' if cvc_configured else 'NO — set TOCK_CARD_CVC in .env'}\n"
            f"  Confirm button   : {'FOUND ✓' if has_confirm else 'NOT FOUND — selector may need updating'}\n"
            f"  >>> confirm was NOT clicked (test mode) <<<\n"
            f"{'='*60}"
        )

        if not has_card:
            logger.warning(
                "[test-flow] No saved payment card detected.\n"
                "  Add a card at https://www.exploretock.com/account/payment\n"
                "  Then re-run --test-booking-flow to verify."
            )
        if not cvc_configured:
            logger.warning(
                "[test-flow] TOCK_CARD_CVC is not set.\n"
                "  Add  TOCK_CARD_CVC=123  to .env so the bot can fill CVC automatically."
            )

        # ── Step 5: Screenshot ──────────────────────────────────────────
        await page.screenshot(path=str(SCREENSHOT_PATH), full_page=False)
        logger.info(f"[test-flow] Screenshot saved → {SCREENSHOT_PATH}")
        logger.info("[test-flow] DONE — confirm button was NOT clicked.")

    except Exception as e:
        logger.error(f"[test-flow] Unexpected error: {e}")
        try:
            await page.screenshot(path=str(SCREENSHOT_PATH))
            logger.info(f"[test-flow] Error screenshot saved → {SCREENSHOT_PATH}")
        except Exception:
            pass
    finally:
        await page.close()


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
        help="Tock restaurant slug used for --test-booking-flow (default: benu)",
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
            await _test_booking_flow(browser, config, args.test_restaurant, logger)
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
