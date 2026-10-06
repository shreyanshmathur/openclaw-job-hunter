# Writing rules and the QC gate

Every piece of text the job hunter sends passes the same gate before it leaves: cold emails, follow-ups, application
emails, cover notes, LinkedIn notes and messages, InMail, free-text form answers, the application package and every
resume that is uploaded or attached. The gate has two independent stages and a human (or `auto`) approval:

1. **Deterministic linter** (code, `scripts/jobhunter/qc/lint.py`). It checks the exact text that will be sent.
2. **Independent reviewer** (a separate agent, `jobhunter-qc`, with no tools and its own model). It never writes and
   the writer never sees its reasoning, only the stored verdict.
3. **Approval.** In `human` mode (the default) you approve, skip or edit each item in chat, in the Sheet or in the
   terminal. In `auto` mode an item that passed both stages is approved by code, except the categories that always
   need you.

A draft gets three attempts (the first text and two rewrites). After the third failure it is dropped, never sent, and
the person or job is skipped for 30 days. Expect 10 to 30% of drafts to be dropped in the first weeks; a gate that
never drops is not checking hard enough.

## What good looks like

- One recipient, one hook, one proof point, one ask. The first sentence is about something the reader did or said.
- The hook is specific, verifiable (stored URL and verbatim snippet), at most 180 days old (90 preferred) and about
  their work. Never personal life, never "saw you viewed my profile", never their own job title read back to them.
- Every statement about you comes from a confirmed profile fact, copied exactly. Every number in a message appears
  in a stored fact (call lengths of 10, 15, 20 and 30 minutes are allowed).
- Plain words, short sentences of mixed length, no ceremony, one small ask plus an easy exit.
- ASCII punctuation only. No en dash, no em dash, no other dash characters, no hyphen with spaces around it used as a
  dash, no curly quotes, no ellipsis, no emoji, no bullets, no markdown. Ranges are written "3 to 5".

Channel sizes (the linter blocks above the hard maximum and warns outside the target):

| Channel | Target | Hard maximum | Questions |
|---|---|---|---|
| Cold email (hiring manager, recruiter) | 50 to 120 words | 150 words | 1 to 2 |
| Cold email (founder) | 40 to 110 words | 140 words | 1 to 2 |
| Follow-up email (one per thread) | 15 to 80 words | 100 words | 0 to 1 |
| Application email, cover note | 80 to 200 words | 250 words | 0 to 1 |
| LinkedIn connection note | 90 to 180 characters | 200 characters | 0 to 1 |
| LinkedIn message | 150 to 500 characters | 700 characters | 1 to 2 |
| LinkedIn follow-up | 60 to 350 characters | 450 characters | 0 to 1 |
| InMail | 200 to 400 characters | 800 characters | 1 to 2 |
| Form answer | 25 to 150 words, at most 95% of the field | 250 words | none |

## The linter rules

Pass condition: no block and at most 2 warnings. Phrase lists live in `qc/banned_phrases.json`.

