"""Compose capture: read what the message panel loads, instead of asking for it.

Opening a lead's message panel makes Sales Navigator fetch, on its own:

    salesApiProfiles/(profileId,...)?decoration=(...contactInfo...memberBadges...)
        Open Profile badge, InMail restriction, and contact info
    salesApiInsights?q=findInsightsByMember&page=INBOX&member=<profileId>
        the panel's right-hand column: latest post, job change, shared
        connections / companies / schools / groups
    salesApiMessagingThreads?recipients=List(<profile urn>)
        any existing conversation with this person

So this module does what a person does -- open the lead, click Message, look,
close -- and records the responses the page receives. It issues no request of
its own, which is the difference from `enrich.py`'s in-page `fetch`: every call
here is byte-for-byte the one the real client makes, headers included.

The cost is time and visibility. A capture is a page load plus a click, about
8-12 s per lead against ~1 s for the fetch, and it is a real profile view: the
lead may see it under "who viewed your profile" unless your Sales Navigator
browsing is set to private.

The panel is only ever **closed** here, through its "Close conversation"
button. Nothing in this module fills a field or touches Send.

`contactInfo` is parsed generically. Observed so far: empty, absent, and a
company website (category COMPANY). Which keys a populated one carries
(emails, websites, phones, handles) varies by profile, so every list and
string under it becomes a row, and the raw object is stored alongside.

Observed live (2026-09-22), in order of arrival: the lead page's own profile
call (contactInfo, no memberBadges), the thread lookup (normalized, under
`data`), the panel's profile call (memberBadges + contactInfo), and insights.
Insight types seen: LEAD_SHARE_CONTENT_INSIGHT, LEAD_SHARE_ARTICLE_INSIGHT
(up to four recent posts) and SHARED_CONNECTIONS_INSIGHT.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sales_nav_mcp.browser import get_browser
from sales_nav_mcp.config import get_config
from sales_nav_mcp.enrich import EnrichDepthDisabled, classify
from sales_nav_mcp.pacing import Pacer
from sales_nav_mcp.store import get_store

logger = logging.getLogger(__name__)

_LEAD_URL = "https://www.linkedin.com/sales/lead/{key}"
_CLOSE_SEL = 'button[aria-label^="Close conversation"]'

# How long to wait for the panel's responses after clicking Message.
_PANEL_TIMEOUT_S = 15.0
_POLL_S = 0.25

_CHANNEL_JS = r"""
() => {
  const text = document.body.innerText || '';
  const credit = text.match(/use\s+1\s+of\s+(\d+)\s+credits/i);
  return JSON.stringify({
    free: /free to open profile/i.test(text),
    credits_remaining: credit ? parseInt(credit[1], 10) : null,
  });
}
"""

# Keys under a contact item that hold the value itself, most specific first.
_VALUE_KEYS = (
    "emailAddress",
    "email",
    "url",
    "number",
    "phoneNumber",
    "handle",
    "name",
    "address",
    "value",
)
_LABEL_KEYS = ("type", "category", "label")


# ------------------------------------------------------------------ parsing


def _kind_for(source_key: str, value: str) -> str:
    key = source_key.lower()
    if "@" in value or "email" in key:
        return "email"
    if "phone" in key:
        return "phone"
    if "twitter" in key or "social" in key or "handle" in key or key == "im":
        return "social"
    if "website" in key or value.startswith(("http://", "https://", "www.")):
        return "website"
    if "address" in key:
        return "address"
    return "other"


def _item_value(item: Any) -> tuple[str | None, str | None]:
    """(value, label) for one contact item, whatever its shape."""
    if isinstance(item, str):
        return (item.strip() or None), None
    if not isinstance(item, dict):
        return None, None
    value = None
    for k in _VALUE_KEYS:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            value = v.strip()
            break
    if value is None:
        value = next(
            (v.strip() for v in item.values() if isinstance(v, str) and v.strip()),
            None,
        )
    label = None
    for k in _LABEL_KEYS:
        v = item.get(k)
        if isinstance(v, str) and v:
            label = v
            break
        if isinstance(v, dict):
            # e.g. {"category": {"com.linkedin...Standard": {"category": "BLOG"}}}
            inner = next(
                (
                    x
                    for x in _walk_strings(v)
                    if x.isupper() or x.lower() in ("blog", "personal", "company")
                ),
                None,
            )
            if inner:
                label = inner
                break
    return value, label


def _walk_strings(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_walk_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_walk_strings(v))
    return out


def parse_contact_info(contact_info: Any) -> list[dict[str, Any]]:
    """Flatten `contactInfo` into rows of kind/value/label/source_key.

    Every list or string under it is kept: an unfamiliar key becomes
    kind "other" rather than being dropped. Duplicates are removed.
    """
    if not isinstance(contact_info, dict):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(source_key: str, item: Any) -> None:
        value, label = _item_value(item)
        if not value:
            return
        kind = _kind_for(source_key, value)
        if (kind, value.lower()) in seen:
            return
        seen.add((kind, value.lower()))
        rows.append(
            {"kind": kind, "value": value, "label": label, "source_key": source_key}
        )

    for key, val in contact_info.items():
        if key.startswith("$"):
            continue
        if isinstance(val, list):
            for item in val:
                add(key, item)
        elif isinstance(val, (str, dict)):
            add(key, val)
    return rows


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _text(value.get("text"))
    return None


def _name_of(entity: Any) -> str | None:
    if isinstance(entity, dict):
        full = " ".join(
            x for x in (entity.get("firstName"), entity.get("lastName")) if x
        ).strip()
        return entity.get("name") or full or None
    return None


def parse_insights(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    """(insights, company name) from a findInsightsByMember response.

    Observed shape: `elements[0] = {company, companyResolutionResult?,
    insights: [{insightType, details: {...}}]}`. The company often fails to
    resolve (`companyResolutionError`), so it is optional.
    """
    if not isinstance(payload, dict):
        return [], None
    elements = payload.get("elements") or []
    out: list[dict[str, Any]] = []
    company = None
    for el in elements:
        if not isinstance(el, dict):
            continue
        company = company or _name_of(el.get("companyResolutionResult"))
        for ins in el.get("insights") or []:
            if not isinstance(ins, dict):
                continue
            d = ins.get("details") or {}
            article = d.get("article") or {}
            content = d.get("content") or {}
            shared: list[str] = []
            for key in (
                "sharedConnections",
                "sharedCompanies",
                "sharedSchools",
                "sharedGroups",
            ):
                # `sharedConnections*~fs_salesProfile(...)` comes back as a list
                # of URN strings, with any resolved entities in a sibling
                # `<key>ResolutionResults` map (observed live, 2026-09-22).
                resolved = d.get(f"{key}ResolutionResults") or {}
                for item in d.get(key) or []:
                    if isinstance(item, str):
                        name = _name_of(resolved.get(item))
                    elif isinstance(item, dict):
                        name = _name_of(item) or _name_of(
                            item.get("companyResolutionResult")
                            or item.get("schoolResolutionResult")
                        )
                    else:
                        name = None
                    if name:
                        shared.append(name)
            change_company = d.get("positionChangeCompanyResolutionResult") or d.get(
                "positionChangeCompany"
            )
            out.append(
                {
                    "insight_type": ins.get("insightType"),
                    "commentary": _text(d.get("commentary")),
                    "share_urn": d.get("share")
                    if isinstance(d.get("share"), str)
                    else None,
                    "article_title": _text(article.get("title")),
                    "article_link": article.get("articleLink"),
                    "content_title": _text(content.get("title")),
                    "position_change_title": _text(d.get("positionChangeTitle")),
                    "position_change_company": _name_of(change_company)
                    or (change_company if isinstance(change_company, str) else None),
                    "promotion": json.dumps(d["promotion"])
                    if isinstance(d.get("promotion"), (dict, list))
                    else d.get("promotion"),
                    "total_count": d.get("totalCount"),
                    "shared_names": shared,
                    "raw": ins,
                }
            )
    return out, company


def thread_message_count(payload: Any) -> int | None:
    """Messages already exchanged with this person, from the thread lookup."""
    if not isinstance(payload, dict):
        return None
    # Normalized like the inbox feed (see replies.py): the list sits under
    # `data`. Accept the flat shape too.
    data = payload.get("data")
    elements = (data if isinstance(data, dict) else payload).get("elements")
    if not isinstance(elements, list):
        return None
    total = 0
    for thread in elements:
        if isinstance(thread, dict):
            total += int(
                thread.get("totalMessageCount") or len(thread.get("messages") or [])
            )
    return total


# ------------------------------------------------------------------ capture


def _matcher(profile_id: str) -> Callable[[str], str | None]:
    """URL -> which panel response it is, or None if it is not ours."""

    def match(url: str) -> str | None:
        if "/sales-api/salesApiProfiles/" in url and f"profileId:{profile_id}" in url:
            return "profile" if "contactInfo" in url else None
        if "salesApiInsights" in url and f"member={profile_id}" in url:
            return "insights"
        if (
            "salesApiMessagingThreads" in url
            and "q=recipients" in url
            and profile_id in url
        ):
            return "thread"
        return None

    return match


async def capture_lead(
    page: Any, target: dict[str, Any], *, timeout: float = _PANEL_TIMEOUT_S
) -> dict[str, Any]:
    """Open one lead's message panel, record what it loads, close it."""
    profile_id = target["profile_id"]
    key = f"{profile_id},{target['auth_type']},{target['auth_token']}"
    match = _matcher(profile_id)
    got: dict[str, Any] = {}
    status: dict[str, int] = {}

    contact_seen: dict[str, Any] = {}

    async def on_response(response: Any) -> None:
        kind = match(response.url)
        if kind is None or kind in got:
            return
        try:
            body = await response.json()
        except Exception:  # noqa: BLE001 - an unreadable body is a failed capture
            body = None
        if kind == "profile":
            # The lead page makes its own profile call first, with contactInfo
            # but without memberBadges (observed live). Only the panel's call
            # carries the badge, so wait for that one, and keep contactInfo
            # from whichever response had it.
            if isinstance(body, dict) and "contactInfo" in body:
                contact_seen.setdefault("contactInfo", body["contactInfo"])
            badges = body.get("memberBadges") if isinstance(body, dict) else None
            if not isinstance(badges, dict) or badges.get("openLink") is None:
                status.setdefault("profile_partial", response.status)
                return
        status[kind] = response.status
        got[kind] = body

    result: dict[str, Any] = {
        "member_id": target["member_id"],
        "profile_id": profile_id,
    }
    page.on("response", on_response)
    try:
        await page.goto(_LEAD_URL.format(key=key), wait_until="domcontentloaded")
        await page.get_by_role("button", name="Message").first.click(timeout=20000)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not (
            "profile" in got and "insights" in got
        ):
            await asyncio.sleep(_POLL_S)
        # The thread lookup usually lands with the others; give it a moment.
        if "thread" not in got:
            await asyncio.sleep(1.0)
        try:
            channel = json.loads(await page.evaluate(_CHANNEL_JS))
            result["credits_remaining"] = channel.get("credits_remaining")
        except Exception:  # noqa: BLE001 - the channel line is a nicety
            pass
    except Exception as e:  # noqa: BLE001 - one bad lead must not end the run
        result["error"] = f"panel did not open: {e}"
    finally:
        page.remove_listener("response", on_response)
        await _close_panel(page)

    profile = got.get("profile")
    result["http_status"] = status.get("profile", status.get("profile_partial"))
    if isinstance(profile, dict):
        contact_info = profile.get("contactInfo", contact_seen.get("contactInfo"))
        result["member_badges"] = profile.get("memberBadges")
        result["inmail_restriction"] = profile.get("inmailRestriction")
        result["contact_info"] = contact_info
        result["contact_items"] = parse_contact_info(contact_info)
        result["raw"] = profile
    elif "error" not in result:
        result["error"] = (
            "message panel profile response not seen"
            if "profile_partial" not in status
            else "profile seen without memberBadges.openLink (panel call missing)"
        )

    # Insights are a bonus: a shape we have not seen must not cost us the
    # badge and contact info already read from the profile response.
    try:
        insights, company = parse_insights(got.get("insights"))
    except Exception as e:  # noqa: BLE001
        logger.warning("insights parse failed for %s: %s", profile_id, e)
        insights, company = [], None
        result["insights_parse_error"] = str(e)
    result["insights"] = insights
    result["insights_company"] = company
    result["insights_seen"] = "insights" in got
    result["thread_messages"] = thread_message_count(got.get("thread"))
    # Which panel responses arrived, with their HTTP status. Goes to the event
    # log so a capture that half-worked can be diagnosed afterwards.
    result["panel_status"] = dict(status)

    # Same success rule as the fast fetch: no openLink answer, no enrichment.
    result["error"] = result.get("error") or classify(result)
    return result


