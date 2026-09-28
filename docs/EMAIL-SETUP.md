# Email setup

The job hunter sends email from your own Gmail address, in one of two ways. The default needs no password at
all. You can change the route later in `private/config.json`, key `gmail.route`.

| Route | Who clicks Send | What you give it |
|---|---|---|
| `web_ui` (default) | The outreach or applier agent, in Gmail in the separate `jobhunter` browser profile, under a gate token | Your consent, once, to copy your Gmail login from your own Chrome. No password. |
| `app_password` (optional) | Code, over SMTP. The agents never see Gmail. | A Google app password (16 letters) that you create and paste once |

On both routes the text that goes out is the text you (or QC in auto mode) approved, the same ceilings, send
windows and stop rules apply, and nothing is sent twice.

What you always need, whatever the route:

* the owner PIN you choose at `./jobhunter init`. It is local, not an account password: it stops the agents
  from approving their own drafts or raising their own limits;
* `owner.gmail_address`, `owner.first_name`, `owner.last_name` and `owner.signature` in
  `private/config.json` (the wizard fills them). The signature block is added by code to every email.

## Route `web_ui` (default): use the Gmail login you already have

### 1. Say yes to Gmail in the consent step

`./jobhunter init` (and `./install.sh`, which runs it) explains the cookie copy in plain words and then asks
you, site by site, whether the agent may use it. Every answer defaults to No. For email:

1. Pick the Chrome profile you use Gmail in. The list shows your Chrome profiles by name; a work profile is
   never picked for you.
2. Answer yes for Gmail.
3. The login cookies of the Google sites only (the ones Gmail needs) are copied from that Chrome profile into
   the separate `jobhunter` browser profile the agents use. Your normal Chrome is never driven, nothing is sent
   anywhere else, and no password is read or stored. `docs/PRIVACY.md` lists what is copied and where it lives.
4. The wizard then opens Gmail in the `jobhunter` profile, read only, and checks that the account shown is
   `owner.gmail_address`. A different account, a sign-in page or a "Verify it's you" prompt stops the step and
   asks you what to do.

Your answer is recorded in `private/consent.json` (mode 600). Agents cannot write that file, and code refuses
every Gmail step without an active consent row, so an agent cannot give itself access. To answer later or change
your answer: `./jobhunter browser consent gmail` (it asks for your PIN). To see what the agent may use:
`./jobhunter browser sites`.

No cookie import (Linux, WSL, or you prefer not to): run `./jobhunter browser login gmail`. A browser window
opens in the `jobhunter` profile; sign in to Gmail yourself. The agents never type a password and never sign
in.

### 2. Turn the Gmail signature off for new emails

In Gmail (in any browser): Settings (gear), See all settings, General, Signature, "Signature defaults", For new
emails use: "No signature", and the same for replies and forwards. Save. You can keep your signature defined
and insert it by hand in your own mails. Code adds your signature to the approved text; a Gmail signature would
make every read-back differ from the approved text, and then nothing is sent.

### 3. Check

```bash
./jobhunter browser check gmail     # read only: Gmail opens in the agent's profile, as owner.gmail_address
./jobhunter doctor
```

`./jobhunter mail test` answers `NOTHING_TO_DO` on this route: there is no SMTP or IMAP login to test.

### 4. Record what you already sent (recommended)

```bash
./jobhunter mail import-history --days 365
```

asks for your PIN and queues a read of your Gmail Sent folder: during its next replies cycles the outreach agent
reads the Sent folder month by month in the browser and code records every address you wrote to as an
`imported` action. From then on the person rules (never a second cold email to the same person) and the company
cooldown apply to people you contacted by hand. Messages to no-reply addresses and to yourself are skipped.
You can also list people and companies in `private/exclusions.csv` (see `private.example/`).

### What the agents do in Gmail

Each email follows the same gate as a LinkedIn message or a job form (skill `jobhunter-gmail-web`):

1. **Precheck.** Gmail searches of Sent, Outbox and Scheduled for the address, your other known addresses of
   the person and the company's names and domains; for a follow-up, whether they already wrote back. Any hit
   means the email is not sent and the earlier message is recorded instead.
2. **Reserve.** Code checks the ceilings, pacing gap, send windows, exclusions and the approval, and hands out a
   token. Without a live token the guard refuses every typing action and every click on Send.
