"""Stop-signature detection (design 4.5, 12.11).

The signature files next to this module (linkedin.json, gmail.json, boards.json, ats.json, smtp.json) are
read by this module and by the guard plugin (U8). Each signature names one matcher (`url`, `title`,
`text`, `smtp` regex, or `http_status` list); regexes are matched case-insensitively and use only syntax
that Python and JavaScript read the same way. A `stop` signature with `trip: true` trips the breaker of
its reason_code (design 4.5 table) in the same transaction and queues a high alert.

detect(conn, payload, source, cycle_id) records one `detections` row and returns
{detect_id, verdict, code, matched, scope, tripped, job_needs_human}.
"""
from __future__ import annotations

import functools
import json
import os
import re

from ..errors import Denied

DETECT_DIR = os.path.dirname(os.path.abspath(__file__))
FILES = ("linkedin.json", "gmail.json", "boards.json", "ats.json", "smtp.json")
PAYLOAD_KEYS = ("platform", "url", "title", "http_status", "text")
# keys only the guard's stop file may carry, for the CAPTCHA hand-off (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.6)
GUARD_KEYS = {"agent": re.compile(r"^jobhunter-[a-z]{2,20}$"), "token": re.compile(r"^T[A-Z2-7]{11}$"),
              "tab_id": re.compile(r"^[A-Za-z0-9_-]{1,64}$")}
CAPABILITY_FLOWS = {"ats_accounts": "account", "email_codes": "email_code"}
MAX_TEXT = 20000
PLATFORM_RE = re.compile(r"^[a-z0-9_.:-]{2,40}$")


