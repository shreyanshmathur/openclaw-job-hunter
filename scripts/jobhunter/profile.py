"""The person's profile: onboarding import, salary record, inference record, interview answers,
feasibility check and the confirmed profile every gate reads (design 6, 8, 12.6, 12.9, 12.16).

Files (all under private/, mode 600):
  profile.json               12.6 shape: {version, profile_version, fields, facts} plus the interview state
                             ({answers, inferred, open_questions}); only fields whose source is
                             `user_confirmed` are used by any gate
  profile_inference.json     the evaluator's inference record as validated (12.9)
  salary_research.json       the scout's salary research (12.16)
  resume/original.<ext>, resume/resume.txt, extra_info.md   the person's inputs
  resume/base.json           the base resume written from the inference (resume.save_base)

Public API (12.10): load_confirmed(), facts(), status(), record_inference(conn, data), record_salary(conn, data),
answer(conn, field, value, source). Also: questions(), feasibility(), import_resume(), interview().
Nothing inferred is used by a gate until the person confirms it (principle 7); `answer` records only what
the person typed or accepted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

from . import paths
from .canon import now
from .errors import Denied
from .events import enqueue_notification, log_event

PROFILE_VERSION = 1
FACT_ID_RE = re.compile(r"^P[1-9][0-9]{0,3}$")
TASK_UID_RE = re.compile(r"^H[A-Z2-7]{7}$")
SENIORITY = ("intern", "entry", "associate", "mid", "senior", "lead", "manager", "director", "executive")
SENIORITY_MIN_YEARS = {"intern": 0, "entry": 0, "associate": 1, "mid": 3, "senior": 5, "lead": 7, "manager": 6,
                       "director": 10, "executive": 15}
WORK_MODES = ("remote", "hybrid", "onsite")
AUTH_STATUSES = ("citizen", "permanent_resident", "work_visa", "needs_sponsorship", "not_authorized")
PERIODS = ("year", "month", "hour")
SALARY_BASIS_KINDS = ("salary_page", "job_posting", "model_estimate", "user_stated")
FACT_ORIGINS = ("resume", "extra_info", "user_confirmed")
MAX_TEXT = 500

# ---------------------------------------------------------------- the question catalogue (8 step 7)
# kind drives parsing; target is the 12.6 field (dotted for a part of a field); answer_key feeds the
# answer bank (answers.py). Required questions must be confirmed before `profile status` says confirmed.
QUESTIONS = [
    {"id": "Q1", "key": "target_roles", "kind": "role_families", "target": "role_families", "required": True,
     "text": "Which role families and job titles should I look for? One family per line, for example "
             "'Data analytics: Data Analyst, Business Analyst'.",
     "why": "Every job is matched against these families; nothing else is searched or scored."},
    {"id": "Q2", "key": "roles_avoid", "kind": "keywords", "target": "roles_avoid", "required": True,
     "text": "Which roles or title words should I skip (for example intern, director, sales)? Say 'none' if "
             "there are none.",
     "why": "Titles with these words are filtered out before any evaluation."},
    {"id": "Q3", "key": "seniority", "kind": "seniority", "target": "seniority", "required": True,
     "text": "Which seniority band fits you? Pick a lowest and highest level from: " + ", ".join(SENIORITY) + ".",
     "why": "Keeps the search away from roles that are far too junior or too senior."},
    {"id": "Q4", "key": "experience_years", "kind": "number", "target": "experience_years", "required": True,
     "text": "How many years of relevant work experience do you have in total?",
     "why": "Used for the years-of-experience filter and in forms that ask for it."},
    {"id": "Q5", "key": "salary_currency", "kind": "currency_period", "target": "salary.currency_period",
     "required": True, "text": "Which currency and pay period should salaries use (for example 'INR per year')?",
     "why": "Salary numbers are compared only in this currency and period."},
    {"id": "Q6", "key": "salary_floor", "kind": "money", "target": "salary.floor", "required": True,
     "text": "What is the lowest total pay you would accept, in that currency and period?",
     "why": "Jobs that disclose a maximum below this floor are skipped."},
    {"id": "Q7", "key": "salary_target", "kind": "money", "target": "salary.target", "required": True,
     "text": "What pay are you aiming for?",
     "why": "Used when a form asks for expected pay (only if you allow it in the next question)."},
    {"id": "Q8", "key": "salary_in_forms", "kind": "bool", "target": "salary.may_state_expected_in_forms",
     "required": True, "text": "May application forms state your target pay as your expected pay? (yes or no)",
     "why": "If no, every form that asks for expected pay comes to you instead."},
    {"id": "Q9", "key": "current_pay", "kind": "money", "target": "current_pay", "required": False,
     "sensitive": True, "text": "What is your current total pay? (optional; say 'skip' to leave it out)",
     "why": "Only used when a form asks for current pay; never in messages."},
    {"id": "Q10", "key": "notice_period_days", "kind": "days", "target": "notice_period_days", "required": True,
     "text": "What is your notice period (for example 'immediate', '30 days', '2 months')?",
     "why": "Forms ask for it; it is never guessed."},
    {"id": "Q11", "key": "cities", "kind": "cities", "target": "locations.cities", "required": True,
     "text": "Which cities can you work in? Add 'remote' if remote roles are fine.",
     "why": "Jobs outside these places are skipped."},
    {"id": "Q12", "key": "work_modes", "kind": "work_modes", "target": "locations.work_modes", "required": True,
     "text": "Which work modes suit you: remote, hybrid, onsite?",
     "why": "Jobs with other modes are skipped."},
    {"id": "Q13", "key": "relocate", "kind": "bool", "target": "locations.relocate", "required": False,
     "text": "Would you relocate for the right role? (yes or no)",
     "why": "Forms ask about relocation; the default is no."},
    {"id": "Q14", "key": "work_authorization", "kind": "work_auth", "target": "work_authorization",
     "required": True,
     "text": "Where are you allowed to work? Give country codes with a status, for example 'IN: citizen; other: "
             "needs_sponsorship'. Statuses: " + ", ".join(AUTH_STATUSES) + ".",
     "why": "Jobs that need an authorization you do not have are skipped."},
    {"id": "Q15", "key": "company_exclusions_note", "kind": "text", "target": "company_exclusions_note",
     "required": False,
     "text": "Any companies to exclude (for example your current employer)? Add them to private/exclusions.csv; "
             "note here what you added.",
     "why": "Excluded companies are never contacted and never applied to."},
    {"id": "Q16", "key": "industries_avoid", "kind": "keywords", "target": "industries_avoid", "required": False,
     "text": "Any industries to avoid? (optional)", "why": "Used by the evaluator when scoring companies."},
    {"id": "Q17", "key": "company_preferences", "kind": "text", "target": "company_preferences", "required": False,
     "text": "Any preference on company size or stage? (optional)", "why": "Used by the evaluator."},
    {"id": "Q18", "key": "sites_enabled", "kind": "keywords", "target": "sites_enabled", "required": False,
     "text": "Which job sites may the agent use besides company career pages (ats_forms)? (optional)",
     "why": "Each site has its own risk level; only listed sites are used."},
    {"id": "Q19", "key": "email_outreach", "kind": "bool", "target": "email_outreach", "required": False,
     "text": "Should the agent send outreach emails (every one passes QC and your approval mode)? (yes or no)",
     "why": "Email is the main outreach channel."},
    {"id": "Q20", "key": "linkedin", "kind": "bool", "target": "linkedin.enabled", "required": False,
     "text": "Do you want LinkedIn actions at all? (yes or no; turning them on still needs ./jobhunter linkedin "
             "enable)",
     "why": "LinkedIn automation breaks the LinkedIn User Agreement; it ships off."},
    {"id": "Q21", "key": "eeo", "kind": "eeo", "target": "eeo", "required": False, "sensitive": True,
     "text": "Equal-opportunity questions (gender, ethnicity, veteran, disability): say 'ask' to be asked every "
             "time, 'decline' to always decline, or give answers as 'gender: ...; veteran: ...'.",
     "why": "These answers are sensitive; by default every form asks you."},
    {"id": "Q22", "key": "contact_email", "kind": "email", "target": "contact.email", "required": False,
     "text": "Which email address should forms use?", "why": "Forms ask for it; the default comes from your resume."},
    {"id": "Q23", "key": "contact_phone", "kind": "phone", "target": "contact.phone", "required": False,
     "text": "Which phone number should forms use?", "why": "Forms ask for it; the default comes from your resume."},
    {"id": "Q24", "key": "full_name", "kind": "name", "target": "contact.full_name", "required": False,
     "text": "What is your full name as it should appear on forms?", "why": "Forms ask for it."},
    {"id": "Q25", "key": "current_city", "kind": "text", "target": "contact.current_city", "required": False,
     "text": "Which city do you live in now (for forms that ask)?", "why": "Forms ask for it."},
    {"id": "Q26", "key": "languages", "kind": "keywords", "target": "languages", "required": False,
     "text": "Which languages can you work in?", "why": "Some jobs require a language."},
]
_Q_BY_ID = {q["id"]: q for q in QUESTIONS}
_Q_BY_KEY = {q["key"]: q for q in QUESTIONS}
REQUIRED_IDS = tuple(q["id"] for q in QUESTIONS if q["required"])


# ---------------------------------------------------------------- files
def profile_path() -> str:
    return os.path.join(paths.private_dir(), "profile.json")


def inference_path() -> str:
    return os.path.join(paths.private_dir(), "profile_inference.json")


def salary_path() -> str:
    return os.path.join(paths.private_dir(), "salary_research.json")


def extra_info_path() -> str:
    return os.path.join(paths.private_dir(), "extra_info.md")


def resume_text_path() -> str:
    return os.path.join(paths.private_dir(), "resume", "resume.txt")


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise Denied("E_CONFIG_INVALID", "%s is unreadable: %s" % (os.path.basename(path), exc), data={"path": path})


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True, ensure_ascii=True)
        fh.write("\n")
    os.replace(tmp, path)


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def extra_info_text() -> str | None:
    return _read_text(extra_info_path())


def load_raw() -> dict:
    data = _read_json(profile_path(), None)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", PROFILE_VERSION)
    data.setdefault("profile_version", "")
    data.setdefault("fields", {})
    data.setdefault("facts", {})
    data.setdefault("answers", {})
    data.setdefault("inferred", {})
    data.setdefault("open_questions", [])
    return data


# ---------------------------------------------------------------- facts
def _fact_origin(source: str) -> str | None:
    s = (source or "").strip().lower()
    if s.startswith("resume"):
        return "resume"
    if s.startswith("extra_info") or s.startswith("extra info"):
        return "extra_info"
    if s.startswith("user_confirmed"):
        return "user_confirmed"
    return None


def facts() -> dict:
    """{"P1": text} for every usable fact (from the resume, the extra information file, or confirmed by
    the person)."""
    raw = load_raw()
    out = {}
    for fid, f in (raw.get("facts") or {}).items():
        if isinstance(f, dict) and FACT_ID_RE.match(fid) and isinstance(f.get("text"), str) and \
                _fact_origin(f.get("source", "")) in FACT_ORIGINS:
            out[fid] = f["text"]
    return out


# ---------------------------------------------------------------- parsing of answers
_MONEY_RE = re.compile(r"^\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k|m|mn|l|lakh|lakhs|lpa|cr|crore|crores|million)?\s*"
                       r"([a-z]{3})?\s*$", re.I)
_MULT = {"k": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "l": 1e5, "lakh": 1e5, "lakhs": 1e5, "lpa": 1e5,
         "cr": 1e7, "crore": 1e7, "crores": 1e7}
_YES = ("yes", "y", "true", "1", "on", "ok", "sure")
_NO = ("no", "n", "false", "0", "off", "none")


def _split_list(value: str) -> list[str]:
    parts = re.split(r"[,;\n]+", value)
    return [p.strip() for p in parts if p.strip()]


def _is_none(value: str) -> bool:
    return value.strip().lower() in ("none", "nothing", "no", "n/a", "na", "-", "")


def _bad(qid: str, msg: str) -> Denied:
    return Denied("E_VALIDATION", "%s: %s" % (qid, msg), data={"field": qid})


def _json_or_none(value: str):
    v = value.strip()
    if v[:1] in ("{", "["):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return None


def _clean_text(value, qid: str, max_len: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise _bad(qid, "expected text")
    v = " ".join(value.split())
    if len(v) > max_len:
        raise _bad(qid, "longer than %d characters" % max_len)
    if re.search("[\u2010-\u2015\u2212]", v):
        raise _bad(qid, "use plain words instead of dash characters")
    return v


def _include_from_titles(titles: list[str]) -> list[str]:
    out = set()
    for t in titles:
        low = " ".join(t.lower().split())
        if low:
            out.add(low)
            head = low.split()[-1]
            if len(head) > 2:
                out.add(head)
    return sorted(out)


def _families(value, qid: str) -> list[dict]:
    data = value if isinstance(value, list) else _json_or_none(value) if isinstance(value, str) else None
    fams: list[dict] = []
    if isinstance(data, list):
        for f in data:
            if not isinstance(f, dict) or not isinstance(f.get("name"), str):
                raise _bad(qid, "each family needs a name")
            titles = [_clean_text(t, qid, 80) for t in f.get("titles") or [] if isinstance(t, str) and t.strip()]
            inc = [_clean_text(t, qid, 80).lower() for t in f.get("include") or [] if isinstance(t, str) and t.strip()]
            exc = [_clean_text(t, qid, 80).lower() for t in f.get("exclude") or [] if isinstance(t, str) and t.strip()]
            fams.append({"name": _clean_text(f["name"], qid, 80), "titles": titles,
                         "include": sorted(set(inc)) or _include_from_titles(titles), "exclude": sorted(set(exc))})
    elif isinstance(value, str):
        for line in re.split(r"[\n;]+", value):
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                name, rest = line.split(":", 1)
                titles = _split_list(rest)
            else:
                name, titles = line, [line]
            titles = [_clean_text(t, qid, 80) for t in titles]
            fams.append({"name": _clean_text(name, qid, 80), "titles": titles,
                         "include": _include_from_titles(titles), "exclude": []})
    else:
        raise _bad(qid, "expected role families")
    fams = [f for f in fams if f["titles"] or f["include"]]
    if not fams:
        raise _bad(qid, "name at least one role family with titles")
    if len(fams) > 8:
        raise _bad(qid, "at most 8 role families")
    return fams


def _money(value, qid: str):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        amount = float(value)
    else:
        v = str(value).strip().lower().replace("per annum", "").replace("p.a.", "").strip()
        m = _MONEY_RE.match(v)
        if not m:
            raise _bad(qid, "expected an amount such as 1500000, 15 lakh or 120k")
        amount = float(m.group(1).replace(",", "")) * _MULT.get((m.group(2) or "").lower(), 1)
    if amount <= 0 or amount > 1e12:
        raise _bad(qid, "amount out of range")
    return int(round(amount)) if amount >= 1 else amount


def _currency_period(value, qid: str) -> dict:
    data = value if isinstance(value, dict) else _json_or_none(value) if isinstance(value, str) else None
    if isinstance(data, dict):
        cur, per = str(data.get("currency", "")).upper(), str(data.get("period", "year")).lower()
    else:
        words = re.findall(r"[A-Za-z]+", str(value))
        cur = next((w.upper() for w in words if len(w) == 3 and w.lower() not in ("per", "the", "and")), "")
        per = "year"
        for w in words:
            lw = w.lower().rstrip("ly")
            if lw in ("year", "annual", "annua", "yearl"):
                per = "year"
            elif lw in ("month", "monthl"):
                per = "month"
            elif lw in ("hour", "hourl"):
                per = "hour"
    if not re.match(r"^[A-Z]{3}$", cur):
        raise _bad(qid, "give a three-letter currency code such as INR, USD or EUR")
    if per not in PERIODS:
        raise _bad(qid, "period must be year, month or hour")
    return {"currency": cur, "period": per}


def _bool(value, qid: str) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in _YES:
        return True
    if v in _NO:
        return False
    raise _bad(qid, "answer yes or no")


def _days(value, qid: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        d = value
    else:
        v = str(value).strip().lower()
        if v in ("immediate", "immediately", "now", "0", "none", "serving notice"):
            return 0
        m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(d|day|days|w|week|weeks|m|month|months)?$", v)
        if not m:
            raise _bad(qid, "expected days, weeks or months, for example '30 days'")
        n = float(m.group(1))
        unit = (m.group(2) or "days")[0]
        d = int(round(n * {"d": 1, "w": 7, "m": 30}[unit]))
    if d < 0 or d > 365:
        raise _bad(qid, "notice period out of range")
    return d


def _seniority(value, qid: str) -> dict:
    data = value if isinstance(value, dict) else _json_or_none(value) if isinstance(value, str) else None
    if isinstance(data, dict):
        levels = [str(data.get("min", "")).lower(), str(data.get("max", "")).lower()]
    else:
        levels = [w for w in re.findall(r"[a-z]+", str(value).lower()) if w in SENIORITY]
    levels = [lv for lv in levels if lv in SENIORITY]
    if not levels:
        raise _bad(qid, "use levels from: " + ", ".join(SENIORITY))
    ranks = sorted(SENIORITY.index(lv) for lv in levels)
    return {"min": SENIORITY[ranks[0]], "max": SENIORITY[ranks[-1]]}


def _work_modes(value, qid: str) -> list[str]:
    items = value if isinstance(value, list) else _split_list(str(value).replace(" and ", ","))
    out = []
    for it in items:
        w = str(it).strip().lower().replace("-", "").replace(" ", "")
        w = {"office": "onsite", "inoffice": "onsite", "wfh": "remote", "workfromhome": "remote"}.get(w, w)
        if w not in WORK_MODES:
            raise _bad(qid, "work modes are remote, hybrid, onsite")
        if w not in out:
            out.append(w)
    if not out:
        raise _bad(qid, "name at least one work mode")
    return out


def _cities(value, qid: str) -> list[str]:
    items = value if isinstance(value, list) else _split_list(str(value))
    out = []
    for it in items:
        c = _clean_text(str(it), qid, 60).lower()
        if c and c not in out:
            out.append(c)
    if not out:
        raise _bad(qid, "name at least one city or 'remote'")
    return out


def _keywords(value, qid: str) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        if _is_none(str(value)):
            return []
        items = _split_list(str(value))
    return sorted({_clean_text(str(i), qid, 60).lower() for i in items if str(i).strip()})


def _work_auth(value, qid: str) -> dict:
    data = value if isinstance(value, dict) else _json_or_none(value) if isinstance(value, str) else None
    pairs = []
    if isinstance(data, dict):
        pairs = list(data.items())
    else:
        for part in re.split(r"[;,\n]+", str(value)):
            if ":" in part:
                k, v = part.split(":", 1)
                pairs.append((k, v))
            elif part.strip():
                raise _bad(qid, "write 'COUNTRY: status' pairs, for example 'IN: citizen'")
    out = {}
    for k, v in pairs:
        key = str(k).strip()
        key = "other" if key.lower() in ("other", "others", "elsewhere", "rest") else key.upper()
        if key != "other" and not re.match(r"^[A-Z]{2}$", key):
            raise _bad(qid, "country codes are two letters (IN, US, GB) or 'other'")
        st = str(v).strip().lower().replace(" ", "_").replace("-", "_")
        st = {"sponsorship": "needs_sponsorship", "need_sponsorship": "needs_sponsorship", "visa": "work_visa",
              "pr": "permanent_resident", "green_card": "permanent_resident", "no": "not_authorized"}.get(st, st)
        if st not in AUTH_STATUSES:
            raise _bad(qid, "status must be one of " + ", ".join(AUTH_STATUSES))
        out[key] = st
    if not out:
        raise _bad(qid, "give at least one country")
    return out


def _eeo(value, qid: str):
    if isinstance(value, dict):
        return {str(k).lower(): _clean_text(str(v), qid, 80) for k, v in value.items()}
    v = str(value).strip()
    if v.lower() in ("ask", "decline"):
        return v.lower()
    data = _json_or_none(v)
    if isinstance(data, dict):
        return _eeo(data, qid)
    out = {}
    for part in re.split(r"[;\n]+", v):
        if ":" not in part:
            raise _bad(qid, "say 'ask', 'decline' or give 'topic: answer' pairs")
        k, val = part.split(":", 1)
        out[k.strip().lower()] = _clean_text(val, qid, 80)
    return out


def parse_value(q: dict, value):
    """Parse one answer (typed text, chat text or JSON) for question q. Denied(E_VALIDATION) when unusable."""
    kind, qid = q["kind"], q["id"]
    if isinstance(value, str) and value.strip().lower() == "skip" and not q["required"]:
        return None
    if kind == "role_families":
        return _families(value, qid)
    if kind == "keywords":
        return _keywords(value, qid)
    if kind == "seniority":
        return _seniority(value, qid)
    if kind == "number":
        try:
            v = float(re.sub(r"[^0-9.]", "", str(value)) or "x")
        except ValueError:
            raise _bad(qid, "expected a number")
        if v < 0 or v > 60:
            raise _bad(qid, "out of range")
        return v
    if kind == "currency_period":
        return _currency_period(value, qid)
    if kind == "money":
        return _money(value, qid)
    if kind == "bool":
        return _bool(value, qid)
    if kind == "days":
        return _days(value, qid)
    if kind == "cities":
        return _cities(value, qid)
    if kind == "work_modes":
        return _work_modes(value, qid)
    if kind == "work_auth":
        return _work_auth(value, qid)
    if kind == "eeo":
        return _eeo(value, qid)
    if kind == "email":
        v = str(value).strip()
        if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", v):
            raise _bad(qid, "not an email address")
        return v
    if kind == "phone":
        v = " ".join(str(value).split())
        if not re.match(r"^\+?[0-9 ().]{6,24}$", v) or len(re.sub(r"[^0-9]", "", v)) < 6:
            raise _bad(qid, "not a phone number")
        return v
    if kind == "name":
        return _clean_text(value, qid, 80)
    return _clean_text(value, qid)


def render_value(q: dict, value) -> str:
    """Human text for a stored or default value (shown in questions and the interview)."""
    if value is None:
        return ""
    kind = q["kind"]
    if kind == "role_families":
        return "\n".join("%s: %s" % (f.get("name"), ", ".join(f.get("titles") or [])) for f in value)
    if kind in ("keywords", "cities", "work_modes"):
        return ", ".join(value) if value else "none"
    if kind == "seniority":
        return "%s to %s" % (value.get("min"), value.get("max"))
    if kind == "currency_period":
        return "%s per %s" % (value.get("currency"), value.get("period"))
    if kind == "bool":
        return "yes" if value else "no"
    if kind == "days":
        return "immediate" if value == 0 else "%d days" % value
    if kind == "work_auth":
        return "; ".join("%s: %s" % (k, v) for k, v in value.items())
    if kind == "eeo":
        return value if isinstance(value, str) else "; ".join("%s: %s" % kv for kv in value.items())
    if kind == "number":
        return ("%g" % value) if isinstance(value, float) else str(value)
    return str(value)


# ---------------------------------------------------------------- derived fields
def _confirmed(raw: dict, qid: str):
    a = (raw.get("answers") or {}).get(qid)
    if isinstance(a, dict) and a.get("source") == "user_confirmed":
        return True, a.get("value")
    return False, None


def derive_fields(raw: dict) -> dict:
    """12.6 fields from the confirmed answers (source user_confirmed) plus inferred placeholders for
    fields that are not confirmed yet (source inferred; ignored by every gate)."""
    fields: dict = {}
    stamp_of = {qid: (a or {}).get("confirmed_at") for qid, a in (raw.get("answers") or {}).items()}

    def put(name, value, qids):
        at = max((stamp_of.get(q) or "" for q in qids), default="") or None
        fields[name] = {"value": value, "source": "user_confirmed", "confirmed_at": at}

    ok1, fams = _confirmed(raw, "Q1")
    ok2, avoid = _confirmed(raw, "Q2")
    if ok1:
        fams = json.loads(json.dumps(fams))
        if ok2 and avoid:
            for f in fams:
                f["exclude"] = sorted(set(f.get("exclude") or []) | set(avoid))
        put("role_families", fams, ["Q1", "Q2"] if ok2 else ["Q1"])
    if ok2:
        put("roles_avoid", avoid, ["Q2"])
    for qid, name in (("Q3", "seniority"), ("Q4", "experience_years"), ("Q10", "notice_period_days"),
                      ("Q14", "work_authorization"), ("Q15", "company_exclusions_note"), ("Q16", "industries_avoid"),
                      ("Q17", "company_preferences"), ("Q18", "sites_enabled"), ("Q19", "email_outreach"),
                      ("Q26", "languages"), ("Q21", "eeo")):
        ok, v = _confirmed(raw, qid)
        if ok and v is not None:
            put(name, v, [qid])
    ok9, pay = _confirmed(raw, "Q9")
    ok5, cp = _confirmed(raw, "Q5")
    if ok9 and pay is not None:
        put("current_pay", {"amount": pay, "currency": (cp or {}).get("currency"), "period": (cp or {}).get("period")},
            ["Q9"])
    ok6, floor = _confirmed(raw, "Q6")
    ok7, target = _confirmed(raw, "Q7")
    ok8, forms = _confirmed(raw, "Q8")
    if ok5 and ok6 and ok7 and ok8:
        put("salary", {"currency": cp["currency"], "period": cp["period"], "floor": floor, "target": target,
                       "may_state_expected_in_forms": bool(forms)}, ["Q5", "Q6", "Q7", "Q8"])
    ok11, cities = _confirmed(raw, "Q11")
    ok12, modes = _confirmed(raw, "Q12")
    ok13, reloc = _confirmed(raw, "Q13")
    if ok11 and ok12:
        put("locations", {"cities": cities, "work_modes": modes, "relocate": bool(reloc) if ok13 else False},
            ["Q11", "Q12"] + (["Q13"] if ok13 else []))
    ok20, li = _confirmed(raw, "Q20")
    if ok20 and li is not None:
        put("linkedin", {"enabled": bool(li)}, ["Q20"])
    contact = {}
    for qid, key in (("Q22", "email"), ("Q23", "phone"), ("Q24", "full_name"), ("Q25", "current_city")):
        ok, v = _confirmed(raw, qid)
        if ok and v:
            contact[key] = v
    if contact:
        put("contact", contact, [q for q in ("Q22", "Q23", "Q24", "Q25") if _confirmed(raw, q)[0]])
    for name, value in (raw.get("inferred") or {}).items():
        if name not in fields and value is not None:
            fields[name] = {"value": value, "source": "inferred"}
    return fields


def _version_of(fields: dict) -> str:
    conf = {k: v["value"] for k, v in fields.items() if v.get("source") == "user_confirmed"}
    return hashlib.sha256(json.dumps(conf, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _missing(raw: dict) -> list[str]:
    return [qid for qid in REQUIRED_IDS if not _confirmed(raw, qid)[0]]


# ---------------------------------------------------------------- public reads
def load_confirmed() -> dict:
    """12.6 with confirmed fields only (source user_confirmed) and usable facts. `confirmed` says whether
    every required question is answered."""
    raw = load_raw()
    fields = derive_fields(raw)
    conf = {k: {"value": v["value"], "source": v["source"]} for k, v in fields.items()
            if v.get("source") == "user_confirmed"}
    fx = {fid: {"text": f["text"], "source": f.get("source")} for fid, f in (raw.get("facts") or {}).items()
          if fid in facts()}
    missing = _missing(raw)
    return {"version": PROFILE_VERSION, "profile_version": raw.get("profile_version") if not missing else "",
            "confirmed": not missing, "fields": conf, "facts": fx}


def input_files() -> dict:
    res_dir = os.path.join(paths.private_dir(), "resume")
    original = None
    for ext in ("pdf", "docx", "txt", "md"):
        p = os.path.join(res_dir, "original." + ext)
        if os.path.isfile(p):
            original = p
            break
    return {
        "resume_original": original,
        "resume_text": resume_text_path() if os.path.isfile(resume_text_path()) else None,
        "extra_info": extra_info_path() if os.path.isfile(extra_info_path()) else None,
        "salary_research": salary_path() if os.path.isfile(salary_path()) else None,
        "inference": inference_path() if os.path.isfile(inference_path()) else None,
        "base_resume": os.path.join(res_dir, "base.json") if os.path.isfile(os.path.join(res_dir, "base.json"))
        else None,
        "answers": os.path.join(paths.private_dir(), "answers.json")
        if os.path.isfile(os.path.join(paths.private_dir(), "answers.json")) else None,
    }


def status() -> dict:
    raw = load_raw()
    missing = _missing(raw)
    from . import resume as R
    base = None
    try:
        base = R.load_base(required=False)
    except Denied:
        base = None
    rv = R.review_state(base) if base else {"reviewed": False}
    return {"confirmed": not missing, "missing": missing,
            "missing_text": [_Q_BY_ID[q]["text"] for q in missing],
            "profile_version": raw.get("profile_version") if not missing else "",
            "input_files": input_files(), "facts": len(facts()), "base_resume_reviewed": bool(rv.get("reviewed")),
            "open_questions": len([q for q in raw.get("open_questions") or [] if not q.get("answered")])}


def questions(only_open: bool = False) -> list[dict]:
    raw = load_raw()
    out = []
    for q in QUESTIONS:
        ok, val = _confirmed(raw, q["id"])
        if only_open and ok:
            continue
        default = val if ok else _default_for(raw, q)
        out.append({"id": q["id"], "key": q["key"], "text": q["text"], "why": q["why"], "required": q["required"],
                    "default": render_value(q, default) if default is not None else None, "confirmed": ok,
                    "sensitive": bool(q.get("sensitive"))})
    for oq in raw.get("open_questions") or []:
        if only_open and oq.get("answered"):
            continue
        out.append({"id": oq["id"], "key": "open:" + oq.get("ref", oq["id"]), "text": oq["text"],
                    "why": "The evaluator could not tell this from your resume.", "required": False,
                    "default": None, "confirmed": bool(oq.get("answered")), "sensitive": False})
    return out


def questions_text(qs: list[dict]) -> str:
    lines = []
    for q in qs:
        head = "%s%s %s" % (q["id"], " (required)" if q["required"] else "", q["text"])
        lines.append(head)
        if q.get("default"):
            lines.append("   default: %s" % q["default"].replace("\n", " | "))
    return "\n".join(lines)


def _default_for(raw: dict, q: dict):
    inf = raw.get("inferred") or {}
    k = q["key"]
    if k == "target_roles":
        return inf.get("role_families")
    if k == "experience_years":
        return inf.get("experience_years")
    if k == "salary_currency":
        band = inf.get("salary_band") or {}
        return {"currency": band["currency"], "period": band.get("period", "year")} if band.get("currency") else None
    if k == "salary_floor":
        return (inf.get("salary_band") or {}).get("floor")
    if k == "salary_target":
        return (inf.get("salary_band") or {}).get("target")
    if k == "salary_in_forms":
        return False
    if k == "notice_period_days":
        return inf.get("notice_period_days")
    if k == "cities":
        return inf.get("locations")
    if k == "work_modes":
        return inf.get("work_modes")
    if k == "relocate":
        return False
    if k == "roles_avoid":
        return inf.get("roles_avoid")
    if k == "seniority":
        years = inf.get("experience_years")
        if isinstance(years, (int, float)):
            if years < 1:
                return {"min": "entry", "max": "entry"}
            if years < 3:
                return {"min": "entry", "max": "associate"}
            if years < 6:
                return {"min": "associate", "max": "senior"}
            if years < 10:
                return {"min": "senior", "max": "lead"}
            return {"min": "lead", "max": "director"}
        return None
    if k == "languages":
        return inf.get("languages")
    if k == "email_outreach":
        return True
    if k == "linkedin":
        return False
    if k == "eeo":
        return "ask"
    if k == "sites_enabled":
        return ["ats_forms"]
    contact = inf.get("contact") or {}
    if k == "contact_email":
        return contact.get("email")
    if k == "contact_phone":
        return contact.get("phone")
    if k == "full_name":
        return contact.get("full_name")
    if k == "current_city":
        return contact.get("location")
    return None


# ---------------------------------------------------------------- writes
def _save_and_sync(conn, raw: dict, by: str) -> dict:
    """Recompute fields and profile_version, write profile.json, update meta and the answer bank."""
    from . import db
    fields = derive_fields(raw)
    missing = _missing(raw)
    old_version = raw.get("profile_version") or ""
    new_version = _version_of(fields) if not missing else ""
    raw["fields"] = fields
    raw["profile_version"] = new_version
    raw["version"] = PROFILE_VERSION
    changed = new_version != old_version
    if conn is not None:
        if (db.meta_get(conn, "profile_version") or "") != new_version:
            db.meta_set(conn, "profile_version", new_version, "human" if by != "system" else "system")
        if new_version and changed:
            conn.execute("UPDATE human_tasks SET done_at = ?, resolution = 'confirmed' WHERE kind = 'confirm_profile' "
                         "AND done_at IS NULL", (now(),))
            log_event(conn, "profile_version", profile_version=new_version, previous=old_version or None, by=by)
            if old_version:
                enqueue_notification(conn, "profile:changed:%s" % new_version[:12], "normal", "info",
                                     "Your profile changed. Jobs can be re-evaluated with "
                                     "./jobhunter eval requeue --since-profile-change.")
    _write_json(profile_path(), raw)
    from . import answers as A
    A.sync_from_profile(raw, fields)
    return {"profile_version": new_version, "changed": changed, "missing": missing}


def answer(conn, field: str, value, source: str = "user_confirmed", *, sensitive_ok: bool = True,
           by: str = "human") -> dict:
    """Store one answer the person gave (terminal, chat or the interview). `field` is a question id (Q3),
    a question key (notice_period_days), an evaluator open question id, or a human task uid (H...) of an
    answer_question task from a form (answers.answer_task, which also hands a job parked for that question
    back to the apply lane through U6 applyq.return_from_human). Runs inside the caller's transaction."""
    if source != "user_confirmed":
        raise Denied("E_VALIDATION", "profile answers are always user_confirmed")
    if not isinstance(field, str) or not field.strip():
        raise Denied("E_USAGE", "--field is required")
    field = field.strip()
    if TASK_UID_RE.match(field):
        from . import answers as A
        res = A.answer_task(conn, field, value, sensitive_ok=sensitive_ok, by=by)
        raw = load_raw()
        res["remaining_required"] = _missing(raw)
        return res
    raw = load_raw()
    q = _Q_BY_ID.get(field) or _Q_BY_KEY.get(field)
    if q is None:
        for oq in raw.get("open_questions") or []:
            if field in (oq.get("id"), oq.get("ref")):
                text = _clean_text(str(value), oq["id"], 1000)
                oq["answered"] = True
                oq["answer"] = text
                oq["answered_at"] = now()
                if not text.lower().startswith(("skip", "none", "n/a")):
                    n = max([int(k[1:]) for k in raw["facts"] if FACT_ID_RE.match(k)] or [0]) + 1
                    raw["facts"]["P%d" % n] = {"text": "%s %s" % (oq["text"], text),
                                               "source": "user_confirmed, %s" % oq["id"]}
                out = _save_and_sync(conn, raw, by)
                if conn is not None:
                    log_event(conn, "profile_answer", field=oq["id"], by=by)
                return {"field": oq["id"], "stored_value": text, "remaining_required": out["missing"],
                        "profile_version": out["profile_version"]}
        raise Denied("E_NOT_FOUND", "unknown profile field %r" % field,
                     data={"fields": [x["id"] for x in QUESTIONS]})
    if q.get("sensitive") and not sensitive_ok:
        raise Denied("E_HUMAN_ONLY", "%s is sensitive; answer it at the terminal with ./jobhunter profile" % q["id"])
    parsed = parse_value(q, value)
    if parsed is None:
        raw["answers"].pop(q["id"], None)
    else:
        raw["answers"][q["id"]] = {"value": parsed, "source": "user_confirmed", "confirmed_at": now()}
    out = _save_and_sync(conn, raw, by)
    if conn is not None:
        log_event(conn, "profile_answer", field=q["id"], by=by)
    return {"field": q["id"], "key": q["key"], "stored_value": render_value(q, parsed) if parsed is not None else None,
            "remaining_required": out["missing"], "profile_version": out["profile_version"],
            "requeue_suggested": bool(out["changed"] and out["profile_version"])}


