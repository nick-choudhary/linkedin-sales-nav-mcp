"""Live outreach: one lead at a time, in the visible browser, drafted in chat.

The loop, per lead:

1. `next_lead` walks the query's search results row by row, opens the next
   eligible lead's message panel from its row, captures what the panel loads
   (Open Profile badge, contact info, insights, existing thread) and leaves the
   panel OPEN. Leads that are not Open Profile, or that already have a
   conversation, are skipped on the spot and recorded as such.
2. The client's model (Claude, in the chat) drafts from that context. There is
   no MCP sampling here on purpose: the words come from the same conversation
   that is steering the run, with whatever skills it has loaded.
3. `submit` fills the open panel and then, by mode:

   draft       records the draft and closes the panel. Nothing is sent.
   supervised  shows a banner in the page -- "Sending in 30s" with Skip /
               Send now / Pause -- so the person watching can edit the text in
               the panel, skip, or let it go. At zero the panel is read back,
               so whatever is in it then (edits included) is what is checked
               and sent.
   auto        the same with a short countdown; nobody needs to watch.

Every send still goes through `outreach.commit_send` -- the same two-phase
record, daily cap, global dedupe and free-channel-only rule as `send_message`.
The channel is checked twice: the panel's badge at capture, and the panel's
own "Free to Open Profile" line right before sending.
"""

import asyncio
import contextlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from sales_nav_mcp.browser import get_browser
from sales_nav_mcp.capture import _with_page_param
from sales_nav_mcp.company_intel import company_intel
from sales_nav_mcp.compose_capture import (
    _RESULTS_SETTLE_S,
    _ROW_LEAD_LINK_SEL,
    _ROW_SEL,
    _close_panel,
    _panel_visible,
    _row_profile_id,
    capture_row,
)
from sales_nav_mcp.config import get_config
from sales_nav_mcp.fit_rules import load_rules, screen
from sales_nav_mcp.outreach import (
    _BODY_SEL,
    _DETECT_CHANNEL,
    _SUBJECT_SEL,
    CHANNEL_CREDIT,
    CHANNEL_FREE,
    _remaining_today,
    commit_send,
    validate_message,
)
from sales_nav_mcp.pacing import Pacer
from sales_nav_mcp.store import get_store

logger = logging.getLogger(__name__)

MODES = ("draft", "supervised", "auto")
COUNTDOWN = {"supervised": 30, "auto": 5}

_OVERLAY_ID = "snm-live-overlay"

# A fixed banner at the top of the page. It never covers the compose panel
# (bottom right), so the text stays editable during the countdown. Buttons
# only set a flag; Python reads it.
_OVERLAY_JS = r"""
([id, name, seconds]) => {
  document.getElementById(id)?.remove();
  window.__snmLive = {decision: null, paused: false};
  const box = document.createElement('div');
  box.id = id;
  box.style.cssText = 'position:fixed;top:12px;left:50%;transform:translateX(-50%);' +
    'z-index:2147483647;background:#1d2226;color:#fff;padding:10px 14px;' +
    'border-radius:8px;font:14px system-ui,sans-serif;display:flex;gap:10px;' +
    'align-items:center;box-shadow:0 4px 16px rgba(0,0,0,.35)';
  const label = document.createElement('span');
  label.id = id + '-label';
  label.textContent = 'Sending to ' + name + ' in ' + seconds + 's';
  box.appendChild(label);
  const mk = (text, fn, bg) => {
    const b = document.createElement('button');
    b.textContent = text;
    b.style.cssText = 'border:0;border-radius:6px;padding:6px 10px;cursor:pointer;' +
      'font:inherit;color:#fff;background:' + bg;
    b.onclick = fn;
    box.appendChild(b);
    return b;
  };
  mk('Skip', () => { window.__snmLive.decision = 'skip'; }, '#b24020');
  const pause = mk('Pause', () => {
    window.__snmLive.paused = !window.__snmLive.paused;
    pause.textContent = window.__snmLive.paused ? 'Resume' : 'Pause';
  }, '#5e6b75');
  mk('Send now', () => { window.__snmLive.decision = 'send'; }, '#0a66c2');
  document.body.appendChild(box);
}
"""

_OVERLAY_STATE_JS = r"""
() => JSON.stringify(window.__snmLive || {decision: null, paused: false})
"""

