# Developing openclaw-job-hunter

This page is for contributors. It explains how the code is split, how to run the tests, what the repo rules
are, and how a release is checked. The full design lives with the maintainers; the parts you need to work on
one area are summarised here.

## Ground rules

- Python 3.9 standard library only for everything under `scripts/` and `tools/`. Every module starts with
  `from __future__ import annotations`.
- The guard plugin is TypeScript run by the Node 24 that ships with OpenClaw, with no runtime dependencies.
- Every committed text file is plain ASCII. No en dash, no em dash, no curly quotes, no ellipsis character,
  no no-break space. In prompts, templates and examples, do not use a spaced hyphen or a double hyphen as a
  dash either: write two sentences, or use a comma or a colon.
- No personal data, ever: no real names, email addresses, phone numbers, companies you contacted, sheet ids,
  web app URLs, resumes or chat logs. Use fictional placeholders such as Alex Rivera, Kestrel Commerce and
  addresses at example.com.
- Tests never touch the network, never send anything and only write inside temporary folders.
- Nothing in the code or the tests may run a real `openclaw` write command. The install tests use
  `tests/fixtures/install/fake-openclaw`.

## Running the checks

From the repo root:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -t .      # all Python tests
python3 -m unittest discover -s tests -p 'test_install*.py' -t . # one area
(cd openclaw/plugins/jobhunter-guard && node --test 'test/*.test.ts')
python3 tools/textcheck.py        # ASCII, no home paths, no spaced dashes in templates
python3 tools/leakcheck.py        # personal data and secrets
python3 tools/check_gitignore.py  # runtime and personal files are not tracked
python3 tools/sync_skills.py --check
python3 tools/gen_drivers.py --check  # drivers/manifest.json matches the driver sources
for f in install.sh uninstall.sh jobhunter macos/stay-awake.sh tools/install_hooks.sh; do /bin/bash -n "$f"; done
```

`tools/install_hooks.sh` installs a pre-commit hook that runs textcheck, leakcheck, check_gitignore and the
gen_drivers check before every commit. The hook runs textcheck and leakcheck with `--staged`: they read the
files from the git index, which is exactly what the commit records, not from the working tree. When the hook
finds something, fix the file and `git add` it again before you re-run `git commit`; an edit that is not staged
does not change what gets committed. Run the same check by hand with `python3 tools/leakcheck.py --staged`.
Re-run `tools/install_hooks.sh` after an update to get the current hook.

### Scanning for your own data

`tools/leakcheck.py` builds a private denylist at run time from `private/config.json` (your owner fields),
`private/profile.json` and `private/answers.json` (contact answers and profile links) and `private/resume/*.json`
(employers, schools, contact fields). Phone numbers are matched in international and national form, profile
links add their handle (github.com/<handle>) or personal domain, and a term of several words also matches when
it is written with hyphens, underscores, dots or no space (Acme-Corp, AcmeCorp). It never prints a matched term,
only its number. If you are the person whose data must never appear, keep an extra list outside the repo and
point the check at it (lines shorter than 3 characters are skipped and reported by line number):

```bash
export JH_LEAKCHECK_DENYLIST="$HOME/.config/jobhunter-denylist.txt"   # one term per line, # for comments
python3 tools/leakcheck.py
```

The check refuses a denylist file that lives inside the repo, so the list itself can never be committed.

## How the work is split

The code is split into ten units with no shared files, plus an integration owner (INT) for the end-to-end
tests. Units talk to each other only through the database
schema, the frozen error codes in `scripts/jobhunter/errors.py`, `scripts/jobhunter/acl.json`, the Python
interfaces between modules, the file formats agents write, the CLI envelope, and `openclaw/crons.json`.

- A unit never edits another unit's file. When it needs a function that has not landed yet, it writes a fake
  under `tests/fakes/u<its number>/` and tests against that.
- `scripts/jobhunter/cli.py` discovers every module in `scripts/jobhunter/commands/`, so each unit owns its
  own command modules and nobody edits a shared dispatcher.
- Hooks that run inside another unit's transaction never commit, never open a transaction, never touch the
  network and finish in under 100 ms.

| Unit | Area |
|---|---|
| U1 | Core ledger, gates, limits, authority, dispatcher, housekeeping |
| U2 | Discovery (APIs and browser search) and evaluation |
| U3 | QC linter and reviewer, drafts, approvals |
| U4 | Profile, resume building and tailoring, answer bank |
| U5 | Google Sheets mirror, notifications, digest, status |
| U6 | Browser agents, outreach, threads, replies, apply queue, page drivers |
| U7 | Installer, uninstaller, control wrapper, OpenClaw declarations, repo checks, docs |
| U8 | The jobhunter-guard OpenClaw plugin |
| U9 | Email transport (SMTP and IMAP) |
| U10 | Email finder (provider chain, verification, budget, cache) |
| INT | End-to-end tests across units (`tests/test_e2e_*.py`) |

### File ownership

`tests/test_repo_layout.py` reads the block below and fails when a file in the repo matches no pattern, or
more than one unit. Patterns are globs relative to the repo root: `*` stays inside one folder, `**` crosses
folders, `{a,b}` lists alternatives. Add a pattern here in the same change that adds a new kind of file.

<!-- owners:start -->
```text
U1 scripts/jh.py
U1 scripts/jobhunter/{__init__,cli,paths,errors,canon,events,db,auth,config,hardmax,keys,companies,people,exclusions,jobstate,hooks,locks,cycles,gate,reconcile,ceilings,pacing,breakers,identity,dispatch,audit,housekeeping,selftest,ocrun}.py
U1 scripts/jobhunter/{otp,accounts,captcha,secretstore}.py
U1 scripts/jobhunter/{acl.json,schema.sql}
U1 scripts/jobhunter/migrations/0001_init.sql
U1 scripts/jobhunter/migrations/0003_otp_accounts.sql
U1 scripts/jobhunter/detect/{__init__.py,linkedin.json,gmail.json,boards.json,ats.json,smtp.json}
U1 scripts/jobhunter/data/*
U1 scripts/jobhunter/commands/{__init__,core,gate,limits,maint}.py
U1 scripts/jobhunter/commands/accounts.py
U1 config.example.json
U1 private.example/{exclusions.example.csv,company_aliases.example.csv}
U1 docs/CONFIG-REFERENCE.md
U1 tests/{__init__,helpers}.py
U1 tests/test_{db_meta,triggers,keys_jobs,keys_company,keys_person,companies_resolve,people_resolve,jobstate,gate,reconcile,ceilings,pacing,breakers_detect,exclusions,auth,acl,dispatch,audit,housekeeping}.py
U1 tests/test_{otp,accounts,secretstore,captcha,no_captcha_solver}.py
U1 tests/fixtures/core/**
U1 tests/fakes/u1/**
U2 scripts/jobhunter/{jobs,prefilter,evaluate,searches}.py
U2 scripts/jobhunter/sources/*.py
U2 scripts/jobhunter/commands/{jobs,sources,eval}.py
U2 prompts/evaluator_scorecard.md
U2 private.example/targets.example.csv
U2 agent-templates/{scout,evaluator}/{AGENTS.template.md,SOUL.md,IDENTITY.md}
U2 agent-templates/scout/skills/{jobhunter-browser-search,jobhunter-linkedin-posts}/**
U2 agent-templates/evaluator/skills/jobhunter-evaluate/**
U2 tests/test_{sources_parsers,ingest,prefilter,evaluate,searches}.py
U2 tests/fixtures/discovery/**
U2 tests/fakes/u2/**
U3 scripts/jobhunter/{drafts,approvals}.py
U3 scripts/jobhunter/qc/{__init__,lint,review,worker,presend}.py
U3 scripts/jobhunter/commands/{drafts,qc}.py
U3 qc/{banned_phrases.json,schema_review.json}
U3 qc/golden/**
U3 prompts/{reviewer.md,writer_brief.md,rewrite.md,tone_rules.json}
U3 agent-templates/qc/{AGENTS.template.md,SOUL.md,IDENTITY.md}
U3 agent-templates/outreach/skills/jobhunter-write/**
U3 skills-src/jobhunter-qc-loop/**
U3 docs/WRITING-RULES.md
U3 tests/test_{lint,presend,review_parse,qc_worker,drafts,approvals}.py
U3 tests/fixtures/qc/**
U3 tests/fakes/u3/**
U4 scripts/jobhunter/{profile,pdftext,answers}.py
U4 scripts/jobhunter/resume/{__init__,model,tailor,pdf,docx,fonts_helvetica}.py
U4 scripts/jobhunter/commands/{profile,resume,answers}.py
U4 prompts/{profile_inference,resume_tailor,form_answer,salary_research}.md
U4 private.example/{extra_info.example.md,answers.example.json,profile.example.json}
U4 agent-templates/evaluator/skills/jobhunter-profile/**
U4 agent-templates/scout/skills/jobhunter-salary/**
U4 agent-templates/applier/skills/{jobhunter-resume-tailor,jobhunter-form-answers,jobhunter-upload}/**
U4 tests/test_{profile,answers,resume_model,resume_pdf,resume_docx,resume_stage}.py
U4 tests/fixtures/profile/**
U4 tests/fakes/u4/**
U5 sheets/{Code.gs,appsscript.json}
U5 scripts/jobhunter/{sheets,sheets_rows,sheets_labels,notify,digest,status,export}.py
U5 scripts/jobhunter/commands/{sheet,notify,report}.py
U5 shared-skills/jobhunter-control/**
U5 docs/GOOGLE-SHEETS.md
U5 docs/img/sheets/**
U5 tests/test_{sheets_rows,sheets_sync,sheets_labels,notify,digest,status}.py
U5 tests/fixtures/sheets/**
U5 tests/fakes/u5/**
U6 scripts/jobhunter/{contacts,research,emailcheck,outreach,threads,replies,applyq}.py
U6 scripts/jobhunter/{cdp,pagefill}.py
U6 scripts/jobhunter/commands/{outreach,threads,apply}.py
U6 drivers/**
U6 tools/gen_drivers.py
U6 prompts/reply_classifier.md
U6 agent-templates/{applier,outreach}/{AGENTS.template.md,SOUL.md,IDENTITY.md}
U6 agent-templates/applier/skills/{jobhunter-apply-ats,jobhunter-apply-boards,jobhunter-apply-email}/**
U6 agent-templates/outreach/skills/{jobhunter-research,jobhunter-linkedin,jobhunter-replies}/**
U6 skills-src/{jobhunter-gate,jobhunter-stop-detect}/**
U6 tests/test_{contacts,research,emailcheck,threads,replies,outreach,applyq,drivers_static,gate_flow_transcript}.py
U6 tests/test_{cdp,pagefill}.py
U6 tests/fixtures/browser/**
U6 tests/fakes/u6/**
U7 {install.sh,uninstall.sh,jobhunter}
U7 openclaw/{agents.json,agents.patch.json5.tmpl,crons.json,exec-approvals.json5.tmpl}
U7 scripts/jobhunter/install.py
U7 scripts/jobhunter/commands/install.py
U7 tools/{sync_skills,textcheck,leakcheck,check_gitignore}.py
U7 tools/install_hooks.sh
U7 .github/workflows/ci.yml
U7 macos/**
U7 {README.md,LICENSE,SECURITY.md,CHANGELOG.md,.gitignore}
U7 docs/{SETUP-macos,SETUP-linux-wsl,SAFETY-AND-TOS,HOW-IT-WORKS,TROUBLESHOOTING,PRIVACY,DEVELOPING}.md
U7 docs/img/setup/**
U7 tests/test_{install_render,install_consent,textcheck,leakcheck,check_gitignore,repo_layout,install_sh}.py
U7 tests/fixtures/install/**
U7 tests/fakes/u7/**
U8 openclaw/plugins/jobhunter-guard/**
U8 openclaw/guard-hosts.json
U8 docs/ENFORCEMENT.md
U9 scripts/jobhunter/mail/{__init__,smtp,imap,mime,outbox,precheck,fetch,audit}.py
U9 scripts/jobhunter/mail/{codes,webcodes}.py
U9 scripts/jobhunter/commands/mail.py
U9 skills-src/jobhunter-gmail-web/**
U9 docs/EMAIL-SETUP.md
U9 docs/img/email/**
U9 tests/test_mail_{mime,smtp,imap,outbox,precheck,fetch,audit}.py
U9 tests/test_mail_codes.py
U9 tests/fixtures/mail/**
U9 tests/fakes/u9/**
U10 scripts/jobhunter/enrich/**
U10 scripts/jobhunter/commands/enrich.py
U10 scripts/jobhunter/migrations/0002_enrich.sql
U10 docs/EMAIL-FINDER.md
U10 tests/test_enrich_*.py
U10 tests/fixtures/enrich/**
U10 tests/fakes/u10/**
INT tests/test_e2e_*.py
INT tests/fixtures/e2e/**
```
<!-- owners:end -->

Generated files have one owner too: `drivers/manifest.json` is written by `tools/gen_drivers.py` (U6); the
rendered workspaces, crons and config patches are written by the install renderers (U7) outside the repo or
under the gitignored `state/`, and `shared-skills/*/SKILL.md` (gitignored) is rendered from its template by
`install render-shared-skills` only for an install made with `--chat-control`.

## The install renderers (U7)

`install.sh`, `uninstall.sh` and `./jobhunter` never build OpenClaw commands by hand. They ask
`scripts/jh.py install ...` (module `scripts/jobhunter/install.py`) to render them from the committed
declarations:

| Declaration | Rendered by | Used by |
|---|---|---|
| `openclaw/crons.json` | `install render-crons` (cron add lines), `install render-crons --alerts` (failure alert edits), `install render-crons --repair <cron list> [--replace\|--check\|--report]` (full-field `cron edit` of drifted jobs, then `cron rm` and add; `--report` names the drifted fields and missing jobs of the list taken before `cron add`, which OpenClaw 2026.9.8 already uses to rewrite a job, so step 13 prints every repair). A command job's argv is `install.COMMAND_ENV_PREFIX` (`/usr/bin/env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT`) plus its declared argv, so `jh.py` stays `system` when the Gateway was started from Claude Code | install.sh step 13 |
| `openclaw/crons.json` (specs) | `install manifest --set <cron list>` writes `cron_jobs` and `cron_specs` (every declared field after substitution, `install.cron_specs`) to `state/install-manifest.json` | the drift check before every `cron run` (`ocrun.preflight`, `install run-preflight <key>`, the dispatcher, `ocrun.qc_turn`) |
| `openclaw/agents.json` | `install agents`, `install render-workspaces` | install.sh steps 7 and 8, uninstall.sh |
| `openclaw/agents.patch.json5.tmpl` | `install render-agents-patch` (explicit `tools.exec` and `tools.elevated` from `exec_policy`; `tools.exec` holds `mode` only, because OpenClaw 2026.9.5 and later refuse `mode` next to `security` or `ask`, and the patch deletes an older install's `security` and `ask` with JSON null in the same write) | install.sh step 8, right after `agents add` |
| `openclaw/exec-approvals.json5.tmpl` | `install render-approvals --current <approvals get --json>` (one argPattern per agent: `install.arg_pattern(repo, agent_id, carrier)`; `autoAllowSkills` false) | install.sh step 8, uninstall.sh |
| `macos/*.plist.tmpl` | `install render-stayawake` | install.sh step 15 |
| `shared-skills/*/SKILL.template.md` | `install render-shared-skills` (`--remove` on uninstall) | install.sh step 9 with `--chat-control`, uninstall.sh |
| `skills.workshop.autonomous.mode` (the one global key, with the owner's consent) | `install workshop-mode --current <config get --json>`, `install render-workshop-patch` (mode `propose`) | install.sh step 7b |
| fail-closed config of a stopped install | `install render-confine-patch --present <agents list --json>` (exec deny, every tool denied, for the jobhunter agents still present) | install.sh exit trap while step 8 is unfinished |

Read-back and run helpers (they call read-only `openclaw` commands through `jobhunter.ocrun`, never a write
except the `config unset` of `--fix`):

| Command | What it does | Used by |
|---|---|---|
| `install cron-id <key> [--from-list <file> --if-enabled]` | the id of one declared automation | install.sh steps 0b and 17 (dispatcher pause) |
| `install run-preflight <key> [--with-wait]` | guard heartbeat with identity proof version 2, then the drift check of the job (`ocrun.preflight`); prints the id (and the wait: job timeout plus 5 minutes) | `./jobhunter` `cron_turn` (lane runs, wizard steps 6 and 7), `doctor --probe`, install.sh step 14 |
| `install verify-exec-policy [--fix]` | effective exec policy of every jobhunter agent (`ocrun.effective_exec`): allowlist (qc deny), ask off, elevated off, approvals ask off and askFallback deny; the host approvals (`approvals get --json`) add nothing for a jobhunter agent: no `agents["*"]` allowlist entries (OpenClaw prepends them to every agent), no entries beyond the rendered one, `autoAllowSkills` false (`install.approvals_problems`); `--fix` unsets exec keys the installer does not write and prints an `unset <path>` line for each | install.sh step 8, `doctor` |
| `install foreign-jobs [--from-list <file>] [--check]` | automations that run a jobhunter agent but are not in the manifest (OpenClaw's `skill-collection-review:<agent>` jobs, a leftover QC one-shot, a job added by hand); `--check` fails while one is enabled | install.sh step 13, `doctor` |
| `install identity-boundary` | other agents with an unconfined shell (red lines, exit 0) | install.sh summary, `doctor` |
| `install probe-stamp read [--match] \| write` | versions the identity probes last passed with | install.sh step 14, `doctor` |
| `install cli-route set\|get\|show` | `cli_route` in `private/home.json`: identity carriers, QC reply mode (`install.set_cli_route(qc_reply="file")` is the F-QC switch), tool mode (`install.check_cli_tools` is the one check of the mode: native needs the acceptance and the argv carrier alone) | install.sh step 6, `jh.py qc smoke` |
| `install pin-hook` | one informational line about the guard's `before_prompt_build` hook from the heartbeat (`pin_hook_seen_at`); WARN when it never ran (OpenClaw 2026.9.8 without `hooks.allowConversationAccess`, which the installer never sets) | `./jobhunter doctor` |
| `install claude-check --help-file <file> [--version-text <claude --version>]` | refuses a `claude` without `--tools`, `--strict-mcp-config`, `--setting-sources`, and one older than a model of the jobhunter agents or jobs needs (`install.CLAUDE_MODEL_MINIMUM`, from Claude Code's own "N or newer required" refusal; add a model there when it names one) | install.sh step 3 |
| `install oc-doctor --file <stdout> --stderr-file <stderr> --rc <status>` | judges `openclaw doctor --lint --json` by finding severity (`install.doctor_verdict`): an error (or unknown severity) fails, warnings are listed, the `skill-workshop-tool-policy` finding of a jobhunter agent is expected; OpenClaw 2026.9.8 reports ok false for warnings only | `./jobhunter doctor` |

Template placeholders in `agent-templates/`, `skills-src/` and the `ref` files listed in `agents.json`:

| Placeholder | Becomes |
|---|---|
| `__REPO__` | absolute path of this clone |
| `__PY__` | absolute path of the python3 recorded in `private/home.json` |
| `__WS__` | the agent's own workspace, `WS_ROOT/<role>` |
| `__WS_ROOT__` | the folder that holds every agent workspace |
| `__ROLE__`, `__AGENT_ID__` | for example `applier` and `jobhunter-applier` |

`tools/sync_skills.py --check` verifies that every skill listed in `openclaw/agents.json` exists exactly once
(in `agent-templates/<role>/skills/<name>/` or `skills-src/<name>/`) with valid frontmatter, and that no skill
folder is left unlisted.

## Browser consent (the installer's helpers)

`private/consent.json` (mode 600) holds one row per site: `{"version": 1, "updated_at", "sites": {"<site>":
{"site", "status": "granted"|"declined"|"revoked", "method": "chrome_import"|"manual_login", "domains",
"chrome_profile", "chrome_profile_name", "granted_at", "revoked_at", "declined_at", "by"}}}`. A site is usable
only when its row has status `granted`, a `granted_at` and no `revoked_at`; a missing, linked, foreign-owned,
group- or world-writable or malformed file gives no site consent. Grants and revokes go through the human-only
`browser consent grant|revoke` commands (U1, PIN); a No is recorded with `install consent-record --decline`
(PIN). The wrapper's `browser consent|forget|check|login|import|sites` use the `install consent-sites`,
`chrome-profiles`, `consent-imports`, `login-probe` and `login-check` helpers (U7). The cookie copy is
`openclaw browser import-profile --browser chrome --system <folder> --into jobhunter --domains <allowed>`;
OpenClaw has no per-domain cookie delete (`browser cookies clear` clears the profile), so a revoke clears the
profile and copies the sites still allowed again. Chrome profiles are listed from Chrome's `Local State` file
only (display name, and whether the profile is signed in to a managed domain).

## Fakes and fixtures

- `tests/helpers.py` (U1) gives a temporary install root, a database factory and a fake clock, and the agent
  identity helpers: `agent_env` and `agent_argv` (both identity carriers for one agent and session),
  `agent_cli` (an in-process agent call that looks like `python -I`) and `run_jh` (a `python -I` child). Tests
  never hand-write agent environments.
- `tests/fixtures/install/fake-openclaw` answers every `openclaw` command the installer uses and logs each
  call as JSON. It refuses to run unless `FAKE_OC_LOG` and `FAKE_OC_STATE` are set. For the browser consent
  step it keeps the agent profile's cookie domains (`browser import-profile --domains` adds, `browser cookies
  clear` empties), answers the read-only login probe (`browser evaluate`) as logged in only for sites whose
  cookies it holds, records which sites `private/consent.json` granted at the moment of each copy
  (`FAKE_OC_CONSENT_FILE`), and plays a verification prompt (`FAKE_OC_CHECKPOINT`) or another Gmail account
  (`FAKE_OC_GMAIL_ACCOUNT`). For the CLI route it keeps the cron job fields of OpenClaw 2026.9.5
  (`agentId`, `sessionTarget`, `payload`, `delivery`), answers `cron edit`, `cron run --wait`, `sandbox explain
  --json`, `exec-policy show --json`, `config get` and `config unset`, writes the probe files of an identity
  check (`FAKE_OC_PROBE=ok|missing|system`), and plays an old guard (`FAKE_OC_GUARD_PROOF=1`), a Gateway that
  needs a restart (`FAKE_OC_HEARTBEAT_ON=restart`), a global exec value (`FAKE_OC_GLOBAL_EXEC`) and an edit that
  does not take (`FAKE_OC_EDIT_IGNORES`). Like OpenClaw 2026.9.5 and later it refuses a config patch whose exec
  object holds `mode` with `security` or `ask`, layers exec policies like OpenClaw (a per-agent `mode` replaces an
  inherited security and ask), lists an enabled system-owned `skill-collection-review:<agent>` job per agent while
  `skills.workshop.autonomous.mode` is auto (unset counts as auto; `FAKE_OC_WORKSHOP_MODE` seeds it, and the test
  Sandbox starts with `propose`) and refuses to edit, disable or remove those jobs, and plays failures for the
  fail-closed install (`FAKE_OC_APPROVALS_SET_FAIL=1`, `FAKE_OC_ADD_FAIL=<agent id>`, `FAKE_OC_DELETE_FAIL=1`).
  Like OpenClaw 2026.9.8 it cancels `plugins install` of a local path without `--force`, links the plugin
  disabled, refuses `plugins enable jobhunter-guard` while the guard config has no `repo`, and answers
  `doctor --lint --json` with ok false and warnings only (exit 1; `FAKE_OC_DOCTOR_ERROR=1` adds an error);
  `FAKE_OC_ADD_UPSERTS=1` plays a `cron add` that rewrites a known job, `FAKE_OC_PLUGINS_LIST_FAIL=1` a failing
  plugin listing. The Sandbox's fake `claude` reports version 2.1.280 (`FAKE_CLAUDE_VERSION` changes it).
- `tests/fakes/u6/fake_cdp.py` (U6) is an in-process Chrome DevTools Protocol server on a loopback port for the
  code-owned browser steps (`jh.py account create`, `account signin`, `code submit`, `code open-link`, the CAPTCHA
  screenshot and check). It serves the recorded pages of `tests/fixtures/browser/ats_account_pages.json` as a tiny
  DOM model, answers only the fixed `pagefill.py` scripts and the read-only Gmail drivers, applies clicks, typing
  and navigations through a per-page state machine, and logs method names with the length of typed text, never a
  value. Point the code at it with `cdp.set_test_port(port)` or a `browser_cdp` entry in the test `home.json`.
  `tests/fakes/u9/fake_inbox.py` serves one set of messages on both email routes (the fake IMAP server and Gmail
  web pages through the fake CDP server), `tests/fakes/u1/fake_security.py` stands in for the macOS `security`
  tool (it records argv and stdin apart, so a test proves a password only ever goes to stdin), and
  `tests.helpers.secret_hits` scans logs, state, the database, Sheet rows, output and recorded argv for the fake
  secrets.
- `tests/fixtures/install/argpattern.mjs` compiles the rendered argPattern with node's RegExp, so the test proves
  Python and OpenClaw read it the same way.
- `tests/fakes/u7/fake_core_commands.py` fills in core commands (`init`, `selftest`, `pause`, `qc smoke`) that
  have not landed yet, inside the temporary copy used by `tests/test_install_sh.py` only. With
  `FAKE_U7_OCRUN=1` it also replaces `ocrun.preflight`, `effective_exec`, `agents_list` and `qc_turn` with
  readers of the fake openclaw's JSON.

## Live check on a test profile (Claude subscription route)

Run this before a release that touches the agents, the guard or the installer, and after an OpenClaw update.
It never touches your own OpenClaw: every write command carries `--profile jhtest`, whose state lives in its own
folder. `<scratch>` is a folder of your choice outside the repo; nothing personal goes into the repo.

1. Clone the repo into `<scratch>/jh-jhtest` and give its `private/` fictional data only (Alex Rivera, Kestrel
   Commerce, addresses at example.com).
2. Snapshot what must not change: the file list and modification times of your OpenClaw state folder and
   `~/Library/LaunchAgents`, a hash of `~/.claude/settings.json`, and the top-level key names of `~/.claude.json`.
3. Set up the profile without a service: `openclaw --profile jhtest onboard --non-interactive --accept-risk
   --mode local --auth-choice anthropic-cli --gateway-bind loopback --gateway-port <port> --no-install-daemon
   --skip-channels --skip-search --skip-skills`, then start its Gateway in the foreground with a minimal
   environment (`env -i HOME=... PATH=... openclaw --profile jhtest gateway run --port <port> --bind loopback`),
   so no Claude Code variable of your own session reaches the agents. To test another OpenClaw build than the one
   on your PATH (for example one unpacked with `npm install --ignore-scripts --prefix <scratch>/oc-<version>
   openclaw@<version>`), use its binary for these commands and pass it to the installer below.
4. `cd <scratch>/jh-jhtest && ./install.sh --profile jhtest --no-daemon --yes --skill-workshop-propose
   [--openclaw-bin <absolute path of that openclaw>]`. The installer prints `openclaw binary <path>` in step 1 and
   records it in `private/home.json`, so `./jobhunter` and later re-runs use the same build; without
   `--openclaw-bin` it uses the `openclaw` on your PATH. `--skill-workshop-propose` sets the jhtest profile's
   `skills.workshop.autonomous.mode` to `propose` (without it a `--yes` install stops at step 7b). Without a
   terminal step 8 skips `models auth login` (the Claude CLI route uses your `claude` login), and `--yes`
   confirms OpenClaw's question about linking the guard plugin from outside ClawHub (`plugins install --link
   --force`, only while the plugin is not installed); step 10 writes the guard config before `plugins enable`. Step 14 runs the
   identity probes and the QC turn; `./jobhunter doctor` must be green apart from the red identity-boundary lines
   you expect.
5. Set a test owner PIN (random digits, kept in a mode 600 file under `<scratch>`, never in the repo). The first
   `jh.py --human auth set-pin` needs a terminal: type it yourself, or drive it with a small pseudo-terminal script
   that waits for each `New PIN` prompt before it writes the line. Do not pipe the PIN into `script -q /dev/null`:
   the prompt turns echo off with a flush that discards input sent before it, so the command waits for ever.
   Every later PIN step, a PIN change included, runs without a terminal as `jh.py --human --pin-stdin <command>`
   with the PIN piped in from that file (`--pin-stdin` reads the first line; `auth set-pin` then reads the new
   PIN twice from the next lines).
6. Work through the verification list in `docs/TROUBLESHOOTING.md`: restricted runs (no Claude Code tools, a
   question to a person cancelled at once), refusals within seconds and never a pending approval, path forms
   refused, the browser profile rule, the onboarding pair recorded as agents, a second install that repairs
   seeded drift. Never run `./jobhunter resume`, never connect a channel, mail or a Sheet on that profile.
7. Remove the `jhtest:*` test jobs, stop the foreground Gateway, and compare the snapshots of step 2: they must
   be identical.

## Release checklist

1. All checks above pass on a clean clone (CI runs them on a macOS runner).
2. Install on a separate clone with a separate OpenClaw profile as described in the live check above, then work
   through the open verification list in `docs/TROUBLESHOOTING.md` (restricted runs under the Claude CLI
   runtime, the exec policy read-back, `/jh` on WhatsApp, and the other items).
3. `./jobhunter doctor` is green on that clone, every automation is disabled until `./jobhunter resume`.
4. `git ls-files` shows no personal data and no runtime files; `tools/leakcheck.py` passes with your private
   denylist set.
5. Update `CHANGELOG.md` and bump the version in `scripts/jobhunter/__init__.py` and the plugin package.
