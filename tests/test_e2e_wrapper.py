"""E2E (INT): install.sh and the ./jobhunter wrapper against the real core commands.

tests/test_install_sh.py (U7) runs the installer against fake core commands unless JH_INSTALL_TEST_REAL_CORE=1.
Here one sandbox (the U7 Sandbox: a repo copy, a temp HOME, only the fake openclaw on PATH) is installed with the
real U1 command modules, then the wrapper is driven at a pseudo terminal the way a person uses it:
`./jobhunter pin set` first sets and then changes the owner PIN through the real `auth set-pin`, and
`./jobhunter edit <code>` shows a draft's body in $EDITOR and stores exactly what the editor saved.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest

import tests  # noqa: F401
from jobhunter import paths
from tests.test_install_sh import BASH, Sandbox

PIN1 = "135790"
PIN2 = "246801"
SUBJECT = "Pincode-level RTO models"
BODY = ("Hi Jordan,\n\nYour post on 18 September said pincode-level models beat the city-level RTO model at Kestrel. "
        "We saw the same pattern at Tidemark Logistics.\n\nI built the COD return-risk model at Tidemark, and RTO fell "
        "from 18% to 13% in two quarters.\n\nWould a 15 minute call next week be useful? If someone else owns this, "
        "a name is plenty.\n\nThanks,")

# Runs inside the sandbox with its own copy of the modules: one cold email waiting for the owner's approval.
MAKE_DRAFT = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from jobhunter import approvals, canon, db, drafts, research
subject, body = json.loads(sys.argv[2])
SNIP = "pincode-level models beat our city-level RTO model last quarter"
POST = "https://www.linkedin.com/posts/example-person-activity-1"
conn = db.connect()
with db.tx(conn):
    ts = canon.now()
    co = conn.execute("INSERT INTO companies (company_uid, display_name, domain, is_agency, contact_state, created_at, "
                      "updated_at) VALUES (?, 'Kestrel Commerce', 'kestrel.example', 0, 'none', ?, ?)",
                      (canon.new_uid("K"), ts, ts)).lastrowid
    ct = conn.execute("INSERT INTO contacts (contact_uid, full_name, first_name, company_id, role_type, email, "
                      "do_not_contact, created_at, updated_at) VALUES (?, 'Jordan Blake', 'Jordan', ?, "
                      "'hiring_manager', 'jordan.blake@kestrel.example', 0, ?, ?)",
                      (canon.new_uid("P"), co, ts, ts)).lastrowid
    ct_uid = conn.execute("SELECT contact_uid FROM contacts WHERE id = ?", (ct,)).fetchone()[0]
    published, retrieved = canon.ts_add(ts, days=-10)[:10], canon.ts_add(ts, days=-1)[:10]
    fact = research.add_research(conn, {"subject": {"kind": "person", "contact_uid": ct_uid}, "facts": [
        {"text": "Post: pincode-level models beat the city-level RTO model last quarter.", "snippet": SNIP,
         "source_type": "linkedin_post", "source_url": POST, "published_at": published,
         "retrieved_at": retrieved}]})["facts"][0]["fact_uid"]
    hook = {"anchor": "pincode-level models", "source_type": "linkedin_post", "source_url": POST, "snippet": SNIP,
            "published_at": published, "retrieved_at": retrieved, "fact_id": fact}
    payload = {"hook": hook, "claims": [], "links": [], "is_reply": False, "field_char_limit": None,
               "field_label": None, "payload": None, "attachment": None, "edited_by_human": 0}
    cfg = drafts._settings(conn)
    sha = canon.sha256_text(drafts.compute_send_text("cold_email", subject, body, payload, cfg))
    d = conn.execute("INSERT INTO drafts (draft_uid, kind, channel, send_route, contact_id, company_id, subject, body, "
                     "payload_json, text_sha256, status, expires_at, created_at, updated_at) VALUES (?, 'cold_email', "
                     "'email_cold', 'mailer', ?, ?, ?, ?, ?, ?, 'awaiting_approval', ?, ?, ?)",
                     (canon.new_uid("D"), ct, co, subject, body, json.dumps(payload, sort_keys=True), sha,
                      canon.ts_add(ts, days=3), ts, ts)).lastrowid
    code = approvals.issue_code(conn, d)
print(json.dumps({"code": code, "draft_id": d}))
'''

# $EDITOR for the edit test: changes one phrase and leaves every other byte alone.
EDITOR = r'''#!/usr/bin/env python3
import os, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    text = fh.read()
with open(os.environ["FAKE_EDITOR_SEEN"], "w", encoding="utf-8") as fh:
    fh.write(text)
with open(path, "w", encoding="utf-8") as fh:
    fh.write(text.replace("15 minute call", "20 minute call"))
'''


@unittest.skipUnless(os.path.exists(BASH), "no /bin/bash")
class TestWrapperWithRealCore(unittest.TestCase):
    """One sandbox, in order: install with the real core, set and change the PIN, edit a draft."""

    @classmethod
    def setUpClass(cls):
        cls.sb = Sandbox(configured=True, real_core=True)
        # the U7 sandbox copies only what the installer needs; QC (the edit's lint and review) also reads these
        for d in ("qc", "prompts"):
            if not os.path.exists(os.path.join(cls.sb.repo, d)):
                shutil.copytree(os.path.join(paths.REPO, d), os.path.join(cls.sb.repo, d),
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cls.rc, cls.out = cls.sb.run("install.sh", "--yes", "--no-smoke", env={"JH_INSTALL_TEST_REAL_CORE": "1"})

    @classmethod
    def tearDownClass(cls):
        cls.sb.close()

    def check_pin(self, pin: str) -> int:
        return self.sb.jh("--human", "--pin-stdin", "auth", "check", stdin=pin.encode() + b"\n")[0]

    def db(self):
        return sqlite3.connect(self.sb.home_json()["db_path"])

    def test_1_install_runs_with_the_real_core(self):
        self.assertEqual(self.rc, 0, self.out)
        self.assertIn("DONE install complete", self.out)
        cmds = os.path.join(self.sb.repo, "scripts", "jobhunter", "commands")
        for m in ("core.py", "limits.py", "maint.py", "gate.py"):
            self.assertTrue(os.path.exists(os.path.join(cmds, m)), m)
        home = self.sb.home_json()
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'install_id'").fetchone()[0],
                             home["install_id"])
        rc, out = self.sb.jh("--human", "status")
        self.assertEqual(rc, 0, out)

    def test_2_pin_set_then_change(self):
        self.assertFalse(os.path.exists(os.path.join(self.sb.repo, "private", "owner_pin.json")))
        rc, out = self.sb.run_tty("jobhunter", "pin", "set",
                                  answers=((b"New PIN (6 to 12 digits): ", PIN1.encode() + b"\n"),
                                           (b"New PIN again: ", PIN1.encode() + b"\n")))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.check_pin(PIN1), 0)
        # a wrong current PIN changes nothing
        rc, out = self.sb.run_tty("jobhunter", "pin", "set",
                                  answers=((b"Current PIN: ", b"999999\n"), (b"New PIN (6 to 12 digits): ", b"111111\n"),
                                           (b"New PIN again: ", b"111111\n")))
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.check_pin(PIN1), 0)
        self.assertNotEqual(self.check_pin("111111"), 0)
        # the right one changes it
        rc, out = self.sb.run_tty("jobhunter", "pin", "set",
                                  answers=((b"Current PIN: ", PIN1.encode() + b"\n"),
                                           (b"New PIN (6 to 12 digits): ", PIN2.encode() + b"\n"),
                                           (b"New PIN again: ", PIN2.encode() + b"\n")))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.check_pin(PIN2), 0)
        self.assertNotEqual(self.check_pin(PIN1), 0)
        self.assertNotIn(PIN2, out)

    def test_3_edit_round_trips_the_body(self):
        if self.check_pin(PIN2) != 0:
            self.skipTest("needs the PIN from test_2")
        p = subprocess.run([sys.executable, "-c", MAKE_DRAFT, os.path.join(self.sb.repo, "scripts"),
                            json.dumps([SUBJECT, BODY])], env=self.sb.env, cwd=self.sb.repo, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout)
        made = json.loads(p.stdout.decode().strip().splitlines()[-1])
        editor = os.path.join(self.sb.bin, "fake-editor")
        with open(editor, "w") as fh:
            fh.write(EDITOR.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        os.chmod(editor, 0o755)
        seen_file = os.path.join(self.sb.root, "editor-seen.txt")
        rc, out = self.sb.run_tty("jobhunter", "edit", made["code"], answers=((b"Owner PIN: ", PIN2.encode() + b"\n"),),
                                  env={"EDITOR": editor, "FAKE_EDITOR_SEEN": seen_file})
        self.assertEqual(rc, 0, out)
        self.assertIn("passed the checks", out)
        with self.db() as conn:
            subject, body, edits, status = conn.execute("SELECT subject, body, human_edits, status FROM drafts WHERE id = ?",
                                                        (made["draft_id"],)).fetchone()
        self.assertEqual(subject, SUBJECT)
        self.assertEqual(body, BODY.replace("15 minute call", "20 minute call"))
        self.assertEqual(edits, 1)
        self.assertNotEqual(status, "awaiting_approval")
        # the editor was shown the stored body itself (no envelope, no quoting), and the temp copy is gone
        with open(seen_file, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().rstrip("\n"), BODY)
        self.assertEqual([n for n in os.listdir(os.path.join(self.sb.root, "tmp")) if n.startswith("jobhunter.")], [])
        # let the review worker the edit started finish before the sandbox goes away
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with self.db() as conn:
                busy = conn.execute("SELECT count(*) FROM qc_jobs WHERE state IN ('queued', 'running')").fetchone()[0]
            if not busy:
                break
            time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
