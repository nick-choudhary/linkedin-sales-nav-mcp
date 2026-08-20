"""Human-like pacing between result pages.

Machine scraping has a signature: identical gaps between page loads, an
identical scroll rhythm, and no breaks. A person reading search results dwells
for a variable time, scrolls unevenly, and every so often stops to do something
else. This module supplies those delays so the browser's request cadence looks
like the browsing it actually is.

Two knobs matter, and both are jittered:

* **dwell** — the pause after a page is captured and before "Next" is clicked.
  Drawn uniformly from `[page_delay_min, page_delay_max]`.
* **long pause** — every few pages the dwell is replaced by a much longer break
  from `[long_pause_min, long_pause_max]`. The interval itself is jittered
  (±1 page), because a break at exactly every 5th page is its own pattern.

Randomness and sleeping are both injectable, so tests can pin the sequence and
run without waiting. This is pacing only — it does not touch the page; callers
perform the scrolling from a `scroll_plan()`.

Honesty note: pacing lowers the volume signature, it does not make automated
collection undetectable. Rate discipline (fewer pages per day) does far more
than any jitter setting.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from sales_nav_mcp.config import PacingConfig, get_config

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]


class Pacer:
    """Draws and performs the human-like delays for one capture run.

    Stateful by design: the long-pause countdown advances across pages, so a
    single Pacer should live for the whole run rather than per page.
    """

    def __init__(
        self,
        config: PacingConfig | None = None,
        *,
        rng: random.Random | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self.config = config if config is not None else get_config().pacing
        self.rng = rng if rng is not None else random.Random()
        self.sleep = sleep if sleep is not None else asyncio.sleep
        self._pages_until_long_pause = 0
        self._reset_long_pause_countdown()

    def _reset_long_pause_countdown(self) -> None:
        every = self.config.long_pause_every
        if every <= 0:
            self._pages_until_long_pause = 0
            return
        # Jitter the interval so the breaks themselves aren't periodic.
        self._pages_until_long_pause = self.rng.randint(max(1, every - 1), every + 1)

    def next_dwell(self) -> tuple[float, bool]:
        """Return `(seconds, is_long_pause)` for the gap before the next page.

        Pure: advances the countdown but does not sleep, so callers can log or
        report the delay before serving it.
        """
        if not self.config.enabled:
            return 0.0, False
        if self.config.long_pause_every > 0:
            self._pages_until_long_pause -= 1
            if self._pages_until_long_pause <= 0:
                self._reset_long_pause_countdown()
                return (
                    self.rng.uniform(
                        self.config.long_pause_min, self.config.long_pause_max
                    ),
                    True,
                )
        return (
            self.rng.uniform(self.config.page_delay_min, self.config.page_delay_max),
            False,
        )

    async def dwell(self) -> float:
        """Sleep the inter-page gap. Returns the seconds actually waited."""
        seconds, is_long = self.next_dwell()
        if seconds <= 0:
            return 0.0
        if is_long:
            logger.info("Taking a %.0fs break before the next page.", seconds)
        else:
            logger.debug("Dwelling %.1fs before the next page.", seconds)
        await self.sleep(seconds)
        return seconds

    def scroll_plan(self) -> list[tuple[int, float]]:
        """Return `[(pixels, gap_seconds), ...]` for settling the current page.

        Varies the step count, the distance per step, and the gap between them.
        With pacing disabled this still scrolls — the page must reach the
        bottom for LinkedIn to finish rendering — but at a fixed rhythm.
        """
        config = self.config
        if not config.enabled:
            return [(2000, 0.6)] * 3
        steps = self.rng.randint(config.scroll_steps_min, config.scroll_steps_max)
        return [
            (
                self.rng.randint(config.scroll_pixels_min, config.scroll_pixels_max),
                self.rng.uniform(config.scroll_gap_min, config.scroll_gap_max),
            )
            for _ in range(steps)
        ]

    async def pause(self, seconds: float) -> None:
        """Sleep a single already-drawn interval (used for scroll gaps)."""
        if seconds > 0:
            await self.sleep(seconds)
