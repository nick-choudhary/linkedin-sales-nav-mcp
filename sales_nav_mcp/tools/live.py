"""Live outreach tools: open a lead, draft in chat, submit with a countdown."""

import logging
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from sales_nav_mcp.config import DEFAULT_TOOL_TIMEOUT_SECONDS
from sales_nav_mcp.error_handler import raise_tool_error
from sales_nav_mcp.live_outreach import next_lead, skip, submit
from sales_nav_mcp.store import get_store

logger = logging.getLogger(__name__)

_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


def register_live_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the live (human-in-the-loop) outreach tools."""

    def _resolve(url_or_hash: str) -> str | None:
        query = get_store().resolve_query(url_or_hash)
        return query.url_hash if query else None

    @mcp.tool(
        timeout=tool_timeout,
        title="Open Next Lead For Outreach",
        annotations={"readOnlyHint": False, "openWorldHint": True},
        tags={"outreach", "live"},
    )
    async def outreach_next(
        url_or_hash: str,
        campaign: str,
        mode: Literal["draft", "supervised", "auto", "manual"] = "supervised",
        max_rows: Annotated[int, Field(ge=1, le=200)] = 60,
    ) -> dict[str, Any]:
        """Open the next eligible lead's message panel and return what to draft from.

        Walks the search results row by row in the visible browser and clicks
        Message on the next lead that is not yet contacted. The panel's own
        data is captured -- Open Profile status, contact info, insights (recent
        posts, job change, shared connections), existing conversation. Leads
        that are not Open Profile or already have a conversation are skipped
        and recorded; the first eligible one is left open and returned.

        Then draft from `lead` and call `outreach_submit`.

        Modes:
          * draft      -- submit saves the draft; nothing is sent.
          * supervised -- submit fills the panel and shows a 30s countdown in
                          the page with Skip / Send now / Pause; the person
                          watching can edit the message in the panel.
          * auto       -- same with a 5s countdown.
          * manual     -- submit fills the panel and waits; the person
                          presses LinkedIn's own Send, which is detected and
                          recorded. Nothing is sent by the tool.

        Args:
            url_or_hash: The contact search to work through.
            campaign: Label for this run; a lead is handled once per campaign
                and never messaged twice across campaigns.
            mode: draft | supervised | auto | manual.
            max_rows: Rows to examine in this call before returning "paused".
        """
        try:
            url_hash = _resolve(url_or_hash)
            if url_hash is None:
                return {"error": "unknown_query", "url_or_hash": url_or_hash}
            return await next_lead(url_hash, campaign, mode, max_rows=max_rows)
        except Exception as e:
            raise_tool_error(e, "outreach_next")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Submit Draft For The Open Lead",
        annotations=_WRITE,
        tags={"outreach", "live", "write"},
    )
    async def outreach_submit(
        url_or_hash: str,
        campaign: str,
        subject: str,
        body: str,
        evidence_used: list[str],
        countdown_seconds: Annotated[float | None, Field(ge=0, le=1800)] = None,
    ) -> dict[str, Any]:
        """Fill the open lead's panel with this draft, then act per the mode.

        The draft is validated first (length, banned phrases, and every
        `evidence_used` entry must be a field of the `lead` returned by
        outreach_next). In supervised/auto mode the panel is filled, a
        countdown runs in the page, and at zero the panel is read back -- so
        edits made during the countdown are what gets checked and sent. Skip
        in the page records the lead as skipped. Sends use the same two-phase
        commit, daily cap and dedupe as send_message, and only ever go to
        Open Profile leads.

        Args:
            url_or_hash: Same query as outreach_next.
            campaign: Same campaign as outreach_next.
            subject: Message subject.
            body: Message body. No sign-off needed: Sales Navigator appends
                your saved signature.
            evidence_used: Fields of `lead` the message draws on, e.g.
                ["title", "insights[0].commentary"].
            countdown_seconds: Override the mode's countdown (30 supervised,
                5 auto), or in manual mode how long to watch for Send (480).
        """
        try:
            url_hash = _resolve(url_or_hash)
            if url_hash is None:
                return {"error": "unknown_query", "url_or_hash": url_or_hash}
            return await submit(
                url_hash,
                campaign,
                subject,
                body,
                evidence_used,
                countdown_seconds=countdown_seconds,
            )
        except Exception as e:
            raise_tool_error(e, "outreach_submit")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Skip The Open Lead",
        annotations={"readOnlyHint": False, "openWorldHint": True},
        tags={"outreach", "live"},
    )
    async def outreach_skip(
        url_or_hash: str, campaign: str, reason: str = "skipped"
    ) -> dict[str, Any]:
        """Close the open lead's panel without sending and record the reason."""
        try:
            url_hash = _resolve(url_or_hash)
            if url_hash is None:
                return {"error": "unknown_query", "url_or_hash": url_or_hash}
            return await skip(url_hash, campaign, reason)
        except Exception as e:
            raise_tool_error(e, "outreach_skip")  # NoReturn
