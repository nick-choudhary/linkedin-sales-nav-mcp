"""FastMCP server construction.

A create_mcp_server() factory: register the tool modules with a shared
per-tool timeout, mask_error_details=True, and a lifespan hook that closes the
shared resources on shutdown (the persistent browser context and the store).
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from sales_nav_mcp import __version__
from sales_nav_mcp.browser import close_browser
from sales_nav_mcp.capabilities import register_capabilities
from sales_nav_mcp.config import DEFAULT_TOOL_TIMEOUT_SECONDS
from sales_nav_mcp.store import close_store
from sales_nav_mcp.tools.accounts import register_account_tools
from sales_nav_mcp.tools.contacts import register_contact_tools
from sales_nav_mcp.tools.data import register_data_tools
from sales_nav_mcp.tools.meta import register_meta_tools

logger = logging.getLogger(__name__)


@lifespan
async def browser_lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Close the persistent browser on shutdown."""
    del app
    logger.info("Sales Navigator MCP server starting...")
    try:
        yield {}
    finally:
        logger.info("Sales Navigator MCP server shutting down...")
        await close_browser()
        close_store()


def create_mcp_server(*, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS) -> FastMCP:
    """Create and configure the MCP server with all Sales Navigator tools."""
    mcp = FastMCP(
        "linkedin-sales-nav-mcp",
        version=__version__,
        lifespan=browser_lifespan,
        mask_error_details=True,
    )

    register_contact_tools(mcp, tool_timeout=tool_timeout)
    register_account_tools(mcp, tool_timeout=tool_timeout)
    register_data_tools(mcp, tool_timeout=tool_timeout)
    register_meta_tools(mcp, tool_timeout=tool_timeout)
    register_capabilities(mcp)

    return mcp
