"""Address checks (`email verify`, design 6.1 step 5).

Grades: A = published for that person, or a role inbox named in the job post; B = pattern confirmed by two
published addresses at that domain (evidence URL required); C = a guess (refused unless the config allows
it). The MX check uses `dig` when present, else DNS over HTTPS (`dns.doh_url`). Never SMTP probing, never
several variants for one person. A domain that bounced a grade B or C address is on the no-guessing list
(meta 'no_guess:<domain>', written by replies) and only accepts grade A afterwards.

verify_address runs inside the caller's transaction; do the DNS lookup first with mx_for_domain() (or
lookup_mx()) and pass the result as mx_hosts, so no network call happens while the write lock is held.

Helpers for the optional email finder (U10, ENRICH-SPEC 4 and 11): mx_for_domain (network, cached per
domain, never inside a transaction), guess_blocked and pattern_evidence (hook-safe reads), render_pattern
(pure). verify_address also consults the finder's stored result for the address (enrich.verify.lookup):
a provider-found address keeps the grade code gave it, and only a research fact that shows the exact
address promotes it to A.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

from . import canon, paths
from .errors import Denied
from .events import log_event
from .threads import cfg

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")
URL_RE = re.compile(r"https://[^\s\"'<>)]+")
DNS_TIMEOUT_S = 5
FREEMAIL_FALLBACK = frozenset({"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com",
                               "hotmail.com", "live.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
                               "aol.com", "rediffmail.com", "gmx.com", "yandex.com", "mail.com"})
PROVIDERS = (("google", ("google.com", "googlemail.com")), ("microsoft", ("outlook.com", "protection.outlook.com")),
             ("zoho", ("zoho.com", "zoho.in", "zoho.eu")), ("proton", ("protonmail.ch", "proton.me")),
             ("yahoo", ("yahoodns.net",)), ("icloud", ("icloud.com",)))


MX_CACHE_TTL_S = 24 * 3600
MX_CACHE_MAX = 500
_MX_CACHE: dict = {}
_MX_LOCK = threading.Lock()
GRADE_RANK = {"A": 3, "B": 2, "C": 1}
EMAIL_SOURCES = ("published", "pattern", "provider", "human")
# local-part patterns a published pair of addresses can prove (ENRICH-SPEC 4 step 6b, DESIGN 6.1 step 5 grade B)
PATTERNS = ("{first}.{last}", "{first}{last}", "{f}{last}", "{f}.{last}", "{first}_{last}", "{first}-{last}",
            "{first}", "{last}.{first}", "{last}{first}", "{last}{f}", "{first}{l}", "{first}.{l}", "{last}",
            "{l}{first}")
PATTERN_MIN_ADDRESSES = 2
ROLE_LOCALS = frozenset({"info", "contact", "hello", "careers", "career", "jobs", "job", "hr", "recruiting",
                         "recruitment", "talent", "hiring", "team", "admin", "support", "sales", "office", "mail",
                         "enquiries", "inquiries", "noreply", "no-reply", "press", "media", "billing", "accounts",
                         "people", "apply", "applications", "resume", "resumes", "cv", "work", "joinus", "join"})
_ADDR_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


class LookupFailed(Exception):
    pass


def freemail_domains() -> frozenset:
    path = os.path.join(paths.PKG_DIR, "data", "freemail_domains.txt")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            items = {ln.strip().lower() for ln in fh if ln.strip() and not ln.startswith("#")}
        return frozenset(items) | FREEMAIL_FALLBACK
    except OSError:
        return FREEMAIL_FALLBACK


def _dig(domain: str) -> list[str]:
    exe = shutil.which("dig")
    if not exe:
        raise LookupFailed("dig not installed")
    try:
        res = subprocess.run([exe, "+short", "+time=3", "+tries=1", "MX", domain], capture_output=True, text=True,
                             timeout=DNS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LookupFailed(str(exc))
    if res.returncode != 0:
        raise LookupFailed("dig exit %d" % res.returncode)
    out = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            out.append(parts[1].rstrip(".").lower())
        elif line.startswith(";;"):
            raise LookupFailed(line)
    return out


def _doh(domain: str) -> list[str]:
    base = str(cfg("dns.doh_url", "https://dns.google/resolve"))
    if not base.startswith("https://"):
        raise LookupFailed("dns.doh_url must be https")
    url = base + "?" + urllib.parse.urlencode({"name": domain, "type": "MX"})
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    try:
        with urllib.request.urlopen(req, timeout=DNS_TIMEOUT_S) as resp:
            data = json.loads(resp.read(200000).decode("utf-8"))
    except Exception as exc:  # any transport or parse error is a failed lookup, never "no MX"
        raise LookupFailed(str(exc))
    if data.get("Status") == 3:      # NXDOMAIN
        return []
    if data.get("Status") != 0:
        raise LookupFailed("DNS status %s" % data.get("Status"))
    out = []
    for ans in data.get("Answer") or []:
        if ans.get("type") == 15:
            parts = str(ans.get("data", "")).split()
            if len(parts) == 2:
                out.append(parts[1].rstrip(".").lower())
    return out


def lookup_mx(domain: str) -> list[str]:
    """MX hosts of a domain ([] when it has none or a null MX). LookupFailed when DNS could not answer."""
    if not re.match(r"^([a-z0-9-]+\.)+[a-z]{2,}$", domain or ""):
        raise LookupFailed("bad domain")
    try:
        hosts = _dig(domain)
    except LookupFailed:
        hosts = _doh(domain)
    return [h for h in hosts if h and h != "."]


def mx_for_domain(domain: str) -> dict:
    """{mx_ok, mx_hosts, provider_hint, cached} for a domain (network; never call it inside a transaction).
    Answers are cached per domain for MX_CACHE_TTL_S in this process. A DNS failure is never "no MX": it
    raises Denied(E_ENRICH_UNAVAILABLE, retry in an hour), so a lookup is skipped for now, not dropped."""
    d = (domain or "").strip().lower().rstrip(".")
    if not re.match(r"^([a-z0-9-]+\.)+[a-z]{2,}$", d):
        raise Denied("E_VALIDATION", "not a domain name")
    stamp = time.time()
    with _MX_LOCK:
        hit = _MX_CACHE.get(d)
        if hit is not None and stamp - hit[0] < MX_CACHE_TTL_S:
            hosts = list(hit[1])
            return {"mx_ok": bool(hosts), "mx_hosts": hosts[:5], "provider_hint": provider(hosts), "cached": True}
    try:
        hosts = lookup_mx(d)
    except LookupFailed:
        raise Denied("E_ENRICH_UNAVAILABLE", "the MX lookup failed; try again later", retry_after=3600,
                     data={"reason": "mx_lookup_failed"})
    with _MX_LOCK:
        if len(_MX_CACHE) >= MX_CACHE_MAX:
            _MX_CACHE.clear()
        _MX_CACHE[d] = (stamp, tuple(hosts))
    return {"mx_ok": bool(hosts), "mx_hosts": list(hosts)[:5], "provider_hint": provider(hosts), "cached": False}


def clear_mx_cache() -> None:
    with _MX_LOCK:
        _MX_CACHE.clear()


def guess_blocked(conn, domain: str) -> bool:
    """True when the domain is on the no-guessing list (a grade B or C address there bounced): only a
    published grade A address may be used there. Hook-safe (one indexed read)."""
    d = (domain or "").strip().lower()
    if not d:
        return False
    return conn.execute("SELECT 1 FROM meta WHERE key = ?", ("no_guess:" + d,)).fetchone() is not None


def _fold(s: str | None) -> str:
    """ASCII letters and digits of a name part, lowercased (NFKD, combining marks dropped)."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _name_parts(first: str | None, last: str | None, full: str | None = None) -> tuple[str, str]:
    words = [w for w in re.split(r"\s+", str(full or "").strip()) if w]
    f = _fold(first) or (_fold(words[0]) if words else "")
    l_ = _fold(last) or (_fold(words[-1]) if len(words) > 1 else "")
    return f, l_


