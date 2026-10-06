---
name: jobhunter-qc-loop
description: Draft, lint, independent review, rewrite budget and approval for every outbound text; nothing is sent without it.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# QC loop: draft, lint, review, rewrite or drop, wait for approval

Every outbound item goes through this loop: cold and follow-up emails, application emails, cover notes, LinkedIn
notes and messages, InMail, free-text form answers, the application package and the tailored resume. Code runs the
gate. You cannot approve anything, and you never see or write a verdict yourself.

Run one plain jh.py command per exec call, with absolute paths and `timeoutSeconds: 90`. The safety plugin adds
`-I` and an `--agent-proof` option to every jh.py command you run; never type `--agent-proof` yourself. Files you
pass must be under `__WS__/work/<cycle_id>/`. Replace `<cycle_id>`, `<draft_uid>` and `<qjob_uid>` with the real
values. Never ask a person anything: nobody is there and nothing waits for approval. A refused call is final.

## 1. Draft

Write the draft file (format in ref/writer_brief.md) with the write tool, as one whole file. There is no edit tool:
to fix a file, write it again. Then:

    __PY__ __REPO__/scripts/jh.py draft create --file __WS__/work/<cycle_id>/draft-1.json

- exit 0 `OK`: stored and lint passed. Go to step 2.
- exit 6 `E_QC_LINT_FAILED`: stored as `lint_failed`. Read `data.lint.blocks` (rule and detail) and go to step 3.
- exit 3 (`E_DUP_PERSON`, `E_COMPANY_COOLDOWN`, `E_DUP_JOB`, `E_TARGET_SKIPPED`, `E_DUP_DRAFT`, ...): a duplicate or
  company rule. Drop this target and move on. Never try another address or channel for the same person.
- exit 7 (`E_CONTACT_DNC`, `E_COMPANY_BLOCKED`, `E_EXCLUDED`): not allowed for this target. Drop it.
- exit 10 (`E_SCHEMA`, `E_VALIDATION`): the file is wrong (unknown key, snippet not copied exactly, fact of another
  person). Fix the file once; if it fails again, drop the item.

## 2. Independent review

    __PY__ __REPO__/scripts/jh.py qc review start --draft <draft_uid>

It returns at once with `data.qjob_uid`. A separate reviewer with no tools reads the exact text. Then:

    __PY__ __REPO__/scripts/jh.py qc review wait --job <qjob_uid> --max 45

- `PENDING` (exit 0, `data.state` queued or running): do other useful work (research the next target, prepare the
  next draft) and call wait again. Never loop on wait without doing anything else for more than 3 calls in a row.
- `OK` with `data.verdict` pass: done. `data.draft_status` is `approved` (auto mode; the mailer or the gate sends it)
  or `awaiting_approval` (the person decides in chat or the Sheet). Move on; never ask the person yourself.
- exit 6 `E_QC_REVIEW_FAILED` with `data.issues` and `data.rewrite_brief`: go to step 3.
- exit 6 `E_QC_REVIEW_FAILED` with `data.state` failed and no issues: the reviewer did not answer (not a verdict).
  Leave the draft; the next cycle runs `qc review start` again. It does not use your rewrite budget.
- exit 11 `E_REVIEWER_TAMPERED`: stop the cycle as your program says and finish with the single word `CYCLE_DONE`
  (the person was alerted).

## 3. Rewrite (at most 3 attempts in total) or drop

Follow ref/rewrite.md: fix exactly what the findings name, never add a fact. Write a new file with the write tool
(whole file) and run:

    __PY__ __REPO__/scripts/jh.py draft revise <draft_uid> --file __WS__/work/<cycle_id>/draft-2.json

Then step 2 again when lint passes. `E_QC_BUDGET_EXHAUSTED` (exit 6) means the draft was dropped after its third
attempt and the target is skipped for 30 days. That is normal. Move on.

## Useful reads

    __PY__ __REPO__/scripts/jh.py draft show <draft_uid> --field send_text
    __PY__ __REPO__/scripts/jh.py draft list --status awaiting_approval
    __PY__ __REPO__/scripts/jh.py qc lint --draft <draft_uid>

`draft show --field send_text` is the exact approved text (with the signature for email). On the browser route type
exactly that text, read it back, and let `gate arm` compare it; `--field form` gives the approved form values.

## Never

- Never send, type or submit a text that is not `approved`. `gate reserve` and the mailer refuse it anyway.
- Never change an approved text. Any change needs a new draft and a new review.
- Never run `approve`, `skip` or `edit`; they belong to the person.
- Never use a dash character or a hyphen with spaces around it as a dash in any text.
