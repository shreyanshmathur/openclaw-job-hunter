# jobhunter-guard (OpenClaw plugin)

The guard fences the five `jobhunter-*` agents inside the OpenClaw Gateway. It decides every tool call of
those agents before it runs, watches every browser result for stop pages, gives each exec call the
agent's identity, writes a heartbeat, and handles the owner's `/jh` chat command. It never writes the
database: every change goes through `scripts/jh.py`, which the plugin runs with `execFile` (no shell).

The rules in plain words are in [docs/ENFORCEMENT.md](../../../docs/ENFORCEMENT.md). The design reference
is section 1.4 of the implementation design and section 7 of the Claude subscription route design.

Trust boundary: agent identity cannot be forged by an agent whose shell is confined. An agent with an
unconfined shell runs as you and is trusted like you. (Confined: OpenClaw's effective exec policy for the
agent is `deny`, or `allowlist` with `ask: off`. `./jobhunter doctor` names every other agent in red.)

## Install

`install.sh` does this for you. By hand, from the repo root:

```bash
openclaw plugins install --link "$PWD/openclaw/plugins/jobhunter-guard"
openclaw plugins enable jobhunter-guard
python3 scripts/jh.py install render-guard-config    # prints the config patch path
# apply the patch to plugins.entries.jobhunter-guard.config, then:
openclaw plugins reload jobhunter-guard
```

Config (`plugins.entries.jobhunter-guard.config`):

| Key | Required | Meaning |
|---|---|---|
| `repo` | yes | absolute path of this clone (no spaces) |
| `python` | yes | absolute path of the interpreter the agents must use in exec calls |
| `homeFile` | yes | absolute path of `private/home.json` |
| `publicReadonlyAgents` | yes | agents outside `jobhunter-*` that may run the public read-only `jh.py` commands |
| `ownerFallback` | no | `[{"channel": "whatsapp", "senderId": "+10000000000"}]`: senders accepted as the owner for `/jh` |
| `claudeNativeTools` | no | `"deny"` (default): every Claude Code native tool call of a `jobhunter-*` agent is refused; `"gate"`: judged instead (the reduced-protection mode N only) |
| `pinToolSurface` | no | `true` (default): `before_prompt_build` asks OpenClaw to narrow every `jobhunter-*` run to its ACL tools (defense in depth; on OpenClaw 2026.9.8 it does not restrict a stray `openclaw agent` run, see V7 below); a run not started by cron also gets the `G_STRAY_RUN` notice. Takes effect only when OpenClaw runs the hook: 2026.9.8 needs `plugins.entries.jobhunter-guard.hooks.allowConversationAccess: true` (see V7 below) |
| `proofCarriers` | no | `["argv", "env"]` (default), `["argv"]` or `["env"]`: where the identity proof goes (must equal `cli_route.carriers` in `private/home.json`) |
| `recordEvents` | no | `false` (default); `true` writes `logs/guard-events-<yyyymmdd>.jsonl` (test profiles only) |
| `protectedRoots` | no | `{"read": [...], "write": [...]}` absolute folders other agents may not read or write; `<repo>/private` (read) and `<repo>` (write) are always added, and so is `WS_ROOT` (write) |
| `qcVerdictFile` | no | `false` (default); `true` (fallback F-QC) lets `jobhunter-qc` write its verdict file under `work/verdict/` |

The plugin has no dependencies. It needs Node 24 (bundled with OpenClaw) for `node:sqlite` and for
running TypeScript without a build step.

## What it registers

