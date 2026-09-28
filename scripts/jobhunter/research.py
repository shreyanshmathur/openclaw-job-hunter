"""Research facts (`research add --file`, `research list`; design 6.1 steps 2 to 4 and 12.2).

Each fact keeps a verbatim snippet (at most 300 characters), its https source URL and its dates. Page
text is data: code flags snippets that read like instructions to an AI (injection_flag), and the linter
refuses such facts as hooks (H-INJECTION-IN-SOURCE). Re-adding the same snippet from the same URL for the
same subject returns the stored fact (idempotent). Runs inside the caller's transaction.
"""
from __future__ import annotations

import re

from . import canon
from .errors import Denied
from .events import log_event
from .threads import dep

FACT_KEYS = {"text", "snippet", "source_type", "source_url", "published_at", "retrieved_at"}
SUBJECT_KINDS = ("person", "company", "job")
MAX_FACTS = 20
SOURCE_TYPE_RE = re.compile(r"^[a-z][a-z_]{1,39}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

INJECTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"\bignore\b.{0,40}\b(instructions?|prompts?|rules|messages?)\b",
    r"\bdisregard\b.{0,40}\b(instructions?|prompts?|rules|above|previous)\b",
    r"\bforget\b.{0,30}\b(instructions?|everything|previous|prior)\b",
    r"\b(system|developer)\s*prompt\b",
    r"\byou\s+are\s+(now\s+)?(an?\s+)?(ai|assistant|language\s+model|llm|chatbot|gpt|claude)\b",
    r"\bas\s+an?\s+(ai|language\s+model|llm)\b",
    r"\bif\s+you\s+are\s+(an?\s+)?(ai|llm|bot|assistant|language\s+model|gpt|chatbot|agent|recruiting\s+tool)\b",
    r"\b(ai|llm|gpt|chatbot|assistant|agent|bot)s?\s+(reading|processing|reviewing|screening|parsing)\s+this\b",
    r"\b(recommend|hire|shortlist|approve)\s+(this|the|me|my)\s+(candidate|applicant|profile)\b",
    r"\bnew\s+instructions?\b",
    r"\bdo\s+not\s+(tell|mention|reveal|disclose)\b",
    r"\b(begin|end)\s+(system|prompt|instructions?)\b",
    r"<\|[a-z_]+\|>|\[/?(inst|system)\]|###\s*(instruction|system)",
    r"^\s*(system|assistant|user)\s*:",
    r"\bprompt\s+injection\b",
    r"\bjailbreak\b",
)]


def injection_flag(*texts: str | None) -> int:
    """1 when any text reads like an instruction aimed at an AI agent."""
    for t in texts:
        if not t:
            continue
        for rx in INJECTION_PATTERNS:
            if rx.search(t):
                return 1
    return 0


