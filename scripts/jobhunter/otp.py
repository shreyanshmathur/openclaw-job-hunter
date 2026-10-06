"""Email verification codes and sign-in links (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.4, 2.5, 2.7) [U1].

A code or link is read only for an open code request of this agent, job and tab (code expect, or implicitly by
account create, account signin and gate arm), only from messages that arrived after the request and within
otp.window_minutes, only from the platform's allowlisted senders (scripts/jobhunter/data/ats_mail.json, after
the hard deny list in code), only when the message names the owner's address, and only when exactly one
candidate of the expected shape remains. The value is typed into the request's tab by code (cdp.py), used once
(code_uses unique indexes on the message, the value and the request) and never stored: only HMAC-SHA256 hashes
keyed with meta otp_salt, the sender domain and times.

Mail from Google, LinkedIn, Microsoft, Apple, Facebook, PayPal or a bank is never read for a code (HARD_DENY,
before the data allowlist), and a message about a password reset, a sign-in attempt or a security alert is
skipped. SMS and authenticator codes are never handled: their pages are tripping stop signatures.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import html.parser
import json
import os
import re
from urllib.parse import urlsplit

from . import db, paths
from .canon import new_uid, now, parse_ts, seconds_between, ts_add
from .errors import Denied
from .events import enqueue_notification, log_event

ALLOWLIST_FILE = os.path.join(paths.REPO, "scripts", "jobhunter", "data", "ats_mail.json")
BANKS_FILE = os.path.join(paths.REPO, "scripts", "jobhunter", "data", "bank_domains.txt")
HARD_DENY = ("google.com", "googlemail.com", "gmail.com", "youtube.com", "linkedin.com", "microsoft.com",
             "microsoftonline.com", "live.com", "outlook.com", "office.com", "apple.com", "icloud.com", "facebook.com",
             "meta.com", "paypal.com")
SECURITY_RE = re.compile(r"password reset|reset your password|sign-in attempt|new sign-in|security alert|2-step", re.I)
KEYWORD_RE = re.compile(r"code|passcode|verification|security code|one-time|otp|\bpin\b", re.I)
LINK_PATH_DENY_RE = re.compile(r"unsubscribe|privacy|preferences|reset|forgot|help|support|terms", re.I)
LINK_TEXT_RE = re.compile(r"verify|confirm|sign in|activate|continue", re.I)
MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", re.I)
URL_RE = re.compile(r"\bhttps?://[^\s<>\"')]+|\bwww\.[^\s<>\"')]+", re.I)
SHAPES = {"digits6": re.compile(r"^[0-9]{6}$"), "alnum8": re.compile(r"^(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9]{8}$"),
          "host": re.compile(r"^[0-9]{4,8}$")}
MAX_SCAN = 8000
REQUEST_STATUSES_OPEN = ("waiting", "found")
FAIL_STATUSES = ("expired", "rejected", "refused")
PURPOSES = ("account_verify", "signin", "application_submit")


# ---------------------------------------------------------------- data
@functools.lru_cache(maxsize=1)
def load_allowlist() -> dict:
    with open(ALLOWLIST_FILE, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    plats = {}
    for key, p in (doc.get("platforms") or {}).items():
        plats[key] = dict(p, _patterns=[re.compile(x, re.I) for x in p.get("link_host_patterns") or []])
    return {"platforms": plats}


@functools.lru_cache(maxsize=1)
def bank_domains() -> frozenset:
    out = set()
    try:
        with open(BANKS_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    out.add(line)
    except OSError:
        pass
    return frozenset(out)


def _under(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


def hard_denied(domain: str) -> bool:
    d = (domain or "").lower().strip(".")
    if not d:
        return True
    if any(_under(d, x) for x in HARD_DENY):
        return True
    if any("bank" in label for label in d.split(".")):
        return True
    return any(_under(d, b) for b in bank_domains())


def domain_of(addr: str | None) -> str:
    a = str(addr or "").strip().lower()
    m = re.search(r"<([^>]+)>", a)
    if m:
        a = m.group(1)
    return a.rsplit("@", 1)[1].strip(".") if "@" in a else ""


def link_host_ok(host: str, platform: str | None, request: dict) -> bool:
    """A link's host: hard deny first; a host: site needs the consented host (or a subdomain); a platform needs a
    listed link host, and for a multi-tenant platform also the request's tenant host (or a subdomain)."""
    h = (host or "").lower().strip(".")
    if not h or hard_denied(h):
        return False
    site = request.get("site") or ""
    if platform is None or site.startswith("host:"):
        return _under(h, site[5:]) if site.startswith("host:") else False
    p = load_allowlist()["platforms"].get(platform)
    if p is None:
        return False
    listed = False
    for x in p.get("link_hosts") or []:
        if (x.startswith("*.") and h.endswith(x[1:])) or h == x:
            listed = True
    if not listed and any(rx.search(h) for rx in p["_patterns"]):
        listed = True
    if not listed:
        return False
    if p.get("multi_tenant") and request.get("host"):
        return _under(h, request["host"])
    return True