| Registration | Purpose |
|---|---|
| `api.registerTrustedToolPolicy({id: "jobhunter-guard", evaluate})` | rules R0 to R4 and R7 before every tool call (declared in `contracts.trustedToolPolicies`; the plugin must be explicitly enabled), including the R2 rewrite with `-I` and the argv proof |
| `api.on("before_tool_call", ..., {priority: 100000})` | backstop: the same policy for any call the trusted tier did not decide (the host only reports a refused trusted registration, it does not throw). A call the trusted tier decided is skipped, so nothing is decided twice. |
| `api.on("after_tool_call")` | R5: stop-signature scan of browser results, snapshot ref cache, tab URLs |
| `api.on("resolve_exec_env")` | R8: `JH_AGENT_ID`, `JH_SESSION_KEY`, `JH_RUN_ID` and a fresh env proof `JH_AGENT_PROOF` (v2) for `jobhunter-*` agents |
| `api.on("before_prompt_build")` | tool-surface pin: `{toolsAllow: <the agent's ACL tools>}` for `jobhunter-*` runs (`[]` for the reviewer, `["write"]` with `qcVerdictFile`, `[]` for every jobhunter run while the config is invalid); nothing for other agents or with `pinToolSurface: false`. A `jobhunter-*` run whose hook context carries a `trigger` other than `cron` (9.8: `user`, `manual`, `heartbeat`, ...) also gets `prependSystemContext`, the `G_STRAY_RUN` notice (use no tool, ask nothing, reply with one `G_STRAY_RUN` line and end), and a `stray_run` guard log line; a missing trigger adds nothing, so a cron run is never told to stop. The notice is model behavior only. Defense in depth for runs started without a tool list; no project path depends on it. On OpenClaw 2026.9.8 the hook is a conversation hook: for a plugin that is not bundled with OpenClaw it is registered only with `plugins.entries.jobhunter-guard.hooks.allowConversationAccess: true`; without that grant the gateway log says `typed hook "before_prompt_build" blocked` at every start and neither the pin nor the notice applies (live checks T6, V7) |
| `api.registerCommand({name: "jh", requireAuth: true, acceptsArgs: true})` | R6: owner-only chat command |
| `api.registerService({id: "jobhunter-guard-heartbeat"})` | heartbeat every 5 minutes (full mode only) |

## Owner check for `/jh`

OpenClaw exposes `senderIsOwner` to an installed plugin's command only when the command declares
`requiredScopes`. So:

- without `ownerFallback`, `/jh` is registered with `requiredScopes: ["operator.admin"]`. On a chat
  surface only the owner satisfies it, and the handler also requires `senderIsOwner === true`;
- with `ownerFallback`, `/jh` is registered without scopes and the handler accepts a sender when the host
  says it is the owner or when the channel and sender match an `ownerFallback` entry (phone numbers are
  compared by their digits, so a leading plus sign or a channel suffix after `@` does not matter).

Anyone else gets `G_NOT_OWNER` and nothing runs.

## Files

Read (never written): `private/home.json`, `private/guard.key`, `private/consent.json`,
`scripts/jobhunter/acl.json`, `scripts/jobhunter/detect/*.json`, `openclaw/guard-hosts.json`,
`drivers/manifest.json`, and the database (read only, `node:sqlite`).

Written:

