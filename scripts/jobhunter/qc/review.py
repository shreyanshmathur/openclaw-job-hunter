"""Independent LLM reviewer: packets, strict verdict parsing, the code pass rule, start and wait (design 5.3).

- start(conn, draft_uid): checks the reviewer hashes (E_REVIEWER_TAMPERED), builds the packet from the database,
  writes it to state/qc/packets/<qjob_uid>.txt (mode 600, outside every agent workspace), inserts a queued
  qc_jobs row and sets the draft review_pending. The caller spawns the worker after COMMIT.
- render_packet(values): fills the reviewer prompt's placeholders once (data is never expanded again); {today} is
  the review date the reviewer measures hook ages against (the golden set passes its fixed as_of date instead).
- parse_verdict(raw, nonce, draft_sha): the last JSON object in the reply, validated against
  qc/schema_review.json (unknown keys or missing fields fail), nonce and draft sha256 echoed.
- decide(verdict, channel): recomputes weighted_score and the pass rule in code; a claim with supported false
  fails the truthful gate; the stricter of model and code wins.
- wait(conn, qjob_uid, max_s): polls every 2 s for at most 45 s; follows a retry of the same draft attempt.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time

from .. import canon, drafts, jobstate, paths
from ..canon import now
from ..errors import Denied
from ..events import log_event
from . import REVIEWER_PROMPT_FILE, SCHEMA_REVIEW_FILE, read_json, settings

WEIGHTS = {"specificity": 0.25, "value": 0.20, "human_voice": 0.20, "clarity": 0.10, "cta": 0.10, "tone_fit": 0.10,
           "channel_fit": 0.05}
GATES = ("truthful", "hook_verified", "swap_test", "no_ai_voice", "safe")
STRUCTURED = ("resume", "application_package")
SOFT_GATES = frozenset({"swap_test", "no_ai_voice"})
PLACEHOLDERS = ("nonce", "draft_sha256", "today", "channel", "recipient_json", "research_facts_json",
                "profile_facts_json", "lint_warnings_json", "subject_line_if_any", "body")
_PH_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
_schema_cache = None


# ---------------------------------------------------------------- strict JSON schema subset
def schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = read_json(SCHEMA_REVIEW_FILE)
    return _schema_cache


def _type_ok(v, t: str) -> bool:
    if t == "object":
        return isinstance(v, dict)
    if t == "array":
        return isinstance(v, list)
    if t == "string":
        return isinstance(v, str)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "null":
        return v is None
    return False


def validate(v, s: dict, path: str = "$") -> list:
    """Errors of value v against the schema subset used by qc/schema_review.json."""
    errs = []
    if "enum" in s and v not in s["enum"]:
        return ["%s: not one of %s" % (path, s["enum"])]
    t = s.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(v, x) for x in types):
            return ["%s: expected %s" % (path, "|".join(types))]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            errs.append("%s: below %s" % (path, s["minimum"]))
        if "maximum" in s and v > s["maximum"]:
            errs.append("%s: above %s" % (path, s["maximum"]))
    if isinstance(v, dict):
        props = s.get("properties") or {}
        for k in s.get("required") or []:
            if k not in v:
                errs.append("%s: missing %s" % (path, k))
        if s.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    # name the keys this object may hold, so a slip such as fact_id in an issue reads clearly
                    errs.append("%s: unknown key %s (this object takes only %s)" % (path, k, ", ".join(props)))
        for k, sub in props.items():
            if k in v:
                errs.extend(validate(v[k], sub, path + "." + k))
    if isinstance(v, list) and "items" in s:
        for i, item in enumerate(v):
            errs.extend(validate(item, s["items"], "%s[%d]" % (path, i)))
    return errs


def json_objects(text: str) -> list:
    """Top-level JSON objects found in text, in order (fenced or bare; prose around them is ignored)."""
    out = []
    dec = json.JSONDecoder()
    i = 0
    text = text or ""
    while True:
        i = text.find("{", i)
        if i < 0:
            return out
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = end


def parse_verdict(raw: str, nonce: str, draft_sha: str) -> dict:
    """{"ok": bool, "error": str | None, "verdict": dict | None}. Any problem is ok False (a FAIL verdict)."""
    objs = json_objects(raw if isinstance(raw, str) else "")
    if not objs:
        return {"ok": False, "error": "no JSON object in the reviewer reply", "verdict": None}
    v = objs[-1]
    errs = validate(v, schema())
    if errs:
        return {"ok": False, "error": "schema: " + "; ".join(errs[:5]), "verdict": None}
    if v["nonce"] != nonce:
        return {"ok": False, "error": "nonce mismatch", "verdict": None}
    if v["draft_sha256"] != draft_sha:
        return {"ok": False, "error": "draft sha256 mismatch", "verdict": None}
    return {"ok": True, "error": None, "verdict": v}


# ---------------------------------------------------------------- code pass rule
def decide(verdict: dict | None, channel: str, cfg: dict | None = None) -> dict:
    """Recompute the pass rule in code (writing-qc 9.2, design 5.3). The stricter of model and code wins."""
    rv = (cfg or settings())["qc"]["review"]
    min_w, min_core, min_any = float(rv["min_weighted"]), int(rv["min_core"]), int(rv["min_any"])
    if not verdict:
        return {"pass": False, "code_verdict": "fail", "model_verdict": None, "weighted_score": None,
                "gates_failed": ["unparseable"], "low_scores": [], "lowest_criterion": None, "soft_only": False,
                "disagree": False}
    scores = verdict["scores"]
    gates = dict(verdict["gates"])
    if any(c.get("supported") is False for c in verdict.get("claims") or []):
        gates["truthful"] = False
    weighted = round(sum(WEIGHTS[k] * float(scores[k]) for k in WEIGHTS), 2)
    low = []
    if channel in STRUCTURED:
        need = ("truthful", "safe")
        for k in ("clarity", "channel_fit"):
            if scores[k] < min_core:
                low.append("%s = %d" % (k, scores[k]))
        weighted_ok = True
    else:
        need = GATES
        for k in ("specificity", "value", "human_voice"):
            if scores[k] < min_core:
                low.append("%s = %d" % (k, scores[k]))
        for k in WEIGHTS:
            if scores[k] < min_any and "%s = %d" % (k, scores[k]) not in low:
                low.append("%s = %d" % (k, scores[k]))
        weighted_ok = weighted >= min_w
        if not weighted_ok:
            low.append("weighted_score = %.2f" % weighted)
    gates_failed = [g for g in need if not gates.get(g)]
    code_pass = not gates_failed and not low and weighted_ok
    model_pass = verdict.get("verdict") == "pass"
    lowest = min(WEIGHTS, key=lambda k: (scores[k], -WEIGHTS[k]))
    return {"pass": code_pass and model_pass, "code_verdict": "pass" if code_pass else "fail",
            "model_verdict": verdict.get("verdict"), "weighted_score": weighted, "gates_failed": gates_failed,
            "low_scores": low, "lowest_criterion": "%s = %d" % (lowest, scores[lowest]),
            "soft_only": (not (code_pass and model_pass)) and set(gates_failed) <= SOFT_GATES,
            "disagree": code_pass != model_pass}


def decision_from_row(rv, channel: str) -> dict:
    """decide() over a stored qc_results review row."""
    try:
        data = json.loads(rv["review_json"] or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict) or "parse_error" in data or not data:
        return decide(None, channel)
    return decide(data, channel)


# ---------------------------------------------------------------- integrity and packets
def agents_md_path() -> str:
    return os.path.join(paths.ws_dir("qc"), "AGENTS.md")


def _file_sha(path: str) -> str | None:
    try:
        return canon.sha256_file(path)
    except OSError:
        return None


def check_integrity(conn) -> None:
    """sha256 of prompts/reviewer.md and WS/qc/AGENTS.md must equal the meta rows written by
    `install render-workspaces`; otherwise E_REVIEWER_TAMPERED (fail closed, also when the rows are missing)."""
    want_p = conn.execute("SELECT value FROM meta WHERE key = 'reviewer_prompt_sha256'").fetchone()
    want_a = conn.execute("SELECT value FROM meta WHERE key = 'qc_agents_md_sha256'").fetchone()
    if not want_p or not want_a or not want_p[0] or not want_a[0]:
        raise Denied("E_REVIEWER_TAMPERED", "the reviewer hashes are not recorded; run ./jobhunter doctor "
                                            "(install render-workspaces)")
    got_p = _file_sha(REVIEWER_PROMPT_FILE)
    got_a = _file_sha(agents_md_path())
    if got_p != want_p[0] or got_a != want_a[0]:
        raise Denied("E_REVIEWER_TAMPERED", "the reviewer prompt or the QC agent's AGENTS.md changed",
                     data={"prompt_ok": got_p == want_p[0], "agents_md_ok": got_a == want_a[0]})


def _j(obj) -> str:
    """JSON for the packet; '<' is escaped so data can never close a tag of the prompt."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")