| Rule ids | What they catch | Severity |
|---|---|---|
| C-DASH, C-SPACED-HYPHEN | any Unicode dash (U+2010 to U+2015, U+2212, U+2E3A, U+2E3B, U+FE31, U+FE32, U+FE58, U+FE63, U+FF0D); a hyphen with spaces around it, or two hyphens | block, never auto-fixed |
| C-CURLY, C-ELLIPSIS, C-INVISIBLE, C-EMOJI, C-BULLET, C-MARKDOWN | typography that marks generated or pasted text | block |
| C-NON-ASCII | any other non-ASCII character; accented Latin letters are allowed only inside the names of the people, companies and cities involved | block |
| C-EXCLAMATION, C-ALLCAPS | more than one "!", any "!" in a subject or connection note; shouting words | block, warn |
| P-PLACEHOLDER | template residue: braces, square brackets, angle-bracket names, TODO, "Hi ,"; placeholder links and addresses (the example config's `you@example.com` and `linkedin.com/in/your-handle`, any `your-handle` style slug, a LinkedIn `/in/` link with no handle) in the subject, body, form values, resume text and the signature appended by code | block |
| B-OPENER, B-AI_VOCAB, B-CLOSER, B-PRESSURE, B-INDIA, B-JOBSEEKER | stock openers, AI vocabulary and puffery, stock closers, pressure and guilt, dated regional phrasing, job-seeker cliches | block |
| W-SOFT | hedges and intensifiers (just, really, very, truly, robust, crucial, ...) | warn; more than 2 block |
| S-AS-A, S-NEG-PARALLEL, S-COLON-REVEAL, S-ING-TAIL, S-SUMMARY-CLOSE | "As a ...", "not just X but Y", "Here's the thing", ", highlighting ...", "Overall," | block |
| S-TRIPLET, S-UNIFORM-RHYTHM, S-LONG-SENTENCE, S-I-HEAVY, S-SELF-FIRST, S-SEMICOLONS, S-COLONS, S-PARENS, S-LONG-PARAGRAPH | rhythm and structure tells | warn (two triplets or a sentence over 35 words block) |
| L-TOO-LONG, L-OFF-TARGET, L-FIELD-LIMIT, L-QUESTIONS, L-LINKS, L-LINK-SHORTENER-OR-TRACKING, L-LINK-NOT-ALLOWED, L-SUBJECT-* | size, questions, links (only your own link hosts), subject line shape | block (L-OFF-TARGET warns) |
| I-GREETING-NAME, I-WRONG-NAME, I-COMPANY-MISSING, I-WRONG-COMPANY | wrong or missing name, another company from the same day's batch | block (company missing warns) |
| H-* | hook evidence: missing fields, bad URL or type, anchor not in both the stored snippet and the text, older than 180 days, research older than 14 days at send time, instruction-like text in the source | block (90 to 180 days warns) |
| F-UNTRACED-NUMBER, F-CLAIM-NO-FACT, F-CLAIM-NUMBER-MISMATCH, F-NO-CLAIMS | numbers and claims that do not trace to a stored fact | block (no claims warns) |
| U-SIMILAR | the text or its opening is more than 60% similar (character trigrams) to a text sent in the last 30 days | block |
| O-OPTOUT-FIXED | the same opt-out sentence was already used in the last 30 days | block |
| L-DEDUP-* | the ledger already has a first touch to this person, a recent email to this company, an application for this job, the thread's one follow-up, a skip, an exclusion or a do-not-contact | block |
| R-* | resumes: every bullet maps to a base bullet, numbers only from that bullet, no new skill, title, date, employer, contact or education change, page limit, and every character rule on the text | block |
| A-* | application packages: every field equals your stored answer, no unconfirmed or sensitive value you did not give, EEO fields only with your stored choice, free-text answers only from drafts that passed QC, the resume file and hash equal the approved variant | block |

## The reviewer

The reviewer gets the exact text (with the signature and the attachment name and hash), the recipient summary, the
research facts with their URLs and snippets, your profile facts and the lint warnings. It must answer with a JSON
verdict that echoes a one-time nonce and the text's hash; anything else counts as a fail. Code recomputes the score
and the pass rule and the stricter of model and code wins:

- gates: truthful, hook_verified, swap_test (true when the text would not work for someone else), no_ai_voice, safe;
  each is true when the draft passes that check, all must be true, and any claim marked unsupported fails truthful;
  the packet carries the review date, and the hook's age is measured against it;
- specificity, value and human voice at least 4, nothing below 3, weighted score at least 4.0;
- resumes and packages: truthful and safe, clarity and channel fit at least 4.

A reviewer error or timeout is not a verdict: the review is retried once, then the draft waits for a later cycle
without using a rewrite. The reviewer prompt (`prompts/reviewer.md`) and the reviewer's `AGENTS.md` are hashed at
install; if either changes, reviews stop with `E_REVIEWER_TAMPERED` and you get an alert.

## Approving, skipping and editing

A notification looks like this (fictional):

```
Approve A7K2? Cold email to Alex R. (Head of Analytics), Kestrel Commerce
Why this person: pincode-level models (linkedin_post, 2026-09-18)
Subject: Pincode-level RTO models
---
<the exact text>
---
QC 4.35/5. Reply "/jh approve A7K2", "/jh skip A7K2", or "/jh edit A7K2 <your text>". Expires in 72 h.
```

- `/jh approve <code>`, the Sheet's Approvals tab, or `./jobhunter approve <code>` (PIN) approve the exact text shown.
  Codes are never reused within 30 days, so a late reply can never approve a different draft; a code from before an
  edit stops working and the reply names the new one.
- `/jh skip <code> [reason]` skips it; the person is not picked again for 30 days.
- `/jh edit <code> <your text>` (or `./jobhunter edit <code> --text-file <f>`) replaces the text with yours. For an
  email you may start with a `Subject: ...` line and a blank line. Your edit is linted first; if it fails you get the
  findings in plain words ("Your edit has a dash character ... (line 2)") and the previous text stays waiting. A clean
  edit goes to the reviewer. If the reviewer only objects to voice or genericness you may still approve it; if it
  finds something untrue or unsafe you cannot approve it, but you can edit again or skip. You have 3 edits per item
  and they never use the writer's attempts. The model never rewrites your words.
- Unanswered items expire after 72 hours (`approval.approval_ttl_hours`).

In `auto` mode (only through `./jobhunter approval auto`, PIN, after the golden check) these still come to you:
anything answering a positive or ambiguous reply, anything that mentions salary, referral asks, forms with sensitive
fields, LinkedIn items while `approval.per_channel.linkedin` is `human`, and every text you edited yourself.

## Calibration (golden set)

`qc/golden/` holds 20 fictional drafts, 10 good and 10 subtly bad (generic hook, inflated ownership, restated job
title, stacked hooks, flattery, wrong tone, mirrored sentences, vague value, personal details, a referral ask to a
stranger). All 20 pass the linter, so only the reviewer can tell them apart. `./jobhunter qc golden` (PIN) runs them
and stores the agreement in `meta.golden_last`; `approval auto` needs at least 18 of 20 on the current reviewer
model and prompt. The set is reviewed as of its fixed `as_of` date (in `qc/golden/labels.json`), so its fictional
hooks never age past the 180-day limit.

## Where to look

- `./jobhunter approvals` (or `jh.py approvals list`): what waits for you.
- The Sheet's QC log tab: one row per attempt with the lint findings, the reviewer score, the lowest criterion, the
  failed gates and the top issue.
- `jh.py draft show <draft_uid or code> --field send_text`: the exact text that was approved or will be sent.
