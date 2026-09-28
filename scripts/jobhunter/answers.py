"""Application form answer bank, "never invent" (design 6.5, 12.20).

File private/answers.json (gitignored): {"version": 1, "answers": [{key, patterns, value, source, sensitive,
origin?}]}. A form label is matched case-insensitively against each entry's regex patterns (searched over
the whole normalised label). Only values whose source is resume, profile or user_confirmed are served.
Missing, empty, ambiguous or unconfirmed answers are never guessed: the caller opens an answer_question
task and the job waits for the person, who answers once (`profile answer <task uid>` in chat or at the
terminal, or `answers add`), after which the answer is reused.

Never stored and always human: date of birth, home street address, government ids, passwords, account
creation and legal attestations.
"""
from __future__ import annotations

import json
import os
import re

from . import paths
from .canon import now
from .errors import Denied
from .events import log_event, open_human_task

USABLE_SOURCES = ("resume", "profile", "user_confirmed")
FIELD_TYPES = ("text", "number", "choice", "date", "boolean")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

DEFAULT_BANK = [
    {"key": "full_name", "patterns": ["^full name$", "^name$", "^(first and last|legal) name$"], "value": "",
     "source": "resume", "sensitive": False},
    {"key": "first_name", "patterns": ["^first name$", "^given name$"], "value": "", "source": "resume",
     "sensitive": False},
    {"key": "last_name", "patterns": ["^last name$", "^surname$", "^family name$"], "value": "", "source": "resume",
     "sensitive": False},
    {"key": "email", "patterns": ["^e-?mail( address)?$", "^your e-?mail"], "value": "", "source": "profile",
     "sensitive": False},
    {"key": "phone", "patterns": ["phone", "mobile"], "value": "", "source": "profile", "sensitive": False},
    {"key": "current_city", "patterns": ["current (city|location)", "where are you (currently )?based"], "value": "",
     "source": "user_confirmed", "sensitive": False},
    {"key": "total_experience_years", "patterns": ["total (years of )?experience", "years of (relevant )?experience"],
     "value": "", "source": "user_confirmed", "sensitive": False},
    {"key": "notice_period_days", "patterns": ["notice period"], "value": "", "source": "user_confirmed",
     "sensitive": False},
    {"key": "expected_ctc", "patterns": ["expected (ctc|salary|compensation|pay)", "salary expectation"],
     "value": "", "source": "user_confirmed", "sensitive": True},
    {"key": "current_ctc", "patterns": ["current (ctc|salary|compensation|pay)"], "value": "",
     "source": "user_confirmed", "sensitive": True},
    {"key": "work_authorization", "patterns": ["authori[sz]ed to work", "work permit", "right to work"], "value": "",
     "source": "user_confirmed", "sensitive": False},
    {"key": "requires_sponsorship", "patterns": ["sponsorship"], "value": "", "source": "user_confirmed",
     "sensitive": False},
    {"key": "willing_to_relocate", "patterns": ["relocat"], "value": "", "source": "user_confirmed",
     "sensitive": False},
    {"key": "linkedin_url", "patterns": ["linkedin"], "value": "", "source": "profile", "sensitive": False},
    {"key": "github_url", "patterns": ["github"], "value": "", "source": "profile", "sensitive": False},
    {"key": "portfolio_url", "patterns": ["portfolio", "personal (website|site)", "^website$"], "value": "",
     "source": "profile", "sensitive": False},
    {"key": "eeo_choice", "patterns": ["gender", "ethnicity", "race", "veteran", "disability"], "value": "",
     "source": "user_confirmed", "sensitive": True},
]

# labels that are never answered from the bank (6.5)
ALWAYS_HUMAN = [
    ("date_of_birth", r"(date of birth|\bdob\b|birth ?date|birthday|\bage\b)"),
    ("home_address", r"(street|home|postal|mailing|residential) address|address line|\bzip\b|postcode|postal code|"
                     r"\bpin ?code\b"),
    ("government_id", r"passport|aadhaa?r|\bpan\b|social security|\bssn\b|national (id|insurance)|government id|"
                      r"driver'?s? licen[cs]e|tax (id|number)|\bvisa number\b"),
    ("password", r"password|passcode|\bpin\b"),
    ("account_creation", r"create (an )?account|username|user name"),
    ("attestation", r"\bi (hereby )?(certify|attest|declare|agree|acknowledge|consent)|declaration|attestation|"
                    r"terms (and|&) conditions|signature|sign here|privacy (policy|notice)"),
]
SENSITIVE_LABEL = re.compile(r"salary|\bctc\b|compensation|\bpay\b|gender|ethnicity|\brace\b|veteran|disability|"
                             r"sexual orientation|religion|caste|pronoun", re.I)


