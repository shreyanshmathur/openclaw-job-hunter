"""Account identity checks (design 12.12, 4.5): the logged-in LinkedIn profile or Gmail account must be the
owner's. A mismatch trips the platform breaker (the trip survives the refusal) and is E_IDENTITY_MISMATCH.
"""
from __future__ import annotations

import re

from . import db, keys
from .canon import now
from .errors import Denied
from .events import log_event

OBSERVED_KEYS = {"linkedin": ("display_name", "profile_url"), "gmail": ("account_email",)}
PLACEHOLDERS = ("you@example.com", "https://www.linkedin.com/in/your-handle")


def _norm_name(s: str | None) -> str:
    return " ".join(keys.fold(s or "").split())


def identity_check(conn, platform: str, observed: dict, cfg: dict | None = None, cycle_id: str | None = None) -> dict:
    """Compare what the browser shows with owner.* in the config. OK: records a clear detection and
    returns {ok: True}. Mismatch: trips the breaker (deferred, so it survives) and raises
    Denied(E_IDENTITY_MISMATCH). Missing owner settings: E_CONFIG_INVALID."""
    from . import breakers, config
    if platform not in OBSERVED_KEYS:
        raise Denied("E_VALIDATION", "identity platform must be gmail or linkedin")
    if not isinstance(observed, dict) or any(k not in OBSERVED_KEYS[platform] for k in observed):
        raise Denied("E_SCHEMA", "observed keys for %s: %s" % (platform, ", ".join(OBSERVED_KEYS[platform])))
    cfg = cfg or config.load(conn)
    owner = cfg.get("owner") or {}
    problems = []
    if platform == "linkedin":
        want_url = owner.get("linkedin_profile_url") or ""
        if not want_url or want_url in PLACEHOLDERS:
            raise Denied("E_CONFIG_INVALID", "set owner.linkedin_profile_url in private/config.json")
        want = keys.linkedin_keys(want_url)[0][0]
        got_url = observed.get("profile_url")
        if not isinstance(got_url, str) or not got_url:
            raise Denied("E_SCHEMA", "observed.profile_url is required")
        try:
            got = keys.linkedin_keys(got_url)[0][0]
        except Denied:
            got = None
        if got != want:
            problems.append("profile %s is not %s" % (got or got_url, want))
        want_name = _norm_name(owner.get("linkedin_name"))
        if want_name and observed.get("display_name") is not None and \
                _norm_name(observed.get("display_name")) != want_name:
            problems.append("display name differs")
    else:
        want = (owner.get("gmail_address") or "").strip().lower()
        if not want or want in PLACEHOLDERS:
            raise Denied("E_CONFIG_INVALID", "set owner.gmail_address in private/config.json")
        got = (observed.get("account_email") or "").strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+$", got):
            raise Denied("E_SCHEMA", "observed.account_email is required")
        if keys.normalize_email(got) != keys.normalize_email(want):
            problems.append("signed in as another account")
    if problems:
        reason = "li_identity_mismatch" if platform == "linkedin" else "gmail_identity_mismatch"
        detail = "identity check: " + "; ".join(problems)

        def _trip(c):
            breakers.trip(c, platform, reason, detail, by="identity", cycle_id=cycle_id)
        db.defer_write(conn, _trip)
        raise Denied("E_IDENTITY_MISMATCH", "the browser is signed in to another %s account" % platform,
                     data={"problems": problems})
    conn.execute("INSERT INTO detections (platform, source, url, http_status, verdict, code, matched, cycle_id, "
                 "created_at) VALUES (?, 'agent', NULL, NULL, 'clear', 'identity_ok', NULL, ?, ?)",
                 (platform, cycle_id, now()))
    log_event(conn, "identity_ok", platform=platform)
    return {"ok": True, "platform": platform}


