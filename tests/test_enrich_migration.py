"""U10 migration 0002_enrich.sql: applies after 0001 on an empty database and on one with contacts; the
trigger fixtures refuse what they must (and allow what they must)."""
from __future__ import annotations

import os
import sqlite3
import unittest

import tests  # noqa: F401
from jobhunter import canon, db, errors, paths
from jobhunter.enrich import TABLES, TRIGGERS, selftest_checks
from tests.fakes.u10 import EnrichTestCase, install_guard, remove_guard
from tests.helpers import HomeTestCase, insert_action, insert_company, insert_contact

MIG = os.path.join(paths.MIGRATIONS_DIR, "0002_enrich.sql")


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class TestApply(HomeTestCase):
    def test_fresh_database_has_everything(self):
        self.assertEqual(db.schema_version(self.conn), db.latest_version())
        self.assertGreaterEqual(db.latest_version(), 2)
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master")}
        for n in TABLES + TRIGGERS + ("u_enrich_find_once", "u_enrich_verify_once", "u_enrich_one_retry",
                                      "u_enrich_running_contact"):
            self.assertIn(n, names)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(contacts)")}
        self.assertTrue({"email_source", "email_enrich_call_id"} <= cols)

    def test_file_is_ascii_and_schema_sql_unchanged(self):
        with open(MIG, "rb") as fh:
            fh.read().decode("ascii")
        with open(paths.SCHEMA_FILE, "rb") as a, open(os.path.join(paths.MIGRATIONS_DIR, "0001_init.sql"), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_on_database_with_contacts(self):
        mem = sqlite3.connect(":memory:")
        self.addCleanup(mem.close)
        with open(os.path.join(paths.MIGRATIONS_DIR, "0001_init.sql")) as fh:
            mem.executescript(fh.read())
        ts = "2026-01-01T00:00:00Z"
        mem.execute("INSERT INTO companies (id, company_uid, display_name, created_at, updated_at) VALUES "
                    "(1, 'KAAAAAAA', 'Kestrel Commerce', ?, ?)", (ts, ts))
        mem.execute("INSERT INTO contacts (id, contact_uid, full_name, company_id, email, email_grade, created_at, "
                    "updated_at) VALUES (1, 'PAAAAAAA', 'Example Person', 1, 'example.person@kestrel.example', 'A', ?, ?)",
                    (ts, ts))
        with open(MIG) as fh:
            mem.executescript(fh.read())
        row = mem.execute("SELECT email, email_grade, email_source, email_enrich_call_id FROM contacts").fetchone()
        self.assertEqual(row, ("example.person@kestrel.example", "A", None, None))
        mem.execute("UPDATE contacts SET email_source = 'published' WHERE id = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            mem.execute("UPDATE contacts SET email_source = 'scraped' WHERE id = 1")

    def test_selftest_checks(self):
        res = {r["name"]: r for r in selftest_checks()}
        self.assertTrue(res["enrich migration objects"]["ok"], res["enrich migration objects"]["detail"])
        self.assertTrue(res["enrich trigger fixtures"]["ok"], res["enrich trigger fixtures"]["detail"])
        self.assertEqual((res["enrich dependencies"]["ok"], res["enrich dependencies"]["detail"]), (True, "all present"))


class TestTriggers(EnrichTestCase):
    def ins_request(self, **kw):
        ts = canon.now()
        row = {"request_uid": canon.new_uid("E"), "status": "running", "created_by": "system", "started_at": ts,
               "created_at": ts, "updated_at": ts}
        row.update(kw)
        cur = self.conn.execute("INSERT INTO enrich_requests (%s) VALUES (%s)" % (", ".join(row), ", ".join("?" * len(row))),
                                list(row.values()))
        return cur.lastrowid

    def ins_call(self, rid, provider="hunter", op="find_name_domain", outcome="inflight", credits=1.0, **kw):
        ts = canon.now()
        row = {"request_id": rid, "provider": provider, "op": op, "outcome": outcome, "started_at": ts,
               "credits_charged": credits, "created_at": ts, "updated_at": ts}
        row.update(kw)
        cur = self.conn.execute("INSERT INTO enrich_calls (%s) VALUES (%s)" % (", ".join(row), ", ".join("?" * len(row))),
                                list(row.values()))
        return cur.lastrowid

    def test_budget_fail_closed_without_meta(self):
        rid = self.ins_request()
        self.conn.execute("DELETE FROM meta WHERE key LIKE 'enrich_%'")
        self.assertDenied("E_CEILING", self.ins_call, rid)

    def test_budget_windows(self):
        rid = self.ins_request()
        self.conn.execute("UPDATE meta SET value = '2' WHERE key = 'enrich_day_credits:hunter'")
        self.ins_call(rid)
        self.ins_call(self.ins_request(), credits=1)
        self.assertDenied("E_CEILING", self.ins_call, self.ins_request())
        self.clock.advance(days=1, seconds=1)
        self.ins_call(self.ins_request())                       # the 24 h window rolled

    def test_reserve_max_and_inflight(self):
        rid = self.ins_request()
        self.assertDenied("E_INTERNAL", self.ins_call, rid, credits=0.5)
        self.assertDenied("E_INTERNAL", self.ins_call, rid, outcome="hit")
        self.ins_call(rid, op="verify", credits=0.5)            # Hunter verify reserves 0.5

    def test_max_finders(self):
        rid = self.ins_request()
        self.ins_call(rid, provider="prospeo")
        self.ins_call(rid, provider="hunter")
        self.assertDenied("E_CEILING", self.ins_call, rid, provider="getprospect")
        rid2 = self.ins_request()
        c = self.ins_call(rid2, provider="prospeo")
        self.conn.execute("UPDATE enrich_calls SET outcome = 'network_before_send', credits_charged = 0 WHERE id = ?",
                          (c,))
        self.ins_call(rid2, provider="hunter")
        self.ins_call(rid2, provider="getprospect")             # a call that never left does not count

    def test_find_once_and_verify_once(self):
        rid = self.ins_request()
        self.ins_call(rid, provider="hunter")
        self.assertDenied("E_ALREADY_DONE", self.ins_call, rid, provider="hunter")       # u_enrich_find_once
        self.ins_call(rid, provider="hunter", op="verify", credits=0.5)
        self.assertDenied("E_ALREADY_DONE", self.ins_call, rid, provider="hunter", op="verify", credits=0.5)

    def test_error_codes_of_the_finder(self):
        """INTEGRATION PATCH LIST U1-1 has landed: the two codes exist with their exit numbers."""
        self.assertEqual((errors.CODES["E_NOT_TARGET"], errors.CODES["E_ENRICH_UNAVAILABLE"]), (7, 4))
        self.assertEqual(errors.Denied("E_NOT_TARGET", "x").code, "E_NOT_TARGET")

    def test_outcome_graph(self):
        rid = self.ins_request()
        c = self.ins_call(rid)
        self.conn.execute("UPDATE enrich_calls SET outcome = 'in_progress' WHERE id = ?", (c,))
        self.conn.execute("UPDATE enrich_calls SET outcome = 'hit' WHERE id = ?", (c,))
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.conn.execute("UPDATE enrich_calls SET outcome = 'miss' WHERE id = ?", (c,))
        self.assertEqual(str(cm.exception), "E_BAD_TRANSITION")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE enrich_calls SET outcome = 'inflight' WHERE id = ?", (c,))

    def test_one_retry(self):
        r1 = self.ins_request(status="not_found")
        r2 = self.ins_request(status="not_found", retry_of=r1)
        self.assertDenied("E_ALREADY_DONE", self.ins_request, retry_of=r2)
        self.assertDenied("E_ALREADY_DONE", self.ins_request, retry_of=r1)             # u_enrich_one_retry

    def test_one_running_per_contact(self):
        comp = insert_company(self.conn)
        cid = insert_contact(self.conn, company_id=comp, email=None)
        self.ins_request(contact_id=cid)
        self.assertDenied("E_LOCKED", self.ins_request, contact_id=cid)                # u_enrich_running_contact
        self.ins_request(contact_id=cid, status="found")

    def test_contact_provider_email_rules(self):
        comp = insert_company(self.conn)
        cid = insert_contact(self.conn, company_id=comp, email=None)
        rid = self.ins_request(contact_id=cid)
        call = self.ins_call(rid, email="example.person@kestrel.example")

        def upd(**cols):
            self.conn.execute("UPDATE contacts SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in cols),
                              list(cols.values()) + [cid])
        self.assertDenied("E_ADDRESS_GRADE", upd, email="example.person@kestrel.example", email_grade="B",
                          email_source="provider")                                   # no call id
        self.assertDenied("E_ADDRESS_GRADE", upd, email="example.person@kestrel.example", email_grade="A",
                          email_source="provider", email_enrich_call_id=call)        # grade A from a provider
        self.assertDenied("E_ADDRESS_GRADE", upd, email="other.person@kestrel.example", email_grade="B",
                          email_source="provider", email_enrich_call_id=call)        # does not match the call
        self.assertDenied("E_ADDRESS_GRADE", upd, email="example.person@kestrel.example", email_grade=None,
                          email_source="provider", email_enrich_call_id=call)        # NULL grade is refused too
        upd(email="example.person@kestrel.example", email_grade="B", email_source="provider", email_enrich_call_id=call)
        self.conn.execute("UPDATE enrich_calls SET bounced_at = ? WHERE id = ?", (canon.now(), call))
        upd(email="example.person@kestrel.example", email_invalid=1)                 # no-op on watched columns
        self.conn.execute("UPDATE contacts SET email = COALESCE(email, 'x@kestrel.example'), "
                          "email_grade = COALESCE(email_grade, 'C') WHERE id = ?", (cid,))
        self.assertDenied("E_ADDRESS_GRADE", upd, email_grade="C")
        upd(email=None, email_grade=None, email_source=None, email_enrich_call_id=None)   # clearing is allowed
        ts = canon.now()
        self.assertDenied("E_ADDRESS_GRADE", self.conn.execute,
                          "INSERT INTO contacts (contact_uid, email, email_grade, email_source, created_at, updated_at) "
                          "VALUES ('PAAAAAA2', 'example.person@kestrel.example', 'B', 'provider', ?, ?)", (ts, ts))

    def test_action_address_rules(self):
        comp = insert_company(self.conn)
        cid = insert_contact(self.conn, company_id=comp, email=None)
        rid = self.ins_request(contact_id=cid)
        call = self.ins_call(rid, provider="prospeo", email="example.person@kestrel.example")
        self.conn.execute("UPDATE contacts SET email = 'example.person@kestrel.example', email_grade = 'B', "
                          "email_source = 'provider', email_enrich_call_id = ? WHERE id = ?", (call, cid))
        self.assertDenied("E_ADDRESS_GRADE", insert_action, self.conn, kind="cold_email", contact_id=cid,
                          company_id=comp, recipient="someone.else@kestrel.example")
        ts = canon.now()
        self.conn.execute("INSERT INTO breakers (scope, state, reason_code, updated_at) VALUES "
                          "('enrich:prospeo', 'open', 'bounce_strikes', ?)", (ts,))
        self.assertDenied("E_ADDRESS_GRADE", insert_action, self.conn, kind="cold_email", contact_id=cid,
                          company_id=comp, recipient="example.person@kestrel.example")
        self.conn.execute("UPDATE breakers SET state = 'closed' WHERE scope = 'enrich:prospeo'")
        self.conn.execute("UPDATE enrich_calls SET purged_at = ? WHERE id = ?", (ts, call))
        self.assertDenied("E_ADDRESS_GRADE", insert_action, self.conn, kind="cold_email", contact_id=cid,
                          company_id=comp, recipient="example.person@kestrel.example")
        self.conn.execute("UPDATE enrich_calls SET purged_at = NULL WHERE id = ?", (call,))
        insert_action(self.conn, kind="cold_email", contact_id=cid, company_id=comp,
                      recipient="example.person@kestrel.example")

    def test_touch_triggers(self):
        rid = self.ins_request()
        before = self.conn.execute("SELECT updated_at FROM enrich_requests WHERE id = ?", (rid,)).fetchone()[0]
        self.conn.execute("UPDATE enrich_requests SET next_step = 1 WHERE id = ?", (rid,))
        after = self.conn.execute("SELECT updated_at FROM enrich_requests WHERE id = ?", (rid,)).fetchone()[0]
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
