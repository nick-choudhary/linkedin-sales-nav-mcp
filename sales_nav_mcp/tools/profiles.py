"""Depth-3 tools: fetch and read full lead profiles."""

import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from sales_nav_mcp.config import DEFAULT_TOOL_TIMEOUT_SECONDS, get_config
from sales_nav_mcp.error_handler import raise_tool_error
from sales_nav_mcp.profiles import fetch_profiles as _fetch_profiles
from sales_nav_mcp.store import get_store

logger = logging.getLogger(__name__)


def register_profile_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the depth-3 profile tools."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Fetch Full Lead Profiles",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"contacts", "profile"},
    )
    async def fetch_lead_profiles(
        url_or_hash: str,
        limit: Annotated[int | None, Field(ge=1, le=200)] = 10,
    ) -> dict[str, Any]:
        """Fetch full profiles for a query's leads — depth 3, drafting material.

        Same endpoint as `enrich_leads`, much wider projection: headline,
        summary, full position descriptions, educations, skills, languages,
        volunteering, connection counts. About 15 KB per lead against ~240
        bytes for the enrichment screen.

        Deliberately separate from `enrich_leads`. The screen runs across a
        whole list to find who is free to message; this runs only for the
        leads you are about to write to. Merging them would pull heavy
        payloads for leads you never contact, and would make "recent activity"
        as stale as the screen.

        Requires `ENABLE_PROFILE=true` — it costs one heavy request per lead,
        so a default install will not do it.

        Resumable: leads with a successful fetch are skipped, so calling
        repeatedly walks the query.

        Args:
            url_or_hash: The query's url_hash or original search URL.
            limit: Max leads to fetch in THIS call (1-200).

        Returns:
            Counts fetched/failed/remaining plus the 24h event summary.
        """
        try:
            store = get_store()
            query = store.resolve_query(url_or_hash)
            if query is None:
                return {
                    "error": "unknown_query",
                    "url_or_hash": url_or_hash,
                    "suggestion": "Call list_queries to see saved queries.",
                }
            logger.info("fetch_lead_profiles hash=%s limit=%s", query.url_hash, limit)
            return await _fetch_profiles(query.url_hash, limit=limit)
        except Exception as e:
            raise_tool_error(e, "fetch_lead_profiles")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Lead Profile",
        annotations={"readOnlyHint": True},
        tags={"contacts", "profile"},
    )
    async def get_lead_profile(member_id: int) -> dict[str, Any]:
        """Read one stored full profile — the payload you draft a message from.

        Reads from the local store only; it does not call LinkedIn. Run
        `fetch_lead_profiles` first for leads that have not been fetched.

        Args:
            member_id: Stable LinkedIn member id.
        """
        try:
            store = get_store()
            profile = store.get_profile(member_id)
            if profile is not None and "profile" not in profile:
                # A recorded attempt with no parsed payload is not a
                # profile; saying found=True would hand the caller an
                # empty record that looks fetched.
                return {
                    "member_id": member_id,
                    "found": False,
                    "http_status": profile.get("http_status"),
                    "error": profile.get("error"),
                    "suggestion": (
                        "The last fetch for this lead did not return a "
                        "usable profile. Call fetch_lead_profiles again."
                    ),
                }
            if profile is None:
                return {
                    "member_id": member_id,
                    "found": False,
                    "suggestion": (
                        "No stored profile for this lead. Call "
                        "fetch_lead_profiles on the query that contains them "
                        "(needs ENABLE_PROFILE=true)."
                    ),
                }
            return {"member_id": member_id, "found": True, **profile}
        except Exception as e:
            raise_tool_error(e, "get_lead_profile")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Pipeline Status",
        annotations={"readOnlyHint": True},
        tags={"data"},
    )
    async def pipeline_status(url_or_hash: str) -> dict[str, Any]:
        """One funnel view of a query: scraped → enriched → open → profiled → sent.

        State otherwise lives across four tables; this joins it so "where is
        everything?" is one call instead of mental arithmetic.

        Args:
            url_or_hash: The query's url_hash or original search URL.
        """
        try:
            import time

            store = get_store()
            query = store.resolve_query(url_or_hash)
            if query is None:
                return {
                    "error": "unknown_query",
                    "url_or_hash": url_or_hash,
                    "suggestion": "Call list_queries to see saved queries.",
                }
            h = query.url_hash
            enrich = store.enrichment_stats(h)
            config = get_config().outreach
            return {
                "url_hash": h,
                "depth": getattr(query, "depth", "search"),
                "funnel": {
                    "scraped": query.records_count,
                    "available_upstream": query.total_available,
                    "enriched": enrich["succeeded"],
                    "open_profile": enrich["open_profiles"],
                    "profiles_fetched": store.profile_stats(h)["fetched"],
                    "eligible_now": len(
                        store.outreach_candidates(h, "__count__", limit=100000)
                    ),
                },
                "pending": {
                    "enrichment": len(store.pending_enrichment(h)),
                    "profiles": len(store.pending_profiles(h)),
                },
                # Scoped to this query. The global figures live on
                # outreach_status; a per-query funnel reporting another
                # query's sends would be simply wrong.
                "outreach": store.outreach_stats_for_query(h),
                "ambiguous_sends": store.count_ambiguous(h),
                "events_last_24h": store.event_summary(time.time() - 24 * 3600),
                "gates": {
                    "enrich": config.enable_enrich,
                    "profile": config.enable_profile,
                    "sending": config.enabled,
                },
            }
        except Exception as e:
            raise_tool_error(e, "pipeline_status")  # NoReturn