def sender_ok(sender_domain: str, platform: str | None, request: dict, company_domains: list, links: list) -> bool:
    """2.5: hard deny, then the platform's listed senders (exact or subdomain); a tenant_sender platform or a
    host: site also accepts the job's company domains when the message links to a listed or tenant host."""
    d = (sender_domain or "").lower()
    if not d or hard_denied(d):
        return False
    site = request.get("site") or ""
    linked = any(link_host_ok(urlsplit(u).hostname or "", platform, request) for u in links)
    if platform is None or site.startswith("host:"):
        return any(_under(d, c) for c in company_domains) and (linked or True)
    p = load_allowlist()["platforms"].get(platform)
    if p is None:
        return False
    if any(_under(d, s) for s in p.get("senders") or []):
        return True
    if p.get("tenant_sender") and any(_under(d, c) for c in company_domains):
        return linked
    return False


def code_shape(platform: str | None, site: str) -> str:
    if platform is None or (site or "").startswith("host:"):
        return "host"
    p = load_allowlist()["platforms"].get(platform) or {}
    return p.get("code") or "digits6"


# ---------------------------------------------------------------- text and extraction (2.7)
class _Html(html.parser.HTMLParser):
    EMPH = ("b", "strong", "code", "h1", "h2", "h3", "big")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self.emph: list = []
        self.links: list = []
        self._skip = 0
        self._em = 0
        self._a = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.EMPH or re.search(r"font-size\s*:\s*(1[8-9]|[2-9][0-9])px|font-size\s*:\s*(x+-)?large",
                                           a.get("style") or "", re.I):
            self._em += 1
        if tag == "a":
            self._a = {"href": a.get("href") or "", "text": ""}
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "td", "table"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.EMPH or tag in ("span", "td", "p", "div"):
            self._em = max(0, self._em - 1) if tag in self.EMPH else self._em
        if tag == "a" and self._a is not None:
            self.links.append(self._a)
            self._a = None
        if tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "table"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._em:
            self.emph.append(data)
        if self._a is not None:
            self._a["text"] += data


def html_to_text(raw: str) -> tuple:
    """(text, emphasized texts, links [{href, text}]) of an HTML body; scripts and styles dropped."""
    p = _Html()
    try:
        p.feed(raw or "")
        p.close()
    except Exception:
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text, p.emph, p.links


def _drop_quoted(text: str) -> str:
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if re.match(r"^on .{4,200} wrote:$", s, re.I) or re.match(r"^-+ ?original message ?-+$", s, re.I):
            break
        if s.startswith(">"):
            continue
        out.append(line)
    return "\n".join(out)


def _excluded_spans(text: str, owner_digits: str | None) -> list:
    spans = []
    for rx in (URL_RE,
               re.compile(r"\b[0-9]{1,4}[/.-][0-9]{1,2}[/.-][0-9]{1,4}\b"),                       # dates
               re.compile(r"\b[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?\b"),                                  # times
               re.compile(r"(?<![0-9A-Za-z])\+?[0-9][0-9 ().-]{7,}[0-9](?![0-9A-Za-z])"),           # phone shapes
               re.compile(r"\b[0-9]{1,6} [A-Za-z][A-Za-z .]{2,40}\b(street|st|road|rd|avenue|ave|lane|ln|drive|dr|"
                          r"boulevard|blvd|suite|floor)\b", re.I)):
        for m in rx.finditer(text):
            if rx is not URL_RE and re.fullmatch(r"[0-9]{4,10}", m.group(0).strip()):
                continue                                                           # a bare number is not a phone
            spans.append((m.start(), m.end()))
    if owner_digits and len(owner_digits) >= 6:
        for m in re.finditer(re.escape(owner_digits[-6:]), re.sub(r"[^0-9]", "#", text)):
            spans.append((m.start(), m.end()))
    return spans


