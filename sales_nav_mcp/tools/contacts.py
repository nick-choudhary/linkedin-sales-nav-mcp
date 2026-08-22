"""Sales Navigator contact (people/lead) search tool — browser capture."""

import logging
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from sales_nav_mcp.config import DEFAULT_TOOL_TIMEOUT_SECONDS, get_config
from sales_nav_mcp.error_handler import raise_tool_error
from sales_nav_mcp.runner import run_search
from sales_nav_mcp.store import get_store, query_hash

logger = logging.getLogger(__name__)


def register_contact_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register contact-search tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Sales Navigator Contacts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"contacts", "search"},
    )
    async def search_contacts(
        search_url: str,
        ctx: Context,
        pages: Annotated[int, Field(ge=1, le=10)] = 1,
        resume: bool = True,
        refresh: bool = False,
        include_raw: bool = False,
        depth: Literal["search", "open_profile", "full"] = "search",
    ) -> dict[str, Any]:
        """
        Search people/leads by driving Sales Navigator in the logged-in
        browser and capturing its search API responses. Results are saved to
        the local database automatically; this tool returns a small summary,
        not the records (call export_results or get_results for the data).

        Build the search in Sales Navigator (title / geography / industry /
        seniority / keyword filters), copy the URL from the address bar, and
        pass it here. Saved lead lists work too.

        Resumable: the search URL is hashed to a query id. If a previous run
        stopped partway, calling again with the same URL resumes from the next
        page instead of restarting — safe to call repeatedly to page deeper.

        Args:
            search_url: Full Sales Navigator URL (/sales/search/people,
                /sales/search/leads, or /sales/lists/people).
            ctx: FastMCP context for progress reporting.
            pages: How many 25-result pages to fetch in THIS call (1-10). Call
                again to continue past that.
            resume: If true (default), continue from where a prior run of this
                same URL stopped. If false, start from page 1 (records already
                saved are kept and de-duplicated).
            refresh: If true, forget this query's saved progress and records
                and re-scrape from page 1.
            include_raw: Keep LinkedIn's raw element JSON alongside each record
                (useful when a field looks wrong).

        Returns:
            Lean summary: url_hash, status (complete|paused), new_records_this_call,
            total_records, total_available, last_page, next_page, and a
            suggestion string telling you/the agent the next step (resume or
            export). Ask the user whether to export to CSV once complete.
        """
        try:
            config = get_config().outreach
            if depth == "open_profile" and not config.enable_enrich:
                return {
                    "error": "depth_not_enabled",
                    "depth": depth,
                    "suggestion": "Set ENABLE_ENRICH=true to allow depth 2.",
                }
            if depth == "full" and not config.enable_profile:
                return {
                    "error": "depth_not_enabled",
                    "depth": depth,
                    "suggestion": (
                        "Set ENABLE_PROFILE=true to allow depth 3. It costs a "
                        "heavy request per lead, so it is opt-in."
                    ),
                }
            logger.info(
                "search_contacts url='%s' pages=%d depth=%s", search_url, pages, depth
            )
            result = await run_search(
                "contacts",
                search_url,
                ctx,
                pages=pages,
                resume=resume,
                refresh=refresh,
                include_raw=include_raw,
            )
            # Depth is a property of the query, not of this call, so a later
            # resume knows what this search was collected for.
            get_store().set_query_depth(query_hash(search_url), depth)
            result["depth"] = depth
            if depth != "search":
                result["next_step"] = (
                    "enrich_leads"
                    if depth == "open_profile"
                    else "enrich_leads, then fetch_lead_profiles"
                )
            return result
        except Exception as e:
            raise_tool_error(e, "search_contacts")  # NoReturn
