"""Canonical keys (design 2.4). Pure functions: no database, no network.

- job_key(url, native_ids, source) -> (canonical_key, alias_keys); precedence ATS id, board id, post id,
  canonical URL. A native id that the URL does not carry (or that has the wrong shape) is E_INVENTED_KEY.
- company_keys(name, domain, ats, tenant) -> [(alias_key, kind)] with kinds name, loose, dom, label, ats,
  tenant (B3): alphanumeric-only name key, noise-free loose key, registrable domain and its label, ATS tenant.
- person_keys(email, linkedin_url, full_name, company_uid) -> [(key, kind)] (M2).
- registrable_domain, norm_title, norm_city, role_key, fingerprint, jaccard.
Data files live in scripts/jobhunter/data/.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from . import paths
from .errors import Denied

DATA_DIR = os.path.join(paths.PKG_DIR, "data")

TRACKING_PARAMS = frozenset(p.lower() for p in (
    "gh_src", "source", "src", "ref", "refId", "trk", "trackingId", "lever-source", "lever-origin", "iis", "iisn",
    "fbclid", "gclid", "campaign", "jobPipeline", "eBP", "from"))
LEGAL_SUFFIXES = frozenset(("pvt", "ltd", "limited", "llp", "inc", "incorporated", "corp", "corporation", "co",
                            "company", "gmbh", "plc", "llc", "bv", "nv", "sa", "ag", "pte", "pty", "sas", "srl", "oy",
                            "ab", "kk"))
LEGAL_PAIRS = (("private", "limited"), ("private", "ltd"), ("pvt", "ltd"), ("pte", "ltd"), ("pty", "ltd"))
HONORIFICS = frozenset(("mr", "mrs", "ms", "miss", "dr", "prof", "sir", "shri", "smt"))
# Tokens a LinkedIn display name carries after the name itself: degrees and certifications, generational
# suffixes, pronouns and headline words. Dropped from the end of a name before the pname key is built, so
# "Jane Doe, MBA", "Jane Doe (She/Her)" and "Jane Doe Jr." stay one person with "Jane Doe".
NAME_TRAILERS = frozenset((
    "jr", "sr", "ii", "iii", "iv", "mba", "phd", "dphil", "md", "mbbs", "dds", "jd", "llb", "llm", "esq", "cpa",
    "cfa", "frm", "acca", "ca", "cs", "cma", "pmp", "csm", "cspo", "safe", "pe", "peng", "msc", "bsc", "ms", "ma",
    "ba", "bs", "mtech", "btech", "be", "me", "mca", "bca", "pgdm", "cissp", "cisa", "cism", "shrm", "shrmcp",
    "sphr", "phr", "cipd", "she", "her", "hers", "he", "him", "his", "they", "them", "theirs", "hiring",
    "recruiting", "recruiter", "opentowork"))
# Noise words that only qualify an agency's name (geography, group), so "Randstad India" is still the listed
# agency "randstad". Other noise words ("labs", "ai", "technologies") make a different company.
AGENCY_QUALIFIERS = frozenset(("india", "global", "group", "hq"))
TITLE_EXPAND = {"sr": "senior", "snr": "senior", "jr": "junior", "mgr": "manager", "eng": "engineer",
                "engg": "engineering", "pm": "product manager", "assoc": "associate", "exec": "executive",
                "dev": "developer", "mngr": "manager"}
ROLE_STOPWORDS = frozenset(("and", "of", "the", "for", "in", "at", "a", "an", "to", "with", "on", "or", "role",
                            "position", "opening", "job", "hiring", "urgent"))
ATS_NAMES = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable", "recruitee", "bamboohr",
             "icims", "successfactors", "taleo", "oracle_hcm", "jobvite")
BOARD_SITES = ("linkedin", "naukri", "yc", "instahyre", "foundit", "wellfound", "cutshort", "hirist", "iimjobs",
               "himalayas", "remoteok", "glassdoor", "indeed", "hn", "remotive", "weworkremotely",
               "workingnomads", "jobicy")
_HOST_SITE = {"naukri.com": "naukri", "instahyre.com": "instahyre", "foundit.in": "foundit",
              "wellfound.com": "wellfound", "angel.co": "wellfound", "cutshort.io": "cutshort",
              "hirist.tech": "hirist", "hirist.com": "hirist", "iimjobs.com": "iimjobs", "himalayas.app": "himalayas",
              "remoteok.com": "remoteok", "remoteok.io": "remoteok", "glassdoor.com": "glassdoor",
              "glassdoor.co.in": "glassdoor", "indeed.com": "indeed", "news.ycombinator.com": "hn",
              "remotive.com": "remotive", "weworkremotely.com": "weworkremotely",
              "workingnomads.com": "workingnomads", "jobicy.com": "jobicy", "workatastartup.com": "yc",
              "ycombinator.com": "yc", "linkedin.com": "linkedin"}
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_NATIVE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


# ---------------------------------------------------------------- data files
def _read_lines(name: str) -> tuple[str, ...]:
    out = []
    with open(os.path.join(DATA_DIR, name), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            out.append(line.lower())
    return tuple(out)


@functools.lru_cache(maxsize=None)
def public_suffixes() -> frozenset:
    return frozenset(_read_lines("public_suffixes.txt"))


@functools.lru_cache(maxsize=None)
def freemail_domains() -> frozenset:
    return frozenset(_read_lines("freemail_domains.txt"))


@functools.lru_cache(maxsize=None)
def hosting_domains() -> tuple:
    return _read_lines("hosting_domains.txt")


@functools.lru_cache(maxsize=None)
def generic_labels() -> frozenset:
    return frozenset(_read_lines("generic_labels.txt"))


@functools.lru_cache(maxsize=None)
def noise_words() -> frozenset:
    return frozenset(_read_lines("company_noise_words.txt"))


@functools.lru_cache(maxsize=None)
def agency_names() -> frozenset:
    return frozenset(_read_lines("agencies.txt"))


# Official renames kept in code rather than in data/city_aliases.json, so that the line can carry the leak check's
# ignore marker (a JSON file cannot): a public place name that a maintainer's private denylist may also hold.
_CODE_CITY_ALIASES = {"gurugram": ["gurgaon"]}  # leakcheck: ignore


@functools.lru_cache(maxsize=None)
def city_aliases() -> dict:
    with open(os.path.join(DATA_DIR, "city_aliases.json"), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for canon_name, aliases in _CODE_CITY_ALIASES.items():
        data.setdefault(canon_name, [])
        data[canon_name] = list(data[canon_name]) + [a for a in aliases if a not in data[canon_name]]
    out = {}
    for canon_name, aliases in data.items():
        out[canon_name] = canon_name
        for a in aliases:
            out[a] = canon_name
    return out


# ---------------------------------------------------------------- text
def fold(s: str | None) -> str:
    """NFKD, drop combining marks, lowercase."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


