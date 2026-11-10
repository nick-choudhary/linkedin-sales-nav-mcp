"""Tests for what `capture_search` counts as a result and when it stops.

The fake page serves one search response per goto/Next click, with a chosen
number of results per page, and can fire other Sales API responses alongside
it the way the real search page does. Its Next control is enabled while pages
remain, can be made to never render, and its click can be made to fail.
"""

import pytest

import sales_nav_mcp.capture as capture
from sales_nav_mcp.capture import capture_search
from sales_nav_mcp.config import PacingConfig
from sales_nav_mcp.pacing import Pacer

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"
ACCOUNTS_URL = "https://www.linkedin.com/sales/search/company?query=(filters:List())"

LEAD_SEARCH_API = "https://www.linkedin.com/sales-api/salesApiLeadSearch"
ACCOUNT_SEARCH_API = "https://www.linkedin.com/sales-api/salesApiAccountSearch"
# The seat's subscription contract: loaded by the search page, and its
# elements carry a `name`, so they pass the generic record check.
CONTRACT_API = "https://www.linkedin.com/sales-api/salesApiContracts?q=x"
CONTRACT_BODY = {
    "elements": [
        {
            "name": "Sales Navigator Advanced",
            "agnosticIdentity": "urn:li:contract:1",
            "agnosticIdentityUnion": {"contract": "urn:li:contract:1"},
            "ssoEnabled": False,
        }
    ]
}


class _Response:
    def __init__(self, url: str, body: dict) -> None:
        self.url = url
        self.headers = {"content-type": "application/json"}
        self._body = body

    async def json(self) -> dict:
        return self._body


class _Mouse:
    async def wheel(self, dx: int, dy: int) -> None:
        pass


class _Locator:
    def __init__(self, page: "_Page", exists: bool) -> None:
        self._page = page
        self._exists = exists

    @property
    def first(self) -> "_Locator":
        return self

    async def count(self) -> int:
        return 1 if self._exists else 0

    async def is_enabled(self) -> bool:
        return self._page.served < len(self._page.page_sizes)

    async def scroll_into_view_if_needed(self, timeout: float | None = None) -> None:
        pass

    async def click(self, timeout: float | None = None) -> None:
        if self._page.click_fails:
            raise TimeoutError("click timed out")
        await self._page.deliver()


class _Page:
    def __init__(
        self,
        page_sizes: list[int],
        *,
        scraper_type: str = "contacts",
        reported_total: int | None = None,
        has_pagination: bool = True,
        extra_on_load: list[tuple[str, dict]] | None = None,
        click_fails: bool = False,
        overlap: int = 0,
        pagination_until: int | None = None,
    ) -> None:
        self.page_sizes = page_sizes
        self.scraper_type = scraper_type
        self.reported_total = (
            sum(page_sizes) if reported_total is None else reported_total
        )
        self.has_pagination = has_pagination
        self.extra_on_load = extra_on_load or []
        self.click_fails = click_fails
        # Each page after the first repeats this many results of the one before.
        self.overlap = overlap
        # The Next control stops rendering once this many pages are served.
        self.pagination_until = pagination_until
        self.served = 0
        self.mouse = _Mouse()
        self._handler = None

    def on(self, event: str, handler) -> None:
        self._handler = handler

    def remove_listener(self, event: str, handler) -> None:
        self._handler = None

    async def route(self, pattern: str, handler) -> None:
        pass

    async def unroute(self, pattern: str, handler) -> None:
        pass

    def _element(self, n: int) -> dict:
        if self.scraper_type == "contacts":
            return {
                "entityUrn": f"urn:li:fs_salesProfile:(P{n},NAME_SEARCH,x)",
                "fullName": f"Person {n}",
            }
        return {"entityUrn": f"urn:li:fs_salesCompany:{1000 + n}", "name": f"Co {n}"}

    async def deliver(self) -> None:
        if self.served >= len(self.page_sizes):
            return
        start = sum(self.page_sizes[: self.served])
        if self.served:
            start -= self.overlap
        size = self.page_sizes[self.served]
        body = {
            "elements": [self._element(start + n) for n in range(size)],
            "paging": {"total": self.reported_total, "count": 25, "start": start},
        }
        self.served += 1
        api = LEAD_SEARCH_API if self.scraper_type == "contacts" else ACCOUNT_SEARCH_API
        await self._handler(_Response(api, body))

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        for extra_url, extra_body in self.extra_on_load:
            await self._handler(_Response(extra_url, extra_body))
        await self.deliver()

    def locator(self, selector: str) -> _Locator:
        exists = (
            self.has_pagination
            and selector == 'button[aria-label="Next"]'
            and (self.pagination_until is None or self.served <= self.pagination_until)
        )
        return _Locator(self, exists)


