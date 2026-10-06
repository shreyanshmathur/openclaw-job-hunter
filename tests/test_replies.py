"""U6 replies: packets, agent and code classifications, consequences, idempotency, the CLI path."""
from __future__ import annotations

import json
import os
import shutil
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import canon, db, replies, threads
from tests.fakes.u6 import U6TestCase, deps
from tests.fakes.u6 import enrich as enrich_fakes
from tests.fakes.u6.agentcall import (agent_cli, agent_cli_isolated, identity, legacy_env, proofs_available,
                                      run_in_process, run_isolated)
from tests.helpers import insert_action, insert_company, insert_contact, insert_draft, insert_job


class RepliesBase(U6TestCase):
    def setUp(self):
        super().setUp()
        self.co = insert_company(self.conn)
        self.ct = insert_contact(self.conn, company_id=self.co, email="alex.rivera@kestrel.example",
                                 linkedin_url="https://www.linkedin.com/in/example-alex-rivera")
        self.draft = insert_draft(self.conn, company_id=self.co, contact_id=self.ct, status="sent",
                                  subject="Returns forecasting", body="Hi Alex,\n\nYour post on RTO models.\n\nThanks,")
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", company_id=self.co, contact_id=self.ct,
                                draft_id=self.draft, recipient="alex.rivera@kestrel.example")
            res = threads.on_confirm(self.conn, self.row("SELECT * FROM actions WHERE id = ?", aid))
        self.key = res["thread_key"]
        self.tid = self.row("SELECT id FROM threads WHERE thread_key = ?", self.key)[0]

    def packet(self, msg_ref="gm:100", text="Thanks Alex, can we talk next week?\n\nOn Mon, someone wrote:\n> old"):
        with db.tx(self.conn):
            return replies.write_packet(self.conn, {"msg_ref": msg_ref, "channel": "email", "thread_key": self.key,
                                                    "from_domain": "Kestrel.example", "received_at": canon.now(),
                                                    "subject": "Re: Returns forecasting"}, text)

    def record(self, payload, by="jobhunter-outreach"):
        with db.tx(self.conn):
            return replies.record(self.conn, payload, by)

    def thread(self):
        return self.row("SELECT * FROM threads WHERE id = ?", self.tid)


class TestPackets(RepliesBase):
    def test_packet_shape_and_idempotency(self):
        path = self.packet()
        self.assertTrue(path.endswith("/outreach/inbox/reply-1.json"))
        with open(path, encoding="utf-8") as fh:
            p = json.load(fh)
        self.assertEqual(set(p), {"inbound_id", "thread_key", "channel", "from_domain", "received_at", "subject",
                                  "text", "our_last_message_excerpt"})
        self.assertEqual(p["thread_key"], self.key)
        self.assertEqual(p["text"], "Thanks Alex, can we talk next week?")
        self.assertEqual(p["from_domain"], "kestrel.example")
        self.assertTrue(p["our_last_message_excerpt"].startswith("Hi Alex,"))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(self.packet(), path)
        self.assertEqual(replies.pending(self.conn, 10), [{"inbound_id": 1, "packet_path": path,
                                                           "thread_key": self.key}])

    def test_packet_file_is_written_only_after_commit(self):
        seen = {}

        def fetch():
            with db.tx(self.conn):
                path = replies.write_packet(self.conn, {"msg_ref": "gm:200", "channel": "email", "thread_key": self.key,
                                                        "from_domain": "kestrel.example"}, "Sounds good.")
                seen["inside"] = os.path.exists(path)
                seen["path"] = path
        fetch()
        self.assertFalse(seen["inside"], "no file while the transaction can still roll back")
        self.assertTrue(os.path.exists(seen["path"]))

    def test_rollback_leaves_no_orphan_packet(self):
        seen = {}
        with self.assertRaises(RuntimeError):
            with db.tx(self.conn):
                seen["path"] = replies.write_packet(self.conn, {"msg_ref": "gm:300", "channel": "email",
                                                                "thread_key": self.key}, "Yes please.")
                raise RuntimeError("the fetcher failed later in the same transaction")
        self.assertFalse(os.path.exists(seen["path"]))
        self.assertEqual(self.row("SELECT count(*) FROM inbound_messages")[0], 0)
        self.assertFalse(os.path.exists(replies.packet_dir()) and os.listdir(replies.packet_dir()))

    def test_missing_packet_file_is_written_again(self):
        path = self.packet()
        os.unlink(path)
        self.assertEqual(self.packet(), path)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["thread_key"], self.key)
        self.assertEqual(self.row("SELECT count(*) FROM inbound_messages")[0], 1)

    def test_strip_quoted(self):
        self.assertEqual(replies.strip_quoted("Yes.\n> earlier text\nOK"), "Yes.\nOK")


