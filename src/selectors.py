"""
All Tock DOM selectors in one place.

HOW TO UPDATE A BROKEN SELECTOR
--------------------------------
1. Run the bot — it will log a line like:
       SELECTOR_FAILED: key='available_day_button'  selector='button.ConsumerCalendar-day...'
2. Open https://www.exploretock.com/fui-hui-hua-san-francisco in Chrome.
3. Open DevTools → Elements tab (or Network tab for API calls).
4. Find the element manually and copy its selector.
5. Update the relevant value in the SELECTORS dict below.
6. Run `python main.py --verify` to confirm the fix.

SOURCE
------
Selectors reverse-engineered from:
- https://github.com/chinhtle/reserve-tfl  (Selenium / Python)
- https://github.com/azoff/tockstalk       (Cypress / TypeScript)
Last validated against live site: 2024.

NOTE: Tock periodically redesigns their UI. Selectors may break after updates.
      The most likely candidates for change are the checkout/payment selectors.
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selector map
# Each value may be a single CSS/Playwright selector string.
# Playwright supports `:text("…")`, `:has-text("…")`, and CSS combinators.
# ---------------------------------------------------------------------------
SELECTORS: dict[str, str] = {

    # --- Login page ---------------------------------------------------------
    # Email field on the Tock login form
    "login_email": 'input[name="email"]',
    # Password field
    "login_password": 'input[name="password"]',
    # Sign-in submit button (try multiple patterns for resilience)
    "login_submit": 'button[type="submit"], button.Button--primary, button.Button',

    # --- Post-login indicator -----------------------------------------------
    # Element that appears only when the user IS logged in.
    # Used to skip re-login when session cookies are still valid.
    # If this selector is wrong the bot will re-login on every cycle (harmless but slow).
    "logged_in_indicator": (
        '[data-testid="user-avatar"], '
        '.ConsumerHeader-account, '
        'a[href*="/account"], '
        'button[aria-label*="account" i]'
    ),

    # --- Search / calendar page ---------------------------------------------
    # Outer container of the monthly calendar grid
    "calendar_container": "div.ConsumerCalendar-month",
    # Clickable day buttons that have at least one open slot
    "available_day_button": "button.ConsumerCalendar-day.is-in-month.is-available",
    # <span> inside a day button containing the day number ("15", "16", …)
    "day_number_span": "span.B2",
    # Month + year heading (e.g. "March 2024") — used for debug logging only
    "calendar_month_heading": "div.ConsumerCalendar-monthHeading, span.H1",

    # --- Time slot results --------------------------------------------------
    # Clickable button for an available time slot
    "available_slot_button": "button.Consumer-resultsListItem.is-available",
    # <span> inside a slot button that shows the time text ("5:00 PM")
    "slot_time_text": "span.Consumer-resultsListItemTime",

    # --- Checkout / booking form --------------------------------------------
    # Main checkout page container — presence signals we're past slot selection.
    # Multiple fallback selectors; the first match wins.
    "checkout_container": (
        '[data-testid="checkout"], '
        '.ConsumerCheckout, '
        '.ConsumerReservation-checkout, '
        'form.Checkout, '
        '[class*="checkout" i]'
    ),

    # A saved payment card widget on the checkout page.
    # ABSENCE of this element (combined with presence of no_payment_indicator)
    # means no card is saved → bot pauses to let you add one.
    "saved_payment_card": (
        '[data-testid="saved-card"], '
        '.SavedCard, '
        '.PaymentMethod--saved, '
        '[class*="savedCard"], '
        '[class*="SavedPayment"], '
        '[class*="saved-payment"]'
    ),

    # "Add payment method" prompt — if visible, no card is on file.
    "no_payment_indicator": (
        'button:text("Add payment method"), '
        'a:text("Add a card"), '
        'a:text("Add payment"), '
        ':text("Add a payment method"), '
        '[data-testid="add-payment"]'
    ),

    # Final confirm button on the checkout page.
    # Tries multiple text variants Tock has used across UI versions.
    "confirm_button": (
        'button:text("Complete reservation"), '
        'button:text("Confirm reservation"), '
        'button:text("Reserve now"), '
        'button:text("Complete"), '
        'button:text("Confirm"), '
        'button[type="submit"]:visible'
    ),

    # Element shown after booking succeeds (confirmation / thank-you page).
    "booking_confirmed": (
        '[data-testid="confirmation"], '
        '.ConsumerConfirmation, '
        'h1:has-text("booked"), '
        'h1:has-text("confirmed"), '
        ':has-text("Confirmation number"), '
        ':has-text("See you soon")'
    ),
}


# ---------------------------------------------------------------------------
# Accessor
# ---------------------------------------------------------------------------

def get(key: str) -> str:
    """Return the selector for *key*, raising a clear KeyError if unknown."""
    if key not in SELECTORS:
        raise KeyError(
            f"Unknown selector key '{key}'. "
            f"Valid keys: {sorted(SELECTORS.keys())}"
        )
    return SELECTORS[key]


# ---------------------------------------------------------------------------
# Live verification  (python main.py --verify)
# ---------------------------------------------------------------------------

# Maps selector key → relative URL path to test it on.
# {today} is replaced with today's ISO date string at runtime.
_VERIFY_PLAN: dict[str, str] = {
    "login_email":          "/login",
    "login_password":       "/login",
    "login_submit":         "/login",
    "calendar_container":   "/search?date={today}&size=2&time=17:00",
    "available_day_button": "/search?date={today}&size=2&time=17:00",
    "available_slot_button":"/search?date={today}&size=2&time=17:00",
}


async def verify_selectors(browser, config) -> None:
    """
    Load each relevant Tock page and check that every selector finds ≥1 element.
    Logs PASS / FAIL with the exact selector string.
    Run with:  python main.py --verify
    """
    base = f"https://www.exploretock.com/{config.restaurant_slug}"
    today_str = date.today().isoformat()

    # Group keys by URL so we only load each page once
    url_to_keys: dict[str, list[str]] = {}
    for key, rel_path in _VERIFY_PLAN.items():
        url = base + rel_path.format(today=today_str)
        url_to_keys.setdefault(url, []).append(key)

    results: list[tuple[str, str, str]] = []  # (key, status, selector)

    for url, keys in url_to_keys.items():
        page = await browser.new_page()
        logger.info(f"[verify] Loading {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)   # let React render

            for key in keys:
                selector = SELECTORS[key]
                try:
                    el = await page.query_selector(selector)
                    if el:
                        results.append((key, "PASS", selector))
                        logger.info(f"  ✓ PASS   {key}")
                    else:
                        results.append((key, "FAIL — element not found", selector))
                        logger.warning(
                            f"  ✗ FAIL   {key}\n"
                            f"           selector : {selector}\n"
                            f"           → Update src/selectors.py"
                        )
                except Exception as e:
                    results.append((key, f"ERROR: {e}", selector))
                    logger.error(f"  ✗ ERROR  {key}: {e}")
        except Exception as e:
            logger.error(f"[verify] Could not load {url}: {e}")
            for key in keys:
                results.append((key, f"PAGE_LOAD_ERROR: {e}", SELECTORS[key]))
        finally:
            await page.close()

    passed = sum(1 for _, s, _ in results if s == "PASS")
    total = len(results)
    logger.info(f"\n[verify] {passed}/{total} selectors passed.")
    if passed < total:
        logger.warning(
            "[verify] Some selectors need updating.\n"
            "         Open the page in Chrome DevTools and find the new selector,\n"
            "         then update src/selectors.py."
        )
