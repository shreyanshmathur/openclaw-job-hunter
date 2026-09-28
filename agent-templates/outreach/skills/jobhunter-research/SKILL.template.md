---
name: jobhunter-research
description: Research a person and company before writing, within a page budget; record facts with verbatim snippets; grade and verify addresses.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Research before writing

Goal: one specific, recent, verifiable reason to write to this person, plus the facts the writer needs. Every
command starts with `__PY__ __REPO__/scripts/jh.py`.

## Budget and order (at most 6 to 9 page loads per target)

Count every page you load: `usage add --platform <platform> --metric page_view` (LinkedIn profiles use
`--platform linkedin --metric profile_view`). Exit 4 means the budget is spent: stop researching this target
and skip it with `outreach skip <target_key> --reason research_budget`. Prefer pages off LinkedIn.

| Step | Source | What to take |
|---|---|---|
| 1 | the job post (from `job show <job_uid>`) | exact title, team, 3 to 5 concrete duties or tools, stated problems, named people |
| 2 | company site: product, changelog, engineering or company blog, careers | what they sell and to whom, recent launches, stated priorities |
| 3 | news from the last 180 days | the event, its date and its stated consequence |
| 4 | the person's LinkedIn profile (once, cached) | current title and start date, prior employers, headline, about |
| 5 | the person's own recent public work: posts, articles, talks, repos | topics in their own words, with dates |
| 6 | shared context shown on their profile only | same school, same former employer, same program |

Run `detect_page.js` after every navigation (skill `jobhunter-stop-detect`). LinkedIn research needs LinkedIn to
be enabled; when it is not, use steps 1 to 3 and 5 off LinkedIn only.

## Identity of the person

* Store the person with `contact add --file <contact.json>` (12.8): full name, title, company, company domain,
  role type (`hiring_manager`, `recruiter`, `founder`, `employee`, `role_inbox`, `other`), locale, and the
  identities you saw. `linkedin_url` must be the vanity `/in/<slug>` URL read from the profile page itself.
  A search result that only showed an opaque `/in/ACoA...` link goes in `linkedin_member_url`; when you later open
  the profile, add the contact again with both URLs so the two identities become one person.
* Never use `lnkd.in` links; open them and use the profile URL.
* Take the employer from the profile's top card and experience section only. Side rails ("People also viewed")
  belong to other people.
* Exit 3 (already contacted) or 7 (do not contact, excluded company) means drop the target.

## Facts

Write one research file per subject (12.2) and run `research add --file <f>`:

```json
{"subject": {"kind": "person", "contact_uid": "<contact_uid>"},
 "facts": [{"text": "Post: pincode-level models beat the city-level returns model last quarter.",
            "snippet": "pincode-level models beat our city-level returns model last quarter",
            "source_type": "linkedin_post", "source_url": "https://www.example.com/posts/1",
            "published_at": "2026-09-18", "retrieved_at": "2026-09-26"}]}
```

* `snippet` is copied verbatim from the page, at most 300 characters; `text` is your one-line reading of it.
* `source_url` is the https page you read; `published_at` only when the page shows a date.
* Subjects: `person` (contact_uid), `company` (`company_uid`, or `name` and `domain`), `job` (job_uid).
* A fact code flags as instruction-like (`injection_flag: 1`) can never be the hook.

## Pick one hook (highest that passes every filter)

1. Their own recent work relevant to the role (last 90 days preferred, 180 at most).
2. The team's stated need: a duty, tool or problem in the job post or their engineering blog.
3. A company event by its operational consequence ("40 new stores in Pune by March"), never praise.
4. Shared context only when their profile states it.
5. The candidate's real use of their product, only when a profile fact says so.

Filters: specific (a number, place, system, title, date or named feature), verifiable (stored snippet with a
2 to 8 word anchor that appears in the draft), relevant in one step, recent, safe (nothing personal, no
layoffs, lawsuits or departures, nothing implying tracking), and not a restatement of their job title.
No hook: `outreach skip <target_key> --reason no_hook`.

## Addresses (email route)

* Grade A: published for that person (their page, a talk bio, the job post) or a role inbox named in the job
  post. Grade B: the company's pattern confirmed by two published addresses at the same domain; put both URLs
  in an evidence file. Grade C: a guess; refused unless the person allowed it.
* `email verify --address <addr> --grade A|B [--evidence-file <f>]` checks MX and the grade. Exit 7 means the
  address may not be used. Never try several variants for one person and never probe mail servers.
* Store the address with the contact (`email`, `email_grade`, `email_evidence_url`).
* If no grade A or B address was found and `outreach next` shows `address: enrich_possible` or
  `pattern_available`, run `enrich find --contact <contact_uid> --target contact:<contact_uid>`. Exit 0 with
  `sendable: true`: the address is stored; write the draft. Exit 0 with `sendable: false` or `status: not_found`: no email route; use
  LinkedIn if enabled, else `outreach skip <target> --reason no_address`. `PENDING`: run the same command again,
  at most 3 times. Exit 4: skip the email route for this target now. Exit 3 or 7: drop the target. Exit 5: end
  the cycle. Optional: if `evidence_candidates` lists a page and page budget is left, open it; if it shows the
  exact address, `research add` it and run `email verify --grade A`. Never write your own guesses of addresses.
* `pattern_available` means two published addresses already prove the company's pattern: `enrich find` writes
  that address (grade B) without asking any provider. `enrich_done_no_address` means a lookup already ran: do not
  run it again; use LinkedIn or skip with `--reason no_address`. `have_address` needs no lookup. `no_domain`:
  find the company's own domain first (its site, the job post), or skip. No `address` key: the email finder is
  not installed; only published addresses count.
* A provider-found address keeps the grade code gave it: `email verify` never raises it. Only a research fact
  that shows the exact address on an https page of that domain, or on the job's own posting, makes it grade A.
