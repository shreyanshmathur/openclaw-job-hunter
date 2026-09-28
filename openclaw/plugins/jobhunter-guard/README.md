# jobhunter-guard (OpenClaw plugin)

The guard fences the five `jobhunter-*` agents inside the OpenClaw Gateway. It decides every tool call of
those agents before it runs, watches every browser result for stop pages, gives each exec call the
agent's identity, writes a heartbeat, and handles the owner's `/jh` chat command. It never writes the
database: every change goes through `scripts/jh.py`, which the plugin runs with `execFile` (no shell).

The rules in plain words are in [docs/ENFORCEMENT.md](../../../docs/ENFORCEMENT.md). The design reference
is section 1.4 of the implementation design.

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

The plugin has no dependencies. It needs Node 24 (bundled with OpenClaw) for `node:sqlite` and for
running TypeScript without a build step.

## What it registers

| Registration | Purpose |
|---|---|
| `api.registerTrustedToolPolicy({id: "jobhunter-guard", evaluate})` | rules R0 to R4 and R7 before every tool call (declared in `contracts.trustedToolPolicies`; the plugin must be explicitly enabled) |
| `api.on("before_tool_call", ..., {priority: 100000})` | backstop: the same policy for any call the trusted tier did not decide (the host only reports a refused trusted registration, it does not throw). A call the trusted tier decided is skipped, so nothing is decided twice. |
| `api.on("after_tool_call")` | R5: stop-signature scan of browser results, snapshot ref cache, tab URLs |
| `api.on("resolve_exec_env")` | R8: `JH_AGENT_ID`, `JH_SESSION_KEY`, `JH_RUN_ID`, `JH_AGENT_PROOF` for `jobhunter-*` agents |
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
| `state/guard/heartbeat.json` | `{"install_id", "version", "loaded_at", "beat_at", "acl_sha256", "hosts_sha256"}`, written only while the guard is healthy |
| `state/guard/<token>.jsonl` | one line per allowed fill or commit: `{"ts", "token", "agent", "class", "action", "host", "ref", "role", "name"}` (`class` is `fill` or `commit`) |
| `state/guard/stop-<ts>-<rand>.json` | the detect file handed to `jh.py detect --source guard`: `{"platform", "url", "title", "http_status", "text"}` |
| `logs/guard-YYYY-MM.jsonl` | one line per decision for `jobhunter-*` agents and per block for other agents: `ts, kind, agent, session, tool, decision, code, class, host, command`; one `kind: "stop"` line per stop match: `code, reason, file, where, trip, host`. Page text and command arguments are never logged. |

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
`guard.key`, setting `JH_*` variables, running `jh.py` beyond `acl.public_readonly` (and only for
`publicReadonlyAgents`), and any tool call with a `profile` or `into` value (at any depth, so a browser
dashboard widget's `props.profile` too) that is `jobhunter` once trimmed and lower-cased
(`G_BROWSER_PROFILE`): driving the profile, importing Chrome cookies into it, or pointing a dashboard at
it. Everything else passes to OpenClaw's own policy.

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
`G_PAGE_UNKNOWN` and the reserved profile); `test/consent.test.ts` covers the consent file and
`G_NO_CONSENT`. `test/runtime.test.ts` replays
recorded transcripts (`test/fixtures/transcript-*.json`) against a temporary install built from the real
`schema.sql`.

### Transcript replay

A transcript is `{"name": "...", "steps": [...]}`. Steps:

| Step | Meaning |
|---|---|
| `{"call": {"agent", "session", "tool", "params"}, "expect": "allow" or "pass" or "G_*"}` | a tool call through the trusted policy |
| `{"result": {"agent", "session", "tool", "params", "result", "error"}, "expect_stop": true or false}` | a tool result through `after_tool_call` |
| `{"sql": "...", "args": [...]}` | a ledger change (what `jh.py` would have written) |
| `{"clock": "2026-09-27T05:02:30Z"}` | move the guard's clock |
| `{"expect_jh": ["detect", "--file", "*", "--source", "guard"]}` | a `jh.py` call the guard made (`*` matches any argument) |

`@JH@`, `@REPO@`, `@WS@` and `@DRIVER@` are replaced with the temporary install's values.

## Open verification items (design 13.1)

| Item | Fallback built in |
|---|---|
| 2: trusted policy registration for a linked plugin | the backstop `before_tool_call` hook (priority 100000) decides every call the trusted tier did not see |
| 3: parameter rewrites (`timeoutSeconds`, `workdir`, `profile`) | none needed for safety: the command itself is validated, and a rewrite only tightens |
| 4: `resolve_exec_env` under the claude-cli bridge | `JH_AGENT_PROOF` is also set so `jh.py` can verify the agent identity cryptographically |
| 9: `node:sqlite` read only on the WAL database | an unreadable database makes the guard unhealthy (fail closed) |
| 10: snapshot refs carry role and name | an unknown ref is always a commit action (stricter) |
