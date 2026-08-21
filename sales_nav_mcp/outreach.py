"""Sending messages — the only capability here that writes to LinkedIn.

Everything else in this server reads. That difference is the whole design:
a burst of reads looks like someone browsing, a burst of identical messages
looks like exactly what it is, and the penalty lands on the account rather
than on the code. So the defaults are all the cautious ones — disabled,
dry-run, free channel, long delays, hard daily cap.

## The compose UI, as observed

Both Open Profile and credit-consuming InMail render the *same* form. The only
difference is a line of text, which is what makes the channel machine-readable:

    Open Profile      ->  "Free to Open Profile"
    InMail credit     ->  "Use 1 of 92 credits"

Field selectors (verified against two different leads):

    subject : input[aria-label="Subject (required)"]
    body    : textarea[name="message"]
    send    : the "Send" button, disabled until both are non-empty

Element **ids are Ember-generated and differ per render** (`...ember201` vs
`...ember184`), so nothing here may select by id. That is the one mistake that
would pass every test and fail in production.

## The evidence gate

The composing model returns `evidence_used`: the record fields it drew on. Each
one must resolve against the lead record actually fetched, or the message is
rejected unsent. It is a cheap, deterministic check that a "personalized"
message is grounded in data we really have, rather than in a plausible
invention. A message claiming a conference talk gets rejected because no field
supports it.
"""

import asyncio
import json
import logging
import random
import re
import time
from typing import Any

from sales_nav_mcp.browser import get_browser
from sales_nav_mcp.config import get_config
from sales_nav_mcp.exceptions import SalesNavMCPError
from sales_nav_mcp.store import get_store

logger = logging.getLogger(__name__)

CHANNEL_FREE = "open_profile"
CHANNEL_CREDIT = "inmail_credit"

# Phrases that mark a message as machine-written. Rejected before sending.
BANNED_PHRASES = (
    "i hope this finds you well",
    "i hope this email finds you",
    "i came across your profile",
    "i stumbled upon your profile",
    "as an ai",
    "i wanted to reach out because i noticed",
    "quick question for you",
    "circle back",
    "touch base",
    "synergy",
)

_SUBJECT_SEL = 'input[aria-label="Subject (required)"]'
_BODY_SEL = 'textarea[name="message"]'


class SendingDisabledError(SalesNavMCPError):
    """Raised when sending is attempted but not enabled in config."""


class MessageRejected(SalesNavMCPError):
    """Raised when a drafted message fails validation. Nothing was sent."""


# ---------------------------------------------------------------- validation


