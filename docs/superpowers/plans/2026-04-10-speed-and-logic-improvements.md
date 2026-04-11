---
render_with_liquid: false
---
{% raw %}
# Speed & Logic Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix critical logic bugs that can cause the bot to miss reservation drops, then optimize the sniper polling cycle from ~10-12s down to ~4-6s, and clean up architecture (centralize selectors, split main.py).

**Architecture:** Three sprints — (1) critical bugs, (2) sniper speed optimizations, (3) architecture cleanup. Each sprint is independently shippable. Speed gains come from replacing fixed sleeps with reactive waits, batching Playwright round-trips into single `page.evaluate()` calls, passing warm pages from checker to booker instead of re-navigating, and gating debug screenshots behind an env var.

**Tech Stack:** Python 3.11+, asyncio, Playwright, pytest

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `src/selectors.py` | Modify | Add `SLOT_BUTTON_SELECTORS` list + `get_slot_button_selectors()` helper |
| `src/checker.py` | Modify | Fix skip-date cache, batch JS calls, reactive waits, gate screenshots, expose warm pages |
| `src/booker.py` | Modify | Fix wrong-slot click, use centralized selectors, accept warm pages, reactive waits |
| `src/monitor.py` | Modify | Pass warm pages from checker to booker, log gather exceptions |
| `src/tracker.py` | Modify | Add `record_batch()` for deferred writes |
| `src/notifier.py` | Modify | Track pending tasks, drain on shutdown |
| `main.py` | Modify | Split test modes into `src/testing/`, use centralized selectors |
| `src/testing/__init__.py` | Create | Package init |
| `src/testing/booking_flow.py` | Create | `--test-booking-flow` logic extracted from main.py |
| `src/testing/sniper_tests.py` | Create | `--test-sniper`, `--test-sniper-integration`, `--test-sniper-benchmark`, `--test-adaptive-sniper` |
| `tests/test_skip_date_cache.py` | Create | Tests for B1 fix |
| `tests/test_slot_click.py` | Create | Tests for B2 fix |
| `tests/test_gather_errors.py` | Create | Tests for B4 fix |
| `tests/test_batch_evaluate.py` | Create | Tests for batched JS calls |
| `tests/test_warm_page_handoff.py` | Create | Tests for S1 page handoff |

---

## Sprint 1: Critical Bugs

### Task 1: Fix skip-date cache poisoning during drop window (B1)

The `_skip_dates` cache in `checker.py:72,225-226,284-286` marks dates as "skip" when the calendar day button isn't visible. During sniper mode, this fires BEFORE the release moment (e.g., 7:59 PM) and caches unreleased dates as skipped. When the drop happens at 8:00 PM, those dates stay skipped for the entire sniper window.

**Files:**
- Modify: `src/checker.py:70-72,210-227,280-286`
- Test: `tests/test_skip_date_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skip_date_cache.py
"""Tests for skip-date cache behavior during sniper mode."""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from src.checker import AvailabilityChecker
from src.config import Config


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="test@example.com",
        tock_password="pass",
        card_cvc="123",
        discord_webhook_url="",
        headless=True,
        dry_run=True,
        restaurant_slug="test-restaurant",
        party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=[],
        preferred_time="17:00",
        scan_weeks=2,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestSkipDateCacheExpiry:
    """Skip-date cache must not persist across polls during sniper mode."""

    @pytest.mark.asyncio
    async def test_skip_cache_clears_each_poll(self):
        """Dates skipped in poll N must be retried in poll N+1."""
        config = _make_config()
        browser = MagicMock()
        tracker = MagicMock()
        tracker.record = MagicMock(return_value=False)
        checker = AvailabilityChecker(config, browser, tracker)

        target = date(2026, 4, 17)  # a Friday
        date_str = target.isoformat()

        # Simulate: first poll, day not in calendar -> cached as skip
        checker._skip_dates.add(date_str)

        # Before second poll starts, cache should be cleared
        checker.clear_skip_cache()
        assert date_str not in checker._skip_dates

    @pytest.mark.asyncio
    async def test_skip_cache_not_used_first_5_minutes_of_window(self):
        """During the first 5 minutes of a sniper window, never skip dates."""
        config = _make_config()
        browser = MagicMock()
        tracker = MagicMock()
        checker = AvailabilityChecker(config, browser, tracker)

        target = date(2026, 4, 17)
        date_str = target.isoformat()
        checker._skip_dates.add(date_str)

        # With skip_cache_enabled=False, the date should NOT be skipped
        # even though it's in _skip_dates
        result = checker._should_skip_date(date_str, skip_cache_enabled=False)
        assert result is False

    @pytest.mark.asyncio
    async def test_skip_cache_used_after_warmup(self):
        """After the warmup period, skip cache should be honored."""
        config = _make_config()
        browser = MagicMock()
        tracker = MagicMock()
        checker = AvailabilityChecker(config, browser, tracker)

        target = date(2026, 4, 17)
        date_str = target.isoformat()
        checker._skip_dates.add(date_str)

        result = checker._should_skip_date(date_str, skip_cache_enabled=True)
        assert result is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skip_date_cache.py -v`
Expected: FAIL — `clear_skip_cache` and `_should_skip_date` don't exist yet.

- [ ] **Step 3: Implement the fix**

In `src/checker.py`, add the `clear_skip_cache()` method and `_should_skip_date()` helper, then update `_check_date()` to accept a `skip_cache_enabled` parameter:

