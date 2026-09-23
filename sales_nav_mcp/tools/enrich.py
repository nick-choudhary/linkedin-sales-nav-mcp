"""Open Profile enrichment tool: compose capture or one profile fetch per lead."""

import logging
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from sales_nav_mcp.compose_capture import capture_leads as _capture_leads
from sales_nav_mcp.config import DEFAULT_TOOL_TIMEOUT_SECONDS
from sales_nav_mcp.enrich import enrich_leads as _enrich_leads
from sales_nav_mcp.error_handler import raise_tool_error
from sales_nav_mcp.store import get_store

logger = logging.getLogger(__name__)


def register_enrich_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the enrichment tool with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Enrich Leads With Open Profile Status",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"contacts", "enrich"},
    )
    async def enrich_leads(
        url_or_hash: str,
        limit: Annotated[int | None, Field(ge=1, le=500)] = 50,
        only_missing: bool = True,
        method: Literal["compose", "fetch"] = "compose",
        via: Literal["search", "lead_page"] = "search",
    ) -> dict[str, Any]:
        """Add Open Profile / InMail status to a saved contact search.

        Search results cannot carry this. Sales Navigator's search payload has
        an `openLink` field, but it is `false` for everyone — the real flag is
        `memberBadges.openLink`, which only the profile endpoint returns, one
        request per lead. There is no bulk endpoint.

        Two methods:

        * `compose` (default): clicks Message on each lead's row in the
          search results (no lead page load) and records what the panel
          loads — Open Profile status plus **contact info** (emails,
          websites, phones), **insights** (recent posts, job change, shared
          connections), the company dossier, and whether a conversation
          already exists. Issues no request of its own. A few seconds per
          lead. The panel is only ever closed, never sent.
        * `fetch`: one trimmed profile request per lead from inside the page.
          ~1 s per lead, Open Profile status only.

        Results go to `lead_enrichment` (plus `lead_contact_info` and
        `lead_insights` for compose), keyed on the stable `member_id`, and are
        joined in by `get_results` / `export_results` as an `enrichment`
        block. Search records are never modified.

        Resumable and idempotent: with `only_missing=true` (the default),
        leads already done by the chosen method are skipped, so calling
        repeatedly walks through the query. Failures stay pending and retry.

        Args:
            url_or_hash: The query's url_hash (from list_queries) or its
                original search URL.
            limit: Max leads to process in THIS call (1-500). With compose,
                keep it modest (e.g. 20-50): each lead is a page visit.
            only_missing: Skip leads already done. Set false to refresh.
            method: "compose" (full panel capture) or "fetch" (fast, badge
                only).
            via: compose only. "search" opens the panel from the results row
                and falls back to the lead page for leads LinkedIn has since
                re-ranked off their page; "lead_page" always uses the lead
                page (slower, and a profile view).

        Returns:
            Counts only: how many succeeded, failed, remain, how many of the
            query's leads are Open Profile, and (compose) how many had
            contact info.
        """
        try:
            store = get_store()
            query = store.resolve_query(url_or_hash)
            if query is None:
                return {
                    "error": "unknown_query",
                    "url_or_hash": url_or_hash,
                    "suggestion": (
                        "No saved query with that hash or URL. Call "
                        "list_queries to see what is stored."
                    ),
                }
            if query.scraper_type != "contacts":
                return {
                    "error": "not_a_contact_search",
                    "url_hash": query.url_hash,
                    "scraper_type": query.scraper_type,
                    "suggestion": (
                        "Open Profile status applies to people, not accounts. "
                        "Pass a contacts query."
                    ),
                }
            logger.info(
                "enrich_leads hash=%s limit=%s method=%s", query.url_hash, limit, method
            )
            if method == "compose":
                return await _capture_leads(
                    query.url_hash, limit=limit, only_missing=only_missing, via=via
                )
            return await _enrich_leads(
                query.url_hash, limit=limit, only_missing=only_missing
            )
        except Exception as e:
            raise_tool_error(e, "enrich_leads")  # NoReturn
