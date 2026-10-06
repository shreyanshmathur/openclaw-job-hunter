"""Fake U1 core commands for the install tests (owned by U7; design 11 rule 4).

Copied into the temporary repo copy as scripts/jobhunter/commands/zz_u7_fake.py. It registers only the
commands that are still missing when it runs (it sorts last), so once U1's real `init`, `selftest`, `pause`,
`unpause` and `status` land, the install test exercises the real ones.

`init` follows the contract install.sh relies on: it writes private/home.json from the environment variables
JH_OC_BIN, JH_OC_PROFILE and JH_PYTHON (ws_root under ~/.openclaw-job-hunter/<install_id>/workspaces),
creates private/guard.key and initialises the database.

`selftest --probe-since <epoch>` plays U1's agent identity check on the probe files the fake openclaw writes
(state/probe/<agent>.json with both carriers, WS/<role>/work/probe/ok.txt holding OK). `qc smoke` plays U3's
smoke turn. With FAKE_U7_OCRUN=1 (set by tests/test_install_sh.py) or when U1 has not landed them,
jobhunter.ocrun.preflight, effective_exec, agents_list and qc_turn are replaced by fakes that read the fake
openclaw's JSON, so the install test never depends on another unit's output parser.
"""
from __future__ import annotations

import json
import os
import re
import secrets

from jobhunter import db, install as ins, ocrun, paths
from jobhunter.canon import new_uid, now
from jobhunter.commands import add_command
from jobhunter.errors import Denied


def _missing(subparsers, path: str) -> bool:
    sp = subparsers
    words = path.split()
    for w in words[:-1]:
        parser = sp.choices.get(w)
        if parser is None:
            return True
        sp = getattr(parser, "_jh_subparsers", None)
        if sp is None:
            return False
    return words[-1] not in sp.choices


# ---------------------------------------------------------------- fake ocrun read-backs (U1 owns the real ones)
def fake_preflight(key: str) -> str:
    st = ins.guard_status(proof_version=2)
    if not st["fresh"]:
        raise Denied("E_GUARD_MISSING", "no verified agent identity: guard heartbeat %s" % st["reason"])
    spec = (ins.read_manifest().get("cron_specs") or {}).get(key)
    jid = ins.known_job_ids().get(key)
    if spec is None or not jid:
        raise Denied("E_NOT_FOUND", "the automation %s is not installed; run ./install.sh again" % key)
    lst = ocrun.cron_list()
    if not lst["ok"]:
        raise Denied("E_PRECONDITION", "openclaw cron list failed")
    row = ins.listed_jobs(lst["doc"]).get(key)
    if row is None or str(row.get("id")) != jid:
        raise Denied("E_NOT_FOUND", "the automation %s is not installed; run ./install.sh again" % key)
    drift = ins.job_drift(spec, row)
    if drift:
        raise Denied("E_CRON_DRIFT", "%s differs from the install manifest: %s" % (key, ", ".join(drift)))
    return jid


def _oc_json(*args):
    r = ocrun.run(ocrun.oc_argv(*args), 60)
    return ocrun.last_json(r["stdout"]) if r["ok"] else None


def fake_effective_exec(agent: str) -> dict:
    doc = _oc_json("sandbox", "explain", "--agent", agent, "--json") or {}
    pol = _oc_json("exec-policy", "show", "--json") or {}
    tools = doc.get("tools") or {}
    ex = tools.get("exec") or {}
    appr = ((pol.get("approvals") or {}).get("agents") or {}).get(agent) or {}
    return {"security": ex.get("security"), "ask": ex.get("ask"), "mode": ex.get("mode"),
            "elevated": (tools.get("elevated") or {}).get("enabled"), "approvals_ask": appr.get("ask"),
            "approvals_fallback": appr.get("askFallback")}


def fake_agents_list() -> list:
    doc = _oc_json("config", "get", "agents.entries", "--json")
    return sorted(doc) if isinstance(doc, dict) else []


def fake_qc_turn(session_key, message_file, timeout_s):
    """A QC turn that answers a review packet (U3's `qc smoke` sends one, D14) the way the reviewer does: a
    valid verdict JSON for its nonce and draft sha256, longer than the 2000 characters of OpenClaw 2026.9.8's run
    record (D13); any other message gets OK."""
    try:
        with open(message_file, encoding="utf-8") as fh:
            msg = fh.read()
    except (OSError, TypeError):
        msg = ""
    nonce = re.search(r"<nonce>([0-9a-f]+)</nonce>", msg)
    sha = re.search(r"<draft_sha256>([0-9a-f]+)</draft_sha256>", msg)
    if not (nonce and sha):
        return {"ok": True, "text": "OK", "raw": ""}
    issue = {"severity": "major", "quote": "I cut fuel costs by 40% in one quarter",
             "problem": "the profile fact says about 8% over a year, so the draft inflates the number. " * 2,
             "fix": "drop the claim or state what fact P1 says, in its own numbers. " * 2}
    verdict = {"nonce": nonce.group(1), "draft_sha256": sha.group(1), "verdict": "fail",
               "gates": {"truthful": False, "hook_verified": False, "swap_test": True, "no_ai_voice": False,
                         "safe": False},
               "scores": {"specificity": 2, "value": 2, "human_voice": 1, "clarity": 3, "cta": 1, "tone_fit": 2,
                          "channel_fit": 3},
               "weighted_score": 1.85,
               "claims": [{"quote": "I led a team of 12 analysts", "fact_id": None, "supported": False,
                           "note": "no fact names a team"}],
               "hook": {"quote": "cut empty miles by 30% in 2025", "fact_id": "R1", "accurate": False,
                        "note": "the post says about 12%"},
               "ai_tells": [{"quote": "I hope this finds you well", "pattern": "stock opener"}],
               "issues": [dict(issue) for _ in range(10)], "rewrite_brief": "one hook, one true result",
               "confidence": 0.9}
    return {"ok": True, "text": json.dumps(verdict, indent=1), "raw": ""}


