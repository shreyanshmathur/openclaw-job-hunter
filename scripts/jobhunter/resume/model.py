"""Base resume model (`private/resume/base.json`), structured dates, text rules and the plain-text view
(design 6.4, 12.9).

The base resume is written once by the evaluator at onboarding (`profile infer-record`) and confirmed by
the person with `resume base-review`. Every date is structured (`{"start": "YYYY-MM", "end": "YYYY-MM" |
"present"}`; a year-only resume may use "YYYY"), so the renderers print "Jan 2020 to Present" and no dash
character can appear. Every dash of the original text was converted by the evaluator and listed in
`dash_conversions` ({location, original, replacement}), which `resume base-review` shows.

Shape (unknown keys are refused with E_SCHEMA; content rules with E_VALIDATION):

    {"version": 1,
     "contact": {"full_name", "first_name", "last_name", "email", "phone", "location",
                 "links": [{"label", "url"}]},
     "headline": str|null, "summary": str|null,
     "sections_order": ["summary", "experience", "projects", "skills", "education", "certifications", "languages"],
     "experience": [{"role_id": "E1", "employer", "title", "location", "dates": {...},
                     "bullets": [{"id": "E1.B1", "text"}]}],
     "projects": [{"project_id": "PR1", "name", "role", "link", "dates": {...}|null,
                   "bullets": [{"id": "PR1.B1", "text"}]}],
     "education": [{"edu_id": "ED1", "institution", "degree", "location", "dates": {...}|null, "details": [str]}],
     "certifications": [{"cert_id": "C1", "name", "issuer", "date": "YYYY-MM"|null}],
     "skills": [str], "languages": [str],
     "dash_conversions": [{"location", "original", "replacement"}]}
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata

from ..errors import Denied
from . import fonts_helvetica

SECTIONS = ("summary", "experience", "projects", "skills", "education", "certifications", "languages")
DEFAULT_ORDER = SECTIONS
SECTION_TITLES = {"summary": "Summary", "experience": "Experience", "projects": "Projects", "skills": "Skills",
                  "education": "Education", "certifications": "Certifications", "languages": "Languages"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")

# character rules (same code points as the QC linter, writing-qc 8.4)
DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2043\u2212\u2e3a\u2e3b\ufe31\ufe32\ufe58\ufe63\uff0d]")
SPACED_HYPHEN = re.compile(r"(?<=\S)[ \t]+-{1,3}[ \t]+(?=\S)|--")
CURLY = re.compile("[\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u2032\u2033\u00b4]")
ELLIPSIS = re.compile(r"\u2026|\.{3,}")
INVISIBLE = re.compile("[\u00a0\u00ad\u180e\u200b-\u200f\u2028\u2029\u202a-\u202f\u205f-\u2064\u3000\ufeff]")
EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u2300-\u23ff\ufe0f\u20e3]")
BULLET_CHARS = re.compile("[\u2022\u2023\u2219\u25aa\u25ab\u25cf\u25e6\u2043]")
CHAR_RULES = (("DASH", DASHES), ("SPACED-HYPHEN", SPACED_HYPHEN), ("CURLY", CURLY), ("ELLIPSIS", ELLIPSIS),
              ("INVISIBLE", INVISIBLE), ("EMOJI", EMOJI), ("BULLET-CHAR", BULLET_CHARS))

_DATE_RE = re.compile(r"^([0-9]{4})(?:-(0[1-9]|1[0-2]))?$")
_ROLE_RE = re.compile(r"^E[1-9][0-9]{0,2}$")
_PROJECT_RE = re.compile(r"^PR[1-9][0-9]{0,2}$")
_EDU_RE = re.compile(r"^ED[1-9][0-9]{0,2}$")
_CERT_RE = re.compile(r"^C[1-9][0-9]{0,2}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

_TOP_KEYS = {"version", "contact", "headline", "summary", "sections_order", "experience", "projects", "education",
             "certifications", "skills", "languages", "dash_conversions"}
_CONTACT_KEYS = {"full_name", "first_name", "last_name", "email", "phone", "location", "links"}
_ROLE_KEYS = {"role_id", "employer", "title", "location", "dates", "bullets"}
_PROJECT_KEYS = {"project_id", "name", "role", "link", "dates", "bullets"}
_EDU_KEYS = {"edu_id", "institution", "degree", "location", "dates", "details"}
_CERT_KEYS = {"cert_id", "name", "issuer", "date"}
_BULLET_KEYS = {"id", "text"}
_CONV_KEYS = {"location", "original", "replacement"}

MAX_BULLET_CHARS = 400
MAX_LINE_CHARS = 200


# ---------------------------------------------------------------- errors
class _Errors:
    def __init__(self):
        self.schema: list[str] = []
        self.content: list[str] = []

    def s(self, path: str, msg: str) -> None:
        self.schema.append("%s: %s" % (path, msg))

    def c(self, path: str, msg: str) -> None:
        self.content.append("%s: %s" % (path, msg))

    def raise_if_any(self, what: str) -> None:
        if self.schema:
            raise Denied("E_SCHEMA", "%s does not match the resume schema" % what,
                         data={"errors": self.schema[:50] + self.content[:50]})
        if self.content:
            raise Denied("E_VALIDATION", "%s breaks the resume text rules" % what, data={"errors": self.content[:100]})


# ---------------------------------------------------------------- text rules
def char_findings(text: str) -> list[tuple[str, str]]:
    """[(rule, detail)] for every character rule the text breaks (rule names without prefix)."""
    out = []
    if not isinstance(text, str):
        return out
    for name, rx in CHAR_RULES:
        for m in rx.finditer(text):
            out.append((name, "U+%04X at %d" % (ord(m.group(0)[0]), m.start()) if len(m.group(0)) == 1
                        else "%r at %d" % (m.group(0), m.start())))
    for ch in sorted({c for c in text if ord(c) > 126 or (ord(c) < 32 and c not in "\n")}):
        if any(rx.match(ch) for _n, rx in CHAR_RULES):
            continue
        if ord(ch) < 32 or ord(ch) == 127:
            out.append(("CONTROL", "U+%04X" % ord(ch)))
        elif not fonts_helvetica.can_encode(ch):
            out.append(("NON-WINANSI", "U+%04X %s" % (ord(ch), unicodedata.name(ch, "?"))))
        elif unicodedata.category(ch)[0] != "L":
            out.append(("NON-ASCII", "U+%04X %s" % (ord(ch), unicodedata.name(ch, "?"))))
    return out


def _check_text(err: _Errors, path: str, value, *, required: bool, max_len: int = MAX_LINE_CHARS,
                multiline: bool = False) -> None:
    if value is None:
        if required:
            err.s(path, "is required")
        return
    if not isinstance(value, str):
        err.s(path, "must be a string")
        return
    if required and not value.strip():
        err.s(path, "must not be empty")
        return
    if len(value) > max_len:
        err.c(path, "is longer than %d characters" % max_len)
    if not multiline and ("\n" in value or "\r" in value):
        err.c(path, "must be one line")
    for rule, detail in char_findings(value):
        err.c(path, "%s %s" % (rule, detail))


def _check_url(err: _Errors, path: str, value, required: bool = False) -> None:
    if value is None:
        if required:
            err.s(path, "is required")
        return
    if not isinstance(value, str) or not re.match(r"^https?://[^\s]+$", value) or len(value) > 300:
        err.c(path, "must be an http(s) URL without spaces")
        return
    if not all(32 < ord(c) < 127 for c in value):
        err.c(path, "must be plain ASCII")


# ---------------------------------------------------------------- dates
def parse_date(value) -> tuple[int, int | None]:
    """'2020-01' -> (2020, 1); '2020' -> (2020, None). ValueError otherwise."""
    if not isinstance(value, str):
        raise ValueError("date must be a string")
    m = _DATE_RE.match(value)
    if not m:
        raise ValueError("date must be YYYY-MM or YYYY, got %r" % value)
    year = int(m.group(1))
    if year < 1950 or year > 2100:
        raise ValueError("year %d is out of range" % year)
    return year, (int(m.group(2)) if m.group(2) else None)


def _date_key(value: str) -> tuple[int, int]:
    y, mth = parse_date(value)
    return y, (mth or 0)


def _check_dates(err: _Errors, path: str, dates, *, start_required: bool) -> None:
    if dates is None:
        if start_required:
            err.s(path, "is required")
        return
    if not isinstance(dates, dict):
        err.s(path, "must be an object {start, end}")
        return
    extra = set(dates) - {"start", "end"}
    if extra:
        err.s(path, "unknown keys %s" % sorted(extra))
    start, end = dates.get("start"), dates.get("end")
    if start is None and start_required:
        err.s(path + ".start", "is required")
    if end is None:
        err.s(path + ".end", "is required (a date or 'present')")
    for key, val in (("start", start), ("end", end)):
        if val is None or (key == "end" and val == "present"):
            continue
        try:
            parse_date(val)
        except ValueError as exc:
            err.s("%s.%s" % (path, key), str(exc))
    if start and end and end != "present":
        try:
            if _date_key(start) > _date_key(end):
                err.c(path, "start is after end")
        except ValueError:
            pass


def format_date(value: str, opts: dict | None = None) -> str:
    """'2020-01' -> 'Jan 2020' (date_format 'MMM YYYY'); 'present' -> present_word."""
    opts = opts or {}
    if value == "present":
        return str(opts.get("present_word") or "Present")
    year, month = parse_date(value)
    if month is None:
        return str(year)
    fmt = str(opts.get("date_format") or "MMM YYYY")
    if fmt == "MMMM YYYY":
        return "%s %d" % (MONTHS_LONG[month - 1], year)
    if fmt == "MM/YYYY":
        return "%02d/%d" % (month, year)
    if fmt == "YYYY":
        return str(year)
    return "%s %d" % (MONTHS[month - 1], year)


def format_range(dates: dict | None, opts: dict | None = None) -> str:
    """{'start': '2020-01', 'end': 'present'} -> 'Jan 2020 to Present'. Never produces a dash."""
    if not dates:
        return ""
    opts = opts or {}
    word = str(opts.get("range_word") or "to")
    start, end = dates.get("start"), dates.get("end")
    if start and end:
        if start == end:
            return format_date(start, opts)
        return "%s %s %s" % (format_date(start, opts), word, format_date(end, opts))
    if end:
        return format_date(end, opts)
    return format_date(start, opts) if start else ""


# ---------------------------------------------------------------- validation
def _check_bullets(err: _Errors, path: str, bullets, owner_id: str, seen: set) -> None:
    if not isinstance(bullets, list):
        err.s(path, "must be a list")
        return
    rx = re.compile("^" + re.escape(owner_id) + r"\.B[1-9][0-9]{0,2}$")
    for i, b in enumerate(bullets):
        bp = "%s[%d]" % (path, i)
        if not isinstance(b, dict):
            err.s(bp, "must be an object {id, text}")
            continue
        extra = set(b) - _BULLET_KEYS
        if extra:
            err.s(bp, "unknown keys %s" % sorted(extra))
        bid = b.get("id")
        if not isinstance(bid, str) or not rx.match(bid):
            err.s(bp + ".id", "must look like %s.B1" % owner_id)
        elif bid in seen:
            err.s(bp + ".id", "duplicate id %s" % bid)
        else:
            seen.add(bid)
        _check_text(err, bp + ".text", b.get("text"), required=True, max_len=MAX_BULLET_CHARS)


def _list_of_objects(err: _Errors, path: str, value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        err.s(path, "must be a list")
        return []
    return value


def validate(base, *, what: str = "base resume") -> dict:
    """Validate a base resume and return a normalized deep copy (missing optional keys filled).
    Denied(E_SCHEMA) for structure, Denied(E_VALIDATION) for text rules (dashes, characters, lengths)."""
    err = _Errors()
    if not isinstance(base, dict):
        raise Denied("E_SCHEMA", "%s must be a JSON object" % what)
    extra = set(base) - _TOP_KEYS
    if extra:
        err.s("$", "unknown keys %s" % sorted(extra))
    if base.get("version", 1) != 1:
        err.s("version", "must be 1")
    contact = base.get("contact")
    if not isinstance(contact, dict):
        err.s("contact", "is required")
        contact = {}
    else:
        extra = set(contact) - _CONTACT_KEYS
        if extra:
            err.s("contact", "unknown keys %s" % sorted(extra))
    for key in ("full_name", "first_name", "last_name"):
        _check_text(err, "contact." + key, contact.get(key), required=True, max_len=80)
    email = contact.get("email")
    if email is not None and (not isinstance(email, str) or not _EMAIL_RE.match(email)):
        err.c("contact.email", "is not an email address")
    _check_text(err, "contact.phone", contact.get("phone"), required=False, max_len=40)
    _check_text(err, "contact.location", contact.get("location"), required=False, max_len=80)
    links = _list_of_objects(err, "contact.links", contact.get("links"))
    for i, ln in enumerate(links):
        lp = "contact.links[%d]" % i
        if not isinstance(ln, dict) or set(ln) - {"label", "url"}:
            err.s(lp, "must be an object {label, url}")
            continue
        _check_text(err, lp + ".label", ln.get("label"), required=False, max_len=40)
        _check_url(err, lp + ".url", ln.get("url"), required=True)
    _check_text(err, "headline", base.get("headline"), required=False, max_len=160)
    _check_text(err, "summary", base.get("summary"), required=False, max_len=600, multiline=False)

    order = base.get("sections_order")
    if order is not None:
        if not isinstance(order, list) or not all(isinstance(s, str) for s in order):
            err.s("sections_order", "must be a list of section names")
        else:
            bad = [s for s in order if s not in SECTIONS]
            if bad:
                err.s("sections_order", "unknown sections %s" % bad)
            if len(set(order)) != len(order):
                err.s("sections_order", "has duplicates")

    seen_bullets: set = set()
    role_ids: set = set()
    roles = _list_of_objects(err, "experience", base.get("experience"))
    for i, r in enumerate(roles):
        rp = "experience[%d]" % i
        if not isinstance(r, dict):
            err.s(rp, "must be an object")
            continue
        extra = set(r) - _ROLE_KEYS
        if extra:
            err.s(rp, "unknown keys %s" % sorted(extra))
        rid = r.get("role_id")
        if not isinstance(rid, str) or not _ROLE_RE.match(rid):
            err.s(rp + ".role_id", "must look like E1")
            rid = "E0"
        elif rid in role_ids:
            err.s(rp + ".role_id", "duplicate id %s" % rid)
        role_ids.add(rid)
        _check_text(err, rp + ".employer", r.get("employer"), required=True, max_len=120)
        _check_text(err, rp + ".title", r.get("title"), required=True, max_len=120)
        _check_text(err, rp + ".location", r.get("location"), required=False, max_len=80)
        _check_dates(err, rp + ".dates", r.get("dates"), start_required=True)
        _check_bullets(err, rp + ".bullets", r.get("bullets", []), rid, seen_bullets)
        if isinstance(r.get("bullets"), list) and not r.get("bullets"):
            err.s(rp + ".bullets", "a role needs at least one bullet")

    proj_ids: set = set()
    for i, p in enumerate(_list_of_objects(err, "projects", base.get("projects"))):
        pp = "projects[%d]" % i
        if not isinstance(p, dict):
            err.s(pp, "must be an object")
            continue
        extra = set(p) - _PROJECT_KEYS
        if extra:
            err.s(pp, "unknown keys %s" % sorted(extra))
        pid = p.get("project_id")
        if not isinstance(pid, str) or not _PROJECT_RE.match(pid):
            err.s(pp + ".project_id", "must look like PR1")
            pid = "PR0"
        elif pid in proj_ids:
            err.s(pp + ".project_id", "duplicate id %s" % pid)
        proj_ids.add(pid)
        _check_text(err, pp + ".name", p.get("name"), required=True, max_len=120)
        _check_text(err, pp + ".role", p.get("role"), required=False, max_len=80)
        _check_url(err, pp + ".link", p.get("link"))
        _check_dates(err, pp + ".dates", p.get("dates"), start_required=False)
        _check_bullets(err, pp + ".bullets", p.get("bullets", []), pid, seen_bullets)

    edu_ids: set = set()
    for i, e in enumerate(_list_of_objects(err, "education", base.get("education"))):
        ep = "education[%d]" % i
        if not isinstance(e, dict):
            err.s(ep, "must be an object")
            continue
        extra = set(e) - _EDU_KEYS
        if extra:
            err.s(ep, "unknown keys %s" % sorted(extra))
        eid = e.get("edu_id")
        if not isinstance(eid, str) or not _EDU_RE.match(eid):
            err.s(ep + ".edu_id", "must look like ED1")
        elif eid in edu_ids:
            err.s(ep + ".edu_id", "duplicate id %s" % eid)
        else:
            edu_ids.add(eid)
        _check_text(err, ep + ".institution", e.get("institution"), required=True, max_len=120)
        _check_text(err, ep + ".degree", e.get("degree"), required=True, max_len=160)
        _check_text(err, ep + ".location", e.get("location"), required=False, max_len=80)
        _check_dates(err, ep + ".dates", e.get("dates"), start_required=False)
        details = e.get("details", [])
        if not isinstance(details, list):
            err.s(ep + ".details", "must be a list of strings")
        else:
            for j, d in enumerate(details):
                _check_text(err, "%s.details[%d]" % (ep, j), d, required=True, max_len=MAX_BULLET_CHARS)

    cert_ids: set = set()
    for i, c in enumerate(_list_of_objects(err, "certifications", base.get("certifications"))):
        cp = "certifications[%d]" % i
        if not isinstance(c, dict):
            err.s(cp, "must be an object")
            continue
        extra = set(c) - _CERT_KEYS
        if extra:
            err.s(cp, "unknown keys %s" % sorted(extra))
        cid = c.get("cert_id")
        if not isinstance(cid, str) or not _CERT_RE.match(cid):
            err.s(cp + ".cert_id", "must look like C1")
        elif cid in cert_ids:
            err.s(cp + ".cert_id", "duplicate id %s" % cid)
        else:
            cert_ids.add(cid)
        _check_text(err, cp + ".name", c.get("name"), required=True, max_len=160)
        _check_text(err, cp + ".issuer", c.get("issuer"), required=False, max_len=120)
        if c.get("date") is not None:
            try:
                parse_date(c.get("date"))
            except ValueError as exc:
                err.s(cp + ".date", str(exc))

    for key in ("skills", "languages"):
        vals = base.get(key, [])
        if not isinstance(vals, list):
            err.s(key, "must be a list of strings")
            continue
        seen = set()
        for i, s in enumerate(vals):
            _check_text(err, "%s[%d]" % (key, i), s, required=True, max_len=60)
            if isinstance(s, str):
                k = s.strip().lower()
                if k in seen:
                    err.s("%s[%d]" % (key, i), "duplicate %r" % s)
                seen.add(k)
    if isinstance(base.get("skills"), list) and len(base.get("skills")) > 80:
        err.c("skills", "more than 80 skills")

    convs = base.get("dash_conversions", [])
    if not isinstance(convs, list):
        err.s("dash_conversions", "must be a list")
    else:
        for i, cv in enumerate(convs):
            cp = "dash_conversions[%d]" % i
            if not isinstance(cv, dict) or set(cv) - _CONV_KEYS or not all(
                    isinstance(cv.get(k), str) for k in _CONV_KEYS):
                err.s(cp, "must be an object {location, original, replacement} of strings")
                continue
            # the original keeps its dash on purpose (it is what the person reviews); the replacement may not
            for rule, detail in char_findings(cv["replacement"]):
                if rule in ("DASH", "SPACED-HYPHEN"):
                    err.c(cp + ".replacement", "%s %s" % (rule, detail))
    err.raise_if_any(what)

    out = copy.deepcopy(base)
    out["version"] = 1
    out.setdefault("headline", None)
    out.setdefault("summary", None)
    out["sections_order"] = list(base.get("sections_order") or DEFAULT_ORDER)
    for key in ("experience", "projects", "education", "certifications", "skills", "languages",
                "dash_conversions"):
        out[key] = list(out.get(key) or [])
    c = out["contact"]
    for key in ("email", "phone", "location"):
        c.setdefault(key, None)
    c["links"] = list(c.get("links") or [])
    for r in out["experience"]:
        r.setdefault("location", None)
    for p in out["projects"]:
        for key in ("role", "link", "dates"):
            p.setdefault(key, None)
        p["bullets"] = list(p.get("bullets") or [])
    for e in out["education"]:
        for key in ("location", "dates"):
            e.setdefault(key, None)
        e["details"] = list(e.get("details") or [])
    for cert in out["certifications"]:
        cert.setdefault("issuer", None)
        cert.setdefault("date", None)
    return out


def assert_printable(model: dict) -> None:
    """Last line of defence in both renderers: no dash or spaced hyphen can reach a file."""
    text = all_text(model)
    for rx in (DASHES, SPACED_HYPHEN):
        m = rx.search(text)
        if m:
            raise ValueError("refusing to render a dash or spaced hyphen: %r" % m.group(0))


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def base_sha256(base: dict) -> str:
    """Version hash of a base resume (dash_conversions excluded: they do not change what is rendered)."""
    b = {k: v for k, v in base.items() if k != "dash_conversions"}
    return hashlib.sha256(canonical_json(b).encode("ascii")).hexdigest()


# ---------------------------------------------------------------- lookups
def bullet_index(base: dict) -> dict:
    """{bullet_id: (owner_id, text)} for experience and project bullets."""
    out = {}
    for r in base.get("experience", []):
        for b in r.get("bullets", []):
            out[b["id"]] = (r["role_id"], b["text"])
    for p in base.get("projects", []):
        for b in p.get("bullets", []):
            out[b["id"]] = (p["project_id"], b["text"])
    return out


def names(model: dict) -> list[str]:
    """Proper names in the model (for the QC linter's non-ASCII name exemption)."""
    out = []
    c = model.get("contact") or {}
    for key in ("full_name", "first_name", "last_name", "location"):
        if c.get(key):
            out.append(c[key])
    for r in model.get("experience", []):
        out.extend(x for x in (r.get("employer"), r.get("location")) if x)
    for e in model.get("education", []):
        out.extend(x for x in (e.get("institution"), e.get("location")) if x)
    return out


def all_text(model: dict) -> str:
    """Every text field of a model, one per line (for corpus building and character checks)."""
    parts: list[str] = []

    def add(v):
        if isinstance(v, str) and v:
            parts.append(v)

    c = model.get("contact") or {}
    for key in ("full_name", "first_name", "last_name", "email", "phone", "location"):
        add(c.get(key))
    for ln in c.get("links") or []:
        add(ln.get("label"))
    add(model.get("headline"))
    add(model.get("summary"))
    for r in model.get("experience", []):
        for key in ("employer", "title", "location"):
            add(r.get(key))
        for b in r.get("bullets", []):
            add(b.get("text"))
    for p in model.get("projects", []):
        for key in ("name", "role"):
            add(p.get(key))
        for b in p.get("bullets", []):
            add(b.get("text"))
    for e in model.get("education", []):
        for key in ("institution", "degree", "location"):
            add(e.get(key))
        for d in e.get("details", []):
            add(d)
    for cert in model.get("certifications", []):
        add(cert.get("name"))
        add(cert.get("issuer"))
    for s in model.get("skills", []):
        add(s)
    for s in model.get("languages", []):
        add(s)
    return "\n".join(parts)


# ---------------------------------------------------------------- render model
def to_render_model(base: dict) -> dict:
    """The model the renderers draw: the base as it is (base variant, tailoring mode off)."""
    m = copy.deepcopy(base)
    m.pop("dash_conversions", None)
    return m


def contact_items(model: dict) -> list[tuple[str, str | None]]:
    """[(text, uri or None)] for the contact line, in display order."""
    c = model.get("contact") or {}
    items: list[tuple[str, str | None]] = []
    if c.get("email"):
        items.append((c["email"], "mailto:" + c["email"]))
    if c.get("phone"):
        items.append((c["phone"], None))
    if c.get("location"):
        items.append((c["location"], None))
    for ln in c.get("links") or []:
        url = ln.get("url")
        label = ln.get("label") or re.sub(r"^https?://(www\.)?", "", url or "").rstrip("/")
        items.append((label, url))
    return items


def sections(model: dict) -> list[str]:
    """Sections that have content, in the model's order (sections not named in the order follow it)."""
    order = list(model.get("sections_order") or DEFAULT_ORDER)
    for s in DEFAULT_ORDER:
        if s not in order:
            order.append(s)
    out = []
    for s in order:
        if s == "summary":
            if model.get("summary"):
                out.append(s)
        elif model.get(s):
            out.append(s)
    return out


def role_heading(r: dict) -> str:
    parts = [r.get("title") or "", r.get("employer") or ""]
    if r.get("location"):
        parts.append(r["location"])
    return ", ".join(p for p in parts if p)


def render_txt(model: dict, opts: dict | None = None) -> str:
    """Plain-text view used for linting, review and forms that take pasted text. No bullet glyphs and no
    list markers: bullets are indented lines."""
    opts = opts or {}
    c = model.get("contact") or {}
    lines = [c.get("full_name") or ""]
    if model.get("headline"):
        lines.append(model["headline"])
    items = [t for t, _u in contact_items(model)]
    if items:
        lines.append(" | ".join(items))
    for sec in sections(model):
        lines.append("")
        lines.append(SECTION_TITLES[sec])
        if sec == "summary":
            lines.append(model["summary"])
        elif sec == "experience":
            for r in model["experience"]:
                head = role_heading(r)
                rng = format_range(r.get("dates"), opts)
                lines.append(head + ((", " + rng) if rng else ""))
                for b in r.get("bullets", []):
                    lines.append("  " + b["text"])
        elif sec == "projects":
            for p in model["projects"]:
                head = p["name"] + ((", " + p["role"]) if p.get("role") else "")
                rng = format_range(p.get("dates"), opts)
                lines.append(head + ((", " + rng) if rng else ""))
                if p.get("link"):
                    lines.append("  " + p["link"])
                for b in p.get("bullets", []):
                    lines.append("  " + b["text"])
        elif sec == "skills":
            lines.append(", ".join(model["skills"]))
        elif sec == "education":
            for e in model["education"]:
                head = e["degree"] + ", " + e["institution"] + ((", " + e["location"]) if e.get("location") else "")
                rng = format_range(e.get("dates"), opts)
                lines.append(head + ((", " + rng) if rng else ""))
                for d in e.get("details", []):
                    lines.append("  " + d)
        elif sec == "certifications":
            for cert in model["certifications"]:
                head = cert["name"] + ((", " + cert["issuer"]) if cert.get("issuer") else "")
                if cert.get("date"):
                    head += ", " + format_date(cert["date"], opts)
                lines.append(head)
        elif sec == "languages":
            lines.append(", ".join(model["languages"]))
    return "\n".join(lines).strip() + "\n"


def review_text(base: dict, opts: dict | None = None) -> str:
    """Text for `resume base-review`: the rendered base followed by every dash the evaluator replaced."""
    out = [render_txt(to_render_model(base), opts).rstrip("\n"), "", "Dash conversions (%d)" % len(
        base.get("dash_conversions") or [])]
    for i, cv in enumerate(base.get("dash_conversions") or [], 1):
        out.append("  %d. %s" % (i, cv.get("location")))
        out.append("     original:    %s" % cv.get("original"))
        out.append("     replacement: %s" % cv.get("replacement"))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- numbers
NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
                "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
                "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "hundred": "100",
                "thousand": "1000", "million": "1000000", "billion": "1000000000", "dozen": "12", "half": "0.5",
                "twice": "2", "double": "2", "triple": "3", "lakh": "100000", "crore": "10000000"}
