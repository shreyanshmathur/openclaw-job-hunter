# Setup on macOS, step by step

The README has the short version. This page adds the details and what to do when a step does not go as
expected. Screenshots referenced below live in `docs/img/setup/` (see the list there).

## 0. Before you start

- macOS 13 or newer, an administrator account is not needed for anything below.
- About 30 minutes, your resume (PDF or DOCX), the Gmail account you will send from, and a Google account for
  the Sheet (it can be the same one).
- Decide how Claude will run: your Claude subscription (Claude Code login) or an Anthropic API key.

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

**Claude subscription:**

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude auth login
claude auth status --text
```

Sign in as the same macOS user that runs OpenClaw. After Claude Code updates, restart the Gateway:
`openclaw gateway restart`.

**API key:** create one in the Anthropic Console and run `./install.sh --api-key`. The installer asks for the
key with the input hidden (nothing lands in your shell history) and passes it to OpenClaw's onboarding through
that one command's environment, never as a command line argument. OpenClaw stores the key; it is never written
into this folder. Avoid `export ANTHROPIC_API_KEY="..."` with the key typed out: the shell history keeps it. If
you must run the installer without a terminal prompt, read the key hidden first:
`read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY`, then `./install.sh --api-key --yes`.

## 4. The repo and the installer

```bash
cd ~
git clone https://github.com/<owner>/openclaw-job-hunter.git
cd ~/openclaw-job-hunter
./install.sh
```

Options: `--api-key`, `--profile <name>` (a separate OpenClaw profile, for a second person or a test install in
a second clone), `--chat-control` (renders the `/jh` help skill in `shared-skills/` and adds that folder to your
own OpenClaw agent's skill folders), `--upload-root <dir>` (OpenClaw's browser upload folder, only when it is not
`/tmp/openclaw/uploads`), `--stay-awake`, `--no-smoke`, `--yes` (no questions; optional extras are skipped).
The preflight also checks that your python3 has SQLite 3.24 or newer.

What the 17 steps do: preflight, OpenClaw, model route, onboarding, Gateway, local state and PIN, agent
workspaces, agents, per-agent config and exec approvals, guard plugin, browser profile (with the site consent
step, asked at a terminal), chat channel check, automations (all disabled), smoke tests, stay awake (asked), the
optional email finder keys (asked), next steps. Re-running is safe. Nothing asks for a password; the owner PIN is
local to this computer and keeps the agent from approving its own drafts or raising its own limits.

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
lists them under `input_files`). If an agent turn ends without recording anything, the wizard says so; run
`./jobhunter init` again and it repeats that step.

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