3. **Type and read back.** The agent types the recipient, the subject and the body slowly, key by key, then
   reads the compose window back: the subject, the body and the recipients (the To, Cc and Bcc fields). Code
   compares the subject and body with the hash of the approved text and checks the recipients: exactly one To
   address, equal to the address reserved for this email, and no Cc or Bcc. Any difference fails the token and
   nothing is sent.
4. **One click on Send**, after a short reading pause. Never twice, never Undo, never Schedule send.
5. **Confirm.** The "Message sent" notice and the message found in your Sent folder, whose text and recipients
   the agent reads back. Code runs the same checks on that Sent copy as on the compose window: the hash of the
   approved text, and exactly one To address equal to the reserved one, with no Cc or Bcc. An email counts as
   sent only with this Sent read-back; code refuses to confirm without it. A Sent copy that differs in text or
   recipients is recorded as `unknown` and gives you a task to check what went out. Anything less than both
   proofs is recorded as `unknown` too. `unknown` blocks that person like a sent email until later Sent, Outbox
   and Scheduled searches (15 minutes and 24 hours later) settle it. Nothing is ever sent again "to make sure".
6. **Replies and delivery failures.** The replies lane searches your inbox for answers in our conversations and
   for delivery failure notices. It reads them and records a class (positive, neutral, out of office, bounce,
   ...); it never answers, forwards, archives, labels or deletes anything, and never clicks links in mail.
7. **Daily Sent audit.** Once a day the replies lane reads the last two days of your Sent folder. A message to
   a person or company the job hunter knows, with no ledger entry, stops everything until you look. Mail to
   anyone else is not examined, and mail you send yourself in a conversation you took over is fine.

The Gmail limits (`gmail.*` keys in `docs/CONFIG-REFERENCE.md`) are the same on both routes.

### When the session expires or Google asks you to verify

A Google sign-in page, "Verify it's you", a code or CAPTCHA prompt, "unusual activity" or a sending limit
notice stops Gmail at once: the `gmail` breaker opens, the agents end their cycle, and you get a chat message
with the one command that renews the session. The agents never retry, never sign in and never try to get
around the prompt. What you do:

1. Open Gmail in your own Chrome and deal with any Google prompt there yourself.
2. Renew the agent's copy of the login: run the command from the message (for example
   `./jobhunter browser import`, or `./jobhunter browser login gmail` without cookie import).
3. `./jobhunter browser check gmail`, then `./jobhunter breaker reset gmail`.

To take the permission back: `./jobhunter browser forget gmail` deletes the Google cookies from the agent's
profile and marks the consent revoked; the agents then stop using Gmail. To stop sending for a while without
revoking anything: `./jobhunter pause gmail` (or `/jh pause gmail` in your chat).

## Route `app_password` (optional)

Choose it when you prefer that code, not the model, clicks Send: code builds the message from the approved text
and sends it over SMTP, reads over IMAP, and never shows the text to a browser. It needs a Google account with
2-Step Verification that offers app passwords (not available with Advanced Protection, with security keys only,
or when a Workspace admin turned them off). Nothing on the `web_ui` route asks for it.

### 1. Turn on 2-Step Verification

1. Open https://myaccount.google.com/security and sign in.
2. Under "How you sign in to Google", open "2-Step Verification".
3. Follow the steps (a phone prompt or an authenticator app is fine) until it says "On".

### 2. Create an app password

1. Open https://myaccount.google.com/apppasswords (Google may ask for your password again).
2. Type a name you will recognise later, for example `job hunter`, and click Create.
3. Google shows a 16-letter password in four groups of four letters. Copy it. Google shows it only once; if
   you lose it, delete it on the same page and create a new one.

An app password can do everything your Google password can do for mail, so treat it like a password: do not
paste it into a chat, a note or an issue.

### 3. Connect and switch the route

```bash
./jobhunter mail connect
```

The command asks for your owner PIN, then for the app password with the screen echo off (spaces are fine).
It logs in to `smtp.gmail.com` (port 465, SSL) and `imap.gmail.com` (port 993, SSL), reads nothing but the
login result and the folder list, and only then stores the password:

* on macOS in your login Keychain (item `openclaw-job-hunter.<install id>`, account = your Gmail address);
  the password is handed to the `security` tool on its standard input, never on a command line;
