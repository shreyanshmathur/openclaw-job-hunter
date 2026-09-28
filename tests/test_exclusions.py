"""Private exclusions (design 9, M9): import only adds or reactivates, a truncated or empty file deactivates
nothing, --deactivate is human only and refuses below 50%, matching covers every key variant, forget keeps
only a hashed suppression."""
from __future__ import annotations

import os

import tests  # noqa: F401
from jobhunter import canon, companies, db, exclusions, gate, paths
from jobhunter.auth import Caller
from tests.fakes.u1 import TUESDAY_NOON, write_config
from tests.helpers import HomeTestCase, insert_contact, insert_job

HUMAN = Caller("human")
SYSTEM = Caller("system")
ROWS = [("company", "Kestrel Commerce", "already in touch"), ("domain", "tidemark.example", "current employer"),
        ("email", "Recruiter.One+jobs@example.com", "contacted in August"),
        ("linkedin", "https://www.linkedin.com/in/example-person/", "met at a meetup"),
        ("job_url", "https://jobs.lever.co/example/00000000-0000-0000-0000-000000000000/apply", "applied manually"),
        ("company", "Harbor Freight Labs", ""), ("company", "Northwind Analytics", "")]


class ExclusionCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        self.path = os.path.join(paths.private_dir(), "exclusions.csv")

    def write_csv(self, rows, extra=""):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("# comment line\n" + exclusions.csv_text(rows) + extra)

    def do_import(self, deactivate=False, caller=SYSTEM):
        with db.tx(self.conn):
            return exclusions.import_csv(self.conn, self.path, deactivate, caller)

    def active(self):
        return self.conn.execute("SELECT count(*) FROM exclusions WHERE active = 1").fetchone()[0]


class TestImport(ExclusionCase):
    def test_import_is_idempotent_and_reports_errors(self):
        self.write_csv(ROWS, "bogus,value\nemail,not-an-address\ncompany,\n")
        r = self.do_import()
        self.assertEqual((r["added"], r["unchanged"]), (len(ROWS), 0))
        self.assertEqual([e["line"] for e in r["errors"]], [10, 11, 12])
        r = self.do_import()
        self.assertEqual((r["added"], r["unchanged"]), (0, len(ROWS)))
        self.assertTrue(os.listdir(os.path.join(paths.state_dir(), "backups")))

    def test_byte_order_mark_and_crlf(self):
        # a spreadsheet saves UTF-8 with a byte order mark and CRLF line ends: the rows still import, and the
        # recorded hash is the file's, so the next preflight does not import it again
        with open(self.path, "wb") as fh:
            fh.write(exclusions.csv_text(ROWS).replace("\n", "\r\n").encode("utf-8-sig"))
        r = self.do_import()
        self.assertEqual((r["added"], r["errors"]), (len(ROWS), []))
        self.assertFalse(exclusions.file_changed(self.conn))
        self.assertTrue(exclusions.match(self.conn, email="recruiter.one@example.com"))

    def test_bad_header_is_reported_and_retried(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("kind,thing\ncompany,Kestrel Commerce\n")
        r = self.do_import()
        self.assertEqual(r["added"], 0)
        self.assertEqual(r["errors"][0]["reason"], "header must be type,value,reason")
        self.assertTrue(exclusions.file_changed(self.conn))       # not marked imported: read again next run
        n = self.conn.execute("SELECT priority, text FROM notifications WHERE dedupe_key LIKE "
                              "'exclusions_errors:%'").fetchall()
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["priority"], "high")
        self.assertIn("not imported", n[0]["text"])
        self.do_import()                                          # same file: one notification only
        self.assertEqual(self.conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 1)
        self.write_csv(ROWS)
        self.do_import()
        self.assertFalse(exclusions.file_changed(self.conn))

    def test_truncated_or_empty_file_deactivates_nothing(self):
        self.write_csv(ROWS)
        self.do_import()
        self.write_csv(ROWS[:1])
        r = self.do_import()
        self.assertEqual((r["missing_from_file"], r["deactivated"]), (len(ROWS) - 1, 0))
        self.assertEqual(self.active(), len(ROWS))
        with open(self.path, "w") as fh:
            fh.write("")
        r = self.do_import()
        self.assertEqual(self.active(), len(ROWS))
        os.unlink(self.path)
        self.do_import()
        self.assertEqual(self.active(), len(ROWS))

    def test_deactivate_rules(self):
        self.write_csv(ROWS)
        self.do_import()
        self.write_csv(ROWS[:3])
        self.assertDenied("E_HUMAN_ONLY", self.do_import, True, SYSTEM)
        d = self.assertDenied("E_PRECONDITION", self.do_import, True, HUMAN)
        self.assertEqual(len(d.data["would_deactivate"]), 4)
        self.assertEqual(self.active(), len(ROWS))
        self.write_csv(ROWS[:5])
        r = self.do_import(True, HUMAN)
        self.assertEqual(r["deactivated"], 2)
        self.assertEqual(self.active(), 5)
        with open(self.path, "w") as fh:
            fh.write("type,value\n")
        self.assertDenied("E_PRECONDITION", self.do_import, True, HUMAN)

    def test_removed_rows_from_other_sources_untouched(self):
        with db.tx(self.conn):
            exclusions.add(self.conn, "email", "someone@example.org", "opted out", source="reply_optout")
        self.write_csv(ROWS[:1])
        r = self.do_import(True, HUMAN)
        self.assertEqual(r["deactivated"], 0)
        self.assertEqual(self.active(), 2)


