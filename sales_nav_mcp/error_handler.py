"""Centralized error handling using FastMCP ToolError.

Known exceptions map to actionable client-facing messages; unknown ones
re-raise for mask_error_details to mask. ToolErrors that arrive already-shaped
pass straight through instead of being double-wrapped.
"""

import logging
from typing import NoReturn

from fastmcp.exceptions import ToolError

from sales_nav_mcp.exceptions import (
    BrowserLaunchError,
    CaptureTimeoutError,
    NotLoggedInError,
    SalesNavAccessError,
    SalesNavMCPError,
    UrlValidationError,
)

logger = logging.getLogger(__name__)


def raise_tool_error(exception: Exception, context: str = "") -> NoReturn:
    """Raise a ToolError for known exceptions, or re-raise unknown ones."""
    ctx = f" in {context}" if context else ""

    if isinstance(exception, ToolError):
        raise exception

    if isinstance(exception, NotLoggedInError):
        logger.warning("Not logged in%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    if isinstance(exception, SalesNavAccessError):
        logger.warning("Sales Navigator access problem%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    if isinstance(exception, BrowserLaunchError):
        logger.warning("Browser launch failed%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    if isinstance(exception, CaptureTimeoutError):
        logger.warning("Capture timed out%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    if isinstance(exception, UrlValidationError):
        logger.info("URL validation failed%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    if isinstance(exception, SalesNavMCPError):
        logger.warning("Sales Nav MCP error%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    logger.error("Unexpected error%s: %s: %s", ctx, type(exception).__name__, exception)
    raise exception
