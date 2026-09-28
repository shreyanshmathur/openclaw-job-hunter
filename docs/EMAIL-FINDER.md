# Email finder (optional, off by default)

The email finder looks up the work email address of a person the outreach agent has already chosen to
contact: someone named on the hiring team of a job you are eligible for or applied to (a hiring manager,
a recruiter or a founder), who passes every exclusion, cooldown and duplicate rule. It uses API keys from
your own free accounts at a few email finder services, grades what comes back, and stores only addresses
that are safe to send to. Everything after that (drafting, QC, your approval, the Gmail send limits) works
exactly as it does for any other address.

It is off until you turn it on, and it does nothing until you connect at least one key of your own.

What it never does:

- It never scrapes LinkedIn and never uses browser extensions that read LinkedIn pages.
- It never uses API keys found online (in GitHub repos, gists, extension bundles, paste sites or "free API
  key" lists). Those keys belong to other people; using them is using someone else's account without
  permission. The code has no way to search for, accept, share or rotate such keys.
- It never probes mail servers (no SMTP `RCPT TO` checks) and never runs OSINT harvesters.
- It never asks for phone numbers and never stores them; personal (non-work) addresses are rejected.
- It has no "look up anyone" command. Only people already selected for outreach can be looked up, and each
  person is looked up at most once (one extra retry needs your PIN).

## 1. Honest risks

- Each lookup shares one person's name and their employer's domain with one or two of the services you
  connected. At most two finders and one verifier ever see a given person.
- Finder data can be wrong or out of date. A bounced email hurts your Gmail sender reputation, so the finder
  stops itself after bounces (section 8), and addresses older than 180 days are never sent to.
- Each service has terms you accept when you sign up: one account per person, no shared keys, and (for
  Apollo) business use and a work email at signup. Read them before you connect a key.
- You are responsible for using contact data lawfully where you live. This is not legal advice.

## 2. Getting keys (your own accounts only)

Sign up on each service's own site with your own email address, one account per service. Then copy the API
key from your account settings. No card is needed except at Anymail Finder (card check at signup), and
ZeroBounce asks for a business or premium email domain at signup.

| Service | Role | Free tier (as of 2026-09) | Pricing page |
|---|---|---|---|
| Prospeo | finder 1 | 100 credits per month | https://help.prospeo.io/en/article/plans-and-pricing-overview-cdloq9/ |
| Hunter | finder 2, verifier 2 | 50 credits per month (find 1, verify 0.5) | https://hunter.io/pricing |
| Tomba | finder 3 | 25 searches per month, 5 requests per day | https://tomba.io/pricing |
| GetProspect | finder 4 | 50 valid emails per month | https://getprospect.com/pricing |
| ZeroBounce | verifier 1 | 100 validations per month | https://www.zerobounce.net/email-validation-pricing |
| Anymail Finder | reserve (one-time trial) | 100 credits once | https://anymailfinder.com/pricing |
| Findymail | reserve (one-time trial) | 10 credits once | https://www.findymail.com/pricing/ |
| Apollo | optional, off | about 75 credits per month | https://www.apollo.io/pricing |

Never use a key you found online: it is someone else's account, it may be revoked at any moment, and using
it breaks the service's terms and possibly the law.

## 3. Connect a key

```
./jobhunter enrich connect prospeo
./jobhunter enrich connect hunter
./jobhunter enrich connect tomba          # asks for the key and the secret
./jobhunter enrich connect getprospect
./jobhunter enrich connect zerobounce
```

`connect` needs your PIN and a terminal. It asks you to confirm that the key is from your own account, then
reads the key with hidden input. It makes no network call.

Where the key is stored:

- macOS: your login Keychain, items named `openclaw-job-hunter.enrich.<service>.<field>`. The key is passed
  to the `security` tool on its standard input, never on a command line. If the Keychain refuses that,
  `security` asks you for the key directly.
- Linux and WSL (or `"key_store": "file"`): `private/enrich_keys.json`, readable only by you (mode 600).
  A key file with any other mode or owner, or a symlink, is refused.

Keys are never read from environment variables, command-line arguments, `config.json` or the database, and
they never appear in output, logs, the Sheet or the agent's context.

To remove keys: `./jobhunter enrich disconnect hunter` or `./jobhunter enrich disconnect --all`
(`uninstall.sh` offers this too, because Keychain items outlive the repo folder).

`./jobhunter enrich test <service>` reports whether a key is stored. None of the services documents a free
test call, so it spends nothing; the first real lookup shows whether the key works.

## 4. Turn it on

In `private/config.json`:

```json
"enrich": {"enabled": true}
```

Defaults (all can be lowered in the file; raising needs `./jobhunter config raise <key> <value>` with your
PIN, and never above the free-tier numbers):

| Key | Default | Meaning |
|---|---|---|
| `enrich.chain` | prospeo, hunter, tomba, getprospect | finder order |
| `enrich.verifiers` | zerobounce, hunter | verifier order |
| `enrich.max_finders_per_person` | 2 | services that ever see one person's name |
| `enrich.max_lookups_per_day` | 8 | new people per rolling 24 hours |
| `enrich.max_lookups_per_cycle` | 2 | per outreach cycle (the agent's running cycle; with none running the agent's lookup is refused) |
| `enrich.max_unsent_found` | 10 | found addresses not yet used |
| `enrich.min_confidence` | 90 | score a finder needs for grade B |
| `enrich.use_linkedin_identifier` | false | pass a stored LinkedIn vanity handle when the name is incomplete |
| `enrich.retention_days` | 90 | unused provider data is deleted after this |
| `enrich.max_result_age_days` | 180 | older results are never sent to |
| `enrich.max_share_of_cold_sends` | 0.5 | share of cold emails (7 days) that may use found addresses |
| `enrich.providers.<service>.budget_31d` | see table in section 2 | credits per rolling 31 days |

Budgets use rolling windows (31 days, 24 hours, and lifetime for the one-time trials), so no reset day has
to be configured and a free tier cannot be overspent. Every credit is reserved before a call and settled
after it; a call whose result is unknown counts as spent.

The reserve services (Anymail Finder, Findymail) run only when you ask:
`./jobhunter enrich find --contact <P...> --include-reserve` (PIN). Their budgets start at 0; raise them
with `config raise` first.

## 5. What is sent, what is stored, for how long

Sent to a service: first name, last name (or full name) and the company's domain; the LinkedIn vanity
handle only when you turned on `use_linkedin_identifier` and the name is incomplete. Never your own data,
job text, notes or research.

Stored: for each call, the service, the outcome, the credits, and (only for usable results) the work
address, its verification status, score and up to five https evidence links. The lookup cache stores only
salted hashes of names, handles and addresses. Raw responses are never stored or logged.

Retention: an address never used is deleted after 90 days; a used one after its thread ends (no reply,
negative reply, bounce) plus 90 days. The send ledger keeps the recipient of real sends, because duplicate
protection needs it. `./jobhunter forget` and opt-out replies delete a person's provider data at once.

People who want their data removed at a service can use its own removal page, for example
https://hunter.io/claim or https://www.apollo.io/privacy-policy/remove.

## 6. Grades

- A: the exact address is published on the company's site or the job post (found by the agent's research).
- B: the address fits a pattern proven by two published addresses, or a service verified the mailbox
  (score 90 or more and the address matches the person's name), or two services returned the same address,
  or a verifier confirmed it.
- C: anything weaker, such as a "catch-all" domain or a low score. C is not sent by default.
- Invalid results are dropped, and free-mail, role (careers@, hr@) and other-company addresses are rejected.
  An address that a service or the verifier called invalid (including spam trap, abuse and do-not-mail) stays
  invalid: another service that later returns the same address as valid does not make it usable, and the
  finder moves on to look for a different address.

The MX check before the lookup covers the company's own domain. A found address at any other domain (another
domain of the same company, or a subdomain) gets its own MX check; with no mail server there it is not
sendable, and when DNS does not answer it is kept but not sent to until `email verify` has checked it.

