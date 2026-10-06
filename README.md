# openclaw-job-hunter

A job search assistant that runs on your own computer inside [OpenClaw](https://docs.openclaw.ai). It reads
your resume, learns what you want, finds jobs, scores every one of them against your profile, tailors your
resume truthfully, drafts applications and short personal emails, checks every draft twice, and logs
everything to a Google Sheet you own.

It is not a mass mailer. Limits are low on purpose, every message is researched and quality checked, and by
default nothing is sent until you approve it.

> **Read this first: the risks**
>
> - **LinkedIn automation breaks the LinkedIn User Agreement, at any volume.** LinkedIn does not allow bots,
>   scripts or browser automation to view profiles, send invitations or send messages for you. If LinkedIn
>   detects it, it can restrict your account, ask you to verify your identity, or ban the account for good.
>   Low limits and human-like pacing lower the chance of detection. They do not make it allowed. For that
>   reason every LinkedIn write action ships **turned off**. Turning it on needs your owner PIN and a typed
>   sentence saying you accept the risk (`./jobhunter linkedin enable`). If your LinkedIn account matters to
>   you, leave it off: email and company career pages work without it.
> - **Indeed and Glassdoor applications are never automated.** Reading their job lists is off by default and
>   opt in only.
> - **Several job boards forbid automated access** in their terms (for example Naukri, Instahyre, Foundit,
>   Cutshort, Hirist, iimjobs and Wellfound). They are off by default. Turning one on is your decision.
> - **The agent stops at the first sign of friction:** a CAPTCHA, a verification page, a login wall, a limit
>   warning, a bounce or a spam complaint. It never tries to solve or get around a challenge. You get a
>   message and decide what happens next.
> - **The agent uses your existing logins only for the sites you allow, one by one.** It works in its own
>   browser profile; your normal Chrome is never driven. Every site starts at No, and
>   `./jobhunter browser forget <site>` takes a site back at any time.
> - **Email goes out from your own Gmail account.** Bad targeting can hurt your reputation and Google can
>   limit your account. The defaults are conservative (at most 15 cold emails a day after a warm up that starts
>   at 5), and every email is written for one person only.
> - **You are responsible for your accounts and for what is sent in your name.** Start in the default
>   `human` approval mode and read what the agent writes.

Full details: [docs/SAFETY-AND-TOS.md](docs/SAFETY-AND-TOS.md).

## Quick start (macOS)

The fewest steps from a new Mac to a running agent. Everything goes into the **Terminal** app (press Cmd+Space,
type Terminal, press Enter). Copy one block at a time, paste it, press Enter. Every step is safe to run again.
The recommended way uses your **Claude subscription** (Pro or Max) through the Claude Code login: no API key and
no separate bill. An API key works too, see [Claude subscription or API key](#claude-subscription-or-api-key).

| # | Command | What it asks you | Time |
|---|---|---|---|
| 1 | `xcode-select --install` | A macOS window: click Install. Skip it if `git --version` already prints a version. | 5 to 15 min |
| 2 | Install Claude Code and sign in (block below) | Your browser opens: sign in with your Claude account. | 2 min |
| 3 | Get the code (block below) | Nothing. | 1 min |
| 4 | `./install.sh` | A few yes/no questions and a new owner PIN (see below). | 5 to 10 min |
| 5 | `./jobhunter init` | Your name, Gmail address, resume, what jobs you want, which sites the agent may use, your Google Sheet. | 15 to 30 min |
| 6 | `./jobhunter doctor`, then `./jobhunter resume` | Your PIN, to start. | 2 min |

```bash
# 2. Claude Code, signed in with your Claude subscription
curl -fsSL https://claude.ai/install.sh | bash
exec zsh -l                      # reloads Terminal so the new claude command is found
claude auth login                # opens your browser; sign in with your Claude account
claude auth status --text        # must say you are logged in
```

```bash
# 3. The code, in your home folder
cd ~
git clone https://github.com/<owner>/openclaw-job-hunter.git
cd ~/openclaw-job-hunter
```

```bash
# 4 to 6
./install.sh
./jobhunter init
./jobhunter doctor
./jobhunter resume
```

What `./install.sh` asks, in this order (at a terminal; it installs OpenClaw for you when it is missing):

1. **A new owner PIN**, 6 to 12 digits, twice. It stays on this computer and is not a password for any account.
   You type it to approve drafts, allow sites and raise limits. The agent never knows it.
2. **"Set skills.workshop.autonomous.mode to propose?"** Answer `y`. OpenClaw's Skill Workshop would otherwise run
   every agent once a week to rewrite its own instructions, outside this project's checks. The install stops if
   you answer no.
3. **"Link the guard plugin from this clone?"** Answer `y`. The guard is the part of this project that checks
   every agent action. OpenClaw asks because the plugin comes from this folder, not from its plugin store.
4. **Optional questions** (`n` is fine, you can do each later): which sites the agent may use with your Chrome
   logins (the wizard asks again), keeping the Mac awake on power (`y` is recommended so the agent runs), and
   email finder keys.

The last part of the install runs each agent once to prove who it is (about 2 to 4 minutes, it uses your plan).
If a step stops with `ERROR:`, the line says what to do; fix it and run `./install.sh` again. Steps that are
already done print `SKIP`. See [If something goes wrong](#if-something-goes-wrong).

> **OpenClaw 2026.9.8 and the Claude subscription route (October 2026):** an earlier version stopped at step 14
> with `the jobhunter-qc test turn failed: the reply was not the numbers 1 to 700`, because its test of the QC
> reviewer was not a review packet and the reviewer refused it. The test now sends a review packet with made-up
> data. If you saw that error, run `./jobhunter update`, then `./install.sh` again, and start the agent with
> `./jobhunter resume` only after an install completed. The new test has unit tests but has not been run live on
> 2026.9.8 yet. Skipping the check (`--no-smoke`) is not a fix. What was tested live on 2026.9.8 with a Claude login (an isolated test profile,
> October 2026): every other install step, the agent identity checks, the refusals of commands and paths outside
> each agent's lane, the onboarding runs and the reviewer calibration (19 of 20). OpenClaw 2026.9.5 was not
> tested again in that round.

Run your agents only through `./jobhunter` and the automations the installer created. Do not start a jobhunter
agent yourself with `openclaw agent`: such a run does not get the restricted tool list
([docs/ENFORCEMENT.md](docs/ENFORCEMENT.md)).

## What you need

- A Mac (macOS 13 or newer). Linux and Windows 11 with WSL2 work too, with a desktop session for the browser
  parts ([docs/SETUP-linux-wsl.md](docs/SETUP-linux-wsl.md)).
- **A Claude subscription (Pro or Max)** used through the Claude Code login (recommended), **or** an Anthropic
  API key.
- Python 3.9 or newer, git and curl (a Mac has all three after `xcode-select --install`).
- Google Chrome with a profile that is logged in to the Gmail account you want to send from and to the job
  sites you use. No password is typed into this tool: with your consent, per site, the logins are copied into
  the agent's own browser profile (or you log in by hand inside it).
- A Google Sheet you own (an empty one is fine).
- Your resume as a PDF or DOCX.
- Optional: a WhatsApp number for notifications and approvals (a separate number is recommended), free API
  keys of your own for the email finder, and a Google app password if you prefer that email route.
- A computer that stays on and awake during your chosen active hours.

What you are still asked for, and why: an **owner PIN** (6 to 12 digits, kept only on this computer; it is not
an account password, and because the agent never knows it, the agent cannot approve its own drafts or raise its
own limits), and **one Allow click in Google** when you set up the script of your Sheet (step 8).

Words used below: **OpenClaw** is the program the agents run inside (it starts them on a schedule and gives them
a browser). The **Gateway** is OpenClaw's background service on your computer. An **agent** is one Claude worker
with one job (find jobs, score them, apply, write emails, review drafts). An **automation** is a scheduled agent
run. **Terminal** is the macOS app where you type the commands.

## Claude subscription or API key

| | Claude subscription (recommended) | Anthropic API key |
|---|---|---|
| What you need | Claude Pro or Max, and Claude Code signed in (`claude auth login`) | A key from the Anthropic Console |
| Cost | Inside your plan's usage limits (a busy search can use a lot of a Pro plan) | Pay per use on the key |
| Install command | `./install.sh` | `./install.sh --api-key` (asks for the key with the input hidden) |
| If the login or key stops working | `claude auth login`, then `./jobhunter doctor --probe` | see [docs/SETUP-macos.md](docs/SETUP-macos.md), step 3 |

The installer remembers the route you chose: `./install.sh` and `./jobhunter update` keep it. To switch later:
`./install.sh --api-key` or `./install.sh --claude-login`.

## Install from zero on macOS, step by step

The Quick start above in more detail. Copy one block at a time. Each step can be re-run safely.

### 1. Developer tools

```bash
xcode-select --install          # git and python3; skip if already installed
python3 --version               # must print 3.9 or newer
```

### 2. OpenClaw (the installer does this for you)

`./install.sh` installs OpenClaw when it is missing. To do it yourself first:

```bash
curl -fsSL --proto '=https' --tlsv1.2 https://openclaw.ai/install-cli.sh | bash -s -- --no-onboard
echo 'export PATH="$HOME/.openclaw/bin:$PATH"' >> ~/.zprofile    # so the short name `openclaw` works
exec zsh -l
openclaw --version               # 2026.9.5 or newer
```

The rest of this page writes `openclaw ...`. If your terminal says the command is not found, add the PATH line
above (the installer prints it too), or type `~/.openclaw/bin/openclaw` instead. With `./install.sh --profile
<name>`, add `--profile <name>` to every `openclaw` command.

If you already use OpenClaw, keep your setup: the installer never resets it. It only adds its own agents,
automations and plugin, and it never changes your channels, bindings or default settings. The one global
setting it changes, after asking you, is the Skill Workshop mode (see step 4).

### 3. Connect Claude

**Recommended: your Claude subscription (Claude Code login).**

```bash
curl -fsSL https://claude.ai/install.sh | bash
exec zsh -l
claude auth login                # opens the browser; sign in with your Claude account
claude auth status --text        # must say you are logged in
claude --version                 # the installer tells you when this is too old: then run claude update
```

Agents then run through your subscription limits. Sonnet does the reading and scoring, Opus only writes. The
writing agents need a recent Claude Code; the installer checks the version and says `claude update` when it is
too old.

On this route every agent runs as a restricted OpenClaw run: an automation with a fixed tool list, so Claude
Code's own tools (Bash, Read, Write, Edit, AskUserQuestion and the rest) are switched off for the agents and they
only have OpenClaw's exec, read, write and browser tools. Those never wait for your approval: each agent may run
only this project's `jh.py`, anything else is refused at once. Your own Claude Code settings are not touched, and
the project never starts its agents with `openclaw agent`.

**Alternative: an Anthropic API key.** Create a key in the Anthropic Console, then run the installer with
`--api-key` in step 4. It asks for the key with the input hidden, so the key never lands in your shell history.
The installer hands it to OpenClaw's onboarding through that command's environment, not its command line, and
OpenClaw stores it; it is never written into this repo. (Do not type `export ANTHROPIC_API_KEY=...` with the
key in it: the shell history keeps that line.)

### 4. Get the code and install

Clone into your home folder. Do not use a path with spaces, and not Documents, Desktop, Downloads or iCloud
Drive (macOS blocks background services from reading those folders).

```bash
cd ~
git clone https://github.com/<owner>/openclaw-job-hunter.git
cd ~/openclaw-job-hunter
./install.sh                     # or, with an API key: ./install.sh --api-key
```

Run it yourself in the Terminal app, not from a script or another program: the PIN and the questions need a
terminal. Without one (or with `--yes`) the installer takes the safe answer for each optional question, skips the
PIN and the site question (set them later with `./jobhunter pin set` and `./jobhunter init`), and, while
OpenClaw's Skill Workshop is in its default mode, stops at that question unless you add `--skill-workshop-propose`.

The installer prints DONE or SKIP for each of its 17 steps: it checks your system, sets up OpenClaw if needed,
creates five agents (scout, evaluator, applier, outreach, qc) with their own workspaces outside the repo,
installs the guard plugin, creates a separate browser profile called `jobhunter`, declares every automation
**disabled**, and runs an identity check per agent: each agent runs the project's `jh.py whoami` once and must be
recognised as itself, with no question to a person (the check repeats only when a version changes; `--smoke`
forces it). It sets every jobhunter agent's exec policy to allowlist with ask off (the agent may run only the
listed command, and nothing ever waits for your click) in the same step that adds the agent, and reads the
effective policy back. It asks once to set OpenClaw's Skill Workshop to `propose`, so OpenClaw does not run the
agents weekly to rewrite their skills (`--skill-workshop-propose` answers for `--yes`). It asks for an owner PIN
(6 to 12 digits); every important change later asks for it. At a terminal it also offers the site consent step
(step 6 below) and the optional email finder keys; both print SKIP otherwise and can be done later. It never
asks for a password.

Agent identity cannot be forged by an agent whose shell is confined. An agent with an unconfined shell runs as
you and is trusted like you: if another OpenClaw agent you use (often `main`) can run any command, the installer
and `./jobhunter doctor` print a red line for it. Set its exec policy to allowlist with ask off if it should not.

### 5. Your details, resume and preferences (first run wizard)

```bash
./jobhunter init
```

The wizard first asks for your first and last name (they sign every email), the Gmail address you send from,
an optional phone number and links for the email signature, and the chat app and number where drafts and alerts
should reach you (WhatsApp in international form, for example `+<country code><number>`, or `none`). They are
stored in `private/config.json` under `owner` (never committed); edit that file to change them later, or
re-run the wizard.

Then it copies your resume into `private/resume/`, opens `private/extra_info.md` for anything that is not on
the resume (projects, results with numbers, tools, links), researches a realistic salary band from public
pages, suggests role families, and asks you to confirm: target roles, roles to avoid, seniority, salary floor
and target, notice period, cities and remote options, work authorization, companies to exclude, which job
sites to use, and whether email outreach is on. Nothing the model guessed is used until you confirm it. It
then asks which sites the agent may use with your Chrome logins, explains how email is sent, and offers the
history import and the Sheet (steps 6 to 8 below; each can also be done later). Re-run any time with `./jobhunter init` (finished steps print SKIP), or
change your answers with `./jobhunter profile`.

Add people and companies you already contacted before using this tool to `private/exclusions.csv` (the wizard
creates it with examples). They are never contacted.

### 6. Choose the sites the agent may use (your Chrome logins, with your consent)

The wizard asks this; to do it again later:

```bash
./jobhunter browser consent          # or name sites: ./jobhunter browser consent gmail naukri
```

What happens, in this order:

1. A plain explanation: the agent works in its own browser profile called `jobhunter`; your normal Chrome is
   never opened, driven or changed by it; for each site you allow, only that site's login cookies are copied
   into the `jobhunter` profile; they stay on this computer; nothing is sent anywhere else.
2. One question per site, **default No**: Gmail, LinkedIn, Naukri, Indeed, Glassdoor, Foundit, Instahyre,
   Wellfound, and any other job site you turned on. Allowing LinkedIn does not turn LinkedIn on: that still
   needs `./jobhunter linkedin enable` and its typed acknowledgement.
3. You pick the Chrome profile by its name (read from Chrome's `Local State` file). There is no default, and
   a work or school profile is marked and needs the typed word `work`. Or choose to log in by hand inside the
   agent's window instead (the only way on Linux and WSL).
4. Your PIN records the answers in `private/consent.json` (mode 600). Only then are the cookies copied, with
   OpenClaw's own `openclaw browser import-profile --domains <only the allowed sites>`. macOS may ask for your
   login password so the Chrome cookie store can be read; that window is macOS's own.
5. A read-only login check per site: Gmail must be logged in as your sender address, LinkedIn must show the
   feed without a checkpoint. A wrong account, a CAPTCHA or a verification prompt stops the check and tells
   you what to do; the agent never solves or skips one.

```bash
./jobhunter browser sites            # which sites the agent may use
./jobhunter browser check            # the read-only login check again
./jobhunter browser login naukri     # log in by hand inside the agent's window (an allowed site)
./jobhunter browser forget naukri    # take a site back (or --all); its cookies are cleared
```

No lane uses a site without an active consent: preflight, the send gate and the guard plugin refuse it in code.

### 7. Email: your own Gmail, no password needed

By default (`gmail.route = "web_ui"`) the agent sends from your own Gmail inside its browser profile, after you
allowed Gmail in step 6. Before the one allowed click it reads the To, Subject and body back and compares them
with the approved text; afterwards it confirms the send in the Sent folder. Replies and bounces are read in
the same browser. Every limit and stop rule applies as for any other route.

```bash
./jobhunter mail import-history --days 365   # recommended: nobody you already wrote to gets a cold email
```

**Optional: the app password route.** If you prefer code to send over SMTP (the text that goes out is then byte
for byte the approved text), set `gmail.route` to `app_password` in `private/config.json`, create an app password
at https://myaccount.google.com/apppasswords (needs 2-Step Verification) and run `./jobhunter mail connect`.
Nothing asks for an app password unless you pick this route. Details and Google Workspace notes:
[docs/EMAIL-SETUP.md](docs/EMAIL-SETUP.md).

### 8. Connect your Google Sheet

About three minutes, no Google Cloud project needed. The wizard offers to open a new sheet (sheets.new) in your
own Chrome for you:

1. Create an empty Google Sheet, for example "Job hunt log".
2. Extensions > Apps Script: paste `sheets/Code.gs` and `sheets/appsscript.json` from this repo, save.
3. Reload the sheet, then Job Hunter > Set up or repair this sheet. Google shows an Allow screen once: this is
   the one click the setup needs in your Google account (the script gets access to this sheet only). Copy the
   connection secret it shows.
4. Deploy > New deployment > Web app, Execute as: Me, Who has access: Anyone. Copy the web app URL.
5. In the terminal:

```bash
./jobhunter sheet connect        # paste the URL, then the secret
```

Step by step with pictures: [docs/GOOGLE-SHEETS.md](docs/GOOGLE-SHEETS.md).

### 9. Notifications and approvals in chat (optional)

```bash
openclaw plugins install @openclaw/whatsapp
openclaw channels login --channel whatsapp       # scan the QR code with the phone of that number
```

Messages go to the chat app and number you gave the wizard (`owner.notify` in `private/config.json`; set the
channel to `none` to turn chat off). Drafts then arrive in that chat, and you answer with `/jh approve A7K2`,
`/jh skip A7K2`, or `/jh edit A7K2 <your text>`. Without chat you approve in the Sheet (Approvals tab) or in
the terminal with `./jobhunter approve <code>`. If you change the number in that file later, run `./install.sh`
again so the guard and the failure alerts use it.

### 10. Check, then start

```bash
./jobhunter doctor               # every line must say ok
./jobhunter resume               # asks for your PIN, then turns the automations on
```

First run: the job source fetch and the evaluator start within minutes; browser work happens only inside
your active hours, at random times. To watch one cycle right away:

```bash
./jobhunter run evaluator        # score the jobs found so far
./jobhunter status
```

With the default `human` approval mode, every email and application waits for your approval. Nothing is sent
without it.

### Pausing and stopping

```bash
./jobhunter pause                # pauses everything; the Sheet and chat still update
./jobhunter pause linkedin       # or gmail, applications, site:<name>
./jobhunter stop-now             # pause, close the agent browser, mark anything in flight as unknown
./jobhunter resume               # start again (PIN)
```

From chat: `/jh pause`. Resuming always needs the terminal and your PIN.

## Updating

```bash
cd ~/openclaw-job-hunter
./jobhunter update
```

It downloads the new version (`git pull`), runs `./install.sh` again with the same model route and OpenClaw
profile as before, and runs a self test. Your data, PIN, site consents and settings stay as they are. A running
agent is paused while the installer works and starts again when it finished. The agent identity checks run again
only when a version changed, so an update takes a few minutes. If it says `sheets/Code.gs changed`, paste the new
`sheets/Code.gs` into Apps Script and deploy a new version of the same deployment
([docs/GOOGLE-SHEETS.md](docs/GOOGLE-SHEETS.md)).

If the download fails because files in the folder were changed by hand, `git status` lists them; put them aside
with `git stash` and run `./jobhunter update` again.

Updating the tools underneath, when a message asks for it:

```bash
openclaw update                  # OpenClaw; then ./install.sh
claude update                    # Claude Code; then openclaw gateway restart, then ./install.sh
```

## If something goes wrong

Start with `./jobhunter doctor`: every line must say `ok`, and a line that does not says what to do. The
installer works the same way: when it stops, the `ERROR:` line names the problem and the fix, and running
`./install.sh` again after the fix is always safe (finished steps print `SKIP`). The issues seen most often:

| What you see | What to do |
|---|---|
| `Claude Code is not signed in, or its login expired` (install), or `FAIL  Claude login` (doctor), or agent runs fail after weeks of working | `claude auth login` (opens your browser), `claude auth status --text`, then `./jobhunter doctor --probe` (or `./install.sh` if the install had stopped). |
| `Claude Code ... is too old for ...` | `claude update`, then `./install.sh` again. |
| `OpenClaw 2026.9.5 or newer is needed` | `openclaw update`, then `./install.sh` again. |
| `openclaw: command not found` | Run `echo 'export PATH="$HOME/.openclaw/bin:$PATH"' >> ~/.zprofile`, then `exec zsh -l`. Or type `~/.openclaw/bin/openclaw`. |
| `NOTE: set your owner PIN at a terminal`, `SKIP site consent`, `the wizard asks you questions, so it needs a terminal`, `this command needs your owner PIN at a terminal` | The command ran without a terminal (from a script, another program or an AI assistant) or with `--yes`. Open the Terminal app, `cd ~/openclaw-job-hunter`, and run `./jobhunter pin set`, then `./jobhunter init`, yourself. |
| `the jobhunter agents must not get OpenClaw's weekly skill reviews` | You answered no (or ran with `--yes`) at the Skill Workshop question. Run `./install.sh` and answer `y`, or `./install.sh --skill-workshop-propose`. It changes one OpenClaw setting for this OpenClaw profile: your other agents get skill proposals instead of automatic weekly rewrites. |
| `the jobhunter agents must run with exec allowlist and ask off, but OpenClaw reports otherwise` | The installer read back what OpenClaw will really enforce and it was weaker than asked, so it stopped. The message names the agent and the setting. Most often a global setting in your own OpenClaw config wins (look with `openclaw config get tools.exec --json`); remove it and run `./install.sh` again. Details: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#installer). |
| `the jobhunter-qc test turn failed: the reply was not the numbers 1 to 700` | An older version's QC test (see the OpenClaw 2026.9.8 note above). Run `./jobhunter update`, then `./install.sh` again. |
| `agent identity check failed` | Check the Claude login (first row), then `./install.sh` again. More in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#claude-subscription-route). |
| `ERROR: dispatch paused; re-run ./install.sh` | The installer paused the running agent for the install and keeps it paused because the install stopped. Fix the error above it and run `./install.sh` again; the agent starts again at the end. |
| Nothing happens after `./jobhunter resume` | `./jobhunter status`: a pause, a breaker, active hours or a sleeping Mac. See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#nothing-happens). |

Every other message: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## What code enforces, and what relies on the model

This project does not trust the model with anything that matters. The guard plugin (inside OpenClaw) and the
local database enforce these rules physically; the model cannot talk its way around them:

- no duplicates: the same job is never applied to twice, the same person never gets a second cold message, and
  a company that was emailed is not cold emailed again within the cooldown (90 days by default);
- daily, weekly, hourly and per cycle limits, pacing gaps and active hours;
- pause and stop switches, and breakers that trip on CAPTCHA, verification, limit and bounce signals;
- nothing is sent without a QC approved draft and a one time send token, and no agent can approve anything;
- no site is used without your consent for that site (preflight, the send gate and the guard all check it);
- agents can only run the project's own commands and only touch files in their own work folders; every agent
  call proves which agent it is with two single-use proofs from the guard, checked by the program itself.

What still depends on the model, with checks around it: typing the approved text into Gmail, LinkedIn and job
application forms (the text is read back and compared with the approved text before the one allowed click, and
a sent email is confirmed by reading it back from the Sent folder), and reporting what a page showed (the guard
also scans every page for stop signs on its own). With the optional app password route, code sends the email
itself, byte for byte the approved text. See
[docs/ENFORCEMENT.md](docs/ENFORCEMENT.md) and [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md).

## Linux and Windows (WSL2)

The same steps work with a few differences: install Python and git with your package manager, keep a desktop
session open for the browser lanes, enable systemd and lingering so the Gateway keeps running, and log in by
hand inside the agent's window for each site you allow (copying Chrome logins is macOS only). Without a display, API discovery, evaluation, email and the
Sheet still run. See [docs/SETUP-linux-wsl.md](docs/SETUP-linux-wsl.md).

## Daily use

- **The Sheet** is your window: Start here, Dashboard, Approvals (you can decide there), Jobs, Skipped by
  filters, Applications, Outreach, Follow-ups (with what they said), QC log, Daily summary, Alerts, Limits and
  settings. It is a mirror; the local database is the source of truth.
- **Chat**: `/jh status`, `/jh inbox`, `/jh approve <code>`, `/jh answer Q3 <text>` for questions the agent
  needs you to answer (it never invents an answer to a form question).
- **Terminal**: `./jobhunter status`, `./jobhunter inbox`, `./jobhunter logs applier`. Answer a question from
  `inbox` (a form question too) with `./jobhunter profile answer --field <id> --value "<text>"`.
- **Digest**: a short summary twice a day in chat.

## Controls

| What | Command |
|---|---|
| Pause, resume, stop now | `./jobhunter pause`, `./jobhunter resume` (PIN), `./jobhunter stop-now` |
| Tighten a limit (any time) | `./jobhunter config lower gmail.ceilings.conservative.cold_day 10` |
| Loosen a limit (up to a hard maximum in code) | `./jobhunter config raise <path> <value>` (PIN) |
| Approval mode | `./jobhunter approval human`, `./jobhunter approval auto` (PIN, typed sentence, only after the reviewer passed its calibration set: run `./jobhunter qc golden` first) |
| Reset a breaker after a stop | `./jobhunter breaker reset <scope>` (PIN, not before its cooldown) |
| LinkedIn | `./jobhunter linkedin enable` (PIN, typed acknowledgement), `./jobhunter linkedin disable` |
| Sites the agent may use | `./jobhunter browser consent` (PIN, each site defaults to No), `./jobhunter browser forget <site>` or `--all` (PIN) |
| Email codes and site accounts | `./jobhunter browser consent` (PIN, default No), `./jobhunter accounts list`, `./jobhunter accounts forget <host>` (PIN) |
| Continue after you solved a CAPTCHA | `/jh continue <code>` in chat, or `./jobhunter continue <code>` (PIN); `./jobhunter captcha list` |
| Email finder (optional) | `./jobhunter enrich connect <provider>` (PIN), `./jobhunter enrich disconnect <provider>` or `--all` (PIN) |
| Health | `./jobhunter doctor`, `./jobhunter doctor --probe` (also runs the agent identity checks; uses the model) |
| Everything else | `./jobhunter help`; any other command a message names (`./jobhunter eval requeue --since-profile-change`, `./jobhunter answers add ...`, `./jobhunter companies split ...`) works the same way and asks for the PIN when it needs the owner |

Every limit and its hard maximum is listed in [docs/CONFIG-REFERENCE.md](docs/CONFIG-REFERENCE.md). The config
file can only make things stricter; anything looser needs the PIN.

## Email codes, site accounts and CAPTCHAs

Some company career sites (Workday and other applicant tracking systems) ask for an account, send a
verification code or a sign-in link by email, or show a CAPTCHA. By default every such job simply goes to you.
Two permissions, both off until you say yes for a site, let the agent do more:

- **Email codes** (`email_codes`): the agent may read a verification code or sign-in link that this site sends
  to your Gmail. It reads only a message that arrives within 10 minutes after the agent itself asked the site
  for it, only from that site's own sender addresses, uses it once, and never shows it to anyone. The code is
  typed into the page by the program; the AI model never sees it. Needs Gmail allowed (default email route) or
  `./jobhunter mail connect` (app password route); otherwise the Sheet shows it as "Unavailable".
- **Site accounts** (`ats_accounts`): the agent may create an account on this site with your sender address,
  only for a job you approved. The program makes a strong random password and keeps it only in your macOS
  Keychain (Linux and WSL: a private file); the model never sees it. One account per company site, reused next
  time. Only a standard candidate privacy or terms box is ticked; anything unusual (job alerts, marketing,
  background checks, paid services) stops and the job comes to you.

```bash
./jobhunter browser consent            # PIN; asks both questions for each job site, default No
./jobhunter browser forget workday     # PIN; takes both back for that site
./jobhunter accounts list              # the site accounts the agent created
./jobhunter accounts forget <host>     # PIN; deletes the stored password (delete the account on the site yourself)
```

Workday jobs go to the agent only when you also run `./jobhunter config raise boards.sites.workday.apply
browser` (PIN); the consent step offers it.

**CAPTCHAs are always solved by you.** The program never solves or works around one. When an application form
shows a CAPTCHA, that job pauses and you get a chat message with a screenshot and a 4 character code (also in
`./jobhunter status` and the Sheet's Alerts tab). Solve it in the agent's browser window, then send
`/jh continue <code>` in chat or run `./jobhunter continue <code>` (PIN). If nobody solves it within 2 hours the
job is skipped. `./jobhunter captcha list` shows the open ones.

## Optional: find work email addresses with your own free API keys

Off by default. When you turn it on, the agent can look up the work email of a person on the hiring team of a
job you are going for, with free keys from accounts **you** create (Prospeo, Hunter, Tomba, GetProspect and
ZeroBounce for checking). It only looks up people already on a target job's hiring team, never scrapes
LinkedIn, never probes mail servers, stops before any free tier runs out, and never looks the same person up
twice. Never use a key you found online: it belongs to someone else.

```bash
./jobhunter enrich connect hunter      # PIN, then type the key (hidden); stored in the macOS Keychain
./jobhunter enrich budget              # credits used and left per provider
./jobhunter enrich disconnect --all    # remove every key (PIN)
```

Then set `"enrich": {"enabled": true}` in `private/config.json`. Details, risks and what is sent to the
providers: [docs/EMAIL-FINDER.md](docs/EMAIL-FINDER.md).

## Safety, privacy, uninstalling

- **Safety model:** [docs/SAFETY-AND-TOS.md](docs/SAFETY-AND-TOS.md), [docs/ENFORCEMENT.md](docs/ENFORCEMENT.md).
- **Privacy:** everything personal stays on your computer in the gitignored `private/`, `state/`, `logs/` and
  `exports/` folders, the agent's own browser profile (only the cookies of the sites you allowed), plus the
  Google Sheet you own. See [docs/PRIVACY.md](docs/PRIVACY.md).
- **Writing rules:** no en or em dashes, no AI filler, facts only from your resume and answers:
  [docs/WRITING-RULES.md](docs/WRITING-RULES.md).
- **Updating:** `./jobhunter update`, see [Updating](#updating).
- **Uninstalling:** `./uninstall.sh` removes the automations, the plugin, the agents and their config, and
  keeps your data. `./uninstall.sh --purge` also deletes local data after typed confirmations.
- **Problems:** [If something goes wrong](#if-something-goes-wrong) and
  [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## FAQ

**Will it apply to hundreds of jobs a day?** No. It applies to a handful of well matched jobs a day and writes
a few personal emails. The limits are compiled into the code.

**Can I let it send without approving each message?** Yes, after a warm up: `./jobhunter qc golden` (the
reviewer's calibration), then `./jobhunter approval auto`. It needs your PIN, a typed sentence, and a
calibration of at least 18 of 20 within the last 14 days. Salary questions, referral asks,
sensitive form fields and replies to positive answers always wait for you.

**What if a job form asks something the agent does not know?** It stops for that job and asks you. The answer is
saved for next time.

**Does it read my email?** Only after you allow Gmail, and only what it needs: your Sent folder to avoid
duplicates and to confirm its own sends, and replies and bounces to messages it sent. See
[docs/PRIVACY.md](docs/PRIVACY.md).

**Do I have to give it my passwords?** No. It uses the logins already in your Chrome, for the sites you allow,
or you log in by hand inside its window. The only secrets you set are your local owner PIN and, if you choose
those options, a Google app password or email finder keys.

**Can I run it for two people on one computer?** Use two clones with two OpenClaw profiles
(`./install.sh --profile <name>` in the second clone). One clone is one install.

## Contributing

Read [docs/DEVELOPING.md](docs/DEVELOPING.md): how the code is split, how to run the tests, and the repo rules
(Python standard library only, ASCII only, no dashes in anything the agent may send, fictional data only).
Security issues: [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
