# Application form answers (applier)

Every value you put in an application form comes from the person: from the answer bank, from the approved
resume, or from a free-text answer that passed QC. You never guess, estimate or "fill something reasonable".
A field you cannot answer from these sources sends the job to the person.

## Structured fields: the answer bank

For each field, write a question file to `__WS__/work/<cycle_id>/q-<n>.json`:

```json
{"label": "Notice period (days)", "field_type": "number", "choices": null, "job_uid": "J7Q2KX4M"}
```

* `label`: the field's label exactly as the page shows it.
* `field_type`: `text`, `number`, `choice`, `date` or `boolean`.
* `choices`: the options exactly as shown, for select, radio and checkbox groups; `null` otherwise.

Run `__PY__ __REPO__/scripts/jh.py answers get --file <question file>`.

* Exit 0: use `data.value` exactly as returned. For a choice field it is one of your `choices`.
  `data.key` goes into the package field's `answer_key`. `data.sensitive: true` (pay, equal-opportunity
  answers) is still the person's own answer; use it only in this form.
* Exit 9: there is no confirmed answer (or the field is one that is never stored). The person was asked
  (`data.human_task`) and the job was handed to them. Do not fill the form; move on to the next job.

## Never stored, always the person

Date of birth, age, home street address, PIN or postal code, government ids (passport, national id, tax id),
passwords, account creation, and any statement the form asks the person to certify, attest or sign.
`answers get` sends these to the person; never type them yourself.

## Free-text questions

"Why do you want to join?", "Describe a project": write a `form_answer` draft with the writer brief
(`__WS__/ref/writer_brief.md`), set `field_label` to the question and `field_char_limit` to the field's limit,
and run it through QC (skill `jobhunter-qc-loop`). Use only profile facts and research facts; cite them in
`claims`. The package field then references the draft with `form_answer_draft_uid`.

## Salary

Only `expected_ctc` or `current_ctc` from the answer bank, in the form's currency and format. If the form
wants another currency or a range the bank does not hold, the job goes to the person. Never mention pay in
any message.

## The package

The application package (`draft create`, kind `application_package`) lists every field with `label`, `type`,
`value`, and `answer_key` or `form_answer_draft_uid`, plus `resume_variant_uid`. Code checks that each value
equals the bank value (`A-*` rules) and later compares what the page shows with the approved package.
