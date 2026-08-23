"""Persistent browser driver (Patchright / stealth Playwright).

This is the heart of the browser-capture approach. One persistent Chromium
profile on your machine, that you log into Sales Navigator once by hand. The
MCP tools then reuse that live session: every request to LinkedIn originates
from the real browser — real IP, real fingerprint, full cookie jar, browser-
generated CSRF/track headers — which is exactly what stops the logout loop
that cookie-replay causes.

We never automate the login. Automating credential entry is one of the
strongest bot signals there is; a human sign-in kept in a persistent profile
is the safe path.

A single asyncio.Lock serializes tool calls, because there is one browser page
and concurrent navigations would clobber each other's captures.
"""

import asyncio
import contextlib
import logging
import time
from typing import Any

from sales_nav_mcp.config import get_config
from sales_nav_mcp.exceptions import (
    BrowserLaunchError,
    NotLoggedInError,
    SalesNavAccessError,
)
from sales_nav_mcp.orphans import looks_like_profile_lock, reclaim_profile

logger = logging.getLogger(__name__)

SALES_HOME_URL = "https://www.linkedin.com/sales/home"
# Where LinkedIn sends an unauthenticated or challenged session.
_LOGGED_OUT_MARKERS = (
    "/login",
    "/checkpoint",
    "/authwall",
    "/uas/login",
    "linkedin.com/login",
)


