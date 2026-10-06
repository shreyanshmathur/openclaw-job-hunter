---
name: jobhunter-stop-detect
description: When to run detect_page.js and jh.py detect, the per-site session check, what a stop looks like, and exactly what to do on a stop, an expired session or a G_* block.
user-invocable: false
metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}
---

# Stop on the first sign of friction

Every command starts with `__PY__ __REPO__/scripts/jh.py`. Files are whole files you write with the write tool
under `__WS__/work/<cycle_id>/` (absolute paths; there is no edit tool); drivers are read with the read tool.
Every browser call carries `"profile": "jobhunter"`.

## After every navigation

Read `__WS__/ref/drivers/detect_page.js` with the read tool and run it with this browser call, `fn` being the
file's exact text (all of it, unchanged):

```json
{"action": "act", "profile": "jobhunter", "kind": "evaluate", "fn": "<exact text of __WS__/ref/drivers/detect_page.js>"}
```

A top-level `{"action": "evaluate"}` is not a browser action the guard knows: it counts as a submit and is
refused. A changed or shortened text is not an allowlisted driver and is refused too (`G_SCRIPT_NOT_ALLOWED`).
The other drivers in `__WS__/ref/drivers/` run the same way. `detect_page.js` returns:

* `detect_file`: the detect file (12.11). Write it with the write tool to
  `__WS__/work/<cycle_id>/detect-<n>.json`.
* `hint`: `clear`, `stop` or `needs_human`; `matched`: every signature it saw, most severe first; `top`: the
  most severe one (`id`, `reason_code`, `trip`, `job_needs_human`, and `scope`, the breaker area it stops). A
  page can match several signatures (a LinkedIn restriction is served under a `/checkpoint/` address); the most
  severe decides, the same way `jh.py detect` decides: a stop that trips a breaker before a job-level stop, then
  the worst reason.

Run `detect --file <that file>` when the hint is `stop`, when you are about to reserve a token (reserve needs a
clear detection from the last 10 minutes), and after the final click. The guard scans every page on its own and
can stop your session without you; `jh.py detect` makes the stop explicit and immediate. If `detect` answers
`clear` although the hint was `stop`, the page still stops you: write the page to a file and run
`breaker trip --scope <top.scope> --reason-code <top.reason_code> --detail-file <f>` (step 2 below).

## Session check (the first page of LinkedIn, Gmail or a board in a cycle)

The agent's browser profile holds only the logins the person allowed, site by site (`preflight` names the sites
this lane may use; never open a login site it does not name). Sessions expire. Before the first write, and
before the replies lane reads Gmail, check the session once per site and cycle:

1. Open the site's start page (`https://mail.google.com/mail/u/0/#inbox`, `https://www.linkedin.com/feed/`, or
   the board page the lane gave you), for example
   `{"action": "navigate", "profile": "jobhunter", "targetUrl": "https://www.linkedin.com/feed/"}`. Run
   `detect_page.js` and `detect --file` as above.
