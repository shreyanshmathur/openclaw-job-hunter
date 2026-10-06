# How the agents are fenced in

The job hunter runs five OpenClaw agents: `jobhunter-scout`, `jobhunter-evaluator`, `jobhunter-applier`,
`jobhunter-outreach` and `jobhunter-qc`. Their instructions tell them what to do, but instructions are
only advice to a model. This page describes the parts that do not depend on the model at all: the way
OpenClaw starts these agents, OpenClaw's own exec policy, the `jobhunter-guard` plugin, which OpenClaw runs
before every tool call of these agents, and the checks inside `jh.py`.

If the guard is not loaded, or cannot read this install's files, no job hunter agent can do anything:
every tool call is refused, the heartbeat stops, and the dispatcher stops starting agent cycles.

## The layers

| Layer | What it enforces |
|---|---|
| L1 tool surface | Every job hunter turn (lane cycles, onboarding, identity probes, the QC reviewer) is an OpenClaw cron job with an explicit tool list. On the Claude subscription route OpenClaw then starts Claude Code with its own tools switched off (`--tools ""`, no user settings, no hooks, no MCP servers but OpenClaw's), so the model only has OpenClaw's `exec`, `read`, `write` and (for browser agents) `browser`, and a question to a person is cancelled at once. The guard also asks OpenClaw to narrow any job hunter run to the agent's tools before the prompt is built; that is defense in depth only, and on OpenClaw 2026.9.8 it does not restrict a run started by hand (see "Runs started by hand are not supported"). |
| L2 OpenClaw policy | Per agent: the tool allow and deny lists, `workspaceOnly` file tools, exec `allowlist` with `ask: off` (a command that is not on the list is refused at once, never sent to a person), no elevated exec and no safe bins. The exec allowlist entry accepts only `<python> -I <repo>/scripts/jh.py --agent-proof <proof naming this agent> ...` (it checks the proof's shape only). |
| L3 guard | The rules below: tools, commands, files, browser, other agents, identity. |
| L4 `jh.py` | Runs only as `python -I`, verifies the agent's identity proofs (both by default), then that agent's command list and file folders again. |
| L5 checks after the fact | The guard log (every decision, and a `native_tool` line whenever a Claude Code tool reaches a job hunter agent), the identity probes, `./jobhunter doctor`, the cron job check before every run, and the nightly audit. |

Every requirement has at least two of the preventive layers L1 to L4, except these, which depend on the
guard alone:

- writing files inside the agent's own workspace other than `work/` and `inbox/` (for example its
  `AGENTS.md`, `skills/` or a `.mcp.json`): OpenClaw allows writes anywhere in the workspace;
- the browser profile: OpenClaw has no setting that pins a browser profile per agent.

The dispatcher, the onboarding wizard, the probes and the QC reviewer refuse to start an agent run without a
fresh guard heartbeat, so these rows never run without the guard.

## Who can impersonate an agent

Agent identity cannot be forged by an agent whose shell is confined. An agent with an unconfined shell runs
as you and is trusted like you.

A confined shell means OpenClaw's effective exec policy for that agent is `deny`, or `allowlist` with
`ask: off`. Any other agent of yours that has an unconfined shell (`security: full`, or a mode or `ask`
setting where one click allows any command) can read files the guard never sees, edit OpenClaw's config,
the guard or `jh.py`, and so act as a job hunter agent. `./jobhunter doctor` and the installer name every
such agent in red. Give your own agents exec only as an allowlist with `ask: off` if they must not reach the
job hunter.

## Runs started by hand are not supported

The project starts every job hunter turn as an OpenClaw cron job with an explicit tool list and never starts
one any other way. A run started by hand, for example `openclaw agent --agent jobhunter-evaluator` typed by a
person or sent by another agent, is not supported. On OpenClaw 2026.9.8 such a run is not restricted: Claude
Code starts with your user settings, and its own tools (Bash, Read, Write, AskUserQuestion and the rest) are
offered to the model. The guard's tool pin (`pinToolSurface`) and its stray-run notice did not change that in the
release checks, because OpenClaw 2026.9.8 runs the guard's `before_prompt_build` hook only when the plugin entry
grants it: `plugins.entries.jobhunter-guard.hooks.allowConversationAccess: true`. Without that grant the gateway log
shows `typed hook "before_prompt_build" blocked` at every start, and the guard heartbeat keeps
`pin_hook_seen_at: null` even after a cron turn, and `./jobhunter doctor` shows that as an informational WARN.
The installer never grants it for you. Whether the pin restricts a run started by hand once the grant is set has
not been checked yet; until it is, treat such runs as unrestricted.

What still holds in such a run:

- A Claude Code tool call that needs a permission (Bash, writes, reads outside the agent's folder) is
  refused at once: the exec policy of every job hunter agent is `allowlist` with `ask: off`, so OpenClaw
  denies the request without asking anyone. Your own `~/.claude/settings.json` applies in such a run, so a
  `permissions.allow` list or a `permissions.defaultMode` there can let calls through; `./jobhunter doctor`
  warns when it sets either.
- A Claude Code tool call that reaches the plugin hooks is refused by the guard (`G_TOOL_DENIED`) and leaves
  a `native_tool` line in the guard log.
- OpenClaw's own `exec`, `read`, `write` and `browser` calls are judged by the guard and `jh.py` exactly as
  in a cron run.
- When OpenClaw runs the guard's `before_prompt_build` hook (see above), the guard tells the model at the start
  of such a run (any run of a job hunter agent that OpenClaw reports as not started by a cron job, for example
  `openclaw agent` or a heartbeat) that the run is not supported, that it must use no tool and ask nothing, and that it must reply with one `G_STRAY_RUN` line and end. It
  also writes a `stray_run` line to the guard log. This notice is text for the model, not a lock.

What does not hold: a question to a person with AskUserQuestion is not a permission request, so it can wait
until the run times out, and only the model keeps such a run from asking or from using a Claude Code tool
that needs no permission. In the release check on 2026.9.8 the model declined both on its own, following its
`AGENTS.md` (the hook was blocked there, so the `G_STRAY_RUN` notice played no part); that is model behavior,
not enforcement. Do not start job hunter agents by hand: use
`./jobhunter` and the automations the installer declares.

## The rules

**Health.** Before anything else the guard checks that `private/home.json`, `private/guard.key`, the
command allowlist, the host list, the stop signatures and the database can be read, and that the
database belongs to this install. If not, every tool call of every job hunter agent is refused
(`G_GUARD_UNHEALTHY`).

**Tools.** Each agent may use only its own tools: the scout, applier and outreach agents get `exec`,
`read`, `write` and `browser`; the evaluator gets `exec`, `read` and `write`; the QC reviewer gets none.
Editing, patching, messaging, cron, sessions, web fetch, web search and everything else is refused
(`G_TOOL_DENIED`). Claude Code's own tools (Bash, Read, Write, Edit, Glob, Grep, WebFetch and the rest) are
off for the job hunter agents: OpenClaw does not apply its exec policy or workspace limit to them, so the
guard refuses any call in their shape (`G_TOOL_DENIED: Claude Code tools are off for jobhunter agents; this
run is not restricted (see doctor)`) and writes a `native_tool` line to the guard log. Such a line means a
run was started without its tool list; `./jobhunter doctor` reports it. Only the reduced-protection mode N
(`install.sh --cli-tools native --i-accept-reduced-protection`) judges these tools instead of refusing them.

**Commands.** An agent's `exec` call must be exactly one `jh.py` command, typed as
`<python> <repo>/scripts/jh.py <command> <arguments>`, and the command must be on that agent's list in
`scripts/jobhunter/acl.json` with arguments of the right shape (ids, tokens, numbers, listed words, email
addresses, and file paths inside the agent's own `work/` or `inbox/` folder). Pipes, quotes, `&&`,
redirects, other programs such as `sqlite3` or `rm`, a word starting with `=`, a pseudo terminal,
background runs, extra environment variables, another exec host, interpreter options other than `-I`, and
the options `--home`, `--pin-stdin`, `--grant`, `--human` and anything starting with `--agent-p` are refused
(`G_EXEC_SHAPE`, `G_EXEC_ACL`, `G_EXEC_PARAM`). The display `title` that OpenClaw 2026.9.8 adds to exec calls
is allowed when it is one line of at most 120 characters; it is never part of the command and nothing uses it. The guard then rewrites the call to
`<python> -I <repo>/scripts/jh.py --agent-proof <proof> <command> <arguments>`, sets the time limit to 90
seconds and the working folder to the agent's `work/` folder. No agent's list contains approve, skip,
edit, unpause, breaker reset or any setting that loosens a limit.

**Files.** An agent may read only inside its own workspace and write only inside its own `work/` and
`inbox/` folders. OpenClaw reads some path spellings in ways a path check could miss (`~` and `~/x` are the
home folder, a leading `@` is dropped, `node://` goes to another computer), so the guard refuses those
forms outright, together with `..`, `$`, backslashes, URIs such as `file:` and empty or binary paths: the
agent writes plain paths, best absolute ones inside its workspace. The call must come from the agent's own
workspace, links are resolved first (so a symbolic link cannot lead out), nothing under the repo's
`private/` folder and no file named `guard.key` is ever readable, and the names `AGENTS.md`, `CLAUDE.md`,
`.mcp.json`, `.claude`, `skills` and `.git` are never written, in any case (`G_PATH_DENIED`).

**Browser.** Every browser call must name the `jobhunter` profile on this computer; a call without a
profile or with another one is refused, never filled in (`G_BROWSER_PROFILE`).
Some places are never allowed, not even to look at: every Google page except Gmail itself (Docs, Sheets,
Drive, Apps Script, Contacts, Calendar, Photos, Keep, Google Search, your Google account pages and every
other `google.com` host), the Microsoft and Apple account and sign-in pages (`login.microsoftonline.com`,
`login.live.com`, `account.live.com`, `account.microsoft.com`, `appleid.apple.com`, `idmsa.apple.com`,
`account.apple.com`), LinkedIn's "sign in to another site" pages (`linkedin.com/oauth`,
`linkedin.com/uas/oauth2`), Hacker News, LinkedIn settings, local files, browser internals, this computer
and the home network (`G_HOST_NEVER`). So a site's "Sign in with Google, Microsoft, Apple or LinkedIn" can
never lead the agent into one of your own accounts. The guard reads a page address the way the browser does, so
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
The code steps below write their one button click to the same file, marked `"by": "code"`; the guard
counts those submit lines exactly like its own, so a code step uses the token's two submits too.

**Passwords, codes and sign-in buttons.** A job hunter agent never types into a password, verification
code, one-time code or PIN field: typing or filling a text box, search box or number box whose name in the
last snapshot matches `secret_field_names` in `openclaw/guard-hosts.json` (password, passcode, verification
code, security code, one-time, OTP, PIN, confirm password) is refused, with or without a token
(`G_SECRET_FIELD: secret fields are filled by code: jh.py account create, account signin or code submit`).
A fill with several fields is checked field by field, and a type aimed by a selector that names a password
input is refused too. Clicking such a field and then typing or pressing keys without naming a field (which
would type into it) is refused the same way: after the click the tab is marked, and the mark goes away
with the next snapshot, the next navigation or a click on another field the snapshot showed. The agent also
never clicks a button or link named like "Sign in with Google" (or LinkedIn, Microsoft, Apple, Facebook,
Indeed, GitHub), "Use your Google account", or a CAPTCHA widget ("I'm not a robot", "Verify you are human",
reCAPTCHA, hCaptcha, Turnstile): `forbidden_names` in `openclaw/guard-hosts.json`, refused for every job hunter
agent with or without a token (`G_TOOL_DENIED`). Your accounts are never used to sign in to a site, and a
CAPTCHA is always solved by you.

**Code steps in the agent's browser.** Creating an account on an ATS site, signing in to it, and entering
an emailed code or opening an emailed sign-in link are done by code, not by the model, and only on sites
where you allowed it (`private/consent.json` `capabilities`: `ats_accounts` and `email_codes`, per site,
default No, recorded with your PIN). The agent runs `jh.py account create`, `account signin`, `code submit`
or `code open-link` with ids, its token and a tab id only. `jh.py` reads the password from the macOS
Keychain, or the code or link from your mailbox, and drives the agent's own `jobhunter` browser profile
directly over the Chrome DevTools Protocol (CDP) on the profile's loopback port: it fills the field, makes
the one click of that step, checks the page for stop pages before and after, and prints an outcome with no
secret in it. The model never sees a password, a code or a sign-in link, and no browser tool call or result
carries one; a password field is checked to be masked before the click.

**ATS sites.** Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Recruitee, BambooHR, iCIMS, SAP
SuccessFactors, Taleo, Oracle Cloud HCM and Jobvite forms are public pages (no consent needed to read
them). Oracle Cloud HCM tenants are recognised by their host shape (`<tenant>.fa.<region>.oraclecloud.com`,
`host_patterns` in `openclaw/guard-hosts.json`, tried after the listed domains). Each ATS has its own breaker
(`ats:workday`, `ats:icims`, ...), which the core opens on a security page, repeated CAPTCHAs, failed codes
or a revoked consent; while it is open every browser action on that ATS's pages stops (`G_BREAKER_OPEN`),
except closing a tab, and other ATS sites go on.

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
An SMS, phone or authenticator code page, an identity check or a social sign-in page on an ATS site is a
stop for the whole site (`ats_security`), never a job-level stop, and it always outranks an email code or
account page shown with it. An account wall or an emailed code page on a site where you allowed the matching
capability (`ats_accounts` or `email_codes`) is not stopped: the guard writes a `capability_page` line to
its log instead of the stop, still hands the page to `jh.py detect` (which records it and answers with the
code step to run), and the agent goes on with that step. A capability can never lift a stop for a whole
site: the guard refuses to load a stop signature that combines the two, and is then unhealthy.

**CAPTCHA hand-off.** A CAPTCHA on an application form is a job-level stop as before (no more typing or
clicking on that site in this session). Its stop file also names the agent (`agent`), the agent's open
token (`token`, when it holds one) and the browser tab (`tab_id`), so `jh.py detect --source guard` can
pause just that application: it releases or settles the token by the gate rules, takes a screenshot of the
tab and sends it to you with a short code. You solve the CAPTCHA yourself in the agent's browser window and
reply `/jh continue <code>` (or run `./jobhunter continue <code>`); `jh.py` checks the tab over CDP, read
only, and puts the job back at the front of the apply queue, where the applier resumes it under the normal
gate. No part of the job hunter solves, clicks or listens to a CAPTCHA, and no outside service is used.
The output of the read-only page drivers is checked by what it says, not by its field names: a form
read-back that reports "no CAPTCHA" (`captcha_visible: false`) passes, and one that reports a CAPTCHA or
an account wall is treated like that page.

**Your chat commands.** `/jh approve`, `skip`, `edit`, `answer`, `pause`, `status`, `inbox`, `lower` and
`continue` (after you solved a CAPTCHA in the agent's browser window) are handled by the guard itself, with no model involved, and only for you: OpenClaw must report you as
the owner, or your channel and number must be listed in the plugin's `ownerFallback` setting. Anyone
else gets `G_NOT_OWNER`. The guard signs each command with `private/guard.key`, the signature is valid
for two minutes and only once, and `jh.py` checks it. Unpausing, resetting a breaker and raising any
limit stay terminal only, behind your PIN.

**Other agents.** Your own agents (for example `main`) may run only the read-only `jh.py` commands
(`status`, `inbox`, `approvals list`, `budget`, `breaker status`, `home show`), and only if they are
listed in `publicReadonlyAgents`. Anything else gets the hint "use /jh in your chat". They also cannot
read the guard key, set the `JH_*` identity variables, pass an `--agent-proof`, drive the `jobhunter`
browser profile, copy Chrome cookies into it (`importprofile` with `into: "jobhunter"`) or point a browser
dashboard at it; the profile name is compared with spaces trimmed and capitals ignored, as OpenClaw reads it.
Their file tools (OpenClaw's and Claude Code's) cannot read the repo's `private/` folder or the OpenClaw
state folder, and cannot write the repo, the job hunter workspaces or the OpenClaw state folder: the guard
reads each path the way OpenClaw does (`~`, `@`, `node://`, relative paths, links) before it compares
(`G_PATH_DENIED`). Their own workspace stays usable when it lies inside the OpenClaw state folder. Their
cron, session, subagent, agent and gateway tools cannot name a job hunter agent, and their shell cannot run
`openclaw` cron, agent, session, config, plugin or approval commands that name the job hunter
(`G_OTHER_AGENT_DENIED`).
The shell checks read the text of the command (they also see quotes pulled apart, `~`, relative paths
against the working folder, and shell patterns such as `guard.k*`, `private/*` or `p?ivat?/*`, which they
expand on disk), so they are a best effort: an agent that can run shell commands as your user without
limits can still reach a file through a spelling no text check sees. That is the trust boundary above.

**Identity.** For every `jh.py` call of a job hunter agent the guard writes two proofs, both signed with
`private/guard.key`, valid for two minutes and usable once: the `--agent-proof` option in the command, which
names the agent, the session and the exact arguments, and `JH_AGENT_PROOF` in the command's environment,
which names the agent and the session. `jh.py` requires both (the installer can switch to one of them where
OpenClaw does not pass the other), checks that they name the same agent and session, refuses a proof it has
seen before, and only then applies that agent's command list. A proof typed by a model is refused by the
guard, and a copied one is useless: it is bound to one command, one session and one use. With the
environment proof alone, `JH_SESSION_KEY` must name the session the proof was made for. In mode N
(`--cli-tools native`) agents call `jh.py` from Claude Code's own Bash, which never gets the environment
proof, so that mode uses the `--agent-proof` option alone (the installer picks it and refuses the other
carrier). A call that comes from an agent tool without a proof is treated as an unproven agent (read-only
commands only), never as you. A process started by Claude Code (`CLAUDECODE` or `CLAUDE_CODE_ENTRYPOINT` set)
counts as such an agent tool wherever it runs; `./jobhunter`, `install.sh` and the command automations drop
those two names before they start `jh.py`, so your own commands, also from a Claude Code terminal, still run
as you. The names are a hint, not a lock: removing them proves nothing.

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
- The secret field check reads field names from the last snapshot. A password or code box with no name,
  or a name no pattern lists, is not recognised; the model still has no password or code to type (they
  never reach its context), and the code steps clear a code field after a rejection.
- Any process that runs as your macOS user can reach the loopback CDP port of the `jobhunter` browser
  profile, which is the same boundary as the OpenClaw browser itself (see "Who can impersonate an agent").
  A code is visible on the page for the few seconds between a code step's fill and its click, inside one
  `jh.py` call with no model turn in between.

## Block codes

| Code | Meaning | What the agent does |
|---|---|---|
| `G_GUARD_UNHEALTHY` | the guard cannot verify this install | end the cycle |
| `G_TOOL_DENIED` | tool or browser action not allowed for this agent (also a click on a social sign-in button or a CAPTCHA widget) | end the cycle; a CAPTCHA or a sign-in is yours to do |
| `G_EXEC_SHAPE` | not one plain `jh.py` command | end the cycle |
| `G_EXEC_ACL` | command not on this agent's list | end the cycle |
| `G_EXEC_PARAM` | an argument has the wrong shape or is forbidden | end the cycle |
| `G_PATH_DENIED` | file outside the allowed folders | end the cycle |
| `G_BROWSER_PROFILE` | no browser profile, another profile, node or dashboard | pass `profile: "jobhunter"`; end the cycle |
| `G_HOST_NEVER` | a never-allowed page | end the cycle |
| `G_BREAKER_OPEN` | kill switch or breaker open | end the cycle |
| `G_NO_TOKEN` | fill or submit without a live token for this site | do the gate steps first, or move on |
| `G_NOT_ARMED` | submit before `gate arm` or before the reading pause ended | do the missing step |
| `G_COMMIT_BUDGET` | third submit on one token | end the cycle; never retry a submit |
| `G_SCRIPT_NOT_ALLOWED` | page script that is not a listed driver | end the cycle |
| `G_UPLOAD_PATH` | upload of anything but the staged resume | end the cycle |
| `G_STOPPED` | a stop page was seen in this session (or, for writes on one site, a job-level stop) | mark the job if it was a job-level stop, run `jh.py cycle end`, finish with `CYCLE_DONE` |
| `G_NOT_OWNER` | `/jh` from someone who is not the owner | nothing runs |
| `G_NO_CONSENT` | a site you have not allowed (or have revoked) in `private/consent.json` | end the cycle; you allow the site with `./jobhunter browser consent` |
| `G_PAGE_UNKNOWN` | a read or an action on a tab whose page the guard does not know | end the cycle; the next cycle opens its page first |
| `G_OTHER_AGENT_DENIED` | another agent tried to start, message, edit or reconfigure a job hunter agent or its jobs | nothing runs |
| `G_SECRET_FIELD` | typing into a password, code or PIN field (or keys right after clicking one) | run the code step (`jh.py account create`, `account signin` or `code submit`), or hand the job to you |
| `G_STRAY_RUN` | not a block: the notice at the start of a job hunter run not started by a cron job (unsupported) | use no tool, ask nothing, reply with the one `G_STRAY_RUN` line and end |

## Checking it

- `./jobhunter doctor` reports the guard heartbeat and runs a blocked-call test; `./jobhunter doctor --probe`
  runs one identity probe per tool agent.
- `logs/guard-YYYY-MM.jsonl` lists every decision for the job hunter agents (no page text), and a
  `native_tool` line for every Claude Code tool call that reached one of them.
- The plugin's tests replay recorded sessions against the rules:
  `node --test 'openclaw/plugins/jobhunter-guard/test/*.test.ts'`.