def render_pattern(pattern: str, first: str | None, last: str | None) -> str | None:
    """The address a pattern such as "{first}.{last}@kestrel.example" gives for a name, or None when a name
    part the pattern needs is missing or the result is not a valid address. Tokens: {first}, {last}, {f},
    {l} (first letters). Pure."""
    if not isinstance(pattern, str) or pattern.count("@") != 1:
        return None
    f, l_ = _fold(first), _fold(last)
    local, dom = pattern.split("@")
    need_f = "{first}" in local or "{f}" in local
    need_l = "{last}" in local or "{l}" in local
    if (need_f and not f) or (need_l and not l_):
        return None
    out = local.replace("{first}", f).replace("{last}", l_).replace("{f}", f[:1]).replace("{l}", l_[:1])
    if "{" in out or "}" in out:
        return None
    addr = (out + "@" + dom).lower()
    return addr if EMAIL_RE.match(addr) else None


def _fact_urls_for(conn, addr: str) -> list[str]:
    """https source URLs of research facts (no injection flag) that show the exact address."""
    like = "%" + addr.replace("%", "").replace("_", "\\_") + "%"
    out = []
    for r in conn.execute("SELECT source_url, snippet, text FROM research_facts WHERE injection_flag = 0 AND "
                          "(lower(snippet) LIKE ? ESCAPE '\\' OR lower(text) LIKE ? ESCAPE '\\') ORDER BY id",
                          (like, like)):
        if not str(r[0]).startswith("https://"):
            continue
        found = {a.lower() for a in _ADDR_IN_TEXT_RE.findall((r[1] or "") + " " + (r[2] or ""))}
        if addr in found and r[0] not in out:
            out.append(r[0])
    return out


