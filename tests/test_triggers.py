"""Schema triggers and unique indexes (design 2.1, 2.2, 13.2): strict fallbacks with an empty meta table
(B4), dedup slots, first-touch and LinkedIn sequence rules, follow-up binding, company and contact
blocks, action/job/draft status graphs, updated_at stamps, concurrent writers."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time

import tests  # noqa: F401
from jobhunter import canon, db, paths
from jobhunter.errors import map_sqlite_error
from tests.helpers import (HomeTestCase, clear_meta, insert_action, insert_company, insert_contact, insert_draft,
                           insert_job, insert_precheck, insert_thread, raw_meta)


class TriggerCase(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.k = insert_company(self.conn, name="Kestrel Commerce", domain="kestrel.example")
        self.p1 = insert_contact(self.conn, company_id=self.k, full_name="Alex Rivera",
                                 email="alex.rivera@kestrel.example")
        self.p2 = insert_contact(self.conn, company_id=self.k, full_name="Jordan Lee",
                                 email="jordan.lee@kestrel.example")

    def attempt(self, **kw):
        """Insert one action inside a transaction that is always rolled back; returns None when the
        database accepts it, else the mapped Denied."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            insert_action(self.conn, **kw)
            return None
        except sqlite3.DatabaseError as exc:
            return map_sqlite_error(exc)
        finally:
            self.conn.execute("ROLLBACK")

    def assertAllowed(self, **kw):
        d = self.attempt(**kw)
        self.assertIsNone(d, d and "%s: %s" % (d.code, d.data))

    def assertRefused(self, code, **kw):
        d = self.attempt(**kw)
        self.assertIsNotNone(d, "expected %s" % code)
        self.assertEqual(d.code, code, d.data)


