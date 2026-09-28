# Changelog

All notable changes to this project. Versions follow the date of the release; the version number lives in
`scripts/jobhunter/__init__.py` and in the guard plugin's `package.json`.

## 2.0.0 (unreleased)

First public version.

- Five OpenClaw agents (scout, evaluator, applier, outreach, qc) coupled only through a local SQLite ledger,
  started by a code-owned dispatcher at random times inside active hours.
- The `jobhunter-guard` OpenClaw plugin: exec allowlist with argument checks, file confinement, browser typing
  and clicks only under a live send token, stop-page detection, `/jh` chat commands for the owner.
- Duplicate protection in code: one application per job, one cold touch per person, company cooldowns,
  enforced by unique indexes and triggers.
- Researched, conservative limits for Gmail, LinkedIn and job boards, compiled hard maximums, warm up, pacing,
  breakers that only the owner can reset.
- Two-stage QC for every outbound text: a deterministic linter (no dashes, no filler, facts traceable to the
  profile) and an independent reviewer agent.
- Email from your own Gmail in the agent's browser profile by default (no password needed): the compose fields
  are read back and compared with the approved text before the one allowed click, and the send is confirmed in
  the Sent folder. Sending by code over Gmail SMTP with an app password stays available as an option.
- Your existing Chrome logins, with consent per site: `./jobhunter browser consent` explains what is copied,
  asks about each site (default No), lets you pick the Chrome profile by name, records the answers with your PIN
  in `private/consent.json`, then copies only those sites' cookies into the agent's own profile and checks the
  logins read only. `./jobhunter browser forget <site>|--all` takes a site back. Preflight, the send gate and
  the guard refuse any site without consent.
- An optional email finder with your own free-tier keys (off by default; `./jobhunter enrich connect`), limited to
  people on the hiring team of a targeted job, with budgets that stop before a free tier runs out.
- Truthful resume tailoring with PDF and DOCX output and an answer bank that never invents answers.
- A Google Sheet mirror through a bound Apps Script web app, with readable tabs, colors and a dashboard.
- `install.sh`, `uninstall.sh` and the `./jobhunter` control wrapper; every automation is created disabled.
- LinkedIn writes off by default; enabling them needs the owner PIN and a typed acknowledgement.