# ---------------------------------------------------------------- file
def bank_path() -> str:
    return os.path.join(paths.private_dir(), "answers.json")


def load() -> dict:
    try:
        with open(bank_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"version": 1, "answers": [dict(e) for e in DEFAULT_BANK]}
    except (OSError, ValueError) as exc:
        raise Denied("E_CONFIG_INVALID", "private/answers.json is unreadable: %s" % exc)
    if not isinstance(data, dict) or not isinstance(data.get("answers"), list):
        raise Denied("E_CONFIG_INVALID", "private/answers.json must be {version, answers: [...]}")
    return data


def save(bank: dict) -> None:
    os.makedirs(paths.private_dir(), mode=0o700, exist_ok=True)
    p = bank_path()
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(bank, fh, indent=1, sort_keys=True, ensure_ascii=True)
        fh.write("\n")
    os.replace(p + ".tmp", p)


def _entry(bank: dict, key: str) -> dict | None:
    for e in bank.get("answers") or []:
        if isinstance(e, dict) and e.get("key") == key:
            return e
    return None


# ---------------------------------------------------------------- labels
def normalize_label(label: str) -> str:
    s = " ".join(str(label or "").split())
    s = re.sub(r"\((required|optional|mandatory)\)", "", s, flags=re.I)
    s = s.replace("*", " ").strip()
    s = re.sub(r"[\s:?.]+$", "", s)
    return " ".join(s.split()).lower()


def always_human(label: str) -> str | None:
    n = normalize_label(label)
    for name, rx in ALWAYS_HUMAN:
        if re.search(rx, n, re.I):
            return name
    return None


def _norm_choice(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+]+", " ", str(s).lower()).split())


def _match_choice(value: str, choices: list[str]) -> str | None:
    nv = _norm_choice(value)
    exact = [c for c in choices if _norm_choice(c) == nv]
    if len(exact) == 1:
        return exact[0]
    yes, no = {"yes", "y", "true"}, {"no", "n", "false"}
    if nv in yes | no:
        want = "yes" if nv in yes else "no"
        hits = [c for c in choices if _norm_choice(c).split()[:1] == [want]]
        if len(hits) == 1:
            return hits[0]
    if re.match(r"^[0-9]+(\.[0-9]+)?$", nv):
        hits = [c for c in choices if re.findall(r"[0-9]+(?:\.[0-9]+)?", c) == [nv]]
        if len(hits) == 1:
            return hits[0]
    return None


def validate_question(q) -> dict:
    if not isinstance(q, dict):
        raise Denied("E_SCHEMA", "the question file must be a JSON object")
    extra = set(q) - {"label", "field_type", "choices", "job_uid"}
    if extra:
        raise Denied("E_SCHEMA", "unknown keys %s" % sorted(extra))
    if not isinstance(q.get("label"), str) or not q["label"].strip() or len(q["label"]) > 500:
        raise Denied("E_SCHEMA", "label is required (at most 500 characters)")
    ft = q.get("field_type", "text")
    if ft not in FIELD_TYPES:
        raise Denied("E_SCHEMA", "field_type must be one of %s" % ", ".join(FIELD_TYPES))
    choices = q.get("choices")
    if choices is not None and (not isinstance(choices, list) or not all(isinstance(c, str) for c in choices)
                                or len(choices) > 200):
        raise Denied("E_SCHEMA", "choices must be a list of strings")
    if ft == "choice" and not choices:
        raise Denied("E_SCHEMA", "a choice field needs its choices")
    job = q.get("job_uid")
    if job is not None and (not isinstance(job, str) or not re.match(r"^J[A-Z2-7]{7}$", job)):
        raise Denied("E_SCHEMA", "job_uid must be a job id")
    return {"label": q["label"], "field_type": ft, "choices": choices, "job_uid": job}


