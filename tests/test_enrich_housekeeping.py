"""U10 housekeeping (8.2, 9.4): stale inflight -> unknown at max charge; stale running requests finished;
retention purge keeps key hashes, outcome and credits and clears contact fields; old rows pruned except
lifetime providers."""
from __future__ import annotations

import unittest

import tests  # noqa: F401
from jobhunter import canon, db
from jobhunter.enrich import cache, housekeeping
from tests.fakes.u10 import EnrichTestCase, install_guard, remove_guard
from tests.helpers import insert_action, insert_thread


def setUpModule():
    install_guard()


def tearDownModule():
    remove_guard()


class TestHousekeeping(EnrichTestCase):
    def request(self, status="running", contact_id=None):
        ts = canon.now()
        with db.tx(self.conn):
            rid = self.conn.execute("INSERT INTO enrich_requests (request_uid, contact_id, status, created_by, "
                                    "started_at, created_at, updated_at) VALUES (?, ?, ?, 'system', ?, ?, ?)",
                                    (canon.new_uid("E"), contact_id, status, ts, ts, ts)).lastrowid
            self.conn.execute("INSERT INTO enrich_request_keys (key_hash, request_id, kind, created_at) VALUES "
                              "(?, ?, 'lookup', ?)", (canon.sha256_text("k%d" % rid), rid, ts))
        return rid

    def call(self, rid, provider="hunter", op="find_name_domain", credits=1.0):
        ts = canon.now()
        with db.tx(self.conn):
            return self.conn.execute("INSERT INTO enrich_calls (request_id, provider, op, outcome, started_at, "
                                     "credits_charged, created_at, updated_at) VALUES (?, ?, ?, 'inflight', ?, ?, ?, ?)",
                                     (rid, provider, op, ts, credits, ts, ts)).lastrowid

    def test_stale_inflight_settled_at_max(self):
        rid = self.request()
        cid = self.call(rid)
        self.clock.advance(minutes=5)
        self.assertEqual(housekeeping.settle_stale_inflight(self.conn), 0)
        self.clock.advance(minutes=6)
        self.assertEqual(housekeeping.settle_stale_inflight(self.conn), 1)
        row = self.conn.execute("SELECT outcome, credits_charged FROM enrich_calls WHERE id = ?", (cid,)).fetchone()
        self.assertEqual(tuple(row), ("unknown", 1.0))

    def test_stale_running_requests(self):
        seen = self.request()
        c = self.call(seen)
        with db.tx(self.conn):
            self.conn.execute("UPDATE enrich_calls SET outcome = 'miss', credits_charged = 0 WHERE id = ?", (c,))
        unseen = self.request()
        held = self.request()
        from jobhunter import locks
        uid = self.conn.execute("SELECT request_uid FROM enrich_requests WHERE id = ?", (held,)).fetchone()[0]
        self.clock.advance(hours=2)
        with db.tx(self.conn):
            locks.acquire(self.conn, cache.lock_name(uid), "enrich:other", 120)
        out = housekeeping.finish_stale_running(self.conn)
        self.assertEqual(out, {"not_found": 1, "unavailable": 1})
        st = {r["id"]: r["status"] for r in self.requests()}
        self.assertEqual((st[seen], st[unseen], st[held]), ("not_found", "unavailable", "running"))
        keys = {r[0] for r in self.conn.execute("SELECT request_id FROM enrich_request_keys")}
        self.assertNotIn(unseen, keys)
        self.assertIn(seen, keys)

    def test_retention_unused(self):
        t = self.provider_contact("example.person@kestrel.example")
        keys_before = self.conn.execute("SELECT count(*) FROM enrich_request_keys").fetchone()[0]
        self.clock.advance(days=89)
        self.assertEqual(housekeeping.retention_purge(self.conn), {"unused": 0, "used": 0})
        self.clock.advance(days=2)
        self.assertEqual(housekeeping.retention_purge(self.conn), {"unused": 1, "used": 0})
        c = self.contact(t["contact_id"])
        self.assertEqual((c["email"], c["email_source"], c["email_enrich_call_id"]), (None, None, None))
        call = self.calls()[0]
        self.assertEqual((call["email"], call["outcome"], call["credits_charged"]), (None, "hit", 1.0))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM enrich_request_keys").fetchone()[0], keys_before)

    def test_retention_used(self):
        t = self.provider_contact("example.person@kestrel.example")
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", status="sent", contact_id=t["contact_id"],
                                company_id=t["company_id"], recipient=t["email"])
            tid = insert_thread(self.conn, aid, contact_id=t["contact_id"], company_id=t["company_id"])
        self.clock.advance(days=200)
        self.assertEqual(housekeeping.retention_purge(self.conn)["used"], 0)         # thread still open
        with db.tx(self.conn):
            self.conn.execute("UPDATE threads SET state = 'replied', reply_class = 'positive', updated_at = ? "
                              "WHERE id = ?", (canon.now(), tid))
        self.clock.advance(days=100)
        self.assertEqual(housekeeping.retention_purge(self.conn)["used"], 0)         # positive reply: kept
        with db.tx(self.conn):
            self.conn.execute("UPDATE threads SET reply_class = 'not_hiring', updated_at = ? WHERE id = ?",
                              (canon.now(), tid))
        self.clock.advance(days=91)
        self.assertEqual(housekeeping.retention_purge(self.conn)["used"], 1)
        self.assertIsNone(self.contact(t["contact_id"])["email"])
        self.assertEqual(self.conn.execute("SELECT recipient FROM actions WHERE id = ?", (aid,)).fetchone()[0],
                         t["email"])                                                # the ledger keeps it

    def test_prune_old_rows(self):
        rolling = self.call(self.request(status="not_found"))
        life_req = self.request(status="not_found")
        with db.tx(self.conn):
            db.meta_set(self.conn, "enrich_budget_lifetime:findymail", "5", "system")
            db.meta_set(self.conn, "enrich_budget_31d:findymail", "5", "system")
            db.meta_set(self.conn, "enrich_day_credits:findymail", "5", "system")
            db.meta_set(self.conn, "enrich_day_requests:findymail", "5", "system")
        lifetime = self.call(life_req, provider="findymail")
        referenced = self.call(self.request(status="found"), provider="prospeo")
        with db.tx(self.conn):
            self.conn.execute("UPDATE enrich_calls SET outcome = 'miss'")
            self.conn.execute("UPDATE enrich_requests SET result_call_id = ? WHERE id = (SELECT request_id FROM "
                              "enrich_calls WHERE id = ?)", (referenced, referenced))
        self.clock.advance(days=401)
        self.assertEqual(housekeeping.prune_old(self.conn), 1)
        left = {r["id"] for r in self.calls()}
        self.assertEqual(left, {lifetime, referenced})
        self.assertNotIn(rolling, left)

    def test_run_housekeeping(self):
        out = housekeeping.run_housekeeping(self.conn)
        self.assertEqual(set(out), {"inflight_settled", "stale_requests", "purged", "pruned"})


if __name__ == "__main__":
    unittest.main()
