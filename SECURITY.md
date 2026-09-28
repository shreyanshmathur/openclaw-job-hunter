# Security

## Reporting a problem

Please report security problems privately, not in a public issue: use GitHub's "Report a vulnerability"
button on the repository's Security tab. Include the OpenClaw version (`openclaw --version`), the commit of this
repo, what you did, and what happened. Never include personal data, tokens, app passwords, sheet URLs or
secrets in a report; replace them with placeholders.

We aim to answer within a week. Fixes for problems that let an agent send, approve or apply without the owner,
bypass a limit or a breaker, or read personal files come first.

## What the agents can and cannot touch

The five `jobhunter-*` agents are fenced by the `jobhunter-guard` OpenClaw plugin and by `scripts/jh.py`:

| Agents can | Agents cannot |
|---|---|
| run `scripts/jh.py` commands listed for them in `scripts/jobhunter/acl.json`, with arguments of a fixed shape | run any other program, shell syntax, `python3 -c`, pipes or redirects |
| read files in their own workspace, write in its `work/` and `inbox/` folders | read `private/`, the database, your home folder, or another agent's workspace |
| browse in the `jobhunter` browser profile | open Google Sheets, Drive, Apps Script or account settings pages; use another browser profile |
| type into a form or compose window while they hold a send token | type anything, click Send, Submit, Connect or Apply without a reserved token and a matching read back |
| draft messages | approve, skip or edit a draft; change limits, the approval mode, LinkedIn or breakers; unpause |
| report what a page showed | hide a stop page: the guard scans every page itself and trips the breaker |

Human-only commands need the owner PIN at the terminal or arrive as `/jh` commands from the owner's own chat,
which the guard signs. The QC reviewer agent has no tools at all.

## Secrets

- The Gmail app password is kept in your macOS login Keychain (on Linux and WSL, or with `./jobhunter mail
  connect --store file`, in `private/secrets.json`). The Sheet URL and secret, the guard key and the PIN hash live
  in `private/` with mode 600. None of them is passed on a command line, put in an automation, or shown to a
  model.
- The Anthropic API key (if you use one) is read at a hidden prompt (or from the environment) and reaches
  `openclaw onboard` through that command's environment, not its command line; OpenClaw stores it, never this
  folder. Only if an OpenClaw version refuses the environment variable does the installer retry once with the
  key as an argument, and it says so.
- `tools/leakcheck.py` and `tools/check_gitignore.py` run in CI and in the pre-commit hook to keep secrets and
  personal files out of git. The hook scans the staged snapshot (what the commit records), so fix a finding and
  `git add` the file again before committing.

## Known limits of the design

- On LinkedIn and application forms the model types the approved text into the page. The text is read back and
  compared before the only allowed click, but the model still operates the browser.
- The guard runs inside the OpenClaw Gateway. Anyone with shell access to your user account can change your
  OpenClaw config, this repo and the database; protect the computer itself.