class BrowserManager:
    """Owns the one persistent browser context for this process."""

    # How often the idle watchdog wakes. Short enough that the browser closes
    # promptly after the timeout, long enough to cost nothing while sleeping.
    IDLE_CHECK_SECONDS = 60.0

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._last_used: float = time.monotonic()
        self._watchdog: asyncio.Task[None] | None = None

    def touch(self) -> None:
        """Mark the browser as just used, deferring the idle close."""
        self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_used

    def should_close_for_idle(self) -> bool:
        """Has the browser sat unused past the configured timeout?

        A timeout of 0 disables the behaviour entirely, which is why this is
        not simply a comparison at the call site.
        """
        if self._context is None:
            return False
        timeout = get_config().browser.idle_timeout_seconds
        if timeout <= 0:
            return False
        return self.idle_seconds() >= timeout

    async def _idle_watchdog(self) -> None:
        """Close the browser once it has been idle long enough.

        Takes the same lock the tools use, so it can never close a browser
        mid-operation: if a scrape is running, the watchdog waits for it and
        re-checks afterwards, by which point the browser is no longer idle.
        """
        while True:
            try:
                await asyncio.sleep(self.IDLE_CHECK_SECONDS)
                if not self.should_close_for_idle():
                    continue
                async with self._lock:
                    if not self.should_close_for_idle():
                        continue
                    logger.info(
                        "Closing the browser after %.0fs idle; it will relaunch "
                        "on the next call.",
                        self.idle_seconds(),
                    )
                    await self._teardown()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a watchdog must not die quietly
                logger.warning("idle watchdog error", exc_info=True)

    def _ensure_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._idle_watchdog())

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def _launch(self, *, headless: bool | None = None) -> None:
        from patchright.async_api import async_playwright

        config = get_config().browser
        user_data_dir = config.resolved_user_data_dir()
        user_data_dir.mkdir(parents=True, exist_ok=True)

        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(user_data_dir),
            "headless": config.headless if headless is None else headless,
            "no_viewport": True,
        }
        if config.chrome_path:
            launch_kwargs["executable_path"] = config.chrome_path
        if config.proxy_server:
            launch_kwargs["proxy"] = {"server": config.proxy_server}

        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )
        except Exception as e:
            # A profile left locked by an orphaned browser is recoverable, and
            # common: the lifespan hook that normally closes it does not run
            # when the server dies ungracefully. Terminate whatever is holding
            # THIS profile and try once more. The orphan cannot be adopted --
            # patchright launches with --remote-debugging-pipe, so there is no
            # CDP endpoint to attach to -- but the session survives, because
            # the cookies live in the profile directory, not in the process.
            if not looks_like_profile_lock(str(e)):
                await self._teardown()
                raise BrowserLaunchError(
                    f"Could not launch Chromium: {e}. If the browser is not "
                    "installed, run 'patchright install chromium'."
                ) from e

            logger.warning("Profile appears locked; attempting to reclaim it.")
            await self._teardown()
            outcome = await asyncio.to_thread(reclaim_profile, user_data_dir)
            if not outcome.get("found"):
                raise BrowserLaunchError(
                    f"Could not launch Chromium: {e}. The profile looks locked "
                    "but no browser process holding it was found — check for a "
                    "browser open on this profile."
                ) from e
            try:
                self._playwright = await async_playwright().start()
                self._context = (
                    await self._playwright.chromium.launch_persistent_context(
                        **launch_kwargs
                    )
                )
                logger.info("Recovered the profile from %s", outcome)
            except Exception as retry_error:
                await self._teardown()
                raise BrowserLaunchError(
                    f"Could not launch Chromium after reclaiming the profile "
                    f"from {outcome.get('found')} orphaned process(es): "
                    f"{retry_error}."
                ) from retry_error

        self.touch()
        self._ensure_watchdog()
        self._context.set_default_navigation_timeout(config.nav_timeout_seconds * 1000)
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )

    async def get_page(self) -> Any:
        """Return the live page, launching the browser on first use.

        Raises NotLoggedInError if the persistent profile has no Sales
        Navigator session — the tools surface that as "run --login".
        """
        if self._context is None or not self._context_alive():
            # A context that exists but is dead -- browser crashed, window
            # closed, process killed -- used to wedge the manager forever,
            # because only `is None` triggered a relaunch. Tear it down and
            # start clean instead.
            if self._context is not None:
                logger.info("Browser context is gone; relaunching.")
                await self._teardown()
            await self._launch()
        if not await self._is_logged_in():
            raise NotLoggedInError(
                "No active LinkedIn Sales Navigator session in the browser "
                "profile. Run 'linkedin-sales-nav-mcp --login' once to sign "
                "in, then retry."
            )
        # Every handed-out page defers the idle close. A long scrape holds the
        # lock rather than calling this repeatedly, which is why the watchdog
        # also re-checks under the lock.
        self.touch()
        return self._page

    def _context_alive(self) -> bool:
        """Best-effort check that the browser is still usable."""
        page, context = self._page, self._context
        if context is None or page is None:
            return False
        try:
            if hasattr(page, "is_closed") and page.is_closed():
                return False
            # Touching .url raises once the target is gone.
            _ = page.url
        except Exception:
            return False
        return True

    async def _has_auth_cookie(self) -> bool:
        """True if the context holds a non-empty `li_at` — LinkedIn's auth
        cookie and the authoritative "is this session signed in" signal.

        URL-based checks alone are unreliable: LinkedIn can render a sign-in
        wall at a /sales URL, which would read as logged-in from the address
        bar. The cookie can't be faked by a login page.
        """
        try:
            cookies = await self._context.cookies()
        except Exception as e:
            logger.warning("Could not read cookies: %s", e)
            return False
        return any(c.get("name") == "li_at" and c.get("value") for c in cookies)

    async def _is_logged_in(self) -> bool:
        """Confirm a real authenticated Sales Navigator session.

        Requires the `li_at` auth cookie AND a Sales home page that is not a
        login/checkpoint redirect — both, so neither a stale cookie nor a
        login wall at a /sales URL reads as signed in.
        """
        if not await self._has_auth_cookie():
            return False
        page = self._page
        try:
            await page.goto(SALES_HOME_URL, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("Could not reach Sales home: %s", e)
            return False
        url = (page.url or "").lower()
        if any(marker in url for marker in _LOGGED_OUT_MARKERS):
            return False
        # A logged-in account without Sales Navigator is redirected off /sales.
        if "/sales" not in url:
            raise SalesNavAccessError(
                "Signed into LinkedIn but not into Sales Navigator (the "
                "session was redirected away from /sales). Confirm the "
                "account has Sales Navigator access."
            )
        return True

    async def run_login(self) -> bool:
        """--login: open a headed window and wait for a manual sign-in.

        Polls until the browser lands on a signed-in /sales page or the login
        timeout elapses. Never types credentials itself.
        """
        await self._launch(headless=False)
        config = get_config().browser
        page = self._page

        print(
            "\nA browser window has opened. Log into LinkedIn and open Sales "
            "Navigator.\nWaiting for a signed-in Sales Navigator session "
            f"(up to {int(config.login_timeout_seconds)}s)...",
            flush=True,
        )
        with contextlib.suppress(Exception):
            await page.goto(SALES_HOME_URL, wait_until="domcontentloaded")

        deadline = config.login_timeout_seconds
        waited = 0.0
        interval = 3.0
        while waited < deadline:
            url = (page.url or "").lower()
            # Require BOTH the real auth cookie and a genuine /sales page, so a
            # sign-in wall rendered at a /sales URL can't be mistaken for a
            # completed login.
            if (
                "/sales" in url
                and not any(m in url for m in _LOGGED_OUT_MARKERS)
                and await self._has_auth_cookie()
            ):
                print("Signed-in Sales Navigator session detected. Saved.", flush=True)
                return True
            await asyncio.sleep(interval)
            waited += interval
        print(
            "Timed out waiting for sign-in. Re-run --login and finish signing "
            "in (including any 2FA/checkpoint) before the timeout.",
            flush=True,
        )
        return False

    async def _teardown(self) -> None:
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._context = None
        self._page = None
        self._playwright = None

    async def close(self) -> None:
        """Shut down for good: stop the watchdog, then tear the browser down.

        Deliberately not part of _teardown, which the watchdog itself calls --
        cancelling the task from inside the task would kill the close midway.
        """
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog
        await self._teardown()


_manager: BrowserManager | None = None


def get_browser() -> BrowserManager:
    global _manager
    if _manager is None:
        _manager = BrowserManager()
    return _manager


async def close_browser() -> None:
    global _manager
    if _manager is not None:
        await _manager.close()
        _manager = None
