# Safety, terms of service and what you are agreeing to

This page says plainly what the risks are before you turn anything on. Summary: email to real people and
company application forms are the main channels; LinkedIn automation is off because it breaks LinkedIn's
rules; every platform gets low limits and an immediate stop on any warning.

## Your logins and your consent

The agent uses your existing logins, not your passwords, and only for the sites you allow one by one
(`./jobhunter browser consent`; every site defaults to No). It works in its own OpenClaw browser profile: your
normal Chrome is never opened or driven. For each allowed site only that site's cookies are copied in, from the
Chrome profile you pick by name (a work or school profile is marked and needs a typed confirmation, because its
logins may belong to your employer). Preflight, the send gate and the guard refuse any site without an active
consent, and `./jobhunter browser forget <site>` takes a site back at any time.

When a session expires or a site asks to verify it is you, the agent stops that site at once (a breaker), tells
you in chat with the one command to run (`./jobhunter browser consent <site>` or `./jobhunter browser login
<site>`), and never retries blindly or tries to get around the prompt. Allowing LinkedIn's login does not turn
LinkedIn on: the typed acknowledgement below is still required.

## LinkedIn

**Automating LinkedIn breaks the LinkedIn User Agreement, whatever the volume.** The agreement forbids using
bots or other unauthorized automated methods to access the service, to add or download contacts, or to send
or redirect messages, and it forbids scraping with software, scripts or robots. LinkedIn's help pages add that
third party software or browser extensions that automate activity on the website are not allowed, and that
accounts using them risk being restricted or shut down. This project drives a real browser for you, which is
exactly that kind of automation.

What can happen to your account:

- a temporary restriction (hours to about a week), often with a request to verify your identity, sometimes
  with a government ID;
- being required to know the email address of everyone you invite, after people mark your invitations as
  unknown or as spam;
- a permanent ban after repeated restrictions, with the loss of your network and messages.

What the project does about it:

- Every LinkedIn write action (invitations, messages, Easy Apply, withdrawals) ships **disabled**. Reading
  LinkedIn job lists is also off until you turn LinkedIn on.
- Turning it on needs your owner PIN and the typed sentence "I understand LinkedIn may restrict my account"
  (`./jobhunter linkedin enable`). You can turn it off at any time, also from chat.
- When on, the limits are far below what automation vendors advertise: a three week warm up starting at 3
  invitations a day, then at most 8 invitations a day and 35 a week, 10 messages a day, and at most two
  touches per person, ever. Invitations with a note are limited by LinkedIn itself to 3 a month on a free
  account.
- Browser work happens only inside your active hours, at random times, with human reading pauses, one tab,
  typed text (never pasted), and at most two clicks per approved action.
- Any CAPTCHA, verification page, "unusual activity" notice, weekly limit notice or login wall trips a breaker:
  LinkedIn work stops at once, you get a message, and only you can reset it, not before a cooldown.

Lower limits reduce the chance that LinkedIn notices. They do not make the automation allowed. If your
LinkedIn account matters to you, keep LinkedIn off. Email outreach and company career sites work without it.

## Indeed and Glassdoor

Indeed's terms ban automating Indeed Apply and using unofficial tools on the site; Glassdoor is part of the same
group. The project never applies on either site. Reading their job lists is off by default and read only when
you turn it on; matching jobs go to a list you apply to yourself.

## Other job boards

Naukri, Instahyre, Foundit, Cutshort, Hirist, iimjobs and Wellfound each forbid robots or automated access in
their terms, and some run active bot protection. They are all **off** by default. You can turn one on for
reading, or for applying, in `private/config.json`; each has its own low daily and weekly limits and its own
breaker. The risk (a blocked or closed account on that board) is yours to accept.

## Company career sites (ATS forms) and public job APIs

Jobs are discovered mostly through the public job APIs that companies publish through their applicant tracking
systems (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, BambooHR, Workday) and public remote
job boards. The agent fills an application form only for a job that passed your filters and the evaluator, with
answers from your confirmed profile, and stops for anything it does not know. Forms that need an account, show
a CAPTCHA or ask sensitive questions go to a list for you.

## Gmail

Email is sent from your own Gmail account, by default through Gmail in the agent's browser profile after you
allowed Gmail (no password needed); an app password route is optional. On the browser route the agent reads the
compose fields back and compares them with the approved text before the one allowed click, and confirms the send
in the Sent folder. Google limits sending and watches spam complaints and bounces:

- defaults: a warm up from 5 cold emails a day, then at most 15 cold emails and 25 emails in total a day, at
  least 10 minutes apart, only on weekdays inside business hours;
- one message per person and one cold email per company inside the cooldown (90 days); one follow up per
  thread, only when nobody answered;
- two bounces in a day, any spam complaint or a Google policy error stops email and tells you.

Send only to people for whom your message is relevant. Comply with the email rules of your country (for
example, identify yourself honestly and stop when someone asks you to).

## Email finder providers (optional, off by default)

The email finder uses only free API keys from accounts you open yourself, one account per provider, and only for
people already on the hiring team of a job you target. Keep to each provider's terms: Apollo, for example, is for
business use and needs a work email to sign up, Anymail Finder asks for card verification, and ZeroBounce's free
checks need a business or premium email domain. Never use a key you found online: it belongs to someone else,
and using it may be illegal.

Excluded on purpose, with no setting to turn them on: LinkedIn scrapers and profile crawlers, Chrome extensions
that read LinkedIn pages, guessing addresses and probing mail servers (SMTP checks), phone number lookups, lists
of "free" or leaked keys, and any "look up anyone" command. You are responsible for lawful use in your country;
this is not legal advice. Details: [EMAIL-FINDER.md](EMAIL-FINDER.md).

## CAPTCHAs and bot detection

The agent never solves, skips, waits out or works around a CAPTCHA or any verification. It stops and tells you.
There is no setting to change this.

## Your responsibilities

- You own the accounts and everything sent in your name. Read the drafts, especially in the first weeks.
- Keep the default `human` approval mode until you trust the output; `auto` mode needs your PIN, a typed
  sentence and a recent reviewer calibration, and some kinds of message always wait for you anyway.
- Do not list facts you would not stand behind in an interview: the agent only uses facts from your resume and
  your answers, and it cannot know if those are out of date.
- Respect the people you contact. If someone says no, the agent records it and never contacts them again.
