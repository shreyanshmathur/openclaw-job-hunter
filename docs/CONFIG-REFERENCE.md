# Configuration reference

This page explains every setting in `private/config.json` (a copy of the committed `config.example.json`) and
the hard limits that the code never goes past. The numbers come from the research notes on LinkedIn and Gmail
limits; the defaults are the researched conservative values.

## How a value becomes the value in use

`jh.py config show --effective` prints the values the code uses. They are computed like this:

- **Limit keys** (a higher number is looser, for example `gmail.ceilings.conservative.cold_day`): the value in
  use is the smallest of the file value, the baseline and the hard maximum. The baseline is the researched
  default, unless you raised it with `./jobhunter config raise <path> <value>` (owner PIN), which is still
  capped at the hard maximum. Editing the file can only make a limit stricter.
- **Floor keys** (a lower number is looser: gaps, cooldowns, delays, minimum ages): the value in use is the
  largest of the file value, the baseline and the hard minimum.
- **Authority keys** (`approval.mode`, `approval.per_channel.linkedin`, `gmail.tier`, `linkedin.tier`,
  `channels.linkedin.enabled`): the stricter of the file and the database wins. Loosening happens only through
  the PIN commands `approval set`, `tier set` and `linkedin enable`. With an empty database the strictest value
  applies: human approval, conservative tiers, LinkedIn off.
- **Fixed keys** always keep the default (`schema_version`, `browser.profile`, `qc.review.agent`,
  `browser.require_display`, `browser.type_slowly`, `outreach.referral_ask_only_after_reply`, every site's
  `risk`, and an `apply` mode of `never`).
- **Bounded keys** are free inside their range (table below).
- Every other key is free for you to edit. A value of the wrong type falls back to the default, unknown keys
  are ignored, and `./jobhunter config validate` lists every clamp with the reason.

`config lower <path> <value>` (also `/jh lower` in chat) tightens one key by editing the file and refuses
anything that loosens. `config apply` copies the values the database triggers need into the `meta` table
(company email cooldown, per-company application caps, per-company LinkedIn first touches, agency caps); it
runs at `init`, at `preflight` when the file changed, and during housekeeping.

Rolling windows: hour = the last 60 minutes, day = the last 24 hours, week = the last 7 days. LinkedIn Easy
Apply uses the UTC day; LinkedIn people searches reset on the 1st at 00:00 America/Los_Angeles; Naukri and the
free LinkedIn note quota use the calendar month. Daily caps are jittered: the cap in use on a day is
`floor(cap * u)` with `u` drawn between (1 minus `boards.daily_jitter_pct` percent) and 1 (never below 1 for a
positive cap).

## Keys

