"""QC gate package (design section 5): deterministic linter, pre-send check, independent reviewer.

Modules:
- lint.py     pure linter (writing-qc 7 and 8, design 5.2) plus build_ctx(conn, draft_id)
- presend.py  hook called by gate.reserve inside its transaction (re-lint, hash check)
- review.py   reviewer packets, strict verdict parsing, the code pass rule, start and wait
- worker.py   runs the jobhunter-qc reviewer turn detached from the writer, drains queued jobs

Shared here: repo data paths, the effective settings the QC code needs (with a fail-closed fallback while
jobhunter.config is not available), the reviewer turn (agent_turn: one restricted one-shot cron run through
U1 ocrun.qc_turn, CLI-ROUTE-DESIGN 6.3.2, with the verdict-file fallback F-QC) and two provider hooks that tests
replace with fakes (tests/fakes/u3): PROFILE_FACTS (U4 profile.facts) and AGENT_TURN (the whole agent_turn).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import stat

from .. import paths
from ..errors import Denied
# the run record's size cap and its cut check (U1 ocrun, D13); re-exported for the QC code and its tests
from ..ocrun import CUT_MARK, RUN_TEXT_CAP, RUN_TEXT_CUT_SLACK, js_len  # noqa: F401
from ..ocrun import summary_cut as reply_cut

QC_DATA_DIR = os.path.join(paths.REPO, "qc")
PROMPTS_DIR = os.path.join(paths.REPO, "prompts")
BANNED_FILE = os.path.join(QC_DATA_DIR, "banned_phrases.json")
SCHEMA_REVIEW_FILE = os.path.join(QC_DATA_DIR, "schema_review.json")
GOLDEN_DIR = os.path.join(QC_DATA_DIR, "golden")
REVIEWER_PROMPT_FILE = os.path.join(PROMPTS_DIR, "reviewer.md")
REWRITE_PROMPT_FILE = os.path.join(PROMPTS_DIR, "rewrite.md")
WRITER_BRIEF_FILE = os.path.join(PROMPTS_DIR, "writer_brief.md")
TONE_RULES_FILE = os.path.join(PROMPTS_DIR, "tone_rules.json")

# Test hooks (never set in production code): callables that replace the other units' functions.
PROFILE_FACTS = None     # () -> {"P1": "text", ...}
AGENT_TURN = None        # (agent, session_key, message_file, timeout_s) -> {"ok", "text", "raw"}; replaces agent_turn
SPAWN = None             # (qjob_uid) -> None; replaces the detached worker spawn

# The keys of the effective config the QC code reads (config.example.json values, 4.2).
DEFAULTS: dict = {
    "owner": {"first_name": "", "last_name": "", "linkedin_profile_url": "",
              "signature": {"full_name": "", "phone": "", "links": []}},
    "approval": {"mode": "inherit", "per_channel": {"linkedin": "human"}, "approval_ttl_hours": 72,
                 "always_human": ["positive_or_ambiguous_reply", "mentions_salary", "referral_ask",
                                  "sensitive_form_field"]},
    "gmail": {"route": "app_password", "opt_out_line": False},
    "outreach": {"target_skip_days": 30,
                 "research": {"hook_max_age_days": 180, "hook_preferred_age_days": 90, "facts_max_age_days": 14}},
    "qc": {"max_rewrites": 2, "max_human_edits": 3,
           "lint": {"max_soft_hits": 2, "max_warnings": 2, "li_connect_hard": 200,
                    "number_whitelist": ["10", "15", "20", "30"], "allowed_link_hosts": []},
           "review": {"agent": "jobhunter-qc", "model": "anthropic/claude-sonnet-5", "timeout_s": 240,
                      "max_tries": 2, "min_weighted": 4.0, "min_core": 4, "min_any": 3},
           "golden_min_agreement": 18},
    "resume": {"filename": "{first}_{last}_Resume", "max_pages": 2},
}


def _merge(base: dict, over) -> dict:
    out = copy.deepcopy(base)
    if not isinstance(over, dict):
        return out
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _num(v, default):
    try:
        return type(default)(v)
    except (TypeError, ValueError):
        return default


def _clamp(cfg: dict) -> dict:
    """Limits can only get stricter than the researched defaults (4.1). Used only on the fallback path;
    jobhunter.config.load() already returns effective values."""
    d = DEFAULTS
    q, dq = cfg["qc"], d["qc"]
    q["max_rewrites"] = max(0, min(_num(q.get("max_rewrites"), 2), dq["max_rewrites"]))
    q["max_human_edits"] = max(0, min(_num(q.get("max_human_edits"), 3), dq["max_human_edits"]))
    lint, dl = q["lint"], dq["lint"]
    for k in ("max_soft_hits", "max_warnings", "li_connect_hard"):
        lint[k] = max(0, min(_num(lint.get(k), dl[k]), dl[k]))
    lint["number_whitelist"] = [str(x) for x in (lint.get("number_whitelist") or []) if str(x) in dl["number_whitelist"]]
    rv, dr = q["review"], dq["review"]
    rv["agent"] = dr["agent"]
    rv["timeout_s"] = max(60, min(_num(rv.get("timeout_s"), 240), 300))
    rv["max_tries"] = max(1, min(_num(rv.get("max_tries"), 2), 2))
    rv["min_weighted"] = max(_num(rv.get("min_weighted"), 4.0), dr["min_weighted"])
    rv["min_core"] = max(_num(rv.get("min_core"), 4), dr["min_core"])
    rv["min_any"] = max(_num(rv.get("min_any"), 3), dr["min_any"])
    q["golden_min_agreement"] = max(_num(q.get("golden_min_agreement"), 18), 18)
    ap = cfg["approval"]
    if ap.get("mode") not in ("inherit", "human"):
        ap["mode"] = "human"
    ap["approval_ttl_hours"] = max(12, _num(ap.get("approval_ttl_hours"), 72))
    ah = list(ap.get("always_human") or [])
    for item in d["approval"]["always_human"]:
        if item not in ah:
            ah.append(item)
    ap["always_human"] = ah
    cfg["outreach"]["target_skip_days"] = max(30, _num(cfg["outreach"].get("target_skip_days"), 30))
    rs = cfg["outreach"]["research"]
    rs["facts_max_age_days"] = max(1, min(_num(rs.get("facts_max_age_days"), 14), 14))
    rs["hook_max_age_days"] = max(1, min(_num(rs.get("hook_max_age_days"), 180), 180))
    rs["hook_preferred_age_days"] = max(1, min(_num(rs.get("hook_preferred_age_days"), 90), 90))
    return cfg


def settings(conn=None) -> dict:
    """Effective settings for QC, drafts and approvals.

    Uses jobhunter.config.load(conn) (U1) when it is available; otherwise the defaults above merged with
    private/config.json and clamped so that the file can only tighten (fail closed)."""
    loaded = None
    try:
        from .. import config as _config   # U1; may not exist yet
        loaded = _config.load(conn) if conn is not None else _config.load()
    except (ImportError, AttributeError, NotImplementedError, Denied):
        loaded = None   # the fallback below uses the researched defaults, which are the strictest values
    if isinstance(loaded, dict):
        return _merge(DEFAULTS, loaded)
    raw = {}
    path = os.path.join(paths.private_dir(), "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    return _clamp(_merge(DEFAULTS, raw if isinstance(raw, dict) else {}))


def profile_facts() -> dict:
    """{"P1": "text"} of confirmed profile facts (U4 profile.facts), with a read of private/profile.json as
    the fallback while U4 has not landed."""
    if PROFILE_FACTS is not None:
        return dict(PROFILE_FACTS())
    try:
        from .. import profile as _profile   # U4
        facts = _profile.facts()
        if isinstance(facts, dict):
            return {str(k): (v.get("text", "") if isinstance(v, dict) else str(v)) for k, v in facts.items()}
    except (ImportError, AttributeError, NotImplementedError):
        pass
    path = os.path.join(paths.private_dir(), "profile.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, dict):
        return {}
    return {str(k): (v.get("text", "") if isinstance(v, dict) else str(v)) for k, v in facts.items()}


# ---------------------------------------------------------------- reviewer turn (CLI-ROUTE-DESIGN 6.3.2)
QC_AGENT = "jobhunter-qc"
QC_REPLY_MODES = ("run", "file")
VERDICT_MAX_BYTES = 200 * 1024
_VERDICT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def qc_reply_mode(home: dict | None = None) -> str:
    """Where the reviewer's reply is read: "run" (the final text of the one-shot cron run, the default) or
    "file" (fallback F-QC: the reviewer writes it to <WS_ROOT>/qc/work/verdict/<id>.json). The switch is
    private/home.json cli_route.qc_reply, set by `qc smoke` (install step 14); any other value counts as "run"."""
    if home is None:
        try:
            home = paths.home()
        except Denied:
            return "run"
    route = home.get("cli_route") if isinstance(home, dict) else None
    mode = route.get("qc_reply") if isinstance(route, dict) else None
    return "file" if mode == "file" else "run"


def verdict_dir() -> str:
    """<WS_ROOT>/qc/work/verdict: the only place the reviewer may write in F-QC mode (guard R3, qcVerdictFile)."""
    return os.path.join(paths.ws_dir("qc"), "work", "verdict")


def new_verdict_id(session_key: str) -> str:
    """12 hex derived from the session key plus 4 random hex (the shape of the one-shot job uid, 6.3.2)."""
    return hashlib.sha256(str(session_key).encode("utf-8")).hexdigest()[:12] + secrets.token_hex(2)


def verdict_path(verdict_id: str) -> str:
    if not _VERDICT_ID_RE.match(verdict_id or ""):
        raise Denied("E_VALIDATION", "bad verdict file id")
    return os.path.join(verdict_dir(), verdict_id + ".json")


def verdict_file_line(path: str) -> str:
    """The line appended, after every data tag, to the message of an F-QC turn (never in run mode)."""
    return ("Final step: write your complete answer (for a review packet, the JSON object), and nothing else, to "
            "the file %s with the write tool, as one whole file. Write no other file. Then reply with the single "
            "word DONE." % path)


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def read_verdict_file(path: str) -> str | None:
    """Text of the reviewer's verdict file, or None when it does not exist. Only a regular file (never a
    symlink) directly inside verdict_dir() and at most VERDICT_MAX_BYTES long is read; anything else is
    Denied(E_VALIDATION), which the caller reports as a reviewer error (not a verdict)."""
    base = os.path.realpath(verdict_dir())
    if os.path.realpath(os.path.dirname(path)) != base or \
            not base.startswith(os.path.realpath(paths.ws_dir("qc")) + os.sep):
        raise Denied("E_VALIDATION", "the verdict file is outside %s" % verdict_dir())
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Denied("E_VALIDATION", "the verdict file cannot be opened (%s)" % type(exc).__name__)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Denied("E_VALIDATION", "the verdict file is not a regular file")
        if st.st_size > VERDICT_MAX_BYTES:
            raise Denied("E_VALIDATION", "the verdict file is larger than %d bytes" % VERDICT_MAX_BYTES)
        chunks, size = [], 0
        while size <= VERDICT_MAX_BYTES:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
            size += len(b)
        if size > VERDICT_MAX_BYTES:
            raise Denied("E_VALIDATION", "the verdict file is larger than %d bytes" % VERDICT_MAX_BYTES)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", "replace")


def _bad_result(res) -> dict | None:
    if isinstance(res, dict):
        return None
    return {"ok": False, "text": None, "raw": "", "error": "bad qc_turn result"}


# OpenClaw 2026.9.8 keeps only the first 2000 characters (JavaScript string length) of a cron run's reply in the
# run record and appends U+2026 (D13, V13). A reviewer verdict is usually longer, so a text cut like that is
# unreadable: never parsed, never a verdict. U1 ocrun owns the cap and the check (imported at the top as
# RUN_TEXT_CAP and reply_cut); ocrun.qc_turn already reports such a reply as cut, _run_turn checks it again.
def _run_turn(qc_turn, session_key: str, message_file: str, timeout_s: int) -> dict:
    """Run mode: the reply is the run's final text. A finished run without readable text, or with a text the
    run record cut at its size cap (reply_cut, D13), is an error (empty True, plus cut True), never a verdict, so
    the worker retries instead of burning a rewrite attempt."""
    res = qc_turn(session_key, message_file, timeout_s)
    bad = _bad_result(res)
    if bad is not None:
        return dict(bad, reply="run")
    if res.get("cut") or (res.get("ok") and reply_cut(res.get("text"))):
        return dict(res, ok=False, text=None, empty=True, cut=True, reply="run",
                    error="the reviewer reply was cut at %d characters in the run record, so it cannot be read; "
                          "replies must come from the verdict file: run ./install.sh --smoke" % RUN_TEXT_CAP)
    if res.get("ok") and not str(res.get("text") or "").strip():
        return dict(res, ok=False, text=None, empty=True, reply="run",
                    error="the reviewer run finished but its reply text could not be read from the run record")
    return dict(res, reply="run")


def _file_turn(qc_turn, session_key: str, message_file: str, timeout_s: int) -> dict:
    """F-QC: the message gets one final line naming a fresh verdict file; the reply is that file's text."""
    from . import review
    vid = new_verdict_id(session_key)
    vpath = verdict_path(vid)
    os.makedirs(verdict_dir(), mode=0o700, exist_ok=True)
    _remove_quietly(vpath)                     # never read a file that was there before this turn
    try:
        with open(message_file, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise Denied("E_VALIDATION", "the reviewer message cannot be read: %s" % type(exc).__name__)
    msg_path = review.write_packet("fqc-" + vid, text.rstrip("\n") + "\n\n" + verdict_file_line(vpath) + "\n")
    got, read_error = None, None
    try:
        res = qc_turn(session_key, msg_path, timeout_s)
        try:
            got = read_verdict_file(vpath)
        except Denied as d:
            read_error = d.message
    finally:
        review.remove_packet(msg_path)
        _remove_quietly(vpath)
    bad = _bad_result(res)
    if bad is not None:
        res = bad
    raw = res.get("raw") or ""
    if read_error is not None:
        return {"ok": False, "text": None, "raw": raw, "reply": "file", "error": read_error}
    if got is not None and got.strip():
        return {"ok": True, "text": got, "raw": raw, "reply": "file"}
    if not res.get("ok"):
        return dict(res, text=None, reply="file")
    return {"ok": False, "text": None, "raw": raw, "reply": "file", "empty": True,
            "error": "the reviewer run finished but wrote no verdict file"}


def agent_turn(agent: str, session_key: str, message_file: str, timeout_s: int) -> dict:
    """One reviewer turn through U1 ocrun.qc_turn (a one-shot cron job with no tools, CLI-ROUTE-DESIGN 6.3.2):
    {ok, text, raw} plus `reply` ("run" or "file") and `empty` (the run finished but no reply could be read).
    Never a verdict by itself. Only jobhunter-qc runs reviews; in F-QC mode (cli_route.qc_reply "file") the
    message gets the verdict-file line and the text is read from <WS_ROOT>/qc/work/verdict/<id>.json."""
    if agent != QC_AGENT:
        raise Denied("E_USAGE", "reviews run only as %s, not %r" % (QC_AGENT, agent))
    if AGENT_TURN is not None:
        return AGENT_TURN(agent, session_key, message_file, timeout_s)
    try:
        from .. import ocrun as _ocrun   # U1
        qc_turn = _ocrun.qc_turn
    except (ImportError, AttributeError) as exc:
        raise Denied("E_OPENCLAW_CALL", "the reviewer cannot be called: %s" % exc)
    try:
        if qc_reply_mode() == "file":
            return _file_turn(qc_turn, session_key, message_file, int(timeout_s))
        return _run_turn(qc_turn, session_key, message_file, int(timeout_s))
    except NotImplementedError as exc:
        raise Denied("E_OPENCLAW_CALL", "the reviewer cannot be called: %s" % exc)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
