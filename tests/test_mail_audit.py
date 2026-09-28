"""Sent-folder audit and history import (U9, design 4.5 nightly audit, 8 step 12), including U1's audit_run
calling mail.audit.sent_since over the fake IMAP server."""
from __future__ import annotations

import io
import json
import os
import stat
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import audit as core_audit
from jobhunter import auth, canon, cli, db, paths
from jobhunter.errors import Denied
from jobhunter.mail import audit
from jobhunter import mail
from tests.fakes.u9 import APP_PW, FIXTURES, OWNER, MailTestCase, inbound, write_config
from tests.helpers import insert_action, insert_company, insert_contact, insert_thread

ALEX = "alex.rivera@kestrel.example"
MORGAN = "morgan.lee@harbor-analytics.example"


class AuditTests(MailTestCase):
    def setUp(self):
        super().setUp()
        self.co = insert_company(self.conn)
        self.pc = insert_contact(self.conn, company_id=self.co, email=ALEX)
        self.conn.execute("INSERT INTO company_aliases (alias_key, company_id, kind, source, created_at) VALUES "
                          "('dom:kestrel.example', ?, 'dom', 'email', ?)", (self.co, canon.now()))
        self.clock.set("2026-09-30T03:10:00Z")

    def sent(self, uid, to, subject="Hello", date="2026-09-29T10:00:00Z", msg_id=None, cc=None):
        headers = {"Cc": cc} if cc else None
        self.imap.add_message(inbound(OWNER, to, subject, "body", date=date, msg_id=msg_id, headers=headers), uid=uid)
        return uid

    def script_sent(self, uids):
        self.imap.match("in:sent after:", uids)

    def test_rule(self):
        heads = [
            {"message_id": "<a@x>", "to": [ALEX], "cc": [], "date": "2026-09-29T10:00:00Z", "subject": "Hi"},
            {"message_id": "<b@x>", "to": ["friend@example.org"], "cc": [], "date": "2026-09-29T10:00:00Z"},
            {"message_id": "<c@x>", "to": ["jordan.kim@kestrel.example"], "cc": [], "date": "2026-09-29T11:00:00Z"},
            {"message_id": "<d@x>", "to": [OWNER], "cc": [], "date": "2026-09-29T11:00:00Z"},
        ]
        rows = audit.unledgered(self.conn, heads, OWNER)
        self.assertEqual(sorted(r["to"] for r in rows), [ALEX, "jordan.kim@kestrel.example"],
                         "known person and known company domain; strangers and yourself are not reported")

    def test_ledgered_messages_are_fine(self):
        a = insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                          recipient=ALEX, reserved_at="2026-09-29T09:59:00Z")
        tok = self.conn.execute("SELECT token FROM actions WHERE id = ?", (a,)).fetchone()[0]
        mid = "<%s@jobhunter.invalid>" % tok
        self.conn.execute("UPDATE actions SET message_id = ? WHERE id = ?", (mid, a))
        heads = [{"message_id": mid, "to": [ALEX], "cc": [], "date": "2026-09-29T10:00:00Z"},
                 {"message_id": "<web@x>", "to": [ALEX], "cc": [], "date": "2026-09-29T12:00:00Z"},
                 {"message_id": "<late@x>", "to": ["jordan.kim@kestrel.example"], "cc": [],
                  "date": "2026-10-20T12:00:00Z"}]
        rows = audit.unledgered(self.conn, heads, OWNER)
        self.assertEqual([r["message_id"] for r in rows], ["<late@x>"])

    def test_conversation_taken_over_is_not_reported(self):
        a = insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                          recipient=ALEX, reserved_at="2026-09-01T09:00:00Z")
        insert_thread(self.conn, a, contact_id=self.pc, company_id=self.co, state="replied")
        rows = audit.unledgered(self.conn, [{"message_id": "<r@x>", "to": [ALEX], "cc": [],
                                            "date": "2026-09-29T10:00:00Z"}], OWNER)
        self.assertEqual(rows, [])

    def test_sent_since_over_imap_and_core_audit_trips_global(self):
        self.script_sent([self.sent(1, ALEX), self.sent(2, "friend@example.org")])
        rows = audit.sent_since(self.conn, 2)
        self.assertEqual([r["to"] for r in rows], [ALEX])
        self.assertTrue(any(q.startswith("in:sent after:2026/09/28") for q in self.imap.queries))
        with db.tx(self.conn):
            res = core_audit.audit_run(self.conn, days=2)
        self.assertEqual(len(res["mismatches"]), 1)
        self.assertEqual(res["errors"], [])
        b = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'global'").fetchone()
        self.assertEqual(tuple(b), ("open", "audit_mismatch"))

    def test_sent_since_without_connection(self):
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM meta WHERE key = 'mail_connected_at'")
        self.assertEqual(audit.sent_since(self.conn, 2), [])
        self.assertEqual(self.imap.commands, [])

    def test_import_history(self):
        own = "<%s@jobhunter.invalid>" % canon.new_token()
        self.conn.execute("UPDATE actions SET message_id = NULL")
        insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                      recipient=ALEX, reserved_at="2026-09-10T09:00:00Z")
        self.conn.execute("UPDATE actions SET message_id = ? WHERE recipient = ?", (own, ALEX))
        uids = [self.sent(1, "Riley Park <riley.park@heron.example>", date="2026-06-01T10:00:00Z"),
                self.sent(2, "riley.park@heron.example", date="2026-07-01T10:00:00Z"),
                self.sent(3, "no-reply@heron.example"),
                self.sent(4, ALEX, msg_id=own),
                self.sent(5, ", ".join("p%d@list.example" % i for i in range(12))),
                self.sent(6, OWNER)]
        self.script_sent(uids)
        res = audit.import_history(self.conn, 365)
        self.assertEqual(res["messages"], 5, "our own Message-ID is already in the ledger")
        self.assertEqual(res["addresses"], 2)
        self.assertEqual(res["imported"], 1)
        self.assertEqual(res["skipped"], 1, "the second message to the same person adds nothing")
        imp = self.conn.execute("SELECT * FROM actions WHERE status = 'imported'").fetchall()
        self.assertEqual([(r["recipient"], r["sent_at"]) for r in imp], [("riley.park@heron.example",
                                                                         "2026-06-01T10:00:00Z")])
        self.assertEqual(audit.import_history(self.conn, 365)["imported"], 0, "idempotent")

    def test_import_items_shape(self):
        items = audit.import_items([{"to": ["a@x.example"], "cc": [], "date": "2026-09-01T00:00:00Z",
                                     "subject": "S"}], OWNER)
        self.assertEqual(items, [{"kind": "cold_email", "platform": "gmail", "recipient": "a@x.example",
                                  "sent_at": "2026-09-01T00:00:00Z", "evidence": "Gmail Sent 2026-09-01: S"}])