@pytest.fixture(autouse=True)
def _fast_pagination_checks(monkeypatch):
    monkeypatch.setattr(capture, "NEXT_RENDER_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(capture, "NEXT_CONFIRM_SECONDS", 0.0)


def _pacer() -> Pacer:
    async def no_sleep(seconds: float) -> None:
        pass

    return Pacer(PacingConfig(enabled=False), sleep=no_sleep)


async def _run(page: _Page, url: str, pages: int, scraper_type: str = "contacts"):
    return await capture_search(page, url, scraper_type, pages, pacer=_pacer())


# --- What counts as a result ------------------------------------------------


async def test_contract_response_is_not_stored_as_an_account():
    page = _Page(
        [25], scraper_type="accounts", extra_on_load=[(CONTRACT_API, CONTRACT_BODY)]
    )

    result = await _run(page, ACCOUNTS_URL, 1, "accounts")

    assert result["count"] == 25
    assert all(
        r["entityUrn"].startswith("urn:li:fs_salesCompany:") for r in result["results"]
    )


async def test_contract_response_is_not_stored_as_a_lead():
    page = _Page([25], extra_on_load=[(CONTRACT_API, CONTRACT_BODY)])

    result = await _run(page, PEOPLE_URL, 1)

    assert result["count"] == 25
    assert all(
        r["entityUrn"].startswith("urn:li:fs_salesProfile:") for r in result["results"]
    )


async def test_results_of_the_other_entity_type_are_ignored():
    accounts_body = {
        "elements": [{"entityUrn": "urn:li:fs_salesCompany:7", "name": "Co"}]
    }
    page = _Page([25], extra_on_load=[(ACCOUNT_SEARCH_API, accounts_body)])

    result = await _run(page, PEOPLE_URL, 1)

    assert result["count"] == 25


# --- When a search ends -----------------------------------------------------


async def test_short_page_mid_search_does_not_end_it():
    # LinkedIn hides some profiles from a page, so 24 does not mean "last page".
    page = _Page([25, 24, 25, 7])

    result = await _run(page, PEOPLE_URL, 10)

    assert result["pages_fetched"] == 4
    assert result["count"] == 81
    assert result["exhausted"] is True


async def test_estimated_total_does_not_end_search():
    # The API reported 40; the search actually holds 55.
    page = _Page([25, 25, 5], reported_total=40)

    result = await _run(page, PEOPLE_URL, 10)

    assert result["pages_fetched"] == 3
    assert result["count"] == 55
    assert result["exhausted"] is True


async def test_last_page_within_budget_completes_by_disabled_next():
    page = _Page([25, 3])

    result = await _run(page, PEOPLE_URL, 5)

    assert result["pages_fetched"] == 2
    assert result["exhausted"] is True
    assert result["next_page"] is None


async def test_budget_ending_on_the_last_page_completes():
    page = _Page([25, 25])

    result = await _run(page, PEOPLE_URL, 2)

    assert result["exhausted"] is True
    assert result["next_page"] is None


async def test_budget_ending_with_next_enabled_pauses():
    page = _Page([25, 25, 25])

    result = await _run(page, PEOPLE_URL, 1)

    assert result["exhausted"] is False
    assert result["next_page"] == 2


async def test_short_budget_page_with_next_enabled_pauses():
    # A short page is not the end while LinkedIn still offers Next.
    page = _Page([24, 25])

    result = await _run(page, PEOPLE_URL, 1)

    assert result["exhausted"] is False
    assert result["next_page"] == 2


async def test_single_short_page_without_pagination_completes():
    # One page of results renders no pagination control at all.
    page = _Page([7], has_pagination=False)

    result = await _run(page, PEOPLE_URL, 1)

    assert result["exhausted"] is True


async def test_full_page_without_pagination_pauses_rather_than_guessing():
    # Pagination that never rendered says nothing; don't call the search done.
    page = _Page([25, 25], has_pagination=False)

    result = await _run(page, PEOPLE_URL, 1)

    assert result["exhausted"] is False
    assert result["next_page"] == 2


async def test_full_page_without_pagination_mid_budget_pauses():
    # Same as above, with budget left: the failed Next click is not the end.
    page = _Page([25, 25], has_pagination=False)

    result = await _run(page, PEOPLE_URL, 5)

    assert result["pages_fetched"] == 1
    assert result["exhausted"] is False
    assert result["next_page"] == 2


async def test_single_short_page_without_pagination_mid_budget_completes():
    page = _Page([7], has_pagination=False)

    result = await _run(page, PEOPLE_URL, 5)

    assert result["exhausted"] is True


async def test_failed_next_click_with_next_enabled_pauses():
    page = _Page([25, 25], click_fails=True)

    result = await _run(page, PEOPLE_URL, 5)

    assert result["pages_fetched"] == 1
    assert result["exhausted"] is False
    assert result["next_page"] == 2


async def test_full_page_repeating_an_earlier_result_counts_as_full():
    # Page 2 repeats one lead from page 1, then pagination never renders.
    # Its 25 results, not its 24 new ones, decide whether it is the last page.
    page = _Page([25, 25, 25], overlap=1, pagination_until=1)

    result = await _run(page, PEOPLE_URL, 2)

    assert result["count"] == 49
    assert result["exhausted"] is False
    assert result["next_page"] == 3
