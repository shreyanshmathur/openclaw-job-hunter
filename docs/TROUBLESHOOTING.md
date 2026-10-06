# Troubleshooting

Start with `./jobhunter doctor`: it runs every check and says which one failed. `./jobhunter status` shows
breakers, queues and the last run of each lane, and `./jobhunter logs <lane>` shows recent runs.

`doctor` judges `openclaw doctor --lint --json` by the severity of its findings: only a finding of severity
error fails (`FAIL  openclaw doctor (read only)` with the finding below it); warnings are listed under the ok
line. `expected core/doctor/skill-workshop-tool-policy for jobhunter-...` is the project's own policy: the
jobhunter agents never get OpenClaw's `skill_workshop` tool, and the Skill Workshop is in mode `propose`.

The `openclaw` commands below use the short name. If your terminal does not find it, add
`export PATH="$HOME/.openclaw/bin:$PATH"` to `~/.zprofile` (or type `~/.openclaw/bin/openclaw`), and add
`--profile <name>` when you installed with `./install.sh --profile <name>`.

## Installer

When the installer stops it prints the `ERROR:` line, then `The install stopped at step <n> (<name>)`. Fix what
the error says and run `./install.sh` again: every step is safe to repeat and finished steps print `SKIP`. The
installer remembers the model route of the first install (Claude subscription or `--api-key`), so a re-run needs
no options; `./install.sh --api-key` or `./install.sh --claude-login` switch it.

