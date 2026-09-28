# How the agents are fenced in

The job hunter runs five OpenClaw agents: `jobhunter-scout`, `jobhunter-evaluator`, `jobhunter-applier`,
`jobhunter-outreach` and `jobhunter-qc`. Their instructions tell them what to do, but instructions are
only advice to a model. This page describes the parts that do not depend on the model at all: the
`jobhunter-guard` plugin, which OpenClaw runs before every tool call of these agents, and the database
rules behind `jh.py`.

If the guard is not loaded, or cannot read this install's files, no job hunter agent can do anything:
every tool call is refused, the heartbeat stops, and the dispatcher stops starting agent cycles.

## The rules

**Health.** Before anything else the guard checks that `private/home.json`, `private/guard.key`, the
command allowlist, the host list, the stop signatures and the database can be read, and that the
database belongs to this install. If not, every tool call of every job hunter agent is refused
(`G_GUARD_UNHEALTHY`).

**Tools.** Each agent may use only its own tools: the scout, applier and outreach agents get `exec`,
`read`, `write` and `browser`; the evaluator gets `exec`, `read` and `write`; the QC reviewer gets none.
Messaging, cron, sessions, web fetch, web search and everything else is refused (`G_TOOL_DENIED`).

**Commands.** An agent's `exec` call must be exactly one `jh.py` command, typed as
`<python> <repo>/scripts/jh.py <command> <arguments>`, and the command must be on that agent's list in
`scripts/jobhunter/acl.json` with arguments of the right shape (ids, tokens, numbers, listed words, email
addresses, and file paths inside the agent's own `work/` or `inbox/` folder). Pipes, quotes, `&&`,
redirects, other programs such as `sqlite3` or `rm`, a pseudo terminal, background runs, extra
environment variables, another exec host, and the options `--home`, `--pin-stdin`, `--grant` and
`--human` are refused (`G_EXEC_SHAPE`, `G_EXEC_ACL`, `G_EXEC_PARAM`). The guard also sets the time limit
to 90 seconds and the working folder to the agent's `work/` folder. No agent's list contains approve,
skip, edit, unpause, breaker reset or any setting that loosens a limit.

**Files.** An agent may read only inside its own workspace and write only inside its own `work/` and
`inbox/` folders. Links are resolved first, so a symbolic link cannot lead out (`G_PATH_DENIED`).

**Browser.** Every browser call uses the `jobhunter` profile on this computer (`G_BROWSER_PROFILE`).
Some places are never allowed, not even to look at: every Google page except Gmail itself (Docs, Sheets,
Drive, Apps Script, Contacts, Calendar, Photos, Keep, Google Search, your Google account pages and every
other `google.com` host), Hacker News, LinkedIn settings, local files, browser internals, this computer
and the home network (`G_HOST_NEVER`). The guard reads a page address the way the browser does, so
capital letters, a port, a trailing dot, backslashes or an encoded dot in the host do not get past it.
When the kill switch `state/PAUSED` exists, every browser action stops (`G_BREAKER_OPEN`). When the
breaker for a site (or the global breaker) is open, every browser action on that site stops too, except
closing a tab, so the agent can still clean up (for example after `./jobhunter browser forget <site>`,
which trips that site's breaker). Closing a tab is the only exception because it shows the agent nothing.
Listing tabs is not an exception: the list carries the title of every open tab, and a Gmail title shows
your account address, so a tab list is treated like reading the page of the tab it was made from.

**Sites you allowed.** The agent's browser profile only has the logins of the sites you allowed in
`./jobhunter init` (or later with `./jobhunter browser consent`), and `private/consent.json` records which
ones. A login is a set of cookies for a whole domain: allowing Gmail copies your Google account session
(every `google.com` cookie), and allowing Work at a Startup copies your YC account session
(`ycombinator.com`). That is why the guard lets the agents use only Gmail on `google.com` and never
Hacker News. The guard reads that file on every browser call (it never writes it) and refuses to open,
read or act on LinkedIn, Gmail or a job board that has no active row there (`G_NO_CONSENT`), even if the
agent was told to. Only closing the tab stays allowed on such a page (listing tabs is refused there too,
because the list would show that page's title). After `./jobhunter browser forget <site>` the very next call
on that site is refused. Job application forms on ATS sites (Greenhouse, Lever and the like) and company
career pages need no login, so they need no consent. If the file is missing, cannot be read, is not
valid JSON, is a link, belongs to another user, or can be changed by other users on this computer, the
guard treats every site as not allowed.

**Tabs the guard has not seen.** The guard judges a page by its address, and it learns the address of a
tab when the agent opens it or navigates it, when a browser result reports it, or when the agent lists
its tabs. A tab it knows nothing about (for example a tab left open by an earlier cycle, in a new agent
session) could be on any site, so on such a tab only opening a page, navigating, listing tabs, closing a
tab and the browser's start, status, profiles and doctor calls run. Reading it (snapshot, screenshot,
page text, a driver) or acting on it is refused (`G_PAGE_UNKNOWN`) until its page is known, and from then
on the never list, the consent check and the breakers apply to it as to any other page. Listing tabs is
how the agent learns which pages its tabs show, so it runs on such a tab, but the kill switch and the
global breaker still stop it. The same tab answers to its raw id, its tab id (such as `t1`) and its label
once a browser result has named them.

The guard sorts every browser action into one of five kinds:

| Kind | Examples | Who may do it |
|---|---|---|
| read | open a page, snapshot, screenshot, page text, scroll keys, hover | every browser agent |
| harmless click | a link (unless it is named like an action, such as "Apply", "Message" or "Follow"), or a button named like "Show more", "Next", "Page 2" | every browser agent |
| driver | one of the read-only page functions in `drivers/`, checked by hash | every browser agent |
| fill | typing, choosing options, ticking boxes, uploading, opening the form ("Apply", "Connect", "Message", "Attach resume") | the applier and outreach agents, only while `jh.py gate reserve` has given them a live token for that site |
| submit | every other click, the Enter key (also NumpadEnter, Return and a line break typed key by key), the Space key, accepting a dialog, clicks by screen position, clicks on anything the last snapshot did not show | only after `jh.py gate arm` has checked that the page holds exactly the approved text, only after the reading pause from `jh.py pace wait --kind dwell`, and at most twice per token |

The guard judges a call the way OpenClaw runs it. Act options given next to `request` fill in the
request, as OpenClaw does. The tab is the one OpenClaw acts on (the request's `targetId` first), and a
call that names two different tabs is refused (`G_TOOL_DENIED`), as is a call that uses the snake case
spellings `target_id`, `target_url` or `input_ref`. Typing with `slowly: true` first clicks the field, so
it is judged as that click too, and a line break in slowly typed text counts as Enter unless the field is
a text box that `multiline_names` in `openclaw/guard-hosts.json` lists for the token's kind (the Gmail
message body, a cover letter box). An upload with `ref` clicks that ref to open the file chooser and is
judged as that click (at least a fill); an upload with `inputRef` is a fill.

So an agent that skips the gate cannot type into a form or press Send (`G_NO_TOKEN`), an agent that did
not read back its text cannot submit (`G_NOT_ARMED`), and nothing can be submitted three times
(`G_COMMIT_BUDGET`). Page scripts other than the listed drivers are refused (`G_SCRIPT_NOT_ALLOWED`),
and the only file that can be uploaded is the resume copy that `jh.py resume stage` prepared for this
token (`G_UPLOAD_PATH`). Every fill and submit action is written to `state/guard/<token>.jsonl`, which
`jh.py gate fail` reads: a token with a recorded submit can never be marked "not sent" by an agent.

**Stop pages.** After every browser result the guard compares the page address and text with the stop
signatures in `scripts/jobhunter/detect/` (CAPTCHA, security checks, sign-in walls, "unusual activity",
invitation limits, HTTP 429 and 999, and so on). When a page matches several, the most serious one
counts, the same way `jh.py detect` picks it (a restricted LinkedIn account before a plain security
check, a stop for the whole site before a stop for one job). On a match it saves the page to
`state/guard/stop-*.json`, runs `jh.py detect --source guard`, which opens the breaker and alerts you,
and stops that agent session: every later call is refused (`G_STOPPED`) except `jh.py detect`,
`jh.py breaker trip`, `jh.py cycle end` and notes in the agent's own `work/` folder. This works even if the agent never notices the page. A stop that
concerns one job only (a CAPTCHA or an account wall on one application form) does not stop the session:
the guard reports it the same way and then refuses every fill and submit action on that site for the
rest of the session, so the agent can hand the job to you with `jh.py job set-status` and end the cycle.
The output of the read-only page drivers is checked by what it says, not by its field names: a form
read-back that reports "no CAPTCHA" (`captcha_visible: false`) passes, and one that reports a CAPTCHA or
an account wall is treated like that page.

**Your chat commands.** `/jh approve`, `skip`, `edit`, `answer`, `pause`, `status`, `inbox` and `lower`
are handled by the guard itself, with no model involved, and only for you: OpenClaw must report you as
the owner, or your channel and number must be listed in the plugin's `ownerFallback` setting. Anyone
else gets `G_NOT_OWNER`. The guard signs each command with `private/guard.key`, the signature is valid
for two minutes and only once, and `jh.py` checks it. Unpausing, resetting a breaker and raising any
limit stay terminal only, behind your PIN.

**Other agents.** Your own agents (for example `main`) may run only the read-only `jh.py` commands
(`status`, `inbox`, `approvals list`, `budget`, `breaker status`, `home show`), and only if they are
listed in `publicReadonlyAgents`. Anything else gets the hint "use /jh in your chat". They also cannot
read the guard key, set the `JH_*` identity variables, drive the `jobhunter` browser profile, copy Chrome
cookies into it (`importprofile` with `into: "jobhunter"`) or point a browser dashboard at it; the
profile name is compared with spaces trimmed and capitals ignored, as OpenClaw reads it.
These checks read the text of the call (they also see quotes pulled apart and shell patterns such as
`guard.k*`, `private/*` or `j?.py`), so they are a best effort: an agent that can run shell commands as
your user can still reach a file through a spelling no text check sees. Give such agents exec only if you
trust them with your job hunter install.

**Identity.** For every exec call of a job hunter agent the guard sets `JH_AGENT_ID` (and a signed
`JH_AGENT_PROOF`), so `jh.py` knows which agent is calling and applies that agent's command list again.

## What still depends on the model

- On LinkedIn, in Gmail (web route) and on job site forms the agent types the approved text. The guard
  cannot see the letters it types, but `jh.py gate arm` compares the read-back with the approved text
  before any submit is possible, and on the Gmail web route `jh.py gate confirm --observed-file`
  compares the Sent-folder copy with it again (a different copy makes the token unknown, never sent).
- On the Gmail web route the guard also cannot see which address the agent types into the To field.
  Both read-backs carry the message's `Subject:`, `To:` and, when Gmail shows them, `Cc:` and `Bcc:` lines,
  and both must name exactly one To address, equal to the reserved recipient (the address the precheck,
  the duplicate rules, the exclusions and the address check saw), with no Cc or Bcc. At `gate arm` any other
  recipient fails the token before any submit is possible; at `gate confirm` it makes the token unknown,
  never sent, and it is never sent again.
- A tab list shows the title of every open tab of the agent's browser profile, and the guard judges it by
  the page of the tab it was made from, not by the other tabs it lists. A tab left open on a site you have
  since revoked can therefore still show its title in a list made from another tab, until that tab is
  closed.
- Clicking "Connect" or "Apply" opens a form and is allowed as a fill action while a token is held. A
  site that sends immediately on such a click would send before `gate arm`. The click is recorded in the
  token log as a fill line that names the button, and the nightly audit compares the site's sent list
  with the ledger.
- The guard reads stop pages from what the browser tool returns. A stop page that shows no known text
  is caught by the agent's own `detect` call, the confirmation checks and the nightly audit.

## Block codes

| Code | Meaning | What the agent does |
|---|---|---|
| `G_GUARD_UNHEALTHY` | the guard cannot verify this install | end the cycle |
| `G_TOOL_DENIED` | tool or browser action not allowed for this agent | end the cycle |
| `G_EXEC_SHAPE` | not one plain `jh.py` command | end the cycle |
| `G_EXEC_ACL` | command not on this agent's list | end the cycle |
| `G_EXEC_PARAM` | an argument has the wrong shape or is forbidden | end the cycle |
| `G_PATH_DENIED` | file outside the allowed folders | end the cycle |
| `G_BROWSER_PROFILE` | another browser profile, node or dashboard | end the cycle |
| `G_HOST_NEVER` | a never-allowed page | end the cycle |
| `G_BREAKER_OPEN` | kill switch or breaker open | end the cycle |
| `G_NO_TOKEN` | fill or submit without a live token for this site | do the gate steps first, or move on |
| `G_NOT_ARMED` | submit before `gate arm` or before the reading pause ended | do the missing step |
| `G_COMMIT_BUDGET` | third submit on one token | end the cycle; never retry a submit |
| `G_SCRIPT_NOT_ALLOWED` | page script that is not a listed driver | end the cycle |
| `G_UPLOAD_PATH` | upload of anything but the staged resume | end the cycle |
| `G_STOPPED` | a stop page was seen in this session (or, for writes on one site, a job-level stop) | mark the job if it was a job-level stop, run `jh.py cycle end`, reply `NO_REPLY` |
| `G_NOT_OWNER` | `/jh` from someone who is not the owner | nothing runs |
| `G_NO_CONSENT` | a site you have not allowed (or have revoked) in `private/consent.json` | end the cycle; you allow the site with `./jobhunter browser consent` |
| `G_PAGE_UNKNOWN` | a read or an action on a tab whose page the guard does not know | end the cycle; the next cycle opens its page first |

## Checking it

- `./jobhunter doctor` reports the guard heartbeat and runs a blocked-call test.
- `logs/guard-YYYY-MM.jsonl` lists every decision for the job hunter agents (no page text).
- The plugin's tests replay recorded sessions against the rules:
  `node --test 'openclaw/plugins/jobhunter-guard/test/*.test.ts'`.