* elsewhere, or with `./jobhunter mail connect --store file`, in `private/secrets.json` (mode 600, never
  committed; `.gitignore` excludes `private/`).

Nothing is stored when a login fails. The password is never printed, logged, passed to an agent or put in the
Sheet, and no agent can read `private/`.

Then set `"gmail": {"route": "app_password", ...}` in `private/config.json`. If you connect but keep `web_ui`,
the agents keep sending in the browser and code uses the connection only, read only, to double-check unknown
sends and to read the Sent folder for the audit.

### 4. Check

```bash
./jobhunter mail test
```

prints `smtp_ok: true` and `imap_ok: true`. `./jobhunter doctor` runs the same check.

Gmail needs IMAP. Personal accounts have it on. If `mail test` says the IMAP login failed although the app
password is right, open Gmail, Settings (gear), See all settings, "Forwarding and POP/IMAP", and turn IMAP on
if the page offers that switch. If it says there is no All Mail folder, open the "Labels" tab of the same
settings page and tick "Show in IMAP" for All Mail.

### 5. Import what you already sent (recommended)

```bash
./jobhunter mail import-history --days 365
```

reads the Sent folder of the last 365 days over IMAP at once and records every address you wrote to as an
`imported` action, as on the `web_ui` route. Messages to more than 10 recipients, no-reply addresses and your
own address are skipped. The records stay in your local database.

### What code sends on this route

The mailer runs every 5 minutes (`jobhunter:mailer` automation, no model involved) and sends at most one email
per run, only inside your send windows, only after the pacing gap since the last email, and only while every
ceiling has room. Before each email it runs the same precheck as above over IMAP.

Each message has:

* `From:` your name and `owner.gmail_address`; `To:` exactly one address; no Cc, no Bcc;
* `Subject:` the approved subject (follow-ups keep `Re: <original subject>` and carry `In-Reply-To` and
  `References`, so they land in the same conversation);
* a plain text body: the approved text followed by your signature block. No HTML, no tracking pixel, no
  added links, no unsubscribe footer you did not approve;
* `Message-ID: <T...@jobhunter.invalid>`, set by code. If a connection breaks while the message is being
  sent, the mailer later searches your mail for this id instead of sending again;
* for an application email only, the approved resume PDF (named `<First>_<Last>_Resume.pdf`); its sha256 is
  part of the approved text, so a changed file is never sent.

### What code reads on this route

The IMAP session opens All Mail read-only (EXAMINE, and message bodies are fetched with BODY.PEEK), so nothing
is marked read, moved, labelled or deleted. Besides the prechecks it looks, at most every 30 minutes
(`gmail.fetch_every_minutes`), for replies from the people and company domains you wrote to (out-of-office
notes and automatic acknowledgements are classified by code, any other reply becomes a packet of at most 2,000
characters for the replies lane), delivery failures of messages the mailer sent, application confirmation
emails, and Google or LinkedIn security emails (these stop the matching channel). Nightly, `housekeeping`
compares the Sent folder of the last two days with the ledger, as the browser audit does on `web_ui`.

### When Gmail says no

| What you see | What it means | What to do |
|---|---|---|
| The `gmail` breaker is open with `gmail_auth_failed` | Gmail refused the app password (`535 5.7.8`), or IMAP said `AUTHENTICATIONFAILED`, or a `5.7.x` policy block | Create a new app password, `./jobhunter mail connect`, then `./jobhunter breaker reset gmail` |
| `gmail_sending_limit` | Gmail's daily or rate limit (`5.4.5`, `421`, `4.7.x`) | Nothing: it reopens after at least 24 hours and restarts at half the daily cap |
| `gmail_security` | A Google security email arrived | Check your account at https://myaccount.google.com/security, then reset the breaker |
| `E_MAIL_TRANSPORT` in the mailer log | The network or Gmail was unreachable | Nothing; the next run retries. Nothing was sent twice |
| An email draft marked expired with "refused the recipient" | Gmail rejected the address | Check the address; write a new draft if it was a typo |

Changing your Google password revokes every app password. Connect again after a password change. To remove
the connection: delete the app password at https://myaccount.google.com/apppasswords and set `gmail.route`
back to `web_ui`. `./uninstall.sh --purge` deletes `private/` (with `secrets.json`); a Keychain item stays
until you delete it in Keychain Access (search for `openclaw-job-hunter`).
