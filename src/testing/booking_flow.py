"""Test booking flow — navigates to checkout on a test restaurant, screenshots, never confirms."""

import logging
from pathlib import Path

from src.browser import TockBrowser
from src.config import Config


async def test_booking_flow(browser: TockBrowser, config: Config, test_slug: str) -> None:
    """
    Navigate through the full booking flow on *test_slug* up to (but NOT including)
    clicking the confirm button.

    Steps:
      1. Scan the next 4 weeks for any available day on the test restaurant.
      2. Click that day, then click the first available time slot.
      3. Wait for the checkout page to load.
      4. Detect whether a saved card / confirm button is present.
      5. Take a screenshot -> test_booking_flow.png.
      6. Exit WITHOUT clicking confirm.
    """
    from datetime import date, timedelta
    import src.selectors as sel

    logger = logging.getLogger("main")

    BASE_URL = "https://www.exploretock.com"
    SCREENSHOT_PATH = Path("test_booking_flow.png")
    PARTY = config.party_size

    logger.info(
        f"[test-flow] Starting booking-flow test on restaurant: {test_slug!r}"
        f"  (party={PARTY}, will NOT confirm)"
    )

    page = await browser.new_page()
    try:
        # -- Step 1: Find a date with available time slots --
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

            # -- Step 2: Dump buttons + find a time slot button --
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

        # -- Step 3: Wait for checkout page --
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
        cvc_el = await browser.find_in_frames(page, sel.get("cvc_input"))
        if cvc_el and config.card_cvc:
            await cvc_el.fill(config.card_cvc)
            logger.info("[test-flow] CVC field found and filled.")
        elif cvc_el and not config.card_cvc:
            logger.warning("[test-flow] CVC field visible but TOCK_CARD_CVC not set — leaving blank.")

        # -- Step 4: Detect card / CVC field / confirm button --
        saved_card_sel = sel.get("saved_payment_card")
        no_payment_sel = sel.get("no_payment_indicator")
        confirm_sel    = sel.get("confirm_button")

        has_card    = await browser.find_in_frames(page, saved_card_sel) is not None
        needs_add   = await browser.find_in_frames(page, no_payment_sel) is not None
        has_confirm = await browser.find_in_frames(page, confirm_sel) is not None
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

        # -- Step 5: Screenshot --
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