class TestMatchAndApply(ExclusionCase):
    def test_variants_match(self):
        self.write_csv(ROWS)
        self.do_import()
        with db.tx(self.conn):
            m = exclusions.match
            self.assertTrue(m(self.conn, company_name="Kestrel Commerce Pvt. Ltd."))
            self.assertTrue(m(self.conn, email="recruiter.one@example.com"))
            self.assertTrue(m(self.conn, email="someone@mail.tidemark.example"))
            self.assertTrue(m(self.conn, linkedin_url="https://in.linkedin.com/in/Example-Person"))
            self.assertTrue(m(self.conn, job_url="https://jobs.lever.co/example/00000000-0000-0000-0000-000000000000"
                                                 "?lever-source=x"))
            self.assertFalse(m(self.conn, company_name="Kestrel Labs"))
            self.assertFalse(m(self.conn, email="recruiter.two@example.com"))

    def test_apply_marks_rows(self):
        with db.tx(self.conn):
            cid = companies.resolve(self.conn, name="Kestrel Commerce", domain="kestrel.example", source="job_board")
            j = insert_job(self.conn, company_id=cid, status="eligible")
            p = insert_contact(self.conn, company_id=cid, email="alex.rivera@kestrel.example")
            self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'email', ?)",
                              ("email:alex.rivera@kestrel.example", p, canon.now()))
            exclusions.add(self.conn, "company", "KESTREL COMMERCE", "test")
        row = self.conn.execute("SELECT contact_state, contact_state_reason FROM companies WHERE id = ?", (cid,)).fetchone()
        self.assertEqual(row[0], "do_not_contact")
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (j,)).fetchone()[0], "excluded")
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "cold_email", contact_id=p, company_id=cid)
        self.assertEqual({h["code"] for h in hits} >= {"E_EXCLUDED", "E_COMPANY_BLOCKED"}, True)
        with db.tx(self.conn):
            exclusions.remove(self.conn, "company", "Kestrel Commerce", "human")
        self.assertEqual(self.conn.execute("SELECT contact_state FROM companies WHERE id = ?", (cid,)).fetchone()[0],
                         "none")

    def test_forget_keeps_hashed_suppression(self):
        from jobhunter.commands.maint import forget
        with db.tx(self.conn):
            cid = companies.resolve(self.conn, name="Kestrel Commerce", source="job_board")
            p = insert_contact(self.conn, company_id=cid, email="alex.rivera@kestrel.example", full_name="Alex Rivera")
            self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'email_norm', ?)",
                              ("email_norm:alex.rivera@kestrel.example", p, canon.now()))
            res = forget(self.conn, email="alex.rivera@kestrel.example")
        self.assertEqual(res["contacts"], 1)
        row = self.conn.execute("SELECT full_name, email, do_not_contact FROM contacts WHERE id = ?", (p,)).fetchone()
        self.assertEqual(tuple(row), (None, None, 1))
        raw = [r[0] for r in self.conn.execute("SELECT value_raw FROM exclusions")]
        self.assertTrue(all(v.startswith("h:") for v in raw))
        self.assertFalse(any("alex" in v for v in raw))
        with db.tx(self.conn):
            self.assertTrue(exclusions.match(self.conn, email="Alex.Rivera+x@kestrel.example"))


    def test_forget_queues_sheet_deletes_including_approvals(self):
        from jobhunter.commands.maint import forget
        from tests.helpers import insert_action, insert_draft, insert_thread
        with db.tx(self.conn):
            cid = companies.resolve(self.conn, name="Kestrel Commerce", source="job_board")
            p = insert_contact(self.conn, company_id=cid, email="alex.rivera@kestrel.example", full_name="Alex Rivera")
            self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'email', ?)",
                              ("email:alex.rivera@kestrel.example", p, canon.now()))
            d = insert_draft(self.conn, status="awaiting_approval", company_id=cid, contact_id=p)
            a = insert_action(self.conn, kind="cold_email", company_id=cid, contact_id=p, draft_id=d)
            insert_thread(self.conn, a, contact_id=p, company_id=cid)
            res = forget(self.conn, email="alex.rivera@kestrel.example")
        uid = self.conn.execute("SELECT draft_uid FROM drafts WHERE id = ?", (d,)).fetchone()[0]
        tok = self.conn.execute("SELECT token FROM actions WHERE id = ?", (a,)).fetchone()[0]
        queued = {(r[0], r[1]) for r in self.conn.execute("SELECT tab, row_id FROM sheet_deletes")}
        self.assertEqual(queued, {("approvals", uid), ("outreach", tok), ("followups", "em:" + tok)})
        self.assertEqual(res["sheet_deletes"], 3)
    def test_forget_runs_the_finder_hook_first_and_clears_the_finder_fields(self):
        from unittest import mock
        from jobhunter import hooks
        from jobhunter.commands.maint import forget
        with db.tx(self.conn):
            cid = companies.resolve(self.conn, name="Kestrel Commerce", source="job_board")
            p = insert_contact(self.conn, company_id=cid, email="alex.rivera@kestrel.example", full_name="Alex Rivera")
            self.conn.execute("UPDATE contacts SET email_source = 'published', email_grade = 'A' WHERE id = ?", (p,))
            self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'email_norm', ?)",
                              ("email_norm:alex.rivera@kestrel.example", p, canon.now()))
            ts = canon.now()
            self.conn.execute("INSERT INTO enrich_requests (request_uid, contact_id, status, created_by, started_at, "
                              "created_at, updated_at) VALUES ('EAAAAAAA', ?, 'not_found', 'system', ?, ?, ?)",
                              (p, ts, ts, ts))
        seen = []
        real = hooks.on_forget

        def spy(conn, contact_id):
            # the contact still has its data when the finder hook runs
            seen.append(conn.execute("SELECT email FROM contacts WHERE id = ?", (contact_id,)).fetchone()[0])
            return real(conn, contact_id)
        with mock.patch.object(hooks, "on_forget", spy), db.tx(self.conn):
            forget(self.conn, email="alex.rivera@kestrel.example")
        self.assertEqual(seen, ["alex.rivera@kestrel.example"])
        row = self.conn.execute("SELECT email, email_source, email_enrich_call_id FROM contacts WHERE id = ?",
                                (p,)).fetchone()
        self.assertEqual(tuple(row), (None, None, None))
        req = self.conn.execute("SELECT contact_id, status FROM enrich_requests").fetchone()
        self.assertEqual(tuple(req), (None, "forgotten"))


if __name__ == "__main__":
    import unittest
    unittest.main()