def today() -> str:
    """The review date (UTC, YYYY-MM-DD) the reviewer measures hook ages against (the {today} placeholder)."""
    return now()[:10]


def render_packet(values: dict) -> str:
    with open(REVIEWER_PROMPT_FILE, "r", encoding="utf-8") as fh:
        template = fh.read()
    return _PH_RE.sub(lambda m: str(values.get(m.group(1), "")), template)


def packet_values(conn, row, nonce: str) -> dict:
    """The packet inputs, all from stored rows: exact send text, recipient summary, research facts with URLs and
    snippets, profile facts, lint warnings, attachment name and hash."""
    from . import profile_facts
    p = drafts.payload_of(row)
    li = drafts.lint_input(conn, row)
    rec = dict(li["recipient"])
    if row["contact_id"] is not None:
        c = conn.execute("SELECT title, role_type FROM contacts WHERE id = ?", (row["contact_id"],)).fetchone()
        if c:
            rec.update(title=c["title"], role_type=c["role_type"])
    research = {}
    for f in conn.execute(
            "SELECT * FROM research_facts WHERE (subject_kind = 'person' AND subject_id IS ?) OR "
            "(subject_kind = 'company' AND subject_id IS ?) OR (subject_kind = 'job' AND subject_id IS ?)",
            (row["contact_id"], row["company_id"], row["job_id"])):
        research[f["fact_uid"]] = {"text": f["text"], "snippet": f["snippet"], "source_type": f["source_type"],
                                   "source_url": f["source_url"], "published_at": f["published_at"],
                                   "retrieved_at": f["retrieved_at"]}
    facts = dict(profile_facts())
    text = drafts.send_text(conn, row["id"])
    subject_line = ""
    body = text
    if row["kind"] in drafts.EMAIL_KINDS and text.startswith("Subject: "):
        subject_line, _, body = text.partition("\n\n")
    if row["kind"] == "resume":
        base = (p.get("payload") or {}).get("base") or {}
        from .lint import _base_bullets
        for bid, btext in _base_bullets(base).items():
            facts["base:" + bid] = btext
        facts["base:skills"] = ", ".join(str(s) for s in base.get("skills") or [])
    if row["kind"] == "application_package":
        bank = drafts.answer_bank()
        for k, a in bank.items():
            if a.get("source") in ("resume", "profile", "user_confirmed") and str(a.get("value") or "").strip():
                facts["answer:" + k] = str(a.get("value"))
        form = drafts.field(conn, row, "form")
        body = "\n".join("Field: %s\nValue: %s" % (f["label"], f["value"]) for f in form["fields"])
        att = p.get("attachment") or {}
        if att:
            body += "\nResume file: %s sha256 %s" % (att.get("filename"), att.get("sha256"))
    warns = conn.execute("SELECT warns_json FROM qc_results WHERE draft_id = ? AND stage IN ('lint','human_edit_lint') "
                         "AND text_sha256 = ? ORDER BY id DESC LIMIT 1", (row["id"], row["text_sha256"])).fetchone()
    try:
        warn_list = json.loads(warns[0]) if warns else []
    except ValueError:
        warn_list = []
    return {"nonce": nonce, "draft_sha256": row["text_sha256"], "today": today(), "channel": row["channel"],
            "recipient_json": _j(rec),
            "research_facts_json": _j(research), "profile_facts_json": _j(facts), "lint_warnings_json": _j(warn_list),
            "subject_line_if_any": subject_line, "body": body}