@functools.lru_cache(maxsize=None)
def load(name: str) -> dict:
    if name not in FILES:
        raise Denied("E_INTERNAL", "unknown signature file %r" % name)
    with open(os.path.join(DETECT_DIR, name), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for sig in data.get("signatures", []) + data.get("error_after_click", []):
        if "capability" in sig and (sig.get("trip") is not False or sig.get("verdict", "stop") != "stop"
                                    or sig["capability"] not in CAPABILITY_FLOWS):
            # no data edit may turn a tripping stop off: a capability on a tripping signature is a load error
            raise Denied("E_INTERNAL", "signature %s in %s: capability is allowed only on a job-level stop"
                         % (sig.get("id"), name))
        if "handoff" in sig and sig["handoff"] != "captcha":
            raise Denied("E_INTERNAL", "signature %s in %s: unknown handoff" % (sig.get("id"), name))
        for k in ("url", "title", "text", "smtp"):
            if k in sig:
                sig["_" + k] = re.compile(sig[k], re.I)
    return data


def file_for(platform: str) -> str:
    from ..keys import ATS_NAMES
    p = (platform or "").lower()
    if p == "linkedin":
        return "linkedin.json"
    if p == "gmail":
        return "gmail.json"
    if p == "ats" or p in ATS_NAMES or p == "site:ats_forms":
        return "ats.json"
    return "boards.json"


def _scope_for(platform: str, sig: dict, url: str | None = None) -> str:
    """The breaker a tripping signature opens: the policy's fixed scope, else the platform's scope; on a job form
    that is ats:<platform> when the platform (or the page host) names one ATS (FEATURES-OTP-ACCOUNTS-CAPTCHA 1.4)."""
    from .. import breakers
    pol = breakers.POLICIES.get(sig.get("reason_code") or "", {})
    if pol.get("scope"):
        return pol["scope"]
    scope = breakers.platform_scope(platform)
    if scope == "ats":
        key = breakers.ats_platform_key(platform)
        if key is None and url:
            from .. import accounts
            cls = accounts.classify_host(accounts.host_of(url), url)
            key = cls["platform"] if cls["platform"] and breakers.ats_platform_key(cls["platform"]) else None
        if key:
            return "ats:" + key
    return scope


# Stop reasons from most to least severe (4.5 table: longer cooldowns, manual-only weeks and warm-up restarts
# first). When a page matches several signatures the most severe one decides: LinkedIn serves restriction
# and identity pages under /checkpoint/, which must not be downgraded to a plain challenge.
SEVERITY = ("li_restricted", "li_identity_mismatch", "li_challenge", "li_security_email", "li_logged_out",
            "li_invite_limit", "li_easy_apply_limit", "li_messaging_blocked", "li_email_needed",
            "li_commercial_limit", "li_http_429", "li_unknown_modal",
            "gmail_security", "gmail_auth_failed", "gmail_identity_mismatch", "gmail_logged_out",
            "gmail_sending_limit", "gmail_unexpected_state", "ats_security", "ats_blocked", "site_challenge",
            "site_logged_out")


def _rank(sig: dict, index: int) -> tuple:
    """Sort key (smaller wins): a tripping stop before a job-level stop before other verdicts, then the
    severity of its reason, then file order."""
    verdict = sig.get("verdict", "stop")
    rc = sig.get("reason_code") or ""
    sev = SEVERITY.index(rc) if rc in SEVERITY else len(SEVERITY)
    return (0 if verdict == "stop" else 1, 0 if sig.get("trip") else 1, sev, index)


def match(payload: dict) -> tuple[str, dict | None]:
    """(verdict, signature) for a page payload; ('clear', None) when nothing matches. Every signature is
    tried and the most severe hit wins (SEVERITY), not the first one in file order."""
    data = load(file_for(payload.get("platform") or ""))
    url = payload.get("url") or ""
    title = payload.get("title") or ""
    text = (payload.get("text") or "")[:MAX_TEXT]
    status = payload.get("http_status")
    hits = []
    for i, sig in enumerate(data.get("signatures", [])):
        hit = False
        if "_url" in sig and url and sig["_url"].search(url):
            hit = True
        elif "_title" in sig and title and sig["_title"].search(title):
            hit = True
        elif "_text" in sig and text and sig["_text"].search(text):
            hit = True
        elif "http_status" in sig and isinstance(status, int) and status in sig["http_status"]:
            hit = True
        if hit:
            hits.append((_rank(sig, i), sig))
    if hits:
        sig = min(hits, key=lambda h: h[0])[1]
        return sig.get("verdict", "stop"), sig
    return "clear", None


def error_after_click(platform: str, text: str | None) -> str | None:
    """Signature id when a note shows a platform error after the click (gate unknown), else None."""
    if not text:
        return None
    data = load(file_for(platform))
    for sig in data.get("error_after_click", []):
        if "_text" in sig and sig["_text"].search(text[:MAX_TEXT]):
            return sig["id"]
    return None


def smtp_signature(reply: str | None) -> dict | None:
    """smtp.json signature matching '<code> <text>' of an SMTP or IMAP reply (U9 mailer), else None."""
    if not reply:
        return None
    for sig in load("smtp.json").get("signatures", []):
        if "_smtp" in sig and sig["_smtp"].search(reply):
            return {k: v for k, v in sig.items() if not k.startswith("_")}
    return None


def validate_payload(payload, source: str = "agent") -> dict:
    if not isinstance(payload, dict):
        raise Denied("E_SCHEMA", "detect file must hold a JSON object")
    allowed = PAYLOAD_KEYS + (tuple(GUARD_KEYS) if source == "guard" else ())
    unknown = [k for k in payload if k not in allowed]
    if unknown:
        raise Denied("E_SCHEMA", "unknown keys in detect file: %s" % ", ".join(sorted(unknown)))
    platform = payload.get("platform")
    if not isinstance(platform, str) or not PLATFORM_RE.match(platform):
        raise Denied("E_SCHEMA", "platform must be a platform or site:<name> id")
    out = {"platform": platform}
    for k in ("url", "title", "text"):
        v = payload.get(k)
        if v is not None and not isinstance(v, str):
            raise Denied("E_SCHEMA", "%s must be a string" % k)
        out[k] = v
    st = payload.get("http_status")
    if st is not None and (isinstance(st, bool) or not isinstance(st, int)):
        raise Denied("E_SCHEMA", "http_status must be an integer or null")
    out["http_status"] = st
    if source == "guard":
        for k, rx in GUARD_KEYS.items():
            v = payload.get(k)
            if v is None:
                continue
            if not isinstance(v, str) or not rx.match(v):
                raise Denied("E_SCHEMA", "%s has the wrong shape" % k)
            out[k] = v
    out["truncated"] = bool(out.get("text") and len(out["text"]) > MAX_TEXT)
    if out.get("text"):
        out["text"] = out["text"][:MAX_TEXT]
    return out


def detect(conn, payload: dict, source: str, cycle_id: str | None = None, job_id=None, token=None, agent_id=None,
           tab_id=None, site_hint: str | None = None, open_captcha: bool = True) -> dict:
    """Record one detection; a tripping stop signature trips its breaker in the same transaction."""
    from .. import breakers
    from ..canon import now
    from ..events import log_event
    if source not in ("agent", "guard", "code"):
        raise Denied("E_VALIDATION", "detect source must be agent, guard or code")
    p = validate_payload(payload, source)
    verdict, sig = match(p)
    flow = None
    if sig and verdict == "stop" and not sig.get("trip") and sig.get("capability"):
        site = page_site(p, site_hint)
        if _capability_on(conn, sig["capability"], site):
            flow = CAPABILITY_FLOWS[sig["capability"]]
            verdict = "clear"
    code = sig.get("reason_code") or sig.get("id") if sig else None
    if flow:
        code = "flow_" + flow
    stored_url = p.get("url") or ""
    if source == "code":
        stored_url = stored_url.split("?", 1)[0].split("#", 1)[0]    # a sign-in link's token is never stored
    cur = conn.execute(
        "INSERT INTO detections (platform, source, url, http_status, verdict, code, matched, cycle_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (p["platform"], source, stored_url[:2000] or None, p.get("http_status"), verdict, code,
         sig["id"] if sig else None, cycle_id, now()))
    detect_id = cur.lastrowid
    out = {"detect_id": detect_id, "verdict": verdict, "code": code, "matched": sig["id"] if sig else None,
           "scope": None, "tripped": False,
           "job_needs_human": sig.get("job_needs_human") if sig and verdict == "stop" else None}
    if flow:
        out["flow"] = flow
    if sig and verdict == "stop" and sig.get("trip"):
        scope = _scope_for(p["platform"], sig, p.get("url"))
        detail = "seen on %s" % _where(p)          # the URL and signature id stay in the evidence file
        snapshot = "platform: %s\nurl: %s\ntitle: %s\nhttp_status: %s\nsignature: %s\n\n%s" % (
            p["platform"], p.get("url") or "", p.get("title") or "", p.get("http_status"), sig["id"],
            p.get("text") or "")
        ev = breakers._evidence(scope, snapshot)
        breakers.trip(conn, scope, sig["reason_code"], detail, evidence_path=ev, by="detect:" + source,
                      cycle_id=cycle_id)
        out.update(scope=scope, tripped=True)
    elif sig and verdict == "stop" and sig.get("handoff") == "captcha" and open_captcha:
        from .. import captcha
        task = captcha.handoff_from_detect(conn, p, source, cycle_id, job_id=job_id, token=token,
                                           agent_id=agent_id, tab_id=tab_id)
        if task:
            out["captcha"] = task
    log_event(conn, "detect", detect_id=detect_id, platform=p["platform"], verdict=verdict, code=code, source=source)
    return out