An address a service found keeps the grade the code gave it: `email verify` can only lower it, and only a
research page that shows the exact address (on the company's own site or the job post) makes it A. Right
before each send the gate checks a found address again: it must still be the one the service returned, it
must be younger than 180 days, the service must not be stopped for bounces, and found addresses may be at
most half of the cold emails of the last 7 days.

## 7. Reading `./jobhunter enrich budget`

For each service: enabled, whether a key is stored (never the key), credits spent and allowed in the last 31
days and 24 hours, requests today, lifetime credits for the trials, `exhausted_until` when the service said
the free credits are used up, and an open breaker.

## 8. When it stops itself

| Why | What happens | How to clear it |
|---|---|---|
| The service refused the key | that service stops | `./jobhunter enrich connect <service>` |
| Free credits used up | the service is skipped for 31 days (trials: until you act) | nothing; it resumes |
| Too many requests (HTTP 429) | waits 1 hour, then 6, then 24 | nothing; it resumes |
| 3 failed calls in a row | waits 6 hours | nothing; it resumes |
| 2 unreadable answers in a day, a TLS failure, or a phone number in an answer | that service stops | update the code or check your network, then `./jobhunter breaker reset enrich:<service>` |
| 2 bounces on one service's addresses in 30 days | that service stops, its unsent addresses are blocked | `./jobhunter breaker reset enrich:<service>` |
| 3 bounces on found addresses in 30 days | the whole finder stops | `./jobhunter breaker reset enrich` |
| You want it off now | `./jobhunter pause --scope enrich` | `./jobhunter unpause --scope enrich` |

The bounce stops hold until you reset them. They apply even when the service was already stopped for
another reason (a 429 wait or a refused key): the bounce stop replaces that stop, so it does not end when
the wait is over or when you run `enrich connect`. A bounce still counts after the person's data is deleted
(`forget`, opt-out or complaint).

A bounce on a guessed or found address also puts that company's domain on the no-guessing list, which
stops lookups there. After a bounce, `./jobhunter enrich show <contact>` reports the address with
`bounced: true` and `sendable: false`, and it is never sent to again.

## 9. Tools that are deliberately not used

- LinkedIn scrapers and "LinkedIn profile APIs" that fetch LinkedIn pages live (for example Proxycurl-style
  services or Icypeas Profile Scraper): they break LinkedIn's terms.
- Browser extensions that read LinkedIn pages (Apollo, Lusha, ContactOut, Kaspr, Wiza, Snov, Hunter,
  GetProspect extensions): same reason, and they need your LinkedIn session.
- Leaked or shared API keys, and several accounts at one service: someone else's account, or against the
  service's terms.
- SMTP probing (Reacher, check-if-email-exists) and search-engine harvesters: they damage your IP's and your
  Gmail account's reputation.
- Services whose free plan has no API (Snov.io, Skrapp, LeadMagic, ContactOut, RocketReach, Kaspr, Wiza,
  FullEnrich, Clearbit), and Lusha (misses cost credits and it returns phone numbers by default).
