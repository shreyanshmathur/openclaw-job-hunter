"""Evaluator stage A: the deterministic pre-filter (design 6.2). Pure: no database, no network.

    ok, reason_code = check(job, profile, config)
    ok, reason_code, sentence = explain(job, profile, config)

`job` is the ingest view of one listing (see `job_view`): title, location, norm_city, work_mode, remote_scope,
employment_type, posted_at, years_min/years_max, salary_*, jd_text, can_apply, is_agency. `profile` is the
confirmed profile of 12.6 (the `{"fields": {name: {"value", "source"}}}` shape) or a flat `{name: value}` dict;
only confirmed values are read (`profile_values`). `config` is the effective config (4.2); missing keys fall back
to the researched defaults below.

Rules run in the order of the 6.2 table and the first failing rule wins. A rule whose profile input is missing is
skipped: the pre-filter only rejects on a stated preference, never on a guess. Exclusions (`excluded_company`,
`excluded_job_url`) need the database and are checked by `jobs.ingest` before this module runs.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import re

from . import canon

REASON_CODES = ("stale", "location", "years_required", "seniority_title", "role_family", "employment_type",
                "comp_below_floor", "language_or_authorization", "agency_unnamed")
EXCLUSION_CODES = ("excluded_company", "excluded_job_url")

DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_TOLERANCE = {"below": 1.0, "above": 3.0}
YEARS_CAP = 40  # numbers above this in a "N+ years" phrase are not experience requirements

# ---------------------------------------------------------------- profile access
_CONFIRMED_SOURCES = ("user_confirmed",)


def _unwrap(v):
    if isinstance(v, dict) and "value" in v and ("source" in v or set(v) <= {"value", "confirmed_at", "basis", "note"}):
        src = v.get("source")
        if src is not None and src not in _CONFIRMED_SOURCES:
            return None
        return v.get("value")
    return v


def profile_values(profile: dict | None) -> dict:
    """{field: value} of the confirmed fields. Accepts the 12.6 file shape or an already flat dict."""
    if not isinstance(profile, dict):
        return {}
    fields = profile.get("fields")
    src = fields if isinstance(fields, dict) else profile
    out = {}
    for k, v in src.items():
        if k in ("version", "profile_version", "facts"):
            continue
        val = _unwrap(v)
        if val is not None:
            out[k] = val
    return out


def _cfg(config: dict | None, *path, default=None):
    cur = config if isinstance(config, dict) else {}
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


# ---------------------------------------------------------------- text helpers
_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")


def _plain(s) -> str:
    """Lowercase, unicode dashes to '-', whitespace collapsed."""
    if s is None:
        return ""
    s = _DASHES.sub("-", str(s))
    return " ".join(s.lower().split())


def _words(s) -> str:
    """Lowercase alphanumeric words separated by single spaces, padded with spaces for phrase search."""
    s = _DASHES.sub("-", str(s or "")).lower().replace("&", " and ")
    return " " + " ".join(re.findall(r"[a-z0-9+#]+", s)) + " "


def _has_phrase(words: str, phrase: str) -> bool:
    p = _words(phrase).strip()
    return bool(p) and (" " + p + " ") in words


def _keys_mod():
    try:
        return importlib.import_module("jobhunter.keys")
    except ImportError:
        return None


def norm_city(city: str | None) -> str | None:
    """keys.norm_city when available (U1), else a plain lowercase form."""
    if not city or not str(city).strip():
        return None
    k = _keys_mod()
    if k is not None and hasattr(k, "norm_city"):
        try:
            return k.norm_city(str(city))
        except NotImplementedError:
            pass
    return " ".join(str(city).lower().split())


# ---------------------------------------------------------------- job view
def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def job_view(item: dict) -> dict:
    """Normalise a 12.1 item, an internal ingest dict or a jobs row dict to the keys the rules read."""
    item = dict(item or {})
    years = item.get("years") if isinstance(item.get("years"), dict) else {}
    salary = item.get("salary") if isinstance(item.get("salary"), dict) else {}
    return {
        "title": item.get("title") or "",
        "company": item.get("company") or item.get("company_name_raw") or "",
        "location": item.get("location") if item.get("location") is not None else item.get("location_raw"),
        "norm_city": item.get("norm_city"),
        "work_mode": item.get("work_mode") or "unknown",
        "remote_scope": item.get("remote_scope"),
        "employment_type": item.get("employment_type"),
        "posted_at": item.get("posted_at"),
        "years_min": _num(years.get("min")) if years else _num(item.get("years_min")),
        "years_max": _num(years.get("max")) if years else _num(item.get("years_max")),
        "salary_min": _num(salary.get("min")) if salary else _num(item.get("salary_min")),
        "salary_max": _num(salary.get("max")) if salary else _num(item.get("salary_max")),
        "salary_currency": (salary.get("currency") if salary else item.get("salary_currency")),
        "salary_period": (salary.get("period") if salary else item.get("salary_period")),
        "jd_text": item.get("jd_text") or "",
        "can_apply": item.get("can_apply"),
        "is_agency": bool(item.get("is_agency")),
    }


# ---------------------------------------------------------------- rule: stale
def _parse_date(s) -> _dt.date | None:
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _rule_stale(job, prof, config):
    if job["can_apply"] is False:
        return "the posting no longer accepts applications"
    max_age = _cfg(config, "sources", "max_age_days", default=DEFAULT_MAX_AGE_DAYS)
    try:
        max_age = int(max_age)
    except (TypeError, ValueError):
        max_age = DEFAULT_MAX_AGE_DAYS
    d = _parse_date(job["posted_at"])
    if d is None or max_age <= 0:
        return None
    age = (canon.utcnow().date() - d).days
    if age > max_age:
        return "posted %d days ago (older than %d days)" % (age, max_age)
    return None


# ---------------------------------------------------------------- rule: location
_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed|home based|home-based)\b")
_WORLDWIDE_RE = re.compile(r"\b(worldwide|anywhere|global|globally|all countries|any country|international)\b")

_EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
       "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
_EUROPE = _EU | {"GB", "CH", "NO", "IS", "UA", "RS"}
_MIDEAST = {"AE", "SA", "QA", "IL", "TR", "EG", "JO", "KW", "BH", "OM"}
_AFRICA = {"ZA", "NG", "KE", "EG", "MA", "GH"}
_APAC = {"IN", "SG", "AU", "NZ", "JP", "KR", "CN", "HK", "TW", "ID", "MY", "PH", "TH", "VN", "PK", "BD", "LK", "NP"}
_LATAM = {"MX", "BR", "AR", "CL", "CO", "PE", "UY", "CR", "EC"}
REGIONS = {
    "us": {"US"}, "usa": {"US"}, "u s": {"US"}, "united states": {"US"}, "america": {"US"},
    "canada": {"CA"}, "north america": {"US", "CA", "MX"}, "americas": {"US", "CA"} | _LATAM, "latam": _LATAM,
    "latin america": _LATAM, "south america": _LATAM, "uk": {"GB"}, "united kingdom": {"GB"}, "england": {"GB"},
    "ireland": {"IE"}, "eu": _EU, "europe": _EUROPE, "european union": _EU, "emea": _EUROPE | _MIDEAST | _AFRICA,
    "germany": {"DE"}, "france": {"FR"}, "spain": {"ES"}, "netherlands": {"NL"}, "poland": {"PL"},
    "portugal": {"PT"}, "apac": _APAC, "asia pacific": _APAC, "asia": _APAC | _MIDEAST, "india": {"IN"},
    "singapore": {"SG"}, "australia": {"AU"}, "new zealand": {"NZ"}, "japan": {"JP"}, "philippines": {"PH"},
    "brazil": {"BR"}, "mexico": {"MX"}, "africa": _AFRICA, "middle east": _MIDEAST, "uae": {"AE"},
}
_REGION_ALT = "|".join(sorted((re.escape(k) for k in REGIONS), key=len, reverse=True))
# "Remote (US only)", "US-based", "US only", "must be located in the United States", "within the EU"
_SCOPE_RES = [
    re.compile(r"\b(?P<r>%s)\s*(?:-|\s)?\s*(?:only|based|residents?)\b" % _REGION_ALT),
    re.compile(r"\b(?:must|need to|needs to|required to|should)\s+(?:be\s+)?(?:located|based|living|reside|residing)"
               r"\s+(?:in|within)\s+(?:the\s+)?(?P<r>%s)\b" % _REGION_ALT),
    re.compile(r"\bremote\s*\(\s*(?P<r>%s)\s*(?:only)?\s*\)" % _REGION_ALT),
    re.compile(r"\b(?:only|exclusively)\s+(?:open\s+to\s+)?(?:candidates|applicants|people)\s+(?:located\s+|based\s+)?"
               r"(?:in|from)\s+(?:the\s+)?(?P<r>%s)\b" % _REGION_ALT),
]
_AUTHORIZED = ("citizen", "permanent_resident", "authorized", "work_permit", "work_visa", "resident", "yes")


def person_countries(prof: dict) -> set:
    out = set()
    loc = prof.get("locations") if isinstance(prof.get("locations"), dict) else {}
    for c in loc.get("countries") or []:
        if isinstance(c, str) and c.strip():
            out.add(c.strip().upper()[:2])
    wa = prof.get("work_authorization") if isinstance(prof.get("work_authorization"), dict) else {}
    for k, v in wa.items():
        if k != "other" and isinstance(k, str) and len(k) == 2 and str(v).lower() in _AUTHORIZED:
            out.add(k.upper())
    return out


def _regions_in(text: str) -> set:
    t = " " + re.sub(r"[^a-z]+", " ", text.lower()) + " "
    found = set()
    for name, cc in REGIONS.items():
        if (" " + name + " ") in t:
            found |= cc
    return found


def _scope_restriction(job) -> set | None:
    """Countries a remote job is restricted to, or None when it is not visibly restricted."""
    scope = _plain(job["remote_scope"])
    if scope:
        if _WORLDWIDE_RE.search(scope):
            return None
        found = _regions_in(scope)
        if found:
            return found
    text = " ".join([_plain(job["location"]), _plain(job["title"]), _plain(job["jd_text"])[:20000]])
    found = set()
    for rx in _SCOPE_RES:
        for m in rx.finditer(text):
            found |= REGIONS.get(" ".join(re.findall(r"[a-z]+", m.group("r"))), set())
    return found or None


def _norm_mode(m: str) -> str:
    s = re.sub(r"[^a-z]", "", m.lower())
    return {"onsite": "onsite", "office": "onsite", "inoffice": "onsite", "remote": "remote",
            "hybrid": "hybrid", "wfh": "remote"}.get(s, s)


def _split_places(location) -> list:
    if not location:
        return []
    parts = re.split(r"\s*(?:[/;|,]|\bor\b|\band\b|\+)\s*", str(location))
    return [p.strip(" ()-") for p in parts if p and p.strip(" ()-")]


def _rule_location(job, prof, config):
    loc = prof.get("locations")
    if not isinstance(loc, dict):
        return None
    cities = [c for c in (loc.get("cities") or []) if isinstance(c, str) and c.strip()]
    modes = [_norm_mode(m) for m in (loc.get("work_modes") or []) if isinstance(m, str)]
    relocate = bool(loc.get("relocate"))
    want_remote = ("remote" in modes) or any(norm_city(c) == "remote" or c.lower() == "remote" for c in cities)
    person_cities = {norm_city(c) for c in cities if c.lower() != "remote"} - {None, "remote"}
    loc_text = _plain(job["location"])
    mode = job["work_mode"] or "unknown"
    if mode == "unknown" and _REMOTE_RE.search(loc_text):
        mode = "remote"
    places = [norm_city(p) for p in _split_places(job["location"])]
    places = [p for p in places if p]
    if job["norm_city"]:
        places.append(job["norm_city"])
    job_cities = {p for p in places if p != "remote" and not _REMOTE_RE.search(p) and p not in REGIONS}
    mentions_remote = mode == "remote" or bool(_REMOTE_RE.search(loc_text))

    def remote_ok():
        if not want_remote:
            return "remote roles are not in your work modes"
        restricted = _scope_restriction(job)
        mine = person_countries(prof)
        if restricted and mine and not (restricted & mine):
            return "remote role restricted to %s" % ", ".join(sorted(restricted)[:6])
        return None

    if mode == "remote":
        return remote_ok()
    if mode in ("hybrid", "onsite"):
        if modes and mode not in modes:
            if mentions_remote and want_remote:
                return remote_ok()
            return "%s roles are not in your work modes" % ("on-site" if mode == "onsite" else mode)
        if job_cities and person_cities and not (job_cities & person_cities) and not relocate:
            return "located in %s, not in your cities" % ", ".join(sorted(job_cities)[:3])
        return None
    # unknown work mode
    if job_cities and person_cities:
        if job_cities & person_cities or relocate:
            return None
        if mentions_remote and want_remote:
            return remote_ok()
        return "located in %s, not in your cities" % ", ".join(sorted(job_cities)[:3])
    if mentions_remote:
        return remote_ok()
    return None


# ---------------------------------------------------------------- rule: years
_YEARS_RE = re.compile(r"(?<![\d.])(\d{1,2})\s*(?:\+|-|to)\s*(\d{1,2})?\s*\+?\s*(?:years|yrs|year)\b")


def years_required(job) -> tuple:
    """(min, max) years asked for: the structured values when given, else the most lenient phrase in the title
    and JD (lowest minimum, highest maximum). (None, None) when nothing is stated."""
    if job["years_min"] is not None or job["years_max"] is not None:
        return job["years_min"], job["years_max"]
    text = _plain(job["title"]) + " \n " + _plain(job["jd_text"])
    mins, maxs = [], []
    for m in _YEARS_RE.finditer(text):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        if lo > YEARS_CAP or (hi is not None and (hi > YEARS_CAP or hi < lo)):
            continue
        mins.append(lo)
        if hi is not None:
            maxs.append(hi)
    return (min(mins) if mins else None), (max(maxs) if maxs else None)


def _rule_years(job, prof, config):
    exp = _num(prof.get("experience_years"))
    if exp is None:
        return None
    tol = _cfg(config, "evaluator", "years_tolerance", default={}) or {}
    below = max(0.0, min(5.0, _num(tol.get("below")) if _num(tol.get("below")) is not None
                         else DEFAULT_TOLERANCE["below"]))
    above = max(0.0, min(5.0, _num(tol.get("above")) if _num(tol.get("above")) is not None
                         else DEFAULT_TOLERANCE["above"]))
    lo, hi = years_required(job)
    if lo is not None and lo > exp + above:
        return "needs %g or more years; you have %g" % (lo, exp)
    if hi is not None and hi < exp - below:
        return "asks for at most %g years; you have %g" % (hi, exp)
    return None


# ---------------------------------------------------------------- rule: seniority in the title
SENIORITY_WORDS = {
    "intern": 0, "internship": 0, "trainee": 0, "apprentice": 0,
    "junior": 1, "jr": 1, "entry level": 1, "graduate": 1, "fresher": 1,
    "associate": 2, "mid level": 3, "senior": 4, "sr": 4, "lead": 5, "staff": 5,
    "principal": 6, "head": 7, "director": 7, "vp": 8, "vice president": 8, "chief": 9,
}
LEVELS = {"intern": 0, "internship": 0, "junior": 1, "entry": 1, "entry_level": 1, "graduate": 1, "associate": 2,
          "mid": 3, "mid_level": 3, "intermediate": 3, "senior": 4, "lead": 5, "staff": 5, "principal": 6,
          "manager": 6, "head": 7, "director": 7, "vp": 8, "executive": 9, "c_level": 9}


def _title_levels(title_words: str) -> dict:
    return {w: r for w, r in SENIORITY_WORDS.items() if _has_phrase(title_words, w)}


def _families(prof) -> list:
    fams = prof.get("role_families")
    return [f for f in fams if isinstance(f, dict)] if isinstance(fams, list) else []


def _rule_seniority(job, prof, config):
    tw = _words(job["title"])
    found = _title_levels(tw)
    if not found:
        return None
    for fam in _families(prof):
        for ex in fam.get("exclude") or []:
            if isinstance(ex, str) and _words(ex).strip() in SENIORITY_WORDS and _has_phrase(tw, ex):
                return "the title says %s, which you excluded" % ex.strip().lower()
    sen = prof.get("seniority")
    if not isinstance(sen, dict):
        return None
    lo = LEVELS.get(str(sen.get("min") or "").lower().replace(" ", "_").replace("-", "_"))
    hi = LEVELS.get(str(sen.get("max") or "").lower().replace(" ", "_").replace("-", "_"))
    top = max(found.values())
    if "intern" in found or "internship" in found:
        top = 0
    word = [w for w, r in found.items() if r == top][0]
    if hi is not None and top > hi:
        return "the title says %s, above your %s level" % (word, sen.get("max"))
    if lo is not None and top < lo:
        return "the title says %s, below your %s level" % (word, sen.get("min"))
    return None


# ---------------------------------------------------------------- rule: role family
def _rule_role_family(job, prof, config):
    fams = _families(prof)
    if not fams:
        return None
    tw = _words(job["title"])
    for fam in fams:
        for ex in fam.get("exclude") or []:
            if isinstance(ex, str) and _words(ex).strip() not in SENIORITY_WORDS and _has_phrase(tw, ex):
                return "the title matches %r, which you excluded" % ex.strip()
    has_include = False
    for fam in fams:
        for kw in list(fam.get("include") or []) + list(fam.get("titles") or []):
            if isinstance(kw, str) and kw.strip():
                has_include = True
                if _has_phrase(tw, kw):
                    return None
    if not has_include:
        return None
    return "the title matches none of your role families"


# ---------------------------------------------------------------- rule: employment type
_ETYPE = {"full_time": "full_time", "fulltime": "full_time", "full time": "full_time", "permanent": "full_time",
          "regular": "full_time", "part_time": "part_time", "parttime": "part_time", "part time": "part_time",
          "contract": "contract", "contractor": "contract", "freelance": "contract", "temporary": "contract",
          "temp": "contract", "fixed term": "contract", "intern": "internship", "internship": "internship",
          "unknown": "unknown", "": "unknown"}


def norm_employment_type(v) -> str:
    s = re.sub(r"[^a-z ]+", " ", str(v or "").lower()).strip()
    s = " ".join(s.split())
    if s in _ETYPE:
        return _ETYPE[s]
    for k, t in _ETYPE.items():
        if k and k in s:
            return t
    return "unknown"


def _rule_employment(job, prof, config):
    et = norm_employment_type(job["employment_type"])
    if et == "unknown" and re.search(r"\b(intern|internship)\b", _plain(job["title"])):
        et = "internship"
    if et == "unknown":
        return None
    wanted = prof.get("employment_types")
    if isinstance(wanted, list) and wanted:
        allowed = {norm_employment_type(w) for w in wanted}
        if et not in allowed:
            return "%s role, which you did not ask for" % et.replace("_", "-")
        return None
    if et in ("internship", "part_time"):
        return "%s role, which you did not ask for" % et.replace("_", "-")
    return None


# ---------------------------------------------------------------- rule: compensation
_PERIOD_FACTOR = {"year": 1.0, "month": 12.0, "week": 52.0, "day": 260.0, "hour": 2080.0}
_CURRENCY_ALIASES = {"rs": "INR", "inr": "INR", "\u20b9": "INR", "$": "USD", "usd": "USD", "us$": "USD",
                     "eur": "EUR", "\u20ac": "EUR", "gbp": "GBP", "\u00a3": "GBP"}


def norm_period(p) -> str | None:
    s = str(p or "").lower()
    if not s:
        return None
    for key, name in (("hour", "hour"), ("day", "day"), ("week", "week"), ("month", "month"), ("year", "year"),
                      ("annum", "year"), ("annual", "year"), ("yr", "year")):
        if key in s:
            return name
    return None


def norm_currency(c) -> str | None:
    s = str(c or "").strip()
    if not s:
        return None
    if s.lower() in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[s.lower()]
    return s.upper() if re.match(r"^[A-Za-z]{3}$", s) else None


def _rule_comp(job, prof, config):
    sal = prof.get("salary")
    if not isinstance(sal, dict):
        return None
    floor = _num(sal.get("floor"))
    pcur = norm_currency(sal.get("currency"))
    pper = norm_period(sal.get("period")) or "year"
    top = job["salary_max"]
    jcur = norm_currency(job["salary_currency"])
    jper = norm_period(job["salary_period"])
    if floor is None or top is None or not pcur or not jcur or jcur != pcur or not jper:
        return None
    annual_top = top * _PERIOD_FACTOR[jper]
    annual_floor = floor * _PERIOD_FACTOR[pper]
    if annual_top < annual_floor:
        return "pays at most %s %s a %s, below your floor" % (jcur, _fmt_money(top), jper)
    return None


def _fmt_money(v: float) -> str:
    return "{:,.0f}".format(v)


# ---------------------------------------------------------------- rule: language or work authorization
_LANGS = ("german", "french", "spanish", "japanese", "mandarin", "chinese", "cantonese", "dutch", "portuguese",
          "italian", "korean", "arabic", "russian", "polish", "swedish", "norwegian", "danish", "finnish", "turkish",
          "hebrew", "hindi", "tamil", "telugu", "kannada", "marathi", "bengali", "malayalam", "gujarati", "punjabi",
          "urdu", "thai", "vietnamese", "indonesian", "czech", "greek", "hungarian", "romanian")
_LANG_ALT = "|".join(_LANGS)
_LANG_RES = [
    re.compile(r"\b(?:fluent|fluency|native|proficien\w*|business[- ]level|professional working)\s+(?:in\s+|level\s+)?"
               r"(?:of\s+)?(?P<l>%s)\b" % _LANG_ALT),
    re.compile(r"\b(?P<l>%s)\s+(?:language\s+)?(?:skills\s+)?(?:is\s+|are\s+)?(?:required|mandatory|a must|essential)\b"
               % _LANG_ALT),
    re.compile(r"\b(?:must|need to|required to)\s+speak\s+(?P<l>%s)\b" % _LANG_ALT),
]
_AUTH_RES = [
    re.compile(r"\b(?:must|need to|needs to|required to|should)\s+(?:be\s+)?(?:legally\s+)?(?:authori[sz]ed|eligible|"
               r"permitted|entitled)\s+to\s+work\s+in\s+(?:the\s+)?(?P<r>%s)\b" % _REGION_ALT),
    re.compile(r"\b(?P<r>%s)\s+(?:citizens?|citizenship|nationals?)\s+(?:only|required)\b" % _REGION_ALT),
    re.compile(r"\b(?:right|authori[sz]ation)\s+to\s+work\s+in\s+(?:the\s+)?(?P<r>%s)\s+(?:is\s+)?(?:required|needed)\b"
               % _REGION_ALT),
]
_NO_SPONSOR_RE = re.compile(r"\b(?:no|not|unable to|cannot|can ?not|can't|will not|won't|do not|does not)\s+"
                            r"(?:provide\s+|offer\s+|support\s+)?(?:visa\s+|work\s+permit\s+)?sponsor")
_NEEDS = ("needs_sponsorship", "no", "none", "not_authorized", "unauthorized", "needs_visa")


def _rule_lang_auth(job, prof, config):
    text = _plain(job["jd_text"])[:30000] + " " + _plain(job["title"])
    langs = prof.get("languages")
    if isinstance(langs, list) and langs:
        mine = {str(x).strip().lower() for x in langs}
        for rx in _LANG_RES:
            for m in rx.finditer(text):
                lang = m.group("l")
                if lang not in mine and not (lang == "chinese" and "mandarin" in mine):
                    return "requires %s, which is not in your languages" % lang.capitalize()
    wa = prof.get("work_authorization")
    if isinstance(wa, dict) and wa:
        countries = set()
        for rx in _AUTH_RES:
            for m in rx.finditer(text):
                countries |= REGIONS.get(" ".join(re.findall(r"[a-z]+", m.group("r"))), set())
        no_sponsor = bool(_NO_SPONSOR_RE.search(text))
        for cc in sorted(countries):
            status = str(wa.get(cc, wa.get(cc.lower(), wa.get("other", ""))) or "").lower()
            if status in _NEEDS or (no_sponsor and status not in _AUTHORIZED):
                return "requires work authorization in %s" % cc
    return None


# ---------------------------------------------------------------- rule: unnamed agency posts
_AGENCY_RE = re.compile(r"\b(leading|reputed|top|well[- ]known|renowned|large|big|fortune \d+)\s+(mnc|client|company|"
                        r"organi[sz]ation|firm|brand)\b|\bconfidential\s+(client|company)\b|\bour\s+client\b|"
                        r"\b(?:hiring|recruiting)\s+for\s+(?:a|one of our|our)\s+client\b|\bclient\s+of\s+ours\b")
AGENCY_SKIP_FIELD = "skip_unnamed_agency"


def _rule_agency(job, prof, config):
    if prof.get(AGENCY_SKIP_FIELD) is not True:
        return None
    text = _plain(job["company"]) + " " + _plain(job["title"]) + " " + _plain(job["jd_text"])[:8000]
    if _AGENCY_RE.search(text) or re.search(r"\bconfidential\b", _plain(job["company"])):
        return "agency post without a named client"
    return None


# ---------------------------------------------------------------- driver
RULES = (
    ("stale", _rule_stale),
    ("location", _rule_location),
    ("years_required", _rule_years),
    ("seniority_title", _rule_seniority),
    ("role_family", _rule_role_family),
    ("employment_type", _rule_employment),
    ("comp_below_floor", _rule_comp),
    ("language_or_authorization", _rule_lang_auth),
    ("agency_unnamed", _rule_agency),
)


def explain(job: dict, profile: dict | None, config: dict | None) -> tuple[bool, str | None, str]:
    """(ok, reason_code, sentence). The sentence is shown in the Sheet's Skipped by filters tab."""
    view = job_view(job)
    prof = profile_values(profile)
    for code, fn in RULES:
        why = fn(view, prof, config or {})
        if why:
            return False, code, why[0].upper() + why[1:]
    return True, None, ""


def check(job: dict, profile: dict, config: dict) -> tuple[bool, str | None]:
    """(True, None) when the job survives, else (False, reason_code) (design 6.2)."""
    ok, code, _ = explain(job, profile, config)
    return ok, code


def reason_sentence(code: str) -> str:
    """Generic sentence for a reason code (used when the specific one is not stored)."""
    return {
        "stale": "The posting is too old or closed",
        "location": "The location or work mode does not match yours",
        "years_required": "The years of experience asked for do not match yours",
        "seniority_title": "The seniority in the title does not match yours",
        "role_family": "The role is outside your role families",
        "employment_type": "The employment type is not one you asked for",
        "comp_below_floor": "The disclosed pay is below your floor",
        "language_or_authorization": "It needs a language or work authorization you do not have",
        "agency_unnamed": "Agency post without a named client",
        "excluded_company": "The company is on your exclusion list",
        "excluded_job_url": "The posting is on your exclusion list",
    }.get(code, code.replace("_", " ").capitalize())