2. Run `__WS__/ref/drivers/read_login_state.js` (act kind `evaluate`, `fn` = the file's exact text). Its
   `state`:
   * `ok`: go on. Gmail: write its `identity` object to `__WS__/work/<cycle_id>/identity.json` and run
     `identity check --platform gmail --file <that file>`. LinkedIn: the identity check below.
   * `logged_out` (a sign-in page or a password field): the session expired. Write a detail file with one line
     the person can act on, for example `Gmail is signed out in the agent's browser. Fix: ./jobhunter browser
     login gmail (or ./jobhunter browser import on a Mac), then ./jobhunter breaker reset gmail`, and run
     `breaker trip --scope <scope> --reason-code <code> --detail-file <f>` with scope and code `gmail`
     `gmail_auth_failed`, `linkedin` `li_logged_out`, or `site:<board>` `site_challenge`. The breaker sends that
     line to the person's chat. End the cycle.
   * `checkpoint` (a CAPTCHA, "Verify it's you", a code prompt, a checkpoint address): a stop. When `detect`
     did not already trip it, trip it the same way with `gmail_security`, `li_challenge` or `site_challenge`
     and a detail line asking the person to finish it themselves in the agent's browser
     (`./jobhunter browser login <site>`). End the cycle.
   * `unknown`: the page is not what a signed-in session shows. No writes on that site this cycle; note it in
     the cycle summary.
3. Never log in, never type a password or a code, never pick an account, never click "Continue as", never
   retry or reload to make the prompt go away. Only the person signs in again; code keeps the site stopped
   until they reset it.

## What counts as a stop

* CAPTCHA, "security check", "verify it's you", "verify your identity", an email or SMS code, "Try a different
  image", "I'm not a robot".
* URLs with `/checkpoint/`, `/challenge`, `/authwall`, a login or sign-in page, being logged out mid-session.
* "unusual activity", "temporarily restricted", "restricted your account", a request for an ID.
* Limit notices: "weekly invitation limit", "You're out of", "commercial use limit", "reached today's Easy Apply
  limit", "temporary pause", "You have reached a limit for sending mail", "too many requests", HTTP 429 or 999.
* "Email address needed" or "enter their email" in a LinkedIn invitation dialog.
* A modal or page you cannot classify, twice in a cycle.

On an ATS company form (applier only):
* A visible CAPTCHA is not a platform stop. The guard hands it to the owner by itself (a chat message with a
  screenshot and a code for `/jh continue <code>`). Stop typing on that page, run
  `captcha status --job <job_uid>`, leave the tab open and go on with the next job. The owner solves it; you
  never do, and you never click inside it.
* An account wall or an "enter the code we emailed you" page is not a stop when the owner allowed site accounts
  or email codes for that site: `detect` answers `clear` with `flow: "account"` or `flow: "email_code"`, and the
  code-owned steps of skill `jobhunter-apply-ats` take over. Without that consent it is a job-level stop (hint
  `needs_human`): `job set-status <job_uid> --status needs_human --reason account_required` and go on.
* A phone (SMS) code, an authenticator app, an identity check or a "Sign in with Google, LinkedIn, Microsoft or
  Apple" page is a stop of that ATS (reason `ats_security`): it always wins over the email-code page, even when
  both show.

## What to do on a stop (identical in every browser agent)

1. Do not click anything else, do not reload, do not retry, do not try another route to the same action
   (another tab, another URL).
2. Write the page as you saw it to `__WS__/work/<cycle_id>/stop.json` (the `detect_file` object) and run
   `detect --file __WS__/work/<cycle_id>/stop.json`. For an event `detect` cannot see (for example two failed
   actions in a row), run `breaker trip --scope <scope> --reason-code <code> --detail-file <f>`.
3. If a token is open: before the final click it is `gate fail <token> --reason precondition_changed
   --evidence-file <f>`; after the click it is `gate unknown <token> --note-file <f>`.
4. Close the tabs this cycle opened, run `cycle end --cycle <cycle_id>`, reply `CYCLE_DONE` (an onboarding
   run has no cycle: stop using that site and finish as its skill says).

Never solve, bypass or wait out a challenge. Never log in, never enter a code, never create an account. The
person resets the stop after the cooldown; you never do.

## Guard blocks and OpenClaw refusals

A refused call is refused at once; nobody approves it later and nothing waits. A tool call refused with
`G_NO_TOKEN` or `G_NOT_ARMED` means a gate step is missing: go back to it (skill `jobhunter-gate`). Any other
refusal means stop:

* any other `G_*` code (`G_STOPPED`, `G_BREAKER_OPEN`, `G_HOST_NEVER`, `G_EXEC_SHAPE`, `G_EXEC_ACL`,
  `G_EXEC_PARAM`, `G_PATH_DENIED`, `G_TOOL_DENIED`, `G_BROWSER_PROFILE`, `G_SCRIPT_NOT_ALLOWED`, ...);
* an OpenClaw refusal: a command that is not allowed (an exec allowlist miss or an exec denial), a path outside
  the workspace, a tool that is not available or not allowed.

Then write the refusal text to `__WS__/work/<cycle_id>/blocked.txt`, run `cycle end`, reply `CYCLE_DONE`. Never
retry a refused call in another form (another path, another tool, a changed command) and never ask anyone to
allow it.

## Identity

Before the first write of a cycle on LinkedIn (and Gmail on the web route) run `read_identity.js` on your own
profile (or the Gmail inbox; `read_login_state.js` gives the same Gmail `identity` object), write its result to a
file and run `identity check --platform linkedin|gmail --file <f>`. Exit 5 means another account is signed in
(the Gmail account is not the sender address the person set): stop, and never switch accounts yourself.
