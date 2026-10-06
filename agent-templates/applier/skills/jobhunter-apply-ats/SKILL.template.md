---
name: jobhunter-apply-ats
description: Recipes for company ATS application forms (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, BambooHR, Workday, iCIMS, SuccessFactors, Taleo, Oracle Cloud HCM, Jobvite) under a gate token, with site accounts and emailed codes done by code.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Applying on a company ATS form

Use this with skill `jobhunter-gate` (kind `application`) once the package is approved. The platform for
`gate precheck` and `gate reserve` is the ATS name: `greenhouse`, `lever`, `ashby`, `smartrecruiters`,
`workable`, `recruitee`, `bamboohr`, `workday`, `icims`, `successfactors`, `taleo`, `oracle_hcm` or `jobvite`.
Workday postings reach you only when the owner allowed site accounts for Workday (otherwise the apply queue sends
them to the person). Every command starts with `__PY__ __REPO__/scripts/jh.py`. Every browser call uses the shapes in
skill `jobhunter-gate` ("Browser calls the guard accepts"), always with `"profile": "jobhunter"` and with refs
from the latest snapshot of the tab. Files (detect, precheck, observed, evidence, note) are whole files you
write with the write tool under `__WS__/work/<cycle_id>/`; drivers are read with the read tool.

## Before the token (read only)

1. Open the apply URL from `apply next`:
   `{"action": "navigate", "profile": "jobhunter", "targetUrl": "https://job-boards.greenhouse.io/example/jobs/1"}`.
   Run `__WS__/ref/drivers/detect_page.js` (act kind `evaluate`, `fn` = the file's exact text), write its
   `detect_file` with the write tool to `__WS__/work/<cycle_id>/detect-<n>.json` and run
   `detect --file <that file>`. `gate reserve` needs a clear detection from the last 10 minutes, so do this
   right before the precheck.
   * If `detect_file.platform` is `ats`, the page is a careers page on the company's own domain, not an ATS
     host. The guard refuses a token for `ats` and lets no token fill that page. Take a snapshot and find the
     ATS address: the "Apply" link, or the address of the embedded application form (hosts such as
     `job-boards.greenhouse.io`, `boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`,
     `jobs.smartrecruiters.com`, `apply.workable.com`, `<company>.recruitee.com`, `<company>.bamboohr.com`).
     Navigate the tab to that address and start this step again; use the platform `detect_page.js` then
     reports. No ATS address on the page: `job set-status <job_uid> --status needs_human --reason unsupported_form`.
2. If `detect` answers `clear` with `flow: "account"` or `flow: "email_code"`, the owner allowed site accounts or
   email codes for this site: follow "Site accounts and emailed codes" below. If it answers a stop with
   `job_needs_human` (an account wall or a code page the owner did not allow), run
   `job set-status <job_uid> --status needs_human --reason account_required` and move on.
   On a CAPTCHA: stop typing, run `captcha status --job <job_uid>` (the guard has already opened the task and asked
   the owner), close nothing, and move to the next job. Never solve a CAPTCHA and never click inside one.
3. Run `read_applied_state.js` and put its `checks` into the precheck file (skill `jobhunter-gate`).
4. Take a snapshot and compare the form with the approved package: every required field must be in the package
   with a value. A required field the package does not cover (a new question, a test, a video, an assessment)
   means `job set-status <job_uid> --status needs_human --reason answer_missing` (or `unsupported_form`).

## Site accounts and emailed codes (code does the secret parts)

Only when `account status --job <job_uid>` says `consent.ats_accounts: granted` (accounts) or
`consent.email_codes: granted` (codes). `<tab>` is the `targetId` of your tab from the last browser result.

1. `account status --job <job_uid>`: `next` is `create`, `signin` or `needs_human` (then `job set-status
   <job_uid> --status needs_human --reason account_required` and move on).
2. Run the precheck and `gate reserve` as usual (skill `jobhunter-gate`) before any account step, so the steps
   carry `--token <token>`.
3. `account create --job <job_uid> --tab <tab> --token <token>` (or `account signin ...` when `next` is
   `signin`). Code makes the password, keeps it in the Keychain, types it, ticks only the standard privacy or
   terms box and clicks the one button. The answer is an `outcome`:
   * `verify_code` or `code_needed`: `code submit <request_uid> --tab <tab>`; while it answers `PENDING`, wait
     `retry_after_s` seconds and run it again; stop at `accepted`, `rejected` or an error.
   * `verify_link`: `code open-link <request_uid> --tab <tab>` the same way; then `account signin` if the page
     shows the sign-in form.
   * `created` or `signed_in`: take a snapshot and continue the application.
   * `rejected`, `exists`: code already sent the job to the owner; move on.
   * `captcha`: the owner was asked; leave the tab open and move on.
4. Before a click that makes the site email a code (for example a "Send code" button), run
   `code expect --job <job_uid> --tab <tab> --purpose signin` (or `account_verify`, `application_submit`).
5. After `gate arm`, a site that emails a security code after the first submit (Greenhouse) is handled by
   `code submit <request_uid> --tab <tab>` with the `code_request` that `gate arm` returned; code clicks the
   resubmit button within the token's commit budget.

Never do these yourself: type into a password, code, passcode or PIN field (the guard refuses it), click "Sign in
with Google", LinkedIn, Microsoft or Apple, open a mailbox to look for a code, or read the Keychain or `private/`.
Phone (SMS) codes, authenticator codes and identity checks are never handled: they stop the site.

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
| Workday | "Apply", then "Apply Manually" | a site account per company: `account create` or `account signin`; the verification is often an emailed link (`code open-link`); multi-step flow with "Next" |
| iCIMS | "Apply for this job online" | an account per company site; codes come from icims.com or the company's own domain |
| SuccessFactors | "Apply" | a one-time passcode for new candidate accounts (`code submit`); split code boxes are typed by code |
| Taleo | "Apply Online" | an account per company; mail may come from the company's domain |
| Oracle Cloud HCM | "Apply Now" | a 6-digit email code verifies the address (`code submit`); an SMS code stops the site |
| Jobvite | "Apply" | usually no account; an account wall means `account_required` |

## After the click

The final click is one act kind `click` on the submit button's ref from the latest snapshot. Then read
`read_toast.js` (act kind `evaluate`) and the page. Confirmation text ("Thank you for applying", "Application
submitted", "We have received your application") means: write that text with the write tool to an evidence file
and run `gate confirm <token> --evidence-file <f>`. A validation error shown after the click, a blank page or anything unclear means
`gate unknown <token> --note-file <f>`. Never click submit a second time. Then `resume unstage --token <token>`.