def extract(message: dict, platform: str | None, request: dict, req_ids: tuple = (), owner_digits: str | None = None,
            company_domains: list | None = None) -> dict | None:
    """The one code or link a message carries for this request, or None. Raises ValueError('ambiguous') when two
    or more distinct candidates of the wanted kind remain (the request is refused, the failure counted)."""
    plain = message.get("text") or ""
    emph: list = []
    links = [dict(x) for x in message.get("links") or [] if isinstance(x, dict)]
    if message.get("html"):
        htext, emph, hlinks = html_to_text(message["html"])
        links += hlinks
        if not plain.strip():
            plain = htext
    text = (str(message.get("subject") or "") + "\n" + _drop_quoted(plain))[:MAX_SCAN]
    for m in URL_RE.finditer(text):
        links.append({"href": m.group(0).rstrip(".,;"), "text": text[max(0, m.start() - 80):m.start()]})
    want = request.get("want") or "either"
    codes: list = []
    if want in ("code", "either"):
        shape = SHAPES.get(code_shape(platform, request.get("site") or ""))
        if shape is not None:
            spans = _excluded_spans(text, owner_digits)
            emph_tokens = set()
            for e in emph:
                emph_tokens.update(re.findall(r"[A-Za-z0-9]+", e))
            for m in re.finditer(r"(?<![A-Za-z0-9])[A-Za-z0-9]{4,10}(?![A-Za-z0-9])", text):
                tok = m.group(0)
                if not shape.match(tok):
                    continue
                if any(a <= m.start() < b or a < m.end() <= b for a, b in spans):
                    continue
                if tok in req_ids:
                    continue
                if re.fullmatch(r"(19|20)[0-9]{2}", tok) and MONTH_RE.search(text[max(0, m.start() - 20):m.end() + 20]):
                    continue
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                line = text[line_start:line_end if line_end >= 0 else len(text)].strip()
                before = text[max(0, m.start() - 80):m.start()]
                if line == tok or tok in emph_tokens or KEYWORD_RE.search(before):
                    if tok not in codes:
                        codes.append(tok)
    if len(codes) > 1:
        raise ValueError("ambiguous")
    if codes:
        return {"kind": "code", "value": codes[0], "link_host": None}
    if want in ("link", "either"):
        urls = []
        for ln in links:
            href = (ln.get("href") or "").strip()
            try:
                sp = urlsplit(href)
            except ValueError:
                continue
            if sp.scheme != "https" or not sp.hostname:
                continue
            if not link_host_ok(sp.hostname, platform, request):
                continue
            if LINK_PATH_DENY_RE.search(sp.path or ""):
                continue
            if not LINK_TEXT_RE.search((ln.get("text") or "") + " " + (sp.path or "")):
                continue
            if href not in urls:
                urls.append(href)
        if len(urls) > 1:
            raise ValueError("ambiguous")
        if urls:
            return {"kind": "link", "value": urls[0], "link_host": urlsplit(urls[0]).hostname.lower()}
    return None


# ---------------------------------------------------------------- hashes
def salt(conn) -> bytes:
    v = db.meta_get(conn, "otp_salt")
    if not v:
        raise Denied("E_CONFIG_INVALID", "meta otp_salt is missing; run ./jobhunter doctor")
    return v.encode("ascii")