_NUM_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:[.,]\d+)*)\s*(%|x|k|m|mn|bn|b|cr|l|lakh|lakhs|crore|crores|"
                     r"million|billion|thousand)?(?![A-Za-z0-9])", re.I)
_WORD_RE = re.compile(r"[A-Za-z]+")


def numbers(text: str | None) -> set[tuple[str, str]]:
    """{(number, unit)}: '18%' -> ('18', '%'), '2.5M' -> ('2.5', 'm'), 'two' -> ('2', '')."""
    out: set[tuple[str, str]] = set()
    if not text:
        return out
    for m in _NUM_RE.finditer(text):
        num = m.group(1).replace(",", "")
        if "." in num:
            num = num.rstrip("0").rstrip(".") or "0"
        unit = (m.group(2) or "").lower()
        unit = {"lakhs": "lakh", "crores": "crore", "mn": "m", "million": "m", "billion": "b", "bn": "b",
                "thousand": "k", "l": "lakh", "cr": "crore"}.get(unit, unit)
        out.add((num, unit))
    for w in _WORD_RE.findall(text):
        v = NUMBER_WORDS.get(w.lower())
        if v:
            out.add((v, ""))
    return out


def numbers_subset(new: set, allowed: set) -> set:
    """Numbers of `new` that `allowed` does not cover. A bare number is covered by the same number with
    any unit; a number with a unit needs the same unit."""
    plain = {n for n, _u in allowed}
    missing = set()
    for n, u in new:
        if (n, u) in allowed:
            continue
        if not u and n in plain:
            continue
        missing.add((n, u))
    return missing