def web_fixture(name: str) -> dict:
    with open(os.path.join(FIXTURES, "web", name), "r", encoding="utf-8") as fh:
        return json.load(fh)


# the acl.json entry the replies lane needs for `mail audit` (U1 owns acl.json; added here when it is missing)
MAIL_AUDIT_ACL = {"--days": "int?", "--file": "path_work?"}


_REAL_LOAD_ACL = auth.load_acl


def _acl_with_mail_audit():
    acl = json.loads(json.dumps(_REAL_LOAD_ACL()))
    acl["agents"]["jobhunter-outreach"]["commands"].setdefault("mail audit", MAIL_AUDIT_ACL)
    return acl


class WebAuditTests(MailTestCase):
    """gmail.route = web_ui without an app password: the Sent audit and the history import are read in the
    browser by the replies lane (recorded fixtures of what the agent writes), never over IMAP."""
    connect = False

    def setUp(self):
        super().setUp()
        write_config(self.home, route="web_ui")
        self.co = insert_company(self.conn)
        self.pc = insert_contact(self.conn, company_id=self.co, email=ALEX)
        self.conn.execute("INSERT INTO company_aliases (alias_key, company_id, kind, source, created_at) VALUES "
                          "('dom:kestrel.example', ?, 'dom', 'email', ?)", (self.co, canon.now()))
        self.co2 = insert_company(self.conn, name="Harbor Analytics", domain="harbor-analytics.example")
        self.clock.set("2026-09-30T03:10:00Z")

    def connect_imap(self):
        mail.store_credentials(OWNER, APP_PW, "file")
        with db.tx(self.conn):
            db.meta_set(self.conn, "mail_connected_at", canon.now(), "human")

    def ledger_alex(self):
        insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                      recipient=ALEX, reserved_at="2026-09-29T09:58:00Z", route="browser")

    def test_no_browser_read_yet_is_reported_not_passed(self):
        with self.assertRaises(Denied) as cm:
            audit.sent_since(self.conn, 2)
        self.assertEqual(cm.exception.code, "E_PRECONDITION")
        self.assertEqual(cm.exception.data["handled_by"], "browser_lane")
        self.assertIn("never", cm.exception.message)
        res = core_audit.run(self.conn, 2)
        self.assertEqual(res["mismatches"], [])
        self.assertEqual(len(res["errors"]), 1, "U1's nightly audit shows the missing browser read as an error")
        self.assertIn("browser", res["errors"][0]["error"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM breakers WHERE scope = 'global' AND state = 'open'")
                          .fetchone())
        self.assertEqual(self.imap.commands, [], "no IMAP on the web_ui route")
        self.assertEqual(self.smtp.connections, 0)

    def test_status_says_when_the_browser_read_is_due(self):
        st = audit.web_status(self.conn)
        self.assertEqual((st["route"], st["handled_by"], st["audit_due"]), ("web_ui", "browser_lane", True))
        self.assertEqual(st["audit_query"], "in:sent newer_than:2d")
        self.assertIsNone(st["history_scan"])
        self.ledger_alex()
        audit.record_web_read(self.conn, web_fixture("sent_read_audit.json"))
        self.assertFalse(audit.web_status(self.conn)["audit_due"])
        self.clock.advance(hours=21)
        self.assertTrue(audit.web_status(self.conn)["audit_due"])
        self.connect_imap()
        self.assertFalse(audit.web_status(self.conn)["audit_due"], "with the optional IMAP connection code audits")

    def test_bounce_check_lists_recent_threads(self):
        self.assertIsNone(audit.web_status(self.conn)["bounce_check"], "nothing went out: no search")
        a = insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co, contact_id=self.pc,
                          recipient=ALEX, reserved_at="2026-09-29T09:58:00Z", route="browser")
        self.conn.execute("UPDATE actions SET sent_at = reserved_at WHERE id = ?", (a,))
        insert_thread(self.conn, a, contact_id=self.pc, company_id=self.co)
        old = insert_action(self.conn, kind="cold_email", status="sent", company_id=self.co2, recipient=MORGAN,
                            reserved_at="2026-09-01T09:00:00Z", route="browser")
        self.conn.execute("UPDATE actions SET sent_at = reserved_at WHERE id = ?", (old,))
        insert_thread(self.conn, old, company_id=self.co2)
        bc = audit.web_status(self.conn)["bounce_check"]
        self.assertEqual(bc["query"], "{from:mailer-daemon from:postmaster} newer_than:3d")
        self.assertEqual([t["recipient"] for t in bc["threads"]], [ALEX], "only threads started in the last 7 days")
        key = self.conn.execute("SELECT thread_key FROM threads WHERE first_action_id = ?", (a,)).fetchone()[0]
        self.assertEqual(bc["threads"][0]["thread_key"], key)

    def test_recorded_read_applies_the_rule_and_trips_at_once(self):
        self.ledger_alex()
        res = audit.record_web_read(self.conn, web_fixture("sent_read_audit.json"))
        self.assertEqual(res["messages"], 4)
        self.assertEqual([r["to"] for r in res["unledgered"]], [MORGAN],
                         "ledgered (Alex), stranger and out-of-window rows are not reported")
        self.assertTrue(res["tripped"])
        b = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'global'").fetchone()
        self.assertEqual(tuple(b), ("open", "audit_mismatch"))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT kind FROM human_tasks WHERE done_at IS NULL")],
                         ["review_audit_mismatch"])
        st = os.stat(os.path.join(paths.state_dir(), audit.WEB_FILE))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        # the nightly audit re-applies the rule to the stored read: once the person records the message, it is fine
        self.assertEqual([r["to"] for r in audit.sent_since(self.conn, 2)], [MORGAN])
        insert_action(self.conn, kind="cold_email", status="imported", company_id=self.co2, recipient=MORGAN,
                      reserved_at="2026-09-29T12:00:00Z", route="browser")
        self.assertEqual(audit.sent_since(self.conn, 2), [])
        self.clock.advance(hours=37)
        self.assertDenied("E_PRECONDITION", audit.sent_since, self.conn, 2)
        self.assertEqual(self.imap.commands, [])

    def test_clean_read_trips_nothing(self):
        self.ledger_alex()
        payload = web_fixture("sent_read_audit.json")
        payload["messages"] = payload["messages"][:1] + payload["messages"][2:3]
        res = audit.record_web_read(self.conn, payload)
        self.assertEqual((res["unledgered"], res["tripped"]), ([], False))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM breakers WHERE state = 'open'").fetchone())

    def test_read_validation(self):
        good = web_fixture("sent_read_audit.json")

        def bad(code, **change):
            p = json.loads(json.dumps(good))
            msgs = change.pop("messages", None)
            p.update(change)
            if msgs is not None:
                p["messages"] = msgs
            self.assertDenied(code, audit.record_web_read, self.conn, p)

        bad("E_VALIDATION", messages=[{"date": "2026-09-29", "to": ["Jordan Kim"], "subject": "x"}])
        bad("E_VALIDATION", messages=[{"date": "yesterday", "to": [ALEX], "subject": "x"}])
        bad("E_VALIDATION", messages=[{"date": "2026-09-29", "to": [], "subject": "x"}])
        bad("E_VALIDATION", messages=[{"date": "2026-09-29", "to": [ALEX], "url": "https://evil.example/x"}])
        bad("E_SCHEMA", messages=[{"date": "2026-09-29", "to": [ALEX], "body": "the whole text"}])
        bad("E_SCHEMA", note="extra key")
        bad("E_VALIDATION", observed_at="2026-09-29T03:00:00Z")
        bad("E_VALIDATION", purpose="everything")
        bad("E_VALIDATION", messages=[{"date": "2026-09-29", "to": [ALEX]}] * (audit.WEB_MAX_MESSAGES + 1))
        parsed = audit.parse_web_read(good)
        self.assertEqual(parsed["headers"][1]["date"], "2026-09-29T12:00:00Z", "a date without a time is noon UTC")
        self.assertEqual(parsed["headers"][1]["to"], [MORGAN], "addresses are lower-cased")
        self.assertEqual(parsed["headers"][2]["to"], ["friend@example.org"], "one address may be a string")

    def test_app_password_route_refuses_browser_reads(self):
        write_config(self.home)
        self.assertDenied("E_ROUTE_UNAVAILABLE", audit.record_web_read, self.conn, web_fixture("sent_read_audit.json"))
        self.assertEqual(audit.sent_since(self.conn, 2), [], "app_password before mail connect: nothing to read")

    def test_history_request_and_browser_import(self):
        self.clock.set("2026-09-29T09:00:00Z")
        self.assertDenied("E_PRECONDITION", audit.record_web_read, self.conn, web_fixture("sent_read_history.json"))
        st = audit.request_web_history(self.conn, 365)
        self.assertEqual(st["history_scan"]["days"], 365)
        self.assertEqual(st["history_scan"]["query"], "in:sent after:2025/09/29")
        self.assertDenied("E_VALIDATION", audit.request_web_history, self.conn, 400)
        res = audit.record_web_read(self.conn, web_fixture("sent_read_history.json"))
        self.assertEqual((res["purpose"], res["messages"], res["imported"]), ("history", 4, 2),
                         "no-reply and your own address are skipped; one entry per person")
        imp = self.conn.execute("SELECT recipient, sent_at, evidence FROM actions WHERE status = 'imported' "
                                "ORDER BY sent_at").fetchall()
        self.assertEqual([(r[0], r[1]) for r in imp], [("morgan.lee@harbor-analytics.example", "2026-03-02T09:12:00Z"),
                                                        ("riley.chen@northwind.example", "2026-07-19T11:05:00Z")])
        self.assertTrue(all(r[2].startswith(audit.WEB_SOURCE) for r in imp))
        self.assertIsNone(audit.web_status(self.conn)["history_scan"], "a complete read closes the request")
        self.assertDenied("E_PRECONDITION", audit.record_web_read, self.conn, web_fixture("sent_read_history.json"))
        audit.request_web_history(self.conn, 365)
        self.assertEqual(audit.record_web_read(self.conn, web_fixture("sent_read_history.json"))["imported"], 0,
                         "a second read of the same addresses adds nothing")
        self.assertEqual(self.imap.commands, [])

    def test_partial_history_read_keeps_the_request(self):
        self.clock.set("2026-09-29T09:00:00Z")
        audit.request_web_history(self.conn, 30)
        p = web_fixture("sent_read_history.json")
        p["complete"] = False
        self.assertFalse(audit.record_web_read(self.conn, p)["complete"])
        self.assertEqual(audit.web_status(self.conn)["history_scan"]["days"], 30)

    # ---- the CLI
    def cli(self, argv, agent=False, stdin=""):
        out = io.StringIO()
        env = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-outreach"} if agent else {}
        with mock.patch.object(auth, "load_acl", _acl_with_mail_audit):
            rc = cli.main(argv, env=env, stdin=io.StringIO(stdin), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_cli_replies_lane_reads_status_then_records(self):
        rc, out = self.cli(["mail", "audit"], agent=True)
        self.assertEqual((rc, out["code"], out["data"]["audit_due"]), (0, "OK", True), out)
        self.assertIn("Sent audit", out["message"])
        self.ledger_alex()
        path = self.home.write_agent_file("outreach", "sent-read.json",
                                          json.dumps(web_fixture("sent_read_audit.json")))
        rc, out = self.cli(["mail", "audit", "--file", path], agent=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual([r["to"] for r in out["data"]["unledgered"]], [MORGAN])
        rc, out = self.cli(["mail", "audit"], agent=True)
        self.assertEqual((rc, out["code"]), (0, "NOTHING_TO_DO"), out)
        rc, out = self.cli(["mail", "audit", "--file", "/etc/hosts"], agent=True)
        self.assertEqual(rc, 10, "agents read files only from their own work folder")
        self.assertEqual(self.imap.commands, [])

    def test_cli_agent_never_triggers_imap(self):
        self.connect_imap()
        rc, out = self.cli(["mail", "audit"], agent=True)
        self.assertEqual((rc, out["code"], out["data"]["audit_due"]), (0, "NOTHING_TO_DO", False), out)
        write_config(self.home)
        rc, out = self.cli(["mail", "audit"], agent=True)
        self.assertEqual((rc, out["code"], out["data"]["handled_by"]), (0, "NOTHING_TO_DO", "code_imap"), out)
        self.assertEqual(self.imap.commands, [])

    def test_cli_human_views(self):
        rc, out = self.cli(["mail", "audit"])
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["data"]["handled_by"], out["data"]["unledgered"]), ("browser_lane", []))
        self.assertIn("error", out["data"], "no browser read yet is shown, not hidden")
        self.clock.set("2026-09-29T09:00:00Z")
        auth.set_pin(None, "482915")
        rc, out = self.cli(["--pin-stdin", "mail", "import-history", "--days", "90"], stdin="482915\n")
        self.assertEqual((rc, out["code"]), (0, "PENDING"), out)
        self.assertEqual(out["data"]["history_scan"]["days"], 90)
        self.assertIn("No app password", out["message"])
        self.assertEqual(self.imap.commands, [])


if __name__ == "__main__":
    unittest.main()
