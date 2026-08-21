# Offer — example

Copy this to a file of your own (e.g. `offer.md`, which is gitignored) and
point `OFFER_FILE` at it in `.env`. Nothing in this file is used unless you do.

This file is read at prompt-render time and never leaves your machine. Keep the
real one out of git — it is your positioning, not the tool's.

Be specific. Vague input here produces vague messages, and a vague message to a
stranger is just spam with better grammar.

---

## What we sell

One paragraph, plain language, no adjectives you cannot defend. What the thing
does, for whom, and what changes for them.

> Example: We clean and de-duplicate consumer mailing files before they go to
> print. Typical client is a direct mail house running 200k+ pieces a month
> where returned mail is eating margin.

## Who it is for

The specific role and situation. Just as important: who it is NOT for, so the
model does not stretch to fit a bad lead.

> Good fit: whoever owns data quality at a mailing house or direct mail agency.
> Not a fit: brand-side marketers who buy mail as a service.

## Proof points

Three to five, each with a number. These are the only credibility claims that
may appear in a message — if it is not here, it cannot be said.

> - Cut undeliverable rate from 6.2% to 1.4% for a 400k/month mailer
> - NCOA + CASS in under 4 hours on a 1M-record file
> - Used by 30+ mailing houses in the US

## Objections to pre-empt

What they are thinking when they read the first line.

> - "We already run NCOA." — We are the pass *after* that; NCOA misses movers
>   who did not file.
> - "We have a vendor." — Fine. We benchmark against them for free on one file.

## Tone

> Direct. Operator to operator. No marketing voice. Short sentences. Assume
> they know their own business better than we do.

## Banned phrases

Beyond the ones the server already rejects, add anything that sounds like you
but shouldn't.

> - "solutions"
> - "leverage"
> - "in today's landscape"

## Subject line patterns that have worked

> - "{{specific number}} on your {{specific process}}"
> - "question about {{thing they actually do}}"

## Hard constraints

> - Under 150 words
> - No links in the first message
> - One ask, answerable in a sentence