# ---------------------------------------------------------------- per-site browser consent
# Change request "use the existing Chrome logins, with consent": the agents browse in their own OpenClaw
# browser profile, and a site's login gets there only with the owner's consent for that site. The consent
# lives in private/consent.json (mode 0600). Only the owner writes it: `browser consent grant` (owner PIN),
# `browser consent revoke` (owner PIN) and the installer's consent step (`./jobhunter init`,
# `./jobhunter browser consent|forget`), which use the same file format. No agent command writes it.
#
#   {"version": 1, "updated_at": "<ts>",
#    "sites": {"linkedin": {"site": "linkedin", "status": "granted", "method": "chrome_import",
#                           "domains": [...], "chrome_profile": "Profile 1", "chrome_profile_name": "...",
#                           "granted_at": "<ts>", "revoked_at": null, "declined_at": null, "by": "owner"}}}
#
# A site has consent only when its row, under its own name, has status "granted", a granted_at and no
# revoked_at (the same rule as the guard plugin's reader, so the two fences never disagree). A missing,
# unreadable or malformed file, a link, or a file of another user or writable by group or others gives no
# site consent (fail closed). Every site starts at No: a site without a row has no consent.
#
# Enforcement in code: gate.reserve refuses a browser-route action on a login site without consent
# (E_CONSENT_MISSING), preflight refuses a browser lane whose login sites all lack consent and lists the sites
# the lane may use, and `usage add` (every page view the agent counts) and `identity check` refuse a login
# site without consent. Sites that need no login (ATS forms, public boards) are outside the list: the agent's
# profile holds no cookies for them. Revoking trips the site's breaker (reason consent_revoked) until the
# owner grants consent again.
CONSENT_VERSION = 1
CONSENT_METHODS = ("chrome_import", "manual_login")
CONSENT_STATUSES = ("granted", "declined", "revoked")
CONSENT_REASON = "consent_revoked"
CONSENT_MAX_BYTES = 1024 * 1024
# site: (label, cookie domains). Same names and domains as the installer's consent step (install.CONSENT_SITES).
CONSENT_SITES = {
    "gmail": ("Gmail", ("google.com", "mail.google.com", "accounts.google.com")),
    "linkedin": ("LinkedIn", ("linkedin.com", "www.linkedin.com")),
    "naukri": ("Naukri", ("naukri.com",)),
    "indeed": ("Indeed", ("indeed.com",)),
    "glassdoor": ("Glassdoor", ("glassdoor.com", "glassdoor.co.in")),
    "foundit": ("Foundit", ("foundit.in",)),
    "instahyre": ("Instahyre", ("instahyre.com",)),
    "wellfound": ("Wellfound", ("wellfound.com",)),
    "cutshort": ("Cutshort", ("cutshort.io",)),
    "hirist": ("Hirist", ("hirist.tech",)),
    "iimjobs": ("iimjobs", ("iimjobs.com",)),
    "yc": ("Work at a Startup (YC)", ("workatastartup.com", "ycombinator.com")),
}
SITES = tuple(CONSENT_SITES)
_CHROME_DIR_RE = re.compile(r"^(Default|Profile [0-9]{1,4})$")
_META_ACTIVE = "consent_active_sites"


def _empty_consent() -> dict:
    return {"version": CONSENT_VERSION, "sites": {}}


def load_consent(strict: bool = True) -> dict:
    """private/consent.json as {version, updated_at, sites: {site: row}} with only known sites and statuses.
    With `strict` (every reader that decides whether a site may be used) a link, a file of another user, one
    that group or others can write, a file over 1 MB or one that is not valid gives no consent at all. Never
    raises."""
    import json
    import os
    from . import paths
    path = paths.consent_file()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return _empty_consent()
    try:
        st = os.fstat(fd)
        if strict and (st.st_uid != os.getuid() or st.st_mode & 0o022 or st.st_size > CONSENT_MAX_BYTES):
            return _empty_consent()
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            doc = json.load(fh)
    except (OSError, ValueError):
        return _empty_consent()
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(doc, dict) or not isinstance(doc.get("sites"), dict):
        return _empty_consent()
    sites = {}
    for site, row in doc["sites"].items():
        if site in CONSENT_SITES and isinstance(row, dict) and row.get("status") in CONSENT_STATUSES:
            sites[site] = row
    out = {"version": CONSENT_VERSION, "sites": sites, "updated_at": doc.get("updated_at")}
    caps = _read_capabilities(doc.get("capabilities"))
    if caps:
        out["capabilities"] = caps
    return out