# ---------------------------------------------------------------- salary research (12.16)
def _date_ok(s) -> bool:
    return isinstance(s, str) and bool(re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9:]{8}Z)?$", s))


def validate_salary(data) -> dict:
    errs = []
    if not isinstance(data, dict):
        raise Denied("E_SCHEMA", "the salary file must be a JSON object")
    extra = set(data) - {"role_titles", "city", "retrieved_at", "sources"}
    if extra:
        errs.append("unknown keys %s" % sorted(extra))
    titles = data.get("role_titles")
    if not isinstance(titles, list) or not titles or not all(isinstance(t, str) and 0 < len(t) <= 80 for t in titles):
        errs.append("role_titles must be a non-empty list of titles")
    if not isinstance(data.get("city"), str) or not data["city"].strip() or len(data["city"]) > 60:
        errs.append("city is required")
    if not _date_ok(data.get("retrieved_at")):
        errs.append("retrieved_at must be YYYY-MM-DD")
    sources = data.get("sources")
    if not isinstance(sources, list) or len(sources) > 6:
        errs.append("sources must be a list of at most 6 pages")
        sources = []
    allowed = {"url", "title", "currency", "period", "low", "high", "experience_band", "note"}
    for i, s in enumerate(sources):
        p = "sources[%d]" % i
        if not isinstance(s, dict):
            errs.append(p + " must be an object")
            continue
        if set(s) - allowed:
            errs.append("%s unknown keys %s" % (p, sorted(set(s) - allowed)))
        if not isinstance(s.get("url"), str) or not re.match(r"^https://[^\s]+$", s["url"]) or len(s["url"]) > 500:
            errs.append(p + ".url must be an https URL")
        if not isinstance(s.get("currency"), str) or not re.match(r"^[A-Z]{3}$", s["currency"]):
            errs.append(p + ".currency must be a three-letter code")
        if s.get("period") not in PERIODS:
            errs.append(p + ".period must be year, month or hour")
        lo, hi = s.get("low"), s.get("high")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for x in (lo, hi)) or lo > hi:
            errs.append(p + ".low and .high must be positive numbers with low <= high")
        for k in ("title", "experience_band", "note"):
            if s.get(k) is not None and (not isinstance(s[k], str) or len(s[k]) > 300):
                errs.append("%s.%s must be text up to 300 characters" % (p, k))
    if errs:
        raise Denied("E_SCHEMA", "the salary research file does not match the contract (12.16)", data={"errors": errs})
    return data


