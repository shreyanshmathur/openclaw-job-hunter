"""Fake `openclaw` for U1 tests (ocrun cron runs, drift checks, QC one-shot runs, effective exec policy).

Run as `<python> fake_openclaw.py [--profile P] <command...>`. State lives in the directory named by
FAKE_OC_DIR: calls.jsonl (one JSON list of arguments per call), jobs.json (the cron store). Behaviour switches
(environment, comma separated in FAKE_OC_MODE):
- widen: `cron list` reports every agent job with toolsAllow ["*"]
- no_run_text: `cron run --wait` carries no reply text (it is only in `cron runs`)
- cut_summary: `cron run --wait` carries the reply cut at 2000 characters plus U+2026 (OpenClaw 2026.9.8)
- run_fail: `cron run` exits 1
- add_fail: `cron add` exits 1
- rm_fail: `cron rm` exits 1
Nothing here touches the network or a real OpenClaw install.
"""
from __future__ import annotations

import json
import os
import sys


def _state_dir() -> str:
    return os.environ["FAKE_OC_DIR"]


def _load_jobs() -> list:
    try:
        with open(os.path.join(_state_dir(), "jobs.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def _save_jobs(jobs: list) -> None:
    with open(os.path.join(_state_dir(), "jobs.json"), "w", encoding="utf-8") as fh:
        json.dump(jobs, fh)


def _opt(args: list, name: str):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _list(value) -> list:
    return [x for x in (value or "").replace(",", " ").split() if x]


def main(argv: list) -> int:
    modes = set(_list(os.environ.get("FAKE_OC_MODE", "")))
    with open(os.path.join(_state_dir(), "calls.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")
    args = list(argv)
    if args[:1] == ["--profile"]:
        args = args[2:]
    cmd = args[:2]
    if cmd == ["cron", "add"]:
        if "add_fail" in modes:
            sys.stderr.write("add failed\n")
            return 1
        jobs = _load_jobs()
        job_id = "job-%d" % (len(jobs) + 1)
        tools = _opt(args, "--tools")
        payload = {"kind": "agentTurn", "message": (_opt(args, "--message") or "").strip(),
                   "model": _opt(args, "--model"), "fallbacks": _list(_opt(args, "--fallbacks")),
                   "thinking": _opt(args, "--thinking"), "timeoutSeconds": int(_opt(args, "--timeout-seconds") or 0),
                   "toolsAllow": _list(tools) if tools is not None else None}
        row = {"id": job_id, "name": _opt(args, "--name"), "declarationKey": _opt(args, "--declaration-key"),
               "enabled": "--disabled" not in args, "agentId": _opt(args, "--agent"),
               "sessionTarget": _opt(args, "--session"), "payload": payload,
               "delivery": {"mode": "none" if "--no-deliver" in args else "announce"}}
        jobs.append(row)
        _save_jobs(jobs)
        print(json.dumps({"ok": True, "id": job_id, "declarationKey": row["declarationKey"]}))
        return 0
    if cmd == ["cron", "list"]:
        jobs = _load_jobs()
        if "widen" in modes:
            for j in jobs:
                if j["payload"].get("kind") == "agentTurn":
                    j["payload"]["toolsAllow"] = ["*"]
        print("note: listing\n" + json.dumps({"jobs": jobs}))
        return 0
    if cmd == ["cron", "run"]:
        if "run_fail" in modes:
            print(json.dumps({"ok": True, "completed": True, "status": "error", "completionStatus": "failed",
                              "run": {"status": "error", "error": "model refused"}}))
            return 1
        run = {"status": "ok", "completionStatus": "succeeded"}
        if "cut_summary" in modes:
            run["summary"] = '{"verdict": "pass", "note": "' + "x" * 1980 + "\u2026"
        elif "no_run_text" not in modes:
            run["summary"] = '{"verdict": "pass"}'
        print(json.dumps({"ok": True, "enqueued": True, "runId": "r1", "completed": True, "status": "ok",
                          "completionStatus": "succeeded", "run": run}))
        return 0
    if cmd == ["cron", "runs"]:
        print(json.dumps({"entries": [{"status": "ok", "summary": '{"verdict": "pass", "from": "runs"}'}]}))
        return 0
    if cmd == ["cron", "rm"]:
        if "rm_fail" in modes:
            sys.stderr.write("rm failed\n")
            return 1
        _save_jobs([j for j in _load_jobs() if j["id"] != args[2]])
        print(json.dumps({"ok": True, "removed": True}))
        return 0
    if cmd == ["exec-policy", "show"]:
        with open(os.path.join(_state_dir(), "exec-policy.json"), "r", encoding="utf-8") as fh:
            print(fh.read())
        return 0
    if cmd == ["sandbox", "explain"]:
        agent = _opt(args, "--agent")
        enabled = agent in _list(os.environ.get("FAKE_OC_ELEVATED", ""))
        print(json.dumps({"agentId": agent, "elevated": {"enabled": enabled, "failures": []}}))
        return 0
    if cmd == ["config", "get"]:
        print(json.dumps({"main": {}, "jobhunter-scout": {}, "helper": {}}))
        return 0
    if cmd == ["plugins", "list"]:
        print(json.dumps({"plugins": [{"id": "jobhunter-guard", "enabled": True},
                                      {"id": "approval-bot", "enabled": True},
                                      {"id": "off-plugin", "enabled": False}]}))
        return 0
    sys.stderr.write("fake openclaw: unknown command %r\n" % args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
