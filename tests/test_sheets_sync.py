"""Sheet sync against a fake Apps Script web app on http.server (design 7.4): chunking, overlap re-scan,
deletes, watermark advance only on success, redirect handling, HTML-login detection, edit application and
acknowledgement, idempotency, secrets handling."""
from __future__ import annotations

import io
import json
import os
import secrets
import stat
import types
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import canon, cli, sheets
from jobhunter import sheets_labels as L
from jobhunter.commands import sheet as sheet_cmd
from jobhunter.errors import Denied
from tests.fakes.u5 import FakeHandlers, config, seed
from tests.fixtures.sheets.fake_webapp import FakeWebApp
from tests.helpers import HomeTestCase


class SyncCase(HomeTestCase):
    def setUp(self):
        super().setUp()
        os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
        self.s = seed(self.conn, self.clock)
        self.secret = secrets.token_hex(32)
        self.app = FakeWebApp(self.secret).__enter__()
        self.sheet = self.app.sheet
        self.cfg = config()
        self.handlers = FakeHandlers()

    def tearDown(self):
        self.app.__exit__(None, None, None)
        super().tearDown()

    def connect(self):
        sheets.save_sheet_secrets(self.app.url, self.secret)

    def sync(self, **kw):
        kw.setdefault("config", self.cfg)
        kw.setdefault("handlers", self.handlers)
        return sheets.sync(self.conn, **kw)

    def watermark(self, tab):
        row = self.conn.execute("SELECT watermark FROM sheet_state WHERE tab = ?", (tab,)).fetchone()
        return row[0] if row else None


