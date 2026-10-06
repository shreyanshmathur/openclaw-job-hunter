#!/usr/bin/env python3
"""Release gate of the Claude subscription route (claude-cli): the live checks T0 to T13 (INT).

CLI-ROUTE-DESIGN 12, 13 and 15. The claude-cli route may be released only when the live checks of section 13 have
passed on an isolated `--profile jhtest` install of exactly the code being released:

  - OpenClaw 2026.9.8: T0 to T13 pass. T10 is optional (not run is fine, a failure is not). T6b records the
    "pin on" result, which must pass.
  - A test with a guard-off (or pin-off) variant passes only when every one of its parts ran and passed: T3, T4,
    T5, T6, T10 and T13 have a guard-on and a guard-off part (T5 also its offline part); T6b's pin-off part is
    the known gap of V7 and is recorded only. A test whose guard-on part passed while another part was not run is
    "partial", which does not meet the gate, and the verdict names the parts still to run.
  - OpenClaw 2026.9.5: T0 to T7 (T6b and T7b included) and T11 pass.
  - Every verification item V1 to V18 is proven, or failed/open with its own fallback applied. V1, V4, V5, V10 and
    V12 have no fallback; V1 not proven is the V1 release rule (the claude-cli route is blocked); V2 and V3 may
    not both fall back.
  - The before/after comparison of 13.2 showed the owner's own OpenClaw state, LaunchAgents and Claude Code
    settings unchanged.
  - The record's fingerprint equals the fingerprint of the files the live checks exercise (agents, guard,
    installer, identity), so a pass never carries over to changed code.

The record is tests/fixtures/e2e/live_gate.json. The operator fills it in after a live run and writes the
fingerprint that `--fingerprint` printed for the tree that was tested. Fictional data only; no paths of a real
machine go into the record.

    python3 tests/fixtures/e2e/live_gate.py                 # verdict for the committed record
    python3 tests/fixtures/e2e/live_gate.py --json          # the same as JSON
    python3 tests/fixtures/e2e/live_gate.py --fingerprint   # fingerprint of the current tree
    python3 tests/fixtures/e2e/live_gate.py --record FILE   # another record

Exit 0 when the gate is met, 1 when it is not, 2 when the record is malformed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RECORD = os.path.join(REPO, "tests", "fixtures", "e2e", "live_gate.json")
RECORD_VERSION = 2

V98 = "2026.9.8"
V95 = "2026.9.5"
TESTS = ("T0", "T1", "T2", "T3", "T4", "T5", "T6", "T6b", "T7", "T7b", "T8", "T9", "T10", "T11", "T12", "T13")
OPTIONAL = {V98: ("T10",), V95: ()}
REQUIRED = {
    V98: tuple(t for t in TESTS if t not in OPTIONAL[V98]),
    V95: ("T0", "T1", "T2", "T3", "T4", "T5", "T6", "T6b", "T7", "T7b", "T11"),
}
TEST_STATUSES = ("pass", "fail", "partial", "not_run")
PART_STATUSES = ("pass", "fail", "not_run")
# section 13.3: the variants of a test, each recorded on its own. A disabled guard (or pin) cannot be inferred
# from a guard-on run, so a test passes only when each of its required parts ran and passed.
PARTS = {
    "T3": ("guard_on", "guard_off"),
    "T4": ("guard_on", "guard_off"),
    "T5": ("guard_on", "guard_off", "offline"),
    "T6": ("pin_on", "pin_off"),
    "T6b": ("pin_on", "pin_off"),
    "T10": ("guard_on", "guard_off"),
    "T13": ("guard_on", "guard_off"),
}
RECORD_ONLY_PARTS = {("T6b", "pin_off")}     # the known gap of V7: recorded, never required
V_STATUSES = ("proven", "failed", "open")
# section 12: the fallback each item may fall back to; an empty tuple means none (the item must be proven)
FALLBACKS = {
    "V1": (),                                   # release rule: the claude-cli route is blocked
    "V2": ("identity-carrier-argv",),
    "V3": ("identity-carrier-env",),            # F-ENV
    "V4": (),                                   # a prompt in T3b or T9 blocks the release
    "V5": (),                                   # a workspaceOnly miss blocks until OpenClaw-side mitigation
    "V6": ("poll-cron-runs",),
    "V7": ("pin-off-stray-unsupported",),
    "V8": ("agent-from-session-key",),
    "V9": ("markers-added",),
    "V10": (),                                  # none new
    "V11": ("install-stops",),
    "V12": (),                                  # browser lanes blocked
    "V13": ("F-QC",),
    "V14": ("normalizer-adjusted",),
    "V15": ("config-get-precedence",),
    "V16": ("lower-lane-timeouts",),
    "V17": ("unit-e2e-only",),
    "V18": ("reprobe-after-policy",),
}
V_ITEMS = tuple(FALLBACKS)

# What the live checks exercise. A change to any of these files makes a recorded pass stale.
COVERED = (
    "install.sh", "uninstall.sh", "jobhunter",
    "scripts/jh.py",
    "scripts/jobhunter/acl.json", "scripts/jobhunter/auth.py", "scripts/jobhunter/cli.py",
    "scripts/jobhunter/dispatch.py", "scripts/jobhunter/errors.py", "scripts/jobhunter/identity.py",
    "scripts/jobhunter/install.py", "scripts/jobhunter/ocrun.py", "scripts/jobhunter/paths.py",
    "scripts/jobhunter/selftest.py",
    "scripts/jobhunter/commands/core.py", "scripts/jobhunter/commands/install.py",
    "scripts/jobhunter/commands/profile.py", "scripts/jobhunter/commands/qc.py",
    "scripts/jobhunter/qc/",
    "openclaw/",
    "agent-templates/", "skills-src/", "prompts/",
    "shared-skills/jobhunter-control/SKILL.template.md",
)
SKIP_NAMES = {"node_modules", "__pycache__", ".DS_Store"}
SKIP_PREFIXES = ("openclaw/plugins/jobhunter-guard/test/",)
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class RecordError(ValueError):
    """The record is malformed; the gate is not met."""


# ---------------------------------------------------------------- fingerprint
def covered_files(root: str) -> list:
    """Relative paths (forward slashes) of every covered file under root, sorted; a missing named file is kept."""
    out = set()
    for entry in COVERED:
        full = os.path.join(root, *entry.rstrip("/").split("/"))
        if not entry.endswith("/"):
            out.add(entry)
            continue
        for dirpath, dirnames, filenames in os.walk(full):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_NAMES)
            for name in filenames:
                if name in SKIP_NAMES or name.endswith(".pyc"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
                if not rel.startswith(SKIP_PREFIXES):
                    out.add(rel)
    return sorted(out)


def fingerprint(root: str = REPO) -> str:
    """sha256 over (path, sha256 of content) of every covered file; a missing file counts as missing."""
    h = hashlib.sha256()
    for rel in covered_files(root):
        path = os.path.join(root, *rel.split("/"))
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        elif os.path.islink(path):
            digest = "link:" + os.readlink(path)
        else:
            digest = "missing"
        h.update(rel.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------- record
def _text(value, where: str, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise RecordError(where + ": needs a non-empty text")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise RecordError(where + ": must be plain ASCII")
    return value


def required_parts(test: str) -> tuple:
    return tuple(p for p in PARTS.get(test, ()) if (test, p) not in RECORD_ONLY_PARTS)


def _validate_parts(test: str, row: dict, where: str) -> None:
    """A test with variants lists each part for a pass or a partial result (a fail or not run may omit them);
    when listed, its status must agree with them."""
    status, parts = row["status"], row.get("parts")
    if test not in PARTS:
        if parts is not None:
            raise RecordError(where + ": has no parts")
        if status == "partial":
            raise RecordError(where + ": partial is only for a test with parts")
        return
    if parts is None:
        if status in ("pass", "partial"):
            raise RecordError(where + ": needs parts " + ", ".join(PARTS[test]))
        return
    if not isinstance(parts, dict) or set(parts) != set(PARTS[test]):
        raise RecordError(where + ".parts: must have exactly the keys " + ", ".join(PARTS[test]))
    for name, part in parts.items():
        pw = "%s.parts.%s" % (where, name)
        if not isinstance(part, dict) or set(part) - {"status", "evidence", "note"}:
            raise RecordError(pw + ": must be an object with status, evidence, note")
        if part.get("status") not in PART_STATUSES:
            raise RecordError(pw + ": status must be one of " + ", ".join(PART_STATUSES))
        _text(part.get("evidence"), pw + ".evidence", part["status"] != "not_run")
        _text(part.get("note"), pw + ".note", False)
    req = [parts[p]["status"] for p in required_parts(test)]
    if "fail" in req:
        want = ("fail",)
    elif all(s == "pass" for s in req):
        want = ("pass", "fail")              # a test can fail on its own terms (time, a prompt) with every part ok
    elif "pass" in req:
        want = ("partial", "fail")
    else:
        want = ("not_run", "fail")
    if status not in want:
        raise RecordError("%s: status %s contradicts its parts (%s)" % (
            where, status, ", ".join("%s %s" % (p, parts[p]["status"]) for p in PARTS[test])))


def missing_parts(test: str, row: dict) -> list:
    parts = row.get("parts") or {}
    return [p for p in required_parts(test) if (parts.get(p) or {}).get("status") != "pass"]


def validate(record) -> dict:
    """Check the record's shape; raise RecordError on anything unknown, missing or contradictory."""
    if not isinstance(record, dict):
        raise RecordError("the record must be a JSON object")
    if record.get("version") != RECORD_VERSION:
        raise RecordError("version must be %d" % RECORD_VERSION)
    allowed = {"version", "fingerprint", "default_profile_untouched", "runs", "v_items", "attempts"}
    extra = sorted(set(record) - allowed)
    if extra:
        raise RecordError("unknown keys: " + ", ".join(extra))
    fp = record.get("fingerprint")
    if fp is not None and not (isinstance(fp, str) and HEX64_RE.match(fp)):
        raise RecordError("fingerprint must be null or 64 lowercase hex characters")
    if record.get("default_profile_untouched") not in (True, False, None):
        raise RecordError("default_profile_untouched must be true, false or null")
    runs = record.get("runs")
    if not isinstance(runs, dict) or set(runs) != {V98, V95}:
        raise RecordError("runs must have exactly the keys %s and %s" % (V98, V95))
    for ver in (V98, V95):
        rows = runs[ver]
        if not isinstance(rows, dict):
            raise RecordError("runs.%s must be an object" % ver)
        unknown = sorted(set(rows) - set(TESTS))
        if unknown:
            raise RecordError("runs.%s: unknown tests %s" % (ver, ", ".join(unknown)))
        missing = [t for t in REQUIRED[ver] + OPTIONAL[ver] if t not in rows]
        if missing:
            raise RecordError("runs.%s: missing tests %s" % (ver, ", ".join(missing)))
        for test, row in rows.items():
            where = "runs.%s.%s" % (ver, test)
            if not isinstance(row, dict) or set(row) - {"status", "evidence", "note", "parts"}:
                raise RecordError(where + ": must be an object with status, evidence, note, parts")
            if row.get("status") not in TEST_STATUSES:
                raise RecordError(where + ": status must be one of " + ", ".join(TEST_STATUSES))
            _text(row.get("evidence"), where + ".evidence", row["status"] != "not_run")
            _text(row.get("note"), where + ".note", False)
            _validate_parts(test, row, where)
    items = record.get("v_items")
    if not isinstance(items, dict) or set(items) != set(V_ITEMS):
        raise RecordError("v_items must have exactly the keys V1 to V18")
    for vid, row in items.items():
        where = "v_items." + vid
        if not isinstance(row, dict) or set(row) - {"status", "fallback", "evidence", "note"}:
            raise RecordError(where + ": must be an object with status, fallback, evidence, note")
        status, fallback = row.get("status"), row.get("fallback")
        if status not in V_STATUSES:
            raise RecordError(where + ": status must be one of " + ", ".join(V_STATUSES))
        if fallback is not None:
            if status == "proven":
                raise RecordError(where + ": a proven item has no fallback")
            if fallback not in FALLBACKS[vid]:
                raise RecordError(where + ": fallback must be one of: " + (", ".join(FALLBACKS[vid]) or "none"))
        _text(row.get("evidence"), where + ".evidence", status != "open")
        _text(row.get("note"), where + ".note", False)
    attempts = record.get("attempts", [])
    if not isinstance(attempts, list):
        raise RecordError("attempts must be a list")
    for i, att in enumerate(attempts):
        if not isinstance(att, dict):
            raise RecordError("attempts[%d] must be an object" % i)
        for key, value in att.items():
            _text(value, "attempts[%d].%s" % (i, key), True)
    return record


