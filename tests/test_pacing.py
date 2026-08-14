"""Tests for human-like pacing.

Two layers: the Pacer's own arithmetic (bounds, long-pause schedule, disable
switch), and the wiring in `capture_search` — that delays actually happen
between pages and never before the first one. Both inject a recording sleep, so
the suite exercises the real delays without waiting for them.
"""

import random

import pytest

from sales_nav_mcp.capture import capture_search
from sales_nav_mcp.config import ConfigurationError, PacingConfig
from sales_nav_mcp.pacing import Pacer

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"


class RecordingSleeper:
    """Stands in for `asyncio.sleep`, recording instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


def make_pacer(config: PacingConfig | None = None, seed: int = 0) -> tuple[Pacer, RecordingSleeper]:
    sleeper = RecordingSleeper()
    pacer = Pacer(
        config or PacingConfig(),
        rng=random.Random(seed),
        sleep=sleeper,
    )
    return pacer, sleeper


# --- Pacer arithmetic -------------------------------------------------------


@pytest.mark.asyncio
async def test_dwell_stays_within_configured_bounds():
    config = PacingConfig(page_delay_min=3.0, page_delay_max=8.0, long_pause_every=0)
    pacer, sleeper = make_pacer(config)

    for _ in range(50):
        await pacer.dwell()

    assert len(sleeper.calls) == 50
    assert all(3.0 <= s <= 8.0 for s in sleeper.calls)


@pytest.mark.asyncio
async def test_dwell_is_jittered_not_constant():
    """The whole point: consecutive gaps must not be identical."""
    config = PacingConfig(long_pause_every=0)
    pacer, sleeper = make_pacer(config)

    for _ in range(20):
        await pacer.dwell()

    assert len(set(sleeper.calls)) > 1


@pytest.mark.asyncio
async def test_long_pause_fires_on_a_jittered_schedule():
    config = PacingConfig(
        page_delay_min=1.0,
        page_delay_max=2.0,
        long_pause_every=5,
        long_pause_min=20.0,
        long_pause_max=45.0,
    )
    pacer, sleeper = make_pacer(config)

    for _ in range(40):
        await pacer.dwell()

    longs = [s for s in sleeper.calls if s >= 20.0]
    shorts = [s for s in sleeper.calls if s < 20.0]
    # Roughly every 5th page (interval jittered 4-6), so 40 pages gives 6-10.
    assert 6 <= len(longs) <= 10
    assert all(20.0 <= s <= 45.0 for s in longs)
    assert all(1.0 <= s <= 2.0 for s in shorts)


@pytest.mark.asyncio
async def test_long_pause_interval_is_not_perfectly_periodic():
    """A break at exactly every Nth page would itself be a detectable pattern."""
    config = PacingConfig(page_delay_min=1.0, page_delay_max=1.0, long_pause_every=5)
    pacer, sleeper = make_pacer(config)

    for _ in range(60):
        await pacer.dwell()

    gaps = []
    last = None
    for i, seconds in enumerate(sleeper.calls):
        if seconds >= 20.0:
            if last is not None:
                gaps.append(i - last)
            last = i
    assert len(set(gaps)) > 1, f"long pauses landed on a fixed period: {gaps}"


@pytest.mark.asyncio
async def test_long_pause_every_zero_disables_breaks():
    config = PacingConfig(long_pause_every=0)
    pacer, sleeper = make_pacer(config)

    for _ in range(30):
        await pacer.dwell()

    assert all(s <= config.page_delay_max for s in sleeper.calls)


@pytest.mark.asyncio
async def test_disabled_pacing_sleeps_not_at_all():
    pacer, sleeper = make_pacer(PacingConfig(enabled=False))

    slept = await pacer.dwell()

    assert slept == 0.0
    assert sleeper.calls == []


def test_scroll_plan_varies_steps_and_distance():
    pacer, _ = make_pacer()

    plans = [pacer.scroll_plan() for _ in range(20)]

    assert all(2 <= len(p) <= 4 for p in plans)
    assert len({len(p) for p in plans}) > 1
    pixels = [px for plan in plans for px, _ in plan]
    gaps = [gap for plan in plans for _, gap in plan]
    assert all(1200 <= px <= 2600 for px in pixels)
    assert all(0.4 <= gap <= 1.2 for gap in gaps)
    assert len(set(pixels)) > 1


def test_scroll_plan_still_scrolls_when_pacing_disabled():
    """Pacing off must not break rendering — the page still needs scrolling."""
    pacer, _ = make_pacer(PacingConfig(enabled=False))

    plan = pacer.scroll_plan()

    assert len(plan) == 3
    assert all(px > 0 for px, _ in plan)


# --- Config validation ------------------------------------------------------


def test_rejects_inverted_delay_bounds():
    with pytest.raises(ConfigurationError, match="page_delay_min"):
        PacingConfig(page_delay_min=9.0, page_delay_max=2.0).validate()


def test_rejects_negative_delay():
    with pytest.raises(ConfigurationError, match="non-negative"):
        PacingConfig(page_delay_min=-1.0).validate()


def test_rejects_negative_long_pause_interval():
    with pytest.raises(ConfigurationError, match="long_pause_every"):
        PacingConfig(long_pause_every=-3).validate()


def test_accepts_defaults():
    PacingConfig().validate()  # must not raise


# --- Wiring into capture_search ---------------------------------------------


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self.url = "https://www.linkedin.com/sales-api/salesApiLeadSearch?q=x"
        self.headers = {"content-type": "application/json"}
        self._body = body

    async def json(self) -> dict:
        return self._body


class _FakeMouse:
    def __init__(self) -> None:
        self.wheels: list[int] = []

    async def wheel(self, dx: int, dy: int) -> None:
        self.wheels.append(dy)


class _FakeLocator:
    def __init__(self, page: "_FakePage", exists: bool) -> None:
        self._page = page
        self._exists = exists

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def count(self) -> int:
        return 1 if self._exists else 0

    async def is_enabled(self) -> bool:
        return True

    async def scroll_into_view_if_needed(self, timeout: float | None = None) -> None:
        pass

    async def click(self, timeout: float | None = None) -> None:
        await self._page.deliver()


class _FakePage:
    """Minimal Playwright page: serves one payload per goto/Next click."""

    def __init__(self, pages_available: int) -> None:
        self.pages_available = pages_available
        self.served = 0
        self.mouse = _FakeMouse()
        self._handler = None
        self.event_log: list[str] = []

    def on(self, event: str, handler) -> None:
        self._handler = handler

    def remove_listener(self, event: str, handler) -> None:
        self._handler = None

    async def deliver(self) -> None:
        """Fire the response the real page would fire for the next result page."""
        if self.served >= self.pages_available:
            return
        start = self.served * 25
        body = {
            "elements": [
                {
                    "$recipeType": "com.linkedin.sales.deco.desktop.searchv2."
                    "LeadSearchResult",
                    "entityUrn": f"urn:li:fs_salesProfile:(P{start + n},X,y)",
                    "fullName": f"Person {start + n}",
                }
                for n in range(25)
            ],
            "paging": {"total": self.pages_available * 25, "count": 25, "start": start},
        }
        self.served += 1
        self.event_log.append("payload")
        await self._handler(_FakeResponse(body))

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        self.event_log.append("goto")
        await self.deliver()

    def locator(self, selector: str) -> _FakeLocator:
        # Only the first selector in _NEXT_SELECTORS "exists".
        return _FakeLocator(self, selector == 'button[aria-label="Next"]')


@pytest.mark.asyncio
async def test_capture_dwells_between_pages_but_not_before_the_first():
    page = _FakePage(pages_available=3)
    config = PacingConfig(
        page_delay_min=3.0,
        page_delay_max=8.0,
        long_pause_every=0,
        scroll_steps_min=1,
        scroll_steps_max=1,
        scroll_gap_min=0.5,
        scroll_gap_max=0.5,
    )
    pacer, sleeper = make_pacer(config)

    result = await capture_search(page, PEOPLE_URL, "contacts", 3, pacer=pacer)

    assert result["pages_fetched"] == 3
    dwells = [s for s in sleeper.calls if s >= 3.0]
    # Three pages means two gaps — none before page one.
    assert len(dwells) == 2
    assert all(3.0 <= s <= 8.0 for s in dwells)


@pytest.mark.asyncio
async def test_capture_single_page_never_dwells():
    page = _FakePage(pages_available=5)
    pacer, sleeper = make_pacer()

    await capture_search(page, PEOPLE_URL, "contacts", 1, pacer=pacer)

    assert not any(s >= 3.0 for s in sleeper.calls)


@pytest.mark.asyncio
async def test_capture_scrolls_each_page():
    page = _FakePage(pages_available=2)
    pacer, _ = make_pacer()

    await capture_search(page, PEOPLE_URL, "contacts", 2, pacer=pacer)

    assert len(page.mouse.wheels) >= 2 * 2
    assert all(px > 0 for px in page.mouse.wheels)


@pytest.mark.asyncio
async def test_capture_still_works_with_pacing_disabled():
    page = _FakePage(pages_available=2)
    pacer, sleeper = make_pacer(PacingConfig(enabled=False))

    result = await capture_search(page, PEOPLE_URL, "contacts", 2, pacer=pacer)

    assert result["pages_fetched"] == 2
    assert result["count"] == 50
    assert sleeper.calls == [0.6] * 6  # fixed scroll rhythm only, no dwell
