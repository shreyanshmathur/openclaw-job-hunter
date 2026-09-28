# How it works

## The pieces

| Piece | What it is |
|---|---|
| `scripts/jh.py` | One command line program (Python standard library) that owns every rule: duplicates, limits, pacing, QC, approvals, exclusions, breakers. Agents only ever run this program. |
| `state/jobhunter.sqlite3` | The single source of truth: jobs, evaluations, contacts, drafts, QC results, every action, threads, breakers, counters. Unique indexes and triggers refuse duplicates even if a bug slips through. |
| Five OpenClaw agents | `jobhunter-scout` (finds jobs in logged-in sites, read only), `jobhunter-evaluator` (scores jobs), `jobhunter-applier` (tailors resumes, fills application forms), `jobhunter-outreach` (researches people and companies, writes emails, reads replies), `jobhunter-qc` (an independent reviewer with no tools at all). |
| `jobhunter-guard` plugin | Runs inside OpenClaw and decides every tool call of those five agents before it happens. |
| Automations | OpenClaw cron jobs declared in `openclaw/crons.json`. Command jobs run code with no model; agent jobs are started only by the dispatcher. |
| The `jobhunter` browser profile | OpenClaw's separate browser profile the agents use. It holds only the logins of the sites you allowed (`./jobhunter browser consent`, recorded in `private/consent.json`); your normal Chrome is never driven. |
| Your Google Sheet | A readable mirror of the database, rebuilt at any time. |

## One job, from found to applied

```
discover -> normalize and dedup -> pre-filter -> evaluate -> tailor and write -> QC -> approve -> reserve -> act -> verify -> confirm -> Sheet and chat
```

1. **Discover.** Every six hours a command job reads public company job APIs (Greenhouse, Lever, Ashby and
   others) and remote job boards. The scout agent adds jobs from logged-in sites you turned on.
2. **Normalize and dedup.** Every job gets a canonical key from its URL and the ATS job id, so the same job seen
   on three sites is one row.
3. **Pre-filter in code.** Hard rules from your confirmed profile (roles to avoid, seniority, location,
   authorization, exclusions) reject obvious mismatches without spending a model turn. They show up in the
   Sheet under Skipped by filters, with the reason.
4. **Evaluate.** The evaluator scores each remaining job against your profile with a fixed scorecard and quotes
   the job text for each claim. Good fits go to the apply queue.
5. **Tailor and write.** The applier builds a truthful resume variant (it may reorder and rephrase, never add a
   fact) and prepares form answers from your answer bank. The outreach agent researches one person and the
   company, then writes a short email that could only be sent to that person. When no published address exists
   and you turned on the optional email finder, code asks your own free-tier providers for that one person's
   work address first ([EMAIL-FINDER.md](EMAIL-FINDER.md)).
6. **QC.** A deterministic linter checks every draft (no dashes, no filler phrases, no placeholders, length,
   every number traceable to your profile), then the separate reviewer agent scores it. Two rewrites at most,
   then the item is dropped.
7. **Approve.** In `human` mode you approve in chat, in the Sheet or in the terminal. No agent can approve.
8. **Reserve.** Code checks everything again at the last moment (pause, breakers, your consent for the site,
   limits, pacing, duplicates, exclusions, a fresh check of your Sent folder or the page) and hands out a one
   time token.
9. **Act.** Email (default route): the agent types the approved email into Gmail in its own browser profile,
   reads the To, Subject and body back, and code compares them with the approved text before the single allowed
   click; the send is then confirmed in the Sent folder. Email on the optional app password route: code sends
   the approved bytes over Gmail's SMTP. Forms: the agent types the approved text, reads it back, and code
   compares it with the approved text before the single allowed click.
10. **Verify and confirm.** The result is recorded; anything uncertain stays blocked (it is never retried
    automatically), so a crash can never cause a second send.
11. **Mirror.** The Sheet updates every 20 minutes; important events reach you in chat.

## The guard

The guard plugin enforces, inside OpenClaw:

- agents may only run `jh.py` commands allowed for them, with arguments of the expected shape;
- file reads only inside the agent's own workspace, file writes only in its `work/` and `inbox/` folders;
- browser typing only while the agent holds a live send token, and the final click only after the typed text
  was read back and matched;
- Google Sheets, Drive, Apps Script and account settings pages are never opened by an agent;
- a login site you have not allowed is never opened by an agent (the same consent file the gate reads);
- every page is scanned for stop signs (CAPTCHA, verification, limit notices); a match trips a breaker and ends
  the cycle even if the model ignores it.

The rules in plain words: [ENFORCEMENT.md](ENFORCEMENT.md).

## When things run

A dispatcher (every 10 minutes, no model) plans each day's cycles at random times inside each lane's window and
starts an agent only when there is work for it. So runs are never at the same minute every day, nothing is spent
on empty queues, and a pause is one check in one place. Browser lanes never overlap.

| Automation | How often | Model |
|---|---|---|
| Dispatcher | every 10 minutes | none |
| Job APIs | every 6 hours | none |
| Mailer (sends approved email, reads replies) | every 5 minutes | none |
| QC worker | every 5 minutes | reviewer agent |
| Sheet sync | every 20 minutes | none |
| Notifications | every 5 minutes | none |
| Digest | 09:00 and 19:00 | none |
| Housekeeping and audit | 03:10 | none |
| Scout, evaluator, applier, outreach, replies | planned by the dispatcher | agents |

## Stops

A stop signal (CAPTCHA, verification, restriction notice, an expired session, HTTP 429, bounce, spam complaint,
identity mismatch, an audit finding, a site consent you took back) opens a breaker for that platform or for
everything. Work in that area stops, you get a
message, and only you can reset it with `./jobhunter breaker reset <scope>`, not before a minimum cooldown.

## Your data

See [PRIVACY.md](PRIVACY.md).