# ---------------------------------------------------------------- B4
class TestStrictFallbacks(TriggerCase):
    def test_every_meta_read_has_coalesce(self):
        with open(paths.SCHEMA_FILE) as fh:
            schema = fh.read()
        idx = schema.find("FROM meta")
        count = 0
        while idx != -1:
            j = schema.rfind("(SELECT", 0, idx)
            self.assertEqual(schema[j - 9:j], "COALESCE(", schema[j - 40:idx + 60])
            count += 1
            idx = schema.find("FROM meta", idx + 1)
        self.assertEqual(count, 9)

    def test_company_email_cooldown(self):
        insert_action(self.conn, kind="cold_email", company_id=self.k, contact_id=self.p1,
                      reserved_at=self.clock.ago(days=200))
        self.assertAllowed(kind="cold_email", company_id=self.k, contact_id=self.p2)       # meta: 90 days
        clear_meta(self.conn)
        self.assertRefused("E_COMPANY_COOLDOWN", kind="cold_email", company_id=self.k, contact_id=self.p2)
        self.assertRefused("E_COMPANY_COOLDOWN", kind="application_email", company_id=self.k, contact_id=self.p2)
        for hostile in ("", "abc", "0", "-5"):
            raw_meta(self.conn, "company_email_cooldown_days", hostile)
            self.assertRefused("E_COMPANY_COOLDOWN", kind="cold_email", company_id=self.k, contact_id=self.p2)
        raw_meta(self.conn, "company_email_cooldown_days", "30")
        self.assertAllowed(kind="cold_email", company_id=self.k, contact_id=self.p2)

    def test_cooldown_counts_blocking_statuses_only(self):
        insert_action(self.conn, kind="cold_email", status="failed", fail_reason="not_attempted",
                      company_id=self.k, contact_id=self.p1, reserved_at=self.clock.ago(days=1))
        self.assertAllowed(kind="cold_email", company_id=self.k, contact_id=self.p2)
        insert_action(self.conn, kind="cold_email", status="unknown", company_id=self.k, contact_id=self.p1,
                      reserved_at=self.clock.ago(days=1))
        self.assertRefused("E_COMPANY_COOLDOWN", kind="cold_email", company_id=self.k, contact_id=self.p2)

    def test_company_app_caps(self):
        j1, j2, j3 = (insert_job(self.conn, company_id=self.k, title=t) for t in ("Data Analyst", "BI Analyst",
                                                                                  "Growth Analyst"))
        insert_action(self.conn, kind="application", company_id=self.k, job_id=j1, reserved_at=self.clock.ago(days=60))
        self.assertAllowed(kind="application", company_id=self.k, job_id=j2)                # meta 1/2/3
        clear_meta(self.conn)
        self.assertRefused("E_COMPANY_APP_CAP", kind="application", company_id=self.k, job_id=j2)
        db.meta_set(self.conn, "company_apps_per_90d", "3", "system")
        self.assertAllowed(kind="application", company_id=self.k, job_id=j2)
        insert_action(self.conn, kind="application", company_id=self.k, job_id=j2, reserved_at=self.clock.ago(hours=2))
        self.assertRefused("E_COMPANY_APP_CAP", kind="application", company_id=self.k, job_id=j3)    # 1 per day

    def test_agency_caps(self):
        ag = insert_company(self.conn, name="Harbor Talent Partners", domain="harbor-talent.example", is_agency=1)
        a1 = insert_contact(self.conn, company_id=ag, full_name="Riley Stone", email="riley@harbor-talent.example")
        a2 = insert_contact(self.conn, company_id=ag, full_name="Casey Moor", email="casey@harbor-talent.example")
        insert_action(self.conn, kind="cold_email", company_id=ag, contact_id=a1, reserved_at=self.clock.ago(days=10))
        # agencies skip the 90-day company cooldown; meta allows 3 per 30 days
        self.assertAllowed(kind="cold_email", company_id=ag, contact_id=a2)
        j1, j2 = insert_job(self.conn, company_id=ag), insert_job(self.conn, company_id=ag)
        insert_action(self.conn, kind="application", company_id=ag, job_id=j1, reserved_at=self.clock.ago(hours=1))
        self.assertAllowed(kind="application", company_id=ag, job_id=j2)                      # 2 per day
        clear_meta(self.conn)
        self.assertRefused("E_AGENCY_CAP", kind="cold_email", company_id=ag, contact_id=a2)
        self.assertRefused("E_AGENCY_CAP", kind="application", company_id=ag, job_id=j2)

    def test_company_li_cap(self):
        insert_action(self.conn, kind="li_invite", company_id=self.k, contact_id=self.p1,
                      reserved_at=self.clock.ago(days=3))
        self.assertAllowed(kind="li_invite", company_id=self.k, contact_id=self.p2)           # meta: 2 per 7 days
        clear_meta(self.conn)
        self.assertRefused("E_COMPANY_LI_CAP", kind="li_invite", company_id=self.k, contact_id=self.p2)
        self.assertRefused("E_COMPANY_LI_CAP", kind="inmail", company_id=self.k, contact_id=self.p2, li_msg_seq=1)
        self.clock.advance(days=5)
        self.assertAllowed(kind="li_invite", company_id=self.k, contact_id=self.p2)           # window rolled


