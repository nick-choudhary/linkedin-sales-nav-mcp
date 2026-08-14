"""Typed exceptions for the Sales Navigator MCP server.

Every exception here has a dedicated branch in
error_handler.raise_tool_error; anything else is re-raised for FastMCP's
mask_error_details to mask.
"""


class SalesNavMCPError(Exception):
    """Base exception for all server-raised errors."""


class NotLoggedInError(SalesNavMCPError):
    """The persistent browser profile has no valid Sales Navigator session.

    The fix is always the same: run `--login` once to sign in manually. We
    never automate the login itself — that is what keeps the session looking
    human and stops the logout loop.
    """


class SalesNavAccessError(SalesNavMCPError):
    """Logged in, but the account has no Sales Navigator access, or LinkedIn
    served a checkpoint / auth-wall instead of results."""


class BrowserLaunchError(SalesNavMCPError):
    """Chromium could not be launched (missing binary, bad CHROME_PATH...)."""


class CaptureTimeoutError(SalesNavMCPError):
    """Navigated to the search URL but no search API response was captured
    before the wait budget elapsed (slow network, changed endpoint, or an
    empty result set that fired no request)."""


class UrlValidationError(SalesNavMCPError):
    """The URL is not a Sales Navigator search/list URL."""