def _published_at_domain(conn, domain: str) -> list[dict]:
    """Addresses at `domain` that research facts show verbatim, with the name of the stored contact who owns
    each one: [{address, first, last, url}]. Role inboxes and addresses nobody is named for are left out."""
    like = "%@" + domain.replace("%", "").replace("_", "\\_") + "%"
    seen: dict = {}
    for r in conn.execute("SELECT source_url, snippet, text FROM research_facts WHERE injection_flag = 0 AND "
                          "(lower(snippet) LIKE ? ESCAPE '\\' OR lower(text) LIKE ? ESCAPE '\\') ORDER BY id",
                          (like, like)):
        if not str(r[0]).startswith("https://"):
            continue
        for a in _ADDR_IN_TEXT_RE.findall((r[1] or "") + " " + (r[2] or "")):
            a = a.lower()
            if a.rsplit("@", 1)[1] == domain and a not in seen:
                seen[a] = r[0]
    out = []
    for addr, url in seen.items():
        if addr.split("@", 1)[0].split("+", 1)[0] in ROLE_LOCALS:
            continue
        c = conn.execute("SELECT first_name, full_name FROM contacts WHERE lower(email) = ? AND merged_into IS NULL "
                         "AND role_type <> 'role_inbox' ORDER BY id LIMIT 1", (addr,)).fetchone()
        if c is None:
            continue
        f, l_ = _name_parts(c["first_name"], None, c["full_name"])
        if f:
            out.append({"address": addr, "first": f, "last": l_, "url": url})
    return out


def pattern_evidence(conn, domain: str) -> dict | None:
    """{pattern, evidence_urls, addresses} when at least PATTERN_MIN_ADDRESSES published addresses of different
    named people at the domain follow one local-part pattern (the grade B rule), else None. Two patterns that
    are each proven by different people mean the domain has no single pattern: None. Hook-safe (reads only)."""
    d = (domain or "").strip().lower()
    if not d or d in freemail_domains():
        return None
    support: dict = {}
    for p in _published_at_domain(conn, d):
        for pat in PATTERNS:
            if render_pattern(pat + "@" + d, p["first"], p["last"]) == p["address"]:
                support.setdefault(pat, {})[p["address"]] = p["url"]
    proven = {pat: hits for pat, hits in support.items() if len(hits) >= PATTERN_MIN_ADDRESSES}
    if not proven:
        return None
    best = max(proven, key=lambda pat: (len(proven[pat]), -PATTERNS.index(pat)))
    if any(set(h) != set(proven[best]) for pat, h in proven.items() if pat != best):
        return None
    urls = []
    for u in proven[best].values():
        if u not in urls:
            urls.append(u)
    return {"pattern": best + "@" + d, "evidence_urls": urls[:5], "addresses": len(proven[best])}


def provider_result(conn, address: str) -> dict | None:
    """The email finder's stored result for an address ({grade, provider, call_id, bounced}) through
    jobhunter.enrich.verify.lookup; None when the finder is not installed or never saw the address."""
    try:
        mod = importlib.import_module("jobhunter.enrich.verify")
    except ImportError:
        return None
    fn = getattr(mod, "lookup", None)
    if fn is None:
        return None
    res = fn(conn, address)
    return res if isinstance(res, dict) else None


def published_evidence(conn, address: str) -> list[str]:
    """https pages that show the exact address, counted only when the page is on the address's own domain
    (its registrable domain) or is the source or apply page of a stored job: the grade A proof."""
    addr = (address or "").strip().lower()
    if "@" not in addr:
        return []
    dom = addr.rsplit("@", 1)[1]
    out = []
    for url in _fact_urls_for(conn, addr):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        on_domain = host == dom or host.endswith("." + dom)
        if not on_domain:
            on_domain = conn.execute("SELECT 1 FROM jobs WHERE source_url = ? OR apply_url = ? LIMIT 1",
                                     (url, url)).fetchone() is not None
        if on_domain:
            out.append(url)
    return out


def provider(mx_hosts: list[str]) -> str:
    for name, suffixes in PROVIDERS:
        for h in mx_hosts:
            if any(h == s or h.endswith("." + s) for s in suffixes):
                return name
    return "other" if mx_hosts else "none"