# ---------------------------------------------------------------- dedup slots
class TestUniqueSlots(TriggerCase):
    def test_one_first_touch_per_person_any_channel(self):
        insert_action(self.conn, kind="cold_email", contact_id=self.p1, recipient="alex.rivera@kestrel.example")
        self.assertRefused("E_DUP_PERSON", kind="li_invite", contact_id=self.p1)
        self.assertRefused("E_DUP_PERSON", kind="inmail", contact_id=self.p1, li_msg_seq=1)
        self.assertRefused("E_DUP_PERSON", kind="application_email", contact_id=self.p1)
        self.assertRefused("E_DUP_PERSON", kind="li_message", contact_id=self.p1, li_msg_seq=1)

    def test_failed_frees_unknown_blocks(self):
        insert_action(self.conn, kind="cold_email", contact_id=self.p1, status="failed", fail_reason="not_attempted")
        self.assertAllowed(kind="cold_email", contact_id=self.p1)
        for status in ("unknown", "failed_after_click", "imported", "armed"):
            with self.subTest(status=status):
                pid = insert_contact(self.conn, email="%s@example.com" % status)
                insert_action(self.conn, kind="cold_email", contact_id=pid, status=status)
                self.assertRefused("E_DUP_PERSON", kind="li_invite", contact_id=pid)

    def test_application_job_draft_token_precheck(self):
        j = insert_job(self.conn)
        d = insert_draft(self.conn, kind="application_package", send_route="browser", job_id=j)
        pc = insert_precheck(self.conn, kind="application", platform="greenhouse", job_id=j, source="agent")
        insert_action(self.conn, kind="application", job_id=j, draft_id=d, precheck_id=pc,
                      agent_id="jobhunter-applier", status="reserved")
        self.assertRefused("E_DUP_JOB", kind="application", job_id=j)
        self.assertRefused("E_DUP_JOB", kind="application_email", job_id=j)
        j2 = insert_job(self.conn)
        self.assertRefused("E_DUP_DRAFT", kind="application", job_id=j2, draft_id=d)
        self.assertRefused("E_TOKEN_OPEN", kind="application", job_id=j2, agent_id="jobhunter-applier",
                           status="armed")
        self.assertAllowed(kind="application", job_id=j2, agent_id="jobhunter-applier", status="sent")
        self.assertAllowed(kind="application", job_id=j2, agent_id="jobhunter-outreach", status="reserved")
        self.assertRefused("E_PRECHECK_STALE", kind="application", job_id=j2, precheck_id=pc)

    def test_one_followup_per_thread(self):
        a1 = insert_action(self.conn, kind="cold_email", company_id=self.k, contact_id=self.p1,
                           recipient="alex.rivera@kestrel.example", reserved_at=self.clock.ago(days=5))
        insert_thread(self.conn, a1, contact_id=self.p1, company_id=self.k)
        key = self.conn.execute("SELECT thread_key FROM threads").fetchone()[0]
        kw = dict(kind="followup_email", company_id=self.k, contact_id=self.p1, thread_key=key,
                  recipient="alex.rivera@kestrel.example")
        insert_action(self.conn, **kw)
        self.assertRefused("E_DUP_THREAD_FOLLOWUP", **kw)

    def test_open_draft_indexes(self):
        insert_draft(self.conn, kind="cold_email", status="drafted", company_id=self.k, contact_id=self.p1)
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            insert_draft(self.conn, kind="application_email", status="qc_passed", company_id=self.k,
                         contact_id=self.p2)
        self.assertEqual(map_sqlite_error(cm.exception).code, "E_DUP_DRAFT")
        insert_draft(self.conn, kind="cold_email", status="sent", company_id=self.k, contact_id=self.p2)
        p3 = insert_contact(self.conn, email="p3@example.com")
        insert_draft(self.conn, kind="li_invite_note", status="awaiting_approval", contact_id=p3,
                     send_route="browser", channel="li_connect", subject=None)
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            insert_draft(self.conn, kind="cold_email", status="drafted", contact_id=p3)
        self.assertEqual(map_sqlite_error(cm.exception).code, "E_DUP_DRAFT")


