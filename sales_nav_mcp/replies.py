"""Reply capture: the other half of outreach.

Sending without measuring is not a campaign. This reads the Sales Navigator
inbox and marks leads who answered, which closes the loop and -- just as
importantly -- stops a follow-up going to someone who already replied.

## The signal

`salesApiMessagingThreads` returns structured threads, so nothing here parses
rendered text. Each element carries:

    participants                  profile URNs in the thread
    participantsResolutionResults URN -> profile, including objectUrn
    messages*(author, deliveredAt, body, subject)
    unreadMessageCount, totalMessageCount, archived

Note what the endpoint does NOT give you: `participantsResolutionResults` maps
`*<urn>` to the same `<urn>` -- references, not resolved profiles -- and
`included` comes back empty, so there is no `objectUrn` and therefore no
member_id anywhere in the payload. Matching instead goes through the profileId
embedded in the participant URN, which is exactly what `leads.entity_urn` and
`lead_outreach.entity_urn` already store.

A reply is simply a message whose `author` is a participant other than the
viewer -- no heuristics about who spoke last.

Threads are read by navigating the inbox and capturing what the page receives,
the same approach the search scraper uses, rather than reconstructing a request.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any

from sales_nav_mcp.browser import get_browser
from sales_nav_mcp.store import get_store

logger = logging.getLogger(__name__)

_INBOX = "https://www.linkedin.com/sales/inbox/"
_THREADS_MARKER = "salesApiMessagingThreads?"

# Who "I" am. Read from the nav decoration rather than inferred from whichever
# participant recurs, so a single-thread inbox resolves correctly too.
_VIEWER_JS = r"""
async () => {
  const li = (s) => s.replace(/\(/g,'%28').replace(/\)/g,'%29').replace(/,/g,'%2C');
  const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)"?/) || [])[1];
  const url = '/sales-api/salesApiNavChrome?decoration='
            + li('(member~fs_salesProfile(entityUrn,objectUrn))');
  const r = await fetch(url, {
    headers: {'csrf-token': csrf, 'x-restli-protocol-version': '2.0.0', 'accept': '*/*'},
    credentials: 'include',
  });
  return JSON.stringify({status: r.status, body: await r.text()});
}
"""


def _viewer_from_nav(body: Any) -> str | None:
    """The signed-in member's profile URN, from the nav-chrome payload.

    `member` comes back as a bare URN string with the resolved profile under
    `memberResolutionResult` -- not as a nested object, which is what the
    decoration's `member~fs_salesProfile(...)` syntax suggests. Both shapes are
    accepted so a change on either side does not silently un-resolve the
    viewer, which is the one thing that makes reply attribution unsafe.
    """
    if not isinstance(body, dict):
        return None
    member = body.get("member")
    if isinstance(member, str) and member:
        return member
    if isinstance(member, dict):
        urn = member.get("entityUrn")
        if urn:
            return str(urn)
    resolved = body.get("memberResolutionResult")
    if isinstance(resolved, dict):
        urn = resolved.get("entityUrn")
        if urn:
            return str(urn)
    return None


def profile_id_of(entity_urn: str | None) -> str | None:
    """`urn:li:fs_salesProfile:(ACwAA...,NAME_SEARCH,tok)` -> `ACwAA...`.

    The profileId is the stable part; the authType and token that follow it are
    scoped to whichever search produced the URN, so only the first component is
    safe to match on.
    """
    if not entity_urn:
        return None
    m = re.search(r"\(([^,)]+)", str(entity_urn))
    return m.group(1) if m else None


def parse_threads(payload: dict[str, Any], viewer_urn: str | None) -> dict[str, dict]:
    """profileId -> reply info, for every thread the payload describes.

    A reply is a message authored by a participant who is not the viewer.
    Returns the latest such message per person, with its timestamp and a short
    preview. Keyed on profileId because the payload carries no member_id.
    """
    out: dict[str, dict[str, Any]] = {}
    if not viewer_urn:
        # Without knowing which participant is us, every message is
        # unattributable -- and the failure mode is not "miss a reply", it is
        # classifying our own outbound message as an answer from the lead.
        # Refuse rather than guess.
        logger.warning("viewer URN unresolved; not classifying any thread")
        return out
    elements = (payload.get("data") or {}).get("elements") or []
    for thread in elements:
        if not isinstance(thread, dict):
            continue
        viewer_pid = profile_id_of(viewer_urn)
        # Everyone in the thread who is not us, by profileId.
        others: dict[str, str] = {}
        for urn in thread.get("participants") or []:
            pid = profile_id_of(urn if isinstance(urn, str) else None)
            if pid and pid != viewer_pid:
                others[urn] = pid
        if not others:
            continue

        for message in thread.get("messages") or []:
            if not isinstance(message, dict):
                continue
            author = message.get("author")
            author_pid = profile_id_of(author if isinstance(author, str) else None)
            if not author_pid or author_pid == viewer_pid:
                continue
            if author_pid not in others.values():
                continue
            mid = author_pid
            delivered = message.get("deliveredAt")
            prev = out.get(mid)
            if prev and (prev.get("delivered_at") or 0) >= (delivered or 0):
                continue
            body = message.get("body")
            if isinstance(body, dict):
                body = body.get("text")
            out[mid] = {
                "profile_id": mid,
                "thread_id": thread.get("id"),
                "delivered_at": delivered,
                "unread": thread.get("unreadMessageCount") or 0,
                "preview": (str(body or "").strip() or None),
            }
    return out


async def check_replies(
    campaign: str | None = None, *, scrolls: int = 3
) -> dict[str, Any]:
    """Read the inbox and mark leads who have answered.

    Only leads already recorded as messaged are considered -- this never
    invents outreach it did not send. A lead marked `replied` still counts as
    contacted, so a reply can never cause a duplicate first-touch.
    """
    store = get_store()
    payloads: list[dict[str, Any]] = []
    viewer_urn: str | None = None

    browser = get_browser()
    async with browser.lock:
        page = await browser.get_page()

        async def on_response(response: Any) -> None:
            if _THREADS_MARKER not in response.url:
                return
            try:
                payloads.append(await response.json())
            except Exception:  # noqa: BLE001 - a skipped page is not fatal
                logger.debug("thread payload not JSON", exc_info=True)

        page.on("response", on_response)
        try:
            await page.goto(_INBOX, wait_until="domcontentloaded")
            await asyncio.sleep(8)

            raw = json.loads(await page.evaluate(_VIEWER_JS))
            if raw.get("status") == 200:
                viewer_urn = _viewer_from_nav(json.loads(raw["body"]))

            # Each scroll pulls another page of threads into the same feed.
            for _ in range(max(0, scrolls)):
                await page.mouse.wheel(0, 2000)
                await asyncio.sleep(3)
        finally:
            page.remove_listener("response", on_response)

    if viewer_urn is None:
        return {
            "error": "viewer_unresolved",
            "threads_seen": sum(
                len((p.get("data") or {}).get("elements") or []) for p in payloads
            ),
            "replies_found": 0,
            "newly_marked": 0,
            "suggestion": (
                "Could not determine which participant is you, so no thread was "
                "classified — attributing messages blind risks recording your "
                "own sends as replies. Check the session with "
                "check_session_status and retry."
            ),
        }

    found: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        for pid, info in parse_threads(payload, viewer_urn).items():
            prev = found.get(pid)
            if not prev or (info.get("delivered_at") or 0) > (
                prev.get("delivered_at") or 0
            ):
                found[pid] = info

    marked: list[dict[str, Any]] = []
    for pid, info in found.items():
        mid = store.member_id_for_profile_id(pid)
        if mid is None:
            # A conversation with someone who is not in any scraped query.
            continue
        row = store.outreach_row_any_campaign(mid, campaign)
        if row is None:
            # A conversation with someone we never messaged from here. Not ours
            # to record.
            continue
        if row["status"] == "replied":
            continue
        store.record_outreach(
            mid,
            row["campaign"],
            "replied",
            channel=row.get("channel"),
            subject=row.get("subject"),
            body=row.get("body"),
            replied_at=(info.get("delivered_at") or 0) / 1000 or None,
        )
        store.log_event(
            "reply", ok=True, member_id=mid, campaign=row["campaign"], detail="replied"
        )
        marked.append(
            {
                "member_id": mid,
                "campaign": row["campaign"],
                "preview": (info.get("preview") or "")[:160] or None,
            }
        )

    stats = store.outreach_stats(campaign)
    return {
        "threads_seen": sum(
            len((p.get("data") or {}).get("elements") or []) for p in payloads
        ),
        "viewer_resolved": viewer_urn is not None,
        "replies_found": len(found),
        "newly_marked": len(marked),
        "marked": marked,
        "by_status": stats["by_status"],
        "checked_at": time.time(),
        "suggestion": (
            f"{len(marked)} lead(s) newly marked as replied. They stay excluded "
            "from future batches, and their conversations are in the Sales "
            "Navigator inbox."
        )
        if marked
        else (
            "No new replies. Threads are only matched to leads this server "
            "recorded a send for."
        ),
    }
