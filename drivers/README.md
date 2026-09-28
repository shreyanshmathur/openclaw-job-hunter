# Drivers: read-only page functions

The browser agents may run page scripts only through the browser act kind `evaluate`, and only when the
script's sha256 is listed in `drivers/manifest.json` (jobhunter-guard rule R4, class `driver`). These files
are those scripts. Each one is a single zero-argument arrow function that reads the page and returns a JSON
object. None of them clicks, types, sets a value, dispatches an event, fetches, stores or navigates: every
change to a page goes through the gate with act kind `type` (`slowly: true`) and ref clicks, where the guard
can see it.

| Driver | Used for | Returns |
|---|---|---|
| `detect_page.js` | after every navigation | `detect_file` (12.11, write it verbatim), `hint` (`clear`, `stop`, `needs_human`), `top` (the most severe matching signature: id, reason_code, trip, job_needs_human, scope), `matched` (most severe first) |
| `read_applied_state.js` | application precheck | `checks` for kind `application` (12.7) |
| `read_form.js` | form read-back before `gate arm` | `observed` (`fields`, `resume_filename_visible`), required fields left empty, CAPTCHA and account-wall flags |
| `read_li_invite_dialog.js` | LinkedIn invitation precheck and note read-back | `profile` (name, vanity slug, degree, primary button, Connect under More), `note_text` and dialog state |
| `read_compose.js` | LinkedIn message box, InMail compose and Gmail compose read-back; conversation messages | `observed_text` in the observed-file format (Gmail: the lines `Subject: <subject>` and `To: <addresses>`, plus `Cc: ...` and `Bcc: ...` when the window holds such an address, a blank line, the body; a LinkedIn compose with a subject field, an InMail: `Subject: <subject>`, blank line, body; the box text otherwise), Gmail `to`, `cc`, `bcc` (an address in no To, Cc or Bcc row counts as To), `subject_field_present`, `messages` |
| `read_toast.js` | proof after the single click | toasts, success and error phrases |
| `read_identity.js` | identity check (12.12) | `{platform, observed}` |
| `read_sent_invites.js` | invitation manager: gauge, reconcile, acceptance | `people_count`, `invites` |
| `read_gmail_list.js` | web_ui route: Sent, Outbox and Scheduled precheck searches, reply and delivery-failure searches, the Sent-folder read of `mail audit` | `loaded`, `count` (null when the list could not be read), `rows` (participant addresses, subject, date, thread id, url), `complete`, `sent_read` (the `mail audit --file` object) |
| `read_gmail_message.js` | web_ui route: a conversation in the replies lane and the Sent-folder read-back after a send | `messages` (`msg_ref` `gm:<id>`, sender, `from_owner`, `recipients` and the same by field in `to`, `cc`, `bcc`, `date`, body without quoted history, `is_bounce`, `bounce_addresses`, `readback_text` in the Gmail observed-file format with its To, Cc and Bcc lines) |
| `read_login_state.js` | the session check of a consented site (first page of a cycle) | `state` (`ok`, `logged_out`, `checkpoint`, `unknown`), `account_email`, Gmail `identity`, the installer's login-probe fields (`jh_login_probe`) |

## How an agent runs a driver

The renderer copies these files unchanged to `WS_ROOT/<role>/ref/drivers/`. The agent reads the file and passes
its exact text as `fn` of the browser act kind `evaluate`:

```json
{"action": "act", "profile": "jobhunter", "kind": "evaluate", "fn": "<exact text of ref/drivers/detect_page.js>"}
```

The hash is the sha256 of the UTF-8 file text with surrounding whitespace removed; the guard accepts the text
with or without the final newline. A top-level `{"action": "evaluate"}` is not a browser action the guard
knows (it counts as a submit and is refused), and any other text is refused as a script that is not a driver.
The recorded transcripts in `tests/fixtures/browser/` write `{DRIVER:<name>}` for this text.

## Changing a driver

1. Edit the `.js` file. Keep it ASCII, one arrow function, no page writes (the static test
   `tests/test_drivers_static.py` lists the forbidden calls, including any property assignment), and no
   `__NAME__` style words (the workspace renderer substitutes those).
2. Run `python3 tools/gen_drivers.py`. It embeds the stop signatures from `scripts/jobhunter/detect/*.json`
   (merged with a research baseline), each with its id, reason and whether it trips a breaker, and the severity
   order of `jobhunter.detect.SEVERITY` into `detect_page.js`, and rewrites `manifest.json`.
3. Run the tests. CI runs `python3 tools/gen_drivers.py --check` and fails when a file is stale. The node tests in
   `tests/test_drivers_static.py` run the drivers against the recorded-shape pages in
   `tests/fixtures/browser/gmail_web_pages.json` (Gmail lists and conversations, login pages, stop pages).
4. Re-render the workspaces (`./install.sh` or `jh.py install render-workspaces`) and restart the gateway so the
   guard loads the new manifest.

LinkedIn, Gmail and job-board markup changes often. When a driver stops finding what it should, it returns
empty values instead of guessing; agents treat that as "not seen" and never as proof of success.