# ---------------------------------------------------------------- per-action rules
class TestActionRules(TriggerCase):
    def test_first_touch_rules(self):
        self.assertRefused("E_INTERNAL", kind="cold_email", contact_id=self.p1, first_touch=0)
        self.assertRefused("E_INTERNAL", kind="li_invite", contact_id=self.p1, first_touch=0)
        self.assertRefused("E_INTERNAL", kind="application", job_id=insert_job(self.conn), first_touch=1)
        self.assertRefused("E_INTERNAL", kind="application_email", contact_id=self.p1, first_touch=0)
        inbox = insert_contact(self.conn, full_name="Careers Team", email="careers@kestrel.example",
                               role_type="role_inbox")
        self.assertRefused("E_INTERNAL", kind="application_email", contact_id=inbox, first_touch=1)
        self.assertAllowed(kind="application_email", contact_id=inbox, first_touch=0)
        self.assertAllowed(kind="application_email", contact_id=None, first_touch=0)
        self.assertRefused("E_INTERNAL", kind="li_message", contact_id=self.p1, first_touch=0, li_msg_seq=1)
        inv = insert_action(self.conn, kind="li_invite", contact_id=self.p1)
        insert_thread(self.conn, inv, contact_id=self.p1, channel="linkedin", state="invite_accepted")
        self.assertRefused("E_INTERNAL", kind="li_message", contact_id=self.p1, first_touch=1, li_msg_seq=1)
        self.assertAllowed(kind="li_message", contact_id=self.p1, first_touch=0, li_msg_seq=1)

    def test_li_seq_rules(self):
        self.assertRefused("E_DUP_LI_TOUCH", kind="li_message", contact_id=self.p1)            # seq missing
        self.assertRefused("E_DUP_LI_TOUCH", kind="li_invite", contact_id=self.p1, li_note=1)  # note needs seq
        self.assertRefused("E_DUP_LI_TOUCH", kind="li_invite", contact_id=self.p1, li_msg_seq=1)
        self.assertRefused("E_DUP_LI_TOUCH", kind="application", job_id=insert_job(self.conn), li_msg_seq=1)
        self.assertRefused("E_DUP_LI_TOUCH", kind="li_invite", contact_id=self.p1, li_note=1, li_msg_seq=2)
        insert_action(self.conn, kind="li_invite", contact_id=self.p1, li_note=1, li_msg_seq=1, status="failed",
                      fail_reason="not_attempted")
        inv = insert_action(self.conn, kind="li_invite", contact_id=self.p1, li_note=1, li_msg_seq=1)
        insert_thread(self.conn, inv, contact_id=self.p1, channel="linkedin", state="invite_accepted")
        insert_action(self.conn, kind="li_message", contact_id=self.p1, li_msg_seq=2)
        open_key = "li:" + canon.new_uid("P")
        insert_thread(self.conn, inv, contact_id=self.p1, channel="linkedin", state="open", thread_key=open_key)
        # third message-bearing touch: refused whatever sequence number the code would pass
        for seq in (2, 3):
            self.assertRefused("E_DUP_LI_TOUCH", kind="li_followup", contact_id=self.p1, thread_key=open_key,
                               li_msg_seq=seq)

    def test_followup_binding(self):
        a1 = insert_action(self.conn, kind="cold_email", company_id=self.k, contact_id=self.p1,
                           recipient="alex.rivera@kestrel.example", reserved_at=self.clock.ago(days=5))
        tid = insert_thread(self.conn, a1, contact_id=self.p1, company_id=self.k)
        key = self.conn.execute("SELECT thread_key FROM threads WHERE id = ?", (tid,)).fetchone()[0]
        ok = dict(kind="followup_email", company_id=self.k, contact_id=self.p1, thread_key=key,
                  recipient="alex.rivera@kestrel.example")
        self.assertAllowed(**ok)
        for bad in (dict(contact_id=self.p2), dict(recipient="alex.rivera+x@kestrel.example"), dict(recipient=None),
                    dict(company_id=None), dict(thread_key=None), dict(thread_key="em:TAAAAAAAAAAA")):
            with self.subTest(bad=bad):
                self.assertRefused("E_FOLLOWUP_BINDING", **dict(ok, **bad))
        self.conn.execute("UPDATE threads SET state = 'replied' WHERE id = ?", (tid,))
        self.assertRefused("E_FOLLOWUP_BINDING", **ok)
        self.conn.execute("UPDATE threads SET state = 'open', followup_action_id = ? WHERE id = ?", (a1, tid))
        self.assertRefused("E_FOLLOWUP_BINDING", **ok)

    def test_no_merged_refs(self):
        k2 = insert_company(self.conn, name="Kestrel Commerce Ltd", domain=None)
        self.conn.execute("UPDATE companies SET merged_into = ? WHERE id = ?", (self.k, k2))
        self.assertRefused("E_INTERNAL", kind="cold_email", company_id=k2, contact_id=self.p1)
        p3 = insert_contact(self.conn, email="old@example.com", merged_into=self.p1)
        self.assertRefused("E_INTERNAL", kind="cold_email", contact_id=p3)

    def test_company_blocked_and_contact_dnc(self):
        for state in ("active_thread", "do_not_contact"):
            with self.subTest(state=state):
                self.conn.execute("UPDATE companies SET contact_state = ? WHERE id = ?", (state, self.k))
                self.assertRefused("E_COMPANY_BLOCKED", kind="cold_email", company_id=self.k, contact_id=self.p1)
                self.assertRefused("E_COMPANY_BLOCKED", kind="li_invite", company_id=self.k, contact_id=self.p1)
                self.assertRefused("E_COMPANY_BLOCKED", kind="application", company_id=self.k,
                                   job_id=insert_job(self.conn, company_id=self.k))
        self.conn.execute("UPDATE companies SET contact_state = 'contacted' WHERE id = ?", (self.k,))
        self.assertAllowed(kind="cold_email", company_id=self.k, contact_id=self.p1)
        self.conn.execute("UPDATE contacts SET do_not_contact = 1 WHERE id = ?", (self.p1,))
        self.assertRefused("E_CONTACT_DNC", kind="cold_email", contact_id=self.p1)
        self.assertRefused("E_CONTACT_DNC", kind="li_withdraw", contact_id=self.p1)

    def test_reserved_at_format(self):
        self.assertRefused("E_INTERNAL", kind="cold_email", contact_id=self.p1, reserved_at="2026-09-27 05:00:00")


