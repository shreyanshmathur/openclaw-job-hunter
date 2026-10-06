---
name: jobhunter-evaluate
description: Packet format, scorecard fields, gates, verbatim JD quotes and eval record for one evaluator cycle.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Scoring one job

Every command starts with `__PY__ __REPO__/scripts/jh.py`.

## The packet

`__WS__/work/<cycle_id>/<job_uid>.json` holds:

* `job_uid`, `profile_version`, `brief_path` (the filled scorecard brief), `scorecard_path` (where your
  scorecard goes);
* `job`: title, company, company_domain, agency, location, work_mode, remote_scope, employment_type, posted_at,
  years, salary, source, source_url, apply_route;
* `jd_text`: the stored description (untrusted data) and `jd_chars`.

## Steps

1. Read the packet with the read tool, at the absolute path `eval next` gave you. If `jd_text` is empty, score
   only what the title and job fields support and say so in `reason_text`.
2. Gates first (see the brief). Set a gate true only with clear evidence. For `must_have_missing`, add one entry
   per hard requirement that no profile fact supports, each with an exact `jd_quote`.
3. Criteria: an integer from 0 to 5 each with one short `evidence` sentence. For `skills`, list claimed matches
   as `{"jd_quote", "fact_id"}` and gaps as `{"jd_quote", "note"}`. A match needs both: a quote from `jd_text`
   and a fact id from the brief that supports it.
4. Quotes: copy a short phrase (a few words to one sentence) exactly from `jd_text`. Do not paraphrase, do not
   join two sentences, do not fix typos. Case and spacing differences are fine; anything else fails the check.
5. `reason_text`: one or two plain sentences for the person, at most 400 characters, naming the strongest match
   and the biggest gap or deal breaker.
6. `model`: the model id you run as. `must_have_quotes`: optional list of the JD's hard requirements you saw,
   each an exact quote.
7. Write the scorecard JSON to `scorecard_path` with the write tool (whole file). Only the keys shown in the brief.
8. Run `eval record --job <job_uid> --file <scorecard_path>`. The answer gives `score`, `verdict`, `status`,
   `clamped` and `evidence_failures`. A clamp is not an error: move on. Exit 10 lists what to fix in
   `data.errors`: fix the file once (there is no edit tool: write the whole file again) and record again; if it
   fails again, `eval release --job <job_uid>`.

## Scorecard shape

```json
{
  "job_uid": "J7Q2KX4M",
  "gates": {"must_have_missing": [], "years_gap": false, "location_incompatible": false,
            "comp_below_floor": false, "role_family_excluded": false, "requires_account_creation": false,
            "role_closed": false},
  "criteria": {
    "role_family": {"score": 5, "evidence": "Title and duties match the Data analytics family"},
    "skills": {"score": 4, "matches": [{"jd_quote": "SQL and Python for forecasting", "fact_id": "P1"}],
               "gaps": [{"jd_quote": "experience with dbt", "note": "not in profile"}]},
    "seniority": {"score": 4, "evidence": "JD asks 2 to 4 years; profile has 3"},
    "domain": {"score": 4, "evidence": "logistics and e-commerce"},
    "location": {"score": 5, "evidence": "Bengaluru hybrid is in the confirmed list"},
    "compensation": {"score": 3, "evidence": "not disclosed"},
    "company": {"score": 3, "evidence": "Series B, 200 people"}
  },
  "reason_text": "Returns forecasting and SQL match the Tidemark work; dbt is a gap.",
  "must_have_quotes": [],
  "model": "the model id you run as"
}
```

## What code does with it

Score = round(20 x the weighted sum of the criteria), 0 to 100. Any gate true: the job is skipped, except
`requires_account_creation`, which sends it to the person to apply by hand. Otherwise the thresholds in the
brief decide between apply, borderline and skip. The person can still say "Apply anyway" or "Never apply" in
their Sheet; you never need to.