_OVERLAY_TICK_JS = r"""
([id, name, seconds, paused]) => {
  const label = document.getElementById(id + '-label');
  if (label) label.textContent = paused
    ? 'Paused: ' + name + ' (' + seconds + 's left). Edit the message freely.'
    : 'Sending to ' + name + ' in ' + seconds + 's';
}
"""

_OVERLAY_REMOVE_JS = "(id) => document.getElementById(id)?.remove()"


@dataclass
class Session:
    url_hash: str
    campaign: str
    mode: str
    page_no: int = 1
    row_index: int = 0
    loaded_page: int | None = None
    current: dict[str, Any] | None = None
    opened: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    # The offer and drafting pointer go out with the first lead only.
    briefed: bool = False


_sessions: dict[tuple[str, str], Session] = {}


def get_session(url_hash: str, campaign: str) -> Session | None:
    return _sessions.get((url_hash, campaign))


def end_session(url_hash: str, campaign: str) -> None:
    _sessions.pop((url_hash, campaign), None)


# --------------------------------------------------------------- lead context


# Budgets for what the drafter reads per lead. Everything is trimmed here, in
# code, so a lead costs roughly the same small number of tokens every time.
_SUMMARY_CHARS = 600
_POSITION_CHARS = 300
_POST_CHARS = 350
_POSTS = 3