# ---------------------------------------------------------------- status graphs
class TestStatusGraphs(TriggerCase):
    def _action(self, status, fail_reason=None):
        return insert_action(self.conn, kind="application", job_id=insert_job(self.conn), status=status,
                             fail_reason=fail_reason)

    def _move(self, aid, status, fail_reason=None):
        try:
            self.conn.execute("UPDATE actions SET status = ?, fail_reason = ? WHERE id = ?", (status, fail_reason, aid))
            return None
        except sqlite3.DatabaseError as exc:
            return map_sqlite_error(exc).code

    def test_action_graph(self):
        cases = [
            ("reserved", "armed", None, True), ("reserved", "unknown", None, True),
            ("reserved", "sent", None, False), ("reserved", "failed_after_click", None, False),
            ("reserved", "failed", "not_attempted", True), ("reserved", "failed", "precondition_changed", True),
            ("reserved", "failed", "form_blocked_before_submit", True),
            ("reserved", "failed", "observed_text_mismatch", True),
            ("reserved", "failed", "smtp_rejected_before_data", True),
            ("reserved", "failed", "smtp_rejected", False), ("reserved", "failed", None, False),
            ("reserved", "failed", "not_found_twice", False),
            ("armed", "sent", None, True), ("armed", "unknown", None, True),
            ("armed", "failed_after_click", "platform_error_after_click", True),
            ("armed", "failed", "smtp_rejected", True), ("armed", "failed", "not_attempted", False),
            ("armed", "reserved", None, False),
            ("unknown", "sent", None, True), ("unknown", "failed_after_click", None, True),
            ("unknown", "failed", "not_found_twice", True), ("unknown", "failed", "human_confirmed_not_sent", True),
            ("unknown", "failed", "not_attempted", False), ("unknown", "reserved", None, False),
            ("sent", "failed", "human_confirmed_not_sent", False), ("sent", "unknown", None, False),
            ("failed_after_click", "failed", "human_confirmed_not_sent", False),
            ("failed", "reserved", None, False), ("imported", "failed", "human_confirmed_not_sent", False),
        ]
        for old, new, reason, ok in cases:
            with self.subTest(old=old, new=new, reason=reason):
                aid = self._action(old, "not_attempted" if old == "failed" else None)
                got = self._move(aid, new, reason)
                self.assertEqual(got, None if ok else "E_BAD_TRANSITION")

    def test_job_and_draft_graph_samples(self):
        j = insert_job(self.conn, status="new")
        self.conn.execute("UPDATE jobs SET status = 'eval_queued' WHERE id = ?", (j,))
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (j,))
        self.assertEqual(map_sqlite_error(cm.exception).code, "E_BAD_TRANSITION")
        d = insert_draft(self.conn, status="qc_passed")
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.conn.execute("UPDATE drafts SET status = 'sent' WHERE id = ?", (d,))
        self.assertEqual(map_sqlite_error(cm.exception).code, "E_BAD_TRANSITION")
        with self.assertRaises(sqlite3.IntegrityError):   # approved needs approved_by (CHECK)
            self.conn.execute("UPDATE drafts SET status = 'approved' WHERE id = ?", (d,))
        self.conn.execute("UPDATE drafts SET status = 'approved', approved_by = 'human:cli' WHERE id = ?", (d,))