def _date(value, name: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise Denied("E_SCHEMA", "%s is required" % name)
        return None
    if not isinstance(value, str):
        raise Denied("E_SCHEMA", "%s must be a string" % name)
    v = value.strip()
    if DATE_RE.match(v):
        try:
            canon.parse_ts(v + "T00:00:00Z")
        except ValueError:
            raise Denied("E_VALIDATION", "%s is not a real date" % name)
        return v
    try:
        return canon.fmt_ts(canon.parse_ts(v))
    except ValueError:
        raise Denied("E_VALIDATION", "%s must be YYYY-MM-DD or a UTC timestamp" % name)


def _as_ts(v: str) -> str:
    return v + "T00:00:00Z" if DATE_RE.match(v) else v


def _resolve_subject(conn, subject) -> tuple[str, int, str]:
    if not isinstance(subject, dict):
        raise Denied("E_SCHEMA", "subject must be an object")
    kind = subject.get("kind")
    if kind not in SUBJECT_KINDS:
        raise Denied("E_SCHEMA", "subject.kind must be person, company or job")
    allowed = {"person": {"kind", "contact_uid"}, "company": {"kind", "company_uid", "name", "domain"},
               "job": {"kind", "job_uid"}}[kind]
    extra = set(subject) - allowed
    if extra:
        raise Denied("E_SCHEMA", "unknown keys in subject", data={"unknown": sorted(extra)})
    if kind == "person":
        row = conn.execute("SELECT id, contact_uid, merged_into FROM contacts WHERE contact_uid = ?",
                           (subject.get("contact_uid"),)).fetchone()
        if row is None:
            raise Denied("E_NOT_FOUND", "no contact %s" % subject.get("contact_uid"))
        cid = row["id"]
        while row["merged_into"]:
            row = conn.execute("SELECT id, contact_uid, merged_into FROM contacts WHERE id = ?",
                               (row["merged_into"],)).fetchone()
            cid = row["id"]
        return kind, cid, row["contact_uid"]
    if kind == "job":
        row = conn.execute("SELECT id, job_uid FROM jobs WHERE job_uid = ?", (subject.get("job_uid"),)).fetchone()
        if row is None:
            raise Denied("E_NOT_FOUND", "no job %s" % subject.get("job_uid"))
        return kind, row["id"], row["job_uid"]
    if subject.get("company_uid"):
        row = conn.execute("SELECT id, company_uid, merged_into FROM companies WHERE company_uid = ?",
                           (subject["company_uid"],)).fetchone()
        if row is None:
            raise Denied("E_NOT_FOUND", "no company %s" % subject["company_uid"])
        while row["merged_into"]:
            row = conn.execute("SELECT id, company_uid, merged_into FROM companies WHERE id = ?",
                               (row["merged_into"],)).fetchone()
        return kind, row["id"], row["company_uid"]
    name, domain = subject.get("name"), subject.get("domain")
    if not (isinstance(name, str) and name.strip()) and not (isinstance(domain, str) and domain.strip()):
        raise Denied("E_SCHEMA", "a company subject needs company_uid, name or domain")
    cid = dep("companies").resolve(conn, name=name or None, domain=domain or None,
                                   source="careers_url" if domain else "job_board", create=True)
    row = conn.execute("SELECT company_uid FROM companies WHERE id = ?", (cid,)).fetchone()
    return kind, cid, row[0]


def _fact(item, idx: int) -> dict:
    if not isinstance(item, dict):
        raise Denied("E_SCHEMA", "facts[%d] must be an object" % idx)
    extra = set(item) - FACT_KEYS
    if extra:
        raise Denied("E_SCHEMA", "unknown keys in facts[%d]" % idx, data={"unknown": sorted(extra)})
    out = {}
    for key, limit in (("text", 600), ("snippet", 300)):
        v = item.get(key)
        if not isinstance(v, str) or not v.strip():
            raise Denied("E_SCHEMA", "facts[%d].%s is required" % (idx, key))
        v = canon.normalize_text(v)
        if _CTRL_RE.search(v):
            raise Denied("E_VALIDATION", "facts[%d].%s has control characters" % (idx, key))
        if len(v) > limit:
            raise Denied("E_VALIDATION", "facts[%d].%s is longer than %d characters" % (idx, key, limit))
        out[key] = v
    st = item.get("source_type")
    if not isinstance(st, str) or not SOURCE_TYPE_RE.match(st):
        raise Denied("E_VALIDATION", "facts[%d].source_type must be a lower_case word" % idx)
    out["source_type"] = st
    url = item.get("source_url")
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2000 or _CTRL_RE.search(url) \
            or " " in url:
        raise Denied("E_VALIDATION", "facts[%d].source_url must be an https URL" % idx)
    if "lnkd.in" in url.lower():
        raise Denied("E_VALIDATION", "facts[%d].source_url: lnkd.in short links are refused" % idx)
    out["source_url"] = url
    out["published_at"] = _date(item.get("published_at"), "facts[%d].published_at" % idx, False)
    out["retrieved_at"] = _date(item.get("retrieved_at"), "facts[%d].retrieved_at" % idx, True)
    stamp = canon.now()
    if _as_ts(out["retrieved_at"]) > canon.ts_add(stamp, days=1):
        raise Denied("E_VALIDATION", "facts[%d].retrieved_at is in the future" % idx)
    if out["published_at"] and _as_ts(out["published_at"]) > canon.ts_add(stamp, days=1):
        raise Denied("E_VALIDATION", "facts[%d].published_at is in the future" % idx)
    return out


def add_research(conn, data: dict) -> dict:
    """Store the facts of one research record (12.2). Returns {subject, facts: [{fact_uid, injection_flag,
    existing}]}."""
    if not isinstance(data, dict):
        raise Denied("E_SCHEMA", "research record must be a JSON object")
    extra = set(data) - {"subject", "facts"}
    if extra:
        raise Denied("E_SCHEMA", "unknown keys in research record", data={"unknown": sorted(extra)})
    facts = data.get("facts")
    if not isinstance(facts, list) or not facts:
        raise Denied("E_SCHEMA", "facts must be a non-empty list")
    if len(facts) > MAX_FACTS:
        raise Denied("E_VALIDATION", "at most %d facts per record" % MAX_FACTS)
    checked = [_fact(item, i) for i, item in enumerate(facts)]
    kind, subject_id, subject_uid = _resolve_subject(conn, data.get("subject"))
    stamp = canon.now()
    out = []
    for f in checked:
        row = conn.execute("SELECT fact_uid, injection_flag FROM research_facts WHERE subject_kind = ? AND "
                           "subject_id = ? AND source_url = ? AND snippet = ?",
                           (kind, subject_id, f["source_url"], f["snippet"])).fetchone()
        if row is not None:
            out.append({"fact_uid": row[0], "injection_flag": row[1], "existing": True})
            continue
        flag = injection_flag(f["snippet"], f["text"])
        uid = canon.new_uid("R")
        conn.execute("INSERT INTO research_facts (fact_uid, subject_kind, subject_id, text, snippet, source_type, "
                     "source_url, published_at, retrieved_at, injection_flag, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (uid, kind, subject_id, f["text"], f["snippet"], f["source_type"], f["source_url"],
                      f["published_at"], f["retrieved_at"], flag, stamp))
        out.append({"fact_uid": uid, "injection_flag": flag, "existing": False})
    log_event(conn, "research_added", subject_kind=kind, subject_uid=subject_uid, n=len(out),
              flagged=sum(1 for x in out if x["injection_flag"]))
    return {"subject": {"kind": kind, "uid": subject_uid}, "facts": out}


