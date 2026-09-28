"""QC gate package (design section 5): deterministic linter, pre-send check, independent reviewer.

Modules:
- lint.py     pure linter (writing-qc 7 and 8, design 5.2) plus build_ctx(conn, draft_id)
- presend.py  hook called by gate.reserve inside its transaction (re-lint, hash check)
- review.py   reviewer packets, strict verdict parsing, the code pass rule, start and wait
- worker.py   runs the jobhunter-qc agent turn detached from the writer, drains queued jobs

Shared here: repo data paths, the effective settings the QC code needs (with a fail-closed fallback while
jobhunter.config is not available), and two provider hooks that tests replace with fakes
(tests/fakes/u3): PROFILE_FACTS (U4 profile.facts) and AGENT_TURN (U1 ocrun.agent_turn).
"""
from __future__ import annotations

import copy
import json
import os

from .. import paths
from ..errors import Denied

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
AGENT_TURN = None        # (agent, session_key, message_file, timeout_s) -> {"ok", "text", "raw"}
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


def agent_turn(agent: str, session_key: str, message_file: str, timeout_s: int) -> dict:
    """One reviewer turn through U1 ocrun.agent_turn: {ok, text, raw}. Never a verdict by itself."""
    if AGENT_TURN is not None:
        return AGENT_TURN(agent, session_key, message_file, timeout_s)
    try:
        from .. import ocrun as _ocrun   # U1
        return _ocrun.agent_turn(agent, session_key, message_file, timeout_s)
    except (ImportError, AttributeError, NotImplementedError) as exc:
        raise Denied("E_OPENCLAW_CALL", "the reviewer cannot be called: %s" % exc)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