def _clip(text: Any, n: int) -> Any:
    if not isinstance(text, str):
        return text
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def lead_context(
    record: dict[str, Any],
    capture: dict[str, Any],
    company: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What the drafter sees, trimmed. Every field is citable in evidence_used.

    Contact info stays in the database; it does not help write the message.
    """
    posts, other = [], []
    for i in capture.get("insights") or []:
        kind = i.get("insight_type") or ""
        text = i.get("commentary") or i.get("article_title")
        if "SHARE" in kind or "POST" in kind:
            if text and len(posts) < _POSTS:
                posts.append({"type": kind, "text": _clip(text, _POST_CHARS)})
        elif i.get("position_change_title"):
            other.append(
                {
                    "type": kind,
                    "text": " at ".join(
                        x
                        for x in (
                            i.get("position_change_title"),
                            i.get("position_change_company"),
                        )
                        if x
                    ),
                }
            )
        elif i.get("total_count"):
            other.append({"type": kind, "text": f"{i['total_count']} shared"})
    positions = record.get("positions") or []
    current = positions[0] if positions and isinstance(positions[0], dict) else {}
    ctx = {
        "firstName": record.get("firstName"),
        "title": record.get("title"),
        "companyName": record.get("companyName"),
        "companyIndustry": record.get("companyIndustry"),
        "geoRegion": record.get("geoRegion"),
        "tenurePositionMonths": record.get("tenurePositionMonths"),
        "summary": _clip(record.get("summary"), _SUMMARY_CHARS),
        "positionDescription": _clip(current.get("description"), _POSITION_CHARS),
        "posts": posts,
        "signals": other,
        "company": company,
    }
    return {k: v for k, v in ctx.items() if v not in (None, "", [], {})}


def _validation_record(record: dict[str, Any], context: dict[str, Any]) -> dict:
    """The search record plus the drafter's context, for the evidence gate.

    Evidence may name any field the drafter was shown (`posts[0].text`,
    `company.priorities[1]`) or any field of the stored record.
    """
    return {**record, **context}


# ------------------------------------------------------------------- walking


async def _next_row(
    page: Any,
    session: Session,
    query_url: str,
    last_page: int,
    eligible: dict[str, dict[str, Any]],
    budget: int,
) -> tuple[dict[str, Any] | None, Any, int]:
    """Advance the session's cursor to the next eligible row.

    Returns (target, row, rows_examined); target is None when the budget ran
    out or the results are exhausted.
    """
    examined = 0
    on_search = "/sales/search/" in (getattr(page, "url", "") or "")
    while session.page_no <= max(1, last_page):
        if session.loaded_page != session.page_no or not on_search:
            url = (
                query_url
                if session.page_no <= 1
                else _with_page_param(query_url, session.page_no)
            )
            await page.goto(url, wait_until="domcontentloaded")
            rows = page.locator(_ROW_SEL)
            try:
                await rows.first.wait_for(state="attached", timeout=20000)
            except Exception:  # noqa: BLE001
                logger.warning("no result rows on page %d", session.page_no)
                session.page_no += 1
                session.row_index = 0
                continue
            await asyncio.sleep(_RESULTS_SETTLE_S)
            session.loaded_page = session.page_no
            on_search = True
        rows = page.locator(_ROW_SEL)
        count = await rows.count()
        while session.row_index < count:
            if examined >= budget:
                return None, None, examined
            row = rows.nth(session.row_index)
            session.row_index += 1
            examined += 1
            try:
                await row.scroll_into_view_if_needed(timeout=5000)
                href = await row.locator(_ROW_LEAD_LINK_SEL).first.get_attribute(
                    "href", timeout=5000
                )
            except Exception:  # noqa: BLE001 - an unrendered row is skipped
                continue
            target = eligible.get(_row_profile_id(href) or "")
            if target is not None:
                return target, row, examined
        session.page_no += 1
        session.row_index = 0
    return None, None, examined


# ------------------------------------------------------------------ public


async def next_lead(
    url_hash: str,
    campaign: str,
    mode: str,
    *,
    max_rows: int = 60,
    pacer: Pacer | None = None,
) -> dict[str, Any]:
    """Open the next eligible lead's panel and return what to draft from."""
    if mode not in MODES:
        return {"error": "bad_mode", "suggestion": f"mode must be one of {MODES}"}
    config = get_config().outreach
    store = get_store()
    if mode != "draft":
        if not config.enabled:
            return {
                "error": "sending_disabled",
                "suggestion": "Set ENABLE_SENDING=true, or use mode='draft'.",
            }
        if _remaining_today(store) <= 0:
            return {
                "error": "daily_cap_reached",
                "suggestion": f"The {config.daily_cap}/day cap is used up.",
            }
    query = store.get_query(url_hash)
    if query is None:
        return {"error": "unknown_query", "url_hash": url_hash}

    key = (url_hash, campaign)
    session = _sessions.get(key)
    if session is None or session.mode != mode:
        session = Session(url_hash, campaign, mode)
        _sessions[key] = session
    pacer = pacer or Pacer()

    browser = get_browser()
    async with browser.lock:
        page = await browser.get_page()
        if session.current is not None:
            # Moved on without submitting: close it, record nothing. The lead
            # stays eligible for a later session.
            await _close_panel(page)
            session.current = None

        eligible = store.live_candidates(url_hash, campaign)
        records = {
            int(r["memberId"]): r
            for r in store.iter_records(url_hash)
            if r.get("memberId") is not None
        }
        # The ICP screen runs on stored data before any panel is opened: a
        # lead it rejects costs no LinkedIn request and no model tokens.
        rules = load_rules(config.resolved_fit_rules_file())
        for pid, target in list(eligible.items()):
            reason = screen(records.get(int(target["member_id"])) or {}, rules)
            if reason is not None:
                eligible.pop(pid)
                store.record_outreach(
                    target["member_id"],
                    campaign,
                    "skipped",
                    entity_urn=target.get("entity_urn"),
                    last_error=reason,
                )
                session.skipped["fit rules"] = session.skipped.get("fit rules", 0) + 1
        budget = max_rows
        while eligible and budget > 0:
            target, row, examined = await _next_row(
                page, session, query.url, query.last_page, eligible, budget
            )
            budget -= examined
            if target is None:
                break
            eligible.pop(target["profile_id"], None)
            if session.opened:
                await pacer.dwell()
            session.opened += 1
            result = await capture_row(page, target, row, keep_open=True)
            store.record_capture(result)
            store.log_event(
                "capture",
                ok=result.get("error") is None,
                member_id=target["member_id"],
                campaign=campaign,
                http_status=result.get("http_status"),
                error=result.get("error"),
                detail=json.dumps(result.get("panel_status") or {}),
            )
            skip = _skip_reason(store, target, result)
            if skip is not None:
                await _close_panel(page)
                if not result.get("error"):
                    store.record_outreach(
                        target["member_id"],
                        campaign,
                        "skipped",
                        channel=None if skip != "not Open Profile" else CHANNEL_CREDIT,
                        entity_urn=target.get("entity_urn"),
                        last_error=skip,
                    )
                session.skipped[skip] = session.skipped.get(skip, 0) + 1
                continue

            record = records.get(int(target["member_id"])) or {}
            company = company_intel(
                store,
                company_id=(result.get("dossier") or {}).get("company_id")
                or record.get("companyId"),
                company_name=record.get("companyName"),
                api_key=config.tinyfish_api_key,
            )
            context = lead_context(record, result, company)
            session.current = {
                "target": target,
                "record": record,
                "context": context,
                "opened_at": time.time(),
            }
            countdown = COUNTDOWN.get(mode)
            out = {
                "status": "ready",
                "mode": mode,
                "member_id": target["member_id"],
                "lead": context,
                "daily_cap_remaining": _remaining_today(store),
                "skipped_so_far": dict(session.skipped),
                "next_step": "Draft, then outreach_submit."
                + (
                    f" {countdown}s in-page countdown before send."
                    if countdown
                    else " Draft mode: nothing is sent."
                ),
            }
            if not session.briefed:
                session.briefed = True
                out["briefing"] = _briefing(config)
            return out

    exhausted = not store.live_candidates(url_hash, campaign)
    return {
        "status": "done" if exhausted else "paused",
        "skipped_so_far": dict(session.skipped),
        "suggestion": (
            "No eligible leads left in this query for this campaign."
            if exhausted
            else f"Looked at {max_rows} rows without an eligible lead; call "
            "outreach_next again to keep going."
        ),
    }


def _briefing(config: Any) -> dict[str, Any]:
    """Sent once per session: the offer and how to draft. Not repeated per lead."""
    path = config.resolved_offer_file()
    offer = (
        path.read_text(encoding="utf-8")
        if path is not None and path.is_file()
        else None
    )
    return {
        "drafting": "Load the outreach-drafting skill before the first draft; "
        "it holds the message rules for every lead in this session.",
        "evidence": "evidence_used names fields of `lead`, e.g. 'title', "
        "'posts[0].text', 'company.priorities[1]', 'signals[0].text'.",
        "offer": offer or "No OFFER_FILE configured: set it before drafting.",
    }


def _skip_reason(
    store: Any, target: dict[str, Any], result: dict[str, Any]
) -> str | None:
    if result.get("error"):
        return "capture failed"
    if not (result.get("member_badges") or {}).get("openLink"):
        return "not Open Profile"
    if result.get("thread_messages"):
        return "existing conversation"
    if store.already_contacted(target["member_id"]):
        return "already contacted"
    return None


async def _panel_is_for(page: Any, full_name: str | None) -> bool:
    if not await _panel_visible(page, wait=1.0):
        return False
    if not full_name:
        return True
    try:
        text = await page.locator(
            'button:has-text("Close conversation")'
        ).first.inner_text(timeout=2000)
    except Exception:  # noqa: BLE001
        return False
    first = full_name.split()[0].lower()
    return first in (text or "").lower()


async def countdown(
    page: Any, name: str, seconds: float, *, tick: float = 0.5, sleep=asyncio.sleep
) -> str:
    """Run the in-page banner. Returns 'send', 'skip' or 'timeout'."""
    await page.evaluate(_OVERLAY_JS, [_OVERLAY_ID, name, math.ceil(seconds)])
    remaining = float(seconds)
    try:
        while True:
            state = json.loads(await page.evaluate(_OVERLAY_STATE_JS) or "{}")
            if state.get("decision") in ("send", "skip"):
                return state["decision"]
            if remaining <= 0:
                return "timeout"
            await sleep(tick)
            paused = bool(state.get("paused"))
            if not paused:
                remaining -= tick
            await page.evaluate(
                _OVERLAY_TICK_JS,
                [_OVERLAY_ID, name, max(0, math.ceil(remaining)), paused],
            )
    finally:
        with contextlib.suppress(Exception):
            await page.evaluate(_OVERLAY_REMOVE_JS, _OVERLAY_ID)


async def submit(
    url_hash: str,
    campaign: str,
    subject: str,
    body: str,
    evidence_used: list[str] | None,
    *,
    countdown_seconds: float | None = None,
) -> dict[str, Any]:
    """Fill the open panel with the draft, then save / count down / send."""
    session = _sessions.get((url_hash, campaign))
    if session is None or session.current is None:
        return {
            "error": "no_open_lead",
            "suggestion": "Call outreach_next first to open a lead.",
        }
    store = get_store()
    config = get_config().outreach
    current = session.current
    target, context = current["target"], current["context"]
    member_id = target["member_id"]
    vrecord = _validation_record(current["record"], context)

    problems = validate_message(subject, body, evidence_used, vrecord)
    prior = store.already_contacted(member_id)
    if prior:
        problems.append(f"already messaged in campaign {prior!r}")
    if problems:
        return {
            "status": "rejected",
            "member_id": member_id,
            "problems": problems,
            "suggestion": "Nothing was filled or sent. Fix and call outreach_submit "
            "again, or outreach_skip.",
        }

    browser = get_browser()
    async with browser.lock:
        page = await browser.get_page()
        if not await _panel_is_for(page, target.get("full_name")):
            session.current = None
            return {
                "error": "panel_gone",
                "member_id": member_id,
                "suggestion": "The message panel for this lead is no longer open. "
                "Call outreach_next to reopen the next lead.",
            }
        await page.fill(_SUBJECT_SEL, subject)
        await page.fill(_BODY_SEL, body)

        if session.mode == "draft":
            store.record_outreach(
                member_id,
                campaign,
                "drafted",
                channel=CHANNEL_FREE,
                entity_urn=target.get("entity_urn"),
                subject=subject,
                body=body,
                evidence_used=evidence_used,
            )
            await _close_panel(page)
            session.current = None
            return {"status": "drafted", "member_id": member_id, "sent": False}

        if not config.enabled:
            return {"error": "sending_disabled", "member_id": member_id}
        if _remaining_today(store) <= 0:
            return {"error": "daily_cap_reached", "member_id": member_id}
        channel = json.loads(await page.evaluate(_DETECT_CHANNEL))
        if not channel.get("free") and not config.allow_credit_spend:
            await _close_panel(page)
            store.record_outreach(
                member_id,
                campaign,
                "skipped",
                channel=CHANNEL_CREDIT,
                entity_urn=target.get("entity_urn"),
                last_error="panel shows a credit charge, not Free to Open Profile",
            )
            session.current = None
            return {"status": "skipped", "member_id": member_id, "why": "not free"}

        seconds = (
            countdown_seconds
            if countdown_seconds is not None
            else COUNTDOWN[session.mode]
        )
        decision = await countdown(page, target.get("full_name") or "lead", seconds)
        if decision == "skip":
            await _close_panel(page)
            store.record_outreach(
                member_id,
                campaign,
                "skipped",
                channel=CHANNEL_FREE,
                entity_urn=target.get("entity_urn"),
                subject=subject,
                body=body,
                last_error="skipped by reviewer",
            )
            session.current = None
            return {"status": "skipped", "member_id": member_id, "why": "reviewer"}

        # Whatever is in the panel now is what goes out -- including edits
        # made during the countdown -- so that is what gets checked.
        final_subject = await page.input_value(_SUBJECT_SEL)
        final_body = await page.input_value(_BODY_SEL)
        edited = (final_subject, final_body) != (subject, body)
        problems = validate_message(final_subject, final_body, evidence_used, vrecord)
        if problems:
            return {
                "status": "held",
                "member_id": member_id,
                "problems": problems,
                "suggestion": "Not sent; the panel is still open. Fix it and call "
                "outreach_submit again, or outreach_skip.",
            }
        error = await commit_send(
            page,
            store,
            member_id=member_id,
            campaign=campaign,
            channel=CHANNEL_FREE,
            subject=final_subject,
            body=final_body,
            evidence_used=evidence_used,
            entity_urn=target.get("entity_urn"),
        )
        session.current = None
        if error is not None:
            return {
                "status": "ambiguous",
                "member_id": member_id,
                "error": error,
                "suggestion": "Left as 'sending'. Run reconcile_outreach.",
            }
        return {
            "status": "sent",
            "member_id": member_id,
            "decision": decision,
            "edited_during_countdown": edited,
            "subject": final_subject,
            "daily_cap_remaining": _remaining_today(store),
        }


async def skip(url_hash: str, campaign: str, reason: str = "skipped") -> dict[str, Any]:
    """Close the open lead without sending and record why."""
    session = _sessions.get((url_hash, campaign))
    if session is None or session.current is None:
        return {"error": "no_open_lead"}
    target = session.current["target"]
    browser = get_browser()
    async with browser.lock:
        page = await browser.get_page()
        await _close_panel(page)
    get_store().record_outreach(
        target["member_id"],
        campaign,
        "skipped",
        entity_urn=target.get("entity_urn"),
        last_error=reason[:200],
    )
    session.current = None
    return {"status": "skipped", "member_id": target["member_id"], "why": reason}