def _row_active(site: str, row) -> bool:
    return (isinstance(row, dict) and row.get("site", site) == site and row.get("status") == "granted"
            and isinstance(row.get("granted_at"), str) and bool(row["granted_at"].strip())
            and row.get("revoked_at") in (None, "", False))


def consent_active(site: str, doc: dict | None = None) -> bool:
    doc = doc if doc is not None else load_consent()
    return _row_active(str(site), doc.get("sites", {}).get(str(site)))


def active_consent_sites(doc: dict | None = None) -> list[str]:
    """The sites with an active consent row, in CONSENT_SITES order."""
    doc = doc if doc is not None else load_consent()
    return [s for s in SITES if _row_active(s, doc.get("sites", {}).get(s))]


def consent_site(platform) -> str | None:
    """The consent site of a platform id (gmail, linkedin, a board name or site:<name>, a URL host), or None
    when the platform uses no login of the owner (ATS forms, public boards, API sources)."""
    p = str(platform or "").strip().lower()
    if not p or p.startswith("api:"):
        return None
    if p.startswith("site:"):
        p = p[5:]
    if p in ("linkedin_jobs", "linkedin_posts"):
        p = "linkedin"
    if p in CONSENT_SITES:
        return p
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", p).split("/", 1)[0].split(":", 1)[0]
    for site, (_label, domains) in CONSENT_SITES.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return site
    return None


def breaker_scope(site: str) -> str:
    """The breaker a revoked consent trips: gmail, linkedin, or site:<board>."""
    return site if site in ("gmail", "linkedin") else "site:" + site


def require_consent(conn, platform) -> str | None:
    """Denied(E_CONSENT_MISSING) when `platform` is a login site without the owner's active consent. Returns
    the site (None for a platform that needs no consent). Read only."""
    site = consent_site(platform)
    if site is None:
        return None
    if not consent_active(site):
        label = CONSENT_SITES[site][0]
        raise Denied("E_CONSENT_MISSING", "you have not allowed the agent to use your %s login; run ./jobhunter "
                     "browser consent to allow it" % label, data={"site": site})
    return site


def consent_summary(conn=None) -> dict:
    """Read-only view for status, the digest and the Sheet: {chrome_profile, sites: [{site, label, state,
    method, chrome_profile, granted_at, revoked_at}]}, every known site listed (no row: not_granted)."""
    doc = load_consent()
    rows = []
    prof, prof_at = None, ""
    for site in SITES:
        label = CONSENT_SITES[site][0]
        row = doc["sites"].get(site) or {}
        if _row_active(site, row):
            state = "granted"
            if row.get("chrome_profile") and str(row.get("granted_at")) > prof_at:
                prof, prof_at = row.get("chrome_profile_name") or row.get("chrome_profile"), str(row["granted_at"])
        elif row.get("status") == "revoked" or row.get("revoked_at"):
            state = "revoked"
        else:
            state = "not_granted"
        rows.append({"site": site, "label": label, "state": state, "method": row.get("method"),
                     "chrome_profile": row.get("chrome_profile"), "granted_at": row.get("granted_at"),
                     "revoked_at": row.get("revoked_at")})
    return {"chrome_profile": prof, "sites": rows, "updated_at": doc.get("updated_at")}


def _consent_sites_arg(value) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else ([] if value is None else [value])
    out = []
    for item in items:
        for w in str(item).split(","):
            w = w.strip().lower()
            if not w:
                continue
            if w not in CONSENT_SITES:
                raise Denied("E_VALIDATION", "unknown site %s; known: %s" % (w, ", ".join(SITES)))
            if w not in out:
                out.append(w)
    return out


def _save_consent(doc: dict) -> str:
    """Atomic write, mode 0600, never through a link."""
    import json
    import os
    from . import paths
    path = paths.consent_file()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if os.path.islink(path):
        raise Denied("E_PATH_NOT_ALLOWED", "private/consent.json is a link; remove it first")
    out = {"version": CONSENT_VERSION, "updated_at": now(), "sites": doc.get("sites", {})}
    if doc.get("capabilities"):
        out["capabilities"] = doc["capabilities"]
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    os.replace(tmp, path)
    return path


