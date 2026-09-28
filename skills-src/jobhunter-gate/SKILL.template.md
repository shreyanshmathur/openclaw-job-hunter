---
name: jobhunter-gate
description: The only way anything is submitted or sent from the browser: precheck, reserve, fill, read back, arm, dwell, one click, verify, confirm; unknown otherwise; reconcile tasks.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# The gate: reserve, act, confirm

Every browser write (an application form, a LinkedIn invitation or message, a web-route email) follows these
steps in this order. The jobhunter-guard plugin refuses fill actions without a reserved token and commit actions
before the token is armed and the dwell has passed, and it allows at most 2 commit actions per token. Every
command starts with `__PY__ __REPO__/scripts/jh.py`; `<k>` is the kind (`application`, `li_invite`,
`li_message`, `li_followup`, and the email kinds on the web route) and `<p>` the platform (`linkedin`,
`gmail`, or the ATS or board name such as `greenhouse` or `naukri`).

`<p>` must name the host the page is on: the guard lets a token fill and click only on its own platform's
hosts. On an ATS it is the name `detect_page.js` reports (`greenhouse`, `lever`, `ashby`, ...); on a board it
is the board name without `site:` (`naukri`). A careers page on the company's own domain reports platform
`ats`, which is no platform at all: the guard refuses a token for `ats` and knows no platform for that host.
Find the ATS address instead (the apply link, or the address of the embedded application form, for example
`job-boards.greenhouse.io/...` or `jobs.lever.co/...`), open it in the tab, run `detect_page.js` again and
reserve for the platform it reports. When there is no ATS address, hand the job over:
`job set-status <job_uid> --status needs_human --reason unsupported_form`.

## Browser calls the guard accepts

Every browser call carries `"profile": "jobhunter"`. Use exactly these shapes:

```json
{"action": "navigate", "profile": "jobhunter", "targetUrl": "https://job-boards.greenhouse.io/example/jobs/1"}
{"action": "snapshot", "profile": "jobhunter"}
{"action": "act", "profile": "jobhunter", "kind": "click", "ref": "e10"}
{"action": "act", "profile": "jobhunter", "kind": "type", "ref": "e20", "text": "Alex", "slowly": true}
{"action": "act", "profile": "jobhunter", "kind": "select", "ref": "e24", "values": ["India"]}
{"action": "upload", "profile": "jobhunter", "paths": ["<upload_path from resume stage>"], "ref": "e12"}
{"action": "act", "profile": "jobhunter", "kind": "evaluate", "fn": "<exact text of __WS__/ref/drivers/read_form.js>"}
```

* `ref` values come from the latest `snapshot` of the same tab. Take a new snapshot after every navigation and
  after any click that opens a dialog, a menu or the next form step, before you use a ref again. When you work
  in more than one tab, pass the same `targetId` to the snapshot and to the actions that use its refs. A ref
  the latest snapshot of that tab did not show counts as a submit click.
* The act kinds are `click`, `type`, `press`, `select`, `fill`, `drag`, `wait`, `evaluate`, `batch` and
  `hover`. Any other kind (there is no `check` kind) and any other top-level action (`{"action": "click"}`,
  `{"action": "type"}`, `{"action": "evaluate"}`) count as a submit: they are refused before `gate arm` and
  after it they use up one of the 2 submit clicks. Never use them.
* Checkboxes and radio buttons: click the ref whose role in the snapshot is `checkbox` or `radio`; that is a
  fill action. Do not click the label text instead: a ref with another role can count as a submit.
* Upload: `paths` holds exactly one path, the `upload_path` that `resume stage` returned, and `ref` is the
  chooser button (or `inputRef` the file input) from the latest snapshot. Any other path is refused.
* A driver runs as act kind `evaluate` whose `fn` is the driver file's exact text: read the file and pass all
  of it unchanged. Any other script is refused (`G_SCRIPT_NOT_ALLOWED`).
* Never `"submit": true` on `type`, never press Enter, never click by coordinates, never `drag`: all of these
  are submit actions.

## 1. Detect, then precheck (read only)

1. On the target page run `__WS__/ref/drivers/detect_page.js` (act kind `evaluate`). Write its `detect_file`
   object to `__WS__/work/<cycle_id>/detect-<n>.json` and run `detect --file <that file>`. A `stop` hint (its
   `top` names the most severe signature) or exit 5 ends the cycle (skill `jobhunter-stop-detect`). Reserve
   needs a clear detection from the last 10 minutes.
2. `gate precheck-plan --kind <k> [--job <job_uid>] [--contact <contact_uid>] [--thread <thread_key>]` lists
   the checks. Run exactly those checks with the drivers and write the precheck file (12.7):

   ```json
   {"kind": "application", "platform": "greenhouse", "observed_at": "2026-09-27T05:10:00Z",
    "page_url": "https://job-boards.greenhouse.io/example/jobs/1",
    "checks": [{"name": "applied_badge", "value": false}, {"name": "already_applied_text", "value": false}]}
   ```

   | Kind | Checks | Driver |
   |---|---|---|
   | application | `applied_badge`, `already_applied_text` | `read_applied_state.js` (`checks`) |
   | li_invite | `profile_button` (Connect, Pending, Message, Follow, None), `vanity_slug` | `read_li_invite_dialog.js` (`profile`) |
   | li_message | `conversation_has_our_message`, `connection_degree` (1st, 2nd, 3rd) | `read_compose.js`, `read_li_invite_dialog.js` |
   | li_followup | `conversation_has_reply` | `read_compose.js` |
   | inmail | `conversation_has_our_message` | `read_compose.js` |
   | cold_email, application_email (web route) | `sent_to_address`, `sent_to_other_addresses`, `sent_company_query`, `outbox_query`, `scheduled_query` | `read_gmail_list.js` (`count`) |
   | followup_email (web route) | `thread_has_reply` (true or false), `company_inbound_since_first` | `read_gmail_message.js`, `read_gmail_list.js` |

   Web route email: for each check with a `query`, open
   `https://mail.google.com/mail/u/0/#search/<the query, URL-encoded>`, run `read_gmail_list.js` and use its
   `count` (a check without a query is 0). `loaded` false or `count` null means the list could not be read:
   write no precheck file and leave the item for the next cycle. `thread_has_reply` is true when
   `read_gmail_message.js` on the thread shows any message whose `from_owner` is false.

