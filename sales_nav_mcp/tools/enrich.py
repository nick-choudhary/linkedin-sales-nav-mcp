"""Open Profile enrichment tool — one profile fetch per lead."""

import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

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
    ) -> dict[str, Any]:
        """Add Open Profile / InMail status to a saved contact search.

        Search results cannot carry this. Sales Navigator's search payload has
        an `openLink` field, but it is `false` for everyone — the real flag is
        `memberBadges.openLink`, which only the profile endpoint returns, one
        request per lead. There is no bulk endpoint.

        Costs one LinkedIn request per lead (~1s each with pacing), so it is a
        separate opt-in tool rather than part of `search_contacts`. Start with
        a `limit` to sample before committing to a whole query.

        Results go to the `lead_enrichment` table, keyed on the stable
        `member_id`, and are joined in by `get_results` / `export_results` as
        an `enrichment` block. Search records are never modified, and a lead
        enriched once is reused by every search that finds them again.

        Resumable and idempotent: with `only_missing=true` (the default),
        leads that already have a successful fetch are skipped, so calling
        repeatedly walks through the query. Failed fetches stay pending and
        are retried.

        Args:
            url_or_hash: The query's url_hash (from list_queries) or its
                original search URL.
            limit: Max leads to enrich in THIS call (1-500). None means all
                pending — that can be a lot of requests, so prefer a limit.
            only_missing: Skip leads already enriched successfully. Set false
                to refresh statuses that may have gone stale.

        Returns:
            Counts only: how many were enriched, how many failed, how many
            remain, and how many of the query's leads are Open Profile.
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
            logger.info("enrich_leads hash=%s limit=%s", query.url_hash, limit)
            return await _enrich_leads(
                query.url_hash, limit=limit, only_missing=only_missing
            )
        except Exception as e:
            raise_tool_error(e, "enrich_leads")  # NoReturn