def record_salary(conn, data: dict) -> dict:
    d = validate_salary(data)
    rec = dict(d)
    rec["recorded_at"] = now()
    _write_json(salary_path(), rec)
    _copy_to_evaluator_onboarding("salary.json", json.dumps(rec, indent=1, sort_keys=True))
    if conn is not None:
        log_event(conn, "salary_recorded", sources=len(d["sources"]))
    return {"sources_recorded": len(d["sources"]), "path": salary_path()}


def salary_urls() -> set:
    rec = _read_json(salary_path(), {}) or {}
    return {s.get("url") for s in rec.get("sources") or [] if isinstance(s, dict)}


# ---------------------------------------------------------------- inference (12.9)
_INF_KEYS = {"facts", "base_resume", "experience_years", "role_families", "salary_band", "locations_guess",
             "work_mode_guess", "notice_period_guess", "languages", "open_questions"}


def _numbers_in(text: str) -> set:
    from .resume import model as M
    return {n for n, _u in M.numbers(text)}


def validate_inference(data) -> dict:
    """Structural checks (E_SCHEMA), fact and evidence checks (E_VALIDATION, E_EVIDENCE_MISSING) and the
    never-invent number check against the person's own texts when they are present."""
    if not isinstance(data, dict):
        raise Denied("E_SCHEMA", "the inference file must be a JSON object")
    errs = []
    extra = set(data) - _INF_KEYS
    if extra:
        errs.append("unknown keys %s" % sorted(extra))
    fx = data.get("facts")
    if not isinstance(fx, dict) or not fx:
        errs.append("facts must be an object of P<n> to {text, source}")
        fx = {}
    for fid, f in fx.items():
        if not FACT_ID_RE.match(str(fid)):
            errs.append("fact id %r must look like P1" % fid)
        if not isinstance(f, dict) or set(f) - {"text", "source"} or not isinstance(f.get("text"), str) or \
                not isinstance(f.get("source"), str):
            errs.append("facts.%s must be {text, source}" % fid)
            continue
        if not f["text"].strip() or len(f["text"]) > MAX_TEXT:
            errs.append("facts.%s.text must be 1 to %d characters" % (fid, MAX_TEXT))
        if _fact_origin(f["source"]) not in ("resume", "extra_info"):
            errs.append("facts.%s.source must start with 'resume' or 'extra_info'" % fid)
    ey = data.get("experience_years")
    if not isinstance(ey, dict) or set(ey) - {"value", "arithmetic"} or not isinstance(ey.get("value"), (int, float)) \
            or isinstance(ey.get("value"), bool) or not isinstance(ey.get("arithmetic"), str):
        errs.append("experience_years must be {value, arithmetic}")
    elif not 0 <= ey["value"] <= 60:
        errs.append("experience_years.value out of range")
    fams = data.get("role_families")
    if not isinstance(fams, list) or not 1 <= len(fams) <= 6:
        errs.append("role_families must be a list of 1 to 6 families")
        fams = []
    for i, f in enumerate(fams):
        p = "role_families[%d]" % i
        allowed = {"name", "titles", "include", "exclude", "evidence_fact_ids", "rationale"}
        if not isinstance(f, dict) or set(f) - allowed:
            errs.append(p + " has unknown keys or is not an object")
            continue
        if not isinstance(f.get("name"), str) or not isinstance(f.get("titles"), list) or not f["titles"]:
            errs.append(p + " needs a name and titles")
        for k in ("include", "exclude", "titles", "evidence_fact_ids"):
            if f.get(k) is not None and (not isinstance(f[k], list) or not all(isinstance(x, str) for x in f[k])):
                errs.append("%s.%s must be a list of strings" % (p, k))
        ev = f.get("evidence_fact_ids") or []
        if not ev:
            errs.append(p + ".evidence_fact_ids must cite at least one fact")
        for e in ev if isinstance(ev, list) else []:
            if e not in fx:
                errs.append("%s cites unknown fact %s" % (p, e))
    band = data.get("salary_band")
    if band is not None:
        allowed = {"currency", "period", "floor", "target", "stretch", "confidence", "basis"}
        if not isinstance(band, dict) or set(band) - allowed:
            errs.append("salary_band has unknown keys or is not an object")
        else:
            if not isinstance(band.get("currency"), str) or not re.match(r"^[A-Z]{3}$", band["currency"]):
                errs.append("salary_band.currency must be a three-letter code")
            if band.get("period") not in PERIODS:
                errs.append("salary_band.period must be year, month or hour")
            nums = [band.get(k) for k in ("floor", "target", "stretch")]
            if not all(isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 for n in nums) or \
                    not nums[0] <= nums[1] <= nums[2]:
                errs.append("salary_band needs floor <= target <= stretch (positive numbers)")
            if band.get("confidence") not in ("low", "medium", "high"):
                errs.append("salary_band.confidence must be low, medium or high")
            basis = band.get("basis")
            if not isinstance(basis, list) or not basis:
                errs.append("salary_band.basis must list where the numbers come from")
                basis = []
            for j, b in enumerate(basis):
                if not isinstance(b, dict) or set(b) - {"kind", "url", "note"} or b.get("kind") not in \
                        SALARY_BASIS_KINDS:
                    errs.append("salary_band.basis[%d] must be {kind, url?, note} with kind in %s"
                                % (j, ", ".join(SALARY_BASIS_KINDS)))
    for k in ("locations_guess", "work_mode_guess", "languages"):
        v = data.get(k)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
            errs.append("%s must be a list of strings" % k)
    npg = data.get("notice_period_guess")
    if npg is not None and (not isinstance(npg, int) or isinstance(npg, bool) or not 0 <= npg <= 365):
        errs.append("notice_period_guess must be days or null")
    oqs = data.get("open_questions") or []
    if not isinstance(oqs, list) or len(oqs) > 12:
        errs.append("open_questions must be a list of at most 12 {id, text}")
        oqs = []
    for i, oq in enumerate(oqs):
        if not isinstance(oq, dict) or set(oq) - {"id", "text"} or not isinstance(oq.get("id"), str) or \
                not isinstance(oq.get("text"), str) or not 0 < len(oq["text"]) <= 300:
            errs.append("open_questions[%d] must be {id, text}" % i)
    if not isinstance(data.get("base_resume"), dict):
        errs.append("base_resume is required")
    if errs:
        raise Denied("E_SCHEMA", "the inference file does not match the contract (12.9)", data={"errors": errs})

    # evidence: salary pages must be the recorded research
    if isinstance(band, dict):
        urls = salary_urls()
        for b in band.get("basis") or []:
            if b.get("kind") == "salary_page" and b.get("url") not in urls:
                raise Denied("E_EVIDENCE_MISSING", "salary_band.basis cites a page that is not in the recorded "
                             "salary research", data={"url": b.get("url")})
            if b.get("kind") == "model_estimate" and not (b.get("note") or "").strip():
                raise Denied("E_VALIDATION", "a model estimate basis needs the note 'model estimate, not verified'")

    # never invent: numbers in facts must appear in the person's own texts
    sources = "\n".join(t for t in (_read_text(resume_text_path()), extra_info_text()) if t)
    if sources.strip():
        have = _numbers_in(sources)
        bad = []
        for fid, f in fx.items():
            missing = sorted(_numbers_in(f["text"]) - have)
            if missing:
                bad.append("%s: %s" % (fid, ", ".join(missing)))
        if bad:
            raise Denied("E_VALIDATION", "facts contain numbers that are not in the resume or the extra information",
                         data={"errors": bad})
    return data


