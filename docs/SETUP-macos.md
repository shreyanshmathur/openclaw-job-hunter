# Setup on macOS, step by step

The README has the short version. This page adds the details and what to do when a step does not go as
expected. Screenshots referenced below live in `docs/img/setup/` (see the list there).

## 0. Before you start

- macOS 13 or newer, an administrator account is not needed for anything below.
- About 30 minutes, your resume (PDF or DOCX), the Gmail account you will send from, and a Google account for
  the Sheet (it can be the same one).
- Decide how Claude will run: your Claude subscription through the Claude Code login (recommended: no API key,
  usage counts against your Pro or Max plan) or an Anthropic API key.
- Run every command yourself in the Terminal app. The installer and the wizard ask questions and a PIN, which
  needs a terminal; run from a script or another program, they skip those parts.

## 1. Command line tools, Python, git

```bash
xcode-select --install
python3 --version     # 3.9 or newer
git --version
```

If `python3` opens an install dialog, accept it and run the command again.

## 2. OpenClaw

The installer installs OpenClaw into `~/.openclaw` when it is missing (the official `install-cli.sh`, without its
own onboarding). To install it yourself:

```bash
curl -fsSL --proto '=https' --tlsv1.2 https://openclaw.ai/install-cli.sh | bash -s -- --no-onboard
echo 'export PATH="$HOME/.openclaw/bin:$PATH"' >> ~/.zprofile
exec zsh -l
openclaw --version
```

The PATH line makes the short name `openclaw` work in new terminals; these docs use it. When the installer
installs OpenClaw for you it prints the same line if your terminal does not find `openclaw` yet. A profile
install (`./install.sh --profile <name>`) needs `--profile <name>` on every `openclaw` command.

When OpenClaw is new, `./install.sh` runs a non-interactive local onboarding (Gateway bound to this computer
only, started as a LaunchAgent). When OpenClaw is already set up, the installer skips onboarding and never
changes your existing agents, channels, bindings or defaults.

## 3. Claude

**Claude subscription (recommended):**

```bash
curl -fsSL https://claude.ai/install.sh | bash
exec zsh -l                   # so the new claude command is found
claude auth login             # opens your browser
claude auth status --text     # must say you are logged in
```

Sign in as the same macOS user that runs OpenClaw. The writing agents use a model that needs a recent Claude
Code: the installer checks `claude --version` and tells you to run `claude update` when it is too old. After
Claude Code updates, restart the Gateway (`openclaw gateway restart`) and run `./install.sh` again. When the login
expires later, `./jobhunter doctor` shows `FAIL  Claude login`: run `claude auth login` again.

**API key (alternative):** create one in the Anthropic Console and run `./install.sh --api-key`. The installer asks for the
key with the input hidden (nothing lands in your shell history) and passes it to OpenClaw's onboarding through
that one command's environment, never as a command line argument. OpenClaw stores the key; it is never written
into this folder. Avoid `export ANTHROPIC_API_KEY="..."` with the key typed out: the shell history keeps it. If
you must run the installer without a terminal prompt, read the key hidden first:
`read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY`, then `./install.sh --api-key --yes`. The key is asked
only while OpenClaw is set up for the first time; the installer remembers the route, so later runs (and
`./jobhunter update`) need no `--api-key`.

## 4. The repo and the installer

```bash
cd ~
git clone https://github.com/<owner>/openclaw-job-hunter.git
cd ~/openclaw-job-hunter
./install.sh
```

Options: `--api-key` (and `--claude-login` to switch back; the route of the first install is remembered),
`--profile <name>` (a separate OpenClaw profile, for a second person or a test install in
a second clone), `--chat-control` (renders the `/jh` help skill in `shared-skills/` and adds that folder to your
own OpenClaw agent's skill folders), `--upload-root <dir>` (OpenClaw's browser upload folder, only when it is not
`/tmp/openclaw/uploads`), `--stay-awake`, `--yes` (no questions; optional extras are skipped), `--no-daemon`
(never install or start a Gateway service; use the one you run), `--smoke` (run the agent identity checks again)
and `--no-smoke` (skip them for now; `./jobhunter doctor --probe` runs them later), `--openclaw-bin <path>` (use
that `openclaw` executable instead of the one on your PATH, for a test install of another OpenClaw build) and
`--skill-workshop-propose` (your answer to the Skill Workshop question below, for a `--yes` install). The preflight also checks that
your python3 has SQLite 3.24 or newer, and on the Claude route that your `claude` is new enough to start agents
with its own tools switched off and to run the models the jobhunter agents use.

What the 17 steps do: preflight (and a pause of a running dispatcher until the install is complete), OpenClaw,
model route, onboarding, Gateway, local state and PIN, agent workspaces, the Skill Workshop question, agents
together with their per-agent config and exec approvals, shared skills (with `--chat-control`), guard plugin
(OpenClaw asks before it links a plugin that is not from ClawHub: the installer asks you at a terminal, and `--yes`
confirms it), browser profile (with the site consent step, asked at a terminal), chat channel check,
automations (all disabled, every field compared with the declaration and repaired; each repair is printed), agent
identity checks, stay
awake (asked), the optional email finder keys (asked), next steps. Re-running is safe. Nothing asks for a
password; the owner PIN is local to this computer and keeps the agent from approving its own drafts or raising
its own limits.