for _name, _fn in (("preflight", fake_preflight), ("effective_exec", fake_effective_exec),
                   ("agents_list", fake_agents_list), ("qc_turn", fake_qc_turn)):
    if os.environ.get("FAKE_U7_OCRUN") == "1" or not hasattr(ocrun, _name):
        setattr(ocrun, _name, _fn)


def register(subparsers):
    if _missing(subparsers, "init"):
        p = add_command(subparsers, "init", cmd_init, callers="SH", help="(fake) create local state")
        p.add_argument("--force-examples", action="store_true")
    if _missing(subparsers, "selftest"):
        p = add_command(subparsers, "selftest", cmd_selftest, callers="SH", help="(fake) selftest")
        p.add_argument("--offline", action="store_true")
        p.add_argument("--probe-since", type=int)
        p.add_argument("--only")
    if _missing(subparsers, "qc smoke"):
        add_command(subparsers, "qc smoke", cmd_qc_smoke, callers="SH", help="(fake) QC smoke turn")
    if _missing(subparsers, "pause"):
        p = add_command(subparsers, "pause", cmd_pause, callers="SHC", help="(fake) pause")
        p.add_argument("--scope", default="all")
        p.add_argument("--reason")
    if _missing(subparsers, "unpause"):
        p = add_command(subparsers, "unpause", cmd_ok, callers="H", help="(fake) unpause")
        p.add_argument("--scope", default="all")
    if _missing(subparsers, "status"):
        add_command(subparsers, "status", cmd_ok, callers="SHCR", help="(fake) status")


def cmd_ok(args, ctx):
    return {"fake": True}


def cmd_selftest(args, ctx):
    if args.probe_since is None:
        return {"fake": True}
    h = paths.home()
    bad = []
    for role in ins.PROBE_ROLES:
        aid = "jobhunter-" + role
        f = os.path.join(paths.state_dir(), "probe", aid + ".json")
        try:
            with open(f) as fh:
                rec = json.load(fh)
            fresh = os.stat(f).st_mtime >= args.probe_since
        except (OSError, ValueError):
            bad.append("%s: no probe file" % aid)
            continue
        okf = os.path.join(h["ws_root"], role, "work", "probe", "ok.txt")
        ok_text = open(okf).read() if os.path.exists(okf) else ""
        if not fresh or rec.get("carriers") != ["argv", "env"] or ok_text != "OK":
            bad.append("%s: identity not proven (carriers %s)" % (aid, rec.get("carriers")))
    if bad:
        raise Denied("E_PRECONDITION", "agent identity: " + "; ".join(bad))
    return {"fake": True, "agent_identity": "ok"}


def cmd_qc_smoke(args, ctx):
    r = ocrun.qc_turn("smoke", None, 120)
    if not r.get("ok") or (r.get("text") or "").strip() != "OK":
        raise Denied("E_PRECONDITION", "the jobhunter-qc test turn did not answer OK")
    return {"reply": "OK"}


def cmd_pause(args, ctx):
    os.makedirs(paths.state_dir(), exist_ok=True)
    with open(paths.paused_file(), "w", encoding="utf-8") as fh:
        fh.write(now() + "\n")
    return {"paused": args.scope}


def cmd_init(args, ctx):
    created = []
    for d in (paths.private_dir(), paths.state_dir(), paths.logs_dir(), paths.exports_dir()):
        if not os.path.isdir(d):
            os.makedirs(d, mode=0o700)
            created.append(d)
    hf = paths.home_file()
    if not os.path.exists(hf):
        iid = new_uid("I", 8)
        home = os.path.expanduser("~")
        data = {"install_id": iid, "repo": paths.root(), "db_path": paths.db_path(),
                "ws_root": os.path.join(home, ".openclaw-job-hunter", iid, "workspaces"),
                "oc_bin": ctx.env.get("JH_OC_BIN", ""), "oc_profile": ctx.env.get("JH_OC_PROFILE", ""),
                "python": ctx.env.get("JH_PYTHON", ""), "created_at": now()}
        with open(hf, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.chmod(hf, 0o600)
        created.append(hf)
    key = os.path.join(paths.private_dir(), "guard.key")
    if not os.path.exists(key):
        with open(key, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32))
        os.chmod(key, 0o600)
    h = paths.home()
    os.makedirs(h["ws_root"], mode=0o700, exist_ok=True)
    conn = db.init_db()
    conn.close()
    return {"created": created, "install_id": h["install_id"], "ws_root": h["ws_root"]}