def _base_number_check(base: dict) -> None:
    sources = "\n".join(t for t in (_read_text(resume_text_path()), extra_info_text()) if t)
    if not sources.strip():
        return
    from .resume import model as M
    have = _numbers_in(sources)
    bad = []
    for bid, (_owner, text) in M.bullet_index(base).items():
        missing = sorted(_numbers_in(text) - have)
        if missing:
            bad.append("%s: %s" % (bid, ", ".join(missing)))
    for r in base.get("experience", []):
        for key in ("start", "end"):
            v = (r.get("dates") or {}).get(key)
            if v and v != "present" and v[:4] not in have:
                bad.append("%s dates: year %s" % (r["role_id"], v[:4]))
    if bad:
        raise Denied("E_VALIDATION", "the base resume contains numbers or years that are not in the resume or the "
                     "extra information", data={"errors": bad})


def record_inference(conn, data: dict) -> dict:
    """Store the evaluator's inference: facts, base resume (validated by resume.model), inferred defaults
    for the interview and open questions. Confirmed answers are never overwritten."""
    from . import resume as R
    from .resume import model as M
    d = validate_inference(data)
    base = M.validate(d["base_resume"], what="base_resume")
    _base_number_check(base)
    raw = load_raw()
    # facts from inference replace earlier inferred facts; facts the person confirmed stay
    kept = {k: v for k, v in (raw.get("facts") or {}).items() if _fact_origin(v.get("source", "")) == "user_confirmed"}
    new_facts = {k: {"text": v["text"].strip(), "source": v["source"].strip()} for k, v in d["facts"].items()}
    clash = set(kept) & set(new_facts)
    if clash:
        n = max([int(k[1:]) for k in list(new_facts) + list(kept)] or [0])
        for k in sorted(clash):
            n += 1
            kept["P%d" % n] = kept.pop(k)
    raw["facts"] = dict(new_facts, **kept)
    band = d.get("salary_band")
    fams = []
    for f in d["role_families"]:
        titles = [t for t in f.get("titles") or []]
        fams.append({"name": f["name"], "titles": titles,
                     "include": [x.lower() for x in f.get("include") or []] or _include_from_titles(titles),
                     "exclude": [x.lower() for x in f.get("exclude") or []]})
    modes = [m.lower() for m in d.get("work_mode_guess") or [] if m.lower() in WORK_MODES]
    raw["inferred"] = {
        "role_families": fams,
        "experience_years": float(d["experience_years"]["value"]),
        "experience_arithmetic": d["experience_years"]["arithmetic"],
        "salary_band": band,
        "locations": [x.lower() for x in d.get("locations_guess") or []] or None,
        "work_modes": modes or None,
        "notice_period_days": d.get("notice_period_guess"),
        "languages": d.get("languages") or None,
        "contact": {k: base["contact"].get(k) for k in ("full_name", "email", "phone", "location")},
        "recorded_at": now(),
    }
    old = {q.get("ref"): q for q in raw.get("open_questions") or []}
    oqs = []
    for i, oq in enumerate(d.get("open_questions") or [], 1):
        prev = old.get(oq["id"])
        if prev and prev.get("answered"):
            oqs.append(prev)
            continue
        oqs.append({"id": "X%d" % i, "ref": oq["id"], "text": " ".join(oq["text"].split()), "answered": False})
    raw["open_questions"] = oqs
    _write_json(inference_path(), d)
    R.save_base(base)
    out = _save_and_sync(conn, raw, "system")
    qs = [q for q in questions(only_open=True)]
    if conn is not None:
        if out["missing"]:
            open_human_task_safe(conn, "confirm_profile", "Answer the profile questions: ./jobhunter profile, or "
                                 "/jh answer <id> <text> in chat.")
            enqueue_notification(conn, "profile:questions:%s" % now()[:10], "normal", "question",
                                 "Your profile draft is ready. %d questions are waiting (./jobhunter profile)."
                                 % len(qs))
        log_event(conn, "profile_inference_recorded", facts=len(new_facts), families=len(fams))
    return {"fields_inferred": sorted(k for k, v in raw["inferred"].items() if v is not None and k != "recorded_at"),
            "questions": len(qs), "profile_version": out["profile_version"], "facts": len(raw["facts"]),
            "base_resume": R.base_path(), "dash_conversions": len(base.get("dash_conversions") or [])}