class TestAgentRecords(RepliesBase):
    def test_positive_reply(self):
        path = self.packet()
        res = self.record({"inbound_id": 1, "thread_key": self.key, "class": "positive",
                           "summary": "Asks for a call next week.", "received_at": canon.now(), "msg_ref": "x"})
        t = self.thread()
        self.assertEqual((t["state"], t["needs_human"], t["reply_class"], t["followup_due_at"]),
                         ("replied", 1, "positive", None))
        self.assertEqual(self.row("SELECT contact_state FROM companies WHERE id = ?", self.co)[0], "active_thread")
        n = self.row("SELECT priority, kind FROM notifications")
        self.assertEqual((n[0], n[1]), ("high", "positive_reply"))
        self.assertEqual(self.row("SELECT kind FROM human_tasks")[0], "review_reply")
        self.assertEqual(self.row("SELECT status FROM inbound_messages WHERE id = 1")[0], "classified")
        self.assertEqual(res["packet_path"], path)
        self.assertTrue(replies.cleanup_packet(path))
        self.assertFalse(os.path.exists(path))
        again = self.record({"inbound_id": 1, "thread_key": self.key, "class": "negative",
                             "summary": "Changed mind.", "received_at": canon.now()})
        self.assertEqual(again["effects"], ["already_recorded"])
        self.assertEqual(self.thread()["state"], "replied")
        self.assertEqual(self.row("SELECT count(*) FROM replies")[0], 1)

    def test_negative_reply(self):
        self.packet()
        self.record({"inbound_id": 1, "thread_key": self.key, "class": "not_hiring",
                     "summary": "No open roles this quarter.", "received_at": canon.now()})
        self.assertEqual(self.thread()["state"], "closed")
        c = self.row("SELECT do_not_contact, dnc_reason FROM contacts WHERE id = ?", self.ct)
        self.assertEqual((c[0], c[1]), (1, "reply_not_hiring"))
        self.assertEqual(self.row("SELECT contact_state FROM companies WHERE id = ?", self.co)[0], "none")

    def test_opt_out_excludes_person_and_company(self):
        self.packet()
        self.record({"inbound_id": 1, "thread_key": self.key, "class": "opt_out",
                     "summary": "Please remove me from your list.", "received_at": canon.now()})
        rows = {(r[0], r[1]) for r in self.conn.execute("SELECT type, source FROM exclusions")}
        self.assertEqual(rows, {("email", "reply_optout"), ("linkedin", "reply_optout"), ("company", "reply_optout")})
        self.assertEqual(self.row("SELECT do_not_contact FROM contacts WHERE id = ?", self.ct)[0], 1)
        self.assertEqual(self.row("SELECT contact_state FROM companies WHERE id = ?", self.co)[0], "do_not_contact")
        self.assertEqual(len(deps.APPLIED), 3)
        self.assertEqual(self.thread()["state"], "closed")

    def test_two_complaints_trip_the_cold_breaker(self):
        self.packet("gm:1")
        self.record({"inbound_id": 1, "thread_key": self.key, "class": "complaint", "summary": "Spam.",
                     "received_at": canon.now()})
        self.assertEqual(deps.TRIPS, [])
        co2 = insert_company(self.conn, name="Tidemark Labs", domain="tidemark.example")
        ct2 = insert_contact(self.conn, company_id=co2, email="sam.lee@tidemark.example", full_name="Sam Lee")
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", company_id=co2, contact_id=ct2,
                                recipient="sam.lee@tidemark.example")
            key2 = threads.on_confirm(self.conn, self.row("SELECT * FROM actions WHERE id = ?", aid))["thread_key"]
        self.record({"inbound_id": None, "thread_key": key2, "class": "complaint", "summary": "Marked as spam.",
                     "received_at": canon.now(), "msg_ref": "gm:2"})
        self.assertEqual(deps.TRIPS, [("gmail.cold", "complaints_30d")])

    def test_out_of_office_moves_due_date(self):
        self.record({"inbound_id": None, "thread_key": self.key, "class": "out_of_office",
                     "summary": "Away until 14 October.", "received_at": canon.now(), "msg_ref": "gm:9",
                     "return_date": "2026-10-14"})
        self.assertEqual(self.thread()["followup_due_at"], "2026-10-16T05:00:00Z")
        self.assertEqual(self.thread()["state"], "open")

    def test_linkedin_reply_without_packet(self):
        res = self.record({"inbound_id": None, "thread_key": self.key, "class": "neutral",
                           "summary": "Asked which role.", "received_at": canon.now(),
                           "msg_ref": "urn:li:msg:1"})
        self.assertEqual(self.row("SELECT status, thread_id FROM inbound_messages")[0], "classified")
        self.assertIn("thread_replied", res["effects"])

    def test_invite_accepted_event(self):
        self.enable_linkedin()
        with db.tx(self.conn):
            ct2 = insert_contact(self.conn, company_id=self.co, email=None, full_name="Sam Lee",
                                 linkedin_url="https://www.linkedin.com/in/example-sam-lee")
            aid = insert_action(self.conn, kind="li_invite", company_id=self.co, contact_id=ct2, recipient="example-sam-lee")
            key = threads.on_confirm(self.conn, self.row("SELECT * FROM actions WHERE id = ?", aid))["thread_key"]
        res = self.record({"thread_key": key, "event": "invite_accepted", "observed_at": canon.now()})
        self.assertEqual(res["effects"], ["invite_accepted"])
        self.assertEqual(self.row("SELECT state FROM threads WHERE thread_key = ?", key)[0], "invite_accepted")
        self.assertDenied("E_SCHEMA", self.record, {"thread_key": key, "event": "invite_accepted",
                                                    "observed_at": canon.now(), "extra": 1})

    def test_validation(self):
        base = {"inbound_id": None, "thread_key": self.key, "class": "positive", "summary": "ok",
                "received_at": canon.now(), "msg_ref": "m"}
        self.assertDenied("E_VALIDATION", self.record, dict(base, summary="x" * 301))
        self.assertDenied("E_SCHEMA", self.record, dict(base, note="hi"))
        self.assertDenied("E_VALIDATION", self.record, dict(base, **{"class": "happy"}))
        self.assertDenied("E_NOT_FOUND", self.record, dict(base, thread_key="em:TAAAAAAAAAAA"))
        self.assertDenied("E_VALIDATION", self.record, dict(base, msg_ref=None))
        self.assertDenied("E_VALIDATION", self.record, dict(base, received_at="yesterday"))
        self.assertDenied("E_NOT_FOUND", self.record, dict(base, inbound_id=99))


