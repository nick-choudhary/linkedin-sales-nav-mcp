"""Outreach tools: queue, compose prompt, and the send tool.

The deterministic/judgement split runs along the MCP boundary. The server
picks who is eligible, enforces caps and dedupe, validates the draft, and
drives the browser. The client's model reads the lead and writes the copy.
The server never generates a word, and never learns what you sell beyond the
offer file you point it at.
"""

import logging
import time
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from sales_nav_mcp.autopilot import run_batch as _run_batch
from sales_nav_mcp.config import DEFAULT_TOOL_TIMEOUT_SECONDS, get_config
from sales_nav_mcp.error_handler import raise_tool_error
from sales_nav_mcp.outreach import build_compose_prompt as _build_compose_prompt
from sales_nav_mcp.outreach import reconcile_sends as _reconcile_sends
from sales_nav_mcp.outreach import send_message as _send_message
from sales_nav_mcp.replies import check_replies as _check_replies
from sales_nav_mcp.store import get_store

logger = logging.getLogger(__name__)


def register_outreach_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register outreach tools and the compose prompt."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Next Outreach Batch",
        annotations={"readOnlyHint": True},
        tags={"outreach"},
    )
    async def next_outreach_batch(
        url_or_hash: str,
        campaign: str,
        limit: Annotated[int, Field(ge=1, le=50)] = 5,
        open_profile_only: bool = True,
    ) -> dict[str, Any]:
        """Leads eligible for a first message, best channel first.

        Read-only. Excludes anyone already sent to in ANY campaign — dedupe is
        on the stable member_id, so a person found by three searches is
        messaged once — and anyone already handled in this campaign.

        With open_profile_only (the default) only leads confirmed Open Profile
        by enrich_leads come back: those are free to message. Run enrich_leads
        first or this returns nothing.

        Args:
            url_or_hash: The query's url_hash or original search URL.
            campaign: A label for this outreach run, e.g. "direct-mail-q3".
                Dedupe is global, but status is tracked per campaign.
            limit: Max leads to return (1-50).
            open_profile_only: Only free-to-message leads. Setting this false
                surfaces leads whose messages would cost an InMail credit.

        Returns:
            `candidates` with member_id, name, title, company, plus counts and
            the remaining daily cap.
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
            candidates = store.outreach_candidates(
                query.url_hash,
                campaign,
                limit=limit,
                open_profile_only=open_profile_only,
            )
            config = get_config().outreach
            used = store.sent_since(time.time() - 24 * 3600)
            return {
                "url_hash": query.url_hash,
                "campaign": campaign,
                "returned": len(candidates),
                "candidates": candidates,
                "daily_cap": config.daily_cap,
                "sent_last_24h": used,
                "daily_cap_remaining": max(0, config.daily_cap - used),
                "sending_enabled": config.enabled,
                "suggestion": (
                    "For each candidate: read the lead with get_results, draft "
                    "with the sales_nav_compose_message prompt, then call "
                    "send_message (dry_run stays true until you have reviewed "
                    "the draft)."
                )
                if candidates
                else (
                    "Nothing eligible. If you have not run enrich_leads on "
                    "this query, no lead is known to be Open Profile yet."
                ),
            }
        except Exception as e:
            raise_tool_error(e, "next_outreach_batch")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Send Sales Navigator Message",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        tags={"outreach", "write"},
    )
    async def send_message(
        member_id: int,
        campaign: str,
        subject: str,
        body: str,
        url_or_hash: str,
        evidence_used: list[str] | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Send ONE Sales Navigator message. Writes to LinkedIn.

        This is the only tool here that is not read-only, and it is guarded
        accordingly:

        * `dry_run` defaults to **true** — you get back exactly what would be
          sent and nothing leaves the browser. Set it false deliberately.
        * Sending must also be enabled server-side (`ENABLE_SENDING=true`), so
          a default install cannot message anyone.
        * A lead already messaged in ANY campaign is refused.
        * A rolling 24h cap applies across all campaigns.
        * If the lead is not Open Profile the message would spend an InMail
          credit, and that is refused unless `ALLOW_CREDIT_SPEND=true`.
        * `evidence_used` must name fields that exist on the stored lead
          record, or the draft is rejected as ungrounded.

        Args:
            member_id: Stable LinkedIn member id (from next_outreach_batch).
            campaign: Campaign label this send belongs to.
            subject: Message subject. Required by Sales Navigator.
            body: Message body.
            url_or_hash: The query the lead belongs to, for record lookup.
            evidence_used: Record fields the personalization drew on, e.g.
                ["positions[0].title", "companyName"]. Each must resolve.
            dry_run: True (default) validates and returns without sending.

        Returns:
            The draft, any problems, whether it would send, the channel used,
            and the remaining daily cap.
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
            logger.info(
                "send_message member=%s campaign=%s dry_run=%s",
                member_id,
                campaign,
                dry_run,
            )
            return await _send_message(
                member_id,
                campaign,
                subject,
                body,
                evidence_used=evidence_used,
                dry_run=dry_run,
                url_hash=query.url_hash,
            )
        except Exception as e:
            raise_tool_error(e, "send_message")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Outreach Status",
        annotations={"readOnlyHint": True},
        tags={"outreach"},
    )
    async def outreach_status(campaign: str | None = None) -> dict[str, Any]:
        """Counts by status and channel, plus remaining daily cap.

        Args:
            campaign: Restrict to one campaign. Omit for all.
        """
        try:
            store = get_store()
            config = get_config().outreach
            used = store.sent_since(time.time() - 24 * 3600)
            stats = store.outreach_stats(campaign)
            ambiguous = len(store.ambiguous_sends(campaign, limit=1000))
            return {
                "campaign": campaign,
                **stats,
                "ambiguous_sends": ambiguous,
                "events_last_24h": store.event_summary(time.time() - 24 * 3600),
                "sent_last_24h": used,
                "daily_cap": config.daily_cap,
                "daily_cap_remaining": max(0, config.daily_cap - used),
                "sending_enabled": config.enabled,
                "credit_spend_allowed": config.allow_credit_spend,
                "suggestion": (
                    f"{ambiguous} send(s) are stuck in 'sending' — we clicked "
                    "but never recorded the outcome. Call reconcile_outreach "
                    "to settle them against LinkedIn. Until then they count as "
                    "contacted, so they cannot cause a duplicate."
                )
                if ambiguous
                else "No ambiguous sends.",
            }
        except Exception as e:
            raise_tool_error(e, "outreach_status")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Reconcile Ambiguous Sends",
        annotations={"readOnlyHint": False, "idempotentHint": True},
        tags={"outreach"},
    )
    async def reconcile_outreach(
        campaign: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Settle sends stuck in `sending` by checking LinkedIn itself.

        A `sending` row means the Send click happened but the outcome was never
        recorded — a crash, a killed browser, a disconnect. The message either
        went out or it did not, and only LinkedIn knows. This opens each lead's
        conversation, looks for our own message text, and resolves the row to
        `sent` or `failed`.

        Sends nothing. Until reconciled, an ambiguous lead is treated as
        already-contacted, so the uncertainty can never produce a duplicate —
        it can only delay a legitimate follow-up.

        Args:
            campaign: Restrict to one campaign. Omit for all.
            limit: Max ambiguous rows to check in this call (1-100).
        """
        try:
            logger.info("reconcile_outreach campaign=%s limit=%s", campaign, limit)
            return await _reconcile_sends(campaign, limit=limit)
        except Exception as e:
            raise_tool_error(e, "reconcile_outreach")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Check For Replies",
        annotations={"readOnlyHint": False, "idempotentHint": True},
        tags={"outreach"},
    )
    async def check_replies(
        campaign: str | None = None,
        scrolls: Annotated[int, Field(ge=0, le=20)] = 3,
    ) -> dict[str, Any]:
        """Read the Sales Navigator inbox and mark leads who replied.

        Sends nothing. Reads structured message threads — a reply is a message
        whose author is the lead rather than you, matched to leads by the stable
        member_id, so nothing depends on parsing rendered text or guessing who
        spoke last.

        Only leads this server recorded a send for are considered; a
        conversation with someone you messaged by hand elsewhere is left alone.
        A lead marked `replied` still counts as contacted, so a reply can never
        cause a duplicate first touch.

        Args:
            campaign: Restrict matching to one campaign. Omit for all.
            scrolls: How many times to scroll the inbox for older threads
                (0-20). Each scroll loads another page.

        Returns:
            Threads seen, replies found, how many were newly marked, and the
            resulting status counts.
        """
        try:
            logger.info("check_replies campaign=%s scrolls=%s", campaign, scrolls)
            return await _check_replies(campaign, scrolls=scrolls)
        except Exception as e:
            raise_tool_error(e, "check_replies")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Run Outreach Batch",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        tags={"outreach", "write"},
    )
    async def run_outreach_batch(
        url_or_hash: str,
        campaign: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50)] = 5,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Draft and send for several leads in one call, unattended.

        Uses MCP sampling: the server asks YOUR client for each draft, so no
        model runs here and no API key lives here — only the request for a
        completion. That is what lets a scheduled job run the loop without a
        person taking a turn.

        Nothing is relaxed for automation. Every draft goes through the same
        send path as a manual one: the evidence gate, global dedupe, the daily
        cap, the free-channel-only default, and two-phase commit. `dry_run`
        still defaults to true, so the first call shows you what it would say
        and sends nothing.

        Stops early when the daily cap is reached. A draft that fails to parse
        or fails validation is recorded and skipped rather than ending the run.

        Args:
            url_or_hash: The query to draw candidates from.
            campaign: Campaign label for these sends.
            ctx: FastMCP context, used for sampling.
            limit: Max leads to attempt in THIS call (1-50).
            dry_run: True (default) drafts and validates without sending.

        Returns:
            Counts plus a per-lead outcome, including the subject drafted and
            any validation problems.
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
            logger.info(
                "run_outreach_batch hash=%s campaign=%s limit=%s dry_run=%s",
                query.url_hash,
                campaign,
                limit,
                dry_run,
            )
            return await _run_batch(
                query.url_hash, campaign, ctx, limit=limit, dry_run=dry_run
            )
        except Exception as e:
            raise_tool_error(e, "run_outreach_batch")  # NoReturn

    @mcp.prompt(
        name="sales_nav_compose_message",
        description=(
            "Draft a personalized Sales Navigator message for one lead, using "
            "your own offer file. Refuses to render if OFFER_FILE is not set."
        ),
    )
    def compose_message_prompt(lead_json: str) -> str:
        """Your offer + this lead + the drafting rules.

        Body lives in outreach.build_compose_prompt so it can be tested
        without standing up an MCP server.
        """
        return _build_compose_prompt(lead_json)
