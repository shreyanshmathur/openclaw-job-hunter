---
name: jobhunter-write
description: Writes one researched, specific outreach message per person from stored facts, in the draft file format the QC gate checks.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Writing one message to one person

Read `__WS__/ref/writer_brief.md` before your first draft in a cycle; it is the full brief with channel limits and
the banned list. Tone per locale is in `__WS__/ref/tone_rules.json`. After a QC failure use `__WS__/ref/rewrite.md`.

## Before you write

1. Research first (skill jobhunter-research) and record facts with `research add`. You write only from stored facts:
   `research list --contact <id>` shows them with their fact ids and verbatim snippets.
2. Pick one hook, in this order: their own recent work relevant to the role; the team's stated need in the job post
   or team blog; a company event by its consequence; a shared school or employer their profile states. It must be
   specific, at most 180 days old (90 preferred), professional, and not a restatement of their job title.
3. No specific hook: do not write. Run `__PY__ __REPO__/scripts/jh.py outreach skip <target> --reason no_hook`.
4. Pick one proof point from the profile facts, with at most two numbers, copied exactly.

## The message

- Sentence 1 is about their hook, in their words. Then the proof. Then one small ask and an easy exit
  ("If someone else owns this hire, a name is plenty.").
- Channels and limits: email_cold 50 to 120 words, email_founder 40 to 110, email_recruiter 50 to 120,
  email_followup 15 to 80 (one only, adds one new thing), li_connect 90 to 180 characters and no link,
  li_message 150 to 500 characters with one question, inmail 200 to 400 characters.
- Recruiter: lead with the post and the level, state the notice period. Founder: their plan or problem, the proof,
  an ask with a fallback. Never ask a stranger for a referral; never mention salary.
- Plain text only. ASCII punctuation only: no dash characters, no hyphen with spaces around it, no curly quotes,
  no ellipsis, no emoji, no bullets, no markdown. Write ranges as "3 to 5".
- Sign off with one word on its own line ("Thanks,"). Code appends the signature. Never type a name, phone number
  or links as a signature.

## The draft file

Write it with your file tool to `__WS__/work/<cycle_id>/draft-<n>.json`:

```json
{"kind": "cold_email", "channel": "email_cold", "contact_uid": "P3KQ7M2A", "job_uid": "J7Q2KX4M",
 "subject": "25 micro-warehouses by June",
 "body": "Hi Nina,\n\nHarbor Parcel's August post says ...\n\nBest,",
 "hook": {"anchor": "25 new micro-warehouses", "source_type": "company_blog",
          "source_url": "https://harborparcel.example/blog/seed",
          "snippet": "our seed round will fund 25 new micro-warehouses by June 2027",
          "published_at": "2026-08-20", "retrieved_at": "2026-09-24", "fact_id": "R4MZ2Q7K"},
 "claims": [{"text": "18 micro-warehouses in 10 months, damaged-parcel rate under 2%", "fact_id": "P1"}],
 "links": []}
```

- `kind`: cold_email, followup_email (give `thread_key`, no contact or job), li_invite_note, li_message,
  li_followup (give `thread_key`), inmail (needs a subject).
- `channel` for cold_email: email_cold (hiring manager), email_recruiter or email_founder.
- The snippet must be the stored snippet, character for character; code replaces the evidence fields with the
  stored ones and refuses a different snippet.
- Every number in the body must appear in a fact (10, 15, 20 and 30 minute call lengths are allowed).

Then follow skill jobhunter-qc-loop: `draft create`, review, rewrite or drop, and wait for approval.