def flatten_record(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a lead record to dotted paths, for evidence resolution.

    `positions[0].title` and `companyName` both become addressable keys, so
    `evidence_used` can name exactly what it drew on.
    """
    flat: dict[str, Any] = {}
    for key, value in record.items():
        if key == "_raw":
            continue
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten_record(value, f"{path}."))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    flat.update(flatten_record(item, f"{path}[{i}]."))
                else:
                    flat[f"{path}[{i}]"] = item
        else:
            flat[path] = value
    return flat


def validate_message(
    subject: str,
    body: str,
    evidence_used: list[str] | None,
    record: dict[str, Any],
) -> list[str]:
    """Return a list of problems. Empty list means the message may be sent."""
    config = get_config().outreach
    problems: list[str] = []

    if not subject or not subject.strip():
        problems.append("subject is empty")
    if not body or not body.strip():
        problems.append("body is empty")
    if len(subject) > config.subject_max_chars:
        problems.append(
            f"subject is {len(subject)} chars, limit is {config.subject_max_chars}"
        )
    if len(body) > config.body_max_chars:
        problems.append(f"body is {len(body)} chars, limit is {config.body_max_chars}")

    haystack = f"{subject}\n{body}".lower()
    for phrase in BANNED_PHRASES:
        if phrase in haystack:
            problems.append(f"banned phrase: {phrase!r}")

    if not evidence_used:
        problems.append(
            "evidence_used is empty — a message with no grounding in the "
            "lead's own data is not personalized, it is a template"
        )
    else:
        flat = flatten_record(record)
        for key in evidence_used:
            base = key.split(":", 1)[0]
            if base not in flat:
                problems.append(
                    f"evidence_used names {key!r}, which is not a field of the "
                    "fetched lead record — the claim is unsupported"
                )
    return problems


# ------------------------------------------------------------- compose prompt


def build_compose_prompt(lead_json: str) -> str:
    """Render the drafting prompt: your offer + this lead + the rules.

    The offer text is read from OFFER_FILE at render time. It is never bundled,
    never committed, and never sent anywhere by this server — the prompt goes
    to the MCP client, which is already running on your machine.

    With no offer file configured this returns an explanation instead of a
    prompt, so a public install cannot be pointed at a stranger and asked to
    sell something.
    """
    offer_path = get_config().outreach.resolved_offer_file()
    if offer_path is None or not offer_path.is_file():
        return (
            "Cannot draft a message: no offer file is configured.\n\n"
            "Set OFFER_FILE in .env to a Markdown file describing what you "
            "sell, who it is for, and your proof points. Start from "
            "offer.example.md in the repo. This server ships without one on "
            "purpose \u2014 it has nothing to sell until you tell it."
        )
    offer = offer_path.read_text(encoding="utf-8")
    return f"""You are drafting ONE Sales Navigator message. It will be read by a
real person who did not ask to hear from you, so it has to earn its place.

## What we sell

{offer}

## The lead

{lead_json}

## Rules

1. Ground every specific claim in the lead data above. Do not infer a
   conference, a mutual contact, or a project that is not in the record.
2. Use at most TWO specifics. More reads as surveillance, not attention.
3. No flattery openers. No "I came across your profile". No "I hope this
   finds you well". These are rejected automatically.
4. One ask, and make it small. A question they can answer in a sentence beats
   a meeting request.
5. Subject: under 60 characters, specific, not clickbait.
6. Body: under 150 words. Short is respectful.
7. Write like one person emailing another. Contractions are fine.

## Output

Return JSON only:

{{"subject": "...", "body": "...", "evidence_used": ["positions[0].title", "companyName"]}}

`evidence_used` must list the exact record fields you drew on. Every entry is
checked against the lead record; naming a field you did not use, or one that
does not exist, rejects the message unsent.
"""


# ------------------------------------------------------------------- sending


def _remaining_today(store: Any) -> int:
    """Daily cap remaining, counted over a rolling 24h across all campaigns."""
    cap = get_config().outreach.daily_cap
    used = store.sent_since(time.time() - 24 * 3600)
    return max(0, cap - used)


_DETECT_CHANNEL = r"""
() => {
  const text = document.body.innerText || '';
  const free = /free to open profile/i.test(text);
  const credit = text.match(/use\s+1\s+of\s+(\d+)\s+credits/i);
  return JSON.stringify({
    free,
    credit: !!credit,
    credits_remaining: credit ? parseInt(credit[1], 10) : null,
  });
}
"""


async def _open_compose(page: Any, entity_urn: str) -> dict[str, Any]:
    """Open the message composer for a lead and report the channel."""
    m = re.search(r"\(([^)]+)\)", entity_urn or "")
    if not m:
        raise MessageRejected(f"cannot parse lead reference from {entity_urn!r}")
    await page.goto(
        f"https://www.linkedin.com/sales/lead/{m.group(1)}",
        wait_until="domcontentloaded",
    )
    await asyncio.sleep(4)
    # The Message control carries no aria-label; match on its text.
    await page.get_by_role("button", name="Message").first.click(timeout=15000)
    await asyncio.sleep(3)
    return json.loads(await page.evaluate(_DETECT_CHANNEL))


async def send_message(
    member_id: int,
    campaign: str,
    subject: str,
    body: str,
    *,
    evidence_used: list[str] | None = None,
    dry_run: bool = True,
    url_hash: str | None = None,
) -> dict[str, Any]:
    """Validate, then (unless dry_run) actually send one message.

    Refuses on: sending disabled, daily cap reached, the person already
    contacted in any campaign, failed validation, or a credit-consuming
    channel when credit spend is not explicitly allowed.
    """
    config = get_config().outreach
    store = get_store()

    record = None
    for rec in store.iter_records(url_hash) if url_hash else []:
        if rec.get("memberId") and int(rec["memberId"]) == int(member_id):
            record = rec
            break
    if record is None:
        raise MessageRejected(
            f"member_id {member_id} is not in query {url_hash!r} — refusing to "
            "message someone we have no stored record for"
        )

    problems = validate_message(subject, body, evidence_used, record)
    prior = store.already_contacted(member_id)
    if prior:
        problems.append(f"already messaged in campaign {prior!r} — never contact twice")
    remaining = _remaining_today(store)
    if remaining <= 0:
        problems.append(
            f"daily cap of {config.daily_cap} reached; resets on a rolling 24h"
        )

    verdict: dict[str, Any] = {
        "member_id": member_id,
        "campaign": campaign,
        "full_name": record.get("fullName"),
        "subject": subject,
        "body": body,
        "evidence_used": evidence_used or [],
        "problems": problems,
        "daily_cap_remaining": remaining,
        "would_send": not problems,
        "dry_run": dry_run,
        "sent": False,
    }

    if problems:
        verdict["suggestion"] = (
            "Not sent. Fix the problems listed and call again. Nothing was "
            "written to LinkedIn."
        )
        return verdict

    if dry_run:
        verdict["suggestion"] = (
            "Dry run — nothing sent. This is exactly what would go out. "
            "Re-call with dry_run=false to send it."
        )
        return verdict

    if not config.enabled:
        raise SendingDisabledError(
            "Sending is disabled. Set ENABLE_SENDING=true in .env to allow it. "
            "This server ships with sending off so a fresh install cannot "
            "message anyone."
        )

    browser = get_browser()
    async with browser.lock:
        page = await browser.get_page()
        channel_info = await _open_compose(page, record.get("entityUrn") or "")
        channel = CHANNEL_FREE if channel_info.get("free") else CHANNEL_CREDIT
        verdict["channel"] = channel
        verdict["credits_remaining"] = channel_info.get("credits_remaining")

        if channel == CHANNEL_CREDIT and not config.allow_credit_spend:
            store.record_outreach(
                member_id,
                campaign,
                "skipped",
                channel=channel,
                last_error="would spend an InMail credit; ALLOW_CREDIT_SPEND is false",
            )
            verdict["sent"] = False
            verdict["problems"] = ["would consume an InMail credit"]
            verdict["suggestion"] = (
                "Skipped: this lead is not Open Profile, so the message would "
                "spend one of your finite InMail credits. Set "
                "ALLOW_CREDIT_SPEND=true only if you mean to."
            )
            return verdict

        await page.fill(_SUBJECT_SEL, subject)
        await asyncio.sleep(random.uniform(0.6, 1.6))
        await page.fill(_BODY_SEL, body)
        await asyncio.sleep(random.uniform(0.8, 2.0))

        try:
            await page.get_by_role("button", name="Send", exact=True).click(
                timeout=15000
            )
            await asyncio.sleep(3)
        except Exception as e:
            store.record_outreach(
                member_id,
                campaign,
                "failed",
                channel=channel,
                subject=subject,
                body=body,
                evidence_used=evidence_used,
                last_error=str(e)[:400],
                bump_attempts=True,
            )
            verdict["problems"] = [f"send click failed: {e}"]
            verdict["suggestion"] = (
                "Recorded as failed and left pending; it will be retried."
            )
            return verdict

        # Commit before pacing: a crash during the delay must not lose the
        # fact that LinkedIn already has this message.
        store.record_outreach(
            member_id,
            campaign,
            "sent",
            channel=channel,
            subject=subject,
            body=body,
            evidence_used=evidence_used,
            bump_attempts=True,
        )
        verdict["sent"] = True
        verdict["suggestion"] = (
            f"Sent via {channel}. {remaining - 1} left under today's cap."
        )
        await asyncio.sleep(
            random.uniform(config.delay_min_seconds, config.delay_max_seconds)
        )

    return verdict