def verify_address(conn, address: str, grade: str, evidence_url: str | None, *,
                   mx_hosts: list[str] | None = None, evidence_text: str | None = None) -> dict:
    """Record the MX result and grade for an address and decide whether it may be used. Returns
    {address, domain, mx_ok, mx_hosts, provider, grade, requested_grade, email_source, allowed, reasons,
    evidence_url, provider_result}; the caller raises E_NO_MX or E_ADDRESS_GRADE after committing when
    allowed is false (the facts are stored either way).

    When the email finder (U10) holds a result for this address, code's grade stands: the agent's grade may
    only be lower, or A when a research fact shows the exact address on an https page of the address's own
    domain or of a stored job's source or apply page (the address is then `published`). A provider verdict X
    or a bounced provider result is never usable (reason provider_invalid)."""
    addr = (address or "").strip().lower()
    if not EMAIL_RE.match(addr):
        raise Denied("E_VALIDATION", "not a valid email address")
    if grade not in ("A", "B", "C"):
        raise Denied("E_VALIDATION", "grade must be A, B or C")
    domain = addr.rsplit("@", 1)[1]
    urls = []
    if evidence_url:
        urls.append(evidence_url)
    if evidence_text:
        urls += URL_RE.findall(evidence_text)
    urls = [u.rstrip(".,;") for u in urls if u.startswith("https://")]
    if mx_hosts is None:
        try:
            mx_hosts = lookup_mx(domain)
        except LookupFailed as exc:
            raise Denied("E_NETWORK", "MX lookup failed: %s" % exc)
    mx_ok = bool(mx_hosts)
    allowed_grades = cfg("gmail.address_grades_allowed", ["A", "B"])
    if not isinstance(allowed_grades, list):
        allowed_grades = ["A", "B"]
    reasons = []
    requested = grade
    prov = provider_result(conn, addr)
    published = published_evidence(conn, addr) if grade == "A" else []
    source = {"A": "published", "B": "pattern"}.get(grade)
    if prov is not None:
        pg = prov.get("grade")
        if pg == "X" or prov.get("bounced"):
            reasons.append("provider_invalid")
        if grade == "A" and published:
            urls = published[:1] + [u for u in urls if u != published[0]]
            source = "published"
        elif pg in GRADE_RANK:
            if GRADE_RANK[grade] >= GRADE_RANK[pg] or grade == "A":
                grade = pg                    # the agent cannot raise a provider result's grade
            source = "provider" if grade == pg else None
        else:
            grade = "C" if grade == "A" else grade
            source = None
    if not mx_ok:
        reasons.append("no_mx")
    if grade not in allowed_grades:
        reasons.append("grade_not_allowed")
    if grade == "B" and not urls and source != "provider":
        reasons.append("grade_b_needs_evidence")
    if grade in ("B", "C"):
        if domain in freemail_domains():
            reasons.append("pattern_on_freemail")
        if guess_blocked(conn, domain):
            reasons.append("no_guess_domain")
    stamp = canon.now()
    allowed = not reasons
    for row in conn.execute("SELECT id, email_source FROM contacts WHERE lower(email) = ?", (addr,)).fetchall():
        # only the finder itself writes `provider` (with its call id, contacts.set_address)
        new_source = row["email_source"] if source in (None, "provider") else source
        if row["email_source"] == "provider":
            # a provider address keeps its provenance and may only go down to C, unless a page proves it (11.1)
            if source == "published" and published:
                new_source = "published"
            elif prov is not None and grade in ("B", "C"):
                new_source = "provider"
            else:
                new_source = None
        if "provider_invalid" in reasons or new_source is None and row["email_source"] == "provider":
            conn.execute("UPDATE contacts SET email_mx_ok = ?, updated_at = ? WHERE id = ?",
                         (1 if mx_ok else 0, stamp, row["id"]))
            continue
        conn.execute("UPDATE contacts SET email_mx_ok = ?, email_grade = ?, email_source = ?, "
                     "email_evidence_url = CASE WHEN ? = 'published' THEN COALESCE(?, email_evidence_url) "
                     "ELSE COALESCE(email_evidence_url, ?) END, updated_at = ? WHERE id = ?",
                     (1 if mx_ok else 0, grade, new_source, new_source, urls[0] if urls else None,
                      urls[0] if urls else None, stamp, row["id"]))
    log_event(conn, "email_verified", domain=domain, grade=grade, requested=requested, mx_ok=mx_ok, allowed=allowed,
              reasons=reasons, provider_result=prov is not None)
    return {"address": addr, "domain": domain, "mx_ok": mx_ok, "mx_hosts": list(mx_hosts)[:5],
            "provider": provider(list(mx_hosts)), "grade": grade, "requested_grade": requested,
            "email_source": source, "allowed": allowed, "reasons": reasons,
            "evidence_url": urls[0] if urls else None,
            "provider_result": {"grade": prov.get("grade"), "provider": prov.get("provider"),
                                "bounced": bool(prov.get("bounced"))} if prov is not None else None}
