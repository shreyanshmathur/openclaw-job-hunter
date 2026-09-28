---
name: jobhunter-form-answers
description: Application form fields from the answer bank only; answers get per field, choice matching, form_answer drafts, sensitive fields, human tasks.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Form answers

Read `__WS__/ref/form_answer.md` for the full rules. Every command starts with
`__PY__ __REPO__/scripts/jh.py`.

## Procedure, per form

1. Read the form (read only) and list every field: label exactly as shown, type, options, character limit.
2. Structured fields: for each one write `__WS__/work/<cycle_id>/q-<n>.json`
   `{"label": "...", "field_type": "text|number|choice|date|boolean", "choices": [...] or null, "job_uid": "<job_uid>"}`
   and run `answers get --file <q file>`.
   * Exit 0: use `data.value` exactly; record `data.key` as the field's `answer_key`.
   * Exit 9: the person was asked (`data.human_task`) and the job was handed to them. Stop working on this
     job: do not fill anything and take the next job.
3. Free-text questions: a `form_answer` draft per question (`field_label`, `field_char_limit`), written with
   `__WS__/ref/writer_brief.md` and passed through QC (skill `jobhunter-qc-loop`).
4. Optional fields with no confirmed answer: leave them empty. Never fill a field to "complete" a form.
5. Build the `application_package` draft: every field with `label`, `type`, `value`, `choices`, and
   `answer_key` or `form_answer_draft_uid`, plus `resume_variant_uid` from skill `jobhunter-resume-tailor`.

## Always the person

Date of birth, age, street address, PIN or postal code, government ids, passwords, account creation, and any
certification, declaration or signature. Equal-opportunity and pay fields only with the stored answer.