# ---------------------------------------------------------------- domains
def _host_of(value: str) -> str:
    v = value.strip().lower()
    if "@" in v and "://" not in v:
        v = v.rsplit("@", 1)[1]
    if "://" in v:
        v = urlsplit(v).hostname or ""
    elif "/" in v:
        v = v.split("/", 1)[0]
    v = v.split(":", 1)[0].strip(".")
    return v


def registrable_domain(host_or_email: str | None) -> str | None:
    """eTLD+1 of a host, URL or email address (data/public_suffixes.txt; unknown suffixes use the default
    one-label rule). None when the input has no registrable part."""
    if not host_or_email or not isinstance(host_or_email, str):
        return None
    host = _host_of(host_or_email)
    if not host or not re.match(r"^[a-z0-9.-]+$", host) or "." not in host:
        return None
    labels = host.split(".")
    if any(not lab for lab in labels):
        return None
    sfx = public_suffixes()
    for i in range(len(labels)):
        cand = ".".join(labels[i:])
        if cand in sfx:
            if i == 0:
                return None
            return ".".join(labels[i - 1:])
    return ".".join(labels[-2:])


def _suffix_match(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_freemail(host_or_email: str | None) -> bool:
    host = _host_of(host_or_email or "")
    return bool(host) and (host in freemail_domains() or (registrable_domain(host) or "") in freemail_domains())


def is_hosting(host_or_email: str | None) -> bool:
    host = _host_of(host_or_email or "")
    if not host:
        return False
    reg = registrable_domain(host) or host
    return _suffix_match(host, hosting_domains()) or _suffix_match(reg, hosting_domains())


def company_domain(value: str | None) -> str | None:
    """Registrable domain usable as a company key: never free-mail, ATS, job board or hosting."""
    reg = registrable_domain(value)
    if not reg or is_freemail(value) or is_hosting(value) or reg in freemail_domains():
        return None
    return reg


# ---------------------------------------------------------------- companies
def _strip_suffixes(tokens: list[str]) -> list[str]:
    changed = True
    while tokens and changed:
        changed = False
        if len(tokens) >= 2 and tuple(tokens[-2:]) in LEGAL_PAIRS:
            tokens = tokens[:-2]
            changed = True
        elif tokens[-1] in LEGAL_SUFFIXES or (tokens[-1] == "and" and len(tokens) > 1):
            tokens = tokens[:-1]
            changed = True
    return tokens


_DOTTED_ABBR = re.compile(r"(?<![a-z0-9])([a-z](?:\.[a-z])+)\.?(?![a-z0-9])")


def _collapse_dotted(s: str) -> str:
    """'s.a.', 'k.k.', 'l.l.c.' and 'n.v' become 'sa', 'kk', 'llc' and 'nv' before tokenizing, so the legal
    suffix list sees them as one token."""
    return _DOTTED_ABBR.sub(lambda m: m.group(1).replace(".", ""), s)


def name_tokens(name: str | None) -> list[str]:
    """Suffix-stripped tokens of a company name (2.4.2 norm_name before the final join)."""
    s = fold(name).replace("&", " and ")
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", s)
    toks = _tokens(_collapse_dotted(s))
    if toks and toks[0] == "the" and len(toks) > 1:
        toks = toks[1:]
    return _strip_suffixes(toks)


def norm_name(name: str | None) -> str:
    """Alphanumeric-only company name key body: 'GrowthValley Pvt. Ltd.' and 'Growth Valley' agree."""
    return "".join(name_tokens(name))


def loose_name(name: str | None) -> str:
    return "".join(t for t in name_tokens(name) if t not in noise_words())


def _label_key(label: str) -> str | None:
    lab = re.sub(r"[^a-z0-9]", "", fold(label))
    if len(lab) >= 4 and lab not in generic_labels():
        return lab
    return None


def company_keys(name: str | None = None, domain: str | None = None, ats: str | None = None,
                 tenant: str | None = None) -> list[tuple[str, str]]:
    """[(alias_key, kind)] for a company (2.4.2). Kinds: name, loose, dom, label, ats, tenant."""
    out: list[tuple[str, str]] = []
    seen = set()

    def add(key, kind):
        if key not in seen:
            seen.add(key)
            out.append((key, kind))

    if name:
        n = norm_name(name)
        if n:
            add("id:" + n, "name")
            lo = loose_name(name)
            if lo and lo != n and len(lo) >= 4:
                add("id:" + lo, "loose")
    if domain:
        reg = company_domain(domain)
        if reg:
            add("dom:" + reg, "dom")
            lab = _label_key(reg.split(".")[0])
            if lab:
                add("id:" + lab, "label")
    if ats and tenant:
        a = re.sub(r"[^a-z0-9]", "", fold(ats))
        t = fold(tenant).strip()
        t = re.sub(r"[^a-z0-9._-]", "", t)
        if a and t:
            add("ats:%s:%s" % (a, t), "ats")
            lab = _label_key(t)
            if lab:
                add("id:" + lab, "tenant")
    return out


def is_agency_name(name: str | None) -> bool:
    """True when the name key is in data/agencies.txt (2.4.2), or the loose key is and every noise word it
    dropped only qualifies the agency (geography or group: "Randstad India"). A descriptive noise word makes
    another company: "Hudson Labs" and "Antal Technologies" are not the listed agencies."""
    n = norm_name(name)
    if not n:
        return False
    if n in agency_names():
        return True
    toks = name_tokens(name)
    dropped = [t for t in toks if t in noise_words()]
    lo = "".join(t for t in toks if t not in noise_words())
    return bool(dropped) and all(t in AGENCY_QUALIFIERS for t in dropped) and len(lo) >= 4 and lo in agency_names()


# ---------------------------------------------------------------- people
def _email_parts(email: str) -> tuple[str, str]:
    e = email.strip().lower()
    if e.startswith("mailto:"):
        e = e[7:]
    if e.count("@") != 1:
        raise Denied("E_VALIDATION", "not an email address: %r" % email)
    local, dom = e.split("@")
    if not local or not re.match(r"^[a-z0-9.-]+\.[a-z0-9-]+$", dom or ""):
        raise Denied("E_VALIDATION", "not an email address: %r" % email)
    return local, dom


def normalize_email(email: str) -> str:
    local, dom = _email_parts(email)
    return local + "@" + dom


def email_keys(email: str) -> list[tuple[str, str]]:
    local, dom = _email_parts(email)
    base = local.split("+", 1)[0] or local
    if dom in ("gmail.com", "googlemail.com"):
        base = base.replace(".", "")
        dom = "gmail.com"
    return [("email:%s@%s" % (local, _email_parts(email)[1]), "email"), ("email_norm:%s@%s" % (base, dom), "email_norm")]


def linkedin_keys(url: str) -> list[tuple[str, str]]:
    """Keys of a LinkedIn profile URL (2.4.3). lnkd.in links and non-profile URLs are E_VALIDATION."""
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    parts = urlsplit(u)
    host = (parts.hostname or "").lower()
    if host == "lnkd.in" or host.endswith(".lnkd.in"):
        raise Denied("E_VALIDATION", "lnkd.in links are not accepted: open it and use the profile URL")
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise Denied("E_VALIDATION", "not a LinkedIn URL: %r" % url)
    segs = [unquote(s) for s in parts.path.split("/") if s]
    if len(segs) >= 2 and segs[0].lower() == "in":
        slug = segs[1]
        if re.match(r"^ACoA[A-Za-z0-9_-]+$", slug):
            return [("li_member:" + slug, "li_member")]
        slug = slug.strip().lower()
        if not slug or not re.match(r"^[^\s/?#]{2,100}$", slug):
            raise Denied("E_VALIDATION", "bad LinkedIn profile slug in %r" % url)
        return [("li:" + slug, "li_slug")]
    if len(segs) >= 2 and segs[0].lower() == "pub":
        return [("li_legacy:" + "/".join(s.lower() for s in segs[1:]), "li_legacy")]
    if len(segs) >= 3 and segs[0].lower() == "sales" and segs[1].lower() in ("lead", "people"):
        sid = segs[2].split(",", 1)[0]
        if not sid:
            raise Denied("E_VALIDATION", "bad Sales Navigator URL %r" % url)
        return [("li_sales:" + sid, "li_sales")]
    raise Denied("E_VALIDATION", "not a LinkedIn profile URL: %r" % url)


def pname_key(full_name: str | None, company_uid: str | None) -> str | None:
    if not full_name or not company_uid:
        return None
    toks = name_person_tokens(full_name)
    if len(toks) < 2:
        return None
    return "pname:%s.%s@%s" % (toks[0], toks[-1], company_uid)


_NAME_SEP = re.compile(r",|\s[-\u2010-\u2015|/]+\s|\|")


def name_person_tokens(full_name: str | None) -> list[str]:
    """The name tokens a pname key uses: bracketed text ("(She/Her)") and everything after a comma, a spaced
    dash or a bar ("Jane Doe, MBA", "Jane Doe - Hiring!", "Jane Doe | Talent") are dropped, then honorifics,
    single letters and trailing degrees, suffixes and pronouns (NAME_TRAILERS)."""
    s = fold(full_name)
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", s)
    s = _NAME_SEP.split(s)[0]
    toks = [t for t in _tokens(s.replace(".", " ")) if t not in HONORIFICS and len(t) > 1]
    while len(toks) > 2 and toks[-1] in NAME_TRAILERS:
        toks = toks[:-1]
    return toks


def person_keys(email: str | None = None, linkedin_url: str | None = None, full_name: str | None = None,
                company_uid: str | None = None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if email:
        out.extend(email_keys(email))
    if linkedin_url:
        out.extend(linkedin_keys(linkedin_url))
    pk = pname_key(full_name, company_uid)
    if pk:
        out.append((pk, "pname"))
    seen = set()
    return [(k, kind) for k, kind in out if not (k in seen or seen.add(k))]


# ---------------------------------------------------------------- jobs
def canonical_url(url: str) -> str:
    """Lowercase scheme and host, drop www. and m., the fragment, a trailing /, /apply, /application and
    tracking parameters; sort the remaining parameters."""
    if not isinstance(url, str) or not url.strip():
        raise Denied("E_VALIDATION", "empty URL")
    u = url.strip()
    parts = urlsplit(u)
    scheme = (parts.scheme or "https").lower()
    if scheme not in ("http", "https"):
        raise Denied("E_VALIDATION", "not an http(s) URL: %r" % url)
    host = (parts.hostname or "").lower()
    if not host:
        raise Denied("E_VALIDATION", "URL has no host: %r" % url)
    for pre in ("www.", "m."):
        if host.startswith(pre):
            host = host[len(pre):]
    if parts.port and parts.port not in (80, 443):
        host = "%s:%d" % (host, parts.port)
    path = parts.path or ""
    path = path.rstrip("/")
    for tail in ("/application", "/apply"):
        if path.lower().endswith(tail):
            path = path[: -len(tail)]
            break
    path = path.rstrip("/")
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not (k.lower() in TRACKING_PARAMS or k.lower().startswith("utm_"))]
    q.sort()
    return urlunsplit((scheme, host, path, urlencode(q), ""))


def _url_hash_key(url: str) -> str:
    return "url:" + hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()


def _host(url: str) -> str:
    h = (urlsplit(url.strip()).hostname or "").lower()
    for pre in ("www.", "m."):
        if h.startswith(pre):
            h = h[len(pre):]
    return h


def _q(url: str) -> dict:
    return {k.lower(): v for k, v in parse_qsl(urlsplit(url.strip()).query, keep_blank_values=True)}


def _segs(url: str) -> list[str]:
    return [unquote(s) for s in urlsplit(url.strip()).path.split("/") if s]


def ats_key_from_url(url: str) -> str | None:
    host, q, segs = _host(url), _q(url), _segs(url)
    if q.get("gh_jid", "").isdigit():
        return "ats:greenhouse:" + q["gh_jid"]
    if host.endswith("greenhouse.io"):
        if "token" in q and q["token"].isdigit():
            return "ats:greenhouse:" + q["token"]
        for i, s in enumerate(segs):
            if s == "jobs" and i + 1 < len(segs) and segs[i + 1].isdigit():
                return "ats:greenhouse:" + segs[i + 1]
    lever_id = q.get("lever-jobid", "")
    if re.match("^" + _UUID + "$", lever_id):
        return "ats:lever:" + lever_id.lower()
    if host.endswith("lever.co") and host.startswith("jobs."):
        if len(segs) >= 2 and re.match("^" + _UUID + "$", segs[1]):
            return "ats:lever:" + segs[1].lower()
    ash = q.get("ashby_jid", "")
    if re.match("^" + _UUID + "$", ash):
        return "ats:ashby:" + ash.lower()
    if host == "jobs.ashbyhq.com" and len(segs) >= 2 and re.match("^" + _UUID + "$", segs[1]):
        return "ats:ashby:" + segs[1].lower()
    m = re.match(r"^([a-z0-9-]+)\.wd[0-9]+\.myworkdayjobs\.com$", host)
    if m and "job" in [s.lower() for s in segs]:
        last = segs[-1]
        if "_" in last:
            req = last.rsplit("_", 1)[1]
            if re.match(r"^[A-Za-z0-9-]{2,40}$", req):
                return "ats:workday:%s:%s" % (m.group(1), req.upper())
    if host == "jobs.smartrecruiters.com" and len(segs) >= 2:
        m2 = re.match(r"^([0-9]{6,})", segs[1])
        if m2:
            return "ats:smartrecruiters:" + m2.group(1)
    if host == "apply.workable.com" and len(segs) >= 3 and segs[1] == "j":
        if re.match(r"^[A-Za-z0-9]{4,20}$", segs[2]):
            return "ats:workable:%s:%s" % (segs[0].lower(), segs[2].upper())
    m = re.match(r"^([a-z0-9-]+)\.bamboohr\.com$", host)
    if m:
        jid = None
        if len(segs) >= 2 and segs[0] == "careers" and segs[1].isdigit():
            jid = segs[1]
        elif q.get("id", "").isdigit():
            jid = q["id"]
        if jid:
            return "ats:bamboohr:%s:%s" % (m.group(1), jid)
    return None


def board_key_from_url(url: str) -> str | None:
    host, q, segs = _host(url), _q(url), _segs(url)
    if host.endswith("linkedin.com"):
        if q.get("currentjobid", "").isdigit():
            return "board:linkedin:" + q["currentjobid"]
        if len(segs) >= 3 and segs[0] == "jobs" and segs[1] == "view":
            m = re.search(r"([0-9]{6,})$", segs[2])
            if m:
                return "board:linkedin:" + m.group(1)
        return None
    if host.endswith("naukri.com") and segs and segs[0].startswith("job-listings-"):
        m = re.search(r"-([0-9]{6,})$", segs[0])
        if m:
            return "board:naukri:" + m.group(1)
    if host == "workatastartup.com" and len(segs) >= 2 and segs[0] == "jobs" and segs[1].isdigit():
        return "board:yc:" + segs[1]
    if host == "ycombinator.com" and "jobs" in segs:
        i = segs.index("jobs")
        if i + 1 < len(segs):
            m = re.match(r"^([0-9]+)", segs[i + 1])
            if m:
                return "board:yc:" + m.group(1)
    if host.startswith("glassdoor.") and q.get("joblistingid", "").isdigit():
        return "board:glassdoor:" + q["joblistingid"]
    if host.endswith("indeed.com") and re.match(r"^[0-9a-f]{16}$", q.get("jk", "")):
        return "board:indeed:" + q["jk"]
    if host == "news.ycombinator.com" and q.get("id", "").isdigit():
        return "board:hn:" + q["id"]
    return None


def post_key_from_url(url: str) -> str | None:
    host = _host(url)
    if not host.endswith("linkedin.com"):
        return None
    text = unquote(url)
    for pat in (r"urn:li:activity:([0-9]{19})", r"activity-([0-9]{19})", r"share-([0-9]{19})",
                r"ugcPost-([0-9]{19})", r"urn:li:ugcPost:([0-9]{19})", r"urn:li:share:([0-9]{19})"):
        m = re.search(pat, text)
        if m:
            return "post:linkedin:" + m.group(1)
    return None


def _site_for(url: str, source: str | None) -> str | None:
    s = (source or "").lower()
    if s in BOARD_SITES:
        return s
    host = _host(url)
    for h, site in _HOST_SITE.items():
        if host == h or host.endswith("." + h):
            return site
    return None


def _validate_native(url: str, value, what: str) -> str:
    v = str(value).strip() if value is not None else ""
    if not v or not _NATIVE_ID_RE.match(v):
        raise Denied("E_INVENTED_KEY", "%s %r is not a valid id" % (what, value))
    if v.lower() not in unquote(url).lower():
        raise Denied("E_INVENTED_KEY", "%s %r does not appear in the URL" % (what, v), data={"url": url})
    return v


def _recruitee_api_key(url: str, value, source: str | None) -> str | None:
    """ats:recruitee:<company>:<offer_id> (2.4.1). Recruitee job URLs carry the slug, not the offer id, so the
    numeric id is taken from the offers API (source 'recruitee') for a <company>.recruitee.com/o/<slug> URL even
    though it is not in the URL. Any other source or URL shape still needs the id in the URL."""
    if (source or "").lower() != "recruitee":
        return None
    v = str(value).strip() if value is not None else ""
    if not re.match(r"^[0-9]{1,15}$", v):
        return None
    parts = urlsplit(url if "://" in url else "https://" + url)
    m = re.match(r"^([a-z0-9-]+)\.recruitee\.com$", (parts.hostname or "").lower())
    if not m or not re.match(r"^(/l/[a-z-]{2,10})?/o/[^/]+/?$", parts.path or ""):
        return None
    return "ats:recruitee:%s:%s" % (m.group(1), v)


def job_key(url: str, native_ids: dict | None = None, source: str | None = None) -> tuple[str, list[str]]:
    """(canonical_key, alias_keys) for a job URL plus native ids (2.4.1). Every key comes from the URL or a
    native id that the URL carries; anything else is E_INVENTED_KEY."""
    canonical_url(url)   # validates the URL
    native_ids = dict(native_ids or {})
    unknown = [k for k in native_ids if k not in ("board_job_id", "ats_job_id", "post_id")]
    if unknown:
        raise Denied("E_VALIDATION", "unknown native id keys: %s" % ", ".join(sorted(unknown)))
    keys: list[str] = []
    ats = ats_key_from_url(url)
    if native_ids.get("ats_job_id") not in (None, ""):
        rk = _recruitee_api_key(url, native_ids["ats_job_id"], source) if ats is None else None
        if rk is not None:
            ats = rk
        else:
            v = _validate_native(url, native_ids["ats_job_id"], "ats_job_id")
            if ats is None:
                raise Denied("E_INVENTED_KEY", "ats_job_id given but the URL is not an ATS job URL")
            if not ats.lower().endswith(":" + v.lower()):
                raise Denied("E_INVENTED_KEY", "ats_job_id %r differs from the id in the URL" % v)
    if ats:
        keys.append(ats)
    board = board_key_from_url(url)
    if native_ids.get("board_job_id") not in (None, ""):
        v = _validate_native(url, native_ids["board_job_id"], "board_job_id")
        site = _site_for(url, source)
        if site is None:
            raise Denied("E_INVENTED_KEY", "board_job_id given for a URL that is not a known board")
        nk = "board:%s:%s" % (site, v)
        if board and board != nk:
            raise Denied("E_INVENTED_KEY", "board_job_id %r differs from the id in the URL" % v)
        board = nk
    if board:
        keys.append(board)
    post = post_key_from_url(url)
    if native_ids.get("post_id") not in (None, ""):
        v = _validate_native(url, native_ids["post_id"], "post_id")
        if not re.match(r"^[0-9]{19}$", v):
            raise Denied("E_INVENTED_KEY", "post_id must be the 19-digit activity id")
        pk = "post:linkedin:" + v
        if post and post != pk:
            raise Denied("E_INVENTED_KEY", "post_id %r differs from the id in the URL" % v)
        post = pk
    if post:
        keys.append(post)
    urlk = _url_hash_key(url)
    keys.append(urlk)
    return keys[0], keys[1:]


# ---------------------------------------------------------------- titles and cities
_REQ_RE = re.compile(r"\b(req(uisition)?\s*(id|no|number)?\s*[:#]?\s*[a-z]*[0-9][a-z0-9-]*|job\s*(id|code)\s*[:#]?\s*"
                     r"[a-z0-9-]+|#\s*[0-9]+|[a-z]{0,3}-?[0-9]{4,})\b")


def norm_title(title: str | None) -> str:
    s = fold(title).replace("&", " and ")
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", s)
    s = _REQ_RE.sub(" ", s)
    toks = []
    for t in _tokens(s):
        toks.extend(TITLE_EXPAND.get(t, t).split())
    return " ".join(toks)


def norm_city(city: str | None) -> str | None:
    if not city:
        return None
    s = fold(city)
    s = re.split(r"[,/|;]|\s-\s|\sor\s", s)[0]
    s = " ".join(_tokens(s))
    if not s:
        return None
    aliases = city_aliases()
    if s in aliases:
        return aliases[s]
    return s


def role_key(title: str | None) -> str:
    toks = sorted(set(t for t in norm_title(title).split() if t not in ROLE_STOPWORDS))
    return " ".join(toks)


def jaccard(a: str | None, b: str | None) -> float:
    sa, sb = set((a or "").split()), set((b or "").split())
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def fingerprint(company_uid: str, title: str, city: str | None) -> str:
    return hashlib.sha1(("%s|%s|%s" % (company_uid or "", norm_title(title), norm_city(city) or ""))
                        .encode("utf-8")).hexdigest()
