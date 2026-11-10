"""Navigate Sales Navigator and capture its search API responses.

The technique, end to end:

1. Attach a `response` listener to the live page BEFORE navigating, so we
   catch the search request the page fires on load.
2. `goto` the Sales Navigator search URL (with a `page=` offset when resuming).
   The browser makes its own authenticated API call; we read the JSON off the
   wire.
3. After each page, hand its records to an `on_page` callback so the caller can
   commit them immediately — that per-page commit is what makes a crash
   resumable instead of losing the whole run.
4. Dwell for a jittered interval, click "Next" to advance, and repeat. The
   pacing lives in `pacing.Pacer`; a fixed page-every-two-seconds cadence is a
   signature in its own right, so the gaps vary and every few pages there is a
   longer break.

Nothing is replayed or reconstructed — we only read what the genuine session
already requested. That is what keeps LinkedIn from flagging it.

Resume honesty: LinkedIn re-ranks results between runs, and the `page=` offset
is a best-effort jump, so resuming can overlap or drift slightly. Dedupe (here
by URN, and again in the store) absorbs the overlap, so you never get
duplicates — but resume is fail-*safe*, not byte-identical.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from sales_nav_mcp.config import get_config
from sales_nav_mcp.exceptions import CaptureTimeoutError, UrlValidationError
from sales_nav_mcp.normalize import find_paging, find_search_elements, normalize
from sales_nav_mcp.pacing import Pacer

logger = logging.getLogger(__name__)

ScraperType = Literal["contacts", "accounts"]
PAGE_SIZE = 25

# Sales Navigator's web client asks lead search for decoration
# `com.linkedin.sales.deco.desktop.searchv2.LeadSearchResult-14`. The server
# also serves -13, -15 and -16 (-17 does not exist), and -16 is a strict
# superset of -14: identical fields plus `seniorityV2s`, for roughly 230 extra
# bytes per lead. We rewrite the request to ask for it, because the alternative
# -- inferring seniority by regexing job titles -- is worse than LinkedIn's own
# classification.
#
# This is the only place we alter what the browser asks for. The rewrite is a
# no-op unless the URL actually carries a LeadSearchResult decoration id, so if
# LinkedIn ever renames the parameter the request goes out untouched.
LEAD_SEARCH_DECORATION_ID = 16
_DECORATION_RE = re.compile(r"(LeadSearchResult-)(\d+)")
_LEAD_SEARCH_ROUTE = "**/sales-api/salesApiLeadSearch*"

# on_page(records, page_number, paging) -> awaitable
OnPage = Callable[[list[dict[str, Any]], int, dict[str, Any] | None], Awaitable[None]]

_VALID_HOSTS = {
    "linkedin.com",
    "www.linkedin.com",
    "uk.linkedin.com",
    "de.linkedin.com",
    "fr.linkedin.com",
    "ca.linkedin.com",
    "au.linkedin.com",
    "in.linkedin.com",
    "br.linkedin.com",
    "es.linkedin.com",
    "it.linkedin.com",
    "nl.linkedin.com",
}
_CONTACT_PATHS = ("/sales/search/people", "/sales/search/leads", "/sales/lists/people")
_ACCOUNT_PATHS = (
    "/sales/search/accounts",
    "/sales/search/company",
    "/sales/lists/accounts",
)

_SEARCH_URL_MARKERS = (
    "salesapileadsearch",
    "salesapipeoplesearch",
    "salesapiaccountsearch",
    "salesapicompanysearch",
    "salesapisearch",
    "leadsearch",
    "accountsearch",
)
_GENERIC_MARKER = "/sales-api/"

# A search result carries the entity URN of the type searched. The search page
# also loads other Sales API responses -- the seat's subscription contract, the
# filter layout -- whose lists of named objects pass the generic record check
# in `find_search_elements`; a contract element was once stored as an account.
_RESULT_URN_PREFIX: dict[str, str] = {
    "contacts": "urn:li:fs_salesProfile:",
    "accounts": "urn:li:fs_salesCompany:",
}

_NEXT_SELECTORS = (
    'button[aria-label="Next"]',
    "button.artdeco-pagination__button--next",
    ".search-results__pagination-next-button",
    'button[aria-label="Next page"]',
)

# The pagination control, not the page length or LinkedIn's result count,
# decides when a search has ended. A page comes back short mid-search when
# LinkedIn hides profiles from it, and `paging.total` is an estimate: one
# account search reported 836 and held 855.
#
# When a call's page budget runs out, the Next control is read without being
# clicked. It renders after the results, so allow it a moment to appear, and
# re-read a disabled one once before trusting it.
NEXT_RENDER_WAIT_SECONDS = 6.0
NEXT_CONFIRM_SECONDS = 1.0


def validate_sales_nav_url(url: str, scraper_type: ScraperType) -> None:
    """Reject non–Sales Navigator URLs with a message naming the fix."""
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise UrlValidationError(f"Not a valid URL: {url!r}") from e
    if parsed.scheme not in ("http", "https"):
        raise UrlValidationError(
            "URL must be a full https:// Sales Navigator URL copied from the "
            "browser address bar, not a bare query."
        )
    if parsed.netloc not in _VALID_HOSTS:
        raise UrlValidationError(
            f"URL host '{parsed.netloc}' is not a LinkedIn domain."
        )
    expected = _CONTACT_PATHS if scraper_type == "contacts" else _ACCOUNT_PATHS
    other = _ACCOUNT_PATHS if scraper_type == "contacts" else _CONTACT_PATHS
    if any(parsed.path.startswith(p) for p in expected):
        return
    if any(parsed.path.startswith(p) for p in other):
        right = "search_accounts" if scraper_type == "contacts" else "search_contacts"
        raise UrlValidationError(
            f"That URL is for the other entity type; use the {right} tool."
        )
    raise UrlValidationError(
        "URL path must be a Sales Navigator search or list URL. Accepted for "
        f"this tool: {', '.join(expected)}. Got: {parsed.path}"
    )


def _upgrade_decoration_id(url: str) -> str:
    """Point a lead-search request at the richer decoration id."""
    return _DECORATION_RE.sub(
        lambda m: f"{m.group(1)}{LEAD_SEARCH_DECORATION_ID}", url, count=1
    )


async def _route_decoration_upgrade(route: Any) -> None:
    """Rewrite lead-search requests to the richer decoration id, then let
    them through untouched in every other respect."""
    url = route.request.url
    upgraded = _upgrade_decoration_id(url)
    if upgraded != url:
        await route.continue_(url=upgraded)
    else:
        await route.continue_()


def _with_page_param(url: str, page: int) -> str:
    """Return *url* with its `page` query param set (or removed for page 1)."""
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "page"
    ]
    if page and page > 1:
        query.append(("page", str(page)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _result_elements(payload: Any, scraper_type: ScraperType) -> list[dict[str, Any]]:
    """The search results in *payload* that are of the searched entity type."""
    prefix = _RESULT_URN_PREFIX[scraper_type]
    return [
        e
        for e in find_search_elements(payload)
        if str(e.get("entityUrn") or "").startswith(prefix)
    ]


class _SearchCapture:
    """Collects search API payloads seen on the page, with a per-page mark."""

    def __init__(self, scraper_type: ScraperType) -> None:
        self._scraper_type = scraper_type
        self._payloads: list[dict[str, Any]] = []
        self._mark_index = 0

    async def on_response(self, response: Any) -> None:
        url = (response.url or "").lower()
        if not (_GENERIC_MARKER in url or any(m in url for m in _SEARCH_URL_MARKERS)):
            return
        if "application/json" not in (response.headers or {}).get("content-type", ""):
            return
        try:
            body = await response.json()
        except Exception:
            return
        # Only a payload holding results counts: a contract or filter-layout
        # response arriving first must not stand in for the page's results.
        if _result_elements(body, self._scraper_type):
            self._payloads.append(body)

    def mark(self) -> None:
        self._mark_index = len(self._payloads)

    @property
    def new_since_mark(self) -> int:
        return len(self._payloads) - self._mark_index

    def drain_since_mark(self) -> list[dict[str, Any]]:
        return self._payloads[self._mark_index :]

    @property
    def payloads(self) -> list[dict[str, Any]]:
        """Every raw search payload captured this call (for schema discovery)."""
        return self._payloads


async def _wait_for_new_payload(
    capture: _SearchCapture, timeout: float, *, require: bool
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if capture.new_since_mark > 0:
            return True
        await asyncio.sleep(0.4)
    if require:
        raise CaptureTimeoutError(
            "No Sales Navigator search response was captured within "
            f"{timeout:.0f}s. The search may be empty, the page may not have "
            "loaded, or LinkedIn changed its search endpoint."
        )
    return False


async def _settle_scroll(page: Any, pacer: Pacer) -> None:
    """Scroll to the bottom so LinkedIn finishes rendering, at a varied rhythm."""
    for pixels, gap in pacer.scroll_plan():
        try:
            await page.mouse.wheel(0, pixels)
        except Exception:
            break
        await pacer.pause(gap)


async def _click_next(page: Any) -> bool:
    for selector in _NEXT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            if not await locator.is_enabled():
                return False
            await locator.scroll_into_view_if_needed(timeout=3000)
            await locator.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


async def _next_state(page: Any) -> bool | None:
    """Read the Next control without clicking it.

    True if it is enabled, False if it is disabled, None if none has rendered
    within NEXT_RENDER_WAIT_SECONDS.
    """
    deadline = time.monotonic() + NEXT_RENDER_WAIT_SECONDS
    while True:
        for selector in _NEXT_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                return await locator.is_enabled()
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.4)


async def _is_last_page(page: Any, records_on_page: int) -> bool:
    """Decide, at the end of a call's page budget, whether results are done.

    A disabled Next means done; an enabled one means more remain. With no
    control at all, a short page is taken as the only page (a single page of
    results renders no pagination), and a full one as unknown, so the search
    pauses rather than being marked complete.
    """
    state = await _next_state(page)
    if state is False:
        await asyncio.sleep(NEXT_CONFIRM_SECONDS)
        state = await _next_state(page)
    if state is None:
        return records_on_page < PAGE_SIZE
    return not state


def _collect(
    payloads: list[dict[str, Any]],
    seen: set[str],
    scraper_type: ScraperType,
    include_raw: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    elements: list[dict[str, Any]] = []
    paging: dict[str, Any] | None = None
    for payload in payloads:
        paging = find_paging(payload) or paging
        for element in _result_elements(payload, scraper_type):
            key = str(
                element.get("entityUrn") or element.get("objectUrn") or id(element)
            )
            if key in seen:
                continue
            seen.add(key)
            elements.append(element)
    # _raw is always kept here so the store persists the complete element;
    # the tool-level include_raw flag only governs the raw-payload dump.
    return normalize(elements, scraper_type, include_raw=True), paging


async def capture_search(
    page: Any,
    url: str,
    scraper_type: ScraperType,
    pages: int,
    *,
    start_page: int = 1,
    on_page: OnPage | None = None,
    include_raw: bool = False,
    pacer: Pacer | None = None,
) -> dict[str, Any]:
    """Drive the browser through up to *pages* result pages from *start_page*.

    Records for each page are delivered to *on_page* (for incremental commit)
    and also accumulated in the return value. Returns a summary describing how
    far it got and whether more pages remain.

    Pages are fetched at a human-like cadence (see `pacing.Pacer`); pass a
    *pacer* to override the configured delays, mainly for tests.
    """
    config = get_config().browser
    pacer = pacer if pacer is not None else Pacer()
    capture = _SearchCapture(scraper_type)
    page.on("response", capture.on_response)
    routed = False
    if scraper_type == "contacts":
        await page.route(_LEAD_SEARCH_ROUTE, _route_decoration_upgrade)
        routed = True

    seen: set[str] = set()
    all_records: list[dict[str, Any]] = []
    paging: dict[str, Any] | None = None
    pages_fetched = 0
    last_count = 0
    exhausted = False

    try:
        for i in range(pages):
            current_page = start_page + i
            # Dwell before marking, not after: the mark starts the window in
            # which a payload counts as "this page's", and it must stay tight.
            if i > 0:
                await pacer.dwell()
            capture.mark()
            if i == 0:
                await page.goto(
                    _with_page_param(url, start_page),
                    wait_until="domcontentloaded",
                )
            elif not await _click_next(page):
                # A click can fail on a slow render or a flaky click, not only
                # on the last page; read the control before calling it done.
                exhausted = await _is_last_page(page, last_count)
                logger.info("Next click failed; exhausted=%s.", exhausted)
                break

            await _settle_scroll(page, pacer)
            got = await _wait_for_new_payload(
                capture, config.capture_wait_seconds, require=(i == 0)
            )
            if not got:
                logger.info("Page %d produced no new results; stopping.", current_page)
                exhausted = True
                break

            page_records, page_paging = _collect(
                capture.drain_since_mark(), seen, scraper_type, include_raw
            )
            paging = page_paging or paging
            pages_fetched += 1
            if on_page is not None:
                await on_page(page_records, current_page, paging)
            all_records.extend(page_records)
            last_count = len(page_records)

            # Mid-budget, the next iteration's Next click finds the end. On
            # the last page of the budget, read the control instead, so a
            # finished search completes rather than pausing and the next call
            # does not jump past the end.
            if i == pages - 1:
                exhausted = await _is_last_page(page, last_count)
    finally:
        page.remove_listener("response", capture.on_response)
        if routed:
            with contextlib.suppress(Exception):
                await page.unroute(_LEAD_SEARCH_ROUTE, _route_decoration_upgrade)

    last_page = start_page + pages_fetched - 1 if pages_fetched else start_page - 1
    return {
        "results": all_records,
        "count": len(all_records),
        "paging": paging,
        "pages_fetched": pages_fetched,
        "first_page": start_page,
        "last_page": last_page,
        "exhausted": exhausted,
        "next_page": None if exhausted else last_page + 1,
        # Complete, untouched API responses — only when explicitly asked, so
        # normal runs don't carry the payload around. Used for schema discovery.
        "raw_payloads": capture.payloads if include_raw else None,
    }
