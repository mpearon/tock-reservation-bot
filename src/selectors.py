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
    # Verified 2026-03-10: profile button has data-testid="profile-button"
    "logged_in_indicator": (
        '[data-testid="profile-button"], '
        '[data-testid="user-avatar"], '
        '.ConsumerHeader-account, '
        'a[href*="/account"]'
    ),

    # --- Search / calendar page ---------------------------------------------
    # Outer container of the monthly calendar grid.
    # Verified 2026-03-10: this selector IS correct. The calendar renders inside
    # SearchBarModalContainer > ConsumerCalendar > ConsumerCalendar-month.
    "calendar_container": "div.ConsumerCalendar-month",
    # Clickable day buttons that have at least one open slot.
    # Verified 2026-03-10: class structure confirmed. States: is-available, is-sold,
    # is-disabled, is-past, is-future, is-today, is-selected (combinable).
    # All current slots are sold/disabled — is-available appears when new slots drop.
    "available_day_button": "button.ConsumerCalendar-day.is-in-month.is-available",
    # <span> inside a day button showing the day number ("15", "16", …).
    # Verified 2026-03-10: was span.B2 (old), now span.MuiTypography-root (MUI v5).
    # Code in checker.py/booker.py reads btn.text_content() directly — more reliable.
    "day_number_span": "span.MuiTypography-root",
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

    # CVC / security code input that appears on the checkout page when a
    # saved card is on file (Tock requires re-entry of CVC per booking).
    "cvc_input": (
        'input[placeholder*="CVC" i], '
        'input[placeholder*="CVV" i], '
        'input[placeholder*="security code" i], '
        'input[name="cvc"], '
        'input[name="cvv"], '
        'input[autocomplete="cc-csc"], '
        '[data-testid*="cvc" i], '
        '[data-testid*="cvv" i]'
    ),

    # Final confirm button on the checkout page.
    # Tries multiple text variants Tock has used across UI versions.
    # "Complete purchase" is the current wording when a card is charged.
    "confirm_button": (
        'button:text("Complete purchase"), '
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
# {friday} is replaced with next Friday's ISO date string at runtime.
# We use next Friday (a preferred booking day) to get the most realistic calendar render.
#
# Keys marked SKIP_IF_SOLD_OUT cannot be verified when all Fuhuihua slots are sold out
# (which is the normal state). They will be reported as "SKIP" rather than FAIL.
_VERIFY_PLAN: dict[str, str] = {
    "login_email":          "/login",
    "login_password":       "/login",
    "login_submit":         "/login",
    "calendar_container":   "/search?date={friday}&size=2&time=17:00",
    # These two only appear when live slots exist — shown as SKIP when sold out
    "available_day_button": "/search?date={friday}&size=2&time=17:00",
    "available_slot_button":"/search?date={friday}&size=2&time=17:00",
}

_SKIP_IF_SOLD_OUT = {"available_day_button", "available_slot_button"}


async def verify_selectors(browser, config) -> None:
    """
    Load each relevant Tock page and check that every selector finds ≥1 element.
    Logs PASS / FAIL with the exact selector string.
    Run with:  python main.py --verify
    """
    from datetime import timedelta
    base = f"https://www.exploretock.com/{config.restaurant_slug}"

    # Next Friday gives the most realistic calendar rendering
    today = date.today()
    days_until_friday = (4 - today.weekday()) % 7 or 7
    friday_str = (today + timedelta(days=days_until_friday)).isoformat()

    # Group keys by URL so we only load each page once
    url_to_keys: dict[str, list[str]] = {}
    for key, rel_path in _VERIFY_PLAN.items():
        url = base + rel_path.format(friday=friday_str)
        url_to_keys.setdefault(url, []).append(key)

    results: list[tuple[str, str, str]] = []  # (key, status, selector)

    for url, keys in url_to_keys.items():
        page = await browser.new_page()
        logger.info(f"[verify] Loading {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            for key in keys:
                selector = SELECTORS[key]

                # Selectors that only appear when live slots exist — skip gracefully
                if key in _SKIP_IF_SOLD_OUT:
                    results.append((key, "SKIP", selector))
                    logger.info(
                        f"  ⊘ SKIP   {key}\n"
                        f"           (only present when slots are available; "
                        f"selector will be tested automatically when slots drop)"
                    )
                    continue

                # Use wait_for_selector so it polls until the element appears
                # (better than a fixed sleep — handles slow React renders)
                try:
                    await page.wait_for_selector(selector, timeout=15000, state="attached")
                    results.append((key, "PASS", selector))
                    logger.info(f"  ✓ PASS   {key}")
                except Exception as e:
                    results.append((key, "FAIL — element not found", selector))
                    logger.warning(
                        f"  ✗ FAIL   {key}\n"
                        f"           selector : {selector}\n"
                        f"           → Update src/selectors.py"
                    )
        except Exception as e:
            logger.error(f"[verify] Could not load {url}: {e}")
            for key in keys:
                results.append((key, f"PAGE_LOAD_ERROR: {e}", SELECTORS[key]))
        finally:
            await page.close()

    passed  = sum(1 for _, s, _ in results if s == "PASS")
    skipped = sum(1 for _, s, _ in results if s == "SKIP")
    failed  = sum(1 for _, s, _ in results if s not in ("PASS", "SKIP"))
    logger.info(
        f"\n[verify] {passed} passed, {skipped} skipped (no live slots), "
        f"{failed} failed  (total {len(results)})"
    )
    if failed:
        logger.warning(
            "[verify] Failing selectors need updating.\n"
            "         Open the page in Chrome DevTools, find the new element,\n"
            "         then update src/selectors.py."
        )