def _consent_breaker_only(conn, scope: str):
    """The open breaker row of `scope` when consent_revoked is the only reason it is open, else None."""
    from . import breakers
    row = conn.execute("SELECT * FROM breakers WHERE scope = ? AND state = 'open'", (scope,)).fetchone()
    if row is None:
        return None
    return row if breakers._trip_reasons_since_open(conn, scope, row) == [CONSENT_REASON] else None


def _consent_breakers(conn, revoked: list, granted: list, by: str) -> dict:
    """Trip the breaker of every revoked site; close the breaker of every granted site that is open only
    because its consent was revoked (any other stop keeps it open for `breaker reset`)."""
    from . import breakers
    tripped, closed = [], []
    for site in revoked:
        scope = breaker_scope(site)
        breakers.trip(conn, scope, CONSENT_REASON, "consent for %s was revoked; nothing uses that login until you "
                      "run ./jobhunter browser consent again" % CONSENT_SITES[site][0], by=by)
        tripped.append(scope)
    for site in granted:
        scope = breaker_scope(site)
        if _consent_breaker_only(conn, scope) is not None:
            breakers._close(conn, scope, "reset", by, "consent granted again")
            closed.append(scope)
    return {"tripped": tripped, "closed": closed}


def _snapshot(conn, sites: list) -> None:
    from . import db
    value = ",".join(sites)
    if db.meta_get(conn, _META_ACTIVE) != value:
        db.meta_set(conn, _META_ACTIVE, value, "system")


def grant_consent(conn, sites, *, method: str, chrome_profile: str | None = None,
                  chrome_profile_name: str | None = None, by: str = "human:cli") -> dict:
    """The owner allows the agent to use these sites' logins (the caller checked the PIN). Writes the consent
    rows, closes breakers that are open only because consent was revoked. Runs inside the caller's tx."""
    grant = _consent_sites_arg(sites)
    if not grant:
        raise Denied("E_USAGE", "name at least one site: %s" % ", ".join(SITES))
    if method not in CONSENT_METHODS:
        raise Denied("E_VALIDATION", "method must be one of %s" % ", ".join(CONSENT_METHODS))
    prof = prof_name = None
    if method == "chrome_import":
        prof = str(chrome_profile or "")
        if not _CHROME_DIR_RE.match(prof):
            raise Denied("E_VALIDATION", "choose the Chrome profile folder (Default or Profile <n>)")
        prof_name = " ".join(str(chrome_profile_name or prof).split())[:80] or prof
        if any(ord(c) < 32 for c in prof_name):
            raise Denied("E_VALIDATION", "the Chrome profile name has control characters")
    doc = load_consent(strict=False)
    ts = now()
    for site in grant:
        doc["sites"][site] = {"site": site, "status": "granted", "method": method,
                              "domains": list(CONSENT_SITES[site][1]), "chrome_profile": prof,
                              "chrome_profile_name": prof_name, "granted_at": ts, "revoked_at": None,
                              "declined_at": None, "by": "owner"}
    path = _save_consent(doc)
    br = _consent_breakers(conn, [], grant, by)
    _snapshot(conn, active_consent_sites())
    log_event(conn, "browser_consent", granted=grant, method=method, by=by)
    return {"granted": grant, "method": method, "path": path, "breakers_closed": br["closed"]}


def revoke_consent(conn, sites=None, everything: bool = False, by: str = "human:cli") -> dict:
    """Mark consent revoked for these sites (or every granted site) and trip each site's breaker (reason
    consent_revoked) until consent is granted again. Clearing the agent profile's cookies is the wrapper's
    step (`./jobhunter browser forget`). Runs inside the caller's tx."""
    doc = load_consent(strict=False)
    if everything:
        targets = [s for s in SITES if (doc["sites"].get(s) or {}).get("status") == "granted"]
    else:
        targets = _consent_sites_arg(sites)
        if not targets:
            raise Denied("E_USAGE", "name a site or use --all")
    ts = now()
    revoked = []
    for site in targets:
        row = doc["sites"].get(site)
        if row and row.get("status") == "granted":
            row["status"] = "revoked"
            row["revoked_at"] = ts
            revoked.append(site)
    path = _save_consent(doc) if revoked else None
    br = _consent_breakers(conn, revoked, [], by)
    _snapshot(conn, active_consent_sites())
    log_event(conn, "browser_consent_revoked", revoked=revoked, by=by)
    return {"revoked": revoked, "path": path, "breakers_tripped": br["tripped"]}