def packet_dir() -> str:
    return os.path.join(paths.state_dir(), "qc", "packets")


def write_packet(qjob_uid: str, text: str) -> str:
    d = packet_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "%s.txt" % qjob_uid)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def remove_packet(path: str | None) -> None:
    if path and os.path.abspath(path).startswith(os.path.abspath(packet_dir()) + os.sep):
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------- queue
def enqueue(conn, row, try_no: int = 1) -> dict:
    """Queue one review of the draft's current text. try_no 1 for a new review (finished rows of the same
    attempt are pruned so the (draft, attempt, try_no) slots are free), 2 for the retry after an error."""
    check_integrity(conn)
    if try_no == 1:
        if row["status"] == "review_failed" and (row["status_reason"] or "") == "reviewer_unavailable":
            jobstate.set_draft_status(conn, row["id"], "drafted", "review retry", "system:qc")
            row = drafts.get_by_id(conn, row["id"])
        if row["status"] != "drafted":
            raise Denied("E_PRECONDITION", "a review starts from a drafted text (status %s)" % row["status"])
        if not drafts.lint_passed_for_current_text(conn, row):
            raise Denied("E_QC_LINT_FAILED", "the current text has not passed lint; run draft revise first")
        for old in conn.execute("SELECT packet_path FROM qc_jobs WHERE draft_id = ? AND attempt = ?",
                                (row["id"], row["attempt"])).fetchall():
            remove_packet(old[0])
        conn.execute("DELETE FROM qc_jobs WHERE draft_id = ? AND attempt = ? AND state IN ('done','failed','timeout')",
                     (row["id"], row["attempt"]))
        if conn.execute("SELECT 1 FROM qc_jobs WHERE draft_id = ? AND attempt = ?", (row["id"], row["attempt"])) \
                .fetchone():
            raise Denied("E_CLAIMED", "a review of this draft is already queued or running")
    nonce = secrets.token_hex(8)
    uid = canon.new_uid("Q")
    path = write_packet(uid, render_packet(packet_values(conn, row, nonce)))
    stamp = now()
    conn.execute("INSERT INTO qc_jobs (qjob_uid, draft_id, attempt, try_no, nonce, packet_path, session_key, state, "
                 "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                 (uid, row["id"], row["attempt"], try_no, nonce, path, "review-" + nonce, stamp, stamp))
    if row["status"] == "drafted":
        jobstate.set_draft_status(conn, row["id"], "review_pending", "review queued", "system:qc")
    log_event(conn, "qc_review_queued", draft_uid=row["draft_uid"], qjob_uid=uid, attempt=row["attempt"],
              try_no=try_no)
    return {"qjob_uid": uid, "state": "queued", "draft_uid": row["draft_uid"], "attempt": row["attempt"]}


def start(conn, draft_uid: str) -> dict:
    """`qc review start --draft <uid>`: {qjob_uid, state}. Idempotent while a review is queued or running."""
    row = drafts.get(conn, draft_uid)
    st = row["status"]
    if st == "review_pending":
        j = conn.execute("SELECT qjob_uid, state FROM qc_jobs WHERE draft_id = ? AND attempt = ? ORDER BY id DESC "
                         "LIMIT 1", (row["id"], row["attempt"])).fetchone()
        if j is not None and j["state"] in ("queued", "running"):
            return {"qjob_uid": j["qjob_uid"], "state": j["state"], "draft_uid": row["draft_uid"],
                    "attempt": row["attempt"], "reused": True}
        raise Denied("E_INTERNAL", "draft is review_pending without a live review job")
    if st in ("drafted",) or (st == "review_failed" and (row["status_reason"] or "") == "reviewer_unavailable"):
        return enqueue(conn, row)
    if st == "lint_failed":
        raise Denied("E_QC_LINT_FAILED", "lint failed; run draft revise with a fixed text first")
    if st == "review_failed":
        raise Denied("E_QC_REVIEW_FAILED", "the review failed; run draft revise first")
    if st == "dropped_qc":
        raise Denied("E_QC_BUDGET_EXHAUSTED", "the draft was dropped by QC")
    if st == "expired":
        raise Denied("E_DRAFT_EXPIRED", "the draft expired")
    j = conn.execute("SELECT qjob_uid FROM qc_jobs WHERE draft_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    return {"qjob_uid": j[0] if j else None, "state": "done", "draft_uid": row["draft_uid"], "attempt": row["attempt"],
            "draft_status": st, "reused": True}


# ---------------------------------------------------------------- wait
def job_status(conn, qjob_uid: str) -> dict:
    job = conn.execute("SELECT * FROM qc_jobs WHERE qjob_uid = ?", (qjob_uid,)).fetchone()
    if job is None:
        raise Denied("E_NOT_FOUND", "unknown QC job %s" % qjob_uid)
    latest = conn.execute("SELECT * FROM qc_jobs WHERE draft_id = ? AND attempt = ? ORDER BY id DESC LIMIT 1",
                          (job["draft_id"], job["attempt"])).fetchone()
    row = drafts.get_by_id(conn, job["draft_id"])
    out = {"qjob_uid": latest["qjob_uid"], "state": latest["state"], "try_no": latest["try_no"],
           "draft_uid": row["draft_uid"], "draft_status": row["status"], "attempt": latest["attempt"],
           "attempts_left": max(0, drafts.max_attempts() - row["attempt"])}
    if latest["state"] == "done":
        rv = conn.execute("SELECT * FROM qc_results WHERE draft_id = ? AND attempt = ? AND stage = 'review' "
                          "ORDER BY id DESC LIMIT 1", (row["id"], latest["attempt"])).fetchone()
        data = {}
        if rv is not None:
            try:
                data = json.loads(rv["review_json"] or "{}")
            except ValueError:
                data = {}
        out.update(verdict="pass" if (rv is not None and rv["passed"]) else "fail",
                   code_verdict=rv["code_verdict"] if rv is not None else None,
                   model_verdict=rv["model_verdict"] if rv is not None else None,
                   weighted_score=rv["weighted_score"] if rv is not None else None,
                   gates=data.get("gates"), gates_failed=json.loads(rv["gates_failed"] or "[]") if rv is not None
                   else [], issues=data.get("issues") or ([{"severity": "blocker", "quote": "",
                                                              "problem": data["parse_error"], "fix": "rewrite"}]
                                                            if data.get("parse_error") else []),
                   rewrite_brief=data.get("rewrite_brief") or "")
    elif latest["state"] in ("failed", "timeout"):
        out.update(error=latest["error"])
    return out


def wait(conn, qjob_uid: str, max_s: int = 45, poll_s: float = 2.0) -> dict:
    """Poll every poll_s seconds for at most max_s (capped at 45); returns the state or the verdict."""
    max_s = max(0, min(int(max_s), 45))
    deadline = time.monotonic() + max_s
    while True:
        st = job_status(conn, qjob_uid)
        left = deadline - time.monotonic()
        if st["state"] not in ("queued", "running") or left <= 0:
            return st
        time.sleep(min(poll_s, left))
