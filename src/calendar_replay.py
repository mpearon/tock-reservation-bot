"""Phase B3.2 fast-path — SPA-header replay for sub-second slot detection.

Replaces the per-date page.reload + DOM polling cycle (12.4s for 6 dates
concurrent on benu) with a single in-browser fetch() per poll (~159ms
empirical).

How it works:
  1. ONCE per restaurant: navigate a Playwright page to the restaurant's
     search URL. The Tock SPA fires `POST /api/consumer/calendar/full/v2`
     with auth headers (JWT, session ID, fingerprint, business scope).
  2. Capture those headers from the first request via
     `page.expect_request`.
  3. Each subsequent poll: run `page.evaluate("await fetch(...)")` from
     inside that same page, presenting the captured headers + the same
     4-byte protobuf body.
  4. The browser supplies cookies + TLS fingerprint automatically, so
     Cloudflare allows the request. Tock allows it because we present
     its own auth headers.
  5. Response body is protobuf-encoded but date/time strings are stored
     as plain ASCII inside the binary frame — regex-parse them out.

Default OFF behind `USE_CALENDAR_REPLAY` config flag. When enabled,
`AvailabilityChecker.check_all` short-circuits the normal per-date
navigate-and-scan loop and uses the replay path instead. On any
failure (401/403, parse failure, network error), falls back to the
existing path.

Empirical benchmarks (benu, 2026-05-10):
  Current concurrent cycle (6 dates):    12.4s avg, σ 0.7s
  Calendar-replay cycle (any N dates):   0.16s avg, σ 0.01s
  Speedup:                                78×
  End-to-end booking projection:         ~3-5s (was ~17s)
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page
    from src.checker import AvailableSlot

logger = logging.getLogger(__name__)


# Tock's calendar/full URL — same path for all restaurants
_CALENDAR_URL_FRAGMENT = "consumer/calendar/full"

# Date marker pattern in the protobuf body (plain ASCII inside binary frame)
_DATE_RE = re.compile(rb"20\d{2}-\d{2}-\d{2}")

# Codex review HIGH 2: detect non-protobuf 200 responses (CF interstitial,
# auth-stripped empty body) so the caller can fall back to legacy DOM
# polling instead of treating the empty parse as "no slots available".
_HTML_SIGNATURE_BYTES = (b"<!doctype", b"<html", b"<HTML", b"<HEAD", b"<head")

# Below this byte threshold, a 200 response is too small to plausibly be a
# real calendar with availability AND too small to be a CF interstitial —
# safer to treat as failure (fall back) than as "no slots success".
_PROTOBUF_MIN_PLAUSIBLE_BYTES = 20

# Per-date section heuristics (empirical, 2026-05-10):
# benu (has slots):     each date section is 1000-1500 bytes, 11-17 times
# fuhuihua (sold out):  each date section is 16-322 bytes, 0-2 times
# A "real bookable date" section is large because it contains slot
# metadata (party limits, prices, availability counts) per slot. A
# "sold out" date section is just the date marker + a few framing bytes.
# Filter date sections that look too small to plausibly contain real
# slot metadata, AND require at least N time strings — together this
# rejects fuhuihua's ghost slots while keeping all benu's real ones.
_MIN_BOOKABLE_SECTION_BYTES = 800
_MIN_BOOKABLE_TIMES_PER_SECTION = 5

# Time marker pattern (HH:MM, 24-hour). Note: the body has many "00:00"
# style framing artifacts (zero offsets in the protobuf wire format) so
# we filter to plausible booking times (10:00–23:59).
_TIME_RE = re.compile(rb"\b([0-2]\d):([0-5]\d)\b")
_MIN_BOOKING_HOUR = 10
_MAX_BOOKING_HOUR = 23


@dataclass
class ReplayParseDiag:
    """Diagnostics from one parse of a calendar/full body.

    We are otherwise BLIND to why the replay path returns 0 slots for
    fuhuihua (the ghost-slot guards silently filter sections). These
    counts let `check_all` log, at INFO, exactly how many date sections
    the size/time guards rejected so the thresholds can be re-tuned from
    real captures instead of guessed.
    """
    body_len: int = 0
    date_hits: int = 0          # total date-string regex matches in body
    unique_dates: int = 0       # distinct date markers
    sections_passed: int = 0    # date sections that cleared BOTH guards
    sections_filtered: int = 0  # date sections rejected by a guard


@dataclass
class CalendarReplaySession:
    """One restaurant's captured replay context.

    Lifecycle:
      - Created once per restaurant per sniper window (or once at
        bot startup, refreshed on auth failure).
      - Holds a Playwright Page + the captured request URL/headers/body.
      - `fetch_calendar()` is called per-poll to refresh availability
        without page.reload.
    """
    page: "Page"
    url: str
    headers: dict[str, str]
    body_bytes: bytes
    restaurant_slug: str
    # Telemetry
    fetch_count: int = 0
    last_fetch_ms: float = 0.0
    consecutive_failures: int = 0


async def initialize_replay_session(
    browser, restaurant_slug: str, target_date: date, party_size: int = 2,
    preferred_time: str = "17:00", timeout_ms: int = 15_000,
) -> CalendarReplaySession | None:
    """Open a page, navigate to the restaurant's search URL, capture
    the calendar/full request that the SPA fires natively. Returns a
    `CalendarReplaySession` ready for `fetch_calendar`, or None on
    failure (CF challenge, login expired, no calendar XHR within timeout).

    The captured page is kept alive — caller is responsible for closing
    it (typically via the Browser's lifecycle).
    """
    url = (
        f"https://www.exploretock.com/{restaurant_slug}/search"
        f"?date={target_date.isoformat()}"
        f"&size={party_size}"
        f"&time={preferred_time}"
    )
    page = await browser.new_page()
    try:
        async with page.expect_request(
            lambda r: _CALENDAR_URL_FRAGMENT in r.url and r.method == "POST",
            timeout=timeout_ms,
        ) as req_info:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 2)
        request = await req_info.value
        captured_headers = dict(request.headers)
        captured_body = request.post_data_buffer or b""
        captured_url = request.url
    except Exception as e:
        logger.warning(
            f"[calendar-replay] init failed for {restaurant_slug}: "
            f"{type(e).__name__}: {e}"
        )
        try:
            await page.close()
        except Exception:
            pass
        return None

    # Drop fetch-metadata headers that Playwright captures but aren't
    # safe to replay verbatim
    for drop in [k for k in captured_headers if k.startswith("sec-fetch-")]:
        del captured_headers[drop]

    logger.info(
        f"[calendar-replay] {restaurant_slug}: captured headers + "
        f"{len(captured_body)}-byte body from {captured_url}"
    )
    return CalendarReplaySession(
        page=page,
        url=captured_url,
        headers=captured_headers,
        body_bytes=captured_body,
        restaurant_slug=restaurant_slug,
    )


def body_looks_protobuf(body: bytes) -> bool:
    """Return True if `body` plausibly looks like a Tock calendar/full
    protobuf response (Codex review HIGH 2 fix).

    Returns False for:
      - HTML bodies (CF interstitial, error pages)
      - bodies smaller than _PROTOBUF_MIN_PLAUSIBLE_BYTES
      - empty bodies

    True signals "ok to parse"; False signals "fall back to legacy".
    """
    if not body or len(body) < _PROTOBUF_MIN_PLAUSIBLE_BYTES:
        return False
    head = body[:60].lower()
    if any(sig in head for sig in (b"<!doctype", b"<html", b"<head")):
        return False
    return True


async def fetch_calendar(session: CalendarReplaySession) -> bytes | None:
    """Fire one fetch() from inside the captured page using replayed
    headers. Returns the protobuf body bytes on success, or None on
    HTTP failure / CF block. Caller decides whether to fall back to
    DOM polling on None."""
    body_b64 = base64.b64encode(session.body_bytes).decode("ascii")
    try:
        result = await session.page.evaluate(
            """
            async ({ url, headers, postB64 }) => {
                const t0 = performance.now();
                const bodyBytes = Uint8Array.from(atob(postB64), c => c.charCodeAt(0));
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: headers,
                    body: bodyBytes,
                    credentials: 'include',
                });
                const buf = await resp.arrayBuffer();
                const elapsed = performance.now() - t0;
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let j = 0; j < bytes.length; j++) bin += String.fromCharCode(bytes[j]);
                return {
                    status: resp.status,
                    body_b64: btoa(bin),
                    body_len: bytes.length,
                    elapsed_ms: Math.round(elapsed),
                };
            }
            """,
            {"url": session.url, "headers": session.headers, "postB64": body_b64},
        )
    except Exception as e:
        session.consecutive_failures += 1
        logger.warning(
            f"[calendar-replay] {session.restaurant_slug}: "
            f"page.evaluate raised: {type(e).__name__}: {e}"
        )
        return None

    session.fetch_count += 1
    session.last_fetch_ms = result.get("elapsed_ms", 0)
    if result.get("status") != 200 or not result.get("body_len"):
        session.consecutive_failures += 1
        logger.warning(
            f"[calendar-replay] {session.restaurant_slug}: "
            f"non-200 / empty response — status={result.get('status')} "
            f"body_len={result.get('body_len')} (likely auth-expired; "
            "caller should re-initialize the session)"
        )
        return None

    body = base64.b64decode(result["body_b64"])
    # Codex HIGH 2: detect HTML / too-small responses so the caller
    # falls back to DOM polling instead of treating empty parse as
    # "no slots available" — that would suppress a real release.
    if not body_looks_protobuf(body):
        session.consecutive_failures += 1
        head_repr = body[:80].decode("utf-8", errors="replace")
        logger.warning(
            f"[calendar-replay] {session.restaurant_slug}: 200 OK but body "
            f"does not look protobuf ({len(body)}b, starts: {head_repr!r}). "
            "Likely CF interstitial / Tock schema change. Treating as failure."
        )
        return None

    session.consecutive_failures = 0
    logger.debug(
        f"[calendar-replay] {session.restaurant_slug}: "
        f"fetched {len(body)}b in {session.last_fetch_ms}ms"
    )
    return body


def parse_available_slots(
    body: bytes, target_dates: list[date],
) -> list["AvailableSlot"]:
    """Extract (date, time) pairs from the calendar/full protobuf body.

    Thin wrapper over `parse_with_diagnostics` (which carries the full
    algorithm). Returns AvailableSlot list (deduplicated, ordered by date
    then time). Kept as the stable public entry point — most callers don't
    need the diagnostics.
    """
    slots, _diag = parse_with_diagnostics(body, target_dates)
    return slots


def parse_with_diagnostics(
    body: bytes, target_dates: list[date],
) -> "tuple[list[AvailableSlot], ReplayParseDiag]":
    """Like `parse_available_slots`, but ALSO returns a `ReplayParseDiag`
    counting body length, date-string hits, and how many date sections
    passed vs were filtered by the ghost-slot guards.

    Strategy:
      - The body is protobuf-encoded but date/time strings are stored
        as plain ASCII inside the binary frame.
      - Each available date appears in the body 1+ times, followed by
        the available time slots for that date, until the next date
        boundary.
      - We scan the body linearly: for each date we find, collect
        unique times that appear BEFORE the next date's first byte.
      - Filter to `target_dates` so the caller only gets back dates
        they care about.
      - Filter times to plausible booking hours (10:00–23:59) since
        the protobuf body has framing bytes that look like 00:00,
        01:23, etc.

    The diagnostics exist because the size/time guards below were
    calibrated on benu (high-volume) and silently drop a real fuhuihua
    release (few seatings → sub-threshold section). Surfacing the
    passed/filtered counts lets `check_all` log them so we can re-tune
    from real captures instead of flying blind.
    """
    from src.checker import AvailableSlot

    target_iso = {d.isoformat(): d for d in target_dates}
    diag = ReplayParseDiag(body_len=len(body))

    # Find every date occurrence with its byte offset
    date_hits: list[tuple[int, str]] = []
    for m in _DATE_RE.finditer(body):
        date_hits.append((m.start(), m.group(0).decode()))
    diag.date_hits = len(date_hits)
    if not date_hits:
        return [], diag

    # For each unique date, find the contiguous range from its FIRST
    # occurrence to the FIRST occurrence of the next unique date.
    # Within that range, collect plausible booking times.
    seen_dates = []
    first_offset_per_date: dict[str, int] = {}
    for off, d in date_hits:
        if d not in first_offset_per_date:
            first_offset_per_date[d] = off
            seen_dates.append(d)
    diag.unique_dates = len(seen_dates)

    slots_by_date: dict[str, set[str]] = {}
    for i, d in enumerate(seen_dates):
        start = first_offset_per_date[d]
        end = (
            first_offset_per_date[seen_dates[i + 1]]
            if i + 1 < len(seen_dates)
            else len(body)
        )
        section = body[start:end]

        # Ghost-slot guard (empirical 2026-05-10): real bookable date
        # sections are >= 800 bytes (contain slot metadata: party
        # limits, prices, availability counts per slot). Sold-out
        # restaurants like fuhuihua have date markers in the body but
        # only ~20-300 bytes per date section because there's no slot
        # metadata to encode. Skip these to avoid attributing protobuf
        # framing bytes that happen to look like times to a date the
        # restaurant has 0 availability for.
        if len(section) < _MIN_BOOKABLE_SECTION_BYTES:
            diag.sections_filtered += 1
            logger.debug(
                f"[calendar-replay] skipping date {d}: "
                f"section {len(section)}b < {_MIN_BOOKABLE_SECTION_BYTES}b "
                "threshold (likely sold out / no real slot metadata)"
            )
            continue

        times: set[str] = set()
        for tm in _TIME_RE.finditer(section):
            hh = int(tm.group(1))
            mm = int(tm.group(2))
            if _MIN_BOOKING_HOUR <= hh <= _MAX_BOOKING_HOUR:
                # Format as "H:MM AM/PM" to match the bot's slot_time format
                times.add(_format_24h_to_12h(hh, mm))
        if len(times) < _MIN_BOOKABLE_TIMES_PER_SECTION:
            diag.sections_filtered += 1
            logger.debug(
                f"[calendar-replay] skipping date {d}: "
                f"only {len(times)} time(s) found "
                f"< {_MIN_BOOKABLE_TIMES_PER_SECTION} threshold (likely "
                "ghost slots from protobuf framing)"
            )
            continue
        diag.sections_passed += 1
        if times:
            slots_by_date[d] = times

    # Build AvailableSlot list filtered to target dates
    result: list[AvailableSlot] = []
    for date_iso in sorted(slots_by_date.keys()):
        if date_iso not in target_iso:
            continue
        d = target_iso[date_iso]
        for t in sorted(slots_by_date[date_iso], key=_time_sort_key):
            result.append(
                AvailableSlot(
                    slot_date=d,
                    slot_time=t,
                    day_of_week=d.strftime("%A"),
                )
            )
    return result, diag


def cap_slots_per_date(
    slots: list["AvailableSlot"], preferred_time: str = "17:00",
    per_date_cap: int = 3,
) -> list["AvailableSlot"]:
    """Codex HIGH 1 fix: cap replay output to top K slots per date,
    sorted by closeness to preferred_time. Avoids the booker fanning
    out into 100+ concurrent booking pages on calendar/full responses
    that include the whole 14-day calendar.

    Empirical: replay returns 145 slots / cycle on benu (10× more than
    the per-date DOM scan). Without this cap, book_best_slot_race
    would race every single slot — Tock ban risk.

    Sort key per date: minutes-distance from preferred_time. Returns
    a flat list ordered by (date, distance-from-preferred).
    """
    # Parse preferred_time once
    try:
        pref_h, pref_m = preferred_time.split(":")
        pref_minutes = int(pref_h) * 60 + int(pref_m)
    except Exception:
        pref_minutes = 17 * 60  # 5pm fallback

    def _slot_minutes(s: "AvailableSlot") -> int:
        m = re.match(r"(\d{1,2}):(\d{2}) (AM|PM)", s.slot_time)
        if not m:
            return 9999
        hh = int(m.group(1)) % 12
        mm = int(m.group(2))
        if m.group(3) == "PM":
            hh += 12
        return hh * 60 + mm

    # Group by date, sort each group by distance from preferred, keep top K
    from collections import defaultdict
    by_date: dict[str, list] = defaultdict(list)
    for s in slots:
        by_date[s.slot_date_str].append(s)
    out: list = []
    for date_str in sorted(by_date.keys()):
        ranked = sorted(
            by_date[date_str],
            key=lambda s: abs(_slot_minutes(s) - pref_minutes),
        )
        out.extend(ranked[:per_date_cap])
    return out


def _format_24h_to_12h(hh: int, mm: int) -> str:
    """Format 24h time as 12h AM/PM string matching bot conventions
    (e.g. (17, 30) → "5:30 PM", (10, 0) → "10:00 AM")."""
    period = "PM" if hh >= 12 else "AM"
    hh12 = hh % 12
    if hh12 == 0:
        hh12 = 12
    return f"{hh12}:{mm:02d} {period}"


def _time_sort_key(time_str: str) -> tuple[int, int]:
    """Sort key for "H:MM AM/PM" strings."""
    m = re.match(r"(\d{1,2}):(\d{2}) (AM|PM)", time_str)
    if not m:
        return (99, 99)
    hh = int(m.group(1)) % 12
    mm = int(m.group(2))
    if m.group(3) == "PM":
        hh += 12
    return (hh, mm)


async def close_session(session: CalendarReplaySession | None) -> None:
    """Close the session's page (best-effort, never raises)."""
    if session is None:
        return
    try:
        if not session.page.is_closed():
            await session.page.close()
    except Exception:
        pass