def sync_consent(conn, by: str = "system:consent") -> dict:
    """Catch consent changes written outside grant_consent/revoke_consent (the installer's consent step, a hand
    edit): a site that lost its active row trips its breaker, a site that got it back closes a breaker that is
    open only for consent_revoked. Compares with meta consent_active_sites. Runs inside the caller's tx
    (preflight, housekeeping)."""
    from . import db
    now_active = active_consent_sites()
    before_raw = db.meta_get(conn, _META_ACTIVE)
    before = [s for s in (before_raw or "").split(",") if s in CONSENT_SITES]
    lost = [s for s in before if s not in now_active]
    gained = [s for s in now_active if s not in before]
    lost = [s for s in lost if _consent_breaker_only(conn, breaker_scope(s)) is None]
    br = _consent_breakers(conn, lost, gained, by)
    if before_raw is None or lost or gained:
        _snapshot(conn, now_active)
    return {"active": now_active, "tripped": br["tripped"], "closed": br["closed"]}


def lane_consent(cfg: dict, lane: str, doc: dict | None = None) -> dict:
    """The login sites a browser lane uses (from the config), which of them have consent, and whether the lane
    also has work that needs no login: {sites, allowed, missing, public_work}."""
    doc = doc if doc is not None else load_consent()
    boards = ((cfg.get("boards") or {}).get("sites") or {})
    li_on = bool(((cfg.get("channels") or {}).get("linkedin") or {}).get("enabled"))
    web_ui = (cfg.get("gmail") or {}).get("route") == "web_ui"
    sites: list[str] = []
    public = False

    def add(site):
        if site not in sites:
            sites.append(site)
    if lane == "scout":
        for name, s in boards.items():
            if not isinstance(s, dict) or s.get("discover") != "browser":
                continue
            site = consent_site(name)
            if site == "linkedin" and not li_on:
                continue
            if site:
                add(site)
            else:
                public = True
    elif lane == "applier":
        if web_ui:
            add("gmail")
        for name, s in boards.items():
            if not isinstance(s, dict):
                continue
            site = consent_site(name)
            if s.get("apply") == "browser":
                if site:
                    add(site)
                else:
                    public = True
            elif site == "linkedin" and s.get("apply") == "via_linkedin_channel" and li_on:
                add("linkedin")
    elif lane in ("outreach", "replies"):
        if web_ui:
            add("gmail")
        if li_on:
            add("linkedin")
    allowed = [s for s in sites if consent_active(s, doc)]
    return {"sites": sites, "allowed": allowed, "missing": [s for s in sites if s not in allowed],
            "public_work": public}