def page_site(p: dict, hint: str | None = None) -> str:
    """The consent site of a page: the ATS platform key (from the payload or the URL host), else host:<host>."""
    if hint:
        return hint
    from .. import accounts, identity
    plat = (p.get("platform") or "").lower()
    plat = plat[5:] if plat.startswith("site:") else plat
    if plat in accounts.ATS_PLATFORMS:
        return plat
    host = accounts.host_of(p.get("url"))
    cls = accounts.classify_host(host, p.get("url"))
    if cls["platform"] in accounts.ATS_PLATFORMS:
        return cls["platform"]
    return identity.capability_site_of(None, host)


def _capability_on(conn, cap: str, site: str) -> bool:
    from .. import identity
    try:
        return identity.capability_active(cap, site) and not identity.capability_prerequisite(conn, cap)
    except Exception:
        return False


def scan_page(conn, page: dict, source: str = "code", platform: str = "ats", job_id=None, token=None, agent_id=None,
              tab_id=None, cycle_id=None, captcha_visible: bool = False, site: str | None = None) -> dict:
    """The stop scan a code step runs on the page text it read over CDP, before and after it acts (2.6). Records a
    detection (source code); a tripping match trips its breaker (kept after the refusal) and raises
    E_STOP_DETECTED; a CAPTCHA (signature or a visible widget) opens the owner's task and returns {captcha}; a
    job-level stop the consent does not cover raises E_STOP_DETECTED; else {verdict: clear}. Inside the caller's
    tx."""
    payload = {"platform": platform if PLATFORM_RE.match(platform or "") else "ats",
               "url": str(page.get("url") or "")[:2000] or None, "title": str(page.get("title") or "")[:500] or None,
               "http_status": None, "text": str(page.get("text") or "")[:MAX_TEXT]}
    if captcha_visible and not re.search(r"captcha|i.m not a robot", payload["text"] or "", re.I):
        payload["text"] = (payload["text"] or "") + "\ncaptcha"
    from .. import db
    verdict, sig = match(validate_payload(payload, source))
    if sig and verdict == "stop" and sig.get("trip"):
        # the detection and the breaker trip must survive the refusal (their own transaction after this one)
        def _keep(c, pl=dict(payload)):
            detect(c, pl, source, cycle_id, job_id=job_id, token=token, agent_id=agent_id, tab_id=tab_id,
                   site_hint=site, open_captcha=False)
        db.defer_write(conn, _keep)
        raise Denied("E_STOP_DETECTED", "the page shows a stop (%s); the site is stopped and you are told"
                     % sig.get("id"), data={"matched": sig.get("id"), "reason_code": sig.get("reason_code")})
    res = detect(conn, payload, source, cycle_id, job_id=job_id, token=token, agent_id=agent_id, tab_id=tab_id,
                 site_hint=site)
    if res.get("captcha"):
        return {"verdict": "stop", "captcha": res["captcha"], "matched": res["matched"]}
    if res["verdict"] == "stop":
        raise Denied("E_STOP_DETECTED", "the page needs you (%s)" % (res["job_needs_human"] or res["matched"]),
                     data={"matched": res["matched"], "job_needs_human": res["job_needs_human"]})
    return {"verdict": "clear", "flow": res.get("flow"), "detect_id": res["detect_id"]}


