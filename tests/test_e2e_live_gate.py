"""E2E (INT): the release gate of the claude-cli route, the live checks T0 to T13 (CLI-ROUTE-DESIGN 12, 13, 15).

tests/fixtures/e2e/live_gate.py reads tests/fixtures/e2e/live_gate.json and says whether the route may be released:
every required live test passed on 2026.9.8 and 2026.9.5, every verification item proven or on its own fallback,
the owner's own OpenClaw state unchanged, and the record made for exactly the code that is in the tree now (its
fingerprint covers the agents, the guard, the installer and the identity code). A test with a guard-off or pin-off
variant passes only when each part ran and passed; a guard-on pass with the guard-off part not run is "partial" and
the verdict names the part still to run. These tests pin those rules on
synthetic records, check the committed record's shape, and check the fingerprint and the command line. They do not
run OpenClaw.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import tests  # noqa: F401
from tests.fixtures.e2e import live_gate as G

REPO = G.REPO
SCRIPT = os.path.join(REPO, "tests", "fixtures", "e2e", "live_gate.py")
FP = "ab" * 32


def passing_row(test: str, evidence: str) -> dict:
    row = {"status": "pass", "evidence": evidence}
    if test in G.PARTS:
        row["parts"] = {p: {"status": "pass", "evidence": "%s %s" % (evidence, p)} for p in G.PARTS[test]}
    return row


def passing_record(fp: str = FP) -> dict:
    runs = {
        G.V98: {t: passing_row(t, "jhtest run, guard events and probe files") for t in G.TESTS},
        G.V95: {t: passing_row(t, "jhtest run on 2026.9.5") for t in G.REQUIRED[G.V95]},
    }
    items = {v: {"status": "proven", "fallback": None, "evidence": "seen in T1"} for v in G.V_ITEMS}
    return {"version": 2, "fingerprint": fp, "default_profile_untouched": True, "runs": runs, "v_items": items}


def guard_off_not_run(row: dict, part: str = "guard_off") -> dict:
    row = copy.deepcopy(row)
    row["parts"][part] = {"status": "not_run", "note": "guard disable refused"}
    row["status"] = "partial"
    return row


class VerdictRules(unittest.TestCase):
    def met(self, record, fp=FP):
        return G.verdict(record, fp)

    def test_everything_passed_on_this_code_is_met(self):
        result = self.met(passing_record())
        self.assertTrue(result["met"], result["reasons"])
        self.assertEqual(result["reasons"], [])

    def test_no_fingerprint_is_not_met(self):
        rec = passing_record()
        rec["fingerprint"] = None
        result = self.met(rec)
        self.assertFalse(result["met"])
        self.assertIn("fingerprint is null", result["reasons"][0])

    def test_changed_code_since_the_run_is_not_met(self):
        result = self.met(passing_record(), fp="cd" * 32)
        self.assertFalse(result["met"])
        self.assertIn("changed since the recorded live run", result["reasons"][0])

    def test_owner_profile_comparison_must_be_unchanged(self):
        for value in (False, None):
            rec = passing_record()
            rec["default_profile_untouched"] = value
            result = self.met(rec)
            self.assertFalse(result["met"])
            self.assertIn("before/after comparison", " ".join(result["reasons"]))

    def test_every_required_test_on_98_must_pass(self):
        for test in G.REQUIRED[G.V98]:
            for status in ("fail", "not_run"):
                rec = passing_record()
                if status == "not_run":
                    rec["runs"][G.V98][test] = {"status": "not_run"}
                else:
                    rec["runs"][G.V98][test]["status"] = status
                result = self.met(rec)
                self.assertFalse(result["met"], (test, status))
                self.assertIn("%s %s: " % (G.V98, test), " ".join(result["reasons"]))

    def test_t10_is_optional_but_a_failure_blocks(self):
        rec = passing_record()
        rec["runs"][G.V98]["T10"] = {"status": "not_run"}
        self.assertTrue(self.met(rec)["met"])
        rec["runs"][G.V98]["T10"] = {"status": "fail", "evidence": "dispatch tick ran for main"}
        result = self.met(rec)
        self.assertFalse(result["met"])
        self.assertIn("T10: fail", " ".join(result["reasons"]))

    def test_guard_off_parts_are_required(self):
        """A guard-on pass does not stand for the guard-off variant (T3b, T4b, T5c, T6 pin off, T13 off)."""
        for test in ("T3", "T4", "T5", "T6", "T13"):
            part = "pin_off" if test == "T6" else "guard_off"
            for ver in (G.V98, G.V95):
                if test not in G.REQUIRED[ver]:
                    continue
                rec = passing_record()
                rec["runs"][ver][test] = guard_off_not_run(rec["runs"][ver][test], part)
                result = self.met(rec)
                self.assertFalse(result["met"], (ver, test))
                self.assertIn("%s %s: partial, not run or not passed: %s" % (ver, test, part.replace("_", " ")),
                              result["reasons"])
        rec = passing_record()
        rec["runs"][G.V98]["T5"]["parts"]["offline"] = {"status": "fail", "evidence": "replay accepted"}
        rec["runs"][G.V98]["T5"]["status"] = "fail"
        self.assertIn("2026.9.8 T5: fail", self.met(rec)["reasons"])

    def test_t10_partial_is_optional_like_not_run(self):
        rec = passing_record()
        rec["runs"][G.V98]["T10"] = guard_off_not_run(rec["runs"][G.V98]["T10"])
        self.assertTrue(self.met(rec)["met"])
        rec["runs"][G.V98]["T10"]["parts"]["guard_on"] = {"status": "fail", "evidence": "dispatch tick ran"}
        rec["runs"][G.V98]["T10"]["status"] = "fail"
        self.assertFalse(self.met(rec)["met"])

    def test_t6b_pin_off_is_recorded_only(self):
        for status in ("not_run", "fail"):
            rec = passing_record()
            part = {"status": status, "evidence": "waited until the timeout"} if status == "fail" else {
                "status": "not_run"}
            rec["runs"][G.V98]["T6b"]["parts"]["pin_off"] = part
            self.assertTrue(self.met(rec)["met"], status)

    def test_t6b_pin_on_is_required(self):
        rec = passing_record()
        rec["runs"][G.V98]["T6b"] = {"status": "fail", "evidence": "turn waited for the operator"}
        self.assertFalse(self.met(rec)["met"])

    def test_95_needs_t0_to_t7_and_t11_only(self):
        self.assertEqual(G.REQUIRED[G.V95],
                         ("T0", "T1", "T2", "T3", "T4", "T5", "T6", "T6b", "T7", "T7b", "T11"))
        rec = passing_record()
        for test in G.REQUIRED[G.V95]:
            bad = copy.deepcopy(rec)
            bad["runs"][G.V95][test] = {"status": "not_run"}
            self.assertFalse(self.met(bad)["met"], test)
        # tests outside the 9.5 subset may be listed as not run
        rec["runs"][G.V95]["T8"] = {"status": "not_run"}
        rec["runs"][G.V95]["T13"] = {"status": "not_run"}
        self.assertTrue(self.met(rec)["met"])

    def test_v1_not_proven_is_the_release_rule(self):
        for status in ("failed", "open"):
            rec = passing_record()
            rec["v_items"]["V1"] = {"status": status, "fallback": None, "evidence": "native tools present"}
            result = self.met(rec)
            self.assertFalse(result["met"])
            self.assertIn("claude-cli route is blocked", " ".join(result["reasons"]))

    def test_items_without_a_fallback_must_be_proven(self):
        for vid in ("V4", "V5", "V10", "V12"):
            self.assertEqual(G.FALLBACKS[vid], ())
            rec = passing_record()
            rec["v_items"][vid] = {"status": "failed", "fallback": None, "evidence": "seen live"}
            result = self.met(rec)
            self.assertFalse(result["met"], vid)
            self.assertIn("%s failed: no fallback" % vid, " ".join(result["reasons"]))

    def test_item_on_its_own_fallback_is_met(self):
        rec = passing_record()
        rec["v_items"]["V13"] = {"status": "failed", "fallback": "F-QC", "evidence": "run record lacks the text"}
        self.assertTrue(self.met(rec)["met"])

    def test_item_failed_without_fallback_is_not_met(self):
        rec = passing_record()
        rec["v_items"]["V13"] = {"status": "failed", "fallback": None, "evidence": "run record lacks the text"}
        result = self.met(rec)
        self.assertFalse(result["met"])
        self.assertIn("V13 failed without its fallback applied", " ".join(result["reasons"]))

    def test_v2_and_v3_may_not_both_fall_back(self):
        rec = passing_record()
        rec["v_items"]["V2"] = {"status": "failed", "fallback": "identity-carrier-argv", "evidence": "no env"}
        self.assertTrue(self.met(rec)["met"])
        rec["v_items"]["V3"] = {"status": "failed", "fallback": "identity-carrier-env", "evidence": "no rewrite"}
        result = self.met(rec)
        self.assertFalse(result["met"])
        self.assertIn("V2 and V3 both unproven", " ".join(result["reasons"]))


class RecordShape(unittest.TestCase):
    def bad(self, rec, text):
        with self.assertRaises(G.RecordError) as cm:
            G.validate(rec)
        self.assertIn(text, str(cm.exception))

    def test_unknown_or_missing_parts_are_malformed(self):
        rec = passing_record()
        rec["version"] = 1
        self.bad(rec, "version")
        rec = passing_record()
        rec["released"] = True
        self.bad(rec, "unknown keys")
        rec = passing_record()
        rec["fingerprint"] = "XYZ"
        self.bad(rec, "fingerprint")
        rec = passing_record()
        del rec["runs"][G.V95]
        self.bad(rec, "runs must have")
        rec = passing_record()
        del rec["runs"][G.V98]["T10"]
        self.bad(rec, "missing tests T10")
        rec = passing_record()
        rec["runs"][G.V98]["T14"] = {"status": "pass", "evidence": "x"}
        self.bad(rec, "unknown tests T14")
        rec = passing_record()
        del rec["v_items"]["V18"]
        self.bad(rec, "V1 to V18")

    def test_parts_must_agree_with_the_status(self):
        rec = passing_record()
        rec["runs"][G.V98]["T3"]["parts"]["guard_off"] = {"status": "not_run"}
        self.bad(rec, "status pass contradicts its parts")
        rec = passing_record()
        rec["runs"][G.V98]["T4"]["parts"]["guard_off"] = {"status": "fail", "evidence": "~ read reached the file"}
        self.bad(rec, "status pass contradicts its parts")
        rec = passing_record()
        rec["runs"][G.V98]["T4"] = guard_off_not_run(rec["runs"][G.V98]["T4"])
        rec["runs"][G.V98]["T4"]["parts"]["guard_on"] = {"status": "not_run"}
        self.bad(rec, "status partial contradicts its parts")
        rec = passing_record()
        del rec["runs"][G.V98]["T13"]["parts"]
        self.bad(rec, "T13: needs parts guard_on, guard_off")
        rec["runs"][G.V98]["T13"]["status"] = "partial"
        self.bad(rec, "T13: needs parts guard_on, guard_off")
        rec["runs"][G.V98]["T13"]["status"] = "fail"
        G.validate(rec)
        rec = passing_record()
        del rec["runs"][G.V98]["T6"]["parts"]["pin_off"]
        self.bad(rec, "T6.parts: must have exactly the keys pin_on, pin_off")
        rec = passing_record()
        rec["runs"][G.V98]["T2"]["parts"] = {"guard_on": {"status": "pass", "evidence": "x"}}
        self.bad(rec, "T2: has no parts")
        rec = passing_record()
        rec["runs"][G.V98]["T2"]["status"] = "partial"
        self.bad(rec, "partial is only for a test with parts")
        rec = passing_record()
        rec["runs"][G.V98]["T3"]["parts"]["guard_off"] = {"status": "pass"}
        self.bad(rec, "T3.parts.guard_off.evidence")
        rec = passing_record()
        rec["runs"][G.V98]["T3"]["parts"]["guard_off"]["status"] = "partial"
        self.bad(rec, "status must be one of")
        # a test that is not run at all needs no parts
        rec = passing_record()
        rec["runs"][G.V98]["T3"] = {"status": "not_run"}
        G.validate(rec)

    def test_statuses_are_closed_lists(self):
        rec = passing_record()
        rec["runs"][G.V98]["T1"]["status"] = "skipped"
        self.bad(rec, "status must be one of")
        rec = passing_record()
        rec["v_items"]["V9"]["status"] = "assumed"
        self.bad(rec, "status must be one of")

    def test_a_result_needs_evidence(self):
        rec = passing_record()
        rec["runs"][G.V98]["T4"] = {"status": "pass"}
        self.bad(rec, "T4.evidence")
        rec = passing_record()
        rec["v_items"]["V5"] = {"status": "proven", "fallback": None, "evidence": "  "}
        self.bad(rec, "V5.evidence")

    def test_fallback_must_be_the_items_own(self):
        rec = passing_record()
        rec["v_items"]["V13"] = {"status": "failed", "fallback": "identity-carrier-env", "evidence": "x"}
        self.bad(rec, "fallback must be one of: F-QC")
        rec = passing_record()
        rec["v_items"]["V4"] = {"status": "failed", "fallback": "install-stops", "evidence": "x"}
        self.bad(rec, "fallback must be one of: none")
        rec = passing_record()
        rec["v_items"]["V13"] = {"status": "proven", "fallback": "F-QC", "evidence": "x"}
        self.bad(rec, "a proven item has no fallback")

    def test_text_is_plain_ascii(self):
        rec = passing_record()
        rec["runs"][G.V98]["T2"]["evidence"] = "read ok " + chr(0x2014) + " no prompt"
        self.bad(rec, "plain ASCII")

    def test_committed_record_is_valid_and_honest(self):
        rec = G.load()
        result = G.verdict(rec, G.fingerprint())
        # a recorded pass or proof always names its evidence (validate), and a met gate only for this exact code
        if result["met"]:
            self.assertEqual(rec["fingerprint"], G.fingerprint())
        for ver in (G.V98, G.V95):
            for test, row in rec["runs"][ver].items():
                if row["status"] != "not_run":
                    self.assertTrue(row.get("evidence"), (ver, test))
                if row["status"] == "pass":
                    self.assertEqual(G.missing_parts(test, row), [], (ver, test))

    def test_committed_record_has_no_machine_paths(self):
        with open(G.RECORD, "rb") as fh:
            text = fh.read().decode("ascii")
        for needle in ("/" + "Users/", "/" + "home/", "/private/", "/tmp/", "~/"):
            self.assertNotIn(needle, text)


class Fingerprint(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="jh-gate-")
        self.addCleanup(shutil.rmtree, self.root)
        for rel in ("install.sh", "scripts/jobhunter/install.py", "openclaw/crons.json",
                    "openclaw/plugins/jobhunter-guard/src/policy.ts",
                    "openclaw/plugins/jobhunter-guard/test/policy.test.ts",
                    "openclaw/plugins/jobhunter-guard/node_modules/x/index.js",
                    "agent-templates/evaluator/AGENTS.template.md", "README.md", "scripts/jobhunter/enrich/x.py"):
            self.write(rel, "v1 " + rel)

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def test_covered_files_change_it(self):
        base = G.fingerprint(self.root)
        self.assertRegex(base, r"^[0-9a-f]{64}$")
        self.assertEqual(G.fingerprint(self.root), base)
        for rel in ("install.sh", "scripts/jobhunter/install.py", "openclaw/crons.json",
                    "openclaw/plugins/jobhunter-guard/src/policy.ts", "agent-templates/evaluator/AGENTS.template.md"):
            self.write(rel, "v2 " + rel)
            changed = G.fingerprint(self.root)
            self.assertNotEqual(changed, base, rel)
            base = changed

    def test_new_and_removed_covered_files_change_it(self):
        base = G.fingerprint(self.root)
        self.write("skills-src/jobhunter-gate/SKILL.template.md", "new")
        added = G.fingerprint(self.root)
        self.assertNotEqual(added, base)
        os.remove(os.path.join(self.root, "scripts", "jobhunter", "install.py"))
        self.assertNotEqual(G.fingerprint(self.root), added)

    def test_uncovered_files_do_not(self):
        base = G.fingerprint(self.root)
        for rel in ("README.md", "scripts/jobhunter/enrich/x.py",
                    "openclaw/plugins/jobhunter-guard/test/policy.test.ts",
                    "openclaw/plugins/jobhunter-guard/node_modules/x/index.js"):
            self.write(rel, "v2 " + rel)
        self.assertEqual(G.fingerprint(self.root), base)

    def test_repo_covers_the_identity_and_install_code(self):
        files = G.covered_files(REPO)
        for rel in ("install.sh", "scripts/jh.py", "scripts/jobhunter/auth.py", "scripts/jobhunter/install.py",
                    "scripts/jobhunter/ocrun.py", "openclaw/agents.patch.json5.tmpl", "openclaw/crons.json",
                    "openclaw/exec-approvals.json5.tmpl", "openclaw/plugins/jobhunter-guard/index.ts"):
            self.assertIn(rel, files)
            self.assertTrue(os.path.isfile(os.path.join(REPO, *rel.split("/"))), rel)
        self.assertFalse([f for f in files if "/node_modules/" in f or f.endswith(".pyc")])


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jh-gate-cli-")
        self.addCleanup(shutil.rmtree, self.dir)

    def run_gate(self, record, *extra):
        path = os.path.join(self.dir, "record.json")
        with open(path, "w") as fh:
            fh.write(record if isinstance(record, str) else json.dumps(record))
        return subprocess.run([sys.executable, "-I", SCRIPT, "--record", path] + list(extra),
                              capture_output=True, text=True, timeout=60)

    def test_met_for_this_tree_exits_0(self):
        proc = self.run_gate(passing_record(G.fingerprint()))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("live gate: met", proc.stdout)

    def test_not_met_exits_1_with_reasons(self):
        rec = passing_record(G.fingerprint())
        rec["runs"][G.V98]["T4"] = {"status": "fail", "evidence": "node:// read reached the file"}
        proc = self.run_gate(rec, "--json")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertFalse(out["met"])
        self.assertIn("2026.9.8 T4: fail", out["reasons"])

    def test_malformed_exits_2(self):
        self.assertEqual(self.run_gate("{not json").returncode, 2)
        self.assertEqual(self.run_gate({"version": 1}).returncode, 2)

    def test_fingerprint_flag(self):
        proc = subprocess.run([sys.executable, "-I", SCRIPT, "--fingerprint"], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), G.fingerprint())

    def test_committed_record_exit_code_matches_its_verdict(self):
        proc = subprocess.run([sys.executable, "-I", SCRIPT], capture_output=True, text=True, timeout=60)
        expected = 0 if G.verdict(G.load(), G.fingerprint())["met"] else 1
        self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
