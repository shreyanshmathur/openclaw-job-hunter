# Privacy

## Where your data lives

Everything personal stays on your computer, in folders git ignores, plus the Google Sheet you own.

| Folder or place | What is in it |
|---|---|
| `private/` (mode 700, files 600) | your config, confirmed profile, answer bank, resume copies and resume variants, exclusions list, owner PIN hash, your per-site browser consent (`consent.json`, including the email code and site account permissions), site account passwords (`ats_accounts.json`, only on Linux and WSL), the Gmail app password (`secrets.json`, only on the optional app password route), email finder keys (`enrich_keys.json`, only on Linux and WSL or with `--store file`), the Sheet URL and secret, the guard key, `home.json` |
| `state/` | the database (jobs, contacts, drafts, actions, replies), backups (14 days), install manifest, QC packets while a review runs |
| `logs/` | command and event logs (kept 30 days) |
| `exports/` | CSV exports you ask for |
| `~/.openclaw-job-hunter/<install id>/workspaces/` | the agents' work files (deleted after 7 days) |
| OpenClaw's own folders | agent sessions and transcripts, the `jobhunter` browser profile (the cookies of the sites you allowed: copied from your Chrome, or from logging in by hand inside it) |
| macOS login Keychain | the Gmail app password (optional route), email finder keys and site account passwords, if you connected or allowed any |
| Your Google Sheet | a readable copy of what the agent did |

Nothing is uploaded anywhere else by this project. The model provider (Anthropic, through your Claude login
or API key) receives the text of each agent turn: job posts, your profile facts, research notes and drafts. If
you turn on the optional email finder, the providers you connected receive a person's name and company domain
for each lookup (see below).

## Your Chrome logins: what is copied, where it lives, how to take it back

The agents never use your normal Chrome. They browse in OpenClaw's separate `jobhunter` browser profile. A site's
login gets there only after you allowed that site in `./jobhunter browser consent` (every site starts at No):

- **What is copied:** only the cookies of the domains of the sites you allowed (for example `naukri.com` for
  Naukri), from the one Chrome profile you picked by name. The copy is OpenClaw's own
  `openclaw browser import-profile --domains <those domains>`, run on your computer. No password, no history, no
  bookmarks and no other site's cookies are copied. On Linux and WSL nothing is copied: you log in by hand inside
  the agent's window.
- **Gmail means your whole Google account session:** Google signs you in on `google.com`, not on Gmail alone. So
  allowing Gmail copies your Google account session cookies (the domains `google.com`, `mail.google.com` and
  `accounts.google.com`) into the agent's `jobhunter` profile; logging in to Gmail by hand there creates the same
  session. With those cookies a browser could open any Google service signed in as you (Drive, Docs, Sheets,
  Photos, account settings, payments), not only Gmail. The guard therefore lets the agents use only
  `mail.google.com`: every other `google.com` page (Drive, Docs, Sheets, Apps Script, account, password and
  payment pages, and the rest) and Gmail's own settings pages are blocked for every agent, whatever it is asked
  to do. The answer still defaults to No; say Yes only if you accept this. To take it back, see below.
- **Reading your Chrome profiles:** to list them by name, the tool reads only Chrome's `Local State` file (the
  profile names, and whether a profile belongs to a work or school account). A work profile is never picked
  for you.
- **Where it lives:** in the `jobhunter` profile inside OpenClaw's folders on this computer. Your answers live in
  `private/consent.json` (site, Chrome profile, method, when you allowed it, and when you took it back). Nothing
  is sent anywhere else.
- **Who can change it:** only you. Allowing a site needs your owner PIN; the agents cannot run these commands,
  and preflight, the send gate and the guard refuse every site without an active consent.
- **How to take it back:** `./jobhunter browser forget <site>` (or `--all`, PIN) marks the consent revoked and
  clears the cookies of the `jobhunter` profile. OpenClaw clears cookies per profile, not per site, so the
  sites you still allow are copied again right after (or you log in to them again by hand). `./uninstall.sh`
  can delete the whole `jobhunter` profile. You can also sign the agent's session out from the site itself (for
  example Google's "Your devices" page).