3. `gate precheck --kind <k> --platform <p> [--job ...] [--contact ...] [--thread ...] --file <f>` returns
   `precheck_id` and `result`. `already_done` means it was done before: code records it and the item is closed;
   move on. `uncertain` means the person decides; move on.

## 2. Reserve

`pace wait --platform <p> --kind write` (or `--kind easy_apply` for Easy Apply) until `remaining_s` is 0, then
`gate reserve --kind <k> --draft <draft_uid> --precheck <precheck_id> --platform <p> [--job ...] [--contact ...]
[--thread ...]`. You get a `token`. Exit 3 or 7: drop the item. Exit 4: this action type is done for now.
Exit 6: the text is not cleared. Exit 11 `E_TOKEN_OPEN`: finish or resolve your open token first
(`gate status`).

## 3. Fill and read back

* Only preparatory clicks (Apply, Next, Continue, Review, Upload resume, Attach, Connect, More, Add a note,
  Message, Compose, Reply) and fill actions: act kind `type` with `slowly: true`, act kind `select`, act kind
  `click` on a checkbox or radio ref, and `upload` of the staged file (shapes above).
* Type exactly the approved text or value. Never paste, never use a value setter, never type anything else.
* Read back what the page holds with the driver (act kind `evaluate`, `fn` = the driver file's exact text) and
  write it to `__WS__/work/<cycle_id>/observed-<token>.<ext>`:
  a form: `read_form.js` `observed` object (JSON); a LinkedIn note: `read_li_invite_dialog.js` `note_text`
  (text); a LinkedIn message, an InMail or a web-route email: `read_compose.js` `observed_text` (text; for an
  InMail and an email it is `Subject: <subject>`, a blank line, then the body; a web-route follow-up is an
  inline reply whose subject is `Re: ` and the conversation's subject, `subject_source` `thread`). For an email
  also check that `to` holds only the recipient; anything else: close the compose with Discard and
  `gate fail <token> --reason precondition_changed --evidence-file <f>`.
* `gate arm <token> --observed-file <f>`. Exit 6 (`E_OBSERVED_MISMATCH`): the page did not hold the approved
  text; the token failed and nothing was sent. Close the dialog or leave the form without submitting; move on.

## 4. Act once

`pace wait --platform <p> --kind dwell` until `remaining_s` is 0 (5 to 40 seconds of reading time), then one
click on the final button (Submit, Send), by the ref the latest snapshot shows for it:
`{"action": "act", "profile": "jobhunter", "kind": "click", "ref": "e30"}`. Never click it twice, never press
Enter to send, never retry.

## 5. Verify and resolve

* Read the result with `read_toast.js` (and `detect_page.js`; the guard also scans the page).
* Success shown (application confirmation, "Invitation sent", the message in the conversation): write the exact
  text to a file and run `gate confirm <token> --evidence-file <f>`.
* Web-route email: the "Message sent" toast is only half the proof. Read the Sent folder back: open
  `https://mail.google.com/mail/u/0/#search/in:sent to:<recipient> newer_than:1d`, run `read_gmail_list.js`,
  open the row with the approved subject and run `read_gmail_message.js`. Its newest message with
  `from_owner` true must carry the same `readback_text` as the observed file you armed with (same subject line,
  same body). Then write the toast, the Sent row and that `readback_text` to the evidence file, the row's `url`
  as `{"url": "https://mail.google.com/..."}` to a ref file, that `readback_text` alone to a read-back file, and
  run `gate confirm <token> --evidence-file <f> --platform-ref-file <ref> --observed-file <readback file>`. Exit 6
  there means the token is already unknown; do not run `gate unknown` and never send again. Not in Sent, or a
  different text: that is `gate unknown` below, never a second send.
* Anything else after the click (error, nothing visible, page changed, timeout): write what you saw and run
  `gate unknown <token> --note-file <f>`. Unknown keeps the slot blocked until a later check; that is correct.
* `gate fail <token> --reason not_attempted|precondition_changed|form_blocked_before_submit --evidence-file <f>`
  only when you never clicked the final button (the token is still reserved) and the page stopped you before it.
* Uploads: always `resume unstage --token <token>` at the end.

## Reconcile tasks

`reconcile list --route browser` lists tokens in `unknown`. Run the named check (ATS page, sent invitations,
conversation; for a web-route email the Sent, Outbox and Scheduled searches for the recipient with
`read_gmail_list.js`, methods `web_sent_search`, `web_outbox_search`, `web_scheduled_search`) without repeating
the action, write what you saw, and run
`reconcile resolve <token> --result found|not_found|unknowable --method <m> --evidence-file <f>`. A list that did
not load (`loaded` false) is `unknowable`, never `not_found`. Exit 4 `E_TOO_EARLY` means check again after
`retry_after_s`. Only code or the person frees a slot.
