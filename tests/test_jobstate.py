"""jobstate: Python graphs equal the schema triggers; single-writer behaviour (design 2.5)."""
from __future__ import annotations

import json
import os
import sqlite3

import tests  # noqa: F401
from jobhunter import canon, db, jobstate, paths
from jobhunter.errors import Denied, map_sqlite_error
from tests.helpers import HomeTestCase, insert_draft, insert_job


class TestGraphParity(HomeTestCase):
    def _trigger_allows(self, table, old, new, extra_set=""):
        ts = canon.now()
        if table == "jobs":
            rid = insert_job(self.conn, status=old)
        else:
            rid = insert_draft(self.conn, status=old)
        try:
            self.conn.execute("UPDATE %s SET status = ?%s WHERE id = ?" % (table, extra_set), (new, rid))
            return True
        except sqlite3.IntegrityError as exc:
            code = map_sqlite_error(exc).code
            self.assertEqual(code, "E_BAD_TRANSITION", (table, old, new, str(exc), ts))
            return False

    def test_job_graph_matches_trigger(self):
        self.conn.execute("BEGIN")
        try:
            for old in jobstate.JOB_STATUSES:
                for new in jobstate.JOB_STATUSES:
                    with self.subTest(old=old, new=new):
                        self.assertEqual(self._trigger_allows("jobs", old, new),
                                         jobstate.job_transition_allowed(old, new))
        finally:
            self.conn.execute("ROLLBACK")

    def test_draft_graph_matches_trigger(self):
        self.conn.execute("BEGIN")
        try:
            for old in jobstate.DRAFT_STATUSES:
                for new in jobstate.DRAFT_STATUSES:
                    with self.subTest(old=old, new=new):
                        self.assertEqual(self._trigger_allows("drafts", old, new, ", approved_by = 'auto'"),
                                         jobstate.draft_transition_allowed(old, new))
        finally:
            self.conn.execute("ROLLBACK")

    def test_graphs_cover_schema_enums(self):
        self.assertEqual(set(jobstate.JOB_GRAPH), set(jobstate.JOB_STATUSES))
        self.assertEqual(set(jobstate.DRAFT_GRAPH), set(jobstate.DRAFT_STATUSES))


class TestSetStatus(HomeTestCase):
    def _events(self):
        path = os.path.join(paths.logs_dir(), "events-%s.jsonl" % canon.now()[:7])
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def test_set_job_status(self):
        j = insert_job(self.conn, status="new")
        self.clock.advance(minutes=1)
        with db.tx(self.conn):
            jobstate.set_job_status(self.conn, j, "eval_queued", "passed_prefilter", "U2:jobs.ingest")
        row = self.conn.execute("SELECT status, status_reason, updated_at FROM jobs WHERE id = ?", (j,)).fetchone()
        self.assertEqual(tuple(row), ("eval_queued", "passed_prefilter", canon.now()))
        ev = self._events()[-1]
        self.assertEqual((ev["kind"], ev["old"], ev["new"], ev["job_id"]), ("job_status", "new", "eval_queued", j))
        with self.assertRaises(Denied) as cm:
            jobstate.set_job_status(self.conn, j, "applied", "x", "test")
        self.assertEqual(cm.exception.code, "E_BAD_TRANSITION")
        with self.assertRaises(Denied) as cm:
            jobstate.set_job_status(self.conn, 99999, "closed", "x", "test")
        self.assertEqual(cm.exception.code, "E_NOT_FOUND")
        with self.assertRaises(Denied) as cm:
            jobstate.set_job_status(self.conn, j, "bogus", "x", "test")
        self.assertEqual(cm.exception.code, "E_VALIDATION")
        jobstate.set_job_status(self.conn, j, "eval_queued", "again", "test")   # same status: no-op transition
        self.assertEqual(self.conn.execute("SELECT status_reason FROM jobs WHERE id = ?", (j,)).fetchone()[0], "again")

    def test_set_draft_status_approval(self):
        d = insert_draft(self.conn, status="awaiting_approval")
        with self.assertRaises(Denied) as cm:
            jobstate.set_draft_status(self.conn, d, "approved", "ok", "jobhunter-outreach")
        self.assertEqual(cm.exception.code, "E_QC_NOT_APPROVED")
        with db.tx(self.conn):
            jobstate.set_draft_status(self.conn, d, "approved", "approved in chat", "human:chat")
        row = self.conn.execute("SELECT status, approved_by, approved_at FROM drafts WHERE id = ?", (d,)).fetchone()
        self.assertEqual(tuple(row), ("approved", "human:chat", canon.now()))
        jobstate.set_draft_status(self.conn, d, "sent", "confirmed", "gate.confirm")
        with self.assertRaises(Denied) as cm:
            jobstate.set_draft_status(self.conn, d, "drafted", "x", "test")
        self.assertEqual(cm.exception.code, "E_BAD_TRANSITION")

    def test_no_commit_inside_callers_tx(self):
        j = insert_job(self.conn, status="new")
        with self.assertRaises(RuntimeError):
            with db.tx(self.conn):
                jobstate.set_job_status(self.conn, j, "eval_queued", "r", "t")
                raise RuntimeError("caller fails later")
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (j,)).fetchone()[0], "new")
        self.assertEqual([e for e in self._events() if e["kind"] == "job_status"], [])


if __name__ == "__main__":
    import unittest
    unittest.main()