- **Taking Gmail back:** run `./jobhunter browser forget gmail` (PIN). The consent is marked revoked at once, so
  preflight, the send gate and the guard refuse Gmail from then on, and every Google cookie is cleared from the
  `jobhunter` profile. To end the session on Google's side as well, sign it out on Google's "Your devices"
  page (https://myaccount.google.com/device-activity) or change your Google password. A copied session is the
  same session your own Chrome uses, so this signs your Chrome out of Google too; a session made by logging in
  by hand inside the `jobhunter` window is separate and can be signed out alone. On the default `web_ui` route
  no email is sent while Gmail is not allowed (the optional app password route is in `docs/EMAIL-SETUP.md`).

## Email codes, site accounts and CAPTCHA screenshots

These are off for every site until you allow them with `./jobhunter browser consent`.

**What is read in your mailbox.** Only when the agent has just asked a site for a code or sign-in link, and only
a message that arrives after that request and within 10 minutes, sent from that site's reviewed sender
domains (a fixed list in the code) to your own address. Mail from Google, LinkedIn, Microsoft, Apple,
Facebook, PayPal and banks is never used, whatever it says. The message must hold exactly one code of the
expected shape, or one sign-in link to the site's own pages. Each code is used once. Nothing of the message is
stored except a keyed hash of the code or link (HMAC-SHA256 with a random salt made on your computer), the
sender domain and the times. After use the message is marked read (`otp.after_use`: `leave`, `mark_read` or
`archive`). The code is typed into the page by the program; it is never sent to the AI model, the Sheet or
your chat.

**Where site passwords live.** On macOS, only in your login Keychain: service
`openclaw-job-hunter.<install id>.ats`, account `<host>|<email>`. On Linux and WSL, in
`private/ats_accounts.json` (mode 600). The password is made by the program (20 random characters by default)
and typed into the page by the program; the model never sees it. `./jobhunter doctor` reports whether the
store is available but never reads a password.

**What `./jobhunter accounts forget <host>` does.** It deletes the stored password and the local record. The
account on the site itself still exists: sign in there and delete it if you want it gone.

**CAPTCHA screenshots.** When a form shows a CAPTCHA, a screenshot of that page goes only to your own chat. It
is kept in `state/captcha/` (mode 600) and deleted after 2 days (`captcha.screenshot_retention_days`).

**The browser control port.** The program types codes and passwords through the agent browser's local control
port (loopback only, recorded as `browser_cdp` in `private/home.json`). Any program running as your own macOS
user can reach that port; this is the same boundary as the OpenClaw browser itself.

## Email finder (optional, off by default)

When you connect your own free keys and turn it on (`docs/EMAIL-FINDER.md`):

- **What is sent:** for a person already on the hiring team of a job you target, their first and last name and
  the company's web domain (a LinkedIn address only if you already have it), to one provider at a time, and the
  found address to a verifier when a check is needed. Never phone numbers, never your data.
- **What is stored:** the address, its grade, which provider found it, the credits used, and a hash of the
  lookup so the same person is never looked up twice. Keys stay in the macOS Keychain or in
  `private/enrich_keys.json` (mode 600); nothing prints a key.
- **How long:** unused results are cleared on a schedule (the retention in `docs/EMAIL-FINDER.md`); a bounce or an
  opt-out clears the address at once. `./jobhunter forget --email <address>` removes it with everything else
  about that person.

## What the agents may read

- Public job posts and company pages; public pages about the people it plans to contact (company site, public
  posts); logged-in job boards you turned on; LinkedIn only if you turned it on.
- Gmail only if you allowed it. On the default route (`web_ui`) the agent reads, in its own browser profile,
  your Sent folder (to prevent duplicates and to confirm its own sends) and the replies and bounces to messages
  it sent. On the optional app password route, code (not the model) does the same over IMAP, and the model sees
  a reply only as a short packet to classify.
- Agents cannot open any Google service other than `mail.google.com`: Sheets, Drive, Docs, Apps Script,
  account settings and every other `google.com` page, and Gmail's settings pages, are blocked by the guard.

## Removing someone

`./jobhunter forget --email person@example.com` (PIN) deletes what is stored about that person, keeps only a
hashed marker so they are never contacted again, and removes their rows from the Sheet.

## The repo stays clean

`.gitignore` excludes every personal folder, and three checks run in CI and before every commit:
`tools/check_gitignore.py` (no personal or runtime files tracked), `tools/leakcheck.py` (email addresses, phone
numbers, sheet ids, tokens, and a private denylist built from your own `private/` files), and
`tools/textcheck.py`. If you fork the repo, install the hook with `tools/install_hooks.sh`.

## Deleting everything

`./uninstall.sh --purge` removes the automations, agents, plugin and config entries, then deletes `state/`,
`logs/` and `exports/`, and with a second confirmation `private/` together with the app password item in the
macOS Keychain (service `openclaw-job-hunter.<install id>`). It asks before removing the email finder keys
(`./jobhunter enrich disconnect --all`) and before deleting the `jobhunter` browser profile with its copied
cookies. Delete the Google Sheet yourself, and revoke the app password at
https://myaccount.google.com/apppasswords if you used that route.