| Key | Default | Class | Meaning |
|---|---|---|---|
| `schema_version` | 2 | fixed | format of this file |
| `timezone` | `auto` | free | local time for windows, the digest and the Sheet; `auto` is the system zone |
| `locale` | `auto` | free | default tone locale |
| `owner.first_name`, `owner.last_name` | empty | free | resume file name and signature |
| `owner.gmail_address` | `you@example.com` | free | the From address and the Gmail identity check |
| `owner.linkedin_name`, `owner.linkedin_profile_url` | placeholders | free | the LinkedIn identity check |
| `owner.signature.*` | empty | free | signature block appended by code (linted like any text) |
| `owner.notify.channel`, `owner.notify.to` | `whatsapp`, placeholder | free | where messages to you go (`none` turns chat delivery off) |
| `owner.notify.desktop` | true | free | desktop notification for urgent items |
| `owner.notify.quiet_hours` | 22:00 to 08:00 | free | only urgent items inside |
| `approval.mode` | `inherit` | authority | `human` forces human approval whatever the database says |
| `approval.per_channel.linkedin` | `human` | authority | LinkedIn items always need you unless `inherit` |
| `approval.approval_ttl_hours` | 72 | floor, at least 12 | pending approvals expire |
| `approval.always_human` | four categories | free, cannot remove the defaults | never auto-approved |
| `approval.suggest_auto_after.*` | 14 days, 50 items, 0.95 | free | when housekeeping suggests `approval auto` (it never switches) |
| `channels.applications.enabled`, `channels.email_outreach.enabled` | true | free | lane on or off |
| `channels.linkedin.enabled` | true | authority | LinkedIn writes need this and `linkedin enable` |
| `channels.linkedin.account_type` | `free` | free | note length and InMail |
| `channels.linkedin.writes.*` | invites, messages, withdraw on | free | each write type (off whenever LinkedIn is off) |
| `channels.linkedin.invite_note_policy` | `note_while_quota_else_no_note` | free | when an invite carries a note |
| `active_hours.browser_days`, `browser_window` | Mon to Fri, 08:30 to 20:30 | free | browser lanes only inside |
| `active_hours.api_window` | whole day | free | the API lane |
| `active_hours.battery_floor_pct` | 25 | floor, at least 10 | no browser lane on battery below it (macOS) |
| `dispatch.mode` | `dispatcher` | free | random day plan, or the fixed-schedule fallback |
| `dispatch.lanes.<lane>.cycles_per_day` | per lane | limit | planned cycles per day (the replies lane only reads; it never holds a token, see [Sending rules no key changes](#sending-rules-no-key-changes)) |
| `dispatch.lanes.<lane>.window`, `days` | per lane | free | where slots are drawn |
| `dispatch.lanes.<lane>.min_spacing_minutes` | per lane | floor, at least 45 | spacing between slots of a lane |
| `dispatch.lanes.<lane>.skip_probability` | 0 to 0.1 | bounded 0 to 0.5 | randomly skipped cycles |
| `gmail.route` | `web_ui` | free | `web_ui`: the agent sends from Gmail in its own browser profile, after you allowed Gmail in the consent step (no password needed); `app_password`: optional fallback, code sends by SMTP with a Google app password. On `web_ui` the compose and Sent read-backs must name the reserved recipient alone (see below) |
| `gmail.tier` | `inherit` | authority | ceiling tier |
| `gmail.ceilings.<tier>.cold_day`, `followup_day`, `total_day`, `cold_week`, `hour`, `cycle` | 15, 8, 25, 60, 3, 2 | limit | Gmail write ceilings |
| `gmail.ceilings.<tier>.min_gap_minutes` | 10 | floor, at least 3 | gap between two sends |
| `gmail.ceilings.<tier>.gap_jitter_minutes` | 0 to 10 | bounded 0 to 20 | extra random gap |
| `gmail.ceilings.<tier>.company_cooldown_days` | 90 | floor, at least 30 | one cold email per company in this window |
| `gmail.warmup.week1_cold_day`, `step_per_week` | 5, 5 | limit | cold email warm-up |
| `gmail.followup_after_business_days` | 5 to 7 | floor | follow-up timing |
| `gmail.address_grades_allowed` | A, B | free (C only with `config raise`) | which addresses may be emailed |
| `gmail.active_days`, `sender_window`, `recipient_window`, `preferred_*` | weekdays, windows | free | send windows (recipient zone from the contact's locale) |
| `gmail.bounce_stop.*`, `complaint_stop.*`, `reply_rate_floor.*` | research values | limit or floor, only stricter | stop rules |
| `gmail.attach_resume_on_cold` | false | free | attach the resume to cold email |
| `gmail.max_links` | 2 | limit, at most 3 | links per email |
| `gmail.opt_out_line` | false | free | a varied opt-out sentence in cold email |
| `gmail.fetch_every_minutes` | 30 | floor, at least 15 | IMAP fetch cadence |
| `gmail.confirmation_check_hours` | 48 | free | how long to wait for an application confirmation email |
| `linkedin.tier` | `inherit` | authority | ceiling tier |
| `linkedin.active.*` | weekday windows | free | the daily LinkedIn window is drawn inside these |
| `linkedin.delays_sec.*` | research values | floor | pacing (write floor 45 s, profile view 15 s, Easy Apply 180 s) |
| `linkedin.ceilings.<tier>.*` | research values | limit | every LinkedIn ceiling |
| `linkedin.warmup_weeks[]` | three weeks | limit | the first three weeks after enabling or after a stop |
| `linkedin.adaptive.*` | 30 days, 20 invites, 0.40, 0.25, 14 days | floor | acceptance clamps |
| `linkedin.withdraw_older_than_days` | 21 | floor, at least 14 | invitation withdrawal |
| `linkedin.note_max_chars.*` | 180, 280 | limit | note length |
| `linkedin.engagement_likes_comments` | 0 | limit, at most 0 | never like or comment |
| `linkedin.max_invites_per_company_week` | 2 | limit, at most 3 | LinkedIn first touches per company per 7 days |
| `boards.global.*` | 25, 120, 6 | limit | all applications per day, week, hour |
| `boards.per_company.apps_day`, `apps_30d`, `apps_90d` | 1, 2, 3 | limit | applications per company |
| `boards.per_company.same_role_jaccard_block`, `second_role_max_jaccard` | 0.6, 0.3 | limit (lower is stricter) | role similarity |
| `boards.per_company.second_role_min_gap_days` | 1 | floor | gap between two roles at one company |
| `boards.per_agency.*` | 1, 3, 2, 6 | limit | agency caps (instead of the company cooldown) |
| `boards.site_gap_minutes` | 4 to 9 | floor, at least 3 to 5 | gap per site |
| `boards.daily_jitter_pct` | 20 | bounded 10 to 40 | daily cap jitter |
| `boards.sites.<site>.discover`, `apply` | per site | free (`never` is fixed) | discovery and apply mode |
| `boards.sites.<site>.pages_day`, `day`, `week`, `hour`, `month_stop` | per site | limit | per-site ceilings |
| `sources.api.<id>` | per source | free | API source on or off |
| `sources.poll_hours.*` | 12, 6, 24, 24, 24 | floor | polling cadence |
| `sources.remotive_max_calls_day` | 4 | limit, at most 4 | Remotive terms |
| `sources.per_host_min_interval_ms` | 1000 | floor, at least 1000 | politeness |
| `sources.max_age_days`, `targets_file`, `user_agent` | 30, path, project UA | free | discovery settings |
| `evaluator.thresholds.apply`, `borderline` | 70, 55 | floor, at least 60 and 45 | verdict thresholds |
| `evaluator.years_tolerance.*` | 1, 3 | bounded 0 to 5 | pre-filter tolerance |
| `evaluator.weights.*` | sums to 1 | free | scorecard weights |
| `evaluator.batch_size` | 15 | limit, at most 20 | packets per cycle |
| `outreach.per_job_max_contacts` | 1 | limit, at most 2 | people per job |
| `outreach.research.*` | 9, 180, 90, 14 | limit | research budget and ages |
| `outreach.target_skip_days` | 30 | floor, at least 30 | a dropped target is not picked again |
| `qc.max_rewrites`, `qc.max_human_edits` | 2, 3 | limit | rewrite and edit budgets |
| `qc.lint.*` counts | 2, 2, 200 | limit | linter settings |
| `qc.review.timeout_s` | 240 | bounded 60 to 300 | per review |
| `qc.review.max_tries` | 2 | limit | retries after a reviewer error |
| `qc.review.min_weighted`, `min_core`, `min_any` | 4.0, 4, 3 | floor | the pass rule |
| `qc.golden_min_agreement` | 18 | floor, at least 18 | needed for `approval auto` |
| `resume.*` | see the file | free (`max_pages` at most 2) | the resume renderer |
| `sheets.*` | see the file | free (`max_ops_per_post` at most 500) | the Google Sheet mirror |
| `exclusions.min_keep_ratio` | 0.5 | floor, at least 0.5 | deactivation safety (section 9) |
| `clock.max_skew_minutes`, `max_backward_minutes` | 10, 5 | limit | clock checks |
| `browser.max_tabs`, `lease_minutes` | 5, 35 | limit, floor | browser use; the next browser cycle that takes the lease turns tokens a dead cycle left open to `unknown` |
| `browser.dwell_seconds` | 5 to 40 | floor, at least 5 to 20 | reading pause before a commit click |
| `dns.doh_url` | Google DNS over HTTPS | free | MX check fallback |
| `enrich.enabled` | false | free | the optional email finder (docs/EMAIL-FINDER.md); off: no provider is ever called |
| `enrich.key_store` | `auto` | free | where your own provider keys live: `auto`, `keychain` (macOS) or `file` (private/enrich_keys.json, mode 600) |
| `enrich.chain`, `verifiers`, `reserve_chain` | four finders, two verifiers, two reserve finders | free, known providers only, each once | the order providers are tried; an unknown or repeated name is a config error |
| `enrich.max_finders_per_person` | 2 | limit, at most 4 | finder APIs tried for one person |
| `enrich.max_lookups_per_day`, `max_lookups_per_cycle` | 8, 2 | limit, at most 20 and 4 | new lookups |
| `enrich.max_unsent_found` | 10 | limit, at most 25 | found addresses not yet emailed before new lookups stop |
| `enrich.min_confidence` | 90 | floor, at least 80 | provider confidence needed for a result |
| `enrich.use_linkedin_identifier` | false | free | pass a LinkedIn URL you already have as an identifier (never scraped) |
| `enrich.timeout_s` | 12 | bounded 5 to 20 | per provider call |
| `enrich.retention_days`, `max_result_age_days` | 90, 180 | limit, at most 180 and 365 | how long results are kept and how old one may be at send time |
| `enrich.max_share_of_cold_sends` | 0.5 | limit, at most 1.0 | share of cold email that may go to provider-found addresses |
| `enrich.bounce_strikes.per_provider_30d`, `all_providers_30d` | 2, 3 | limit | bounces that stop a provider, or the whole finder |
| `enrich.skip_locales` | none | free | locales never looked up |
| `enrich.providers.<p>.enabled` | on for the free chain, off for reserve and Apollo | free | provider on or off |
| `enrich.providers.<p>.budget_31d`, `budget_lifetime`, `day_credits`, `day_requests` | per provider | limit | credit and request budgets (0 when the finder or the provider is off) |
| `enrich.providers.<p>.min_interval_s` | per provider | floor | gap between two calls to one provider |
| `otp.window_minutes` | 10 | limit, at most 10 | only messages after the request and within this many minutes |
| `otp.poll_seconds` | 15 | floor, at least 10 | how often the mailbox is checked for the code |
| `otp.max_uses_day_per_site` | 3 | limit, at most 6 | codes or links used per site per rolling 24 h |
| `otp.max_uses_day` | 10 | limit, at most 20 | codes or links used per rolling 24 h, all sites |
| `otp.failure_breaker_24h` | 3 | limit, at most 5 | failed requests (expired, rejected, ambiguous) per site in 24 h before `ats:<platform>` stops (`otp_failures`) |
| `otp.after_use` | `mark_read` | bounded: `leave`, `mark_read`, `archive` | what happens to the used message |
| `accounts.max_new_day` | 3 | limit, at most 5 | new site accounts per rolling 24 h, all sites |
| `accounts.max_new_week` | 10 | limit, at most 20 | new site accounts per rolling 7 days |
| `accounts.failure_breaker_24h` | 2 | limit, at most 3 | failed creates or sign-ins per site in 24 h before `ats:<platform>` stops (`account_failures`) |
| `accounts.password_length` | 20 | floor, at least 16 | length of the generated password |
| `accounts.key_store` | `auto` | bounded: `auto`, `keychain`, `file` | where site passwords are kept: `auto` is the macOS Keychain on macOS and `private/ats_accounts.json` (mode 600) on Linux and WSL |
| `captcha.handoff` | true | authority, strict false | false: a CAPTCHA sends the job to you as before, no task |
| `captcha.timeout_minutes` | 120 | limit, at most 240 | task deadline; then the job is skipped (`captcha_timeout`) |
| `captcha.max_tasks_day` | 5 | limit, at most 10 | CAPTCHA tasks opened per rolling 24 h; beyond, the job goes to you without a task |
| `captcha.max_open` | 2 | limit, at most 3 | open tasks at once; beyond, the job goes to you without a task |
| `captcha.repeat_breaker_per_site_day` | 3 | limit, at most 5 | CAPTCHAs per site per rolling 24 h that stop `ats:<platform>` (`captcha_repeat`) |
| `captcha.screenshot` | true | free | attach a screenshot to the chat message |
| `captcha.screenshot_retention_days` | 2 | limit, at most 7 | screenshots in `state/captcha/` are deleted after this |

`config lower` and `/jh lower` accept every limit above and `captcha.handoff false`; raising needs
`./jobhunter config raise` with the PIN.

## Browser consent (per site)

The agents browse in their own OpenClaw browser profile, never in your normal Chrome. A site's login gets there
only after you allowed that site: its cookies are copied from the Chrome profile you pick, or you log in by hand
inside the agent profile. Every site starts at No.

- The record is `private/consent.json` (mode 600), one row per site: `site`, `status` (`granted`, `declined` or
  `revoked`), `method` (`chrome_import` or `manual_login`), `chrome_profile`, `granted_at`, `revoked_at`.
- Only you write it: `./jobhunter init` and `./jobhunter browser consent` (installer), `jh.py browser consent
  grant --site <site> --method chrome_import|manual_login [--chrome-profile "Profile 1"]` and `jh.py browser
  consent revoke --site <site> | --all` (both need your PIN), and `./jobhunter browser forget [site|--all]`.
  `jh.py browser consent list` shows the state (read only). No agent command writes it.
- Sites: `gmail`, `linkedin`, `naukri`, `indeed`, `glassdoor`, `foundit`, `instahyre`, `wellfound`, `cutshort`,
  `hirist`, `iimjobs`, `yc`. ATS job forms and public boards need no login and no consent.
- Enforced in code: `gate reserve` refuses a browser action on a site without an active row (`E_CONSENT_MISSING`,
  exit 7); `preflight` lists the sites a lane may use and refuses a browser lane whose login sites all lack
  consent; `usage add` (every counted page) and `identity check` refuse such a site too. The guard plugin checks
  the same file on every browser call.
- Revoking trips that site's breaker (`gmail`, `linkedin` or `site:<name>`, reason `consent_revoked`) until you
  allow the site again; allowing it again closes a breaker that was open only for that reason. A change made
  outside these commands is caught at the next preflight or housekeeping (meta `consent_active_sites` holds the
  last seen list).
- The file is ignored when it is a link, belongs to another user or can be written by group or others: then no
  site has consent.

### Capabilities: email codes and site accounts

A top-level `capabilities` object in the same file records two more permissions per job site, both No until
you allow them:

- `email_codes`: read verification codes and sign-in links the site emails to your Gmail. It needs Gmail
  allowed on route `web_ui`, or `./jobhunter mail connect` on route `app_password`; otherwise it shows as
  "Unavailable".
- `ats_accounts`: create and use an account on the site with `owner.gmail_address`.

A site is an ATS platform key (`workday`, `icims`, `successfactors`, `taleo`, `greenhouse`, `lever`, `ashby`,
`smartrecruiters`, `oracle_hcm`, `jobvite`) or `host:<careers host>` for a company portal. `./jobhunter browser
consent` asks the questions; the core commands are `jh.py browser consent grant --capability email_codes
--capability ats_accounts --site workday` and `jh.py browser consent revoke --site workday [--capability ...]`
(PIN). `./jobhunter browser forget workday` takes both back, opens the breaker `ats:workday` (reason
`consent_revoked`) until you allow it again, and clears and re-imports the agent profile's cookies of the sites
still allowed. Workday jobs go to the agent only when `boards.sites.workday.apply` is `browser` (shipped
default `human_queue`; `./jobhunter config raise boards.sites.workday.apply browser`, PIN) and `ats_accounts`
is granted for `workday`.

## Sending rules no key changes

No setting loosens these. They are here because they explain refusals and states you may see.

- **Only the outreach and applier lanes hold a token.** An agent reserves inside its running cycle, and `gate
  reserve` refuses a cycle of any other lane (`E_CALLER_NOT_ALLOWED`, exit 11). The replies lane reads untrusted
  mail and messages, so it never reserves and never sends, although its agent (`jobhunter-outreach`) does send in
  its outreach cycles. `dispatch.lanes.replies.*` only plans when that reading happens.
- **A token left open by a dead cycle becomes `unknown` at the next preflight.** Once a browser cycle holds the
  lease (`browser.lease_minutes`), `preflight` takes every token its agent still holds as `reserved` or `armed`
  from an earlier cycle and marks it `unknown` (note `stale_cycle: ...`). That earlier cycle ends as `error` and
  its claims and staged files are released. The preflight output lists both under `stale` (`tokens`, `cycles`).
  Reconcile decides what happened, and the old token can never be sent again, so a crash between the click and
  `gate confirm` never leaves a live token that would allow a second Send.
- **A web-route email goes to the reserved recipient alone.** With `gmail.route` set to `web_ui`, the read-back
  that `gate arm --observed-file` checks is the compose window's `Subject:` and `To:` lines (and `Cc:`, `Bcc:`
  when the window shows them), a blank line, then the body. It needs exactly one To address, equal to the
  address the token was reserved for, and no Cc or Bcc address. Otherwise arm fails the token
  (`observed_text_mismatch`, `E_OBSERVED_MISMATCH`, exit 6) with `mismatch.recipient` set to `to_missing`,
  `cc_or_bcc`, `to_not_one_address` or `to_other_address`, and nothing is submitted. `gate confirm` needs the
  Sent folder copy in the same form (`E_EVIDENCE_MISSING` without it). A copy with other text or other
  recipients makes the action `unknown` with a `resolve_unknown` task for you, never `sent`; do not send it
  again. The `app_password` route has no read-back step.
- **A logged-out session is its own stop.** `li_logged_out` (LinkedIn), `gmail_logged_out` (a Google sign-in or
  account chooser page, including the password step) and `site_logged_out` (a job board login page or login
  wall) open the breaker of that site. `gmail_logged_out` and `site_logged_out` have no waiting time and no
  resume clamp: the alert names the one thing to do (on a Mac `./jobhunter browser import` to copy your Chrome
  login again, or `./jobhunter browser login <site>`, then `./jobhunter browser check <site>` and `./jobhunter
  breaker reset <scope>`). `li_logged_out` resumes at half the LinkedIn caps for a day (below). A verification
  or CAPTCHA page is `gmail_security` or `site_challenge` instead, which wins when both match: `gmail_security`
  steps Gmail warm-up down a week and `site_challenge` waits 24 hours.

## Hard limits in code

These values live in `scripts/jobhunter/hardmax.py`. No file edit and no `config raise` goes past them.

| Key (pattern) | Class | Never beyond |
|---|---|---|
| `boards.global.apps_day` | limit | at most 25 |
| `boards.global.apps_hour` | limit | at most 6 |
| `boards.global.apps_week` | limit | at most 120 |
| `boards.per_agency.apps_30d` | limit | at most 6 |
| `boards.per_agency.apps_day` | limit | at most 2 |
| `boards.per_agency.emails_30d` | limit | at most 3 |
| `boards.per_agency.emails_day` | limit | at most 1 |
| `boards.per_company.apps_30d` | limit | at most 2 |
| `boards.per_company.apps_90d` | limit | at most 3 |
| `boards.per_company.apps_day` | limit | at most 1 |
| `boards.per_company.same_role_jaccard_block` | limit | at most 0.6 |
| `boards.per_company.second_role_max_jaccard` | limit | at most 0.3 |
| `boards.sites.*.day` | limit | at most 25 |
| `boards.sites.*.hour` | limit | at most 6 |
| `boards.sites.*.pages_day` | limit | at most 40 |
| `boards.sites.*.week` | limit | at most 120 |
| `boards.sites.cutshort.week` | limit | at most 15 |
| `boards.sites.glassdoor.day` | limit | at most 0 |
| `boards.sites.indeed.day` | limit | at most 0 |
| `boards.sites.naukri.day` | limit | at most 50 |
| `boards.sites.naukri.month_stop` | limit | at most 130 |
| `boards.sites.yc.week` | limit | at most 7 |
| `browser.max_tabs` | limit | at most 5 |
| `clock.max_backward_minutes` | limit | at most 5 |
| `clock.max_skew_minutes` | limit | at most 10 |
| `dispatch.lanes.applier.cycles_per_day` | limit | at most [5, 6] |
| `dispatch.lanes.evaluator.cycles_per_day` | limit | at most [12, 16] |
| `dispatch.lanes.outreach.cycles_per_day` | limit | at most [5, 6] |
| `dispatch.lanes.replies.cycles_per_day` | limit | at most [3, 4] |
| `dispatch.lanes.scout.cycles_per_day` | limit | at most [3, 4] |
| `evaluator.batch_size` | limit | at most 20 |
| `gmail.bounce_stop.per_24h` | limit | at most 2 |
| `gmail.bounce_stop.rolling_rate_pause` | limit | at most 0.02 |
| `gmail.bounce_stop.rolling_rate_stop` | limit | at most 0.05 |
| `linkedin.adaptive.min_invites_aged_7d` | limit | at most 20 |
| `gmail.ceilings.*.cold_day` | limit | at most 30 |
| `gmail.ceilings.*.cold_week` | limit | at most 150 |
| `gmail.ceilings.*.cycle` | limit | at most 5 |
| `gmail.ceilings.*.followup_day` | limit | at most 15 |
| `gmail.ceilings.*.hour` | limit | at most 8 |
| `gmail.ceilings.*.total_day` | limit | at most 50 |
| `gmail.complaint_stop.per_30d` | limit | at most 2 |
| `gmail.max_links` | limit | at most 3 |
| `gmail.reply_rate_floor.after_sends` | limit | at most 100 |
| `gmail.warmup.step_per_week` | limit | at most 5 |
| `gmail.warmup.week1_cold_day` | limit | at most 5 |
| `linkedin.ceilings.*.actions_total_day` | limit | at most 250 |
| `linkedin.ceilings.*.content_search.day` | limit | at most 8 |
| `linkedin.ceilings.*.content_search.week` | limit | at most 30 |
| `linkedin.ceilings.*.cycles_day` | limit | at most 8 |
| `linkedin.ceilings.*.easy_apply.cycle` | limit | at most 4 |
| `linkedin.ceilings.*.easy_apply.day` | limit | at most 15 |
| `linkedin.ceilings.*.easy_apply.hour` | limit | at most 4 |
| `linkedin.ceilings.*.easy_apply.week` | limit | at most 70 |
| `linkedin.ceilings.*.inmail.day` | limit | at most 3 |
| `linkedin.ceilings.*.invites.cycle` | limit | at most 4 |
| `linkedin.ceilings.*.invites.day` | limit | at most 20 |
| `linkedin.ceilings.*.invites.hour` | limit | at most 6 |
| `linkedin.ceilings.*.invites.week` | limit | at most 80 |
| `linkedin.ceilings.*.job_search_pages.day` | limit | at most 100 |
| `linkedin.ceilings.*.messages.cycle` | limit | at most 6 |
| `linkedin.ceilings.*.messages.day` | limit | at most 30 |
| `linkedin.ceilings.*.messages.hour` | limit | at most 8 |
| `linkedin.ceilings.*.messages.week` | limit | at most 140 |
| `linkedin.ceilings.*.notes_free_month` | limit | at most 3 |
| `linkedin.ceilings.*.pending_invites_stop` | limit | at most 400 |
| `linkedin.ceilings.*.people_search.day` | limit | at most 15 |
| `linkedin.ceilings.*.people_search.month` | limit | at most 200 |
| `linkedin.ceilings.*.profile_views.cycle` | limit | at most 20 |
| `linkedin.ceilings.*.profile_views.day` | limit | at most 100 |
| `linkedin.ceilings.*.profile_views.hour` | limit | at most 30 |
| `linkedin.ceilings.*.profile_views.week` | limit | at most 500 |
| `linkedin.ceilings.*.withdraw.cycle` | limit | at most 5 |
| `linkedin.ceilings.*.withdraw.day` | limit | at most 15 |
| `linkedin.ceilings.*.writes_total_day` | limit | at most 60 |
| `linkedin.engagement_likes_comments` | limit | at most 0 |
| `linkedin.max_invites_per_company_week` | limit | at most 3 |
| `linkedin.note_max_chars.free` | limit | at most 200 |
| `linkedin.note_max_chars.premium` | limit | at most 300 |
| `linkedin.warmup_weeks[].*` | limit | at most 30 |
| `outreach.per_job_max_contacts` | limit | at most 2 |
| `outreach.research.facts_max_age_days` | limit | at most 14 |
| `outreach.research.hook_max_age_days` | limit | at most 180 |
| `outreach.research.hook_preferred_age_days` | limit | at most 90 |
| `outreach.research.max_page_loads` | limit | at most 9 |
| `qc.lint.li_connect_hard` | limit | at most 200 |
| `qc.lint.max_soft_hits` | limit | at most 2 |
| `qc.lint.max_warnings` | limit | at most 2 |
| `qc.max_human_edits` | limit | at most 3 |
| `qc.max_rewrites` | limit | at most 2 |
| `qc.review.max_tries` | limit | at most 2 |
| `resume.max_pages` | limit | at most 2 |
| `sheets.max_ops_per_post` | limit | at most 500 |
| `sources.remotive_max_calls_day` | limit | at most 4 |
| `active_hours.battery_floor_pct` | floor | at least 10 |
| `approval.approval_ttl_hours` | floor | at least 12 |
| `boards.per_company.second_role_min_gap_days` | floor | at least 1 |
| `boards.site_gap_minutes` | floor | at least [3, 5] |
| `browser.dwell_seconds` | floor | at least [5, 20] |
| `browser.lease_minutes` | floor | at least 35 |
| `dispatch.lanes.*.min_spacing_minutes` | floor | at least 45 |
| `evaluator.thresholds.apply` | floor | at least 60 |
| `evaluator.thresholds.borderline` | floor | at least 45 |
| `exclusions.min_keep_ratio` | floor | at least 0.5 |
| `gmail.bounce_stop.pause_hours` | floor | at least 24 |
| `gmail.ceilings.*.company_cooldown_days` | floor | at least 30 |
| `gmail.ceilings.*.min_gap_minutes` | floor | at least 3 |
| `gmail.complaint_stop.cut_days` | floor | at least 7 |
| `gmail.complaint_stop.first_complaint_cut_pct` | floor | at least 50 |
| `gmail.fetch_every_minutes` | floor | at least 15 |
| `gmail.followup_after_business_days` | floor | at least [3, 5] |
| `gmail.reply_rate_floor.min_rate` | floor | at least 0.02 |
| `linkedin.adaptive.half_below` | floor | at least 0.4 |
| `linkedin.adaptive.pause_below` | floor | at least 0.25 |
| `linkedin.adaptive.pause_days` | floor | at least 14 |
| `linkedin.delays_sec.between_cycles_min` | floor | at least [45, 45] |
| `linkedin.delays_sec.easy_apply` | floor | at least [180, 180] |
| `linkedin.delays_sec.easy_apply_floor` | floor | at least 180 |
| `linkedin.delays_sec.followup_days` | floor | at least [7, 7] |
| `linkedin.delays_sec.post_accept_message_days` | floor | at least [1, 1] |
| `linkedin.delays_sec.profile_view` | floor | at least [15, 15] |
| `linkedin.delays_sec.profile_view_floor` | floor | at least 15 |
| `linkedin.delays_sec.search_page` | floor | at least [5, 5] |
| `linkedin.delays_sec.write` | floor | at least [45, 45] |
| `linkedin.delays_sec.write_floor` | floor | at least 45 |
| `linkedin.withdraw_older_than_days` | floor | at least 14 |
| `outreach.target_skip_days` | floor | at least 30 |
| `qc.golden_min_agreement` | floor | at least 18 |
| `qc.review.min_any` | floor | at least 3 |
| `qc.review.min_core` | floor | at least 4 |
| `qc.review.min_weighted` | floor | at least 4.0 |
| `sources.per_host_min_interval_ms` | floor | at least 1000 |
| `sources.poll_hours.ats` | floor | at least 6 |
| `sources.poll_hours.hn` | floor | at least 24 |
| `sources.poll_hours.remote_boards` | floor | at least 3 |
| `sources.poll_hours.serpapi` | floor | at least 24 |
| `sources.poll_hours.yc` | floor | at least 24 |
| `boards.daily_jitter_pct` | bounded | between 10 and 40 |
| `dispatch.lanes.*.skip_probability` | bounded | between 0.0 and 0.5 |
| `evaluator.years_tolerance.above` | bounded | between 0 and 5 |
| `evaluator.years_tolerance.below` | bounded | between 0 and 5 |
| `gmail.bounce_stop.rolling_window_sends` | bounded | between 20 and 100 |
| `linkedin.adaptive.acceptance_window_days` | bounded | between 30 and 60 |
| `gmail.ceilings.*.gap_jitter_minutes` | bounded | between 0 and 20 |
| `qc.review.timeout_s` | bounded | between 60 and 300 |
| `enrich.bounce_strikes.all_providers_30d` | limit | at most 3 |
| `enrich.bounce_strikes.per_provider_30d` | limit | at most 2 |
| `enrich.max_finders_per_person` | limit | at most 4 |
| `enrich.max_lookups_per_cycle` | limit | at most 4 |
| `enrich.max_lookups_per_day` | limit | at most 20 |
| `enrich.max_result_age_days` | limit | at most 365 |
| `enrich.max_share_of_cold_sends` | limit | at most 1.0 |
| `enrich.max_unsent_found` | limit | at most 25 |
| `enrich.providers.anymailfinder.budget_31d` | limit | at most 20 |
| `enrich.providers.anymailfinder.budget_lifetime` | limit | at most 100 |
| `enrich.providers.anymailfinder.day_credits` | limit | at most 5 |
| `enrich.providers.anymailfinder.day_requests` | limit | at most 10 |
| `enrich.providers.apollo.budget_31d` | limit | at most 75 |
| `enrich.providers.apollo.day_credits` | limit | at most 10 |
| `enrich.providers.apollo.day_requests` | limit | at most 30 |
| `enrich.providers.findymail.budget_31d` | limit | at most 10 |
| `enrich.providers.findymail.budget_lifetime` | limit | at most 10 |
| `enrich.providers.findymail.day_credits` | limit | at most 5 |
| `enrich.providers.findymail.day_requests` | limit | at most 10 |
| `enrich.providers.getprospect.budget_31d` | limit | at most 50 |
| `enrich.providers.getprospect.day_credits` | limit | at most 10 |
| `enrich.providers.getprospect.day_requests` | limit | at most 30 |
| `enrich.providers.hunter.budget_31d` | limit | at most 50 |
| `enrich.providers.hunter.day_credits` | limit | at most 10 |
| `enrich.providers.hunter.day_requests` | limit | at most 30 |
| `enrich.providers.prospeo.budget_31d` | limit | at most 100 |
| `enrich.providers.prospeo.day_credits` | limit | at most 10 |
| `enrich.providers.prospeo.day_requests` | limit | at most 30 |
| `enrich.providers.tomba.budget_31d` | limit | at most 25 |
| `enrich.providers.tomba.day_credits` | limit | at most 5 |
| `enrich.providers.tomba.day_requests` | limit | at most 5 |
| `enrich.providers.zerobounce.budget_31d` | limit | at most 100 |
| `enrich.providers.zerobounce.day_credits` | limit | at most 10 |
| `enrich.providers.zerobounce.day_requests` | limit | at most 30 |
| `enrich.retention_days` | limit | at most 180 |
| `enrich.min_confidence` | floor | at least 80 |
| `enrich.providers.anymailfinder.min_interval_s` | floor | at least 1 |
| `enrich.providers.apollo.min_interval_s` | floor | at least 2 |
| `enrich.providers.findymail.min_interval_s` | floor | at least 1 |
| `enrich.providers.getprospect.min_interval_s` | floor | at least 1 |
| `enrich.providers.hunter.min_interval_s` | floor | at least 1 |
| `enrich.providers.prospeo.min_interval_s` | floor | at least 2 |
| `enrich.providers.tomba.min_interval_s` | floor | at least 31 |
| `enrich.providers.zerobounce.min_interval_s` | floor | at least 1 |
| `enrich.timeout_s` | bounded | between 5 and 20 |

Recipients per email are always 1. The `max_never_exceed` column of the research is never an operating tier:
`moderate` needs `./jobhunter tier set` (PIN) and the eligibility rules (28 clean days on conservative, and for
LinkedIn at least 40% acceptance on 20 or more invites aged 7 days and an account older than one year).

## Temporary clamps after a stop

When a breaker is reset, its resume policy can lower caps for a while (stored in `meta` as `clamp:<scope>`):
LinkedIn logged out or HTTP 429 halves LinkedIn caps for a day (a Gmail or job board logout adds no clamp); the weekly invitation limit halves invites for
14 days; the Easy Apply limit halves Easy Apply for 7 days; a Gmail sending limit halves Gmail caps for 7 days;
a restricted LinkedIn account allows no automation for 7 days, then restarts warm-up at week 1. A second stop on
an area that is already stopped never replaces a stricter resume policy, and the reset applies the policies of
every stop since the area stopped; a new clamp never weakens an active one.