def list_research(conn, *, contact_uid: str | None = None, company_uid: str | None = None,
                  job_uid: str | None = None) -> list[dict]:
    """Facts of one subject with their age in days (from retrieved_at)."""
    given = [x for x in (contact_uid, company_uid, job_uid) if x]
    if len(given) != 1:
        raise Denied("E_USAGE", "give exactly one of --contact, --company or --job")
    if contact_uid:
        kind, table, col, uid = "person", "contacts", "contact_uid", contact_uid
    elif company_uid:
        kind, table, col, uid = "company", "companies", "company_uid", company_uid
    else:
        kind, table, col, uid = "job", "jobs", "job_uid", job_uid
    row = conn.execute("SELECT id FROM %s WHERE %s = ?" % (table, col), (uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no %s %s" % (kind, uid))
    stamp = canon.now()
    out = []
    for r in conn.execute("SELECT * FROM research_facts WHERE subject_kind = ? AND subject_id = ? ORDER BY id",
                          (kind, row[0])):
        try:
            age = max(0, canon.seconds_between(_as_ts(r["retrieved_at"]), stamp) // 86400)
        except ValueError:
            age = None
        out.append({"fact_uid": r["fact_uid"], "text": r["text"], "snippet": r["snippet"],
                    "source_type": r["source_type"], "source_url": r["source_url"],
                    "published_at": r["published_at"], "retrieved_at": r["retrieved_at"], "age_days": age,
                    "injection_flag": r["injection_flag"]})
    return out