# ---------------------------------------------------------------- lookup (12.10)
def lookup(question: dict) -> dict:
    """{found, key, value, source, sensitive} or {found: false, reason, ...}. Pure: reads the bank only."""
    q = validate_question(question)
    label = normalize_label(q["label"])
    miss = {"found": False, "key": None, "value": None, "source": None, "sensitive": bool(SENSITIVE_LABEL.search(label))}
    rule = always_human(q["label"])
    if rule:
        return dict(miss, reason="always_human", rule=rule)
    bank = load()
    matches = []
    for e in bank.get("answers") or []:
        if not isinstance(e, dict) or not isinstance(e.get("key"), str):
            continue
        for p in e.get("patterns") or []:
            try:
                m = re.search(p, label, re.I)
            except re.error:
                continue
            if m:
                matches.append((e, p.startswith("^") and p.endswith("$")))
                break
    if not matches:
        return dict(miss, reason="no_match")
    def usable(e):
        v = e.get("value")
        return e.get("source") in USABLE_SOURCES and v is not None and str(v).strip() != ""

    filled = [(e, a) for e, a in matches if usable(e)]
    if filled:
        matches = filled            # an empty generic entry never hides a filled specific one
    anchored = [e for e, a in matches if a]
    pool = anchored or [e for e, _a in matches]
    keys = sorted({e["key"] for e in pool})
    if len(keys) > 1:
        return dict(miss, reason="ambiguous", keys=keys)
    e = pool[0]
    sensitive = bool(e.get("sensitive")) or miss["sensitive"]
    value = e.get("value")
    if value is None or (isinstance(value, str) and not value.strip()):
        return dict(miss, key=e["key"], sensitive=sensitive, reason="empty_value")
    if e.get("source") not in USABLE_SOURCES:
        return dict(miss, key=e["key"], sensitive=sensitive, reason="unconfirmed_source")
    value = str(value)
    ft = q["field_type"]
    if ft == "choice":
        chosen = _match_choice(value, q["choices"] or [])
        if chosen is None:
            return dict(miss, key=e["key"], sensitive=sensitive, reason="choice_mismatch")
        value = chosen
    elif ft == "number":
        m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*$", value.replace(",", ""))
        if not m:
            return dict(miss, key=e["key"], sensitive=sensitive, reason="type_mismatch")
        value = m.group(1)
    elif ft == "boolean":
        nv = _norm_choice(value)
        if nv in ("yes", "true", "y"):
            value = "Yes"
        elif nv in ("no", "false", "n"):
            value = "No"
        else:
            return dict(miss, key=e["key"], sensitive=sensitive, reason="type_mismatch")
    elif ft == "date":
        if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", value):
            return dict(miss, key=e["key"], sensitive=sensitive, reason="type_mismatch")
    return {"found": True, "key": e["key"], "value": value, "source": e["source"], "sensitive": sensitive}


def human_question_text(q: dict) -> str:
    text = "A form asks: %s" % " ".join(q["label"].split())
    if q.get("choices"):
        text += " (choices: %s)" % ", ".join(q["choices"][:20])
    return text


def _to_human(conn, job_uid: str, reason: str) -> bool:
    """Hand the job to the person through U6 applyq.to_human, the single writer of apply_queued ->
    needs_human (design 2.5). Returns False when the job's status does not allow it or U6 is absent."""
    try:
        from .applyq import to_human
    except ImportError:
        return False
    to_human(conn, job_uid, reason)
    return True


def _from_human(conn, job_uid: str, reason: str) -> bool:
    """Give a parked job back to the apply lane through U6 applyq.return_from_human, the single writer of
    needs_human -> eligible (design 2.5). Returns False when U6 is absent."""
    try:
        from .applyq import return_from_human
    except ImportError:
        return False
    return_from_human(conn, job_uid, reason)
    return True