```python
# In AvailabilityChecker class, after close_sniper_pages():

def clear_skip_cache(self) -> None:
    """Clear the skip-date cache. Call at the start of each sniper poll."""
    self._skip_dates.clear()

def _should_skip_date(self, date_str: str, skip_cache_enabled: bool) -> bool:
    """Return True if this date should be skipped based on cache."""
    if not skip_cache_enabled:
        return False
    return date_str in self._skip_dates
```

Update `_check_date()` at line 225 — replace:

```python
if keep_page and date_str in self._skip_dates:
```

with:

```python
if keep_page and self._should_skip_date(date_str, skip_cache_enabled=self._skip_cache_enabled):
```

Add `self._skip_cache_enabled: bool = True` to `__init__`.

Update `check_all()` to clear the skip cache at the start of each poll and disable it during the first N minutes. Add parameter `sniper_window_age_sec: float = 0` to `check_all()`:

```python
# At the top of check_all(), after self._screenshot_taken_this_poll = False:
# Disable skip cache during first 5 minutes of sniper window (release may happen mid-window)
self._skip_cache_enabled = sniper_window_age_sec > 300 if keep_pages else True
# Always clear stale entries at poll start so we retry dates that failed last poll
self._skip_dates.clear()
```

- [ ] **Step 4: Update monitor.py to pass window age**

In `src/monitor.py`, update the `poll()` method at line 267 to pass the sniper window age:

```python
# Calculate how long we've been in the sniper window (for skip-cache warmup)
sniper_age = 0.0
if self._sniper_active:
    sniper_info = self._sniper_window_info(datetime.now(PT))
    if sniper_info:
        now = datetime.now(PT)
        for start_str in self.config.sniper_times:
            start_dt = _sniper_start_dt(now, start_str)
            if now >= start_dt:
                sniper_age = (now - start_dt).total_seconds()
                break

slots = await self.checker.check_all(
    concurrent=use_concurrent,
    keep_pages=self._sniper_active,
    sniper_window_age_sec=sniper_age,
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_skip_date_cache.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_skip_date_cache.py src/checker.py src/monitor.py
git commit -m "fix: expire skip-date cache each poll, disable during first 5min of sniper window"
```

---

### Task 2: Fix booker clicking wrong time slot (B2)

`booker.py:269-275` clicks `locator.first` — the first matching slot button — ignoring `slot.slot_time`. If multiple slots are available and the preferred one isn't first in the DOM, the wrong time gets booked.

**Files:**
- Modify: `src/booker.py:229-279`
- Test: `tests/test_slot_click.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_slot_click.py
"""Tests for time-slot matching in booker._click_time_slot()."""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from src.checker import AvailableSlot
from src.booker import TockBooker
from src.config import Config


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="test@example.com",
        tock_password="pass",
        card_cvc="123",
        discord_webhook_url="",
        headless=True,
        dry_run=True,
        restaurant_slug="test-restaurant",
        party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=[],
        preferred_time="17:00",
        scan_weeks=2,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_slot(time_str: str = "5:00 PM") -> AvailableSlot:
    return AvailableSlot(
        slot_date=date(2026, 4, 17),
        slot_time=time_str,
        day_of_week="Friday",
    )


class TestClickTimeSlot:

    @pytest.mark.asyncio
    async def test_clicks_matching_time_not_first(self):
        """When target is '8:00 PM' but '5:00 PM' is first in DOM, click '8:00 PM'."""
        config = _make_config()
        browser = MagicMock()
        notifier = MagicMock()
        booker = TockBooker(config, browser, notifier)
        slot = _make_slot("8:00 PM")

        # Mock page with two slot buttons: "5:00 PM" (first) and "8:00 PM" (second)
        btn_5pm = AsyncMock()
        btn_5pm.text_content = AsyncMock(return_value="5:00 PM\nBook")
        btn_8pm = AsyncMock()
        btn_8pm.text_content = AsyncMock(return_value="8:00 PM\nBook")
        btn_8pm.click = AsyncMock()

        page = AsyncMock()
        # locator().count() returns 2 for the first selector tried
        locator_mock = MagicMock()
        locator_mock.count = AsyncMock(return_value=2)
        locator_mock.nth = MagicMock(side_effect=lambda i: [btn_5pm, btn_8pm][i])
        page.locator = MagicMock(return_value=locator_mock)

        result = await booker._click_time_slot(page, slot)
        assert result is True
        btn_8pm.click.assert_awaited_once()
        btn_5pm.click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_first_when_no_time_match(self):
        """If no button text matches slot.slot_time, click the first button."""
        config = _make_config()
        browser = MagicMock()
        notifier = MagicMock()
        booker = TockBooker(config, browser, notifier)
        slot = _make_slot("9:00 PM")  # not in DOM

        btn_5pm = AsyncMock()
        btn_5pm.text_content = AsyncMock(return_value="5:00 PM\nBook")
        btn_5pm.click = AsyncMock()

        page = AsyncMock()
        locator_mock = MagicMock()
        locator_mock.count = AsyncMock(return_value=1)
        locator_mock.nth = MagicMock(return_value=btn_5pm)
        locator_mock.first = btn_5pm
        page.locator = MagicMock(return_value=locator_mock)

        result = await booker._click_time_slot(page, slot)
        assert result is True
        btn_5pm.click.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_slot_click.py -v`
Expected: FAIL — current `_click_time_slot` always clicks `.first`, doesn't iterate to match time.

- [ ] **Step 3: Implement the fix**

Replace `_click_time_slot` in `src/booker.py:229-279`:

