# Troubleshooting

Start with `./jobhunter doctor`: it runs every check and says which one failed. `./jobhunter status` shows
breakers, queues and the last run of each lane, and `./jobhunter logs <lane>` shows recent runs.

The `openclaw` commands below use the short name. If your terminal does not find it, add
`export PATH="$HOME/.openclaw/bin:$PATH"` to `~/.zprofile` (or type `~/.openclaw/bin/openclaw`), and add
`--profile <name>` when you installed with `./install.sh --profile <name>`.

## Installer

| Message | What to do |
|---|---|
| `the repo path may only contain letters, digits ...` or `the repo is inside ~/Documents` | Move the folder: `mv <path> ~/openclaw-job-hunter`, then run `./install.sh` again from there. |
| `Claude Code is not signed in` | `claude auth login`, then `./install.sh` again. Or use `./install.sh --api-key`. |
| `OpenClaw 2026.9.5 or newer is needed` | `openclaw update`, then `./install.sh` again. |
| `the agents config patch did not validate` | Nothing was changed. Run `openclaw config validate` to see which key OpenClaw rejects and report it (include the OpenClaw version). |
| `openclaw approvals get failed` | The installer needs to merge its exec approvals with yours. Check `openclaw approvals get --json` works, then re-run. |
| `unrecognised openclaw approvals get --json output` | Your OpenClaw prints the approvals in a newer shape. Add for each `jobhunter-*` agent the entry from `openclaw/exec-approvals.json5.tmpl` by hand (`openclaw approvals get`, edit, `openclaw approvals set --file`). |
| `the guard did not report in` | `openclaw plugins list` must show `jobhunter-guard` enabled. Check the Gateway log (`openclaw logs`) for `jobhunter-guard`, restart the Gateway (`openclaw gateway restart`) and run `./install.sh` again. Agents refuse to work without a fresh guard heartbeat. |
| `this clone is installed for OpenClaw profile ...` | One clone is one install. For a second profile, clone the repo again into another folder. |
| `openclaw onboard failed` with `Missing --anthropic-api-key` (API key route) | The key did not reach onboarding. Run `./install.sh --api-key` at a terminal and paste the key when asked (input hidden). The installer retries once with the key as an argument when an OpenClaw version needs that, and says so. |
| `the test turn for jobhunter-... failed` | The model login does not work for that agent. Claude route: `claude auth status --text`, then `openclaw models auth login --provider anthropic --method cli --agent <id>`. API route: check the key with `openclaw models list --provider anthropic`. |

## First run wizard

| Message | What to do |
|---|---|
| `profile inference recorded nothing` | The evaluator's turn ended without `private/profile_inference.json` and `private/resume/base.json`. Run `./jobhunter init` again: finished steps print SKIP and this one repeats. Check `./jobhunter logs evaluator` and `openclaw logs` if it keeps happening. |
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
  step 3 sets it); after changing it by hand, run `./install.sh` again (with the options you installed with, such as
  `--api-key`) so the guard and the failure alerts use the new number.
- `/jh` is not recognised: approve in the Sheet or with `./jobhunter approve <code>` instead, and check that
  the chat is the owner number set in `private/config.json`.

## LinkedIn

A restriction or verification page stops all LinkedIn work at once. Resolve it yourself in the browser (never
let a tool do it), wait for the cooldown, and consider leaving LinkedIn off: `./jobhunter linkedin disable`.

## Things to verify on a new OpenClaw version

These behaviours were designed against OpenClaw 2026.9.5 and are worth checking on a test clone installed with
`./install.sh --profile jhtest` after an OpenClaw update:

1. `openclaw cron run <id>` starts a disabled agent job and returns without waiting.
2. The guard's trusted tool policy sees every tool call under the Claude CLI runtime and one-shot agent runs.
3. The per-agent keys `tools.fs.workspaceOnly`, `tools.exec.mode` and `memory.search.enabled` are accepted.
4. `/jh` works on WhatsApp and only for the owner.
5. `openclaw sandbox explain --agent jobhunter-qc` shows no tools.
6. `browser.profiles.jobhunter.headless` is honoured on Linux.

If one fails, the design has a fallback for each (for example `dispatch.mode: "cron_schedule"` in
`private/config.json` for the first), and `./jobhunter doctor` reports the state.

## Starting over

`./uninstall.sh` then `./install.sh` keeps your data. `./uninstall.sh --purge` deletes it (typed confirmations).
