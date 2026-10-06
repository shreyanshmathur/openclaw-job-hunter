---
name: jobhunter-form-answers
description: Application form fields from the answer bank only; answers get per field, choice matching, form_answer drafts, sensitive fields, human tasks.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Form answers

Read `__WS__/ref/form_answer.md` with the read tool for the full rules. Run every command with the exec tool;
every command starts with `__PY__ __REPO__/scripts/jh.py`. Write files with the write tool, always as whole files
with absolute paths that start with `__WS__/` (never `~`, `@`, `..` or `$`). There is no edit tool: to change a
file, write it again.

## Procedure, per form

1. Read the form (read only): a browser `snapshot`, or the `read_form.js` driver, always with
   `"profile": "jobhunter"` and the shapes in skill `jobhunter-gate`. List every field: label exactly as shown,
   type, options, character limit.
2. Structured fields: for each one write the whole file `__WS__/work/<cycle_id>/q-<n>.json` with the write tool
   `{"label": "...", "field_type": "text|number|choice|date|boolean", "choices": [...] or null, "job_uid": "<job_uid>"}`
   and run `answers get --file __WS__/work/<cycle_id>/q-<n>.json`.
   * Exit 0: use `data.value` exactly; record `data.key` as the field's `answer_key`.
   * Exit 9: the person was asked (`data.human_task`) and the job was handed to them. Stop working on this
     job: do not fill anything and take the next job.
3. Free-text questions: a `form_answer` draft per question (`field_label`, `field_char_limit`), written with
   `__WS__/ref/writer_brief.md` (read it with the read tool) and passed through QC (skill `jobhunter-qc-loop`).
4. Optional fields with no confirmed answer: leave them empty. Never fill a field to "complete" a form.
5. Build the `application_package` draft: every field with `label`, `type`, `value`, `choices`, and
   `answer_key` or `form_answer_draft_uid`, plus `resume_variant_uid` from skill `jobhunter-resume-tailor`.

## Always the person

Date of birth, age, street address, PIN or postal code, government ids, passwords, account creation, and any
certification, declaration or signature. Equal-opportunity and pay fields only with the stored answer. Code
hands these to the person (`answers get` exit 9); never ask anyone yourself.