class TestTouchStamps(TriggerCase):
    start_ts = "2020-01-01T00:00:00Z"

    def test_updated_at_stamped(self):
        j = insert_job(self.conn)
        self.conn.execute("UPDATE jobs SET title = 'Senior Data Analyst' WHERE id = ?", (j,))
        stamped = self.conn.execute("SELECT updated_at FROM jobs WHERE id = ?", (j,)).fetchone()[0]
        self.assertNotEqual(stamped, "2020-01-01T00:00:00Z")
        self.assertRegex(stamped, canon.TS_RE)
        self.conn.execute("UPDATE jobs SET title = 'x', updated_at = '2020-02-02T00:00:00Z' WHERE id = ?", (j,))
        self.assertEqual(self.conn.execute("SELECT updated_at FROM jobs WHERE id = ?", (j,)).fetchone()[0],
                         "2020-02-02T00:00:00Z")
        aid = insert_action(self.conn, kind="application", job_id=j, status="reserved")
        self.conn.execute("UPDATE actions SET status = 'armed' WHERE id = ?", (aid,))
        self.assertNotEqual(self.conn.execute("SELECT updated_at FROM actions WHERE id = ?", (aid,)).fetchone()[0],
                            "2020-01-01T00:00:00Z")
        self.conn.execute("INSERT INTO target_skips (target_key, reason, until, created_at, updated_at) "
                          "VALUES ('job:JAAAAAAA', 'no_hook', 'x', 'x', 'x')")
        self.conn.execute("UPDATE target_skips SET drops = 2")
        self.assertNotEqual(self.conn.execute("SELECT updated_at FROM target_skips").fetchone()[0], "x")


# ---------------------------------------------------------------- concurrency
_WRITER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from jobhunter import canon, db, paths
from jobhunter.errors import Denied
paths.use_test_home(sys.argv[2])
kind, company, contact, job, start = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], float(sys.argv[7])
conn = db.connect()
while time.time() < start:
    time.sleep(0.002)
ts = canon.now()
try:
    with db.tx(conn):
        conn.execute(
            "INSERT INTO actions (token, kind, route, first_touch, platform, contact_id, company_id, job_id, status,"
            " reserved_at, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)",
            (canon.new_token(), kind, 'mailer' if kind == 'cold_email' else 'browser', 1 if kind == 'cold_email' else 0,
             'gmail' if kind == 'cold_email' else 'greenhouse', int(contact) or None, int(company) or None,
             int(job) or None, ts, canon.ts_add(ts, minutes=30), ts, ts))
        time.sleep(0.15)
    print("OK")
except Denied as d:
    print(d.code)
"""


class TestConcurrentWriters(TriggerCase):
    def _race(self, kind, company, contacts, job):
        scripts = os.path.join(paths.REPO, "scripts")
        start = time.time() + 1.0
        procs = [subprocess.Popen([sys.executable, "-c", _WRITER, scripts, self.home.dir, kind, str(company),
                                   str(c), str(job), repr(start)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for c in contacts]
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err.decode())
            outs.append(out.decode().strip())
        return sorted(outs)

    def test_same_person(self):
        self.assertEqual(self._race("cold_email", 0, [self.p1, self.p1], 0), ["E_DUP_PERSON", "OK"])

    def test_same_company(self):
        self.assertEqual(self._race("cold_email", self.k, [self.p1, self.p2], 0), ["E_COMPANY_COOLDOWN", "OK"])

    def test_same_job(self):
        j = insert_job(self.conn)
        self.assertEqual(self._race("application", 0, [0, 0], j), ["E_DUP_JOB", "OK"])


if __name__ == "__main__":
    import unittest
    unittest.main()