def open_human_task_safe(conn, kind: str, question: str) -> str:
    from .events import open_human_task
    return open_human_task(conn, kind, question)


# ---------------------------------------------------------------- feasibility (8 step 8)
def feasibility() -> dict:
    conf = load_confirmed()["fields"]
    raw = load_raw()
    conflicts = []

    def add(fields, text, suggestion, severity="block"):
        conflicts.append({"fields": fields, "text": text, "suggestion": suggestion, "severity": severity})

    val = {k: v["value"] for k, v in conf.items()}
    sal = val.get("salary")
    if sal:
        if sal["floor"] > sal["target"]:
            add(["salary"], "Your salary floor is above your target.", "Lower the floor or raise the target.")
        band = (raw.get("inferred") or {}).get("salary_band") or {}
        research = _read_json(salary_path(), {}) or {}
        highs = [s["high"] for s in research.get("sources") or []
                 if s.get("currency") == sal["currency"] and s.get("period") == sal["period"]]
        top = None
        if band.get("currency") == sal["currency"] and band.get("period") == sal["period"]:
            top = band.get("stretch")
        if highs:
            top = max([top or 0] + highs)
        if top:
            if sal["floor"] > top:
                add(["salary"], "Your salary floor (%s) is above the highest researched pay (%s) for your roles."
                    % (sal["floor"], top), "Relax the floor to at most %s, or keep it and expect very few matches."
                    % top)
            elif sal["target"] > top * 1.25:
                add(["salary"], "Your target is well above the researched range.",
                    "Consider a target near %s." % top, "warn")
    sen = val.get("seniority")
    years = val.get("experience_years")
    if sen:
        if SENIORITY.index(sen["min"]) > SENIORITY.index(sen["max"]):
            add(["seniority"], "The lowest seniority is above the highest.", "Swap them.")
        if isinstance(years, (int, float)):
            need = SENIORITY_MIN_YEARS[sen["min"]]
            if years + 3 < need:
                add(["seniority", "experience_years"],
                    "%s roles usually ask for about %d years; you have %g." % (sen["min"].capitalize(), need, years),
                    "Lower the lowest seniority, or keep it and expect few matches.")
    loc = val.get("locations")
    if loc:
        if not loc["work_modes"]:
            add(["locations"], "No work mode is allowed.", "Allow at least one of remote, hybrid, onsite.")
        only_remote_city = [c for c in loc["cities"] if c != "remote"] == []
        if only_remote_city and "remote" not in loc["work_modes"]:
            add(["locations"], "You listed no city but do not allow remote work.",
                "Add a city or allow remote work.")
        if "remote" in loc["cities"] and "remote" not in loc["work_modes"]:
            add(["locations"], "'remote' is in your cities but not in your work modes.", "Add remote as a work mode.",
                "warn")
    fams = val.get("role_families")
    avoid = set(val.get("roles_avoid") or [])
    if fams and avoid:
        for f in fams:
            for t in f.get("titles") or []:
                hit = [w for w in avoid if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t.lower())]
                if hit:
                    add(["role_families", "roles_avoid"], "You target '%s' but avoid '%s'." % (t, hit[0]),
                        "Remove one of them.")
    npd = val.get("notice_period_days")
    if isinstance(npd, int) and npd > 90:
        add(["notice_period_days"], "A notice period above 90 days rules out many roles.",
            "Check whether it can be shortened.", "warn")
    return {"ok": not any(c["severity"] == "block" for c in conflicts), "conflicts": conflicts,
            "confirmed": not _missing(raw)}