class TestCodeClasses(RepliesBase):
    def code(self, **kw):
        with db.tx(self.conn):
            return replies.record_code_class(self.conn, kw)

    def test_auto_ack_is_not_a_reply(self):
        before = dict(self.thread())
        res = self.code(msg_ref="gm:ack", code_class="auto_ack", thread_key=self.key, received_at=canon.now())
        after = dict(self.thread())
        self.assertEqual((before["state"], before["followup_due_at"]), (after["state"], after["followup_due_at"]))
        self.assertIsNone(after["reply_class"])
        self.assertEqual(self.code(msg_ref="gm:ack", code_class="auto_ack")["effects"], ["already_recorded"])
        self.assertIsNotNone(res["reply_id"])

    def test_bounce(self):
        self.conn.execute("UPDATE contacts SET email_grade = 'B' WHERE id = ?", (self.ct,))
        self.code(msg_ref="gm:b1", code_class="bounce", thread_key=self.key, received_at=canon.now())
        self.assertEqual(self.thread()["state"], "bounced")
        self.assertEqual(self.row("SELECT email_invalid FROM contacts WHERE id = ?", self.ct)[0], 1)
        self.assertEqual(db.meta_get(self.conn, "no_guess:kestrel.example"), "1")
        self.assertEqual(deps.TRIPS, [])
        co2 = insert_company(self.conn, name="Tidemark Labs", domain="tidemark.example")
        ct2 = insert_contact(self.conn, company_id=co2, email="sam.lee@tidemark.example", full_name="Sam Lee")
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", company_id=co2, contact_id=ct2,
                                recipient="sam.lee@tidemark.example")
            key2 = threads.on_confirm(self.conn, self.row("SELECT * FROM actions WHERE id = ?", aid))["thread_key"]
        self.code(msg_ref="gm:b2", code_class="bounce", thread_key=key2, received_at=canon.now())
        self.assertEqual(deps.TRIPS, [("gmail.cold", "bounces_24h")])

    def test_application_confirmation(self):
        job = insert_job(self.conn, company_id=self.co, status="applied")
        aid = insert_action(self.conn, kind="application", company_id=self.co, job_id=job)
        stamp = canon.now()
        self.conn.execute("INSERT INTO applications (action_id, job_id, route, created_at, updated_at) VALUES "
                          "(?, ?, 'ats_form', ?, ?)", (aid, job, stamp, stamp))
        res = self.code(msg_ref="gm:conf", code_class="application_confirmation", job_id=job, received_at=stamp)
        self.assertEqual(res["effects"], ["application_confirmed"])
        self.assertEqual(self.row("SELECT confirmation_email_at FROM applications")[0], stamp)

    def test_bad_class(self):
        self.assertDenied("E_VALIDATION", self.code, msg_ref="gm:x", code_class="positive")