def hmac_of(conn, value: str) -> str:
    return hmac.new(salt(conn), (value or "").encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- requests
def _req_row(conn, request_uid: str):
    if not re.match(r"^O[A-Z2-7]{7}$", request_uid or ""):
        raise Denied("E_NOT_FOUND", "not a code request id")
    row = conn.execute("SELECT * FROM code_requests WHERE request_uid = ?", (request_uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no code request %s" % request_uid)
    return row


def route_of(cfg: dict) -> str:
    return "app_password" if (cfg.get("gmail") or {}).get("route") == "app_password" else "web_ui"


def open_request(conn, *, step=None, platform=None, site=None, host=None, tenant=None, purpose: str, want: str,
                 job_id=None, action_id=None, account_id=None, tab_id=None, agent_id=None, cycle_id=None,
                 cfg: dict | None = None, requested_at: str | None = None) -> dict:
    """Insert a waiting request (inside the caller's tx). A waiting request on the same tab is cancelled first."""
    from . import config as _config
    if step is not None:
        platform, site, host, tenant = step.platform, step.site, getattr(step, "host", None) or step.job_host, \
            getattr(step, "tenant", None)
        job_id, tab_id, agent_id, cycle_id = step.job_id, step.tab_id, step.agent_id, step.cycle_id
        action_id = step.action["id"] if step.action is not None else action_id
        cfg = step.cfg
    cfg = cfg or _config.load(conn)
    if purpose not in PURPOSES or want not in ("code", "link", "either"):
        raise Denied("E_VALIDATION", "bad code request purpose or kind")
    ts = requested_at or now()
    for r in conn.execute("SELECT id FROM code_requests WHERE tab_id = ? AND status IN ('waiting','found')",
                          (tab_id,)).fetchall():
        cancel_request(conn, r["id"], "superseded")
    uid = new_uid("O")
    window = min(10, int(cfg["otp"]["window_minutes"]))
    cur = conn.execute(
        "INSERT INTO code_requests (request_uid, platform, site, host, tenant, purpose, want, job_id, action_id, account_id, "
        "tab_id, route, agent_id, cycle_id, requested_at, window_ends_at, status, created_at, updated_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)",
        (uid, platform or "host", site, host or "", tenant, purpose, want, job_id, action_id, account_id, tab_id,
         route_of(cfg), agent_id or "", cycle_id, ts, ts_add(ts, minutes=window), now(), now()))
    log_event(conn, "code_request", request_uid=uid, site=site, purpose=purpose)
    return dict(conn.execute("SELECT * FROM code_requests WHERE id = ?", (cur.lastrowid,)).fetchone())


def cancel_request(conn, request_id: int, reason: str = "cancelled") -> None:
    ts = now()
    conn.execute("UPDATE code_requests SET status = 'cancelled', reason = ?, resolved_at = ?, updated_at = ? "
                 "WHERE id = ? AND status IN ('waiting','found')", (reason[:40], ts, ts, request_id))


def end_request(conn, request_id: int, status: str, reason: str) -> None:
    ts = now()
    conn.execute("UPDATE code_requests SET status = ?, reason = ?, resolved_at = ?, updated_at = ? WHERE id = ?",
                 (status, (reason or "")[:40] or None, ts, ts, request_id))


def failure(conn, req, cfg: dict, why: str) -> None:
    """Count a failed request for its site; at otp.failure_breaker_24h trip ats:<platform> (otp_failures)."""
    n = conn.execute("SELECT count(*) FROM code_requests WHERE site = ? AND status IN ('expired','rejected','refused') "
                     "AND updated_at > ?", (req["site"], ts_add(now(), days=-1))).fetchone()[0]
    log_event(conn, "code_request_failed", request_uid=req["request_uid"], site=req["site"], why=why, failures_24h=n)
    if req["platform"] != "host" and n >= int(cfg["otp"]["failure_breaker_24h"]):
        from . import breakers
        breakers.trip(conn, "ats:" + req["platform"], "otp_failures",
                      "%d email code failures for %s in 24 hours" % (n, req["site"]), by="code",
                      cycle_id=req["cycle_id"])


def expire_waiting(conn, cfg: dict | None = None) -> list:
    """Waiting requests past their window end: expired, failure counted (housekeeping, dispatch tick)."""
    from . import config as _config
    cfg = cfg or _config.load(conn)
    out = []
    for r in conn.execute("SELECT * FROM code_requests WHERE status IN ('waiting','found') AND window_ends_at < ?",
                          (now(),)).fetchall():
        end_request(conn, r["id"], "expired", "window_expired")
        failure(conn, r, cfg, "window_expired")
        out.append(r["request_uid"])
    return out


def uses_caps(conn, cfg: dict, site: str, platform=None) -> None:
    """otp.max_uses_day_per_site and otp.max_uses_day over the rolling 24 hours (E_CEILING with retry_after_s)."""
    from .accounts import retry_after
    o = cfg["otp"]
    since = ts_add(now(), days=-1)
    per = conn.execute("SELECT count(*), min(used_at) FROM code_uses WHERE site = ? AND used_at > ?",
                       (site, since)).fetchone()
    if per[0] >= int(o["max_uses_day_per_site"]):
        raise Denied("E_CEILING", "email codes on %s: %d in 24 hours (limit %d)" % (site, per[0],
                     int(o["max_uses_day_per_site"])), retry_after=retry_after(per[1], 86400), data={"window": "site_day"})
    tot = conn.execute("SELECT count(*), min(used_at) FROM code_uses WHERE used_at > ?", (since,)).fetchone()
    if tot[0] >= int(o["max_uses_day"]):
        raise Denied("E_CEILING", "email codes: %d in 24 hours (limit %d)" % (tot[0], int(o["max_uses_day"])),
                     retry_after=retry_after(tot[1], 86400), data={"window": "day"})


# ---------------------------------------------------------------- the mailbox read
def _floor_minute(ts: str) -> str:
    return ts[:17] + "00Z"


def in_window(received_at: str | None, req, minute_precision: bool = False) -> bool:
    if not received_at:
        return False
    try:
        parse_ts(received_at)
    except ValueError:
        return False
    start = _floor_minute(req["requested_at"]) if minute_precision else req["requested_at"]
    return start <= received_at <= req["window_ends_at"]


def senders_query(platform: str | None, company_domains: list) -> list:
    if platform is None:
        return list(company_domains)
    p = load_allowlist()["platforms"].get(platform) or {}
    out = list(p.get("senders") or [])
    if p.get("tenant_sender"):
        out += [d for d in company_domains if d not in out]
    return [d for d in out if not hard_denied(d)]


def gmail_query(platform: str | None, company_domains: list) -> str:
    doms = senders_query(platform, company_domains)
    return "from:(%s) newer_than:1d" % " OR ".join(doms) if doms else ""


def select_candidate(conn, req, messages: list, cfg: dict, minute_precision: bool) -> dict | None:
    """Newest first: the first message that passes every rule and carries exactly one candidate. Raises
    ValueError('ambiguous') for a message with two or more candidates."""
    from .accounts import company_domains
    owner = str((cfg.get("owner") or {}).get("gmail_address") or "").strip().lower()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (req["job_id"],)).fetchone()
    doms = company_domains(conn, job["company_id"]) if job is not None else []
    req_ids = tuple(x for x in (job["native_id"] if job is not None and "native_id" in job.keys() else None,) if x)
    digits = re.sub(r"[^0-9]", "", str(((cfg.get("owner") or {}).get("signature") or {}).get("phone") or ""))
    platform = None if req["platform"] == "host" else req["platform"]
    msgs = sorted(messages, key=lambda m: m.get("received_at") or "", reverse=True)
    used_hit = False
    for m in msgs:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        mh = hmac_of(conn, mid)
        if conn.execute("SELECT 1 FROM code_uses WHERE message_hmac = ?", (mh,)).fetchone():
            # a used message that arrived after this request (two requests saw it): refused, not waited for;
            # an older one belonged to an earlier request and is simply passed over
            used_hit = used_hit or (in_window(m.get("received_at"), req, False) and
                                    m.get("received_at") > req["requested_at"])
            continue
        rcpts = [str(x).strip().lower() for x in (m.get("to") or [])]
        if owner not in rcpts and not any(owner in r for r in rcpts):
            continue
        sdom = domain_of(m.get("from"))
        links = [x.get("href") for x in m.get("links") or [] if isinstance(x, dict) and x.get("href")]
        if not sender_ok(sdom, platform, dict(req), doms, links):
            continue
        body = " ".join(str(m.get(k) or "") for k in ("subject", "text", "html"))
        if SECURITY_RE.search(body):
            continue
        if not in_window(m.get("received_at"), req, minute_precision):
            continue
        cand = extract(m, platform, dict(req), req_ids=req_ids, owner_digits=digits or None, company_domains=doms)
        if cand is None:
            continue
        cand.update(message_id=mid, message_hmac=mh, sender_domain=sdom, received_at=m.get("received_at"),
                    uid=m.get("uid"), thread_url=m.get("thread_url"))
        return cand
    if used_hit:
        raise Denied("E_ALREADY_DONE", "the only matching email was used before; a code is used once",
                     data={"reason": "message_used"})
    return None


def find(conn, req, cfg: dict) -> dict | None:
    """Read the mailbox on the request's route for a candidate (None: nothing yet)."""
    from .accounts import company_domains
    job = conn.execute("SELECT company_id FROM jobs WHERE id = ?", (req["job_id"],)).fetchone()
    doms = company_domains(conn, job["company_id"]) if job is not None else []
    platform = None if req["platform"] == "host" else req["platform"]
    query = gmail_query(platform, doms)
    if not query:
        return None
    if req["route"] == "app_password":
        from .mail import codes
        msgs = codes.search(conn, query, req["requested_at"], req["window_ends_at"])
        minute = False
    else:
        from .mail import webcodes
        msgs = webcodes.search(conn, query, req["requested_at"])
        minute = True
    return select_candidate(conn, req, msgs, cfg, minute)


def after_use(conn, req, cand: dict, cfg: dict) -> None:
    """otp.after_use: leave, mark_read or archive the used message (best effort)."""
    mode = (cfg.get("otp") or {}).get("after_use") or "mark_read"
    if mode == "leave":
        return
    try:
        if req["route"] == "app_password":
            from .mail import codes
            codes.after_use(cand.get("uid"), mode)
        else:
            from .mail import webcodes
            webcodes.after_use(cand.get("thread_url"), mode)
    except Exception:
        pass


# ---------------------------------------------------------------- the agent's commands
def expect(conn, *, agent_id: str, cycle_id: str | None, job_uid: str, tab_id: str, purpose: str,
           token: str | None) -> dict:
    from . import accounts
    step = accounts.prepare(conn, agent_id=agent_id, cycle_id=cycle_id, job_uid=job_uid, tab_id=tab_id, token=token,
                            capabilities=("email_codes",), caps_check=uses_caps)
    session, _t = accounts.open_tab(step)
    session.close()
    with db.tx(conn):
        req = open_request(conn, step=step, purpose=purpose, want="either")
    return {"request_uid": req["request_uid"], "requested_at": req["requested_at"],
            "window_ends_at": req["window_ends_at"], "route": req["route"]}


def cancel(conn, request_uid: str, agent_id: str | None) -> dict:
    with db.tx(conn):
        req = _req_row(conn, request_uid)
        if agent_id and req["agent_id"] != agent_id:
            raise Denied("E_NOT_FOUND", "the request belongs to another agent")
        cancel_request(conn, req["id"], "cancelled")
        st = conn.execute("SELECT status FROM code_requests WHERE id = ?", (req["id"],)).fetchone()[0]
    return {"request_uid": request_uid, "status": st}


def _step_for_request(conn, req, agent_id: str, cycle_id: str | None, tab_id: str, cap_check=True):
    from . import accounts
    if req["agent_id"] != agent_id:
        raise Denied("E_NOT_FOUND", "the request belongs to another agent")
    if req["tab_id"] != tab_id:
        raise Denied("E_VALIDATION", "the request was made for another tab", data={"reason": "wrong_tab"})
    if req["status"] not in REQUEST_STATUSES_OPEN:
        raise Denied("E_PRECONDITION", "the request is %s" % req["status"], data={"reason": "request_" + req["status"]})
    job = conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (req["job_id"],)).fetchone()
    tok = None
    if req["action_id"]:
        a = conn.execute("SELECT token, status FROM actions WHERE id = ?", (req["action_id"],)).fetchone()
        tok = a["token"] if a is not None and a["status"] in ("reserved", "armed") else None
    step = accounts.prepare(conn, agent_id=agent_id, cycle_id=cycle_id, job_uid=job["job_uid"], tab_id=tab_id,
                            token=tok, capabilities=("email_codes",), caps_check=uses_caps)
    if step.site != req["site"]:
        raise Denied("E_VALIDATION", "the request is for another site", data={"reason": "wrong_site"})
    return step


def _expired(conn, req, cfg) -> None:
    def _end(c, rid=req["id"]):
        r = c.execute("SELECT * FROM code_requests WHERE id = ?", (rid,)).fetchone()
        if r["status"] in REQUEST_STATUSES_OPEN:
            end_request(c, rid, "expired", "window_expired")
            failure(c, r, cfg, "window_expired")
    db.defer_write(conn, _end)
    raise Denied("E_PRECONDITION", "no matching email arrived in time; the job goes to you",
                 data={"reason": "window_expired"})


def submit(conn, *, request_uid: str, agent_id: str, cycle_id: str | None, tab_id: str, link: bool = False) -> dict:
    """code submit / code open-link: PENDING while nothing matching arrived; then type the code (or open the link)
    on the request's tab, once."""
    from . import accounts, cdp, pagefill
    from .commands import Result
    from .secretstore import Secret
    req = _req_row(conn, request_uid)
    step = _step_for_request(conn, req, agent_id, cycle_id, tab_id)
    cfg = step.cfg
    if now() > req["window_ends_at"]:
        with db.tx(conn):
            _expired(conn, req, cfg)
    try:
        cand = find(conn, req, cfg)
    except ValueError:
        with db.tx(conn):
            end_request(conn, req["id"], "refused", "ambiguous")
            failure(conn, req, cfg, "ambiguous")
        raise Denied("E_PRECONDITION", "the email held more than one possible code or link; the job goes to you",
                     data={"reason": "ambiguous"})
    if cand is None:
        waited = max(0, seconds_between(req["requested_at"], now()))
        return Result(data={"request_uid": request_uid, "waited_s": waited, "window_ends_at": req["window_ends_at"]},
                      code="PENDING", message="no matching email yet; ask again in %d s" % int(cfg["otp"]["poll_seconds"]),
                      retry_after_s=int(cfg["otp"]["poll_seconds"]), next="run the same command again after retry_after_s")
    if link and cand["kind"] != "link":
        cand = None
    if not link and cand["kind"] != "code":
        raise Denied("E_PRECONDITION", "the email holds a sign-in link, not a code: use code open-link",
                     data={"reason": "is_link"})
    if cand is None:
        raise Denied("E_PRECONDITION", "the email holds a code, not a link: use code submit", data={"reason": "is_code"})
    value = Secret(cand.pop("value"))
    vh = hmac_of(conn, value.reveal())
    # claim the message and the value before the page is touched: a second use is impossible (unique indexes)
    with db.tx(conn):
        if conn.execute("SELECT 1 FROM code_uses WHERE value_hmac = ? OR message_hmac = ? OR request_id = ?",
                        (vh, cand["message_hmac"], req["id"])).fetchone():
            raise Denied("E_ALREADY_DONE", "this code or message was used before", data={"reason": "used"})
        cur = conn.execute("INSERT INTO code_uses (request_id, kind, value_hmac, message_hmac, sender_domain, "
                           "received_at, link_host, outcome, used_at, site, job_id, cycle_id) VALUES "
                           "(?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?)",
                           (req["id"], cand["kind"], vh, cand["message_hmac"], cand["sender_domain"],
                            cand["received_at"], cand.get("link_host"), now(), req["site"], req["job_id"],
                            req["cycle_id"]))
        use_id = cur.lastrowid
        conn.execute("UPDATE code_requests SET status = 'found', updated_at = ? WHERE id = ?", (now(), req["id"]))
    session, _t = accounts.open_tab(step)
    with session:
        with db.tx(conn):
            pre = accounts.scan(conn, step, session, "before")
        if pre.get("captcha"):
            with db.tx(conn):
                conn.execute("UPDATE code_uses SET outcome = 'unknown' WHERE id = ?", (use_id,))
                end_request(conn, req["id"], "cancelled", "captcha")
            return {"outcome": "captcha", "captcha_code": pre["captcha"]["code"]}
        if link:
            outcome, detail = _open_link(session, step, value, cand)
        else:
            outcome, detail = _type_code(conn, session, step, req, value)
        del value
        with db.tx(conn):
            post = accounts.scan(conn, step, session, "after")
            if post.get("captcha"):
                outcome = "captcha"
            use_out = {"accepted": "accepted", "opened": "accepted", "rejected": "rejected", "captcha": "unknown",
                       "unknown": "unknown"}[outcome]
            conn.execute("UPDATE code_uses SET outcome = ? WHERE id = ?", (use_out, use_id))
            if outcome in ("accepted", "opened"):
                end_request(conn, req["id"], "used", None)
                if req["account_id"]:
                    ts = now()
                    conn.execute("UPDATE ats_accounts SET status = CASE WHEN status = 'pending_verify' THEN 'active' "
                                 "ELSE status END, verified_at = COALESCE(verified_at, ?), last_used_at = ?, "
                                 "updated_at = ? WHERE id = ?", (ts, ts, ts, req["account_id"]))
            elif outcome == "rejected":
                end_request(conn, req["id"], "rejected", detail or "rejected")
                failure(conn, req, cfg, "rejected")
            else:
                end_request(conn, req["id"], "used", outcome)
            accounts.step_log(conn, step, "link_open" if link else "code_fill",
                              {"accepted": "ok", "opened": "ok", "rejected": "rejected", "captcha": "captcha",
                               "unknown": "unknown"}[outcome], detail or outcome, request_id=req["id"])
            what = "sign-in link" if link else "email code"
            enqueue_notification(conn, "code_use:%d" % use_id, "low", "info",
                                 "Used an %s from %s for %s, %s." % (what, cand["sender_domain"], step.company,
                                                                     step.title))
            log_event(conn, "code_used", request_uid=request_uid, site=req["site"], value_kind=cand["kind"],
                      outcome=outcome, sender_domain=cand["sender_domain"])
        after_use(conn, req, cand, cfg)
    out = {"request_uid": request_uid, "outcome": outcome, "sender_domain": cand["sender_domain"],
           "received_at": cand["received_at"]}
    if link:
        out["link_host"] = cand.get("link_host")
    if outcome == "captcha":
        out["captcha_code"] = post["captcha"]["code"]
    return out


def _type_code(conn, session, step, req, value) -> tuple:
    from . import accounts, gate, pagefill
    form = pagefill.read_form(session)
    cf = pagefill.find_code_field(form)
    if cf is None:
        return "unknown", "no_code_field"
    fields = cf["fields"]
    if cf["kind"] == "split":
        v = value.reveal()
        if len(v) != len(fields):
            return "unknown", "split_length"
        from .secretstore import Secret
        for f, ch in zip(fields, v):
            pagefill.fill(session, f, Secret(ch))
        v = None
    else:
        pagefill.fill(session, fields[0], value)
    form = pagefill.read_form(session)
    step_name = "resubmit" if req["purpose"] == "application_submit" else "verify"
    button = pagefill.find_button(form, step_name) or (pagefill.find_button(form, "verify") if step_name == "resubmit"
                                                       else None)
    before = session.page_text()
    if req["purpose"] == "application_submit":
        # the resubmit button is a commit: the token must be armed, the dwell over and a commit left
        a = gate.action_by_token(conn, step.token) if step.token else None
        if a is None or a["status"] != "armed":
            for f in fields:
                pagefill.clear(session, f)
            raise Denied("E_PRECONDITION", "the resubmit needs the armed application token", data={"reason": "not_armed"})
        dwell = int(step.cfg["browser"]["dwell_seconds"][0])
        if seconds_between(a["armed_at"], now()) < dwell or gate.commit_count(step.token) + 1 > 2:
            for f in fields:
                pagefill.clear(session, f)
            raise Denied("E_PRECONDITION", "the resubmit is not allowed now (dwell or commit budget)",
                         data={"reason": "commit_budget"})
        accounts.token_line(step.token, "commit", "code_resubmit", step.host, (button or {}).get("name"))
    else:
        accounts.token_line(step.token, "fill", "code_fill", step.host, (button or {}).get("name"))
    if button is not None:
        pagefill.press_button(session, button)
    else:
        session.press("Enter")
    after = accounts._wait_change(session, before)
    form2 = pagefill.read_form(session)
    still = pagefill.find_code_field(form2)
    err = re.search(r"(incorrect|invalid|wrong|expired|does not match|try again).{0,40}(code)?|code.{0,40}"
                    r"(incorrect|invalid|wrong|expired)", after["text"], re.I)
    if still is not None and err:
        for f in still["fields"]:
            pagefill.clear(session, f)
        return "rejected", "code_rejected"
    if still is None and not err:
        return "accepted", None
    if still is not None:
        for f in still["fields"]:
            pagefill.clear(session, f)
    return "unknown", "page_unclear"


def _open_link(session, step, value, cand) -> tuple:
    from . import accounts
    host = cand.get("link_host") or ""
    if step.platform is not None and accounts.classify_host(host)["platform"] != step.platform:
        return "rejected", "link_host"
    before = session.page_text()
    session.navigate(value)
    after = accounts._wait_change(session, before)
    ah = accounts.host_of(after["url"])
    cls = accounts.classify_host(ah, after["url"])
    ok_host = (cls["platform"] == step.platform) if step.platform else (ah == step.site[5:] or
                                                                        ah.endswith("." + step.site[5:]))
    err = re.search(r"(link|token).{0,40}(expired|invalid|no longer valid)|something went wrong|error", after["text"], re.I)
    if ok_host and not cls["never"] and not err:
        return "opened", None
    return "rejected", "link_page"