The exec approvals step gives each jobhunter agent one allowlist entry: the project's own `jh.py`, run with
`python -I`, with a proof of that agent's identity that the guard adds. Its policy is allowlist with ask off, so
anything else is refused at once and nothing ever waits for you to click. The installer reads the effective
policy back and stops if OpenClaw reports anything else. The identity checks then run each agent once (it runs
`jh.py whoami`, writes and reads one file) and one QC turn; they run again only when OpenClaw, claude, the guard
or `jh.py` change.

Each agent is restricted in the same step that adds it. If the installer stops before the restrictions are read
back, it removes the agents it just added (or, when that fails, sets every jobhunter agent to exec deny with every
tool denied) until `./install.sh` completes. Without a terminal the installer skips `openclaw models auth login`
for the new agents; the Claude CLI route uses your `claude` login.

OpenClaw's Skill Workshop runs every agent once a week to review and rewrite its skills while
`skills.workshop.autonomous.mode` is `auto`, OpenClaw's default. Those jobs belong to OpenClaw and cannot be
switched off one agent at a time, and they would run the jobhunter agents outside the dispatcher, the pause and
this project's checks. Before it adds any agent the installer therefore asks to set the mode to `propose` for your
OpenClaw profile (your other agents then get skill proposals instead of automatic weekly rewrites). If you say
no, the install stops; `--skill-workshop-propose` gives the answer for a `--yes` install. To turn the weekly
reviews back on later: `openclaw config set skills.workshop.autonomous.mode auto` (then `./jobhunter doctor`
reports the jobhunter agents' review jobs in red).

Other OpenClaw agents you use are not changed. Exec approvals that you keep for every agent (`agents["*"]` in
`openclaw approvals get`) would also apply to the jobhunter agents, so the installer and `./jobhunter doctor` stop
on them: move such entries to the agents that need them. If one of them has an unconfined shell (exec security full, or a
policy that asks you), the installer and `./jobhunter doctor` print a red line for it: such an agent runs as you
and is trusted like you, so it could act as a jobhunter agent.

Where things end up:

| What | Where |
|---|---|
| Your data (profile, resume copies, config, secrets, PIN hash) | `~/openclaw-job-hunter/private/` (mode 700) |
| Database, logs, exports | `state/`, `logs/`, `exports/` in the repo folder (gitignored) |
| Agent workspaces | `~/.openclaw-job-hunter/<install id>/workspaces/` |
| OpenClaw config entries | `agents.entries["jobhunter-*"]` and `plugins.entries.jobhunter-guard` only |
| Automations | `openclaw cron list --all`, names starting with `jobhunter-` |
| What the installer created | `state/install-manifest.json` |

## 5. The browser profile

The agents use their own OpenClaw browser profile, `jobhunter`. Your normal Chrome stays yours.

Which sites the agent may use is your decision, site by site: `./jobhunter browser consent` (the installer and
the first run wizard ask it too). It explains what happens, asks about Gmail, LinkedIn and each job site with
the answer No by default, lets you pick the Chrome profile by its name (a work or school profile is marked and
needs the typed word `work`), records your answers with your PIN in `private/consent.json`, and only then copies
the cookies of the allowed sites with OpenClaw's own command:

```bash
openclaw browser import-profile --browser chrome --system "<Chrome profile folder>" --into jobhunter --domains <allowed sites only>
```

The `--domains` filter keeps every other site's cookies out. macOS may ask for your login password so Chrome's
cookie store can be read. A read-only login check follows (Gmail must show your sender address, LinkedIn the
feed); Google sometimes refuses a copied session, and the check then tells you to log in by hand with
`./jobhunter browser login <site>`. Take a site back with `./jobhunter browser forget <site>` (or `--all`):
OpenClaw clears cookies for the whole profile, so the sites you still allow are copied again right after.
Allowing LinkedIn does not turn LinkedIn on (`./jobhunter linkedin enable` does, with its acknowledgement).

The agent never types a password and never creates accounts.

## 6. First run wizard

```bash
./jobhunter init
```

It walks through: PIN, folders, your details (first and last name, the Gmail address you send from, an optional
signature phone and links, and the chat app and number for notifications; stored under `owner` in
`private/config.json`), resume import, extra information, salary research, profile inference, your answers, a
feasibility check (for example a salary floor above what the market pays for the chosen roles), the base resume,
the sites the agent may use (the consent step above, only for sites not answered yet), email (Gmail in the
agent's browser by default, no password; the app password only if you set `gmail.route` to `app_password`),
history import, the Sheet (it offers to open sheets.new in your Chrome; Google then shows its Allow screen once),
and a final `doctor`.

Salary research and profile inference count as done only when their files exist (`./jobhunter profile status`
lists them under `input_files`). Both run as OpenClaw automations (`jobhunter:onboard-salary` and
`jobhunter:onboard-profile`) after a check that the guard runs and the automation is unchanged. If a run ends
without recording anything, the wizard says so; run `./jobhunter init` again and it repeats that step.

## 7. Keep the Mac awake

Automations do not run while the Mac sleeps. `./install.sh --stay-awake` (or answer yes when asked) installs a
LaunchAgent that keeps the Mac awake only while it is on AC power. Remove it with `./uninstall.sh` or
`launchctl bootout gui/$(id -u)/ai.openclaw-job-hunter.stayawake`.

## 8. Start

```bash
./jobhunter doctor
./jobhunter resume
./jobhunter status
```