class TestCli(RepliesBase):
    """The outreach agent's calls run the way the guard runs them: `python -I`, argv and env proof of one
    session (tests.fakes.u6.agentcall)."""

    def record_file(self):
        return self.home.write_agent_file("outreach", "C20260927T050000ZAAAA/reply.json", json.dumps(
            {"inbound_id": 1, "thread_key": self.key, "class": "positive", "summary": "Wants a call.",
             "received_at": canon.now(), "msg_ref": None}))

    def test_reply_record_as_agent_deletes_packet(self):
        path = self.packet()
        rec = self.record_file()
        rc, env_out = agent_cli_isolated("jobhunter-outreach", ["reply", "record", "--file", rec])
        self.assertEqual(rc, 0, env_out)
        self.assertFalse(os.path.exists(path))
        rc, env_out = agent_cli_isolated("jobhunter-outreach", ["reply", "pending"])
        self.assertEqual((rc, env_out["code"]), (0, "NOTHING_TO_DO"))

    def test_agent_file_outside_work_is_refused(self):
        rc, out = agent_cli("jobhunter-outreach", ["reply", "record", "--file", "/etc/hosts"])
        self.assertEqual((rc, out["code"]), (10, "E_PATH_NOT_ALLOWED"))

    def test_other_agents_may_not_record_replies(self):
        rec = self.record_file()
        rc, out = agent_cli("jobhunter-applier", ["reply", "record", "--file", rec])
        self.assertEqual((rc, out["code"]), (11, "E_CALLER_NOT_ALLOWED"), out)

    @unittest.skipUnless(proofs_available(), "needs a core with agent proof version 2 (U1)")
    def test_reply_record_needs_both_proofs_once(self):
        """Proof version 2 (CLI-ROUTE 5.3): the old agent env, the env proof alone, the argv proof alone, a plain
        interpreter or a replayed pair never record a reply; both fresh carriers under a real `python -I` do, once."""
        path = self.packet()
        rec = self.record_file()
        argv = ["reply", "record", "--file", rec]
        rc, out = run_in_process(argv, legacy_env("jobhunter-outreach"))
        self.assertEqual((rc, out["code"]), (11, "E_AUTH_FAILED"), out)
        env, full, mode = identity("jobhunter-outreach", argv)
        self.assertEqual((mode, full[0], full[2:]), ("v2", "--agent-proof", argv))
        rc, out = run_in_process(argv, env)                                 # env proof alone
        self.assertEqual((rc, out["code"]), (11, "E_AUTH_FAILED"), out)
        env, full, _ = identity("jobhunter-outreach", argv)
        rc, out = run_in_process(full, {k: v for k, v in env.items() if k != "JH_AGENT_PROOF"})   # argv alone
        self.assertEqual((rc, out["code"]), (11, "E_AUTH_FAILED"), out)
        env, full, _ = identity("jobhunter-outreach", argv)
        rc, out = run_in_process(full, env, isolated=False)                 # not python -I
        self.assertEqual((rc, out["code"]), (11, "E_AUTH_FAILED"), out)
        self.assertIn("python -I", out["message"])
        self.assertTrue(os.path.exists(path), "nothing was recorded so far")
        env, full, _ = identity("jobhunter-outreach", argv)
        rc, out = run_isolated(full, env)                                   # both carriers, real python -I
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.row("SELECT status FROM inbound_messages WHERE id = 1")[0], "classified")
        rc, out = run_in_process(full, env)                                 # the same proofs again: refused
        self.assertEqual((rc, out["code"]), (11, "E_AUTH_FAILED"), out)