| File | Format |
|---|---|
| `state/guard/heartbeat.json` | `{"install_id", "version", "guard_version", "proof_version": 2, "carriers", "native_tools", "pin_tool_surface", "pin_hook_seen_at", "loaded_at", "beat_at", "acl_sha256", "hosts_sha256"}`, written only while the guard is healthy. `pin_hook_seen_at` is the time of the last `before_prompt_build` call since the gateway loaded the guard, `null` before the first: still `null` after a cron turn means OpenClaw did not run the hook (on 2026.9.8: the `allowConversationAccess` grant is missing) |
| `state/guard/<token>.jsonl` | one line per allowed fill or commit: `{"ts", "token", "agent", "class", "action", "host", "ref", "role", "name"}` (`class` is `fill` or `commit`). The core appends the one click of a code step (`jh.py account create`, `account signin`, `code submit`, `code open-link`) to the same file with an optional `"by": "code"` (for example `{"ts", "token", "agent": "jobhunter-applier", "by": "code", "class": "commit", "action": "code_resubmit", "host", ...}`); a line without `by` is the guard's. The commit budget counts every `class: "commit"` line, whoever wrote it (an unreadable line counts too) |
| `state/guard/stop-<ts>-<rand>.json` | the detect file handed to `jh.py detect --source guard`: `{"platform", "url", "title", "http_status", "text"}`; for a signature with `"handoff": "captcha"` also `"agent"` (the jobhunter agent id), `"token"` (the agent's open token from the ledger; left out when it has none) and `"tab_id"` (the raw targetId of the tab; left out when unknown) |
| `logs/guard-YYYY-MM.jsonl` | one line per decision for `jobhunter-*` agents and per block for other agents: `ts, kind, agent, session, tool, decision, code, class, host, command`; one `kind: "stop"` line per stop match: `code, reason, file, where, trip, host`; one `kind: "capability_page"` line instead for a job-level match whose capability is granted for the page's site: `code, capability, file, where, host`; one `kind: "native_tool"` line per call of a `jobhunter-*` agent in a Claude Code native shape: `agent, session, tool_raw, tool, decision, code` (a run without its tool list; doctor and the selftest identity check look for it). One `kind: "stray_run"` line per prompt of a `jobhunter-*` run not started by a cron job: `agent, session, trigger, code` (`G_STRAY_RUN`). Page text and command arguments are never logged. |
| `logs/guard-events-<yyyymmdd>.jsonl` | only with `recordEvents`: `{at, agent, tool_raw, tool, native, param_keys, ctx_keys, command_redacted, path_rel_ws, decision, code}` per call. Proofs become `jhp2.<agent>.REDACTED` / `jhe2.<agent>.REDACTED`, the repo, `WS_ROOT`, the interpreter and the home folder become `@REPO@`, `@WS@`, `@PY@`, `@HOME@`, paths are relative to `WS_ROOT` (`<outside>`, `<form>` otherwise), contents are never written. Used to record the transcripts of `test/fixtures/transcript-claude-cli-*.json` on a test profile. |

## Identity proofs (R2 and R8, version 2)

Every allowed `jh.py` exec of a `jobhunter-*` agent is rewritten to
`<python> -I <repo>/scripts/jh.py --agent-proof <T> <rest>` (`-I` always; the proof pair only when `argv` is
in `proofCarriers`). `rest` is every `jh.py` argument the agent gave. `resolve_exec_env` adds the env proof.
Before that the exec params may carry only `command`, `workdir` (inside the own workspace), `timeoutSeconds`,
`yieldMs`, `host` (`gateway`), `pty`, `background`, `elevated` (none of the three true), an empty `env`,
`description` and `title`; anything else (OpenClaw 2026.9.8 `ask` and `node` included) is `G_EXEC_SHAPE`.
`title` is the display label 2026.9.8 puts on exec calls: one line of at most 120 characters (no control
or line separator characters), never part of the rewritten command (OpenClaw merges the rewrite into the
original params, so the label is still shown).
Both are minted per call with `private/guard.key`, live 120 seconds, carry a random nonce that `jh.py` uses
once, and name the agent and the first 16 hex of sha256 of the session key (`sk`):

```
argv: "jhp2." + agent + "." + ts + "." + nonce + "." + sk + "." + HMAC(key, "jh-agent-proof\n2\n" + agent + "\n" + ts + "\n" + nonce + "\n" + sk + "\n" + sha256hex("\n".join(rest)))
env:  "jhe2." + agent + "." + ts + "." + nonce + "." + sk + "." + HMAC(key, "jh-agent-env\n2\n" + agent + "\n" + ts + "\n" + nonce + "\n" + sk)
```

`src/grant.ts` holds the minting and verification code; `test/grant.test.ts` checks it against the vectors
in `tests/fixtures/core/proof_vectors.json`, which `tests/test_auth.py` checks the core against. A model
never types a proof: any token starting with `--agent-p` is `G_EXEC_PARAM`. The only exception is a second
decision of the same call (the host deciding the rewritten params again): a `-I <jh.py> --agent-proof <T>`
pair whose `T` this guard minted for the same agent, session and arguments within the TTL is removed first
and a fresh proof is minted. Other agents get `G_EXEC_PARAM` for `--agent-p*` too.

## Claude Code native tools

On the Claude subscription route OpenClaw projects Claude Code's own tools for plugin hooks (`Bash` becomes
`exec` with `command`, `description`, `timeout`; `Read`, `Write`, `Edit` become `read`, `write`, `edit` with
`path = file_path`) and does not apply its exec policy or workspace limit to them. The guard maps raw names
too (`Bash`, `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Glob`, `Grep`, `LS`, `WebFetch`,
`WebSearch`, `Task`, `TodoWrite`; Claude Code's question tool never reaches plugin hooks) and treats a call
as native when it has a raw native name, an exec option `timeout`, `run_in_background` or
`dangerouslyDisableSandbox`, or a `file_path`. With `claudeNativeTools: "deny"` such a call of a
`jobhunter-*` agent is `G_TOOL_DENIED: Claude Code tools are off for jobhunter agents; this run is not
restricted (see doctor)`. With `"gate"` (mode N) Bash may only carry `command`, `description`, `timeout`
(at most 600000 ms), `run_in_background` and `dangerouslyDisableSandbox` (neither true) and is rewritten to
`{command, description, timeout: min(timeout, 90000)}` with the proof; Read and Write may carry only their
native keys and `file_path` must equal `path`; everything else is judged as usual.

## File paths (R3)

Every path parameter (`path`, `file_path`, `filePath`, `apply_patch` targets) of a `jobhunter-*` agent, and
every browser upload path, is refused (`G_PATH_DENIED`, "path form not allowed") when it is not a non-empty
string, holds NUL, a backslash, `$` or `://`, starts with `~` or `@`, starts with a URI scheme such as
`file:`, or has a `..` segment. The base folder of the call (`ctx.workspaceDir`, else `ctx.cwd`, else
`WS_ROOT/<role>`) must resolve to the agent's own workspace ("workspace mismatch" otherwise). The path is
resolved with the realpath of its longest existing ancestor (so new files work and links cannot lead out),
then: never under `<repo>/private` and never a file named `guard.key`; reads inside the workspace; writes
inside `work/` or `inbox/` (with `qcVerdictFile` the reviewer writes only under `work/verdict/`), and never
to `AGENTS.md`, `CLAUDE.md`, `.mcp.json`, `.claude`, `skills` or `.git` as any segment, in any case.

## Health (R0)

The guard is healthy when `home.json` names an `install_id`, `ws_root` and `db_path`; `guard.key` holds
64 hex characters; `acl.json` and `guard-hosts.json` parse; `detect/*.json` yields at least one browser
signature; and the database opens read only with the same `install_id` in `meta`. Health is re-checked
every 15 seconds. While unhealthy, every tool call of every `jobhunter-*` agent is blocked with
`G_GUARD_UNHEALTHY` and no heartbeat is written, so the dispatcher stops starting agent lanes.

## Stop signatures (R5)

`scripts/jobhunter/detect/*.json` belongs to the core ledger unit; the guard reads the same files as
`jh.py detect`:

```json
{"platform": "linkedin", "hosts": ["linkedin.com"],
 "signatures": [{"id": "li_checkpoint_url", "verdict": "stop", "trip": true, "url": "/checkpoint/|/challenge"},
                {"id": "li_login_title", "verdict": "stop", "trip": true, "title": "^linkedin login"},
                {"id": "li_http_block", "verdict": "stop", "trip": true, "http_status": [429, 999]}]}
```

A signature matches when its `url`, `title` or `text` regex (case-insensitive) or its `http_status`
matches the browser result. It applies to a page when the file lists the page's host, or when its
platform equals the host's scope or platform key in `guard-hosts.json`. Entries whose `verdict` is not
`stop` (and `error_after_click`, `smtp` entries) are ignored. The loader also accepts `code`/`name` for
`id`, `url_regex`/`text_regex`/`title_regex`, a `{"stop": {"url": [...], "text": [...]}}` block and a
bare list of signatures, and strips a leading Python `(?i)`.

The result of an allowlisted driver (`act` with kind `evaluate` and a function whose hash is in
`drivers/manifest.json`) is JSON, not a page: the guard scans its string values, never its key names, so
`"captcha_visible": false` from `read_form.js` matches nothing. Driver flags are acted on by value: a
`captcha_visible` or `account_wall` that is true puts one line in front of the scanned text
(`captcha_visible: true (the driver saw a CAPTCHA on the page)`, or an account wall line with "create an
account to apply"), which the platform's own CAPTCHA or account wall signature matches, and so does
`jh.py detect` on the stop file. Driver output that is not valid JSON has its `"key":` names removed.
Other results (snapshots, page text, scripts that are not drivers) are scanned as they are.

When a page matches several signatures the most severe one wins, as in `jh.py detect`: a tripping stop
before a job-level stop (`trip` false), then the order of `reason_code` in `SEVERITY` (the same list as
`SEVERITY` in `scripts/jobhunter/detect/__init__.py`; a test keeps them equal; reasons not in the list
come after it), then load order (file name, then position in the file). So a LinkedIn restriction page
served under `/checkpoint/` is reported as `li_restricted`, not as a plain challenge, and an ATS form
that shows a CAPTCHA and answers 429 stops the session instead of only the job.

On a match the guard writes `state/guard/stop-*.json` with the signature file's platform (for board files
the host's `site:<name>` scope) and a 20,000 character window of the page text that contains the match,
and runs `jh.py detect --file <that> --source guard`, which re-matches it and trips the breaker. A failed
call is retried on each heartbeat for about an hour.

- `trip` true (or missing): the session is stopped. Every later call is refused with `G_STOPPED` except
  `jh.py detect`, `jh.py breaker trip`, `jh.py cycle end` (all still checked against the agent's ACL)
  and file reads and writes within the usual file rules.
- `trip` false (a job-level stop such as a CAPTCHA or an account wall on one ATS form): the session goes
  on, but every fill and submit action on that site is refused with `G_STOPPED` for the rest of the
  session, so the agent can run `jh.py job set-status ... --status needs_human` and end the cycle.
- `capability` (job-level signatures only, for example `ats_account_wall` with `ats_accounts` and
  `ats_email_code` with `email_codes`): when that capability is active for the page's site in
  `private/consent.json` (site: the platform key of the host in `guard-hosts.json`, else `host:<host>`),
  there is no soft stop; the guard logs `capability_page`, still writes the stop file and runs `jh.py
  detect` (which records it and answers clear with a `flow`). A `capability` key on a tripping signature
  (or a value that is not a lower-case name) is a load error: the guard is unhealthy and fails closed.
- `handoff: "captcha"` (`ats_captcha`): the soft stop is unchanged, and the stop file carries the hand-off
  keys above so `jh.py detect` opens the owner's CAPTCHA task. The owner answers `/jh continue <code>`.

`SEVERITY` includes `ats_security` (SMS, phone and authenticator code pages, identity checks and social
sign-in pages on ATS sites; tripping), right after `gmail_unexpected_state` and before `ats_blocked`.

## Site consent

The agent's browser profile holds logins only for the sites the owner allowed (`./jobhunter init` or
`./jobhunter browser consent`: cookies copied from a Chrome profile the owner picks, or a login by hand in
the `jobhunter` profile). `private/consent.json` records that, and only the owner's terminal commands write
it (`scripts/jobhunter/install.py` `record_consent`; `./jobhunter browser forget [site|--all]` marks rows
revoked). The guard reads it, read only, on every browser call of a `jobhunter-*` agent (no cache, so a
revoke applies to the next call) and refuses with `G_NO_CONSENT` any call on a site with no active consent
row: a navigation is judged by its target, every other call by the tab's current page, and only `close`
stays allowed on such a page. This is a second fence behind `preflight` and `gate reserve`. `close` also
passes an open site or global breaker and runs on a never page (a revoke trips the site's breaker with
`consent_revoked`, and the agent must still be able to close that tab); only `state/PAUSED` stops it.
`close` is the only such exception because it returns no page data. `tabs` is not one: a tab list carries
every tab's title (a Gmail title shows the account address), so it is judged by the addressed tab's page
like a read, with the never list, the consent fence and the breakers.

A consent imports cookies by domain (`CONSENT_SITES` in `install.py`): Gmail's covers every `google.com`
host and YC's covers `ycombinator.com`. So `guard-hosts.json` never allows a `google.com` host other than
`mail.google.com` (a `never.url_patterns` entry) or `news.ycombinator.com` (`never.hosts`). Never patterns
are matched against the URL as given and as `new URL()` serializes it (host lower-cased and
percent-decoded, backslashes read as slashes), so a spelling of the host cannot slip past them.

A platform may also list `host_patterns`: regexes anchored with `^` and `$` that a whole host must match
(Oracle Cloud HCM: `^[a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com$`). `classifyUrl` tries them only after
every platform's suffix hosts. A platform with scope `ats` is also blocked by its own breaker
`ats:<key>` (for example `ats:workday`, tripped by the core), for reads and writes, as `ats` is.

Which hosts need consent comes from `guard-hosts.json`: `platforms.<key>.consent` names the site
(`linkedin`, `gmail`, and each job board under its key, such as `naukri` or `indeed`; a test keeps this
list equal to `CONSENT_SITES` in `install.py`); `false` marks public pages that need no login (the ATS
forms). An entry without the key needs consent under its own key unless its scope is `ats`. Pages on no
listed platform (a company's careers page) need none.

```json
{"version": 1, "updated_at": "2026-09-28T08:00:00Z",
 "sites": {"linkedin": {"site": "linkedin", "status": "granted", "method": "chrome_import",
                        "domains": ["linkedin.com", "www.linkedin.com"], "chrome_profile": "Profile 1",
                        "chrome_profile_name": "Personal", "granted_at": "2026-09-28T08:00:00Z",
                        "revoked_at": null, "declined_at": null, "by": "owner"}}}
```

A site has consent when its row, under its exact name, has `status` `granted` (as
`install.consent_active`), a `granted_at`, no `revoked_at`, and no `site` field naming another site;
`declined` and `revoked` rows give none. The guard is never more permissive than the core's reader: a
missing, unreadable or malformed file, a symlink, a file of another user, a file that group or others can
write, or one over 1 MB gives no site consent.

Capabilities (FEATURES-OTP-ACCOUNTS-CAPTCHA 1.2) live in the same file under `capabilities.<capability>.<site>`
(`email_codes`, `ats_accounts`; other names are ignored): a row is active when `status` is `granted`,
`granted_at` is not empty, `revoked_at` is empty, and its `site` and `capability` fields equal their keys
(`consent.ts` `activeCapabilities`, `capabilityActive`). Sites are a platform key or `host:<host>`, which
covers that host and its subdomains. A missing `capabilities` object grants nothing; a file that fails the
checks above grants nothing at all. The guard uses a capability only to skip a job-level soft stop, never
to allow an action, and does not compare an `ats_accounts` row's `email` with `owner.gmail_address` (it
does not read `private/config.json`; the core checks it before every code step).

## Secret fields and forbidden names

For every `jobhunter-*` agent, with or without a token, before the host, consent, breaker and token checks:

- `type`, `fill` (each entry of `fields` too) or `press` on a ref whose role is `textbox`, `searchbox` or
  `spinbutton` and whose name matches `secret_field_names` of `guard-hosts.json` (case-insensitive), or a
  `type`/`fill` aimed by a selector that names a password input or matches that pattern, is
  `G_SECRET_FIELD: secret fields are filled by code: jh.py account create, account signin or code submit`.
- A click on such a ref marks the tab (`secretFocus`); while it is marked, a `type` or `press` without a
  ref is refused the same way. The mark is cleared by the next allowed `snapshot` or `navigate` of that tab,
  or by a click on another known ref (a click on a ref the last snapshot did not show keeps it). Inside one
  `batch` the actions are checked in order.
- A click (also through `type` with `slowly`, `upload` with `ref`, or `download`) on a ref whose name
  matches any `forbidden_names` regex (social sign-in buttons, CAPTCHA widgets) is `G_TOOL_DENIED`.

`forbidden_names` and `secret_field_names` are required: a `guard-hosts.json` without them does not load
and the guard is unhealthy.

## Tab addresses

A call other than a navigation is judged by the page of the tab it addresses (the `targetId`, else the
tab this session used last). The guard learns a tab's address in `after_tool_call`: from the target of an
allowed `open` or `navigate`, from the `url` a result reports, and from every tab a `tabs` result lists
(without making it the tab used last). OpenClaw accepts several handles for one tab; the raw `targetId`
of a result is the tab's key, and the handles the result names (`tabId`, `label`, `suggestedTargetId`,
and the handle the call used) become aliases of it, so refs and addresses are found under any of them.

When the addressed tab's address is not known in this session, only `open`, `navigate`, `tabs`,
`close`, `start`, `status`, `profiles` and `doctor` run; everything else is refused with
`G_PAGE_UNKNOWN` (after the `state/PAUSED` and global breaker checks), because the never list, the
consent fence and the site breakers cannot judge a page without its host. `tabs` stays allowed there so
the agent can learn its tabs' pages, and an open global breaker still stops it (`close` alone passes it).
A tab list made from a known page is judged by that page only, not by the other tabs it lists.

## Other agents

For agents outside `jobhunter-*` the guard only refuses what touches the job hunter: naming
`guard.key`, setting `JH_*` variables, passing `--agent-p*`, running `jh.py` beyond `acl.public_readonly`
(and only for `publicReadonlyAgents`), and any tool call with a `profile` or `into` value (at any depth, so a
browser dashboard widget's `props.profile` too) that is `jobhunter` once trimmed and lower-cased
(`G_BROWSER_PROFILE`): driving the profile, importing Chrome cookies into it, or pointing a dashboard at
it. Also:

- file tools (`read`, `write`, `edit`, `apply_patch` and the native Read, Write, Edit, NotebookEdit, Glob,
  Grep, LS shapes): the path is resolved like OpenClaw does (a leading `@` dropped, `node://<node>/<p>` and
  `file://<p>` as the local `<p>`, `~` and `~/x` in the home folder, relative paths against
  `ctx.workspaceDir`, else `ctx.cwd`), then realpath; reads under `protectedRoots.read` and writes under
  `protectedRoots.write` are `G_PATH_DENIED`. Glob is checked by the folder before its first pattern
  character, Grep by its folder and by any protected folder inside it (it reads contents). The agent's own
  workspace stays usable when it lies inside a protected root (main's workspace inside the OpenClaw state
  folder), except `private/` and any `guard.key`;
- shell commands (OpenClaw's exec or native Bash): every word that looks like a path (contains `/`, starts
  with `~` or `.`, and the value after `=`) is expanded (`~`), resolved against the exec working folder and,
  when it holds `*`, `?` or `[`, expanded on disk (bounded); one under a protected read root is
  `G_PATH_DENIED`. A command with `openclaw` and one of `cron`, `agent(s)`, `session(s)`, `config`,
  `plugin(s)`, `approval(s)`, `exec-policy` that also names `jobhunter` is `G_OTHER_AGENT_DENIED`. This is best
  effort for shell text; structured tool paths are exact;
- `cron`, `gateway`, `subagents` and every `sessions_*` and `agents_*` tool whose JSON parameters name
  `jobhunter` (any case) is `G_OTHER_AGENT_DENIED`.

Everything else passes to OpenClaw's own policy.

## Driver manifest

`drivers/manifest.json` (generated by `tools/gen_drivers.py`) maps driver names to the sha256 of each
driver's file text with surrounding whitespace removed. `evaluate` (and `wait` with a function) is
allowed only when the sha256 of the `fn` argument, as given or with surrounding whitespace removed, is in
the manifest. Without a manifest every script is refused.

## Tests

```bash
cd openclaw/plugins/jobhunter-guard && node --test 'test/*.test.ts'
# or from the repo root:
node --test 'openclaw/plugins/jobhunter-guard/test/*.test.ts'
```

`test/policy.test.ts` is the decision table for rules R0 to R4 and R7 (including the never hosts,
`G_PAGE_UNKNOWN`, the reserved profile, native tool shapes, proofs typed by a model, path forms and protected
roots); `test/consent.test.ts` covers the consent file and `G_NO_CONSENT`; `test/grant.test.ts` checks the
proofs against `tests/fixtures/core/proof_vectors.json` and a Python stdlib implementation. `test/runtime.test.ts`
replays recorded transcripts (`test/fixtures/transcript-*.json`, including the Claude subscription route
shapes in `transcript-claude-cli-restricted.json` and `transcript-claude-cli-native.json`) against a
temporary install built from the real `schema.sql`.

### Transcript replay

A transcript is `{"name": "...", "steps": [...]}`. Steps:

| Step | Meaning |
|---|---|
| `{"call": {"agent", "session", "tool", "params", "ctx"}, "expect": "allow" or "pass" or "G_*"}` | a tool call through the trusted policy (`ctx` adds context keys such as `workspaceDir` or `cwd`); with `"mode": "gate"` it is decided with `claudeNativeTools: "gate"`; with `"expect_rewrite": {"rest", "workdir"?, "timeout"?}` the rewritten command must be `<py> -I <jh.py> --agent-proof <valid proof for this agent and session> <rest>` |
| `{"env": {"agent", "session", "runId"}, "expect_env": true or false}` | `resolve_exec_env`: a valid env proof for the agent (or no env at all) |
| `{"expect_log": {"kind": "native_tool", "count": 9}}` | the number of guard log lines of one kind |
| `{"result": {"agent", "session", "tool", "params", "result", "error"}, "expect_stop": true or false}` | a tool result through `after_tool_call` |
| `{"sql": "...", "args": [...]}` | a ledger change (what `jh.py` would have written) |
| `{"clock": "2026-09-27T05:02:30Z"}` | move the guard's clock |
| `{"expect_jh": ["detect", "--file", "*", "--source", "guard"]}` | a `jh.py` call the guard made (`*` matches any argument) |

`@JH@`, `@PY@`, `@REPO@`, `@WS@`, `@HOME@` and `@DRIVER@` are replaced with the temporary install's values.
A transcript may carry a top-level `"config"` (for example `protectedRoots`) merged into the plugin config.

## Open verification items (design 13.1)

| Item | Fallback built in |
|---|---|
| 2: trusted policy registration for a linked plugin | the backstop `before_tool_call` hook (priority 100000) decides every call the trusted tier did not see |
| 3: parameter rewrites (`command` with `-I` and the argv proof, `timeoutSeconds`, `workdir`) | `proofCarriers: ["env"]` (F-ENV): jh.py then requires the env proof only; the browser profile is never rewritten, only required |
| 4: `resolve_exec_env` under the claude-cli bridge | `proofCarriers: ["argv"]`: jh.py then requires the argv proof only |
| CLI route V7: `before_prompt_build` `toolsAllow` narrows stray runs | **failed on OpenClaw 2026.9.8** (T6: a stray `openclaw agent` run starts claude with user settings, `--allowedTools mcp__openclaw__*` and no `--tools ""`). Fallback in force: stray runs are unsupported and documented (`docs/ENFORCEMENT.md`, "Runs started by hand are not supported"); no project path starts a jobhunter agent without a tool list; Claude Code calls that need a permission are denied by the agents' exec policy (`allowlist`, `ask: off`) and any that reach the hooks by the guard; AskUserQuestion can wait until the run timeout (T6b "on" passed by model behavior only). Since then the pin also adds the `G_STRAY_RUN` system notice to such a run (trigger not `cron`) and logs `stray_run`: still model behavior, not a lock. Cause found in the gateway logs of all three live rounds: OpenClaw 2026.9.8 never registered the hook (`typed hook "before_prompt_build" blocked because non-bundled plugins must set plugins.entries.jobhunter-guard.hooks.allowConversationAccess=true`, 9.8 dist `loader-runtime-load` `registerTypedHook`, `hook-policy-decisions` `resolveConversationAccessAllowed`), so neither the pin nor the notice reached those runs; the 9.8 CLI path itself turns a hook `toolsAllow` into a restricted run (`prepare.runtime`: `cliToolAvailability {native: []}` for a selectable backend). With the grant set, V7 is open (T6 and T6b still to run); without it the fallback above is what holds, and the heartbeat shows it (`pin_hook_seen_at` stays `null`). The pin stays on: it narrows nothing that a cron run does not already narrow, and `pinToolSurface: false` only turns it off |
| CLI route V8: ctx of bridged calls carries `agentId`, `sessionKey`, `workspaceDir` | the agent from the session key; the base folder `WS_ROOT/<role>` |
| 9: `node:sqlite` read only on the WAL database | an unreadable database makes the guard unhealthy (fail closed) |
| 10: snapshot refs carry role and name | an unknown ref is always a commit action (stricter) |