def _release_job(conn, job_id) -> bool:
    """After an answer_question task is answered: hand the job back when it was parked for form questions
    (needs_human with reason answer_question) and no other answer_question task for it is still open.
    A job parked for anything else (CAPTCHA, account wall, a question only the person can fill in) stays."""
    if job_id is None:
        return False
    job = conn.execute("SELECT job_uid, status, status_reason FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or job["status"] != "needs_human" or job["status_reason"] != "answer_question":
        return False
    still_open = conn.execute("SELECT 1 FROM human_tasks WHERE job_id = ? AND kind = 'answer_question' "
                              "AND done_at IS NULL LIMIT 1", (job_id,)).fetchone()
    if still_open is not None:
        return False
    return _from_human(conn, job["job_uid"], "answered")


def request_human(conn, question: dict, reason: str) -> dict:
    """Open (or reuse) an answer_question task for a question the bank cannot answer. When the job is
    eligible, queued or failed it is handed to the person (U6 applyq.to_human); U4 itself never writes a
    job status. Runs inside the caller's transaction."""
    q = validate_question(question)
    job_id = None
    parked = False
    if q.get("job_uid"):
        row = conn.execute("SELECT id, status FROM jobs WHERE job_uid = ?", (q["job_uid"],)).fetchone()
        if row is None:
            raise Denied("E_NOT_FOUND", "no job %s" % q["job_uid"])
        job_id = row["id"]
    detail = json.dumps({"label": q["label"], "field_type": q["field_type"], "choices": q["choices"],
                         "job_uid": q["job_uid"], "reason": reason}, sort_keys=True)
    uid = open_human_task(conn, "answer_question", human_question_text(q), job_id=job_id, detail=detail)
    if job_id is not None and row["status"] in ("eligible", "apply_queued", "apply_failed"):
        parked = _to_human(conn, q["job_uid"], "answer_question")
    return {"human_task": uid, "reason": reason, "job_uid": q["job_uid"], "job_to_human": parked}


# ---------------------------------------------------------------- writes
def add(key: str, value: str, patterns: list[str] | None = None, sensitive: bool = False,
        source: str = "user_confirmed") -> dict:
    """Upsert one answer the person gave. Existing patterns are kept unless new ones are given."""
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise Denied("E_VALIDATION", "key must be lower case letters, digits and underscores")
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise Denied("E_VALIDATION", "value must be 1 to 2000 characters")
    if re.search("[\u2010-\u2015\u2212]", value):
        raise Denied("E_VALIDATION", "use plain words instead of dash characters")
    if source not in USABLE_SOURCES:
        raise Denied("E_VALIDATION", "source must be one of %s" % ", ".join(USABLE_SOURCES))
    if patterns is not None:
        if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p for p in patterns):
            raise Denied("E_VALIDATION", "patterns must be a non-empty list of regular expressions")
        for p in patterns:
            try:
                re.compile(p)
            except re.error as exc:
                raise Denied("E_VALIDATION", "bad pattern %r: %s" % (p, exc))
    bank = load()
    e = _entry(bank, key)
    if e is None:
        e = {"key": key, "patterns": patterns or ["^%s$" % re.escape(key.replace("_", " "))]}
        bank["answers"].append(e)
    elif patterns is not None:
        e["patterns"] = patterns
    if e.get("sensitive") and not sensitive:
        sensitive = True        # an entry marked sensitive stays sensitive
    e.update({"value": value.strip(), "source": source, "sensitive": bool(sensitive), "origin": "person",
              "updated_at": now()})
    save(bank)
    return {"key": key, "sensitive": bool(sensitive), "patterns": e["patterns"]}