class TestFinderHooks(RepliesBase):
    """Bounces and opt-outs reach the email finder (U10) through U1 hooks (faked finder here)."""

    def test_bounce_calls_on_bounce_with_the_sent_action(self):
        enrich_fakes.TRIP_ON_BOUNCE.append("enrich:hunter")
        with db.tx(self.conn):
            res = replies.record_code_class(self.conn, {"msg_ref": "gm:b9", "code_class": "bounce",
                                                        "thread_key": self.key, "received_at": canon.now()})
        self.assertEqual(enrich_fakes.BOUNCES, [(self.ct, "alex.rivera@kestrel.example")])
        self.assertIn("finder_bounce_strikes", res["effects"])

    def test_agent_recorded_bounce_calls_the_hook_too(self):
        res = self.record({"thread_key": self.key, "class": "bounce", "summary": "Delivery failure notice.",
                           "received_at": canon.now(), "msg_ref": "gm:18c2f0000000b003"})
        self.assertEqual(len(enrich_fakes.BOUNCES), 1)
        self.assertNotIn("finder_bounce_strikes", res["effects"])

    def test_opt_out_and_complaint_purge_finder_data(self):
        res = self.record({"thread_key": self.key, "class": "opt_out", "summary": "Asks not to be contacted.",
                           "received_at": canon.now(), "msg_ref": "gm:o1"})
        self.assertEqual(enrich_fakes.OPTOUTS, [self.ct])
        self.assertIn("finder_data_purged", res["effects"])

    def test_negative_reply_does_not_purge(self):
        self.record({"thread_key": self.key, "class": "negative", "summary": "Not interested.",
                     "received_at": canon.now(), "msg_ref": "gm:n1"})
        self.assertEqual((enrich_fakes.OPTOUTS, enrich_fakes.BOUNCES), ([], []))


