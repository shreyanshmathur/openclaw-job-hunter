"""Housekeeping (design 3.4): expiry, approval codes, backups (keep 14), work-dir and log pruning, clock
check, stale reconcile tasks, the auto-mode suggestion; one failing task never stops the others."""
from __future__ import annotations

import os
import time
from unittest import mock

import tests  # noqa: F401
from jobhunter import canon, db, housekeeping, paths
from tests.fakes.u1 import TUESDAY_NOON, write_config, write_heartbeat
from tests.helpers import HomeTestCase, insert_action, insert_draft


class TestHousekeeping(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        write_heartbeat()

    def test_full_run(self):
        aid = insert_action(self.conn, kind="cold_email", status="reserved", reserved_at=self.clock.ago(hours=1),
                            contact_id=None)
        self.conn.execute("UPDATE actions SET expires_at = ? WHERE id = ?", (self.clock.ago(minutes=20), aid))
        d = insert_draft(self.conn, status="sent")
        self.conn.execute("INSERT INTO approval_codes (code, draft_id, issued_at) VALUES ('A7K2', ?, ?)", (d, canon.now()))
        old = os.path.join(paths.ws_dir("scout"), "work", "C20260901T000000ZAAAA")
        new = os.path.join(paths.ws_dir("scout"), "work", "C20260929T000000ZAAAA")
        keep = os.path.join(paths.ws_dir("scout"), "work", "notes")
        for p in (old, new, keep):
            os.makedirs(p, exist_ok=True)
        past = time.time() - 10 * 86400
        os.utime(old, (past, past))
        os.utime(keep, (past, past))
        ev_dir = os.path.join(paths.logs_dir(), "evidence")
        os.makedirs(ev_dir, exist_ok=True)
        stale = os.path.join(ev_dir, "old.txt")
        with open(stale, "w") as fh:
            fh.write("x")
        os.utime(stale, (time.time() - 40 * 86400,) * 2)
        res = housekeeping.run(self.conn)["tasks"]
        errors = {k: v for k, v in res.items() if isinstance(v, dict) and "error" in v}
        self.assertEqual(errors, {})
        self.assertEqual(res["expire"]["tokens"], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM actions WHERE id = ?", (aid,)).fetchone()[0], "unknown")
        self.assertIsNotNone(self.conn.execute("SELECT closed_at FROM approval_codes").fetchone()[0])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new) and os.path.exists(keep))
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(res["backup"]["path"]))
        self.assertIsNotNone(db.meta_get(self.conn, "housekeeping_last_at"))

    def test_old_closed_codes_are_pruned(self):
        d1 = insert_draft(self.conn, status="sent")
        d2 = insert_draft(self.conn, status="sent")
        d3 = insert_draft(self.conn, status="awaiting_approval")
        for code, did, age in (("A7K2", d1, 31), ("C3M4", d2, 5), ("D4N6", d3, 40)):
            self.conn.execute("INSERT INTO approval_codes (code, draft_id, issued_at) VALUES (?, ?, ?)",
                              (code, did, self.clock.ago(days=age)))
        res = housekeeping.run(self.conn, only="codes")["tasks"]["codes"]
        self.assertEqual(res, {"closed": 2, "pruned": 1})
        left = {r[0]: r[1] for r in self.conn.execute("SELECT code, closed_at FROM approval_codes")}
        self.assertEqual(sorted(left), ["C3M4", "D4N6"])     # a recent code and an open one stay
        self.assertIsNone(left["D4N6"])

    def test_stale_staged_files_are_removed(self):
        from jobhunter import resume
        up = os.path.join(self.home.dir, "uploads")
        os.makedirs(up, exist_ok=True)
        vid = self.conn.execute(
            "INSERT INTO resume_variants (variant_uid, mode, base_sha256, pdf_path, txt_path, pdf_sha256, created_at) "
            "VALUES ('VAAAAAAA', 'base', 'b', 'r.pdf', 'r.txt', 'p', ?)", (canon.now(),)).lastrowid
        files = {}
        for tok, age_min in (("TAAAAAAAAAAA", 90), ("TAAAAAAAAAAB", 10)):
            path = os.path.join(up, tok + ".pdf")
            with open(path, "w") as fh:
                fh.write("x")
            files[tok] = path
            self.conn.execute("INSERT INTO staged_files (token, variant_id, path, sha256, staged_at) VALUES "
                              "(?, ?, ?, ?, ?)", (tok, vid, path, "s", self.clock.ago(minutes=age_min)))
        with mock.patch.object(resume, "upload_root", lambda: os.path.realpath(up)):
            res = housekeeping.run(self.conn, only="staged")["tasks"]["staged"]
        self.assertEqual(res, {"removed": 1})
        self.assertFalse(os.path.exists(files["TAAAAAAAAAAA"]))
        self.assertTrue(os.path.exists(files["TAAAAAAAAAAB"]))
        open_rows = [r[0] for r in self.conn.execute("SELECT token FROM staged_files WHERE removed_at IS NULL")]
        self.assertEqual(open_rows, ["TAAAAAAAAAAB"])

    def test_audit_task_reads_mail_outside_the_transaction(self):
        from jobhunter import audit
        seen = []

        def fake(conn, days):
            seen.append(conn.in_transaction)
            return [], None
        with mock.patch.object(audit, "_mail_sent_since", fake):
            res = housekeeping.run(self.conn, only="audit")["tasks"]["audit"]
        self.assertEqual((seen, res["mismatches"], res["errors"]), ([False], [], []))
        self.assertIsNotNone(db.meta_get(self.conn, "audit_last_at"))

    def test_backups_keep_fourteen(self):
        for i in range(16):
            self.clock.advance(days=1)
            housekeeping.run(self.conn, only="backup")
        files = os.listdir(os.path.join(paths.state_dir(), "backups"))
        self.assertEqual(len([f for f in files if f.startswith("jobhunter-")]), 14)

    def test_only_and_unknown_task(self):
        res = housekeeping.run(self.conn, only="locks")
        self.assertEqual(list(res["tasks"]), ["locks"])
        self.assertDenied("E_USAGE", housekeeping.run, self.conn, "everything")

    def test_enrich_and_consent_tasks(self):
        from jobhunter import identity
        from tests.helpers import write_consent
        from jobhunter.enrich import housekeeping as enrich_housekeeping
        seen = []
        with mock.patch.object(enrich_housekeeping, "run_housekeeping",
                               lambda conn: seen.append(conn.in_transaction) or {"settled": 0, "purged": 0}):
            res = housekeeping.run(self.conn, only="enrich")["tasks"]["enrich"]
        self.assertEqual((res, seen), ({"settled": 0, "purged": 0}, [False]))   # it opens its own transactions
        import builtins
        real = builtins.__import__

        def no_finder(name, globals=None, locals=None, fromlist=(), level=0):
            if (level and name == "enrich") or name.startswith("jobhunter.enrich") or \
                    (name == "" and fromlist and "enrich" in fromlist):
                raise ImportError("no finder")
            return real(name, globals, locals, fromlist, level)
        with mock.patch.object(builtins, "__import__", no_finder):
            res = housekeeping.run(self.conn, only="enrich")["tasks"]["enrich"]
        self.assertIn("skipped", res)
        # consent: a site whose consent was taken back outside the commands is stopped
        housekeeping.run(self.conn, only="consent")
        write_consent(["gmail"])
        res = housekeeping.run(self.conn, only="consent")["tasks"]["consent"]
        self.assertIn("linkedin", res["tripped"])
        self.assertEqual(res["active"], ["gmail"])
        self.assertIn("consent", housekeeping.TASKS)
        self.assertEqual(identity.active_consent_sites(), ["gmail"])

    def test_stale_reconcile_tasks(self):
        aid = insert_action(self.conn, kind="application", platform="greenhouse", status="unknown", contact_id=None,
                            reserved_at=self.clock.ago(days=2))
        res = housekeeping.run(self.conn, only="reconcile")
        self.assertEqual(res["tasks"]["reconcile"]["tasks"], 1)
        self.assertTrue(self.conn.execute("SELECT 1 FROM human_tasks WHERE kind = 'apply_manually' AND action_id = ?",
                                          (aid,)).fetchone())

    def test_suggest_auto(self):
        for i in range(50):
            d = insert_draft(self.conn, status="approved")
            self.conn.execute("UPDATE drafts SET approved_at = ? WHERE id = ?", (self.clock.ago(days=20), d))
            self.conn.execute("INSERT INTO qc_results (draft_id, attempt, stage, passed, text_sha256, code_verdict, "
                              "created_at) VALUES (?, 1, 'review', 1, 'x', 'pass', ?)", (d, canon.now()))
        res = housekeeping.run(self.conn, only="suggest_auto")
        self.assertTrue(res["tasks"]["suggest_auto"]["suggested"], res)
        self.assertTrue(self.conn.execute("SELECT 1 FROM human_tasks WHERE kind = 'suggest_auto'").fetchone())
        res = housekeeping.run(self.conn, only="suggest_auto")
        self.assertFalse(res["tasks"]["suggest_auto"]["suggested"])

    def test_clock_backwards(self):
        with db.tx(self.conn):
            db.meta_set(self.conn, "max_seen_ts", canon.ts_add(canon.now(), hours=1), "system")
        res = housekeeping.run(self.conn, only="clock")
        self.assertEqual(res["tasks"]["clock"]["problem"], "clock_backward")


if __name__ == "__main__":
    import unittest
    unittest.main()