def _where(p: dict) -> str:
    """The page's host (never the full URL) or the platform's name, for the owner-facing breaker detail."""
    from urllib.parse import urlsplit
    host = ""
    try:
        host = (urlsplit(p.get("url") or "").hostname or "").lower()
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    if host:
        return host
    from ..sheets_labels import platform_label
    plat = p["platform"][5:] if p["platform"].startswith("site:") else p["platform"]
    return platform_label(plat) or plat


ATS_FAMILY = ("ats", "ats_forms", "site:ats_forms")


def platform_names(platform: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(clear_names, stop_names): the detections.platform spellings that stand for one reserve platform.
    detect_page.js reports a board as site:<board> and an ATS host by its ATS name, while agents and gate
    reserve use the board id (naukri and site:naukri are one platform, as in breakers.platform_scope and
    ceilings.site_name). A clear must name the platform itself; a stop on the platform's family (the guard
    records ATS host stops as 'ats') also cancels an earlier clear."""
    from ..keys import ATS_NAMES
    p = (platform or "").strip().lower()
    if p in ("linkedin", "gmail") or p.startswith("api:"):
        return (p,), (p,)
    if p in ATS_FAMILY:
        return ATS_FAMILY, ATS_FAMILY
    if p in ATS_NAMES:
        return (p, "site:" + p), (p, "site:" + p) + ATS_FAMILY
    name = p[5:] if p.startswith("site:") else p
    if name in ATS_NAMES:
        return (name, "site:" + name), (name, "site:" + name) + ATS_FAMILY
    return (name, "site:" + name), (name, "site:" + name)


def same_platform(a: str, b: str) -> bool:
    """Whether two platform spellings name one platform (board id and site:<id>)."""
    return (a or "").strip().lower() in platform_names(b)[0]


def recent_clear(conn, platform: str, within_s: int = 600) -> dict | None:
    """The latest clear detection for the platform (any of its spellings) in the last within_s seconds, unless
    a later stop on the platform or its family came after it (gate reserve)."""
    from ..canon import now, ts_add
    clear_names, stop_names = platform_names(platform)
    row = conn.execute("SELECT * FROM detections WHERE lower(platform) IN (%s) AND verdict = 'clear' AND created_at >= ? "
                       "ORDER BY id DESC LIMIT 1" % ",".join("?" * len(clear_names)),
                       clear_names + (ts_add(now(), seconds=-within_s),)).fetchone()
    if row is None:
        return None
    stop = conn.execute("SELECT 1 FROM detections WHERE lower(platform) IN (%s) AND verdict <> 'clear' AND id > ?"
                        % ",".join("?" * len(stop_names)), stop_names + (row["id"],)).fetchone()
    return None if stop else dict(row)