def answer_task(conn, task_uid: str, value, sensitive_ok: bool, by: str) -> dict:
    """Answer an open answer_question task: store the answer under a key for that exact label and close the
    task. When that was the job's last open form question and the job waits in needs_human for it, the job is
    handed back through U6 applyq.return_from_human (U4 never writes a job status itself, design 2.5). Runs
    inside the caller's transaction."""
    row = conn.execute("SELECT * FROM human_tasks WHERE task_uid = ?", (task_uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no task %s" % task_uid)
    if row["kind"] != "answer_question":
        raise Denied("E_VALIDATION", "task %s is not a form question" % task_uid)
    if row["done_at"] is not None:
        raise Denied("E_PRECONDITION", "task %s is already answered" % task_uid)
    try:
        detail = json.loads(row["detail"] or "{}")
    except ValueError:
        detail = {}
    label = detail.get("label")
    if not isinstance(label, str) or not label.strip():
        raise Denied("E_VALIDATION", "task %s has no form label" % task_uid)
    if always_human(label):
        raise Denied("E_VALIDATION", "this kind of question is never stored; answer it on the form yourself")
    sensitive = bool(SENSITIVE_LABEL.search(normalize_label(label)))
    if sensitive and not sensitive_ok:
        raise Denied("E_HUMAN_ONLY", "sensitive answers are stored only at the terminal (./jobhunter answers add)")
    text = " ".join(str(value).split())
    choices = detail.get("choices")
    if choices and _match_choice(text, choices) is None:
        raise Denied("E_VALIDATION", "the answer must be one of: %s" % ", ".join(choices[:20]))
    slug = re.sub(r"[^a-z0-9]+", "_", normalize_label(label)).strip("_")[:50] or "question"
    key = "q_" + slug
    res = add(key, text, patterns=["^%s$" % re.escape(normalize_label(label))], sensitive=sensitive)
    conn.execute("UPDATE human_tasks SET done_at = ?, resolution = ? WHERE id = ?", (now(), "answered", row["id"]))
    log_event(conn, "answer_stored", key=key, task_uid=task_uid, by=by)
    released = _release_job(conn, row["job_id"])
    return {"field": task_uid, "key": res["key"], "stored_value": "(sensitive)" if sensitive else text,
            "sensitive": sensitive, "job_released": released}


def _fmt_num(v) -> str:
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def sync_from_profile(raw: dict, fields: dict) -> None:
    """Fill answer-bank entries derived from the confirmed profile and the resume contact block. Entries
    the person set (origin 'person') are never overwritten; a derived entry whose source field is no
    longer confirmed is cleared."""
    bank = load()
    conf = {k: v["value"] for k, v in fields.items() if v.get("source") == "user_confirmed"}
    contact_inferred = ((raw.get("inferred") or {}).get("contact") or {})
    contact_conf = conf.get("contact") or {}
    links = []
    try:
        from . import resume as R
        base = R.load_base(required=False)
        if base:
            links = [ln.get("url") or "" for ln in base["contact"].get("links") or []]
            contact_inferred = dict(contact_inferred, **{k: base["contact"].get(k) for k in
                                                         ("full_name", "first_name", "last_name", "email", "phone")})
    except Denied:
        pass

    def pick(k):
        if contact_conf.get(k):
            return contact_conf[k], "user_confirmed"
        if contact_inferred.get(k):
            return contact_inferred[k], "resume"
        return None, None

    derived: dict[str, tuple] = {}
    for key in ("full_name", "first_name", "last_name", "email", "phone"):
        v, src = pick(key)
        derived[key] = (v, src)
    derived["current_city"] = (contact_conf.get("current_city"), "user_confirmed")
    ey = conf.get("experience_years")
    derived["total_experience_years"] = (_fmt_num(ey) if ey is not None else None, "user_confirmed")
    npd = conf.get("notice_period_days")
    derived["notice_period_days"] = (str(npd) if npd is not None else None, "user_confirmed")
    sal = conf.get("salary")
    exp = None
    if sal and sal.get("may_state_expected_in_forms"):
        exp = _fmt_num(sal.get("target"))
    derived["expected_ctc"] = (exp, "user_confirmed")
    cp = conf.get("current_pay")
    derived["current_ctc"] = (_fmt_num(cp["amount"]) if cp else None, "user_confirmed")
    loc = conf.get("locations")
    derived["willing_to_relocate"] = (("Yes" if loc.get("relocate") else "No") if loc else None, "user_confirmed")
    eeo = conf.get("eeo")
    derived["eeo_choice"] = (None, "user_confirmed") if eeo in (None, "ask") else (
        ("Decline to self-identify" if eeo == "decline" else None), "user_confirmed")
    for key, host in (("linkedin_url", "linkedin.com"), ("github_url", "github.com")):
        url = next((u for u in links if host in u), None)
        derived[key] = (url, "resume")
    other = [u for u in links if "linkedin.com" not in u and "github.com" not in u]
    derived["portfolio_url"] = (other[0] if other else None, "resume")

    changed = False
    if isinstance(eeo, dict):
        for topic, ans in eeo.items():
            slug = re.sub(r"[^a-z0-9]+", "_", str(topic).lower()).strip("_")[:40]
            if not slug:
                continue
            key = "eeo_" + slug
            derived[key] = (ans, "user_confirmed")
            if _entry(bank, key) is None:
                bank["answers"].append({"key": key, "patterns": [re.escape(str(topic).lower())], "value": "",
                                        "source": "user_confirmed", "sensitive": True})
    for e in bank.get("answers") or []:
        k = e.get("key") if isinstance(e, dict) else None
        if isinstance(k, str) and k.startswith("eeo_") and k not in derived and e.get("origin") == "profile":
            derived[k] = (None, "user_confirmed")
    for key, (value, src) in derived.items():
        e = _entry(bank, key)
        if e is None:
            tmpl = next((d for d in DEFAULT_BANK if d["key"] == key), None)
            if tmpl is None or value is None:
                continue
            e = dict(tmpl)
            bank["answers"].append(e)
            changed = True
        if e.get("origin") == "person":
            continue
        new_val = "" if value is None else str(value)
        if e.get("value") != new_val or (value is not None and e.get("source") != src):
            e["value"] = new_val
            if value is not None:
                e["source"] = src
            e["origin"] = "profile"
            changed = True
    if changed or not os.path.exists(bank_path()):
        save(bank)
