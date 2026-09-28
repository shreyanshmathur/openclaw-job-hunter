---
name: jobhunter-apply-ats
description: Recipes for company ATS application forms (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, BambooHR) under a gate token.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Applying on a company ATS form

Use this with skill `jobhunter-gate` (kind `application`) once the package is approved. The platform for
`gate precheck` and `gate reserve` is the ATS name: `greenhouse`, `lever`, `ashby`, `smartrecruiters`,
`workable`, `recruitee` or `bamboohr`. Workday postings always go to the person (the apply queue already sends
them there). Every command starts with `__PY__ __REPO__/scripts/jh.py`. Every browser call uses the shapes in
skill `jobhunter-gate` ("Browser calls the guard accepts"), with refs from the latest snapshot of the tab.

## Before the token (read only)

1. Open the apply URL from `apply next`. Run `__WS__/ref/drivers/detect_page.js` (act kind `evaluate`, `fn` =
   the file's exact text), write its `detect_file` to `__WS__/work/<cycle_id>/detect-<n>.json` and run
   `detect --file <that file>`. `gate reserve` needs a clear detection from the last 10 minutes, so do this
   right before the precheck.
   * If `detect_file.platform` is `ats`, the page is a careers page on the company's own domain, not an ATS
     host. The guard refuses a token for `ats` and lets no token fill that page. Take a snapshot and find the
     ATS address: the "Apply" link, or the address of the embedded application form (hosts such as
     `job-boards.greenhouse.io`, `boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`,
     `jobs.smartrecruiters.com`, `apply.workable.com`, `<company>.recruitee.com`, `<company>.bamboohr.com`).
     Navigate the tab to that address and start this step again; use the platform `detect_page.js` then
     reports. No ATS address on the page: `job set-status <job_uid> --status needs_human --reason unsupported_form`.
2. If `hint` is `needs_human` (a visible CAPTCHA or "create an account"), run
   `job set-status <job_uid> --status needs_human --reason captcha_visible` (or `account_required`) and move on.
   Never solve a CAPTCHA, never create an account, never sign in.
3. Run `read_applied_state.js` and put its `checks` into the precheck file (skill `jobhunter-gate`).
4. Take a snapshot and compare the form with the approved package: every required field must be in the package
   with a value. A required field the package does not cover (a new question, a test, a video, an assessment)
   means `job set-status <job_uid> --status needs_human --reason answer_missing` (or `unsupported_form`).

## Under the token (fill)

Take a snapshot first, and again after every step that changes the form (a new step, a dialog, an upload that
re-renders the fields). Every `ref` below is from the latest snapshot of this tab.

* Preparatory buttons are fill actions and need the reserved token: "Apply", "Apply now", "Apply for this job",
  "Next", "Continue", "Review", "Upload resume", "Attach". Any other button is a commit.
  `{"action": "act", "profile": "jobhunter", "kind": "click", "ref": "e10"}`
* Resume: skill `jobhunter-upload`. `resume stage --variant <variant_uid> --token <token>` returns
  `upload_path`; upload exactly that path through the page's own file chooser (the button ref as `ref`, or the
  file input ref as `inputRef`):
  `{"action": "upload", "profile": "jobhunter", "paths": ["<upload_path>"], "ref": "e12"}`.
  Check the filename appears on the page; if the form parsed the resume and pre-filled fields, read them back
  and correct only fields that differ from the package.
* Text fields: act kind `type` with `slowly: true`, the exact package value, one field at a time:
  `{"action": "act", "profile": "jobhunter", "kind": "type", "ref": "e20", "text": "Alex", "slowly": true}`.
* Selects: act kind `select` with the option whose visible text equals the package value:
  `{"action": "act", "profile": "jobhunter", "kind": "select", "ref": "e24", "values": ["India"]}`.
* Checkboxes and radios: act kind `click` on the ref whose snapshot role is `checkbox` or `radio`. There is no
  `check` kind; an unknown kind counts as a submit. Tick only what the package lists:
  `{"action": "act", "profile": "jobhunter", "kind": "click", "ref": "e25"}`.
* If no option matches the package value, stop filling, run `gate fail <token> --reason precondition_changed
  --evidence-file <f>` (possible only before any commit) and send the job to the person with reason
  `answer_missing`.
* Cover letter box: paste nothing. Type the approved cover note only if the package includes it.
* EEO and demographic questions: only the stored choice from the package, otherwise the form's own
  "decline to answer" option if the package says so.

## Per-ATS notes (verify on the page; layouts change)

| ATS | Where the form is | Notes |
|---|---|---|
| Greenhouse | same page below the posting, or after "Apply for this job" | resume upload is a button that opens a chooser; custom questions follow the standard fields; submit is "Submit application" |
| Lever | `.../apply` page | one page; "Additional information" box is free text (package decides); submit "Submit application" |
| Ashby | `.../application` tab | the resume may auto-fill fields: read back everything; submit "Submit Application" |
| SmartRecruiters | "I'm interested" opens a multi-step flow | steps use "Next"; an account or social sign-in prompt means `account_required` |
| Workable | "Apply for this job" | may ask for a phone with country code from the answer bank; submit "Submit application" |
| Recruitee | "Apply" | consent checkbox only when the package lists it |
| BambooHR | "Apply for this job" | file input ignores `input.files`; verify the filename text on the page |

## After the click

The final click is one act kind `click` on the submit button's ref from the latest snapshot. Then read
`read_toast.js` (act kind `evaluate`) and the page. Confirmation text ("Thank you for applying", "Application submitted",
"We have received your application") means `gate confirm <token> --evidence-file <f>` with that text in the
file. A validation error shown after the click, a blank page or anything unclear means
`gate unknown <token> --note-file <f>`. Never click submit a second time. Then `resume unstage --token <token>`.