class TestWebLane(RepliesBase):
    """web_ui route: `reply pending` adds the delivery-failure search and, when the agent may record it, the
    daily Sent audit and a requested history scan (U9 mail.audit.web_status)."""

    STATUS = {"route": "web_ui", "handled_by": "browser_lane", "audit_due": True, "audit_query": "in:sent newer_than:2d",
              "last_audit_at": None, "history_scan": {"days": 30, "query": "in:sent after:2026/08/29",
                                                      "requested_at": "2026-09-28T00:00:00Z"},
              "bounce_check": {"query": "{from:mailer-daemon from:postmaster} newer_than:3d",
                               "threads": [{"thread_key": "em:TABCDEFGHJKM", "recipient": "alex.rivera@kestrel.example"}]},
              "imap_connected": False}

    @staticmethod
    def audit():
        """The mail.audit module the code imports now (U6TestCase drops modules a test imported)."""
        import importlib
        return importlib.import_module("jobhunter.mail.audit")

    def run_cli(self, argv):
        # in this process (the mocks below apply), under the flags of `python -I`, with both proofs
        return agent_cli("jobhunter-outreach", argv)

    def test_web_lane_from_mail_status(self):
        with mock.patch.object(self.audit(), "web_status", return_value=dict(self.STATUS)), \
                mock.patch.object(replies, "_agent_may", return_value=True):
            lane = replies.web_lane(self.conn, "jobhunter-outreach")
        self.assertEqual(lane["bounce_check"], self.STATUS["bounce_check"])
        self.assertEqual(lane["sent_audit"], {"purpose": "audit", "query": "in:sent newer_than:2d", "last_audit_at": None})
        self.assertEqual((lane["history_scan"]["purpose"], lane["history_scan"]["days"]), ("history", 30))
        with mock.patch.object(self.audit(), "web_status", return_value=dict(self.STATUS)), \
                mock.patch.object(replies, "_agent_may", return_value=False):
            lane = replies.web_lane(self.conn, "jobhunter-outreach")
        self.assertEqual(set(lane), {"bounce_check"}, "no audit work the agent cannot record")
        with mock.patch.object(self.audit(), "web_status", side_effect=RuntimeError("no config")):
            self.assertEqual(replies.web_lane(self.conn, "jobhunter-outreach"), {})
        with mock.patch.object(self.audit(), "web_status", return_value={"route": "app_password"}):
            self.assertEqual(replies.web_lane(self.conn, None), {})

    def test_agent_may_reads_the_acl(self):
        self.assertTrue(replies._agent_may(None, "mail audit"))
        self.assertTrue(replies._agent_may("jobhunter-outreach", "reply record"))
        self.assertFalse(replies._agent_may("jobhunter-scout", "reply record"))

    def test_reply_pending_on_the_web_route(self):
        deps.config_set("gmail.route", "web_ui")
        with mock.patch.object(self.audit(), "web_status", return_value=dict(self.STATUS)), \
                mock.patch.object(replies, "_agent_may", return_value=True):
            rc, out = self.run_cli(["reply", "pending"])
        self.assertEqual(rc, 0, out)
        data = out["data"]
        self.assertEqual([c["thread_key"] for c in data["checks"]][:1], [self.key])
        self.assertTrue(data["checks"][0]["query"].startswith("from:alex.rivera@kestrel.example after:"))
        self.assertTrue(data["checks"][0]["query"].endswith(" -in:sent"))
        self.assertFalse(any("in:inbox" in c["query"] for c in data["checks"]))
        self.assertEqual(data["bounce_check"]["query"], self.STATUS["bounce_check"]["query"])
        self.assertIn("sent_audit", data)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestWebReadsToRecords(RepliesBase):
    """What read_gmail_message.js reports on recorded-shape pages becomes a reply record the code accepts."""

    def read(self, page):
        from tests.test_drivers_static import load_pages, run_driver
        return run_driver("read_gmail_message", load_pages()[page])

    def test_reply_found_in_the_inbox(self):
        out = self.read("inbox_reply")
        theirs = [m for m in out["messages"] if not m["from_owner"] and not m["is_bounce"]]
        self.assertEqual(len(theirs), 1)
        m = theirs[0]
        res = self.record({"inbound_id": None, "thread_key": self.key, "class": "positive",
                           "summary": "Happy to talk on Tuesday; asks for the notice period.",
                           "received_at": m["date"], "msg_ref": m["msg_ref"]})
        self.assertEqual(self.thread()["state"], "replied")
        self.assertEqual(self.row("SELECT platform_msg_ref FROM replies WHERE id = ?", res["reply_id"])[0],
                         "gm:18c2f0000000b002")
        again = self.record({"inbound_id": None, "thread_key": self.key, "class": "positive", "summary": "Same.",
                             "received_at": m["date"], "msg_ref": m["msg_ref"]})
        self.assertEqual(again["effects"], ["already_recorded"])

    def test_delivery_failure_is_recorded_on_the_matching_thread(self):
        co2 = insert_company(self.conn, name="Tidemark Labs", domain="tidemark.example")
        ct2 = insert_contact(self.conn, company_id=co2, email="jordan.lee@tidemark.example", full_name="Jordan Lee")
        with db.tx(self.conn):
            aid = insert_action(self.conn, kind="cold_email", company_id=co2, contact_id=ct2,
                                recipient="jordan.lee@tidemark.example")
            key2 = threads.on_confirm(self.conn, self.row("SELECT * FROM actions WHERE id = ?", aid))["thread_key"]
        bounce_threads = [{"thread_key": self.key, "recipient": "alex.rivera@kestrel.example"},
                          {"thread_key": key2, "recipient": "jordan.lee@tidemark.example"}]
        m = self.read("bounce_notice")["messages"][0]
        self.assertTrue(m["is_bounce"])
        hits = [t["thread_key"] for t in bounce_threads if t["recipient"] in m["bounce_addresses"]]
        self.assertEqual(hits, [key2])
        self.record({"inbound_id": None, "thread_key": hits[0], "class": "bounce", "summary": "Delivery failure notice.",
                     "received_at": m["date"], "msg_ref": m["msg_ref"]})
        self.assertEqual(self.row("SELECT state FROM threads WHERE thread_key = ?", key2)[0], "bounced")
        self.assertEqual(self.row("SELECT email_invalid FROM contacts WHERE id = ?", ct2)[0], 1)
        self.assertEqual(enrich_fakes.BOUNCES, [(ct2, "jordan.lee@tidemark.example")])
        self.assertEqual(self.thread()["state"], "open")


if __name__ == "__main__":
    unittest.main()