# ---------------------------------------------------------------- per-site capabilities (email codes, site accounts)
# FEATURES-OTP-ACCOUNTS-CAPTCHA 1.2: next to `sites`, private/consent.json holds a top-level "capabilities" object,
# written only by the owner (browser consent grant|revoke --capability, PIN; the installer's consent step):
#
#   "capabilities": {"email_codes": {"workday": {"site": "workday", "capability": "email_codes", "status": "granted",
#                                                "granted_at": "<ts>", "revoked_at": null, "declined_at": null,
#                                                "by": "owner", "method": "pin"}},
#                    "ats_accounts": {"workday": {..., "email": "<owner.gmail_address at grant time>"}}}
#
# email_codes: the agent may have code read verification codes and sign-in links that this site sends to the
# owner's mailbox. ats_accounts: code may create and use an account on this site with the owner's address.
# Sites are ATS platform keys (accounts.ATS_PLATFORMS) or host:<host> for a company's own portal (that host and its
# subdomains). Every site starts at No. A row is active with status granted, a granted_at, no revoked_at and its
# site and capability equal to its keys; an ats_accounts row is also inactive when its email is not the current
# owner.gmail_address. The same file-safety rules as `sites` apply (load_consent).
CAPABILITIES = ("email_codes", "ats_accounts")
CAPABILITY_LABEL = {"email_codes": "email codes and sign-in links", "ats_accounts": "site accounts"}
_HOST_SITE_RE = re.compile(r"^host:([a-z0-9]([a-z0-9-]*[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _read_capabilities(raw) -> dict:
    """{capability: {site: row}} of the rows of known capabilities with a known status (unknown names ignored)."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for cap, rows in raw.items():
        if cap not in CAPABILITIES or not isinstance(rows, dict):
            continue
        keep = {}
        for site, row in rows.items():
            if isinstance(site, str) and isinstance(row, dict) and row.get("status") in CONSENT_STATUSES:
                keep[site] = row
        if keep:
            out[cap] = keep
    return out


def capability_sites() -> tuple:
    """The ATS platform keys a capability can be granted for."""
    from .accounts import ATS_PLATFORMS
    return ATS_PLATFORMS


def valid_capability_site(site: str) -> str:
    """The site name, lower-cased; Denied(E_VALIDATION) for anything but an ATS key or a host:<host> that is not a
    never host nor a platform host (at most 100 characters, no port)."""
    from . import accounts
    s = str(site or "").strip().lower()
    if s in accounts.ATS_PLATFORMS:
        return s
    if s.startswith("host:"):
        host = s[5:]
        if len(host) > 100 or not _HOST_SITE_RE.match(s):
            raise Denied("E_VALIDATION", "a company portal site is host:<host> (lower case, no port, at most 100 "
                         "characters)")
        cls = accounts.classify_host(host)
        if cls["never"] or cls["platform"] is not None:
            raise Denied("E_VALIDATION", "%s is not a company portal (use the platform name %s)"
                         % (host, cls["platform"] or "") if cls["platform"] else "%s is never allowed" % host)
        return s
    raise Denied("E_VALIDATION", "unknown site %s; use an ATS name (%s) or host:<careers host>"
                 % (s, ", ".join(accounts.ATS_PLATFORMS)))


def _owner_address(cfg: dict | None = None) -> str:
    try:
        if cfg is None:
            from . import config
            cfg = config.load()
        return str(((cfg or {}).get("owner") or {}).get("gmail_address") or "").strip().lower()
    except Exception:
        return ""


def _cap_row_active(cap: str, site: str, row, owner: str | None = None) -> bool:
    if not (isinstance(row, dict) and row.get("site") == site and row.get("capability") == cap
            and row.get("status") == "granted" and isinstance(row.get("granted_at"), str)
            and bool(row["granted_at"].strip()) and row.get("revoked_at") in (None, "", False)):
        return False
    if cap == "ats_accounts":
        want = owner if owner is not None else _owner_address()
        if not want or str(row.get("email") or "").strip().lower() != want:
            return False
    return True


def capability_active(cap: str, site: str, doc: dict | None = None, cfg: dict | None = None) -> bool:
    """Whether the owner's consent for capability `cap` on `site` is active (a host: row also covers subdomains of
    its host when `site` is host:<sub.host>). Read only; never raises."""
    if cap not in CAPABILITIES:
        return False
    doc = doc if doc is not None else load_consent()
    rows = (doc.get("capabilities") or {}).get(cap) or {}
    s = str(site or "").lower()
    owner = _owner_address(cfg) if cap == "ats_accounts" else None
    if _cap_row_active(cap, s, rows.get(s), owner):
        return True
    if s.startswith("host:"):
        host = s[5:]
        for key, row in rows.items():
            if key.startswith("host:") and host.endswith("." + key[5:]) and _cap_row_active(cap, key, row, owner):
                return True
    return False


def capability_site_of(platform: str | None, host: str | None, doc: dict | None = None) -> str:
    """The consent site of a page: the ATS platform key, else host:<host> (or the host: row that covers it)."""
    from . import accounts
    p = (platform or "").lower()
    if p in accounts.ATS_PLATFORMS:
        return p
    h = (host or "").lower().rstrip(".")
    doc = doc if doc is not None else load_consent()
    for cap in CAPABILITIES:
        for key in ((doc.get("capabilities") or {}).get(cap) or {}):
            if key.startswith("host:") and (h == key[5:] or h.endswith("." + key[5:])):
                return key
    return "host:" + h


def capability_prerequisite(conn, cap: str, cfg: dict | None = None) -> str | None:
    """None when the capability can work on this install, else why not (email_codes needs the gmail site consent
    on route web_ui, or `mail connect` on route app_password)."""
    if cap != "email_codes":
        return None
    if cfg is None:
        from . import config
        cfg = config.load(conn)
    route = (cfg.get("gmail") or {}).get("route")
    if route == "app_password":
        try:
            from . import mail
            return None if mail.is_connected(conn) else "mail_not_connected"
        except Exception:
            return "mail_not_connected"
    return None if consent_active("gmail") else "gmail_not_allowed"


def require_capability(conn, cap: str, site: str, cfg: dict | None = None) -> str:
    """Denied(E_CONSENT_MISSING, data {site, capability}) unless the capability is active for the site and its
    prerequisite holds. Returns the site. Read only."""
    if not capability_active(cap, site, cfg=cfg):
        raise Denied("E_CONSENT_MISSING", "you have not allowed %s on %s; run ./jobhunter browser consent to allow it"
                     % (CAPABILITY_LABEL.get(cap, cap), site), data={"site": site, "capability": cap})
    why = capability_prerequisite(conn, cap, cfg)
    if why:
        raise Denied("E_CONSENT_MISSING", "email codes need Gmail: %s" % (
            "allow the agent's Gmail login (./jobhunter browser consent)" if why == "gmail_not_allowed" else
            "connect the mailbox (./jobhunter mail connect)"), data={"site": site, "capability": cap, "reason": why})
    return site


def _caps_arg(value) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else ([] if value is None else [value])
    out = []
    for item in items:
        for w in str(item).split(","):
            w = w.strip().lower()
            if not w:
                continue
            if w not in CAPABILITIES:
                raise Denied("E_VALIDATION", "unknown capability %s; known: %s" % (w, ", ".join(CAPABILITIES)))
            if w not in out:
                out.append(w)
    return out


def _cap_sites_arg(value) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else ([] if value is None else [value])
    out = []
    for item in items:
        for w in str(item).split(","):
            if w.strip():
                s = valid_capability_site(w)
                if s not in out:
                    out.append(s)
    return out


def _platform_breaker(site: str) -> str | None:
    """ats:<platform> for a platform site (a host: site has no breaker scope of its own)."""
    return None if site.startswith("host:") else "ats:" + site


def grant_capability(conn, caps, sites, by: str = "human:cli", cfg: dict | None = None) -> dict:
    """The owner allows capabilities on sites (the caller checked the PIN). ats_accounts records the current
    owner.gmail_address. A platform breaker open only because the consent was revoked closes. Inside the caller's
    tx."""
    from . import breakers
    caps, sites = _caps_arg(caps), _cap_sites_arg(sites)
    if not caps or not sites:
        raise Denied("E_USAGE", "name at least one --capability and one --site")
    owner = _owner_address(cfg)
    if "ats_accounts" in caps and (not owner or owner in PLACEHOLDERS):
        raise Denied("E_CONFIG_INVALID", "set owner.gmail_address in private/config.json first")
    doc = load_consent(strict=False)
    allcaps = doc.setdefault("capabilities", {})
    ts = now()
    granted = []
    for cap in caps:
        rows = allcaps.setdefault(cap, {})
        for site in sites:
            row = {"site": site, "capability": cap, "status": "granted", "granted_at": ts, "revoked_at": None,
                   "declined_at": None, "by": "owner", "method": "pin"}
            if cap == "ats_accounts":
                row["email"] = owner
            rows[site] = row
            granted.append({"site": site, "capability": cap})
    path = _save_consent(doc)
    closed = []
    for site in sites:
        scope = _platform_breaker(site)
        if scope and _consent_breaker_only(conn, scope) is not None:
            breakers._close(conn, scope, "reset", by, "consent granted again")
            closed.append(scope)
    log_event(conn, "capability_consent", granted=granted, by=by)
    return {"granted": granted, "path": path, "breakers_closed": closed}


def decline_capability(conn, caps, sites, by: str = "human:cli") -> dict:
    """Record the owner's No (the installer's question). An earlier grant becomes inactive."""
    caps, sites = _caps_arg(caps), _cap_sites_arg(sites)
    doc = load_consent(strict=False)
    allcaps = doc.setdefault("capabilities", {})
    ts = now()
    out = []
    for cap in caps:
        rows = allcaps.setdefault(cap, {})
        for site in sites:
            old = rows.get(site) or {}
            rows[site] = {"site": site, "capability": cap, "status": "declined", "granted_at": old.get("granted_at"),
                          "revoked_at": old.get("revoked_at") or (ts if old.get("status") == "granted" else None),
                          "declined_at": ts, "by": "owner", "method": "pin"}
            out.append({"site": site, "capability": cap})
    path = _save_consent(doc)
    log_event(conn, "capability_declined", declined=out, by=by)
    return {"declined": out, "path": path}


def revoke_capability(conn, caps, sites, everything: bool = False, by: str = "human:cli") -> dict:
    """Take capabilities back (default for a site: both). A platform site trips ats:<platform> with
    consent_revoked, so a running cycle stops at once; a later grant closes it again. Inside the caller's tx."""
    from . import breakers
    caps = _caps_arg(caps) or list(CAPABILITIES)
    doc = load_consent(strict=False)
    allcaps = doc.setdefault("capabilities", {})
    if everything:
        sites = sorted({s for c in caps for s, r in (allcaps.get(c) or {}).items() if r.get("status") == "granted"})
    else:
        sites = _cap_sites_arg(sites)
        if not sites:
            raise Denied("E_USAGE", "name a site or use --all")
    ts = now()
    revoked = []
    for cap in caps:
        for site in sites:
            row = (allcaps.get(cap) or {}).get(site)
            if row and row.get("status") == "granted":
                row["status"] = "revoked"
                row["revoked_at"] = ts
                revoked.append({"site": site, "capability": cap})
    path = _save_consent(doc) if revoked else None
    tripped = []
    for site in sorted({r["site"] for r in revoked}):
        scope = _platform_breaker(site)
        if scope:
            breakers.trip(conn, scope, CONSENT_REASON, "consent for %s on %s was revoked; nothing uses it until you "
                          "run ./jobhunter browser consent again" % ("email codes or site accounts", site), by=by)
            tripped.append(scope)
    log_event(conn, "capability_revoked", revoked=revoked, by=by)
    return {"revoked": revoked, "path": path, "breakers_tripped": tripped}


def capability_summary(conn=None, cfg: dict | None = None) -> dict:
    """{capability: [{site, label, state, granted_at, revoked_at}]} for status, the Sheet and the wrapper. state:
    granted, declined, revoked, unavailable (granted but its prerequisite is missing)."""
    doc = load_consent()
    out: dict = {}
    for cap in CAPABILITIES:
        rows = []
        for site, row in sorted(((doc.get("capabilities") or {}).get(cap) or {}).items()):
            if capability_active(cap, site, doc, cfg):
                state = "granted"
                if conn is not None and capability_prerequisite(conn, cap, cfg):
                    state = "unavailable"
            elif row.get("status") == "declined":
                state = "declined"
            elif row.get("status") == "revoked" or row.get("revoked_at"):
                state = "revoked"
            else:
                state = "revoked" if row.get("status") == "granted" else "not_granted"
            rows.append({"site": site, "label": site_label(site), "state": state,
                         "granted_at": row.get("granted_at"), "revoked_at": row.get("revoked_at")})
        out[cap] = rows
    return out


def site_label(site: str) -> str:
    from . import accounts
    if site.startswith("host:"):
        return site[5:]
    return accounts.PLATFORM_LABEL.get(site, site)