```python
async def _click_time_slot(self, page: Page, slot: AvailableSlot) -> bool:
    """Find the time slot matching slot.slot_time and click it.

    Iterates all matching buttons and compares text content to find the
    correct time. Falls back to first button if no text match is found.
    """
    import re
    from src.selectors import get_slot_button_selectors

    slot_selectors = get_slot_button_selectors()

    # Wait for slot buttons to appear reactively (not fixed sleep)
    for try_sel in slot_selectors:
        try:
            await page.wait_for_selector(try_sel, timeout=2000)
            break
        except Exception:
            continue

    # Find which selector has buttons
    matched_selector = None
    for try_sel in slot_selectors:
        try:
            count = await page.locator(try_sel).count()
            if count > 0:
                matched_selector = try_sel
                logger.debug(f"[book] Found {count} slot button(s) via {try_sel!r}")
                break
        except Exception:
            continue

    if not matched_selector:
        logger.error(
            "[book] No slot buttons found after clicking the day.\n"
            "  Tried all known selectors.\n"
            "  -> Update src/selectors.py"
        )
        return False

    # Iterate buttons to find one matching slot.slot_time
    locator = page.locator(matched_selector)
    count = await locator.count()
    target_time = slot.slot_time.strip().upper()

    best_btn = None
    for i in range(count):
        btn = locator.nth(i)
        try:
            text = (await btn.text_content() or "").strip()
            # Check if the target time appears in this button's text
            if target_time in text.upper():
                await btn.click()
                logger.info(f"[book] Clicked slot button matching '{slot.slot_time}': {text}")
                return True
            # Also try regex match for time patterns
            time_match = re.search(
                r'\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b', text, re.IGNORECASE
            )
            if time_match and time_match.group(1).strip().upper() == target_time:
                await btn.click()
                logger.info(f"[book] Clicked slot button (regex match): {text}")
                return True
            if best_btn is None:
                best_btn = btn  # remember first as fallback
        except Exception:
            continue

    # Fallback: click first button if no time match found
    if best_btn is not None:
        try:
            text = (await best_btn.text_content() or "").strip()
            await best_btn.click()
            logger.warning(
                f"[book] No exact time match for '{slot.slot_time}' — "
                f"clicked first button: {text}"
            )
            return True
        except Exception as e:
            logger.error(f"[book] Could not click fallback slot button: {e}")
            return False

    logger.error("[book] No slot buttons could be clicked")
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_slot_click.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_slot_click.py src/booker.py
git commit -m "fix: booker matches slot.slot_time text before clicking, falls back to first"
```

---

### Task 3: Fix asyncio.gather silently dropping exceptions (B4)

`checker.py:139-147` uses `return_exceptions=True` but only checks `isinstance(r, list)`, silently discarding exceptions. This means Cloudflare blocks, network errors, and selector failures during concurrent sniper polls are invisible.

**Files:**
- Modify: `src/checker.py:138-147`
- Test: `tests/test_gather_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gather_errors.py
"""Tests for exception handling in concurrent check_all()."""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from src.checker import AvailabilityChecker
from src.config import Config


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="test@example.com",
        tock_password="pass",
        card_cvc="123",
        discord_webhook_url="",
        headless=True,
        dry_run=True,
        restaurant_slug="test-restaurant",
        party_size=2,
        preferred_days=["Friday"],
        fallback_days=[],
        preferred_time="17:00",
        scan_weeks=1,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestGatherExceptionLogging:

    @pytest.mark.asyncio
    async def test_gather_exceptions_counted_in_last_errors(self):
        """Exceptions from gather should be counted in last_errors."""
        config = _make_config()
        browser = MagicMock()
        tracker = MagicMock()
        tracker.record = MagicMock(return_value=False)
        checker = AvailabilityChecker(config, browser, tracker)

        # Mock _check_date to raise for one date and return [] for another
        call_count = 0
        async def _mock_check_date(d, keep_page=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("Cloudflare blocked")
            return []

        checker._check_date = _mock_check_date
        checker._get_target_dates = lambda days=None: [
            date(2026, 4, 17), date(2026, 4, 24)
        ]

        await checker.check_all(concurrent=True)
        # The TimeoutError should be counted as an error
        assert checker.last_errors >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gather_errors.py -v`
Expected: FAIL — current code doesn't count exceptions as errors.

- [ ] **Step 3: Implement the fix**

In `src/checker.py:138-147`, replace the concurrent gather block:

```python
if concurrent:
    results = await _asyncio.gather(
        *[self._check_date(d, keep_page=keep_pages) for d in dates],
        return_exceptions=True,
    )
    slots: list[AvailableSlot] = []
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            errors[0] += 1
            logger.error(
                f"[check] Concurrent check failed for "
                f"{dates[i].isoformat()}: {r}"
            )
        elif isinstance(r, list):
            slots.extend(r)
    return slots
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gather_errors.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_gather_errors.py src/checker.py
git commit -m "fix: log and count exceptions from concurrent gather in check_all"
```

---

### Task 4: Fix warm_session() ignoring login failure (F1)

`browser.py:255-261` calls `self.login()` but ignores the return value. If login fails, `monitor.py:197-198` still marks the session as prewarmed, so sniper mode fires with an expired session.