| Message | What to do |
|---|---|
| `NOTE: set your owner PIN at a terminal`, `SKIP site consent`, `NOTE: no terminal, so models auth login is skipped` | The installer ran without a terminal (from a script, another program or an AI assistant) or with `--yes`, so it asked nothing. Open the Terminal app, `cd ~/openclaw-job-hunter`, then run `./jobhunter pin set` and `./jobhunter init` yourself. The Claude subscription route does not need the skipped `models auth login`: it uses your `claude` login. |
| `the API key is asked at a terminal` | Run `./install.sh --api-key` in the Terminal app and paste the key when asked (input hidden). |
| `OpenClaw is already set up, so the key is not asked again` (API key route) | The key reaches OpenClaw only through its first onboarding. If the key changed, or this install used your Claude subscription before, save it in OpenClaw with `openclaw models auth paste-api-key --provider anthropic`, then `./install.sh --api-key --smoke` checks that the agents can use it. |
| `the repo path may only contain letters, digits ...` or `the repo is inside ~/Documents` | Move the folder: `mv <path> ~/openclaw-job-hunter`, then run `./install.sh` again from there. |
| `Claude Code is not signed in, or its login expired` | `claude auth login` (opens your browser), check with `claude auth status --text`, then `./install.sh` again. Or use `./install.sh --api-key`. |
| `Claude Code is not installed` | `curl -fsSL https://claude.ai/install.sh \| bash`, then `exec zsh -l` (or a new Terminal window), `claude auth login`, and `./install.sh` again. |
| `OpenClaw 2026.9.5 or newer is needed (this one is ...)` | `openclaw update`, then `./install.sh` again. |
| `the agents config patch did not validate` | Nothing was changed, and agents this run added were removed again. Run `openclaw config validate` to see which key OpenClaw rejects and report it (include the OpenClaw version). The installer writes `tools.exec.mode` only and deletes an older install's `security` and `ask` in the same patch, because OpenClaw 2026.9.5 and later refuse `mode` next to them. |
| `the jobhunter agents must not get OpenClaw's weekly skill reviews` | OpenClaw's Skill Workshop is in mode `auto` (its default). Run `./install.sh` at a terminal and answer yes, or `./install.sh --skill-workshop-propose`, or set it yourself: `openclaw config set skills.workshop.autonomous.mode propose`. The setting is global for that OpenClaw profile. |
| `enabled automation ... runs jobhunter-<role> outside the install` (also red in `doctor`) | A cron job that this install did not create runs a jobhunter agent. `skill-collection-review:jobhunter-*` jobs come from OpenClaw's Skill Workshop: set `skills.workshop.autonomous.mode` to `propose`. Disable or remove any other such job (`openclaw cron disable <id>`), then run `./install.sh` again. |
| `NOTE: removed jobhunter-<role> again: the install stopped before it was restricted` | The install stopped between adding the agents and reading back their exec policy, so the new agents were removed (or, when that failed, every jobhunter agent was set to exec deny with every tool denied). Fix the reported error and run `./install.sh` again. |
| `NOTE: no terminal, so models auth login is skipped` | Normal for `./install.sh --yes` from a script: the Claude CLI route uses your `claude` login. At a terminal the installer runs `openclaw models auth login --provider anthropic --method cli --agent <id>`. |
| `this clone is installed for the OpenClaw binary ...` | One clone is one install: the clone records the `openclaw` it was installed with (`--openclaw-bin`, else the one on your PATH). Clone the repo again for a test install of another build. |
| `openclaw approvals get failed` | The installer needs to merge its exec approvals with yours. Check `openclaw approvals get --json` works, then re-run. |
| `unrecognised openclaw approvals get --json output` | Your OpenClaw prints the approvals in a newer shape. Add for each `jobhunter-*` agent the entry from `openclaw/exec-approvals.json5.tmpl` by hand (`openclaw approvals get`, edit, `openclaw approvals set --file`). |
| `the guard did not report in` (also `... with identity proof version 2`) | `openclaw plugins list` must show `jobhunter-guard` enabled. Check the Gateway log (`openclaw logs`) for `jobhunter-guard`, restart the Gateway (`openclaw gateway restart`) and run `./install.sh` again. On OpenClaw before 2026.9.7 a plugin change needs that restart; the installer does it unless you used `--no-daemon`. Agents refuse to work without a fresh guard heartbeat. |
| `the jobhunter agents must run with exec allowlist and ask off, but OpenClaw reports otherwise` | The installer wrote explicit values for every jobhunter agent and read back the effective policy (`openclaw sandbox explain --agent <id> --json`, `openclaw exec-policy show --json`). The message names the agent and the value that won. A global `tools.exec` value or another plugin can override ours on some OpenClaw versions: remove the override, then run `./install.sh` again. Keys the installer does not write (for example an old `reviewer`) are removed by the installer itself. `approvals: agents["*"] adds ... allowlist entries` means exec approvals you keep for every agent, which OpenClaw adds to the jobhunter agents too: move them to the agents that need them (`openclaw approvals get`, edit, `openclaw approvals set --file`). `autoAllowSkills is true` or `allowlist entries the installer does not write` on a jobhunter entry: run `./install.sh` again, which writes the entry back. |
| `this Claude Code is too old for restricted agent runs` | `claude update`, then `./install.sh` again. OpenClaw starts every agent run with `--tools`, `--strict-mcp-config` and `--setting-sources`; an older claude cannot switch its own tools off. |
| `Claude Code <version> is too old for <model> of jobhunter-<role>` | That agent's model needs a newer Claude Code, which would otherwise refuse the model only at the agent's first run. `claude update`, then `./install.sh` again. |
| `openclaw plugins install --link failed` with `Install cancelled; rerun with --force` | OpenClaw asks before it installs a plugin that is not from ClawHub, and cancels when nobody can answer. The guard plugin comes with this clone: run `./install.sh` at a terminal and answer yes, or run `./install.sh --yes` (both confirm with `--force`; the step runs only while the plugin is not installed). |
| `openclaw plugins list failed` | The installer must know whether the guard plugin is installed before it confirms a link. Check `openclaw plugins list --json`, then run `./install.sh` again. |
| `openclaw plugins enable jobhunter-guard failed` with `must have required property 'repo'` | An older installer enabled the guard before writing its config, which OpenClaw 2026.9.8 refuses. Run `./install.sh` again: it writes the guard config first, then enables the plugin. |
| `the Gateway is not running. With --no-daemon ...` | Start your Gateway yourself (for example `openclaw gateway run` in another terminal), then run the installer again. |
| `ERROR: dispatch paused; re-run ./install.sh` | A running dispatcher is paused for the whole install and started again only when every step passed. Fix the reported error and run `./install.sh` again; the dispatcher starts at the end. |
| `this clone is installed for OpenClaw profile ...` | One clone is one install. For a second profile, clone the repo again into another folder. |
| `openclaw onboard failed` with `Missing --anthropic-api-key` (API key route) | The key did not reach onboarding. Run `./install.sh --api-key` at a terminal and paste the key when asked (input hidden). The installer retries once with the key as an argument when an OpenClaw version needs that, and says so. |
| `the jobhunter-qc test turn failed: the reply was not the numbers 1 to 700` | An older version of this project (October 2026, OpenClaw 2026.9.8, Claude subscription route): its QC test sent a message that was not a review packet, which the reviewer rightly refused, so step 14 failed every time. The test now sends a review packet with made-up data. Run `./jobhunter update`, then `./install.sh` again; start the agent with `./jobhunter resume` only after an install completed. |
| `agent identity check failed` or `the jobhunter-qc test turn failed` (any other reason) | See [Claude subscription route](#claude-subscription-route) below. A model login problem shows up here too. Claude route: `claude auth status --text`, then `openclaw models auth login --provider anthropic --method cli --agent <id>`. API route: check the key with `openclaw models list --provider anthropic`. |

## First run wizard

| Message | What to do |
|---|---|
| `profile inference recorded nothing` | The evaluator's run ended without `private/profile_inference.json` and `private/resume/base.json`. Run `./jobhunter init` again: finished steps print SKIP and this one repeats. If it keeps happening, run `./jobhunter doctor --probe` and read [Claude subscription route](#claude-subscription-route). |
| `the salary research turn recorded nothing` | Not fatal: the salary band says "model estimate, not verified". `./jobhunter init` tries again. |
| `there is no base resume yet; run the onboarding inference first` | Same cause as the first line: re-run `./jobhunter init`. |
| `set owner.gmail_address in private/config.json` | Re-run `./jobhunter init` (step 3 asks for your name, Gmail address and chat number) or edit `owner` in `private/config.json`. |
| `unknown command` from `./jobhunter` | `./jobhunter help` lists the commands. Any command a message names, such as `./jobhunter qc golden` or `./jobhunter eval requeue --since-profile-change`, runs as written and asks for the PIN when it needs the owner. |
| The pre-commit hook still fails after you fixed a file | The hook checks what is staged. `git add` the fixed file, then commit again. |

## Nothing happens

- Did you run `./jobhunter resume`? Automations are created disabled.
- `state/PAUSED` exists: you paused. `./jobhunter resume`.
- A breaker is open: `./jobhunter status` names it and says when it may be reset.
- Outside active hours: browser lanes only run inside `active_hours.browser_window` on the listed days.
- The Mac was asleep: turn on the stay-awake helper (`./install.sh --stay-awake`).
- `profile status` is not confirmed: finish `./jobhunter init`. The evaluator, applier and outreach lanes wait
  for a confirmed profile.
- No display (Linux, WSL): browser lanes are off on that host, see [SETUP-linux-wsl.md](SETUP-linux-wsl.md).

## Sites, logins and consent

| Symptom | What to do |
|---|---|
| `you have not allowed the agent to use your ... login` (`E_CONSENT_MISSING`) | Nothing uses a site without your consent. Allow it with `./jobhunter browser consent <site>` (PIN), or leave it off. `./jobhunter browser sites` lists what is allowed. |
| An alert says a session expired, or a site asks to verify it is you | The site's breaker is open and the agent does not retry. Open the site in your normal Chrome and sign in (finish any verification yourself there), then copy the login again with `./jobhunter browser import` (macOS), or log in inside the agent's window with `./jobhunter browser login <site>`. Check with `./jobhunter browser check <site>`, then `./jobhunter breaker reset <scope>` after its cooldown. |
| `STOP  ... shows a CAPTCHA or a verification prompt` during the login check | Finish the prompt yourself in the jobhunter window (the agent never does), then `./jobhunter browser check <site>`. If it keeps appearing, the site does not accept the copied session: log in by hand with `./jobhunter browser login <site>`. |
| `STOP  the Gmail account in the agent profile is ..., but your sender address ...` | The copied Gmail login is another account. Log in to the right account (`./jobhunter browser login gmail`), change your sender address (`./jobhunter init`), or take Gmail back (`./jobhunter browser forget gmail`). |
| `LOGIN ... is not logged in in the agent profile` | The copy found no valid session (you may be logged out of that site in Chrome, or Google refused the copied cookies). Log in by hand: `./jobhunter browser login <site>`. |
| `the copy failed` after picking a Chrome profile | macOS may have denied access to Chrome's cookie store (the Keychain prompt was cancelled). Run `./jobhunter browser import` and allow the prompt, or log in by hand. Copying works on macOS only. |
| `Google Chrome's Local State file was not found` | Chrome is not installed or was never opened on this user account. Open Chrome once, or choose to log in by hand (`m`). |
| After `./jobhunter browser forget <site>` you are logged out of other sites too | Expected: OpenClaw clears cookies for the whole jobhunter profile. The sites you still allow are copied again at once; sites you logged in to by hand need `./jobhunter browser login <site>` again. |

## Email codes, site accounts and CAPTCHAs

| Symptom | What to do |
|---|---|
| A verification code or sign-in link never arrives | Look in your mailbox for the site's message and check who sent it: the agent only uses mail from the site's own senders, within 10 minutes. After 3 failed tries at one site in a day its breaker opens (`ats:workday`, reason `otp_failures`); the job comes to you. After the cooldown: `./jobhunter breaker reset ats:workday`. |
| The site says an account already exists for your address | The agent does not guess or reset passwords. The job went to you. Sign in once yourself in the agent's browser window, or reset the password on the site, and apply there. |
| An alert says the Keychain is locked | The program could not store or read the site password. Unlock your Mac's login Keychain (log in to the desktop), then run the job again. |
| `doctor` says the browser control port does not answer | The program types codes and passwords through the agent browser's local port. Run `./jobhunter doctor` and follow what it says; running `./install.sh` again records the port anew. |
| `/jh continue` says the CAPTCHA is still there | The check found the CAPTCHA, another stop, or a different site in that tab. Finish the CAPTCHA in the agent's browser window (do not close the tab), then send `/jh continue <code>` again. |
| A job was skipped with "CAPTCHA not solved in time" | Nobody continued it within 2 hours (`captcha.timeout_minutes`). The tab was closed. Apply yourself if you still want the job. |
| How do I remove an account the agent created? | `./jobhunter accounts forget <host>` (PIN) deletes the stored password. The account on the site remains: sign in there and delete it. To stop new ones: `./jobhunter browser forget <site>`. |
| The Sheet shows "Unavailable: Gmail not allowed" | Email codes need Gmail: allow it with `./jobhunter browser consent gmail`, or on the app password route run `./jobhunter mail connect`. |

## Email

| Symptom | What to do |
|---|---|
| `doctor` says `email is off: gmail.route is web_ui and Gmail is not allowed` | The default email route uses Gmail in the agent's browser. Allow Gmail: `./jobhunter browser consent gmail`. No password is needed. |
| `mail test` fails with an authentication error (app password route only) | Make a new app password (2-Step Verification must be on) and run `./jobhunter mail connect`. Google Workspace admins can disable app passwords: switch back to the default browser route (`gmail.route: "web_ui"`), see [EMAIL-SETUP.md](EMAIL-SETUP.md). |
| Email stopped with a `gmail` breaker | A bounce, complaint or Google policy error. Read the alert, fix the cause (bad addresses, too many sends), wait for the cooldown, then `./jobhunter breaker reset gmail`. |
| An action is `unknown` | The send result was not certain. It stays blocked on purpose. The mailer checks the Sent folder twice over a day; for forms, confirm with `./jobhunter reconcile not-sent <token>` only if you are sure nothing went out. |

## Email finder (optional)

| Symptom | What to do |
|---|---|
| An `enrich:<provider>` breaker is open | `./jobhunter enrich budget` shows why. A rejected key (`auth_failed`): make a new key in your own account and run `./jobhunter enrich connect <provider>`, which closes that breaker. Used-up credits close by themselves when the provider's month renews. Other reasons (errors, rate limits, bounces from that provider): wait for the cooldown, then `./jobhunter breaker reset enrich:<provider>`. The whole finder has the scope `enrich`. |
| macOS asks whether `security` may use a Keychain item | The email finder stores and reads its keys as login Keychain items named `openclaw-job-hunter.enrich.<provider>...`. Choose Always Allow for the Python that runs the agent, or use `./jobhunter enrich connect <provider> --store file` (the key goes to `private/enrich_keys.json`, mode 600). |
| `enrich connect needs a terminal` | You type the key yourself with the echo off; run it in Terminal, not from a script or chat. |
| Nothing is looked up | The finder is off by default: set `"enrich": {"enabled": true}` in `private/config.json` and connect at least one key. See [EMAIL-FINDER.md](EMAIL-FINDER.md). |

## Google Sheet

`./jobhunter sheet sync` prints the reason. The most common: "Who has access" is not "Anyone" (the answer is a
Google login page), a mistyped secret (`bad_secret`), or an old `Code.gs` (`schema_mismatch`: paste the new one
and deploy a new version of the same deployment). See [GOOGLE-SHEETS.md](GOOGLE-SHEETS.md).

## Chat

- Messages do not arrive: `openclaw channels status --probe`. Undelivered alerts are listed by
  `./jobhunter status` and on the Sheet dashboard. Check `owner.notify` in `private/config.json` (the wizard's
  step 3 sets it); after changing it by hand, run `./install.sh` again so the guard and the failure alerts use the
  new number.
- `/jh` is not recognised: approve in the Sheet or with `./jobhunter approve <code>` instead, and check that
  the chat is the owner number set in `private/config.json`.

## LinkedIn

A restriction or verification page stops all LinkedIn work at once. Resolve it yourself in the browser (never
let a tool do it), wait for the cooldown, and consider leaving LinkedIn off: `./jobhunter linkedin disable`.

## Claude subscription route

On the Claude subscription route every jobhunter agent run is a restricted OpenClaw run: a cron job with an
explicit tool list, so Claude Code's own tools (Bash, Read, Write, Edit, AskUserQuestion and the rest) are off
and the agent only has OpenClaw's exec, read, write (and browser) tools. Those never wait for a person: the exec
policy of every jobhunter agent is allowlist with ask off, so a command outside the allowlist is refused at once.
Every `jh.py` call of an agent carries two proofs minted by the guard (an `--agent-proof` option and an
environment proof), and `jh.py` runs as `python -I`. The project never starts an agent with `openclaw agent`.

| Symptom | What to do |
|---|---|
| `FAIL  Claude login` in `doctor`, or agent runs that worked before now fail | The Claude Code login expired or Claude Code was removed. `claude auth login`, `claude auth status --text`, then `./jobhunter doctor --probe`. After `claude update`, run `openclaw gateway restart` and `./install.sh`. |
| Salary research or profile inference "recorded nothing" | Run `./jobhunter doctor --probe`. If the probes pass, the model ended without recording: run `./jobhunter init` again. If they fail, follow the lines below. |
| `E_CALLER_NOT_ALLOWED` and the audit actor is `system` | The call reached `jh.py` without the guard's proofs. An agent of this project cannot do that; a person's or another agent's shell can. Run `./install.sh` again (it re-checks the exec policy and the guard) and `./jobhunter doctor`. |
| `G_TOOL_DENIED: Claude Code tools are off for jobhunter agents; this run is not restricted` | Someone started a jobhunter agent outside its automations (for example with `openclaw agent`), or the automation lost its tool list. Run `./install.sh` again: it compares every field of every automation and repairs it. Do not start jobhunter agents by hand. |
| `E_CRON_DRIFT` (from `./jobhunter run`, the dispatcher or the wizard) | An automation no longer matches what the installer declared (its tools, message, model or agent changed). Nothing ran. `./install.sh` repairs it; the message names the fields, never their values. |
| `G_PATH_DENIED: path form not allowed` | Agents must use absolute paths inside their own workspace; `~`, `@`, `..`, `$` and URLs are refused. If it comes from a skill you changed, fix the path in the skill. |
| `G_BROWSER_PROFILE` | Browser calls must pass `profile: "jobhunter"`. The shipped skills do; check a skill you edited. |
| `agent identity check failed` during install or `doctor --probe` | The probe of that agent did not run `jh.py whoami` as itself with both proofs. Check `openclaw cron runs --id <probe job id>` and the guard log (`./jobhunter logs`). If the guard log shows `native_tool`, the run was not restricted on this OpenClaw version: use the API key route (`./install.sh --api-key`) or another OpenClaw version. |
| `old guard; run ./install.sh` (`E_AUTH_FAILED`) | The guard still mints the version 1 proof. `./install.sh` reloads it; on OpenClaw before 2026.9.7 it also restarts the Gateway. |
| `the jobhunter-qc test turn failed` | The QC test sends the reviewer one review packet with made-up data (a cold email that breaks most rules) and checks that the answer is a valid verdict for that packet, longer than 2000 characters. The reviewer runs as a one-shot cron job with no tools. When its reply cannot be read from the run record (OpenClaw 2026.9.8 cuts it at 2000 characters), the installer switches to a verdict file (`cli_route.qc_reply: "file"` in `private/home.json`) and tries once more. If that fails too, check `openclaw cron runs` and the model login. With `not a valid verdict for the test packet` the reviewer answered, but not in the verdict format (the message names the field): run `./install.sh --smoke` once more, and if it repeats, `./jobhunter qc golden` shows how the reviewer answers real packets. With `the reply was not the numbers 1 to 700` you run an older version: see [Installer](#installer). |
| A red line `agent <id> has an unconfined shell and can impersonate jobhunter agents` | That OpenClaw agent (often your own `main`) can run any command as you, so it can read files and do what you can. Set its exec policy to allowlist with ask off if it should not. Agent identity cannot be forged by an agent whose shell is confined. An agent with an unconfined shell runs as you and is trusted like you. |
| You installed with `--cli-tools native --i-accept-reduced-protection` | Reduced protection, shown in red by `doctor`: Claude Code's own tools bypass the exec allowlist and `workspaceOnly` (only the guard checks them), AskUserQuestion can wait until the run times out, and your `~/.claude` settings, hooks and MCP servers load into agent runs. Run `./install.sh` without `--cli-tools` to return to restricted runs, or use the API key route. |
| `--cli-tools native works only with --identity-carrier argv` or `... works only with the argv identity carrier` (`E_CONFIG_INVALID` from `private/home.json`) | In native mode an agent runs `jh.py` from Claude Code's own Bash, which never gets the guard's env proof, so every call with the env carrier was refused `missing env proof`. Native mode now uses the argv carrier alone: run `./install.sh --cli-tools native --i-accept-reduced-protection` without `--identity-carrier` (it picks argv and says so), or with `--identity-carrier argv`. |
| `WARN  the guard's tool pin hook has never run` (doctor) | Information only. OpenClaw 2026.9.8 runs the guard's `before_prompt_build` hook only when `plugins.entries.jobhunter-guard.hooks.allowConversationAccess` is true, and the installer does not grant that. The automations are restricted without the pin; it only adds a notice to a run started by hand, which is not supported. See [ENFORCEMENT.md](ENFORCEMENT.md#runs-started-by-hand-are-not-supported). |
| A call made from a Claude Code session is refused with `no verified agent identity` | `jh.py` treats any process that Claude Code started (`CLAUDECODE` or `CLAUDE_CODE_ENTRYPOINT` set) as an unproven agent, wherever it runs. Use `./jobhunter <command>` (it drops those markers for you) or run the command in a normal terminal. |

`./jobhunter doctor --probe` runs the four identity probes and one QC turn (it uses the model). The installer runs
them once per OpenClaw, claude, guard and `jh.py` version; `./install.sh --smoke` runs them again.

## Things to verify on a new OpenClaw version

These behaviours were designed against OpenClaw 2026.9.5 and 2026.9.8 and are worth checking on a test clone
installed with `./install.sh --profile jhtest --no-daemon` after an OpenClaw update:

1. `openclaw cron run <id> --wait` runs a disabled agent job and waits for it.
2. A cron job with `--tools` gives a restricted run under the Claude CLI runtime (no Claude Code tools,
   AskUserQuestion cancelled at once), and the guard sees every bridged exec, read, write and browser call.
3. The per-agent keys `tools.fs.workspaceOnly`, `tools.exec.mode` (alone, without `security` or `ask`),
   `tools.elevated.enabled` and `memory.search.enabled` are accepted, and `openclaw sandbox explain --agent <id>
   --json` and `openclaw exec-policy show --json` show security allowlist with ask off even with a global
   `tools.exec.ask`.
4. `/jh` works on WhatsApp and only for the owner.
5. The QC one-shot run returns its reply in `openclaw cron run --wait --json` or `openclaw cron runs --json`
   (on 2026.9.8 it does not: the summary is cut at 2000 characters, and the verdict file is used instead).
6. `browser.profiles.jobhunter.headless` is honoured on Linux.
7. With `skills.workshop.autonomous.mode` set to `propose`, `openclaw cron list --all --json` shows the
   `skill-collection-review:jobhunter-*` jobs disabled (`./jobhunter doctor` checks this).

If one fails, the design has a fallback for each (for example `dispatch.mode: "cron_schedule"` in
`private/config.json` for the first), and `./jobhunter doctor` reports the state.

## Starting over

`./uninstall.sh` then `./install.sh` keeps your data. `./uninstall.sh --purge` deletes it (typed confirmations).