async def _close_panel(page: Any) -> None:
    """Close the compose panel. Only the close control is ever clicked."""
    try:
        close = page.locator(_CLOSE_SEL)
        if await close.count():
            await close.first.click(timeout=5000)
    except Exception:  # noqa: BLE001 - the next navigation discards it anyway
        logger.debug("compose panel close failed", exc_info=True)


async def capture_leads(
    url_hash: str,
    *,
    limit: int | None = None,
    only_missing: bool = True,
    capture: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    pacer: Pacer | None = None,
) -> dict[str, Any]:
    """Run `capture_lead` across a query's pending leads and store results."""
    if not get_config().outreach.enable_enrich:
        raise EnrichDepthDisabled(
            "Open Profile enrichment (depth 2) is disabled. Set "
            "ENABLE_ENRICH=true to allow it. It opens one lead page per lead."
        )
    capture = capture or capture_lead
    pacer = pacer or Pacer()
    store = get_store()
    targets = store.pending_capture(url_hash, limit=limit, only_missing=only_missing)
    if not targets:
        return {
            "url_hash": url_hash,
            "method": "compose",
            "captured_this_call": 0,
            "nothing_pending": True,
            **store.enrichment_stats(url_hash),
            "suggestion": "Every lead in this query has been captured already.",
        }

    ok = 0
    failures = 0
    with_contact = 0
    browser = get_browser()
    async with browser.lock:
        page = await browser.get_page()
        for i, target in enumerate(targets):
            if i:
                await pacer.dwell()
            started = time.monotonic()
            try:
                result = await capture(page, target)
            except Exception as e:  # noqa: BLE001 - one bad lead must not end the run
                logger.warning(
                    "capture crashed for %s", target["member_id"], exc_info=True
                )
                result = {
                    "member_id": target["member_id"],
                    "profile_id": target["profile_id"],
                    "http_status": None,
                    "error": f"capture failed: {e}",
                }
            store.record_capture(result)
            problem = result.get("error")
            store.log_event(
                "capture",
                ok=problem is None,
                member_id=target["member_id"],
                http_status=result.get("http_status"),
                error=problem,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail=json.dumps(result.get("panel_status") or {}),
            )
            if problem is None:
                ok += 1
                with_contact += bool(result.get("contact_items"))
            else:
                failures += 1
            logger.info(
                "capture %s: %d/%d (%s)", url_hash, i + 1, len(targets), problem or "ok"
            )

    stats = store.enrichment_stats(url_hash)
    remaining = len(store.pending_capture(url_hash, only_missing=True))
    return {
        "url_hash": url_hash,
        "method": "compose",
        "captured_this_call": ok,
        "failed_this_call": failures,
        "with_contact_info_this_call": with_contact,
        "remaining": remaining,
        "events_last_24h": store.event_summary(time.time() - 24 * 3600),
        **stats,
        "suggestion": (
            f"{ok} captured ({with_contact} with contact info), {failures} failed "
            f"and stay pending. {remaining} left; call again to continue."
            if remaining
            else f"{ok} captured, {failures} failed. Nothing pending; "
            "get_results / export_results now include contact info and insights."
        ),
    }
