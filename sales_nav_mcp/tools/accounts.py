"""Sales Navigator account (company) search tool — browser capture."""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from sales_nav_mcp.config import DEFAULT_TOOL_TIMEOUT_SECONDS
from sales_nav_mcp.error_handler import raise_tool_error
from sales_nav_mcp.runner import run_search

logger = logging.getLogger(__name__)


def register_account_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register account-search tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Accounts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"accounts", "search"},
    )
    async def search_accounts(
        search_url: str,
        ctx: Context,
        pages: Annotated[int, Field(ge=1, le=10)] = 1,
        resume: bool = True,
        refresh: bool = False,
        include_raw: bool = False,
    ) -> dict[str, Any]:
        """
        Search companies/accounts by driving Sales Navigator in the logged-in
        browser and capturing its search API responses. Results are saved to
        the local database automatically; this tool returns a small summary,
        not the records (call export_results or get_results for the data).

        Build the search in Sales Navigator (industry / headcount / geography /
        growth filters), copy the URL from the address bar, and pass it here.
        Saved account lists work too.

        Resumable: the search URL is hashed to a query id. If a previous run
        stopped partway, calling again with the same URL resumes from the next
        page instead of restarting.

        Args:
            search_url: Full Sales Navigator URL (/sales/search/accounts,
                /sales/search/company, or /sales/lists/accounts).
            ctx: FastMCP context for progress reporting.
            pages: How many 25-result pages to fetch in THIS call (1-10).
            resume: If true (default), continue from where a prior run of this
                same URL stopped. If false, start from page 1 (saved records
                are kept and de-duplicated).
            refresh: If true, forget this query's saved progress and records
                and re-scrape from page 1.
            include_raw: Keep LinkedIn's raw element JSON alongside each record.

        Returns:
            Lean summary: url_hash, status (complete|paused), new_records_this_call,
            total_records, total_available, last_page, next_page, and a
            suggestion string for the next step. Ask the user whether to export
            to CSV once complete.
        """
        try:
            logger.info("search_accounts url='%s' pages=%d", search_url, pages)
            return await run_search(
                "accounts",
                search_url,
                ctx,
                pages=pages,
                resume=resume,
                refresh=refresh,
                include_raw=include_raw,
            )
        except Exception as e:
            raise_tool_error(e, "search_accounts")  # NoReturn