# ---------------------------------------------------------------- import (8 step 3 and 4)
def _copy_to_evaluator_onboarding(name: str, text: str) -> str | None:
    """Put an onboarding input where the evaluator can read it (WS/evaluator/work/onboarding/)."""
    try:
        d = os.path.join(paths.ws_dir("evaluator"), "work", "onboarding")
    except Denied:
        return None
    os.makedirs(d, mode=0o700, exist_ok=True)
    p = os.path.join(d, name)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(p + ".tmp", p)
    return p


def import_resume(resume_path: str, extra_path: str | None = None) -> dict:
    """Copy the resume to private/resume/original.<ext>, extract its text to private/resume/resume.txt
    (kept when the person pasted text there and the new extraction is poor), copy the extra information
    to private/extra_info.md, and give the evaluator copies for the onboarding inference."""
    from . import pdftext
    src = os.path.realpath(resume_path)
    ext = os.path.splitext(src)[1].lower().lstrip(".")
    if ext not in ("pdf", "docx", "txt", "md"):
        raise Denied("E_VALIDATION", "the resume must be a PDF, DOCX, TXT or MD file")
    res_dir = os.path.join(paths.private_dir(), "resume")
    os.makedirs(res_dir, mode=0o700, exist_ok=True)
    dest = os.path.join(res_dir, "original." + ext)
    ex = pdftext.extract(src)
    if os.path.realpath(dest) != src:
        for other in ("pdf", "docx", "txt", "md"):
            p = os.path.join(res_dir, "original." + other)
            if other != ext and os.path.exists(p):
                os.unlink(p)
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o600)
    txt_path = resume_text_path()
    kept_existing = False
    if ex["quality"] == "poor" and os.path.isfile(txt_path) and os.path.getsize(txt_path) > 200:
        kept_existing = True
    else:
        fd = os.open(txt_path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(ex["text"])
        os.replace(txt_path + ".tmp", txt_path)
    extra_dest = None
    if extra_path:
        esrc = os.path.realpath(extra_path)
        if not os.path.isfile(esrc):
            raise Denied("E_NOT_FOUND", "extra information file not found", data={"path": extra_path})
        extra_dest = extra_info_path()
        if esrc != os.path.realpath(extra_dest):
            shutil.copyfile(esrc, extra_dest)
            os.chmod(extra_dest, 0o600)
    text = _read_text(txt_path) or ""
    _copy_to_evaluator_onboarding("resume.txt", text)
    extra_text = extra_info_text()
    if extra_text is not None:
        _copy_to_evaluator_onboarding("extra_info.md", extra_text)
    return {"resume_path": dest, "text_path": txt_path, "extract_method": ex["method"], "chars": ex["chars"],
            "quality": ex["quality"], "kept_pasted_text": kept_existing, "extra_path": extra_dest,
            "next": ("paste the resume text into %s, then run profile import again" % txt_path)
            if ex["quality"] == "poor" and not kept_existing else ""}


def write_salary_packet(role_titles: list[str], city: str) -> str:
    """Onboarding step 5: the scout gets only role titles and a city (no resume) in
    WS/scout/work/onboarding/salary_inputs.json."""
    titles = [_clean_text(t, "role_titles", 80) for t in role_titles if isinstance(t, str) and t.strip()][:6]
    if not titles:
        raise Denied("E_VALIDATION", "give at least one role title")
    d = os.path.join(paths.ws_dir("scout"), "work", "onboarding")
    os.makedirs(d, mode=0o700, exist_ok=True)
    p = os.path.join(d, "salary_inputs.json")
    _write_json(p, {"role_titles": titles, "city": _clean_text(city, "city", 60).lower()})
    return p


# ---------------------------------------------------------------- interview (TTY)
def interview(conn_factory, inp, out, only_open: bool = False) -> dict:
    """Ask every question with its default; Enter accepts the default, 'skip' leaves an optional one out.
    `conn_factory()` returns a connection; each answer is stored in its own transaction."""
    from . import db
    answered = 0
    qs = questions(only_open=only_open)
    for q in qs:
        if q["id"].startswith("X"):
            prompt = "%s %s\n> " % (q["id"], q["text"])
        else:
            prompt = "%s%s %s\n   (%s)\n" % (q["id"], " [required]" if q["required"] else "", q["text"], q["why"])
            if q.get("default"):
                prompt += "   default: %s\n" % q["default"].replace("\n", " | ")
            prompt += "> "
        while True:
            out.write(prompt)
            out.flush()
            line = inp.readline()
            if not line:
                return {"answered": answered, "complete": False}
            line = line.rstrip("\n")
            if not line.strip():
                if q.get("default") is None:
                    if q["required"]:
                        out.write("   this one is required\n")
                        continue
                    break
                line = q["default"]
            if line.strip().lower() == "skip" and q["required"]:
                out.write("   this one is required\n")
                continue
            conn = conn_factory()
            try:
                with db.tx(conn):
                    answer(conn, q["id"], line, sensitive_ok=True, by="human")
                answered += 1
                break
            except Denied as d:
                out.write("   %s\n" % d.message)
            finally:
                conn.close()
    st = status()
    return {"answered": answered, "complete": st["confirmed"], "missing": st["missing"]}
