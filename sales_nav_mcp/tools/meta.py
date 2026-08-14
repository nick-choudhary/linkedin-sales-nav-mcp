"""Operational tool: session / login status."""

import logging
from typing import Any

from fastmcp import Context, FastMCP

from sales_nav_mcp.browser import get_browser
from sales_nav_mcp.config import DEFAULT_TOOL_TIMEOUT_SECONDS, get_config
from sales_nav_mcp.error_handler import raise_tool_error
from sales_nav_mcp.exceptions import NotLoggedInError, SalesNavAccessError

logger = logging.getLogger(__name__)


def register_meta_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register operational tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Check Session Status",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"meta"},
    )
    async def check_session_status(ctx: Context) -> dict[str, Any]:
        """
        Check whether the browser profile has a live Sales Navigator session.

        Launches (or reuses) the browser and verifies it lands on a signed-in
        /sales page. Use this first when searches fail — it tells you if you
        need to run `--login` again.

        Returns:
            Dict with logged_in (bool), plus the profile directory and
            headless setting for this server.
        """
        try:
            config = get_config().browser
            browser = get_browser()
            async with browser.lock:
                logged_in = True
                detail = "Signed-in Sales Navigator session is active."
                try:
                    await browser.get_page()
                except NotLoggedInError as e:
                    logged_in = False
                    detail = str(e)
                except SalesNavAccessError as e:
                    logged_in = False
                    detail = str(e)

            return {
                "logged_in": logged_in,
                "detail": detail,
                "profile_dir": str(config.resolved_user_data_dir()),
                "headless": config.headless,
            }

        except Exception as e:
            raise_tool_error(e, "check_session_status")  # NoReturn
