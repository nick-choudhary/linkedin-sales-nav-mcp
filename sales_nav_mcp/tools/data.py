"""Data-management tools: list saved queries, export, and sample results.

These read from the SQLite store the search tools write to. They exist so the
agent can manage and export scraped data WITHOUT the raw records ever flowing
through a search call's return value.
"""

from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from sales_nav_mcp.config import DEFAULT_TOOL_TIMEOUT_SECONDS
from sales_nav_mcp.error_handler import raise_tool_error
from sales_nav_mcp.exceptions import SalesNavMCPError
from sales_nav_mcp.export import export_query
from sales_nav_mcp.store import get_store


def register_data_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register data-management tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="List Saved Queries",
        annotations={"readOnlyHint": True},
        tags={"data"},
    )
    async def list_queries(ctx: Context) -> dict[str, Any]:
        """
        List every saved search and its progress.

        Returns:
            Dict with `queries`: for each, url_hash, url, scraper_type, status
            (new|in_progress|paused|complete), last_page, total_available,
            records_count. Use a url_hash with export_results or get_results.
        """
        try:
            store = get_store()
            return {
                "db_path": str(store.db_path),
                "queries": [
                    {
                        "url_hash": q.url_hash,
                        "url": q.url,
                        "scraper_type": q.scraper_type,
                        "status": q.status,
                        "last_page": q.last_page,
                        "total_available": q.total_available,
                        "records_count": q.records_count,
                    }
                    for q in store.list_queries()
                ],
            }
        except Exception as e:
            raise_tool_error(e, "list_queries")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Export Results",
        annotations={"readOnlyHint": True},
        tags={"data"},
    )
    async def export_results(
        url_or_hash: str,
        ctx: Context,
        format: Literal["json", "csv", "both"] = "both",
        include_raw: bool = False,
    ) -> dict[str, Any]:
        """
        Export a saved query's records to files under the output folder.

        Args:
            url_or_hash: The query's url_hash (from list_queries) or its
                original search URL.
            ctx: FastMCP context.
            format: 'json', 'csv', or 'both' (default). JSON is the full store
                view; CSV is flat columns for spreadsheets.
            include_raw: Include LinkedIn's raw element JSON in the export.

        Returns:
            Dict with url_hash, record_count, and the written file paths
            (output/<url_hash>/query.json + the record file(s)).
        """
        try:
            store = get_store()
            query = store.resolve_query(url_or_hash)
            if query is None:
                raise SalesNavMCPError(
                    f"No saved query matches '{url_or_hash}'. Call list_queries "
                    "to see saved queries."
                )
            return export_query(store, query, format, include_raw=include_raw)
        except Exception as e:
            raise_tool_error(e, "export_results")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Results Sample",
        annotations={"readOnlyHint": True},
        tags={"data"},
    )
    async def get_results(
        url_or_hash: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """
        Pull a bounded slice of a saved query's records into the conversation
        for analysis. Bounded on purpose — for large sets, prefer export_results
        and analyze the file with code rather than loading everything into
        context.

        Args:
            url_or_hash: The query's url_hash or original search URL.
            ctx: FastMCP context.
            limit: Max records to return (1-200, default 25).
            offset: Records to skip (for paging through the sample).

        Returns:
            Dict with url_hash, total_records, returned, offset, and `records`.
        """
        try:
            store = get_store()
            query = store.resolve_query(url_or_hash)
            if query is None:
                raise SalesNavMCPError(
                    f"No saved query matches '{url_or_hash}'. Call list_queries "
                    "to see saved queries."
                )
            records = list(
                store.iter_records(query.url_hash, limit=limit, offset=offset)
            )
            if query.scraper_type == "contacts":
                enrichment = store.enrichment_map(query.url_hash)
                for record in records:
                    member_id = record.get("memberId")
                    if member_id is not None and int(member_id) in enrichment:
                        record["enrichment"] = enrichment[int(member_id)]
            return {
                "url_hash": query.url_hash,
                "scraper_type": query.scraper_type,
                "total_records": query.records_count,
                "returned": len(records),
                "offset": offset,
                "records": records,
            }
        except Exception as e:
            raise_tool_error(e, "get_results")  # NoReturn