def load(path: str = RECORD) -> dict:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise RecordError("cannot read the record: %s" % exc.strerror)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise RecordError("the record must be plain ASCII")
    try:
        record = json.loads(text)
    except ValueError as exc:
        raise RecordError("the record is not JSON: %s" % exc)
    return validate(record)


# ---------------------------------------------------------------- verdict
def verdict(record: dict, current_fp: str) -> dict:
    """Apply the release rules of CLI-ROUTE-DESIGN 12 and 15 to a valid record."""
    validate(record)
    reasons = []
    fp = record.get("fingerprint")
    if fp is None:
        reasons.append("no live run is recorded for this code (fingerprint is null): run CLI-ROUTE-DESIGN 13")
    elif fp != current_fp:
        reasons.append("the agents, guard or installer changed since the recorded live run (fingerprint %s, "
                       "now %s): run CLI-ROUTE-DESIGN 13 again" % (fp[:12], current_fp[:12]))
    if record.get("default_profile_untouched") is not True:
        reasons.append("the before/after comparison of the owner's own OpenClaw state is not recorded as unchanged")
    for ver in (V98, V95):
        rows = record["runs"][ver]
        for test in REQUIRED[ver]:
            status = rows[test]["status"]
            if status == "partial":
                reasons.append("%s %s: partial, not run or not passed: %s" % (
                    ver, test, ", ".join(p.replace("_", " ") for p in missing_parts(test, rows[test]))))
            elif status != "pass":
                reasons.append("%s %s: %s" % (ver, test, status.replace("_", " ")))
        for test in OPTIONAL[ver]:
            if rows[test]["status"] == "fail":
                reasons.append("%s %s: fail (optional, but a failure blocks)" % (ver, test))
    items = record["v_items"]
    if items["V1"]["status"] != "proven":
        reasons.append("V1 %s: release rule, the claude-cli route is blocked on this OpenClaw version"
                       % items["V1"]["status"])
    if items["V2"]["status"] != "proven" and items["V3"]["status"] != "proven":
        reasons.append("V2 and V3 both unproven: the V1 release rule applies")
    for vid in V_ITEMS:
        if vid == "V1":
            continue
        row = items[vid]
        if row["status"] == "proven":
            continue
        if not FALLBACKS[vid]:
            reasons.append("%s %s: no fallback, it must be proven" % (vid, row["status"]))
        elif row.get("fallback") is None:
            reasons.append("%s %s without its fallback applied (%s)" % (vid, row["status"],
                                                                     " or ".join(FALLBACKS[vid])))
    return {"met": not reasons, "reasons": reasons, "fingerprint": current_fp, "record_fingerprint": fp}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Release gate of the claude-cli route (live checks T0 to T13).")
    ap.add_argument("--record", default=RECORD)
    ap.add_argument("--root", default=REPO, help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fingerprint", action="store_true", help="print the fingerprint of the current tree")
    args = ap.parse_args(argv)
    current = fingerprint(args.root)
    if args.fingerprint:
        print(current)
        return 0
    try:
        result = verdict(load(args.record), current)
    except RecordError as exc:
        if args.json:
            print(json.dumps({"met": False, "error": str(exc), "fingerprint": current}, sort_keys=True))
        else:
            print("live gate: record malformed: %s" % exc)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("live gate: %s" % ("met" if result["met"] else "NOT MET"))
        for reason in result["reasons"]:
            print("  - " + reason)
    return 0 if result["met"] else 1


if __name__ == "__main__":
    sys.exit(main())