class TestConnectAndSecrets(SyncCase):
    def test_connect_pings_stores_configures_and_full_syncs(self):
        with open(os.path.join(self.home.dir, "private", "secrets.json"), "w") as fh:
            json.dump({"gmail_app_password": "kept-as-is"}, fh)
        with mock.patch.object(sheets.S, "load_config", return_value=config(timezone="Asia/Kolkata")):
            out = sheets.connect(self.conn, self.app.url, self.secret.upper())
        self.assertEqual(out["app"], "openclaw-job-hunter")
        self.assertEqual(out["tz"], "Asia/Kolkata")
        self.assertEqual(self.sheet.tz, "Asia/Kolkata")
        self.assertTrue(out["sync"]["full"])
        path = os.path.join(self.home.dir, "private", "secrets.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            stored = json.load(fh)
        self.assertEqual(stored["sheet_secret"], self.secret)
        self.assertEqual(stored["gmail_app_password"], "kept-as-is")
        self.assertIn("JAAAAAA2", self.sheet.tabs["jobs"])
        self.assertEqual(self.sheet.violations, [])
        # the secret travels only in the body, never in the URL, and never comes back in the result
        self.assertNotIn(self.secret, json.dumps(out))

    def test_bad_inputs_are_refused_before_any_request(self):
        self.assertRaises(Denied, sheets.connect, self.conn, "http://example.com/exec", self.secret)
        self.assertRaises(Denied, sheets.connect, self.conn, "https://script.google.com/macros/s/x/edit", self.secret)
        self.assertRaises(Denied, sheets.connect, self.conn, self.app.url, "short")
        self.assertEqual(self.sheet.requests, [])
        good = "https://script.google.com/macros/s/" + "abc" + "/exec"   # built at run time: no id-like literal
        self.assertEqual(sheets.validate_url(good), good)

    def test_wrong_secret_is_refused_and_nothing_stored(self):
        with self.assertRaises(Denied) as cm:
            sheets.connect(self.conn, self.app.url, secrets.token_hex(32))
        self.assertEqual(cm.exception.code, "E_SHEET_ACCESS")
        self.assertFalse(sheets.is_connected())

    def test_html_login_page_means_access_is_not_anyone(self):
        self.connect()
        self.sheet.html_login = True
        with self.assertRaises(Denied) as cm:
            sheets.ping()
        self.assertEqual(cm.exception.code, "E_SHEET_ACCESS")
        self.assertIn("Who has access", cm.exception.message)
        with self.assertRaises(Denied):
            self.sync()
        err = self.conn.execute("SELECT last_error FROM sheet_state WHERE tab = '_all'").fetchone()[0]
        self.assertIn("E_SHEET_ACCESS", err)
        self.assertIsNone(self.watermark("jobs"))

    def test_schema_mismatch_explains_the_redeploy(self):
        self.connect()
        self.sheet.schema_version = 1
        with self.assertRaises(Denied) as cm:
            sheets.ping()
        self.assertEqual(cm.exception.code, "E_SHEET_ERROR")
        self.assertIn("New version", cm.exception.message)

    def test_not_connected_or_disabled_is_a_quiet_skip(self):
        self.assertEqual(self.sync()["skipped"], "not_connected")
        self.connect()
        self.assertEqual(self.sync(config=config(sheets__enabled=False))["pushed"], {})
        self.assertEqual(self.sheet.requests, [])

    def test_unreachable_web_app_is_a_network_error(self):
        sheets.save_sheet_secrets("http://127.0.0.1:9/macros/s/x/exec", self.secret)
        with self.assertRaises(Denied) as cm:
            sheets.ping()
        self.assertEqual(cm.exception.code, "E_NETWORK")


class TestSyncProtocol(SyncCase):
    def setUp(self):
        super().setUp()
        self.connect()

    def test_full_sync_pushes_every_tab_and_renders(self):
        out = self.sync(full=True)
        self.assertEqual(self.sheet.violations, [])
        for tab in ("approvals", "jobs", "skipped", "applications", "outreach", "followups", "qc", "daily", "alerts"):
            self.assertTrue(self.sheet.tabs[tab], tab)
            self.assertTrue(self.watermark(tab), tab)
        self.assertEqual(out["pushed"]["jobs"], 3)
        dash = self.sheet.render["dashboard"]
        self.assertEqual(dash["undelivered_high"], 1)
        self.assertTrue(dash["agent_state"].startswith("Stopped"))
        self.assertEqual(self.sheet.render["start"]["agent_state"], dash["agent_state"])
        settings = self.sheet.render["settings"]
        self.assertTrue(any(r[0] == "Approval mode" for r in settings))
        # rows of 4 strings plus an optional style; the browser consent block is always there
        self.assertTrue(all(len(r) in (4, 5) and all(isinstance(x, str) for x in r) for r in settings))
        self.assertIn(["Browser sites (your Chrome logins)", "", "", "", "section"], settings)
        req = self.sheet.sync_requests()[-1]
        self.assertEqual(req["schema_version"], L.SCHEMA_VERSION)
        self.assertEqual(req["client"], L.CLIENT)

    def test_incremental_sync_uses_the_overlap_and_skips_unchanged_rows(self):
        self.sync(full=True)
        self.clock.advance(minutes=30)
        out = self.sync()   # rows stamped inside the overlap window before the watermark are sent once more
        self.assertLessEqual(out["pushed"].get("jobs", 0), 3)
        self.clock.advance(minutes=30)
        n0 = len(self.sheet.sync_requests())
        out = self.sync()
        self.assertEqual({k: v for k, v in out["pushed"].items() if k != "daily"}, {})
        self.assertEqual(len(self.sheet.sync_requests()), n0 + 1)
        # a change 1 second after the watermark is caught (stamped before commit, 1 second resolution)
        wm = self.watermark("jobs")
        self.conn.execute("UPDATE jobs SET title = 'Lead Data Analyst', updated_at = ? WHERE id = ?",
                          (canon.ts_add(wm, seconds=-5), self.s.j_good))
        out = self.sync()
        self.assertEqual(out["pushed"].get("jobs"), 1)
        self.assertEqual(self.sheet.tabs["jobs"]["JAAAAAA2"]["role"], "Lead Data Analyst")

    def test_chunks_respect_max_ops(self):
        self.sync(full=True, config=config(sheets__max_ops_per_post=5))
        reqs = self.sheet.sync_requests()
        self.assertGreater(len(reqs), 3)
        for r in reqs:
            self.assertLessEqual(len(r["ops"]) + len(r["deletes"]), 5)
        self.assertIn("render", reqs[-1])
        self.assertTrue(all("render" not in r for r in reqs[:-1]))
        self.assertEqual(self.sheet.violations, [])

    def test_watermark_advances_only_after_success(self):
        cfg = config(sheets__max_ops_per_post=5)
        self.sheet.fail_on = {2: {"ok": False, "error": "busy"}}
        with self.assertRaises(Denied) as cm:
            self.sync(full=True, config=cfg)
        self.assertEqual(cm.exception.code, "E_SHEET_ERROR")
        first = self.sheet.sync_requests()[0]
        first_tabs = {o["tab"] for o in first["ops"]}
        failed = set(cm.exception.data["failed_tabs"])
        self.assertTrue(failed)
        for tab in failed:
            self.assertIsNone(self.watermark(tab), tab)
        for tab in first_tabs - failed:
            self.assertIsNotNone(self.watermark(tab), tab)
        row = self.conn.execute("SELECT last_error FROM sheet_state WHERE tab = ?", (sorted(failed)[0],)).fetchone()
        self.assertIn("busy", row[0])
        # the next run sends the failed tabs again and clears the error
        self.sheet.fail_on = {}
        out = self.sync(config=cfg)
        for tab in failed:
            self.assertIsNotNone(self.watermark(tab), tab)
            self.assertIn(tab, out["pushed"])
        self.assertIsNone(self.conn.execute("SELECT last_error FROM sheet_state WHERE tab = '_all'").fetchone()[0])

    def test_too_large_splits_the_chunk(self):
        self.sheet.too_large_over = 3
        out = self.sync(full=True)
        self.assertEqual(sum(len(r["ops"]) for r in self.sheet.sync_requests() if len(r["ops"]) <= 3),
                         sum(out["pushed"].values()))
        self.assertIn("JAAAAAA2", self.sheet.tabs["jobs"])

    def test_deletes_are_sent_once_and_marked_done(self):
        self.sheet.tabs["skipped"]["JAAAAAA7"] = {"role": "BI Developer"}   # an old row already in the sheet
        with sheets.db.tx(self.conn):
            sheets.queue_delete(self.conn, "outreach", "TAAAAAAAAAAA9")
        out = self.sync()
        self.assertNotIn("JAAAAAA7", self.sheet.tabs["skipped"])
        sent = [d for r in self.sheet.sync_requests() for d in r["deletes"]]
        self.assertIn({"tab": "skipped", "id": "JAAAAAA7"}, sent)
        self.assertIn({"tab": "outreach", "id": "TAAAAAAAAAAA9"}, sent)
        self.assertEqual(out["deleted"], 1)
        left = self.conn.execute("SELECT count(*) FROM sheet_deletes WHERE done_at IS NULL").fetchone()[0]
        self.assertEqual(left, 0)
        self.sync()
        self.assertEqual([d for d in self.sheet.sync_requests()[-1]["deletes"]], [])

    def test_editable_columns_are_never_overwritten(self):
        self.sync(full=True)
        self.sheet.tabs["applications"]["JAAAAAA6"]["notes"] = "Called them on Monday"
        self.conn.execute("UPDATE applications SET confirmation = 'Received', updated_at = ? WHERE id = ?",
                          (self.clock.advance(minutes=30), self.s.app))
        self.sync()
        row = self.sheet.tabs["applications"]["JAAAAAA6"]
        self.assertEqual(row["proof"], "Received")
        self.assertEqual(row["notes"], "Called them on Monday")

    def test_dry_run_sends_nothing(self):
        out = self.sync(dry_run=True, full=True)
        self.assertTrue(out["dry_run"])
        self.assertGreater(out["ops"], 5)
        self.assertEqual(self.sheet.requests, [])
        self.assertIsNone(self.watermark("jobs"))

    def test_single_tab(self):
        out = self.sync(full=True, tab="alerts")
        self.assertEqual(list(out["pushed"]), ["alerts"])
        self.assertEqual(self.sheet.tabs["jobs"], {})
        self.assertRaises(Denied, self.sync, tab="nope")


class TestEdits(SyncCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.sync(full=True)

    def test_edits_are_applied_once_and_acknowledged(self):
        e1 = self.sheet.human_edit("approvals", "DAAAAAA2", "decision", "Approve")
        e2 = self.sheet.human_edit("jobs", "JAAAAAA4", "your_call", "Apply anyway")
        e3 = self.sheet.human_edit("skipped", "JAAAAAA3", "your_call", "Apply anyway")
        e4 = self.sheet.human_edit("applications", "JAAAAAA6", "outcome", "Interview")
        e5 = self.sheet.human_edit("followups", self.s.thread_key, "outcome", "Offer")
        e6 = self.sheet.human_edit("outreach", "TAAAAAAAAAAA2", "notes", "Met at a meetup")
        e7 = self.sheet.human_edit("applications", "JAAAAAA6", "notes", "Recruiter is friendly")
        e8 = self.sheet.human_edit("approvals", "DAAAAAA2", "decision", "")
        out = self.sync()
        self.assertEqual(out["edits"]["applied"], 7)
        self.assertEqual(out["edits"]["ignored"], 1)
        self.assertIn(("approve", "DAAAAAA2", "human:sheet"), self.handlers.calls)
        self.assertIn(("set_human_call", "JAAAAAA4", "apply_anyway", "human:sheet"), self.handlers.calls)
        self.assertIn(("set_human_call", "JAAAAAA3", "apply_anyway", "human:sheet"), self.handlers.calls)
        self.assertIn(("set_outcome", None, "JAAAAAA6", "interview", "human:sheet"), self.handlers.calls)
        self.assertIn(("set_outcome", self.s.thread_key, None, "offer", "human:sheet"), self.handlers.calls)
        self.assertIn(("set_application_notes", "JAAAAAA6", "Recruiter is friendly", "human:sheet"),
                      self.handlers.calls)
        # acknowledged in the next request, so the sheet's pending list is empty
        self.assertEqual(self.sheet.edits, [])
        acked = set(self.sheet.sync_requests()[-1]["ack_edits"])
        self.assertEqual(acked, {e1, e2, e3, e4, e5, e6, e7, e8})
        note = self.conn.execute("SELECT value, result FROM sheet_edits_applied WHERE edit_id = ?", (e6,)).fetchone()
        self.assertEqual(tuple(note), ("Met at a meetup", "note_kept"))
        # an edit the sheet sends again (ack lost) is not applied twice
        n = len(self.handlers.calls)
        ack, counts = sheets.apply_edits(self.conn, [{"edit_id": e1, "tab": "approvals", "row_id": "DAAAAAA2",
                                                      "col": "decision", "value": "Approve"}], self.handlers)
        self.assertEqual((ack, counts["already"]), ([e1], 1))
        self.assertEqual(len(self.handlers.calls), n)

    def test_outcome_choices_are_checked_per_tab(self):
        # a stale sheet may still offer "Withdrawn" on Follow-ups; threads cannot store it
        e1 = self.sheet.human_edit("followups", self.s.thread_key, "outcome", "Withdrawn")
        e2 = self.sheet.human_edit("applications", "JAAAAAA6", "outcome", "Referred")
        e3 = self.sheet.human_edit("followups", self.s.thread_key, "outcome", "Referred")
        out = self.sync()
        self.assertEqual((out["edits"]["ignored"], out["edits"]["applied"], out["edits"]["refused"]), (2, 1, 0))
        outcome_calls = [c for c in self.handlers.calls if c[0] == "set_outcome"]
        self.assertEqual(outcome_calls, [("set_outcome", self.s.thread_key, None, "referred", "human:sheet")])
        res = dict(self.conn.execute("SELECT edit_id, result FROM sheet_edits_applied").fetchall())
        self.assertEqual((res[e1], res[e2], res[e3]), ("ignored_value", "ignored_value", "outcome_referred"))
        self.assertEqual(self.sheet.edits, [])

    def test_refused_edit_is_recorded_acknowledged_and_explained(self):
        self.handlers.fail["approve"] = Denied("E_DRAFT_EXPIRED", "this approval expired")
        eid = self.sheet.human_edit("approvals", "DAAAAAA2", "decision", "Approve")
        out = self.sync()
        self.assertEqual(out["edits"]["refused"], 1)
        res = self.conn.execute("SELECT result FROM sheet_edits_applied WHERE edit_id = ?", (eid,)).fetchone()[0]
        self.assertEqual(res, "refused:E_DRAFT_EXPIRED")
        note = self.conn.execute("SELECT text FROM notifications WHERE dedupe_key = ?", ("sheet_edit:" + eid,)).fetchone()
        self.assertIn("this approval expired", note[0])
        self.assertEqual(self.sheet.edits, [])

    def test_owner_bug_defers_the_edit_without_losing_it(self):
        self.handlers.fail["set_outcome"] = Denied("E_LOCKED", "busy")
        eid = self.sheet.human_edit("applications", "JAAAAAA6", "outcome", "Offer")
        out = self.sync()
        self.assertEqual(out["edits"]["deferred"], 1)
        self.assertEqual([e["edit_id"] for e in self.sheet.edits], [eid])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sheet_edits_applied WHERE edit_id = ?", (eid,)).fetchone())
        del self.handlers.fail["set_outcome"]
        self.sync()
        self.assertEqual(self.sheet.edits, [])

    def test_invalid_edits_are_dropped(self):
        ack, counts = sheets.apply_edits(self.conn, [
            {"edit_id": "11111111-2222", "tab": "jobs", "row_id": "JAAAAAA2", "col": "status", "value": "Applied"},
            {"edit_id": "11111111-3333", "tab": "approvals", "row_id": "DAAAAAA2", "col": "decision",
             "value": "=HYPERLINK(1)"},
            {"edit_id": "x", "tab": "jobs"}, "junk"], self.handlers)
        self.assertEqual(counts["invalid"], 3)
        self.assertEqual(counts["ignored"], 1)
        self.assertEqual(self.handlers.calls, [])
        self.assertEqual(ack, ["11111111-2222", "11111111-3333"])

    def test_default_handlers_call_the_owner_functions(self):
        h = sheets.Handlers()
        calls = []
        fake_threads = types.SimpleNamespace(set_outcome=lambda conn, **kw: calls.append(kw))
        fake_approvals = types.SimpleNamespace(approve=lambda conn, uid, by: calls.append(("approve", uid, by)),
                                               skip=lambda conn, uid, reason, by: calls.append(("skip", uid, by)))
        fake_jobs = types.SimpleNamespace(set_human_call=lambda conn, uid, call, by: calls.append((uid, call, by)))
        with mock.patch.dict("sys.modules", {"jobhunter.threads": fake_threads, "jobhunter.approvals": fake_approvals,
                                             "jobhunter.jobs": fake_jobs}):
            h.set_outcome(self.conn, job_uid="JAAAAAA6", outcome="offer", by="human:sheet")
            h.approve(self.conn, "DAAAAAA2", "human:sheet")
            h.skip(self.conn, "DAAAAAA2", "no", "human:sheet")
            h.set_human_call(self.conn, "JAAAAAA4", "never", "human:sheet")
            with sheets.db.tx(self.conn):   # no set_notes in threads: the notes column is written directly
                h.set_application_notes(self.conn, "JAAAAAA6", "note text", "human:sheet")
        self.assertEqual(calls, [{"thread_key": None, "job_uid": "JAAAAAA6", "outcome": "offer", "by": "human:sheet"},
                                 ("approve", "DAAAAAA2", "human:sheet"), ("skip", "DAAAAAA2", "human:sheet"),
                                 ("JAAAAAA4", "never", "human:sheet")])
        notes = self.conn.execute("SELECT notes FROM applications WHERE id = ?", (self.s.app,)).fetchone()[0]
        self.assertEqual(notes, "note text")
        # a missing owner module defers the edit instead of failing the sync
        with mock.patch.dict("sys.modules", {"jobhunter.approvals": None}):
            ack, counts = sheets.apply_edits(self.conn, [{"edit_id": "22222222-aaaa", "tab": "approvals",
                                                          "row_id": "DAAAAAA2", "col": "decision",
                                                          "value": "Approve"}], h)
        self.assertEqual((ack, counts["deferred"]), ([], 1))


class TestEditsWithRealOwners(SyncCase):
    """The default Handlers against the real U2 and U6 functions, when those modules are present."""

    def test_your_call_and_outcome_through_the_owning_units(self):
        try:
            import jobhunter.jobs as jobs_mod
            import jobhunter.threads as threads_mod
        except ImportError as exc:
            self.skipTest("owner module missing: %s" % exc)
        if not (hasattr(jobs_mod, "set_human_call") and hasattr(threads_mod, "set_outcome")):
            self.skipTest("owner functions missing")
        ack, counts = sheets.apply_edits(self.conn, [
            {"edit_id": "bbbbbbbb-0001", "tab": "jobs", "row_id": "JAAAAAA4", "col": "your_call",
             "value": "Apply anyway"},
            {"edit_id": "bbbbbbbb-0002", "tab": "applications", "row_id": "JAAAAAA6", "col": "outcome",
             "value": "Interview"}])
        self.assertEqual(len(ack), 2)
        self.assertEqual(counts["applied"], 2, counts)
        job = self.conn.execute("SELECT status, human_call FROM jobs WHERE job_uid = 'JAAAAAA4'").fetchone()
        self.assertEqual(tuple(job), ("eligible", "apply_anyway"))
        self.assertEqual(self.conn.execute("SELECT outcome FROM applications").fetchone()[0], "interview")

    def test_every_followups_outcome_choice_is_storable_by_threads(self):
        try:
            import jobhunter.threads as threads_mod
        except ImportError as exc:
            self.skipTest("owner module missing: %s" % exc)
        if not hasattr(threads_mod, "THREAD_OUTCOMES"):
            self.skipTest("owner constants missing")
        for label in L.editable_columns("followups")["outcome"]:
            self.assertIn(L.OUTCOME_CODE[label], threads_mod.THREAD_OUTCOMES, label)
        for label in L.editable_columns("applications")["outcome"]:
            self.assertIn(L.OUTCOME_CODE[label], threads_mod.APPLICATION_OUTCOMES, label)
        self.assertNotIn("Withdrawn", L.editable_columns("followups")["outcome"])


class TestSheetCli(SyncCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        rc = cli.main(list(argv), env={}, stdin=io.StringIO(""), stdout=out, modules=[sheet_cmd])
        return rc, out.getvalue().strip()

    def test_sync_and_ping_as_system_caller(self):
        self.connect()
        with mock.patch.object(sheets.S, "load_config", return_value=self.cfg), \
                mock.patch.object(sheets, "Handlers", FakeHandlers):
            rc, text = self.run_cli("sheet", "sync", "--quiet")
            self.assertEqual((rc, text), (0, "NO_REPLY"))
            rc, text = self.run_cli("sheet", "ping")
        env = json.loads(text)
        self.assertEqual(rc, 0)
        self.assertEqual(env["data"]["app"], "openclaw-job-hunter")
        self.assertNotIn(self.secret, text)

    def test_not_connected_is_nothing_to_do(self):
        rc, text = self.run_cli("sheet", "sync")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(text)["code"], "NOTHING_TO_DO")

    def test_connect_is_human_only(self):
        rc, text = self.run_cli("sheet", "connect")
        self.assertEqual(rc, 11)
        self.assertIn(json.loads(text)["code"], ("E_HUMAN_ONLY", "E_CALLER_NOT_ALLOWED"))

    def test_access_error_exit_code(self):
        self.connect()
        self.sheet.html_login = True
        with mock.patch.object(sheets.S, "load_config", return_value=self.cfg):
            rc, text = self.run_cli("sheet", "sync")
        self.assertEqual(rc, 12)
        self.assertEqual(json.loads(text)["code"], "E_SHEET_ACCESS")


if __name__ == "__main__":
    unittest.main()
