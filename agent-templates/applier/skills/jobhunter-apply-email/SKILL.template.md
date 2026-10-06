---
name: jobhunter-apply-email
description: Postings that ask for a CV by email; write the application_email draft with the tailored PDF; send it in Gmail through the gate on the web route, or let the mailer send it on the app_password route.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Applying by email

Used for `apply next` items with `needs: email` (the posting says "send your CV to ..."). You write the draft
and QC and approval clear it. Then `preflight` tells you the email route: on `web_ui` (the default) you send the
approved draft in Gmail yourself under a gate token (section "Web route" below); on `app_password` the mailer
sends it with the approved resume attached and you never open Gmail. Every command starts with
`__PY__ __REPO__/scripts/jh.py`. Every file below (contact, evidence, draft, precheck, observed) is a whole file
you write with the write tool under `__WS__/work/<cycle_id>/`; there is no edit tool, so to fix a file, write it
again.

## Steps

1. Check the address: it is the posting's own address (`apply_email` in the item). If it is a role inbox such as
   careers@ or jobs@, store it once with `contact add --file <contact.json>` using `"role_type": "role_inbox"`;
   a named person gets their own role type (an email to a person is a first touch and follows every person
   rule). Exit 3 or 7 means drop the job's email route: `job set-status <job_uid> --status needs_human
   --reason unsupported_form`.
2. `email verify --address <addr> --grade A` (the address is published on the posting, so grade A; put the
   posting URL in an evidence file and pass `--evidence-file <f>`). Exit 7 (`E_NO_MX`, `E_ADDRESS_GRADE`)
   means the email route is not usable: send the job to the person as above.
3. Tailor the resume (skill `jobhunter-resume-tailor`) and wait until the resume draft is approved (in human
   mode this can take until a later cycle; release the job with `apply release --job <job_uid>`).
4. Write the application email with the writer brief (`__WS__/ref/writer_brief.md`, channel
   `email_application`; read it with the read tool): subject names the role as the posting names it, 60 to 150
   words, why this role, one or two proof points that trace to profile facts, a plain close. No links unless the
   posting asks for them. The draft file:

   ```json
   {"kind": "application_email", "channel": "email_application", "job_uid": "<job_uid>",
    "contact_uid": "<contact_uid>", "attachment_variant_uid": "<variant_uid>",
    "subject": "<subject>", "body": "<body without signature>", "is_reply": false,
    "hook": null, "claims": [{"text": "<claim>", "fact_id": "P1"}], "links": []}
   ```

   `draft create --file <f>`, then `qc review start` and `qc review wait` (skill `jobhunter-qc-loop`).
5. `app_password` route: stop here. The mailer sends the approved draft in the recipient's window, records the
   thread and marks the job applied. Do not open Gmail, do not send anything yourself. `web_ui` route: once the
   draft is approved, send it as below.

## Web route (`gmail.route = web_ui`) only

When `preflight` reports the web route, the send is a browser action under a gate token: follow skill
`jobhunter-gmail-web` with skill `jobhunter-gate` after the draft is approved. Every browser call carries
`"profile": "jobhunter"`, for example
`{"action": "navigate", "profile": "jobhunter", "targetUrl": "https://mail.google.com/mail/u/0/#inbox"}`:

1. Session check on the Gmail inbox (skill `jobhunter-stop-detect`: `read_login_state.js`, then
   `identity check --platform gmail`). Signed out, a verification prompt or another account: stop.
2. `gate precheck-plan --kind application_email --job <job_uid>`; run each query in Gmail search and count the
   rows with `read_gmail_list.js` (`loaded` false: no precheck this cycle); `gate precheck --kind
   application_email --platform gmail --job <job_uid> --file <f>`.
3. `gate reserve`, compose with `type --slowly` (recipient, subject, body exactly), `resume stage` and upload
   only the staged file, `read_compose.js` (`to` is only the recipient, the file name shows), write its
   `observed_text` and run `gate arm`.
4. Dwell, one click on Send, `read_toast.js`, then the Sent-folder read-back with `read_gmail_message.js`: its
   `readback_text` must be the text you armed with. Then `gate confirm` with the evidence, the Sent row's URL
   and `--observed-file` holding that `readback_text` (exit 6: already unknown, never resend); anything else is
   `gate unknown`. `resume unstage --token <token>` at the end, whatever happened.
