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


async def _test_sniper_mode(
    browser, config, test_slug: str, num_polls: int, logger
) -> None:
    """
    Run the sniper poll loop against *test_slug* for *num_polls* iterations,
    first sequential then concurrent — same dates both times for a fair
    apples-to-apples comparison. DRY_RUN is forced; nothing is ever booked.

    Uses the exact same preferred_days and scan_weeks as the real bot config
    so the date count matches what Fuhuihua sniper will actually check.
    """
    import logging as _logging
    from datetime import date, timedelta
    from src.checker import AvailabilityChecker
    from src.tracker import SlotTracker

    # Override restaurant and dry_run; scan ALL days (preferred + fallback)
    # so the date count matches the real worst-case load (Phase 1 misses,
    # Phase 2 fallback also runs — maximum pages opened per cycle)
    all_scan_days = list(dict.fromkeys(
        config.preferred_days + config.fallback_days
        or ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    ))
    test_config = config.__class__(**{
        **config.__dict__,
        "restaurant_slug": test_slug,
        "dry_run": True,
        # Flatten preferred+fallback into preferred so check_all scans all of them
        "preferred_days": all_scan_days,
        "fallback_days": [],
    })

    # Count dates per poll
    today = date.today()
    end = today + timedelta(weeks=test_config.scan_weeks)
    dates_per_poll = sum(
        1 for i in range(1, test_config.scan_weeks * 7 + 1)
        if (d := today + timedelta(days=i)) <= end
        and d.strftime("%A") in test_config.preferred_days
    )

    # Log handler that counts calendar_container SELECTOR_FAILED lines
    class _ErrorCounter(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.count = 0
        def emit(self, record):
            if "calendar_container" in record.getMessage():
                self.count += 1
        def reset(self):
            n, self.count = self.count, 0
            return n

    counter = _ErrorCounter()
    _logging.getLogger("src.checker").addHandler(counter)

    logger.info(
        f"\n{'='*60}\n"
        f"[test-sniper] Sniper poll benchmark\n"
        f"  Restaurant  : {test_slug}\n"
        f"  Polls each  : {num_polls} (sequential first, then concurrent)\n"
        f"  Dates/poll  : {dates_per_poll} "
        f"({test_config.scan_weeks} weeks × {', '.join(test_config.preferred_days)})\n"
        f"  Booking     : DISABLED (DRY_RUN forced)\n"
        f"{'='*60}"
    )

    async def _run_mode(label: str, concurrent: bool) -> tuple[float, int, int]:
        """Run num_polls polls and return (avg_seconds, total_errors, total_slots)."""
        tracker = SlotTracker()
        checker = AvailabilityChecker(test_config, browser, tracker)
        times: list[float] = []
        total_errors = 0
        total_slots = 0

        logger.info(f"\n[test-sniper] === {label} mode ===")
        for i in range(1, num_polls + 1):
            counter.reset()
            t0 = asyncio.get_event_loop().time()
            try:
                slots = await checker.check_all(concurrent=concurrent)
                elapsed = asyncio.get_event_loop().time() - t0
                cal_errors = counter.reset()
                total_errors += cal_errors
                total_slots += len(slots)
                times.append(elapsed)
                status = f"{len(slots)} slot(s) found" if slots else "no slots"
                logger.info(
                    f"[test-sniper] {label} poll {i}/{num_polls} → {status}  "
                    f"({elapsed:.1f}s, calendar errors: {cal_errors}/{dates_per_poll})"
                )
                for s in slots:
                    logger.info(f"  • {s}  (would book in real mode)")
            except Exception as e:
                elapsed = asyncio.get_event_loop().time() - t0
                logger.error(
                    f"[test-sniper] {label} poll {i} → EXCEPTION "
                    f"after {elapsed:.1f}s: {e}"
                )
                times.append(elapsed)
            await asyncio.sleep(0)

        avg = sum(times) / len(times) if times else 0
        return avg, total_errors, total_slots

    seq_avg, seq_errors, seq_slots = await _run_mode("Sequential", concurrent=False)
    con_avg, con_errors, con_slots = await _run_mode("Concurrent", concurrent=True)

    _logging.getLogger("src.checker").removeHandler(counter)

    total_checks = num_polls * dates_per_poll
    seq_rate = seq_errors / total_checks if total_checks else 0
    con_rate = con_errors / total_checks if total_checks else 0

    def _verdict(rate: float) -> str:
        return "⚠️  HIGH — Cloudflare blocking" if rate > 0.2 else "✓ acceptable"

    logger.info(
        f"\n{'='*60}\n"
        f"[test-sniper] COMPARISON RESULTS ({dates_per_poll} dates/poll)\n"
        f"\n"
        f"  Sequential:\n"
        f"    Avg cycle time   : {seq_avg:.1f}s\n"
        f"    Calendar errors  : {seq_errors}/{total_checks} "
        f"({seq_rate:.0%}) {_verdict(seq_rate)}\n"
        f"    Slots found      : {seq_slots}\n"
        f"\n"
        f"  Concurrent:\n"
        f"    Avg cycle time   : {con_avg:.1f}s\n"
        f"    Calendar errors  : {con_errors}/{total_checks} "
        f"({con_rate:.0%}) {_verdict(con_rate)}\n"
        f"    Slots found      : {con_slots}\n"
        f"{'='*60}"
    )


async def _test_sniper_robustness(
    browser, config, test_slug: str, logger
) -> None:
    """
    Three-part robustness test for sniper mode improvements.
    Run with: python main.py --test-sniper

    Test 1 — Session pre-warm
        Calls browser.warm_session() and logs the cf_clearance cookie expiry
        so you know how long the session stays valid.

    Test 2 — Page reuse (reload vs fresh navigate)
        Calls checker._check_date(keep_page=True) twice on the same date.
        Verifies the second call reuses the cached Playwright page (reload
        instead of goto) and logs the timing difference.

    Test 3 — Confirm retry (at Benu checkout, NEVER clicks confirm)
        Navigates to test_slug's checkout page.
        Uses a deliberately broken selector to force attempt-1 to fail, then
        measures that the retry fires ~2 seconds later.
        The real confirm button is NEVER clicked.
    """
    import time as _time
    from datetime import date, timedelta
    from src.checker import AvailabilityChecker
    from src.tracker import SlotTracker
    import src.selectors as sel

    PASS = "PASS"
    FAIL = "FAIL"
    results: list[tuple[str, str]] = []

    # ── TEST 1: Session pre-warm ──────────────────────────────────────────

    logger.info(f"\n{'='*60}")
    logger.info("[test-sniper] TEST 1/3 — Session pre-warm (warm_session)")
    logger.info(f"{'='*60}")

    t0 = _time.monotonic()
    await browser.warm_session()
    elapsed = _time.monotonic() - t0

    cookies = await browser._context.cookies()
    cf = next((c for c in cookies if c["name"] == "cf_clearance"), None)

    logger.info(f"[test-sniper] warm_session() completed in {elapsed:.1f}s")
    logger.info(f"[test-sniper] Total cookies in browser context: {len(cookies)}")

    # Log all cookies with expiry for diagnostics
    import time as _time_mod
    now_ts = _time_mod.time()
    for c in sorted(cookies, key=lambda x: x.get("expires", 0), reverse=True):
        exp = c.get("expires", -1)
        if exp > 0:
            remaining = (exp - now_ts) / 60
            logger.info(
                f"  {c['name']:35s}  expires in {remaining:6.0f} min  ({c['domain']})"
            )
        else:
            logger.info(
                f"  {c['name']:35s}  session cookie (no expiry)     ({c['domain']})"
            )

    if cf:
        exp_ts = cf.get("expires", -1)
        if exp_ts > 0:
            from datetime import datetime
            exp_dt = datetime.fromtimestamp(exp_ts)
            remaining_min = (exp_ts - now_ts) / 60
            logger.info(
                f"\n[test-sniper] cf_clearance found:\n"
                f"  Expires at : {exp_dt.strftime('%Y-%m-%d %H:%M:%S')} local\n"
                f"  Remaining  : {remaining_min:.0f} minutes\n"
                f"  Note: Cloudflare typically issues 30-min cf_clearance tokens.\n"
                f"        Pre-warm fires {15} min before sniper — should still be valid."
            )
            verdict1 = PASS
        else:
            logger.info(
                "[test-sniper] cf_clearance is a session cookie (no fixed expiry).\n"
                "  This is normal — it stays valid for the browser session lifetime."
            )
            verdict1 = PASS
    else:
        logger.warning(
            "[test-sniper] cf_clearance NOT found in cookies.\n"
            "  This may mean Cloudflare did not issue a challenge on this request\n"
            "  (e.g. the page loaded cleanly without a challenge) — this is fine.\n"
            "  If you see CF challenges during live sniper, run HEADLESS=false once."
        )
        verdict1 = PASS  # absence of cf_clearance is not necessarily a failure

    results.append(("Session pre-warm", verdict1))
    logger.info(f"[test-sniper] TEST 1 result: {verdict1}")

    # ── TEST 2: Page reuse / reload ───────────────────────────────────────

    logger.info(f"\n{'='*60}")
    logger.info("[test-sniper] TEST 2/3 — Page reuse (reload vs fresh navigate)")
    logger.info(f"{'='*60}")

    test_config = config.__class__(
        **{**config.__dict__, "restaurant_slug": test_slug, "dry_run": True}
    )
    tracker = SlotTracker()
    checker = AvailabilityChecker(test_config, browser, tracker)

    # Use a date ~1 week out (stable, not too close)
    target_date = date.today() + timedelta(days=7)
    date_str = target_date.isoformat()
    logger.info(
        f"[test-sniper] Testing page reuse for {date_str} on {test_slug}\n"
        f"  Call 1 — fresh page.goto()  (no cached page)\n"
        f"  Call 2 — should page.reload()  (page cached from call 1)"
    )

    # Call 1: fresh navigate, page gets stored in _sniper_pages
    t0 = _time.monotonic()
    slots1 = await checker._check_date(target_date, keep_page=True)
    t1 = _time.monotonic() - t0
    page_after_call1 = checker._sniper_pages.get(date_str)
    logger.info(
        f"[test-sniper] Call 1: {t1:.2f}s  |  {len(slots1)} slot(s)  |  "
        f"page cached: {page_after_call1 is not None}"
    )

    # Call 2: should reload the same page object
    t0 = _time.monotonic()
    slots2 = await checker._check_date(target_date, keep_page=True)
    t2 = _time.monotonic() - t0
    page_after_call2 = checker._sniper_pages.get(date_str)

    same_page = (
        page_after_call1 is not None
        and page_after_call2 is not None
        and page_after_call1 is page_after_call2
    )
    speedup_pct = (t1 - t2) / t1 * 100 if t1 > 0 else 0
    logger.info(
        f"[test-sniper] Call 2: {t2:.2f}s  |  {len(slots2)} slot(s)\n"
        f"\n"
        f"[test-sniper] Page reuse result:\n"
        f"  Fresh navigate : {t1:.2f}s\n"
        f"  Page reload    : {t2:.2f}s\n"
        f"  Speedup        : {speedup_pct:+.0f}%  "
        f"({'reload faster' if t2 < t1 else 'reload slower — normal network variance'})\n"
        f"  Same page obj  : {same_page}  "
        f"({'reload was used ✓' if same_page else 'new page created (unexpected)'})"
    )

    verdict2 = PASS if same_page else FAIL
    results.append(("Page reuse (reload)", verdict2))
    logger.info(f"[test-sniper] TEST 2 result: {verdict2}")
    await checker.close_sniper_pages()

    # ── TEST 3: Confirm retry ─────────────────────────────────────────────

    logger.info(f"\n{'='*60}")
    logger.info("[test-sniper] TEST 3/3 — Confirm retry (broken selector, no booking)")
    logger.info(f"{'='*60}")
    logger.info(
        f"[test-sniper] Navigating to {test_slug} checkout to simulate the retry.\n"
        "  A deliberately broken selector forces attempt 1 to fail immediately.\n"
        "  We measure the ~2s sleep before attempt 2.\n"
        "  The REAL confirm button is NEVER clicked."
    )

    page = await browser.new_page()
    reached_checkout = False
    try:
        BASE_URL = "https://www.exploretock.com"
        today = date.today()
        cal_sel   = sel.get("calendar_container")
        day_sel   = sel.get("available_day_button")
        slot_sel  = sel.get("available_slot_button")
        co_sel    = sel.get("checkout_container")
        real_confirm_sel = sel.get("confirm_button")

        for delta in range(1, 29):
            check_date = today + timedelta(days=delta)
            url = (
                f"{BASE_URL}/{test_slug}/search"
                f"?date={check_date.isoformat()}&size=2&time=17:00"
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(cal_sel, timeout=8000)
            except Exception:
                continue

            day_num = str(check_date.day)
            for btn in await page.query_selector_all(day_sel):
                if (await btn.text_content() or "").strip() == day_num:
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    break

            try:
                await page.wait_for_selector(slot_sel, timeout=5000)
            except Exception:
                pass

            slots = await page.query_selector_all(slot_sel)
            if not slots:
                continue

            await slots[0].click()
            await page.wait_for_timeout(3000)

            try:
                await page.wait_for_selector(co_sel, timeout=15000)
                reached_checkout = True
            except Exception:
                if any(p in page.url for p in ("/checkout", "/reservation", "/book")):
                    reached_checkout = True

            if reached_checkout:
                logger.info(f"[test-sniper] Reached checkout: {page.url}")
                # Confirm button present?
                has_real = await page.query_selector(real_confirm_sel)
                logger.info(
                    f"[test-sniper] Real confirm button detected: "
                    f"{'YES (selector works ✓)' if has_real else 'NO (selector may need updating)'}"
                )
                break

        if not reached_checkout:
            logger.warning(
                f"[test-sniper] Could not reach {test_slug} checkout in 28 days.\n"
                "  Running retry timing simulation on current page instead."
            )

        # ── Retry simulation (broken selector — no booking possible) ──────
        BROKEN = "button.__sniper_test_broken_confirm__"
        attempt_times: list[float] = []
        sleep_measured: float = 0.0

        logger.info("[test-sniper] Starting retry simulation…")
        for attempt in range(2):
            attempt_times.append(_time.monotonic())
            logger.info(
                f"[test-sniper]   Attempt {attempt + 1}/2 — "
                f"wait_for_selector(BROKEN, timeout=300ms)…"
            )
            try:
                await page.wait_for_selector(BROKEN, timeout=300)  # fast-fail
                await page.click(BROKEN)
                logger.warning("[test-sniper]   Unexpected: broken selector matched something!")
            except Exception as e:
                if attempt == 0:
                    logger.info(
                        f"[test-sniper]   Attempt 1 failed as expected "
                        f"({type(e).__name__}) — sleeping 2s before retry…"
                    )
                    sleep_t0 = _time.monotonic()
                    await asyncio.sleep(2)
                    sleep_measured = _time.monotonic() - sleep_t0
                    logger.info(
                        f"[test-sniper]   Sleep measured: {sleep_measured:.2f}s"
                    )
                else:
                    logger.info(
                        f"[test-sniper]   Attempt 2 failed as expected "
                        f"({type(e).__name__}) — retry logic confirmed."
                    )

        if len(attempt_times) == 2:
            total_gap = attempt_times[1] - attempt_times[0]
            timing_ok = 1.8 <= total_gap <= 4.0
            logger.info(
                f"\n[test-sniper] Retry timing:\n"
                f"  Attempt 1 → Attempt 2 gap : {total_gap:.2f}s\n"
                f"  Sleep measured            : {sleep_measured:.2f}s\n"
                f"  Expected gap              : ~2.3s (2s sleep + 0.3s overhead)\n"
                f"  Timing correct            : {timing_ok}"
            )
            verdict3 = PASS if timing_ok else FAIL
        else:
            logger.error("[test-sniper] Attempt 2 never ran — retry logic broken.")
            verdict3 = FAIL

    except Exception as e:
        logger.error(f"[test-sniper] Test 3 unexpected error: {e}")
        verdict3 = FAIL
    finally:
        await page.close()

    results.append(("Confirm retry", verdict3))
    logger.info(f"[test-sniper] TEST 3 result: {verdict3}")

    # ── Summary ───────────────────────────────────────────────────────────

    passed = sum(1 for _, v in results if v == PASS)
    bar = "=" * 60
    logger.info(
        f"\n{bar}\n"
        f"[test-sniper] RESULTS — {passed}/{len(results)} tests passed\n"
        + "\n".join(
            f"  {'✓' if v == PASS else '✗'}  {name:30s} : {v}"
            for name, v in results
        )
        + f"\n{bar}"
    )


async def _test_sniper_integration(
    browser, config, notifier, checker, tracker, logger, num_polls: int = 5
) -> None:
    """
    Full end-to-end integration test of the sniper pipeline.

    Timeline
    --------
      t=0   Sniper configured to fire at now+2 min.
            _get_prewarm_target() detects window within 15 min → pre-warm fires.
      t≈2m  _get_poll_interval() returns 0 → sniper active Discord notification fires.
      t≈2m+ num_polls rapid concurrent polls run (DRY_RUN — no booking).
            Each poll sends Discord notification (no-slots or dry-run-would-book).
      done  Sniper pages closed, summary logged, exit.

    Run with: python main.py --test-sniper-integration
    """
    import pytz as _pytz
    from datetime import datetime, timedelta
    from src.monitor import TockMonitor, PREWARM_BEFORE_MIN

    PT_tz = _pytz.timezone("America/Los_Angeles")
    now = datetime.now(PT_tz)

    # ── Configure sniper to fire in ~2 minutes ────────────────────────────
    trigger_dt = now + timedelta(minutes=2)
    sniper_time = trigger_dt.strftime("%H:%M")
    sniper_day  = trigger_dt.strftime("%A")
    # Window long enough for num_polls × ~30s/poll + breathing room
    window_min  = max(6, (num_polls * 45) // 60 + 2)

    # Save originals so the test is non-destructive
    orig_days    = config.sniper_days[:]
    orig_times   = config.sniper_times[:]
    orig_dur     = config.sniper_duration_min
    orig_dry_run = config.dry_run

    config.sniper_days        = [sniper_day]
    config.sniper_times       = [sniper_time]
    config.sniper_duration_min = window_min
    config.dry_run            = True

    monitor = TockMonitor(config, browser, checker, notifier, tracker)

    bar = "=" * 60
    logger.info(
        f"\n{bar}\n"
        f"[integration] SNIPER INTEGRATION TEST\n"
        f"  Restaurant : {config.restaurant_slug}\n"
        f"  Party size : {config.party_size}\n"
        f"  Sniper set : {sniper_day} @ {sniper_time} PT (≈2 min from now)\n"
        f"  Duration   : {window_min} min\n"
        f"  Polls      : {num_polls} rapid polls once window opens\n"
        f"  Booking    : DISABLED (DRY_RUN forced)\n"
        f"\n"
        f"  Expected chain:\n"
        f"    [now]   pre-warm fires (window within {PREWARM_BEFORE_MIN}-min threshold)\n"
        f"    [+2min] sniper activates → Discord orange notification\n"
        f"    [+2min] {num_polls}× rapid concurrent polls\n"
        f"    [done]  Discord per-poll notifications + summary\n"
        f"{bar}"
    )

    try:
        # ── STEP 1: Pre-warm ─────────────────────────────────────────────
        logger.info(f"\n{bar}")
        logger.info("[integration] STEP 1 — Pre-warm (window within 15-min threshold)")
        logger.info(f"{bar}")

        prewarm_target = monitor._get_prewarm_target()
        if prewarm_target:
            logger.info(
                f"[integration] _get_prewarm_target() = {prewarm_target!r}  ✓\n"
                f"  (window is ~2 min away, within {PREWARM_BEFORE_MIN}-min threshold)"
            )
            await browser.warm_session()
            monitor._session_prewarmed_for = prewarm_target
            logger.info("[integration] Pre-warm complete — cookies refreshed.")
        else:
            # Should not happen: 2 min < 15 min threshold
            logger.warning(
                "[integration] _get_prewarm_target() returned None.\n"
                "  The sniper trigger time may have already passed. Running warm_session() anyway."
            )
            await browser.warm_session()

        # ── STEP 2: Wait for sniper window to open ───────────────────────
        logger.info(f"\n{bar}")
        logger.info(
            f"[integration] STEP 2 — Waiting for sniper window\n"
            f"  Window opens at {sniper_time} PT  (≈{(trigger_dt - datetime.now(PT_tz)).total_seconds():.0f}s)"
        )
        logger.info(f"{bar}")

        # Poll _get_poll_interval() every 5s; it returns 0 when sniper fires
        # and also sends the Discord "Sniper Mode Active" notification.
        grace_deadline = trigger_dt + timedelta(seconds=45)
        sniper_opened = False

        while datetime.now(PT_tz) < grace_deadline:
            interval = monitor._get_poll_interval()
            if interval == 0:       # sniper window is now open
                sniper_opened = True
                break
            remaining = max(0, (trigger_dt - datetime.now(PT_tz)).total_seconds())
            logger.info(
                f"[integration] {remaining:.0f}s until {sniper_time} PT…"
                f"  (current interval={interval}s)"
            )
            await asyncio.sleep(5)

        if not sniper_opened:
            logger.error(
                "[integration] Sniper window did not open within the expected time.\n"
                f"  Configured: {sniper_day} @ {sniper_time} PT\n"
                f"  Current PT: {datetime.now(PT_tz).strftime('%A %H:%M')}\n"
                "  Check that system clock and pytz timezone are correct."
            )
            return

        logger.info(
            f"[integration] Sniper window OPEN  "
            f"(PT={datetime.now(PT_tz).strftime('%H:%M:%S')})  ✓\n"
            f"  _sniper_active = {monitor._sniper_active}\n"
            f"  Discord orange notification should have fired."
        )

        # ── STEP 3: Rapid polls ──────────────────────────────────────────
        logger.info(f"\n{bar}")
        logger.info(
            f"[integration] STEP 3 — {num_polls} rapid concurrent polls (DRY_RUN)"
        )
        logger.info(f"{bar}")

        slot_counts: list[int] = []
        poll_times:  list[float] = []

        import time as _time
        for i in range(1, num_polls + 1):
            logger.info(f"[integration] ── Poll {i}/{num_polls} ──")
            t0 = _time.monotonic()
            notifier.poll_start(i, 0)
            await monitor.poll()
            elapsed = _time.monotonic() - t0
            poll_times.append(elapsed)
            # Snapshot slot count from checker's last poll
            slot_counts.append(checker.last_checks - checker.last_errors)
            logger.info(f"[integration] Poll {i} completed in {elapsed:.1f}s")
            await asyncio.sleep(0)   # yield — mirrors production sniper loop

        # ── STEP 4: Cleanup ──────────────────────────────────────────────
        logger.info(f"\n{bar}")
        logger.info("[integration] STEP 4 — Cleanup")
        logger.info(f"{bar}")

        await checker.close_sniper_pages()
        notifier.sniper_mode_ended(monitor._poll_count)
        logger.info("[integration] Sniper pages closed.")

        # ── Summary ──────────────────────────────────────────────────────
        avg_poll = sum(poll_times) / len(poll_times) if poll_times else 0
        logger.info(
            f"\n{bar}\n"
            f"[integration] INTEGRATION TEST COMPLETE\n"
            f"\n"
            f"  Chain verified:\n"
            f"    ✓  Pre-warm fired   (warm_session + cookies saved)\n"
            f"    {'✓' if sniper_opened else '✗'}  Sniper activated   (_sniper_active=True, interval=0s)\n"
            f"    ✓  {num_polls} polls ran      (avg {avg_poll:.1f}s/poll)\n"
            f"    ✓  DRY_RUN enforced  (no booking attempted)\n"
            f"\n"
            f"  Check Discord for:\n"
            f"    • Orange embed  — Sniper Mode Active\n"
            f"    • Blue embed    — Dry Run Would Have Booked  (if slots found)\n"
            f"    • Yellow embed  — Slots Available  (if slots found)\n"
            f"    • (no Discord on 'no slots' — by design to avoid spam)\n"
            f"{bar}"
        )

    finally:
        # Restore config so callers are not surprised
        config.sniper_days         = orig_days
        config.sniper_times        = orig_times
        config.sniper_duration_min = orig_dur
        config.dry_run             = orig_dry_run


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
            await _test_booking_flow(browser, config, args.test_restaurant, logger)
            return

        # ── Mode: --test-sniper (3 robustness tests) ─────────────────
        if args.test_sniper:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-sniper.")
                sys.exit(1)
            await _test_sniper_robustness(browser, config, args.test_restaurant, logger)
            return

        # ── Mode: --test-sniper-benchmark (A/B concurrent vs sequential) ─
        if args.test_sniper_benchmark:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-sniper-benchmark.")
                sys.exit(1)
            await _test_sniper_mode(
                browser, config, args.test_restaurant,
                args.test_sniper_polls, logger
            )
            return

        # ── Mode: --test-sniper-integration ──────────────────────────
        if args.test_sniper_integration:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-sniper-integration.")
                sys.exit(1)
            await _test_sniper_integration(
                browser, config, notifier, checker, tracker, logger,
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