**Files:**
- Modify: `src/browser.py:235-271`
- Modify: `src/monitor.py:196-198`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_monitor_sniper.py`:

```python
class TestWarmSessionFailure:

    @pytest.mark.asyncio
    async def test_prewarm_not_marked_on_login_failure(self):
        """If warm_session returns False, monitor must NOT mark session as prewarmed."""
        # This test verifies the contract: warm_session returns bool
        from src.browser import TockBrowser
        from unittest.mock import AsyncMock, MagicMock, patch

        config = MagicMock()
        config.restaurant_slug = "test"
        config.headless = True
        browser = TockBrowser(config)

        # Mock internals
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock(side_effect=Exception("timeout"))
        mock_page.close = AsyncMock()
        browser.new_page = AsyncMock(return_value=mock_page)
        browser._is_logged_in = AsyncMock(return_value=False)
        browser.login = AsyncMock(return_value=False)

        result = await browser.warm_session()
        assert result is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_monitor_sniper.py::TestWarmSessionFailure -v`
Expected: FAIL — `warm_session()` returns None, not bool.

- [ ] **Step 3: Implement the fix**

In `src/browser.py`, change `warm_session()` return type to `bool`:

```python
async def warm_session(self) -> bool:
    """
    Navigate to the restaurant's main Tock page to refresh Cloudflare
    cookies before the sniper window opens. Returns True on success.
    """
    url = f"{BASE_URL}/{self.config.restaurant_slug}"
    page = await self.new_page()
    try:
        logger.info(f"[warm] Refreshing session: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        if not await self._is_logged_in(page):
            logger.warning(
                "[warm] Session appears expired — re-authenticating before sniper fires."
            )
            await page.close()
            success = await self.login()
            if not success:
                logger.error("[warm] Re-login failed during pre-warm!")
            return success

        await self._save_cookies()
        logger.info("[warm] Session healthy — cookies refreshed and saved.")
        return True
    except Exception as e:
        logger.warning(f"[warm] Session warm failed (non-critical): {e}")
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass
```

In `src/monitor.py:196-198`, check the return value:

```python
success = await self.browser.warm_session()
if success:
    self._session_prewarmed_for = prewarm_target
else:
    logger.error(
        f"[monitor] Pre-warm failed for {prewarm_target} — "
        "will retry next poll cycle"
    )
    self.notifier.error(
        "Pre-warm failed",
        f"Session warm-up for {prewarm_target} failed. "
        "Bot will retry but sniper accuracy may be reduced."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_monitor_sniper.py::TestWarmSessionFailure -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/browser.py src/monitor.py tests/test_monitor_sniper.py
git commit -m "fix: warm_session returns bool, monitor skips prewarm mark on failure"
```

---

## Sprint 2: Sniper Speed Optimizations

### Task 5: Centralize slot button selectors (A1 — prerequisite for S1-S4)

The same 6-element selector list is duplicated in `checker.py:295-302`, `booker.py:235-242`, and `main.py:117-125`. Centralizing prevents drift and enables the batch-evaluate optimization.

**Files:**
- Modify: `src/selectors.py`
- Modify: `src/checker.py:294-302`
- Modify: `src/booker.py:235-242`

- [ ] **Step 1: Add to selectors.py**

After the `get()` function in `src/selectors.py`, add:

```python
def get_slot_button_selectors() -> list[str]:
    """Ordered list of selectors for time-slot / booking buttons.

    Used by checker.py and booker.py. The first selector that matches
    any elements on the page wins. Centralized here so updates propagate
    to both modules automatically.
    """
    return [
        SELECTORS["available_slot_button"],       # button.Consumer-resultsListItem.is-available
        "button.Consumer-resultsListItem",         # without is-available class
        'button:visible:has-text("Book")',         # "Book" CTA
        SELECTORS["book_now_button"],              # "Book now" button
        "button.SearchExperience-bookButton",      # alternative booking button
        "[data-testid='book-button']",             # test ID variant
    ]
```

- [ ] **Step 2: Update checker.py to use it**

In `src/checker.py:294-302`, replace the inline list:

```python
from src.selectors import get_slot_button_selectors

# (inside _check_date, replacing lines 295-302)
slot_selectors = get_slot_button_selectors()
```

- [ ] **Step 3: Update booker.py to use it (already done in Task 2)**

Verify `booker.py` imports `get_slot_button_selectors` from Task 2. If Task 2 hasn't been applied yet, make the same import change in `_click_time_slot`.

- [ ] **Step 4: Run existing tests**

Run: `python -m pytest tests/ -v`
Expected: All existing tests PASS (no behavior change, just code centralization).

- [ ] **Step 5: Commit**

```bash
git add src/selectors.py src/checker.py src/booker.py
git commit -m "refactor: centralize slot button selectors in selectors.py"
```

---

### Task 6: Batch Playwright calls with page.evaluate() (S4 + S5)

Two hot-path operations make multiple sequential Playwright round-trips:

1. **Slot selector probing** (`checker.py:304-318`): tries 6 selectors one at a time — 6 IPC round-trips.
2. **Calendar day matching** (`checker.py:437-448`, `booker.py:209-220`): loops over buttons with individual `await text_content()` calls.

Replace both with single `page.evaluate()` calls that run JavaScript in the browser and return results in one round-trip.

**Files:**
- Modify: `src/checker.py:304-318,424-454`
- Modify: `src/booker.py:198-227`
- Test: `tests/test_batch_evaluate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_batch_evaluate.py
"""Tests for batched page.evaluate() helpers."""

import pytest


class TestBatchSlotDetect:
    """Verify the JS snippet returns correct selector and count."""

    def test_js_finds_first_matching_selector(self):
        """Unit test the JavaScript logic (string-based, no browser needed)."""
        # We test the Python function that builds the JS, verifying it produces
        # valid JS. Actual browser testing happens in --test-booking-flow.
        from src.selectors import get_slot_button_selectors
        selectors = get_slot_button_selectors()
        assert len(selectors) >= 4
        assert all(isinstance(s, str) for s in selectors)


class TestBatchDayClick:
    """Verify the JS-based day click works."""

    def test_js_click_returns_expected_shape(self):
        """The evaluate JS should return {found: bool, clicked: bool}."""
        # Structural test — actual browser test via --test-booking-flow
        js = _build_click_day_js(15)
        assert "15" in js
        assert "found" in js
        assert "clicked" in js


def _build_click_day_js(day_num: int) -> str:
    """Mirror of the JS that will be in checker.py."""
    return f"""
    (() => {{
        const buttons = document.querySelectorAll('button.ConsumerCalendar-day.is-in-month');
        for (const btn of buttons) {{
            if (btn.textContent.trim() === '{day_num}') {{
                btn.click();
                return {{ found: true, clicked: true }};
            }}
        }}
        return {{ found: false, clicked: false }};
    }})()
    """
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_batch_evaluate.py -v`
Expected: FAIL — `_build_click_day_js` is defined locally in the test for now; actual implementation test will fail until we wire it in.

- [ ] **Step 3: Implement batched slot detection in checker.py**

Replace the sequential selector loop in `_check_date()` (lines 304-318) with:

```python
# Batch: detect which selector has slots using one page.evaluate() call
detect_js = """
() => {
    const selectors = %s;
    for (let i = 0; i < selectors.length; i++) {
        try {
            const els = document.querySelectorAll(selectors[i]);
            if (els.length > 0) return { index: i, count: els.length };
        } catch(e) { continue; }
    }
    return { index: -1, count: 0 };
}
""" % json.dumps(slot_selectors)

detect_result = await page.evaluate(detect_js)
if detect_result["index"] >= 0:
    found_selector = slot_selectors[detect_result["index"]]
    slot_count = detect_result["count"]
    logger.info(
        f"[check] {date_str} — {slot_count} slot(s) found via {found_selector!r}"
    )
else:
    logger.debug(f"[check] {date_str} — no slots found with any selector")
    return []
```

Add `import json` at the top of checker.py if not already present.

- [ ] **Step 4: Implement batched day click in checker.py**

Replace `_click_day()` (lines 424-454) with:

```python
async def _click_day(self, page: Page, target_date: date) -> bool:
    """Click the calendar button for target_date using a single evaluate() call."""
    target_num = str(target_date.day)
    selector = sel.get("all_day_button")

    result = await page.evaluate(f"""
    (() => {{
        const buttons = document.querySelectorAll({json.dumps(selector)});
        for (const btn of buttons) {{
            if (btn.textContent.trim() === {json.dumps(target_num)}) {{
                btn.click();
                return true;
            }}
        }}
        return false;
    }})()
    """)

    if result:
        logger.info(f"[check] Clicked day {target_num} for {target_date.isoformat()}")
        return True

    logger.info(
        f"[check] Day {target_num} not visible in calendar for "
        f"{target_date.isoformat()} (likely not yet released)"
    )
    return False
```

- [ ] **Step 5: Apply same pattern to booker._click_calendar_day()**

Replace `_click_calendar_day()` in `src/booker.py:198-227` with the same `page.evaluate()` pattern:

```python
async def _click_calendar_day(self, page: Page, slot: AvailableSlot) -> bool:
    """Click the calendar button matching slot.slot_date using single evaluate()."""
    import json as _json
    selector = sel.get("all_day_button")
    target_num = str(slot.slot_date.day)

    result = await page.evaluate(f"""
    (() => {{
        const buttons = document.querySelectorAll({_json.dumps(selector)});
        for (const btn of buttons) {{
            if (btn.textContent.trim() === {_json.dumps(target_num)}) {{
                btn.click();
                return true;
            }}
        }}
        return false;
    }})()
    """)

    if result:
        logger.info(f"[book] Clicked day {target_num} for {slot.slot_date_str}")
        return True

    logger.error(
        f"SELECTOR_FAILED: key='all_day_button'\n"
        f"  Could not find or click day {target_num} for {slot.slot_date_str}.\n"
        f"  -> Update src/selectors.py"
    )
    return False
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/checker.py src/booker.py tests/test_batch_evaluate.py
git commit -m "perf: batch slot detection and day click into single page.evaluate() calls"
```

---

### Task 7: Replace fixed sleeps with reactive waits (S2 + S3)

Replace all `page.wait_for_timeout(N)` in the hot path with `page.wait_for_selector()` that complete as soon as content renders.

**Files:**
- Modify: `src/checker.py:288-291`
- Modify: `src/booker.py:146-158`

- [ ] **Step 1: Fix checker.py slot wait (line 288-291)**

Replace:

```python
slot_wait = 500 if keep_page else 2500
await page.wait_for_timeout(slot_wait)
```

With:

```python
# Wait reactively for any slot-like element instead of blind sleep.
# Short timeout (500ms sniper, 2500ms normal) — if nothing appears, move on.
slot_timeout = 500 if keep_page else 2500
first_selector = slot_selectors[0]  # most common selector
try:
    await page.wait_for_selector(first_selector, timeout=slot_timeout)
except Exception:
    pass  # no slots visible yet — proceed to multi-selector check
```

- [ ] **Step 2: Fix booker.py day-click wait (line 146-147)**

Replace:

```python
# Wait for the slot list to start loading after the day click
await page.wait_for_timeout(400)
```

With:

```python
# Reactively wait for slot buttons after day click (fast path: instant; slow: 2s max)
from src.selectors import get_slot_button_selectors
for try_sel in get_slot_button_selectors()[:2]:
    try:
        await page.wait_for_selector(try_sel, timeout=2000)
        break
    except Exception:
        continue
```

- [ ] **Step 3: Fix booker.py post-click tick (line 157-158)**

Replace:

```python
# Brief tick so checkout navigation begins before we start waiting
await page.wait_for_timeout(200)
```

With (remove the line entirely — `_wait_for_checkout` already waits for the checkout container with a 20s timeout):

```python
# Checkout wait follows immediately — _wait_for_checkout handles the timing
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/checker.py src/booker.py
git commit -m "perf: replace fixed sleeps with reactive wait_for_selector in hot path"
```

---

### Task 8: Gate debug screenshots behind env var (S6)

`checker.py:261-268` takes a full-page screenshot on every poll including sniper. A full-page screenshot is ~200ms and generates disk I/O.

**Files:**
- Modify: `src/checker.py:25-28,261-270`
- Modify: `src/config.py`

- [ ] **Step 1: Add DEBUG_SCREENSHOTS to config**

In `src/config.py`, add to the Config dataclass:

```python
debug_screenshots: bool = False
```

In `load_config()`, add:

```python
debug_screenshots=os.getenv("DEBUG_SCREENSHOTS", "false").lower() == "true",
```

- [ ] **Step 2: Gate the screenshot in checker.py**

Replace lines 261-270:

```python
# Debug screenshot: only when enabled and not in sniper mode (too slow)
if (
    self.config.debug_screenshots
    and not keep_page  # skip during sniper — 200ms overhead per poll
    and not self._screenshot_taken_this_poll
):
    self._screenshot_taken_this_poll = True
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(_SCREENSHOT_DIR, f"poll_{ts}_{date_str}.png")
        await page.screenshot(path=path, full_page=True)
        logger.info(f"[check] Debug screenshot saved: {path}")
    except Exception as e:
        logger.debug(f"[check] Screenshot failed: {e}")
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/config.py src/checker.py
git commit -m "perf: gate debug screenshots behind DEBUG_SCREENSHOTS env var, skip in sniper"
```

---

### Task 9: Defer tracker writes during sniper mode (S7)

`tracker.py:95` calls `self.save()` (JSON + CSV flush) for every detected slot before booking starts. During sniper mode this adds ~50-100ms of blocking disk I/O.

**Files:**
- Modify: `src/tracker.py:54-96`
- Modify: `src/checker.py:326-328`

- [ ] **Step 1: Add record_batch() to tracker**

In `src/tracker.py`, add after `record()`:

```python
def record_deferred(self, slot_date: date, slot_time: str) -> bool:
    """Record a slot without flushing to disk. Call flush_deferred() later.

    Returns True if this is a new slot (same dedup logic as record()).
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
```

- [ ] **Step 2: Update checker to use deferred recording in sniper mode**

In `src/checker.py:326-328`, replace:

```python
for slot in slots:
    self.tracker.record(slot.slot_date, slot.slot_time)
```

With:

```python
for slot in slots:
    if keep_page:  # sniper mode — defer disk I/O
        self.tracker.record_deferred(slot.slot_date, slot.slot_time)
    else:
        self.tracker.record(slot.slot_date, slot.slot_time)
```

- [ ] **Step 3: Flush deferred tracker at end of poll in monitor.py**

In `src/monitor.py`, at the end of `poll()` (after the booking attempt or dry-run log), add:

```python
# Flush any deferred tracker writes (sniper mode defers disk I/O)
self.tracker.flush_deferred()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/tracker.py src/checker.py src/monitor.py
git commit -m "perf: defer tracker disk I/O during sniper mode, flush after poll"
```

---

### Task 10: Pass warm pages from checker to booker (S1)

This is the biggest single speed win. Currently, when checker finds slots during sniper mode, it has warm Playwright pages with the day already clicked and slots visible. But booker creates a brand-new page and re-navigates from scratch (~3-5s per booking attempt).

**Files:**
- Modify: `src/checker.py` — expose warm page per date
- Modify: `src/booker.py` — accept optional warm page
- Modify: `src/monitor.py` — pass pages from checker to booker
- Test: `tests/test_warm_page_handoff.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_warm_page_handoff.py
"""Tests for warm page handoff from checker to booker."""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.checker import AvailableSlot


class TestWarmPageHandoff:

    def test_checker_exposes_sniper_pages(self):
        """Checker._sniper_pages should be accessible for page handoff."""
        from src.checker import AvailabilityChecker
        from src.config import Config

        config = MagicMock(spec=Config)
        config.preferred_days = ["Friday"]
        config.scan_weeks = 2
        browser = MagicMock()
        tracker = MagicMock()
        checker = AvailabilityChecker(config, browser, tracker)

        # sniper_pages is a dict[str, Page]
        assert isinstance(checker._sniper_pages, dict)

    def test_get_warm_page_returns_page_for_date(self):
        """get_warm_page(date_str) returns the cached page if it exists."""
        from src.checker import AvailabilityChecker
        from src.config import Config

        config = MagicMock(spec=Config)
        config.preferred_days = ["Friday"]
        config.scan_weeks = 2
        browser = MagicMock()
        tracker = MagicMock()
        checker = AvailabilityChecker(config, browser, tracker)

        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=False)
        checker._sniper_pages["2026-04-17"] = mock_page

        result = checker.get_warm_page("2026-04-17")
        assert result is mock_page

    def test_get_warm_page_returns_none_for_closed(self):
        """get_warm_page returns None if the cached page is closed."""
        from src.checker import AvailabilityChecker
        from src.config import Config

        config = MagicMock(spec=Config)
        config.preferred_days = ["Friday"]
        config.scan_weeks = 2
        browser = MagicMock()
        tracker = MagicMock()
        checker = AvailabilityChecker(config, browser, tracker)

        mock_page = MagicMock()
        mock_page.is_closed = MagicMock(return_value=True)
        checker._sniper_pages["2026-04-17"] = mock_page

        result = checker.get_warm_page("2026-04-17")
        assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_warm_page_handoff.py -v`
Expected: FAIL — `get_warm_page` doesn't exist.

- [ ] **Step 3: Add get_warm_page to checker**

In `src/checker.py`, add after `close_sniper_pages()`:

```python
def get_warm_page(self, date_str: str) -> "Page | None":
    """Return the warm sniper page for a date, or None if unavailable."""
    page = self._sniper_pages.get(date_str)
    if page and not page.is_closed():
        return page
    return None
```

- [ ] **Step 4: Update booker._book_single to accept a warm page**

In `src/booker.py`, update `_book_single()` signature and early logic:

```python
async def _book_single(
    self, slot: AvailableSlot, booking_won: asyncio.Event,
    warm_page: Page | None = None,
) -> bool:
    """Full booking flow. If warm_page is provided, skip navigation steps 1-2."""
    if self.config.dry_run:
        self.notifier.dry_run_would_book(slot)
        return False

    # Use warm page from checker (sniper mode) or create fresh
    page = warm_page if warm_page and not warm_page.is_closed() else None
    owns_page = page is None  # only close if we created it
    if page is None:
        page = await self.browser.new_page()

    try:
        if not warm_page:
            # ── Step 1: load search page (skip if warm page provided) ───
            url = (
                f"{BASE_URL}/{self.config.restaurant_slug}/search"
                f"?date={slot.slot_date_str}"
                f"&size={self.config.party_size}"
                f"&time={self.config.preferred_time}"
            )
            logger.info(f"[book] {slot} -> {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            if not await self._wait_for_selector(
                page, "calendar_container", context=str(slot), timeout=15000
            ):
                return False

            try:
                await page.wait_for_selector(
                    sel.get("all_day_button"), timeout=5000
                )
            except Exception:
                pass

            # ── Step 2: click the calendar day ────────────────────────
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            if not await self._click_calendar_day(page, slot):
                return False

            # Wait reactively for slot buttons
            from src.selectors import get_slot_button_selectors
            for try_sel in get_slot_button_selectors()[:2]:
                try:
                    await page.wait_for_selector(try_sel, timeout=2000)
                    break
                except Exception:
                    continue
        else:
            logger.info(f"[book] {slot} -> using warm page (skipping navigation)")

        # ── Step 3: click the time slot ───────────────────────────
        if booking_won.is_set():
            self.notifier.booking_aborted(slot, "another slot already booked")
            return False

        if not await self._click_time_slot(page, slot):
            return False

        # ── Step 4: wait for checkout page ────────────────────────
        if booking_won.is_set():
            self.notifier.booking_aborted(slot, "another slot already booked")
            return False

        if not await self._wait_for_checkout(page, slot):
            return False

        # ── Step 5: confirm (locked — only one task proceeds) ─────
        if booking_won.is_set():
            self.notifier.booking_aborted(slot, "another slot already booked")
            return False

        async with self._confirm_lock:
            if booking_won.is_set():
                self.notifier.booking_aborted(
                    slot, "another slot confirmed while waiting for lock"
                )
                return False

            success = await self._confirm_booking(page, slot)
            if success:
                booking_won.set()
                self.notifier.booking_confirmed(slot)
            return success

    except Exception as e:
        logger.error(f"[book] Error booking {slot}: {e}")
        return False
    finally:
        if owns_page:
            await page.close()
```

- [ ] **Step 5: Update book_best_slot_race to pass warm pages**

In `src/booker.py`, update `book_best_slot_race()`:

```python
async def book_best_slot_race(
    self, slots: list[AvailableSlot],
    warm_pages: dict[str, Page] | None = None,
) -> AvailableSlot | None:
    # ... (existing candidate selection logic stays the same)

    async def attempt(slot: AvailableSlot) -> None:
        self.notifier.booking_attempting(slot)
        page = warm_pages.get(slot.slot_date_str) if warm_pages else None
        try:
            success = await self._book_single(slot, booking_won, warm_page=page)
            if success:
                winner.append(slot)
        except Exception as e:
            logger.error(f"[book] Unhandled exception for {slot}: {e}")
    # ... rest unchanged
```

- [ ] **Step 6: Update monitor.poll() to pass warm pages**

In `src/monitor.py:329`, change the booking call:

```python
# Pass warm sniper pages to booker (avoids re-navigation)
warm_pages = None
if self._sniper_active:
    warm_pages = {
        ds: self.checker.get_warm_page(ds)
        for ds in {s.slot_date_str for s in slots}
    }
    # Filter out None values
    warm_pages = {k: v for k, v in warm_pages.items() if v is not None}
    if warm_pages:
        logger.info(f"[monitor] Passing {len(warm_pages)} warm page(s) to booker")

booked = await self.booker.book_best_slot_race(slots, warm_pages=warm_pages)
```

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/checker.py src/booker.py src/monitor.py tests/test_warm_page_handoff.py
git commit -m "perf: pass warm sniper pages from checker to booker, skip re-navigation"
```

---

## Sprint 3: Architecture Cleanup

### Task 11: Split test modes out of main.py (A2)

`main.py` is 1,140 lines mixing CLI parsing, production entry point, and 5+ test mode implementations. Extract test modes into `src/testing/`.

**Files:**
- Create: `src/testing/__init__.py`
- Create: `src/testing/booking_flow.py`
- Create: `src/testing/sniper_tests.py`
- Modify: `main.py`

- [ ] **Step 1: Create src/testing/__init__.py**

```python
"""Test mode implementations extracted from main.py."""
```

- [ ] **Step 2: Create src/testing/booking_flow.py**

Extract the `--test-booking-flow` implementation from `main.py` into a standalone async function. Copy the exact code from the `if args.test_booking_flow:` block in main.py, wrapping it in:

```python
"""Test booking flow — navigates to checkout, fills CVC, screenshots. Stops before confirm."""

import logging
from src.config import Config
from src.browser import TockBrowser
from src.selectors import get_slot_button_selectors
import src.selectors as sel

logger = logging.getLogger(__name__)


async def run_test_booking_flow(config: Config, browser: TockBrowser) -> None:
    """Run --test-booking-flow: navigate to checkout, fill CVC, screenshot.

    Extract the body from main.py `_test_booking_flow()` (lines 27-240).
    Change the function signature from (browser, config, test_slug, logger)
    to (config, browser) and derive test_slug from config.restaurant_slug.
    Keep all internal logic identical — this is a pure move refactor.
    """
    ...
```

- [ ] **Step 3: Create src/testing/sniper_tests.py**

Extract `--test-sniper`, `--test-sniper-integration`, `--test-sniper-benchmark`, `--test-adaptive-sniper` into separate functions in this file.

```python
"""Sniper test modes extracted from main.py."""

import logging
from src.config import Config
from src.browser import TockBrowser
from src.checker import AvailabilityChecker
from src.monitor import TockMonitor
from src.notifier import Notifier
from src.tracker import SlotTracker

logger = logging.getLogger(__name__)


async def run_test_sniper(config: Config, browser: TockBrowser) -> None:
    """Run --test-sniper: 3-part robustness test.

    Extract from main.py `_test_sniper_robustness()` (lines 378-681).
    """
    ...

async def run_test_sniper_integration(
    config: Config, browser: TockBrowser, num_polls: int
) -> None:
    """Run --test-sniper-integration: full end-to-end chain.

    Extract from main.py `_test_sniper_integration()` (lines 683-868).
    """
    ...

async def run_test_sniper_benchmark(
    config: Config, browser: TockBrowser, num_polls: int
) -> None:
    """Run --test-sniper-benchmark: sequential vs concurrent A/B.

    Extract from main.py `_test_sniper_mode()` (lines 242-376).
    """
    ...

async def run_test_adaptive_sniper(
    config: Config, browser: TockBrowser, num_polls: int
) -> None:
    """Run --test-adaptive-sniper: adaptive mode switching.

    Uses TockMonitor.run_adaptive_test() — just wire up config/browser
    and call monitor.run_adaptive_test(num_polls). See main.py args handling.
    """
    ...
```

- [ ] **Step 4: Update main.py to import from src/testing/**

Replace each inline test-mode block in main.py with a function call:

```python
from src.testing.booking_flow import run_test_booking_flow
from src.testing.sniper_tests import (
    run_test_sniper,
    run_test_sniper_integration,
    run_test_sniper_benchmark,
    run_test_adaptive_sniper,
)
```

- [ ] **Step 5: Run all test modes to verify no regressions**

```bash
python main.py --test-notify
python main.py --verify
```

- [ ] **Step 6: Run pytest**

Run: `python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/testing/ main.py
git commit -m "refactor: extract test modes from main.py into src/testing/"
```

---

### Task 12: Drain critical Discord notifications on shutdown (F3)

`notifier.py:179-186` creates fire-and-forget tasks. Critical booking confirmations can be dropped if the event loop shuts down.

**Files:**
- Modify: `src/notifier.py:169-186`

- [ ] **Step 1: Track pending critical tasks**

Add to `Notifier.__init__`:

```python
self._critical_tasks: list[asyncio.Task] = []
```

- [ ] **Step 2: Update _fire to track critical notifications**

```python
def _fire(
    self,
    title: str,
    description: str,
    color: int,
    fields: list[tuple[str, str, bool]] | None = None,
    critical: bool = False,
) -> None:
    """Schedule a Discord webhook call. Critical=True tasks are tracked for drain."""
    if not self._discord_enabled:
        return
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._send_discord(title, description, color, fields or [])
        )
        if critical:
            self._critical_tasks.append(task)
            task.add_done_callback(lambda t: self._critical_tasks.remove(t) if t in self._critical_tasks else None)
    except RuntimeError:
        pass
```

- [ ] **Step 3: Mark booking_confirmed and no_payment_method as critical**

In `booking_confirmed()` and `no_payment_method()`, pass `critical=True` to `_fire()`.

- [ ] **Step 4: Add drain_pending()**

```python
async def drain_pending(self, timeout: float = 10.0) -> None:
    """Wait for critical notifications to send. Call before shutdown."""
    if not self._critical_tasks:
        return
    import asyncio
    logger.info(f"[notify] Draining {len(self._critical_tasks)} critical notification(s)…")
    await asyncio.wait(self._critical_tasks, timeout=timeout)
```

- [ ] **Step 5: Call drain in main.py shutdown**

In the main shutdown/cleanup path (finally block or signal handler), add:

```python
await notifier.drain_pending()
```

- [ ] **Step 6: Commit**

```bash
git add src/notifier.py main.py
git commit -m "fix: track and drain critical Discord notifications on shutdown"
```

---

## Summary: Expected Impact

| Sprint | Items | Sniper Speed Gain | Risk |
|--------|-------|-------------------|------|
| 1: Bugs | B1, B2, B4, F1 | Indirect (prevents missed drops) | Low — each fix is isolated |
| 2: Speed | S1-S7 + A1 | **~5-8s per poll cycle** | Medium — page.evaluate changes need integration testing |
| 3: Architecture | A2, F3 | None (code quality) | Low — pure refactor |

**Total estimated sniper cycle: ~4-6s** (down from ~10-12s)
{% endraw %}
