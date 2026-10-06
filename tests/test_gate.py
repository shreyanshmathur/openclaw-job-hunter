"""gate: reserve checks in order and every denial code, one open token per agent, arm read-back, confirm side
effects, fail rules, expiry, unknown blocking and counting, prechecks (design 2.3, 12.7, 13.2)."""
from __future__ import annotations

import io
import json
import os
from unittest import mock

import tests  # noqa: F401
from jobhunter import breakers, canon, ceilings, cli, db, gate, hooks, identity, keys, paths, reconcile
from jobhunter.auth import Caller
from jobhunter.errors import Denied
from tests.fakes.u1 import (TUESDAY_NOON, World, enable_linkedin, fake_presend, patch_hooks, write_config,
                             write_heartbeat)
from tests.helpers import (HomeTestCase, agent_cli, clear_consent, insert_action, insert_cycle, insert_thread,
                           write_consent)

AP = "jobhunter-applier"
OU = "jobhunter-outreach"


class GateCase(HomeTestCase):
    start_ts = TUESDAY_NOON

    def setUp(self):
        super().setUp()
        write_config()
        write_heartbeat()
        self.p = patch_hooks()
        self.p.start()
        self.w = World(self.conn)
        # agent callers reserve inside their running cycle (per-cycle ceilings and cycle stop rules)
        self.cycles = {OU: insert_cycle(self.conn, "outreach"), AP: insert_cycle(self.conn, "applier")}

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def reserve(self, **kw):
        args = dict(route="mailer", agent_id="system:mailer", platform="gmail", kind="cold_email")
        args.update(kw)
        with db.tx(self.conn):
            return gate.reserve(self.conn, **args)

    def cold_ready(self, contact=None):
        contact = contact or self.w.person
        d = self.w.draft("cold_email", contact_id=contact)
        with db.tx(self.conn):
            pc = self.w.precheck("cold_email", "gmail", contact_id=contact)
        return d, pc["precheck_id"]

    def send_cold(self, contact=None):
        d, pc = self.cold_ready(contact)
        r = self.reserve(draft_id=d, precheck_id=pc)
        with db.tx(self.conn):
            gate.mark_armed(self.conn, r["token"])
            out = gate.confirm(self.conn, r["token"], "250 2.0.0 OK")
        return r["token"], out


class TestMailerFlow(GateCase):
    def test_reserve_arm_confirm(self):
        token, out = self.send_cold()
        self.assertRegex(token, r"^T[A-Z2-7]{11}$")
        a = self.conn.execute("SELECT * FROM actions WHERE token = ?", (token,)).fetchone()
        self.assertEqual((a["status"], a["first_touch"], a["thread_key"]), ("sent", 1, "em:" + token))
        self.assertEqual(out["thread_key"], "em:" + token)
        d = self.conn.execute("SELECT status FROM drafts WHERE id = ?", (a["draft_id"],)).fetchone()[0]
        self.assertEqual(d, "sent")
        st = self.conn.execute("SELECT contact_state FROM companies WHERE id = ?", (self.w.company,)).fetchone()[0]
        self.assertEqual(st, "contacted")
        self.assertEqual(self.conn.execute("SELECT used_by_action FROM prechecks").fetchone()[0], a["id"])
        self.assertEqual(ceilings.warmup_week(self.conn, "gmail"), 1)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM warmup WHERE platform = 'gmail'").fetchone())

    def test_second_cold_email_same_person_and_company_refused(self):
        self.send_cold()
        self.clock.advance(minutes=30)
        other = self.w.contact("Jordan Lee", "jordan.lee@kestrel.example")
        d, pc = self.cold_ready(other)
        self.assertDenied("E_COMPANY_COOLDOWN", self.reserve, draft_id=d, precheck_id=pc)
        # a second first touch to the same person through LinkedIn is refused too
        enable_linkedin(self.conn)
        d2 = self.w.draft("li_invite_note", body="")
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "li_invite", contact_id=self.w.person, company_id=self.w.company)
        self.assertIn("E_DUP_PERSON", [h["code"] for h in hits])
        self.assertTrue(d2)

    def test_route_and_agent_checks(self):
        d, pc = self.cold_ready()
        self.assertDenied("E_ROUTE_UNAVAILABLE", self.reserve, draft_id=d, precheck_id=pc, route="browser",
                          agent_id=OU)
        self.assertDenied("E_ROUTE_UNAVAILABLE", self.reserve, draft_id=d, precheck_id=pc, agent_id=OU)
        write_config({"gmail.route": "web_ui"})
        os.unlink(os.path.join(paths.guard_dir(), "heartbeat.json"))
        self.assertDenied("E_GUARD_MISSING", self.reserve, draft_id=d, precheck_id=pc, route="browser", agent_id=OU)
        write_heartbeat(age_s=900)
        self.assertDenied("E_GUARD_MISSING", self.reserve, draft_id=d, precheck_id=pc, route="browser", agent_id=OU)
        write_heartbeat(install_id="IAAAAAAAA")
        self.assertDenied("E_GUARD_MISSING", self.reserve, draft_id=d, precheck_id=pc, route="browser", agent_id=OU)
        write_heartbeat()
        self.assertDenied("E_DETECT_MISSING", self.reserve, draft_id=d, precheck_id=pc, route="browser", agent_id=OU)
        self.w.detect_clear("gmail")
        r = self.reserve(draft_id=d, precheck_id=pc, route="browser", agent_id=OU)
        self.assertEqual(r["status"], "reserved")


class TestRealThreadsHook(GateCase):
    """hooks.on_confirm returns the U6 threads.on_confirm result, so gate confirm reports the thread (3.4)."""

    def setUp(self):
        super().setUp()
        self.p.stop()
        self.p = mock.patch.object(hooks, "presend", fake_presend)     # only presend is faked here
        self.p.start()

    def test_confirm_reports_thread_key_and_followup_due(self):
        d, pc = self.cold_ready()
        r = self.reserve(draft_id=d, precheck_id=pc)
        mid = "<%s@jobhunter.invalid>" % r["token"]
        with db.tx(self.conn):
            gate.mark_armed(self.conn, r["token"], message_id=mid)
        self.assertEqual(self.conn.execute("SELECT status, message_id FROM actions WHERE token = ?",
                                           (r["token"],)).fetchone()[:], ("armed", mid))
        with db.tx(self.conn):
            out = gate.confirm(self.conn, r["token"], "250 2.0.0 OK")
        self.assertEqual(out["thread_key"], "em:" + r["token"])
        self.assertIsNotNone(out["followup_due_at"])
        th = self.conn.execute("SELECT followup_due_at, first_message_id FROM threads WHERE thread_key = ?",
                               (out["thread_key"],)).fetchone()
        self.assertEqual((th[0], th[1]), (out["followup_due_at"], mid))

    def test_hook_passes_on_result_and_none(self):
        with mock.patch("jobhunter.threads.on_confirm", lambda c, row: {"thread_key": "em:x", "followup_due_at": "t"}):
            self.assertEqual(hooks.on_confirm(self.conn, {}), {"thread_key": "em:x", "followup_due_at": "t"})
        with mock.patch("jobhunter.threads.on_confirm", lambda c, row: None):
            self.assertIsNone(hooks.on_confirm(self.conn, {}))

    def test_mark_armed_message_id_validation(self):
        d, pc = self.cold_ready()
        r = self.reserve(draft_id=d, precheck_id=pc)
        for bad in ("", "a\r\nBcc: x@example.com", 5):
            with self.subTest(bad=bad):
                with db.tx(self.conn):
                    self.assertDenied("E_VALIDATION", gate.mark_armed, self.conn, r["token"], message_id=bad)
        with db.tx(self.conn):
            gate.mark_armed(self.conn, r["token"])
        self.assertIsNone(self.conn.execute("SELECT message_id FROM actions WHERE token = ?", (r["token"],)).fetchone()[0])


class TestDenials(GateCase):
    def test_paused_and_breakers_first(self):
        d, pc = self.cold_ready()
        with db.tx(self.conn):
            breakers.pause(self.conn, "all", "test", "human")
        self.assertDenied("E_PAUSED", self.reserve, draft_id=d, precheck_id=pc)
        with db.tx(self.conn):
            breakers.unpause(self.conn, "all")
            breakers.trip(self.conn, "gmail.cold", "gmail_bounces_24h", "two bounces")
        self.assertDenied("E_BREAKER_OPEN", self.reserve, draft_id=d, precheck_id=pc)
        with db.tx(self.conn):
            breakers.pause(self.conn, "gmail", "test", "human")
        with db.tx(self.conn):
            self.conn.execute("UPDATE breakers SET state = 'closed' WHERE scope = 'gmail.cold'")
        self.assertDenied("E_BREAKER_OPEN", self.reserve, draft_id=d, precheck_id=pc)

    def test_channel_disabled(self):
        d, pc = self.cold_ready()
        write_config({"channels.email_outreach.enabled": False})
        self.assertDenied("E_CHANNEL_DISABLED", self.reserve, draft_id=d, precheck_id=pc)

    def test_outside_hours(self):
        d, pc = self.cold_ready()
        self.clock.set("2026-09-29T22:30:00Z")
        dn = self.assertDenied("E_OUTSIDE_HOURS", self.reserve, draft_id=d, precheck_id=pc)
        self.assertGreater(dn.retry_after, 0)
        self.clock.set("2026-10-03T12:00:00Z")   # Saturday
        self.assertDenied("E_OUTSIDE_HOURS", self.reserve, draft_id=d, precheck_id=pc)

    def test_pacing_between_sends(self):
        self.send_cold()
        other = self.w.contact("Sam Patel", "sam.patel@tidemark.example")
        with db.tx(self.conn):
            k2 = self.conn.execute("INSERT INTO companies (company_uid, display_name, created_at, updated_at) VALUES "
                                   "('KTIDEMAR', 'Tidemark', ?, ?)", (canon.now(), canon.now())).lastrowid
            self.conn.execute("UPDATE contacts SET company_id = ? WHERE id = ?", (k2, other))
        d, pc = self.cold_ready(other)
        self.conn.execute("UPDATE drafts SET company_id = ? WHERE id = ?", (k2, d))
        dn = self.assertDenied("E_PACING", self.reserve, draft_id=d, precheck_id=pc)
        self.assertTrue(600 <= dn.retry_after <= 1200, dn.retry_after)
        self.clock.advance(minutes=21)
        with db.tx(self.conn):
            pc2 = self.w.precheck("cold_email", "gmail", contact_id=other)["precheck_id"]
        self.assertEqual(self.reserve(draft_id=d, precheck_id=pc2)["status"], "reserved")

    def test_ceiling_and_warmup(self):
        for i in range(5):
            insert_action(self.conn, kind="cold_email", status="sent", reserved_at=self.clock.ago(hours=5 + i),
                          company_id=None, contact_id=None)
        d, pc = self.cold_ready()
        dn = self.assertDenied("E_WARMUP", self.reserve, draft_id=d, precheck_id=pc)
        self.assertEqual(dn.data["warmup_week"], 1)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO warmup (platform, lane_kind, started_at, restart_week) VALUES "
                              "('gmail', 'cold', ?, 1)", (self.clock.ago(days=50),))
        self.assertEqual(self.reserve(draft_id=d, precheck_id=pc)["status"], "reserved")

    def test_exclusions_and_target_skips(self):
        from jobhunter import exclusions
        d, pc = self.cold_ready()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO target_skips (target_key, reason, until, created_at, updated_at) VALUES "
                              "(?, 'no_hook', ?, ?, ?)", ("contact:" + self.conn.execute(
                                  "SELECT contact_uid FROM contacts WHERE id = ?", (self.w.person,)).fetchone()[0],
                                  self.clock.ago(days=-30), canon.now(), canon.now()))
        self.assertDenied("E_TARGET_SKIPPED", self.reserve, draft_id=d, precheck_id=pc)
        with db.tx(self.conn):
            exclusions.add(self.conn, "email", "Alex.Rivera@kestrel.example", "met before")
        self.assertDenied("E_EXCLUDED", self.reserve, draft_id=d, precheck_id=pc)

    def test_address_grade_and_mx(self):
        d, pc = self.cold_ready()
        self.conn.execute("UPDATE contacts SET email_grade = 'C' WHERE id = ?", (self.w.person,))
        self.assertDenied("E_ADDRESS_GRADE", self.reserve, draft_id=d, precheck_id=pc)
        self.conn.execute("UPDATE contacts SET email_grade = 'B', email_mx_ok = 0 WHERE id = ?", (self.w.person,))
        self.assertDenied("E_NO_MX", self.reserve, draft_id=d, precheck_id=pc)

    def test_draft_states_and_presend(self):
        waiting = self.w.draft("cold_email", status="awaiting_approval")
        with db.tx(self.conn):
            pcw = self.w.precheck("cold_email", "gmail", contact_id=self.w.person)["precheck_id"]
        self.assertDenied("E_QC_NOT_APPROVED", self.reserve, draft_id=waiting, precheck_id=pcw)
        self.conn.execute("UPDATE drafts SET status = 'superseded' WHERE id = ?", (waiting,))
        d, pc = self.cold_ready()
        self.conn.execute("UPDATE drafts SET status = 'approved', approved_by = 'auto', expires_at = ? WHERE id = ?",
                          (self.clock.ago(minutes=1), d))
        self.assertDenied("E_DRAFT_EXPIRED", self.reserve, draft_id=d, precheck_id=pc)
        self.conn.execute("UPDATE drafts SET expires_at = NULL, body = body || ' tampered' WHERE id = ?", (d,))
        self.assertDenied("E_QC_HASH_MISMATCH", self.reserve, draft_id=d, precheck_id=pc)
        with mock.patch.object(hooks, "presend", lambda c, i: {"ok": False, "blocks": [["C-DASH", "x"]]}):
            self.assertDenied("E_QC_LINT_FAILED", self.reserve, draft_id=d, precheck_id=pc)

    def test_presend_refusal_code_is_passed_on(self):
        d, pc = self.cold_ready()
        for code in ("E_DRAFT_EXPIRED", "E_QC_HASH_MISMATCH", "E_RESEARCH_STALE", "E_QC_NOT_APPROVED",
                     "E_QC_LINT_FAILED"):
            res = {"ok": False, "code": code, "sha256": None, "send_text": None, "blocks": [["X", "y"]]}
            with self.subTest(code=code), mock.patch.object(hooks, "presend", lambda c, i, r=res: r):
                dn = self.assertDenied(code, self.reserve, draft_id=d, precheck_id=pc)
                self.assertEqual(dn.data["blocks"], [["X", "y"]])
        # an unknown or missing code stays the lint refusal
        with mock.patch.object(hooks, "presend", lambda c, i: {"ok": False, "code": "E_SOMETHING", "blocks": []}):
            self.assertDenied("E_QC_LINT_FAILED", self.reserve, draft_id=d, precheck_id=pc)
        with mock.patch.object(hooks, "presend", lambda c, i: None):
            self.assertDenied("E_QC_LINT_FAILED", self.reserve, draft_id=d, precheck_id=pc)

    def test_research_stale(self):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO research_facts (fact_uid, subject_kind, subject_id, text, snippet, source_type, "
                              "source_url, retrieved_at, created_at) VALUES ('RAAAAAAA', 'person', ?, 't', 's', 'web', "
                              "'https://example.com/a', ?, ?)", (self.w.person, self.clock.ago(days=20), canon.now()))
        d = self.w.draft("cold_email", payload={"hook": {"fact_id": "RAAAAAAA"}})
        with db.tx(self.conn):
            pc = self.w.precheck("cold_email", "gmail", contact_id=self.w.person)["precheck_id"]
        self.assertDenied("E_RESEARCH_STALE", self.reserve, draft_id=d, precheck_id=pc)

    def test_precheck_rules(self):
        d = self.w.draft("cold_email")
        self.assertDenied("E_PRECHECK_MISSING", self.reserve, draft_id=d, precheck_id=999)
        with db.tx(self.conn):
            pc = self.w.precheck("cold_email", "gmail", contact_id=self.w.person)["precheck_id"]
        self.clock.advance(minutes=16)
        self.assertDenied("E_PRECHECK_STALE", self.reserve, draft_id=d, precheck_id=pc)
        other = self.w.contact("Jordan Lee", "jordan.lee@kestrel.example")
        with db.tx(self.conn):
            pc2 = self.w.precheck("cold_email", "gmail", contact_id=other)["precheck_id"]
        self.assertDenied("E_PRECHECK_MISSING", self.reserve, draft_id=d, precheck_id=pc2)

    def test_precheck_already_done_imports(self):
        with db.tx(self.conn):
            res = self.w.precheck("cold_email", "gmail", {"sent_to_address": 1}, contact_id=self.w.person)
        self.assertEqual(res["result"], "already_done")
        a = self.conn.execute("SELECT * FROM actions WHERE token = ?", (res["imported_token"],)).fetchone()
        self.assertEqual((a["status"], a["route"], a["first_touch"]), ("imported", "import", 1))
        d = self.w.draft("cold_email")
        # the imported action already blocks the person (duplicate rules run before the precheck rules)
        self.assertDenied("E_DUP_PERSON", self.reserve, draft_id=d, precheck_id=res["precheck_id"])
        with db.tx(self.conn):
            self.conn.execute("DELETE FROM actions WHERE token = ?", (res["imported_token"],))
        self.assertDenied("E_ALREADY_DONE", self.reserve, draft_id=d, precheck_id=res["precheck_id"])

    def test_precheck_validation(self):
        ev = {"kind": "cold_email", "platform": "gmail", "observed_at": canon.now(),
              "checks": [{"name": "sent_to_address", "value": 0}]}
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", gate.record_precheck, self.conn, "cold_email", "gmail", ev, "agent",
                              contact_id=self.w.person)
        enable_linkedin(self.conn)
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", self.w.precheck, "li_invite", "linkedin", {"vanity_slug": "someone-else"},
                              contact_id=self.w.person)
            res = self.w.precheck("li_invite", "linkedin", {"profile_button": "Message"}, contact_id=self.w.person)
        self.assertEqual(res["result"], "uncertain")
        self.assertTrue(self.conn.execute("SELECT 1 FROM human_tasks WHERE kind = 'resolve_unknown'").fetchone())


class TestBrowserFlow(GateCase):
    def setUp(self):
        super().setUp()
        enable_linkedin(self.conn)

    def li_ready(self, note="Hi Alex, I read your post on returns."):
        d = self.w.draft("li_invite_note", body=note)
        with db.tx(self.conn):
            pc = self.w.precheck("li_invite", "linkedin", contact_id=self.w.person)["precheck_id"]
        self.w.detect_clear("linkedin")
        return d, pc

    def li_reserve(self, d, pc, agent=OU):
        with db.tx(self.conn):
            return gate.reserve(self.conn, kind="li_invite", draft_id=d, precheck_id=pc, platform="linkedin",
                                agent_id=agent, route="browser", cycle_id=None)

    def test_invite_arm_confirm_and_sequence(self):
        d, pc = self.li_ready()
        r = self.li_reserve(d, pc)
        a = self.conn.execute("SELECT * FROM actions WHERE token = ?", (r["token"],)).fetchone()
        self.assertEqual((a["li_note"], a["li_msg_seq"], a["first_touch"]), (1, 1, 1))
        self.assertEqual(r["guard"], {"fill": True, "commit_after_arm": 2})
        with db.tx(self.conn):
            self.assertEqual(gate.open_token(self.conn, OU)["token"], r["token"])
            out = gate.arm(self.conn, r["token"], "Hi Alex, I read your post on returns.\n", agent_id=OU)
        self.assertTrue(out["armed"])
        with db.tx(self.conn):
            res = gate.confirm(self.conn, r["token"], "Invitation sent toast", agent_id=OU)
        self.assertEqual(res["status"], "sent")
        self.assertEqual(self.conn.execute("SELECT state FROM threads WHERE thread_key = ?",
                                           (res["thread_key"],)).fetchone()[0], "invite_pending")

    def test_one_open_token_per_agent(self):
        d, pc = self.li_ready()
        self.li_reserve(d, pc)
        other = self.w.contact("Jordan Lee", None, "jordan-lee-example")
        d2 = self.w.draft("li_invite_note", contact_id=other, body="")
        with db.tx(self.conn):
            pc2 = self.w.precheck("li_invite", "linkedin", contact_id=other)["precheck_id"]
        self.assertDenied("E_TOKEN_OPEN", self.li_reserve, d2, pc2)

    def test_arm_mismatch_fails_token_and_persists(self):
        d, pc = self.li_ready()
        r = self.li_reserve(d, pc)
        # the diff text comes from drafts.send_text: presend (which records a qc_results row) is not called
        no_presend = mock.Mock(side_effect=AssertionError("presend called"))
        with self.assertRaises(Denied) as cm, mock.patch.object(hooks, "presend", no_presend):
            with db.tx(self.conn):
                gate.arm(self.conn, r["token"], "Hi Alex \u2014 I read your post.", agent_id=OU)
        self.assertEqual(cm.exception.code, "E_OBSERVED_MISMATCH")
        self.assertIn("first_diff_at", cm.exception.data["mismatch"])
        self.assertFalse(no_presend.called)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM qc_results WHERE stage = 'presend'").fetchone()[0], 0)
        a = self.conn.execute("SELECT status, fail_reason FROM actions WHERE token = ?", (r["token"],)).fetchone()
        self.assertEqual(tuple(a), ("failed", "observed_text_mismatch"))
        # the slot is free again: a new precheck and token for the same person work
        with db.tx(self.conn):
            pc2 = self.w.precheck("li_invite", "linkedin", contact_id=self.w.person)["precheck_id"]
        self.clock.advance(minutes=2)
        self.assertEqual(self.li_reserve(d, pc2)["status"], "reserved")

    def test_curly_quotes_and_nbsp_do_not_mismatch(self):
        d, pc = self.li_ready(note="Hi Alex, I liked 'Returns at scale'.")
        r = self.li_reserve(d, pc)
        with db.tx(self.conn):
            self.assertTrue(gate.arm(self.conn, r["token"], "Hi\u00a0Alex, I liked \u2018Returns at scale\u2019.  ",
                                     agent_id=OU)["armed"])

    def test_fail_rules(self):
        d, pc = self.li_ready()
        r = self.li_reserve(d, pc)
        agent = Caller("agent", OU)
        with db.tx(self.conn):
            self.assertDenied("E_FAIL_NOT_ALLOWED", gate.fail, self.conn, r["token"], "smtp_rejected", "x", agent)
        with open(os.path.join(paths.guard_dir(), r["token"] + ".jsonl"), "w") as fh:
            fh.write(json.dumps({"class": "commit", "token": r["token"], "action": "click"}) + "\n")
        with db.tx(self.conn):
            self.assertDenied("E_FAIL_NOT_ALLOWED", gate.fail, self.conn, r["token"], "not_attempted", "x", agent)
        os.unlink(os.path.join(paths.guard_dir(), r["token"] + ".jsonl"))
        with db.tx(self.conn):
            self.assertDenied("E_NOT_FOUND", gate.fail, self.conn, r["token"], "not_attempted", "x",
                              Caller("agent", AP))
            gate.fail(self.conn, r["token"], "not_attempted", "dialog did not open", agent)
        self.assertEqual(self.conn.execute("SELECT status FROM actions WHERE token = ?", (r["token"],)).fetchone()[0],
                         "failed")
        # from armed: refused
        with db.tx(self.conn):
            pc2 = self.w.precheck("li_invite", "linkedin", contact_id=self.w.person)["precheck_id"]
        self.clock.advance(minutes=2)
        r2 = self.li_reserve(d, pc2)
        with db.tx(self.conn):
            gate.arm(self.conn, r2["token"], "Hi Alex, I read your post on returns.", agent_id=OU)
            self.assertDenied("E_FAIL_NOT_ALLOWED", gate.fail, self.conn, r2["token"], "not_attempted", "x", agent)

    def test_expiry_unknown_blocks_and_counts(self):
        d, pc = self.li_ready()
        r = self.li_reserve(d, pc)
        self.clock.advance(minutes=31)
        with db.tx(self.conn):
            self.assertEqual(gate.expire(self.conn), 1)
        a = self.conn.execute("SELECT status FROM actions WHERE token = ?", (r["token"],)).fetchone()
        self.assertEqual(a[0], "unknown")
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "li_invite", contact_id=self.w.person, company_id=self.w.company)
        self.assertIn("E_DUP_PERSON", [h["code"] for h in hits])
        self.assertEqual(ceilings.count_writes(self.conn, ["linkedin"], ["li_invite"]), 1)

    def test_unknown_after_click_error(self):
        d, pc = self.li_ready()
        r = self.li_reserve(d, pc)
        with db.tx(self.conn):
            gate.arm(self.conn, r["token"], "Hi Alex, I read your post on returns.", agent_id=OU)
            res = gate.mark_unknown(self.conn, r["token"], "Something went wrong", after_click_error=True)
        self.assertEqual(res["status"], "failed_after_click")

    def test_needs_vanity_and_linkedin_disabled(self):
        d, pc = self.li_ready()
        self.conn.execute("UPDATE contacts SET needs_vanity = 1 WHERE id = ?", (self.w.person,))
        self.assertDenied("E_PRECONDITION", self.li_reserve, d, pc)
        self.conn.execute("UPDATE contacts SET needs_vanity = 0 WHERE id = ?", (self.w.person,))
        with db.tx(self.conn):
            db.meta_set(self.conn, "channel_linkedin_enabled", "0", "human")
        self.assertDenied("E_CHANNEL_DISABLED", self.li_reserve, d, pc)

    def test_third_linkedin_touch_refused(self):
        enable_linkedin(self.conn)
        t1 = insert_action(self.conn, kind="li_invite", li_note=1, li_msg_seq=1, contact_id=self.w.person,
                           company_id=self.w.company, reserved_at=self.clock.ago(days=10))
        insert_thread(self.conn, t1, contact_id=self.w.person, company_id=self.w.company, channel="linkedin",
                      state="invite_accepted")
        insert_action(self.conn, kind="li_message", li_msg_seq=2, contact_id=self.w.person, company_id=self.w.company,
                      reserved_at=self.clock.ago(days=5))
        with db.tx(self.conn):
            self.assertEqual(gate.next_li_seq(self.conn, self.w.person), 3)
            hits = gate.dedup_hits(self.conn, "li_followup", contact_id=self.w.person, li_seq=3)
        self.assertIn("E_DUP_LI_TOUCH", [h["code"] for h in hits])


class TestApplications(GateCase):
    def app_ready(self, job=None):
        job = job or self.w.job
        fields = [{"label": "Notice period", "value": "30 days"}]
        d = self.w.draft("application_package", job_id=job, payload={"fields": fields,
                                                                     "resume_filename": "Alex_Rivera_Resume.pdf"})
        with db.tx(self.conn):
            pc = self.w.precheck("application", "greenhouse", job_id=job)["precheck_id"]
        self.w.detect_clear("greenhouse")
        return d, pc, fields

    def reserve_app(self, d, pc, job=None):
        with db.tx(self.conn):
            return gate.reserve(self.conn, kind="application", draft_id=d, precheck_id=pc, platform="greenhouse",
                                agent_id=AP, route="browser", job_id=job)

    def test_application_flow(self):
        d, pc, fields = self.app_ready()
        r = self.reserve_app(d, pc)
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (self.w.job,)).fetchone()[0],
                         "applying")
        observed = json.dumps({"fields": fields, "resume_filename_visible": "Alex_Rivera_Resume.pdf"})
        with db.tx(self.conn):
            gate.arm(self.conn, r["token"], observed, agent_id=AP)
            gate.confirm(self.conn, r["token"], "Thank you for applying", agent_id=AP)
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id = ?", (self.w.job,)).fetchone()[0],
                         "applied")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM applications").fetchone()[0], 1)
        # a second application to the same job is a duplicate
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "application", job_id=self.w.job, company_id=self.w.company)
        self.assertIn("E_DUP_JOB", [h["code"] for h in hits])

    def test_role_similarity_and_company_cap(self):
        d, pc, _f = self.app_ready()
        self.reserve_app(d, pc)
        with db.tx(self.conn):
            self.conn.execute("UPDATE actions SET status = 'unknown'")
        from tests.helpers import insert_job
        j2 = insert_job(self.conn, company_id=self.w.company, status="apply_queued", title="Senior Data Analyst")
        self.conn.execute("UPDATE jobs SET role_key = 'analyst data senior' WHERE id = ?", (j2,))
        d2, pc2, _f = self.app_ready(job=j2)
        self.clock.advance(days=2)
        write_heartbeat()
        with db.tx(self.conn):
            pc2 = self.w.precheck("application", "greenhouse", job_id=j2)["precheck_id"]
        self.w.detect_clear("greenhouse")
        self.assertDenied("E_ROLE_SIMILAR", self.reserve_app, d2, pc2)
        with db.tx(self.conn):
            db.meta_set(self.conn, "company_apps_per_30d", "1", "config_apply")
        self.assertDenied("E_COMPANY_APP_CAP", self.reserve_app, d2, pc2)

    def test_form_application_records_resume_variant(self):
        vid = self.conn.execute(
            "INSERT INTO resume_variants (variant_uid, job_id, mode, base_sha256, pdf_path, txt_path, pdf_sha256, "
            "created_at) VALUES ('VAAAAAAA', ?, 'light', 'b', 'r.pdf', 'r.txt', 'p', ?)", (self.w.job, canon.now())).lastrowid
        fields = [{"label": "Notice period", "value": "30 days"}]
        # the U3 package shape: {"payload": {job_uid, resume_variant_uid, fields, ...}}
        d = self.w.draft("application_package", job_id=self.w.job, payload={"payload": {
            "fields": fields, "resume_variant_uid": "VAAAAAAA", "resume_filename": "Alex_Rivera_Resume.pdf"}})
        with db.tx(self.conn):
            pc = self.w.precheck("application", "greenhouse", job_id=self.w.job)["precheck_id"]
        self.w.detect_clear("greenhouse")
        r = self.reserve_app(d, pc)
        observed = json.dumps({"fields": fields, "resume_filename_visible": "Alex_Rivera_Resume.pdf"})
        with db.tx(self.conn):
            gate.arm(self.conn, r["token"], observed, agent_id=AP)
            gate.confirm(self.conn, r["token"], "Thank you for applying", agent_id=AP)
        row = self.conn.execute("SELECT route, resume_variant_id, package_draft_id FROM applications").fetchone()
        self.assertEqual((row["resume_variant_id"], row["package_draft_id"]), (vid, d))

    def test_variant_uid_of_payload_shapes(self):
        f = gate.variant_uid_of_payload
        self.assertEqual(f(json.dumps({"payload": {"resume_variant_uid": "VAAAAAAA"}})), "VAAAAAAA")
        self.assertEqual(f(json.dumps({"attachment": {"variant_uid": "VBBBBBBB"}})), "VBBBBBBB")
        self.assertEqual(f(json.dumps({"resume_variant_uid": "VCCCCCCC"})), "VCCCCCCC")
        for bad in (None, "", "not json", "[]", json.dumps({"payload": "x"})):
            self.assertIsNone(f(bad))

    def test_board_detection_under_site_spelling(self):
        """detect_page.js stores boards as site:<board>; reserve --platform <board> must find that clear."""
        write_config({"boards.sites.naukri.apply": "browser"})
        fields = [{"label": "Notice period", "value": "30 days"}]
        d = self.w.draft("application_package", job_id=self.w.job, payload={"fields": fields})
        ev = {"kind": "application", "platform": "naukri", "observed_at": canon.now(),
              "checks": [{"name": "applied_badge", "value": False}, {"name": "already_applied_text", "value": False}]}
        with db.tx(self.conn):
            pcn = gate.record_precheck(self.conn, "application", "naukri", ev, "agent", job_id=self.w.job)["precheck_id"]
        args = dict(kind="application", draft_id=d, precheck_id=pcn, platform="naukri", agent_id=AP, route="browser")
        with db.tx(self.conn):
            self.assertDenied("E_DETECT_MISSING", gate.reserve, self.conn, **args)
        det = self.w.detect_clear("site:naukri")
        with db.tx(self.conn):
            r = gate.reserve(self.conn, **args)
        a = self.conn.execute("SELECT detect_id FROM actions WHERE token = ?", (r["token"],)).fetchone()
        self.assertEqual(a[0], det)
        with db.tx(self.conn):
            gate.arm(self.conn, r["token"], json.dumps({"fields": fields}), agent_id=AP)
        post = self.conn.execute("INSERT INTO detections (platform, source, verdict, created_at) VALUES "
                                 "('site:naukri', 'agent', 'clear', ?)", (canon.now(),)).lastrowid
        with db.tx(self.conn):
            self.assertEqual(gate.confirm(self.conn, r["token"], "Applied", post_detect_id=post, agent_id=AP)["status"],
                             "sent")

    def test_site_apply_mode_off(self):
        d, pc, _f = self.app_ready()
        with db.tx(self.conn):
            ev = {"kind": "application", "platform": "naukri", "observed_at": canon.now(),
                  "checks": [{"name": "applied_badge", "value": False}, {"name": "already_applied_text", "value": False}]}
            pcn = gate.record_precheck(self.conn, "application", "naukri", ev, "agent", job_id=self.w.job)["precheck_id"]
        with db.tx(self.conn):
            self.assertDenied("E_CHANNEL_DISABLED", gate.reserve, self.conn, kind="application", draft_id=d,
                              precheck_id=pcn, platform="naukri", agent_id=AP, route="browser")


class TestInmailReadBack(GateCase):
    def test_inmail_observed_subject(self):
        act = {"kind": "inmail", "platform": "linkedin", "token": "TAAAAAAAAAAA"}
        want = canon.canonical_send_text("inmail", "Returns forecasting", "Hi Alex,\n\nA note.", None, None)
        got = gate._observed_canonical(self.conn, act, "Subject:  Returns forecasting\n\nHi Alex,\n\nA note.\n")
        self.assertEqual(got, want)
        changed = gate._observed_canonical(self.conn, act, "Subject: Something else\n\nHi Alex,\n\nA note.")
        self.assertNotEqual(canon.sha256_text(changed), canon.sha256_text(want))
        self.assertEqual(gate._observed_canonical(self.conn, act, "Hi Alex,\n\nA note."), "Hi Alex,\n\nA note.")


class TestDetectSpellings(GateCase):
    def stop(self, platform: str) -> int:
        return self.conn.execute("INSERT INTO detections (platform, source, verdict, code, created_at) VALUES "
                                 "(?, 'guard', 'stop', 'x', ?)", (platform, canon.now())).lastrowid

    def test_board_id_and_site_prefix_are_one_platform(self):
        from jobhunter.detect import recent_clear, same_platform
        c = self.w.detect_clear("site:naukri")
        self.assertEqual(recent_clear(self.conn, "naukri")["id"], c)
        self.assertEqual(recent_clear(self.conn, "site:naukri")["id"], c)
        self.assertIsNone(recent_clear(self.conn, "instahyre"))
        self.stop("naukri")
        self.assertIsNone(recent_clear(self.conn, "site:naukri"))
        c2 = self.w.detect_clear("naukri")
        self.assertEqual(recent_clear(self.conn, "site:naukri")["id"], c2)
        self.assertTrue(same_platform("site:naukri", "naukri") and same_platform("NAUKRI", "site:naukri"))
        self.assertFalse(same_platform("site:instahyre", "naukri"))
        self.clock.advance(minutes=11)
        self.assertIsNone(recent_clear(self.conn, "naukri"))

    def test_ats_family_stop_cancels_a_clear(self):
        from jobhunter.detect import recent_clear
        self.w.detect_clear("lever")
        self.assertIsNone(recent_clear(self.conn, "greenhouse"))      # another ATS's clear does not count
        c = self.w.detect_clear("greenhouse")
        self.assertEqual(recent_clear(self.conn, "greenhouse")["id"], c)
        self.stop("ats")                                              # the guard records ATS host stops as 'ats'
        self.assertIsNone(recent_clear(self.conn, "greenhouse"))
        c = self.w.detect_clear("linkedin")
        self.stop("site:naukri")
        self.assertEqual(recent_clear(self.conn, "linkedin")["id"], c)


class TestFollowups(GateCase):
    def test_followup_binding_from_thread(self):
        token, out = self.send_cold()
        key = out["thread_key"]
        self.clock.advance(days=6)
        d = self.w.draft("followup_email", thread_key=key, subject="Re: Returns forecasting",
                         body="Hi Alex,\n\nOne more point.\n\nThanks,")
        with db.tx(self.conn):
            pc = self.w.precheck("followup_email", "gmail", thread_key=key)["precheck_id"]
        r = self.reserve(kind="followup_email", draft_id=d, precheck_id=pc)
        a = self.conn.execute("SELECT * FROM actions WHERE token = ?", (r["token"],)).fetchone()
        first = self.conn.execute("SELECT * FROM actions WHERE token = ?", (token,)).fetchone()
        self.assertEqual((a["contact_id"], a["company_id"], a["recipient"], a["first_touch"]),
                         (first["contact_id"], first["company_id"], first["recipient"], 0))
        with db.tx(self.conn):
            gate.mark_armed(self.conn, r["token"])
            gate.confirm(self.conn, r["token"], "250 OK")
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "followup_email", thread_key=key)
        self.assertEqual(sorted({h["code"] for h in hits}), ["E_DUP_THREAD_FOLLOWUP", "E_FOLLOWUP_BINDING"])

    def test_web_route_followup_plan_without_message_id(self):
        """A web-route send has no Message-ID: the reply check searches the person's messages since the first send,
        and the company check falls back to the recipient's domain when the company has none on file."""
        token, out = self.send_cold()
        key = out["thread_key"]
        self.conn.execute("UPDATE threads SET first_message_id = NULL WHERE thread_key = ?", (key,))
        self.conn.execute("UPDATE companies SET domain = NULL WHERE id = ?", (self.w.company,))
        first = self.conn.execute("SELECT sent_at, recipient FROM actions WHERE token = ?", (token,)).fetchone()
        day = first["sent_at"][:10].replace("-", "/")
        self.clock.advance(days=6)
        with db.tx(self.conn):
            plan = gate.precheck_plan(self.conn, "followup_email", route="browser", thread_key=key)
        q = {c["name"]: c["query"] for c in plan["checks"]}
        self.assertEqual(q, {"thread_has_reply": "from:%s after:%s -in:sent" % (first["recipient"], day),
                             "company_inbound_since_first": "from:(kestrel.example) after:%s" % day})
        # a free-mail address names no company domain; the reply query still stands. The free-mail list is
        # extended with a reserved domain so the repo holds no real mail provider address.
        self.conn.execute("UPDATE actions SET recipient = 'alex.rivera@freemail.example' WHERE token = ?", (token,))
        free = keys.freemail_domains() | {"freemail.example"}
        with mock.patch.object(keys, "freemail_domains", return_value=free), db.tx(self.conn):
            plan = gate.precheck_plan(self.conn, "followup_email", route="browser", thread_key=key)
        q = {c["name"]: c["query"] for c in plan["checks"]}
        self.assertEqual(q["thread_has_reply"], "from:alex.rivera@freemail.example after:%s -in:sent" % day)
        self.assertIsNone(q["company_inbound_since_first"])
        # without the free-mail entry the same reserved domain would name a company: the patch is what counts
        with db.tx(self.conn):
            plan = gate.precheck_plan(self.conn, "followup_email", route="browser", thread_key=key)
        self.assertEqual(plan["checks"][1]["query"], "from:(freemail.example) after:%s" % day)
        # with a Message-ID the thread itself is searched
        self.conn.execute("UPDATE threads SET first_message_id = '<m1@kestrel.example>' WHERE thread_key = ?", (key,))
        with db.tx(self.conn):
            plan = gate.precheck_plan(self.conn, "followup_email", route="browser", thread_key=key)
        self.assertEqual(plan["checks"][0]["query"], "rfc822msgid:<m1@kestrel.example>")

    def test_followup_needs_thread(self):
        d = self.w.draft("followup_email", thread_key=None)
        with db.tx(self.conn):
            self.assertDenied("E_FOLLOWUP_BINDING", gate.reserve, self.conn, kind="followup_email", draft_id=d,
                              precheck_id=1, platform="gmail", agent_id="system:mailer", route="mailer")


class TestReferralAsk(GateCase):
    """A referral ask only in the thread where that person replied, always human-approved (6.1 step 9)."""

    def replied_thread(self):
        token, out = self.send_cold()
        key = out["thread_key"]
        self.clock.advance(days=1)
        return key

    def referral_precheck(self, key):
        with db.tx(self.conn):
            return self.w.precheck("referral_ask", "gmail", thread_key=key)["precheck_id"]

    def test_cold_draft_as_referral_without_thread_is_refused(self):
        self.send_cold()
        self.clock.advance(days=1)
        jordan = self.w.contact("Jordan Lee", "jordan.lee@kestrel.example")
        d = self.w.draft("cold_email", contact_id=jordan)
        # as a cold email the company cooldown refuses it; as a referral ask it needs the reply thread
        self.assertDenied("E_COMPANY_COOLDOWN", gate.precheck_plan, self.conn, "cold_email", route="mailer",
                          contact_id=jordan)
        with db.tx(self.conn):
            self.assertDenied("E_FOLLOWUP_BINDING", self.w.precheck, "referral_ask", "gmail", contact_id=jordan)
        self.assertDenied("E_FOLLOWUP_BINDING", gate.precheck_plan, self.conn, "referral_ask", route="mailer",
                          contact_id=jordan)
        self.assertDenied("E_FOLLOWUP_BINDING", self.reserve, kind="referral_ask", draft_id=d, precheck_id=1)

    def test_needs_a_reply_in_the_thread_and_human_approval(self):
        key = self.replied_thread()
        d = self.w.draft("followup_email", thread_key=key, subject="Re: Returns forecasting",
                         body="Hi Alex,\n\nWould you refer me for the analyst role?\n\nThanks,")
        pc = self.referral_precheck(key)
        dn = self.assertDenied("E_PRECONDITION", self.reserve, kind="referral_ask", draft_id=d, precheck_id=pc,
                               thread_key=key)
        self.assertEqual(dn.data["hits"][0]["rule"], "referral_after_reply")
        # another person's draft cannot ride on this thread
        jordan = self.w.contact("Jordan Lee", "jordan.lee@kestrel.example")
        dj = self.w.draft("cold_email", contact_id=jordan)
        self.assertDenied("E_FOLLOWUP_BINDING", self.reserve, kind="referral_ask", draft_id=dj, precheck_id=pc,
                          thread_key=key)
        with db.tx(self.conn):
            self.conn.execute("UPDATE threads SET state = 'replied', reply_class = 'positive', reply_at = ? "
                              "WHERE thread_key = ?", (canon.now(), key))
        self.conn.execute("UPDATE drafts SET approved_by = 'auto' WHERE id = ?", (d,))
        self.assertDenied("E_QC_NOT_APPROVED", self.reserve, kind="referral_ask", draft_id=d, precheck_id=pc,
                          thread_key=key)
        self.conn.execute("UPDATE drafts SET approved_by = 'human:chat' WHERE id = ?", (d,))
        # a LinkedIn referral ask cannot use an email thread
        enable_linkedin(self.conn)
        self.assertDenied("E_FOLLOWUP_BINDING", gate.record_precheck, self.conn, "referral_ask", "linkedin",
                          {"kind": "referral_ask", "platform": "linkedin", "observed_at": canon.now(),
                           "checks": [{"name": "already_asked", "value": False}]}, "agent", thread_key=key)
        r = self.reserve(kind="referral_ask", draft_id=d, precheck_id=pc, thread_key=key)
        a = self.conn.execute("SELECT * FROM actions WHERE token = ?", (r["token"],)).fetchone()
        self.assertEqual((a["thread_key"], a["contact_id"], a["first_touch"]), (key, self.w.person, 0))

    def test_trigger_refuses_a_referral_ask_without_reply(self):
        import sqlite3
        key = self.replied_thread()
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            insert_action(self.conn, kind="referral_ask", platform="gmail", contact_id=self.w.person,
                          company_id=self.w.company, thread_key=key, first_touch=0)
        self.assertEqual(str(cm.exception), "E_PRECONDITION")
        insert_action(self.conn, kind="referral_ask", platform="gmail", contact_id=self.w.person, thread_key=key,
                      first_touch=0, route="import", status="imported")


class TestRecipientDomainCompany(GateCase):
    """An email counts for the company that owns the recipient's domain (2.4.2)."""

    def setUp(self):
        super().setUp()
        self.conn.execute("INSERT INTO company_aliases (alias_key, company_id, kind, source, created_at) VALUES "
                          "('dom:kestrel.example', ?, 'dom', 'email', ?)", (self.w.company, canon.now()))
        self.k2 = self.conn.execute("INSERT INTO companies (company_uid, display_name, created_at, updated_at) VALUES "
                                    "('KKCRETAI', 'KC Retail Systems', ?, ?)", (canon.now(), canon.now())).lastrowid
        self.conn.execute("INSERT INTO company_aliases (alias_key, company_id, kind, source, created_at) VALUES "
                          "('id:kcretailsystems', ?, 'name', 'job_board', ?)", (self.k2, canon.now()))

    def test_application_email_to_an_emailed_companys_inbox(self):
        self.send_cold()
        self.clock.advance(days=2)
        from tests.helpers import insert_job
        job = insert_job(self.conn, company_id=self.k2, status="apply_queued", title="Retail Analyst")
        self.conn.execute("UPDATE jobs SET apply_email = 'careers@kestrel.example' WHERE id = ?", (job,))
        t = gate.resolve_target(self.conn, "application_email", job_id=job)
        self.assertEqual((t["company_id"], t["recipient"]), (self.k2, "careers@kestrel.example"))
        hits = gate.dedup_hits(self.conn, "application_email", job_id=job, company_id=self.k2,
                               email="careers@kestrel.example")
        self.assertIn("E_COMPANY_COOLDOWN", [h["code"] for h in hits])
        self.assertDenied("E_COMPANY_COOLDOWN", gate.precheck_plan, self.conn, "application_email", route="mailer",
                          job_id=job)
        q = gate._company_query(self.conn, self.k2, "careers@kestrel.example")
        self.assertIn("kestrel.example", q)
        self.assertIn('"kc retail systems"', q)

    def test_later_cold_email_sees_an_email_filed_under_another_company(self):
        insert_action(self.conn, kind="application_email", platform="gmail", company_id=self.k2,
                      recipient="careers@kestrel.example", reserved_at=self.clock.ago(days=2), first_touch=0)
        hits = gate.dedup_hits(self.conn, "cold_email", contact_id=self.w.person, company_id=self.w.company,
                               email="alex.rivera@kestrel.example")
        self.assertIn("E_COMPANY_COOLDOWN", [h["code"] for h in hits])
        # a free-mail or unrelated domain links nothing
        freemail = "someone" + "@" + "gmail.com"        # spelled apart for the repo leak check
        self.assertEqual(gate._email_scope(self.conn, [], freemail)[0], [])


class TestImportHistory(GateCase):
    """Imported history is recorded even inside a cooldown or cap, so it blocks what comes after (12.20)."""

    def test_cooldown_does_not_drop_history(self):
        items = [{"kind": "cold_email", "recipient": "sam.ortiz@tidemark.example", "company_name": "Tidemark",
                  "sent_at": "2026-09-20T10:30:00Z"},
                 {"kind": "cold_email", "recipient": "jo.park@tidemark.example", "company_name": "Tidemark",
                  "sent_at": "2026-09-21T10:30:00+00:00"},
                 {"kind": "cold_email", "recipient": "Jo.Park@tidemark.example", "company_name": "Tidemark"}]
        with db.tx(self.conn):
            out = gate.import_actions(self.conn, items, "gmail_scan")
        self.assertEqual([i["index"] for i in out["imported"]], [0, 1])
        self.assertEqual(out["skipped"], [{"index": 2, "token": None, "reason": "E_DUP_PERSON"}])
        jo = self.conn.execute("SELECT contact_id, reserved_at, sent_at FROM actions WHERE recipient = "
                               "'jo.park@tidemark.example'").fetchone()
        self.assertEqual((jo["reserved_at"], jo["sent_at"]), ("2026-09-21T10:30:00Z", "2026-09-21T10:30:00Z"))
        self.clock.set("2027-01-05T12:00:00Z")          # after the 90-day cooldown: the person stays blocked
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "cold_email", contact_id=jo["contact_id"])
        self.assertIn("E_DUP_PERSON", [h["code"] for h in hits])

    def test_pending_invite_import_past_the_company_li_cap(self):
        other = self.w.contact("Jordan Lee", None, "jordan-lee-example")
        insert_action(self.conn, kind="li_invite", platform="linkedin", contact_id=self.w.person,
                      company_id=self.w.company, reserved_at=self.clock.ago(days=1))
        with db.tx(self.conn):
            tok, why = gate._import_action_ex(self.conn, "li_invite", "linkedin",
                                              {"contact_id": other, "company_id": self.w.company, "job_id": None,
                                               "thread_key": None, "recipient": "jordan-lee-example"}, "pending")
        self.assertIsNotNone(tok, why)
        with db.tx(self.conn):
            hits = gate.dedup_hits(self.conn, "cold_email", contact_id=other, company_id=self.w.company)
        self.assertIn("E_DUP_PERSON", [h["code"] for h in hits])

    def test_bad_sent_at_is_refused_not_dropped(self):
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", gate.import_actions, self.conn,
                              [{"kind": "cold_email", "recipient": "sam@tidemark.example", "sent_at": "yesterday"}], "x")

    def test_company_name_and_recipient_domain_resolve_together(self):
        with db.tx(self.conn):
            gate.import_actions(self.conn, [{"kind": "cold_email", "recipient": "sam.ortiz@tidemark.example",
                                             "company_name": "Tidemark Analytics Group"}], "gmail_scan")
        cid = self.conn.execute("SELECT company_id FROM actions WHERE recipient = 'sam.ortiz@tidemark.example'"
                                ).fetchone()[0]
        aliases = [r[0] for r in self.conn.execute("SELECT alias_key FROM company_aliases WHERE company_id = ?",
                                                   (cid,))]
        self.assertIn("dom:tidemark.example", aliases)
        from jobhunter import companies
        with db.tx(self.conn):
            self.assertEqual(companies.resolve(self.conn, name="TMA", domain="tidemark.example", source="human"), cid)


class TestAgentCycles(GateCase):
    """Agent callers reserve inside their running cycle: per-cycle ceilings and cycle stop rules count."""

    def setUp(self):
        super().setUp()
        enable_linkedin(self.conn)

    def li_ready(self, contact=None):
        contact = contact or self.w.person
        d = self.w.draft("li_invite_note", contact_id=contact, body="Hi Alex, I read your post on returns.")
        with db.tx(self.conn):
            pc = self.w.precheck("li_invite", "linkedin", contact_id=contact)["precheck_id"]
        self.w.detect_clear("linkedin")
        return d, pc

    def li_reserve(self, d, pc, cycle_id=None):
        with db.tx(self.conn):
            return gate.reserve(self.conn, kind="li_invite", draft_id=d, precheck_id=pc, platform="linkedin",
                                agent_id=OU, route="browser", cycle_id=cycle_id)

    def test_reserve_takes_the_running_cycle_and_refuses_made_up_ones(self):
        d, pc = self.li_ready()
        self.assertDenied("E_VALIDATION", self.li_reserve, d, pc, "C-bogus-0")
        self.assertDenied("E_CALLER_NOT_ALLOWED", self.li_reserve, d, pc, self.cycles[AP])
        r = self.li_reserve(d, pc)
        a = self.conn.execute("SELECT cycle_id, lane FROM actions WHERE token = ?", (r["token"],)).fetchone()
        self.assertEqual(tuple(a), (self.cycles[OU], "outreach"))

    def test_replies_lane_never_holds_a_token(self):
        """1.1.3: the replies lane reads untrusted mail and messages; it never holds a token, although its agent
        holds tokens in outreach cycles. The lane stored on the action is the cycle's own."""
        d, pc = self.li_ready()
        self.conn.execute("UPDATE cycles SET status = 'ok', ended_at = ? WHERE cycle_id = ?", (canon.now(),
                                                                                              self.cycles[OU]))
        rep = insert_cycle(self.conn, "replies")
        self.assertDenied("E_CALLER_NOT_ALLOWED", self.li_reserve, d, pc)             # the newest running cycle
        self.assertDenied("E_CALLER_NOT_ALLOWED", self.li_reserve, d, pc, rep)        # named
        self.assertIsNone(self.conn.execute("SELECT 1 FROM actions WHERE lane = 'replies'").fetchone())
        # Gmail in the browser: refused before any draft is read, whatever lane the caller names
        write_config({"gmail.route": "web_ui"})
        with db.tx(self.conn):
            self.assertDenied("E_CALLER_NOT_ALLOWED", gate.reserve, self.conn, kind="cold_email", draft_id=d,
                              precheck_id=pc, platform="gmail", agent_id=OU, route="browser", cycle_id=rep,
                              lane="outreach")
        out = insert_cycle(self.conn, "outreach")
        with db.tx(self.conn):
            r = gate.reserve(self.conn, kind="li_invite", draft_id=d, precheck_id=pc, platform="linkedin",
                             agent_id=OU, route="browser", cycle_id=out, lane="replies")
        a = self.conn.execute("SELECT cycle_id, lane FROM actions WHERE token = ?", (r["token"],)).fetchone()
        self.assertEqual(tuple(a), (out, "outreach"))

    def test_no_running_cycle(self):
        d, pc = self.li_ready()
        self.conn.execute("UPDATE cycles SET status = 'ok', ended_at = ?", (canon.now(),))
        self.assertDenied("E_PRECONDITION", self.li_reserve, d, pc)
        self.assertDenied("E_PRECONDITION", self.li_reserve, d, pc, self.cycles[OU])

    def test_per_cycle_cap_counts_without_cycle_argument(self):
        for m in (20, 10):
            a = insert_action(self.conn, kind="li_invite", platform="linkedin", reserved_at=self.clock.ago(minutes=m))
            self.conn.execute("UPDATE actions SET cycle_id = ? WHERE id = ?", (self.cycles[OU], a))
        d, pc = self.li_ready()
        dn = self.assertDenied("E_CEILING", self.li_reserve, d, pc)
        self.assertEqual(dn.data["window"], "cycle")

    def li_cycle(self, hours_ago, minutes_long=30):
        cyc = insert_cycle(self.conn, "outreach", status="ok", started_at=self.clock.ago(hours=hours_ago),
                           ended_at=canon.ts_add(self.clock.ago(hours=hours_ago), minutes=minutes_long))
        self.conn.execute("INSERT INTO detections (platform, source, verdict, cycle_id, created_at) VALUES "
                          "('linkedin', 'agent', 'clear', ?, ?)", (cyc, canon.ts_add(self.clock.ago(hours=hours_ago),
                                                                                      minutes=1)))
        return cyc

    def test_linkedin_cycles_per_day_and_idle_gap(self):
        from jobhunter import config, cycles
        cfg = config.load(self.conn)
        # the first LinkedIn cycle of the day is admitted (the running cycle itself is not counted)
        w = cycles.linkedin_window(self.conn, cfg, "outreach", cycle_id=self.cycles[OU])
        self.assertEqual((w["ok"], w["idle_ok"], w["cycles_today"]), (True, True, 0))
        # an email-only cycle is not a LinkedIn cycle
        insert_cycle(self.conn, "outreach", status="ok", started_at=self.clock.ago(minutes=20),
                     ended_at=self.clock.ago(minutes=10))
        d, pc = self.li_ready()
        # a LinkedIn cycle that ended 10 minutes before this one started: the idle gap is not over
        self.li_cycle(hours_ago=0.5, minutes_long=20)
        dn = self.assertDenied("E_PACING", self.li_reserve, d, pc)
        self.assertFalse(dn.data["linkedin_window"]["idle_ok"])
        self.conn.execute("DELETE FROM detections WHERE cycle_id IS NOT NULL")
        for h in (9, 7, 5, 3):
            self.li_cycle(hours_ago=h)
        dn = self.assertDenied("E_CEILING", self.li_reserve, d, pc)
        self.assertEqual(dn.data["linkedin_window"]["cycles_today"], 4)


class TestPlatformSpellingsAndWeekends(GateCase):
    def test_site_prefix_is_one_board_for_ceilings_and_breakers(self):
        write_config({"boards.sites.naukri.apply": "browser"})
        for i in range(5):
            insert_action(self.conn, kind="application", platform="naukri", reserved_at=self.clock.ago(hours=1 + i))
        for p in ("naukri", "site:naukri"):
            with self.subTest(platform=p):
                self.assertDenied("E_CEILING", ceilings.check, self.conn, p, "application")
        self.assertEqual(gate.canonical_platform("site:naukri"), "naukri")
        self.assertEqual(breakers.scopes_for("site:greenhouse", "application")[:2], ["global", "ats"])
        self.assertEqual(breakers.scopes_for("site:naukri", "application")[:2], ["global", "site:naukri"])

    def test_no_cold_writes_on_weekends_on_the_conservative_tier(self):
        import datetime as _dt
        from jobhunter import config
        write_config({"gmail.active_days": [1, 2, 3, 4, 5, 6, 7], "linkedin.active.days": [1, 2, 3, 4, 5, 6, 7],
                      "gmail.recipient_window": ["00:00", "23:59"]})
        cfg = config.load(self.conn)
        sat = _dt.datetime(2026, 10, 3, 12, 0, tzinfo=_dt.timezone.utc)
        for kind, plat in (("cold_email", "gmail"), ("followup_email", "gmail"), ("li_invite", "linkedin"),
                           ("li_message", "linkedin")):
            with self.subTest(kind=kind):
                self.assertFalse(gate.hours_ok(self.conn, cfg, kind, plat, None, sat))
        self.assertTrue(gate.hours_ok(self.conn, cfg, "application_email", "gmail", None, sat))
        self.assertTrue(gate.hours_ok(self.conn, cfg, "li_withdraw", "linkedin", None, sat))
        self.assertTrue(gate.hours_ok(self.conn, cfg, "cold_email", "gmail", None, sat - _dt.timedelta(days=4)))


class TestDupDenialsTripGlobal(GateCase):
    def test_three_dup_denials_in_a_cycle(self):
        cyc = canon.new_cycle_id()
        for _ in range(3):
            with db.tx(self.conn):
                gate.note_denial(self.conn, cyc, Denied("E_DUP_PERSON", "dup"))
        self.assertEqual(self.conn.execute("SELECT state FROM breakers WHERE scope = 'global'").fetchone()[0], "open")


class TestGateCli(GateCase):
    def run_cli(self, argv, agent=OU):
        if agent:
            return agent_cli(agent, argv)          # both identity carriers, as python -I (CLI route 5)
        out = io.StringIO()
        rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_dedup_check_and_status_commands(self):
        self.send_cold()
        uid = self.conn.execute("SELECT contact_uid FROM contacts WHERE id = ?", (self.w.person,)).fetchone()[0]
        rc, env = self.run_cli(["dedup", "check", "--kind", "person", "--contact", uid])
        self.assertEqual((rc, env["code"], env["data"]["allowed"]), (3, "E_DUP_PERSON", False))
        rc, env = self.run_cli(["gate", "status"])
        self.assertEqual((rc, env["data"]), (0, {"open": None}))
        rc, env = self.run_cli(["gate", "reserve", "--kind", "application", "--draft", "DAAAAAAA", "--precheck", "1",
                                "--platform", "greenhouse"], agent=OU)
        self.assertEqual((rc, env["code"]), (11, "E_CALLER_NOT_ALLOWED"))

    def test_reads_and_page_checks_count_in_the_running_cycle(self):
        enable_linkedin(self.conn)
        rc, env = self.run_cli(["usage", "add", "--platform", "linkedin", "--metric", "profile_view"])
        self.assertEqual(rc, 0, env)
        row = self.conn.execute("SELECT cycle_id FROM counters WHERE metric = 'profile_view'").fetchone()
        self.assertEqual(row[0], self.cycles[OU])

    def test_agent_file_confinement_for_evidence(self):
        rc, env = self.run_cli(["gate", "confirm", "TAAAAAAAAAAA", "--evidence-file", "/etc/hosts"])
        self.assertEqual((rc, env["code"]), (10, "E_PATH_NOT_ALLOWED"))


class TestBrowserConsent(GateCase):
    """Per-site browser consent (change request "use the existing Chrome logins, with consent"): no active row,
    no browser use of that site; every site starts at No; revoking stops the site until consent again."""

    def setUp(self):
        super().setUp()
        enable_linkedin(self.conn)
        self.bw = TestBrowserFlow.li_ready.__get__(self)

    def li_reserve(self, d, pc):
        with db.tx(self.conn):
            return gate.reserve(self.conn, kind="li_invite", draft_id=d, precheck_id=pc, platform="linkedin",
                                agent_id=OU, route="browser", cycle_id=None)

    def run_cli(self, argv, agent=None, stdin="", env=None):
        if agent and env is None:
            return agent_cli(agent, argv, stdin=stdin)
        out = io.StringIO()
        rc = cli.main(argv, env=dict(env or {}), stdin=io.StringIO(stdin), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_default_no_and_site_map(self):
        clear_consent()
        view = identity.consent_summary(self.conn)
        self.assertEqual({r["state"] for r in view["sites"]}, {"not_granted"})
        self.assertEqual([r["site"] for r in view["sites"]], list(identity.SITES))
        self.assertEqual(identity.active_consent_sites(), [])
        for p, site in (("gmail", "gmail"), ("linkedin", "linkedin"), ("site:naukri", "naukri"), ("naukri", "naukri"),
                        ("linkedin_jobs", "linkedin"), ("https://www.linkedin.com/feed/", "linkedin"),
                        ("mail.google.com", "gmail"), ("greenhouse", None), ("ats", None), ("site:remoteok", None),
                        ("api:greenhouse", None), ("", None)):
            self.assertEqual(identity.consent_site(p), site, p)
        self.assertIsNone(identity.require_consent(self.conn, "greenhouse"))     # ATS forms use no login
        dn = self.assertDenied("E_CONSENT_MISSING", identity.require_consent, self.conn, "site:naukri")
        self.assertEqual((dn.exit_code, dn.data["site"]), (7, "naukri"))

    def test_reserve_refuses_a_site_without_consent(self):
        d, pc = self.bw()
        for sites, status in ((["gmail"], "granted"), (["linkedin"], "revoked"), (["linkedin"], "declined")):
            write_consent(sites, status=status)
            with self.subTest(sites=sites, status=status):
                self.assertDenied("E_CONSENT_MISSING", self.li_reserve, d, pc)
        clear_consent()
        self.assertDenied("E_CONSENT_MISSING", self.li_reserve, d, pc)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM actions").fetchone()[0], 0)
        write_consent(["linkedin"])
        self.assertRegex(self.li_reserve(d, pc)["token"], r"^T")

    def test_mailer_route_needs_no_browser_consent(self):
        clear_consent()
        token, out = self.send_cold()        # app_password route: code sends, no browser login is used
        self.assertEqual(out["thread_key"], "em:" + token)

    def test_web_ui_email_needs_gmail_consent(self):
        write_config({"gmail.route": "web_ui"})
        d, pc = self.cold_ready()
        self.w.detect_clear("gmail")
        write_consent(["linkedin"])
        self.assertDenied("E_CONSENT_MISSING", self.reserve, draft_id=d, precheck_id=pc, route="browser", agent_id=OU)
        write_consent(["gmail"])
        self.assertTrue(self.reserve(draft_id=d, precheck_id=pc, route="browser", agent_id=OU)["token"])

    def test_reader_fails_closed(self):
        path = paths.consent_file()
        write_consent()
        self.assertEqual(len(identity.active_consent_sites()), len(identity.SITES))
        os.chmod(path, 0o620)                                     # group can write it: nobody has consent
        self.assertEqual(identity.active_consent_sites(), [])
        os.chmod(path, 0o600)
        link = path + ".real"
        os.rename(path, link)
        os.symlink(link, path)                                    # a link is never followed
        self.assertEqual(identity.active_consent_sites(), [])
        os.unlink(path)
        for text in ("not json", "[]", json.dumps({"sites": []}),
                     json.dumps({"sites": {"gmail": {"site": "linkedin", "status": "granted", "granted_at": "x"}}}),
                     json.dumps({"sites": {"gmail": {"status": "granted"}}}),
                     json.dumps({"sites": {"gmail": {"status": "granted", "granted_at": "x", "revoked_at": "y"}}}),
                     json.dumps({"sites": {"gmail": {"status": "yes", "granted_at": "x"}}}),
                     json.dumps({"sites": {"elsewhere": {"status": "granted", "granted_at": "x"}}})):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(path, 0o600)
            with self.subTest(text=text):
                self.assertEqual(identity.active_consent_sites(), [])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"sites": {"gmail": {"status": "granted", "granted_at": "x"}}}))
        self.assertEqual(identity.active_consent_sites(), ["gmail"])

    def test_revoke_trips_the_breaker_until_consent_again(self):
        with db.tx(self.conn):
            res = identity.revoke_consent(self.conn, ["linkedin", "naukri"], by="human:cli")
        self.assertEqual((res["revoked"], res["breakers_tripped"]), (["linkedin", "naukri"], ["linkedin", "site:naukri"]))
        self.assertEqual(os.stat(paths.consent_file()).st_mode & 0o777, 0o600)
        row = self.conn.execute("SELECT * FROM breakers WHERE scope = 'linkedin'").fetchone()
        self.assertEqual((row["state"], row["reason_code"], row["requires_human"]), ("open", "consent_revoked", 1))
        d, pc = self.bw()
        self.assertDenied("E_BREAKER_OPEN", self.li_reserve, d, pc)
        # consent again closes a breaker that was open only for the revoke
        with db.tx(self.conn):
            g = identity.grant_consent(self.conn, ["linkedin"], method="manual_login", by="human:cli")
        self.assertEqual(g["breakers_closed"], ["linkedin"])
        self.assertRegex(self.li_reserve(d, pc)["token"], r"^T")
        # a site that is also stopped for another reason stays stopped for `breaker reset`
        with db.tx(self.conn):
            breakers.trip(self.conn, "site:naukri", "site_challenge", "a CAPTCHA appeared")
            g = identity.grant_consent(self.conn, "naukri", method="chrome_import", chrome_profile="Profile 1",
                                       chrome_profile_name="Personal")
        self.assertEqual(g["breakers_closed"], [])
        self.assertEqual(self.conn.execute("SELECT state FROM breakers WHERE scope = 'site:naukri'").fetchone()[0],
                         "open")
        row = identity.load_consent()["sites"]["naukri"]
        self.assertEqual((row["method"], row["chrome_profile"], row["status"]), ("chrome_import", "Profile 1", "granted"))
        self.assertEqual(identity.consent_summary()["chrome_profile"], "Personal")
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", identity.grant_consent, self.conn, ["naukri"], method="chrome_import",
                              chrome_profile="../Other")
            self.assertDenied("E_VALIDATION", identity.grant_consent, self.conn, ["myspace"], method="manual_login")
            self.assertDenied("E_USAGE", identity.revoke_consent, self.conn, [])
            everything = identity.revoke_consent(self.conn, everything=True)
        self.assertEqual(identity.active_consent_sites(), [])
        self.assertIn("gmail", everything["revoked"])

    def test_changes_outside_the_commands_are_caught(self):
        with db.tx(self.conn):
            first = identity.sync_consent(self.conn)
        self.assertEqual((first["tripped"], len(first["active"])), ([], len(identity.SITES)))
        write_consent([s for s in identity.SITES if s != "gmail"])          # the installer revoked Gmail
        with db.tx(self.conn):
            res = identity.sync_consent(self.conn)
        self.assertEqual(res["tripped"], ["gmail"])
        self.assertEqual(self.conn.execute("SELECT reason_code FROM breakers WHERE scope = 'gmail' AND state = 'open'"
                                           ).fetchone()[0], "consent_revoked")
        with db.tx(self.conn):
            self.assertEqual(identity.sync_consent(self.conn)["tripped"], [])   # once
        write_consent()
        with db.tx(self.conn):
            self.assertEqual(identity.sync_consent(self.conn)["closed"], ["gmail"])
        self.assertEqual(self.conn.execute("SELECT state FROM breakers WHERE scope = 'gmail'").fetchone()[0], "closed")

    def test_commands_are_the_owners(self):
        from jobhunter import auth
        auth.set_pin(None, "482915")
        rc, env = self.run_cli(["browser", "consent", "list"])
        self.assertEqual((rc, len(env["data"]["sites"])), (0, len(identity.SITES)))
        rc, env = self.run_cli(["browser", "consent", "list"], agent=OU)    # lanes get their sites from preflight
        self.assertEqual(env["code"], "E_CALLER_NOT_ALLOWED")
        for argv in (["browser", "consent", "grant", "--site", "gmail", "--method", "manual_login"],
                     ["browser", "consent", "revoke", "--site", "gmail"]):
            with self.subTest(argv=argv):
                rc, env = self.run_cli(argv, agent=OU)
                self.assertEqual(env["code"], "E_CALLER_NOT_ALLOWED", env)
                rc, env = self.run_cli(argv)
                self.assertEqual(env["code"], "E_HUMAN_ONLY", env)
        rc, env = self.run_cli(["--pin-stdin", "browser", "consent", "revoke", "--site", "gmail"], stdin="482915\n")
        self.assertEqual((rc, env["data"]["revoked"]), (0, ["gmail"]))
        rc, env = self.run_cli(["--pin-stdin", "browser", "consent", "revoke", "--site", "gmail", "--all"],
                               stdin="482915\n")
        self.assertEqual(env["code"], "E_USAGE")
        rc, env = self.run_cli(["--pin-stdin", "browser", "consent", "grant", "--site", "gmail", "--method",
                                "manual_login"], stdin="482915\n")
        self.assertEqual((rc, env["data"]["granted"], env["data"]["breakers_closed"]), (0, ["gmail"], ["gmail"]))

    def test_page_views_and_identity_checks_need_consent(self):
        write_consent(["gmail"])
        rc, env = self.run_cli(["usage", "add", "--platform", "linkedin", "--metric", "profile_view"], agent=OU)
        self.assertEqual((rc, env["code"]), (7, "E_CONSENT_MISSING"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM counters WHERE metric = 'profile_view'").fetchone()[0], 0)
        path = os.path.join(paths.ws_dir("outreach"), "work", "ident.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"platform": "linkedin", "observed": {"profile_url": "https://www.linkedin.com/in/example-owner"}},
                      fh)
        rc, env = self.run_cli(["identity", "check", "--platform", "linkedin", "--file", path], agent=OU)
        self.assertEqual(env["code"], "E_CONSENT_MISSING", env)
        write_consent(["linkedin"])
        rc, env = self.run_cli(["usage", "add", "--platform", "linkedin", "--metric", "profile_view"], agent=OU)
        self.assertEqual(rc, 0, env)
        rc, env = self.run_cli(["identity", "check", "--platform", "linkedin", "--file", path], agent=OU)
        self.assertEqual((rc, env["data"]["ok"]), (0, True))


class TestReserveAddressHookFallback(GateCase):
    """hooks.on_reserve_address without the email finder installed: a provider-found address is refused
    (fail closed), any other address goes on."""

    def test_missing_finder_refuses_provider_addresses_only(self):
        import importlib
        real = importlib.import_module

        def no_finder(name, *a, **kw):
            if name.startswith("jobhunter.enrich"):
                raise ImportError("no finder")
            return real(name, *a, **kw)
        ctx = {"kind": "cold_email", "contact_id": self.w.person, "recipient": "x@kestrel.example",
               "company_id": self.w.company, "reserved_at": canon.now()}
        with mock.patch("importlib.import_module", side_effect=no_finder):
            self.assertIsNone(hooks.on_reserve_address(self.conn, ctx))
            self.conn.execute("DROP TRIGGER IF EXISTS t_contact_provider_email_upd")   # test database only
            self.conn.execute("UPDATE contacts SET email_source = 'provider' WHERE id = ?", (self.w.person,))
            self.assertDenied("E_ADDRESS_GRADE", hooks.on_reserve_address, self.conn, ctx)
            self.assertEqual(hooks.on_bounce(self.conn, None, None), [])
            self.assertEqual(hooks.on_optout(self.conn, self.w.person), 0)
            self.assertIsNone(hooks.on_contact_merge(self.conn, 1, 2))
            self.assertIsNone(hooks.on_forget(self.conn, self.w.person))



class TestWebEmailSentReadBack(GateCase):
    """gate confirm --observed-file (change request: confirm a web-route email by reading back the Sent folder):
    the Sent copy must hash to the approved text; a different text makes the action unknown, never sent."""

    TO = "alex.rivera@kestrel.example"
    SENT = "Subject: Returns forecasting\nTo: alex.rivera@kestrel.example\n\nHi Alex,\n\nA short note about returns.\n\nThanks,"

    def setUp(self):
        super().setUp()
        write_config({"gmail.route": "web_ui"})

    def web_armed(self):
        d, pc = self.cold_ready()
        self.w.detect_clear("gmail")
        r = self.reserve(draft_id=d, precheck_id=pc, route="browser", agent_id=OU)
        with db.tx(self.conn):
            self.assertTrue(gate.arm(self.conn, r["token"], self.SENT + "\n", agent_id=OU)["armed"])
        return r["token"], d

    def action(self, token):
        return self.conn.execute("SELECT * FROM actions WHERE token = ?", (token,)).fetchone()

    def run_cli(self, argv, agent=OU):
        return agent_cli(agent, argv)

    def test_matching_sent_copy_confirms(self):
        token, d = self.web_armed()
        # rich-text rendering (a non-breaking space, trailing blanks) folds to the same canonical text
        seen = self.SENT.replace("Hi Alex,", "Hi\u00a0Alex,") + "  \n"
        with db.tx(self.conn):
            out = gate.confirm(self.conn, token, "Message sent; Sent row to the recipient", agent_id=OU,
                               platform_ref="https://mail.google.com/mail/u/0/#sent/abc", observed_text=seen)
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.action(token)["status"], "sent")
        self.assertEqual(self.conn.execute("SELECT status FROM drafts WHERE id = ?", (d,)).fetchone()[0], "sent")

    def test_changed_sent_copy_is_unknown_not_sent(self):
        token, d = self.web_armed()
        changed = self.SENT.replace("returns", "refunds")
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                gate.confirm(self.conn, token, "Message sent", agent_id=OU, observed_text=changed)
        self.assertEqual((cm.exception.code, cm.exception.exit_code), ("E_OBSERVED_MISMATCH", 6))
        self.assertEqual(cm.exception.data["status"], "unknown")
        self.assertIn("first_diff_at", cm.exception.data["mismatch"])
        a = self.action(token)
        # the unknown state survives the refusal; nothing of a confirmed send happened
        self.assertEqual((a["status"], a["sent_at"], a["resolved_at"]), ("unknown", None, None))
        self.assertTrue(a["note"].startswith("sent_readback_mismatch"), a["note"])
        self.assertNotIn("refunds", a["note"])
        self.assertEqual(self.conn.execute("SELECT status FROM drafts WHERE id = ?", (d,)).fetchone()[0], "approved")
        self.assertEqual(self.conn.execute("SELECT contact_state FROM companies WHERE id = ?",
                                           (self.w.company,)).fetchone()[0], "none")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM threads WHERE thread_key = ?", ("em:" + token,)).fetchone())
        task = self.conn.execute("SELECT kind FROM human_tasks WHERE action_id = ?", (a["id"],)).fetchone()
        self.assertEqual(task["kind"], "resolve_unknown")
        # reconcile now owns it: the browser lane gets the Sent, Outbox and Scheduled searches
        with db.tx(self.conn):
            methods = [t["method"] for t in reconcile.work_list(self.conn, "browser", OU) if t["token"] == token]
        self.assertEqual(methods, list(reconcile.WEB_METHODS))
        # and a second confirm of the same token is refused (it is no longer armed)
        with db.tx(self.conn):
            self.assertDenied("E_BAD_TRANSITION", gate.confirm, self.conn, token, "Message sent", agent_id=OU,
                              observed_text=self.SENT)

    def test_read_back_needs_the_subject_line_and_keeps_the_token_armed(self):
        token, _d = self.web_armed()
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", gate.confirm, self.conn, token, "Message sent", agent_id=OU,
                              observed_text="Hi Alex,\n\nA short note about returns.\n\nThanks,")
        self.assertEqual(self.action(token)["status"], "armed")

    def test_read_back_is_only_for_web_route_email(self):
        write_config()                                    # app_password: the mailer sends and confirms
        d, pc = self.cold_ready()
        r = self.reserve(draft_id=d, precheck_id=pc)
        with db.tx(self.conn):
            gate.mark_armed(self.conn, r["token"])
            self.assertDenied("E_VALIDATION", gate.confirm, self.conn, r["token"], "250 OK", observed_text=self.SENT)
        self.assertEqual(self.action(r["token"])["status"], "armed")
        enable_linkedin(self.conn)
        other = self.w.contact("Jordan Lee", None, "jordan-lee-example")
        d2 = self.w.draft("li_invite_note", contact_id=other, body="Hi Jordan, I read your post.")
        with db.tx(self.conn):
            pc2 = self.w.precheck("li_invite", "linkedin", contact_id=other)["precheck_id"]
        self.w.detect_clear("linkedin")
        with db.tx(self.conn):
            li = gate.reserve(self.conn, kind="li_invite", draft_id=d2, precheck_id=pc2, platform="linkedin",
                              agent_id=OU, route="browser", cycle_id=None)
            gate.arm(self.conn, li["token"], "Hi Jordan, I read your post.", agent_id=OU)
            self.assertDenied("E_VALIDATION", gate.confirm, self.conn, li["token"], "Invitation sent", agent_id=OU,
                              observed_text="Hi Jordan, I read your post.")
        self.assertEqual(self.action(li["token"])["status"], "armed")

    def test_cli_observed_file_mismatch(self):
        token, _d = self.web_armed()
        ev = self.home.write_agent_file("outreach", "evidence-%s.txt" % token, "Toast: Message sent\nSent: 1 row\n")
        bad = self.home.write_agent_file("outreach", "readback-%s.txt" % token, self.SENT.replace("Thanks,", "Best,"))
        rc, env = self.run_cli(["gate", "confirm", token, "--evidence-file", ev, "--observed-file", bad])
        self.assertEqual((rc, env["code"], env["data"]["status"]), (6, "E_OBSERVED_MISMATCH", "unknown"), env)
        self.assertEqual(self.action(token)["status"], "unknown")

    def test_cli_observed_file_match(self):
        token, _d = self.web_armed()
        ev = self.home.write_agent_file("outreach", "evidence-%s.txt" % token, "Toast: Message sent\nSent: 1 row\n")
        good = self.home.write_agent_file("outreach", "readback-%s.txt" % token, self.SENT)
        rc, env = self.run_cli(["gate", "confirm", token, "--evidence-file", ev, "--observed-file", good])
        self.assertEqual((rc, env["data"]["status"]), (0, "sent"), env)
        self.assertEqual(self.action(token)["status"], "sent")

    def test_confirm_needs_the_sent_read_back(self):
        """A web-route email is confirmed only by its Sent-folder read-back (change request item 3)."""
        token, d = self.web_armed()
        with db.tx(self.conn):
            self.assertDenied("E_EVIDENCE_MISSING", gate.confirm, self.conn, token, "Toast: Message sent", agent_id=OU)
        self.assertEqual(self.action(token)["status"], "armed")
        ev = self.home.write_agent_file("outreach", "evidence-%s.txt" % token, "Toast: Message sent\n")
        rc, env = self.run_cli(["gate", "confirm", token, "--evidence-file", ev])
        self.assertEqual(env["code"], "E_EVIDENCE_MISSING", env)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.action(token)["status"], "armed")
        self.assertEqual(self.conn.execute("SELECT status FROM drafts WHERE id = ?", (d,)).fetchone()[0], "approved")

    def reserve_web(self):
        """(token, address) of a web-route cold email to Alex; a failed token frees the draft for the next one."""
        d = self.conn.execute("SELECT id FROM drafts WHERE kind = 'cold_email' AND contact_id = ? AND status = "
                              "'approved'", (self.w.person,)).fetchone()
        if d is None:
            d, pc = self.cold_ready()
        else:
            d = d[0]
            with db.tx(self.conn):
                pc = self.w.precheck("cold_email", "gmail", contact_id=self.w.person)["precheck_id"]
        self.w.detect_clear("gmail")
        return self.reserve(draft_id=d, precheck_id=pc, route="browser", agent_id=OU)["token"], self.TO

    def test_arm_checks_the_recipients(self):
        """The compose window must be addressed to the reserved address alone: one To, no Cc, no Bcc."""
        body = "\n\nHi Alex,\n\nA short note about returns.\n\nThanks,"
        subj = "Subject: Returns forecasting"
        bad = [("to_missing", lambda to: subj + body),
               ("to_other_address", lambda to: subj + "\nTo: alex.rivera@other.example" + body),
               ("to_not_one_address", lambda to: subj + "\nTo: %s, jordan.lee@kestrel.example" % to + body),
               ("cc_or_bcc", lambda to: subj + "\nTo: %s\nCc: jordan.lee@kestrel.example" % to + body),
               ("cc_or_bcc", lambda to: subj + "\nTo: %s\nBcc: jordan.lee@kestrel.example" % to + body)]
        for why, text in bad:
            with self.subTest(why=why):
                token, to = self.reserve_web()
                with self.assertRaises(Denied) as cm:
                    with db.tx(self.conn):
                        gate.arm(self.conn, token, text(to), agent_id=OU)
                self.assertEqual(cm.exception.code, "E_OBSERVED_MISMATCH")
                self.assertEqual(cm.exception.data["mismatch"]["recipient"], why)
                self.assertNotIn("example", json.dumps(cm.exception.data))
                a = self.action(token)
                self.assertEqual((a["status"], a["fail_reason"]), ("failed", "observed_text_mismatch"))
        # a header block with an unknown line is a malformed file: the token stays reserved
        token, to = self.reserve_web()
        with db.tx(self.conn):
            self.assertDenied("E_VALIDATION", gate.arm, self.conn, token, "From: me@x.example\n%s\nTo: %s%s"
                              % (subj, to, body), agent_id=OU)
        self.assertEqual(self.action(token)["status"], "reserved")
        with db.tx(self.conn):
            gate.fail(self.conn, token, "not_attempted", "malformed read-back", Caller("agent", OU))
        # headers in any order, a display name, other letter case and an empty Cc line are fine
        token, to = self.reserve_web()
        ok = "To: Alex Rivera <%s>\nCc:\n%s%s" % (to.upper(), subj, body)
        with db.tx(self.conn):
            self.assertTrue(gate.arm(self.conn, token, ok, agent_id=OU)["armed"])

    def test_sent_copy_to_another_address_is_unknown(self):
        token, d = self.web_armed()
        other = self.SENT.replace("To: " + self.TO, "To: %s\nBcc: jordan.lee@kestrel.example" % self.TO)
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                gate.confirm(self.conn, token, "Message sent", agent_id=OU, observed_text=other)
        self.assertEqual((cm.exception.code, cm.exception.data["status"]), ("E_OBSERVED_MISMATCH", "unknown"))
        self.assertEqual(cm.exception.data["mismatch"]["recipient"], "cc_or_bcc")
        a = self.action(token)
        self.assertEqual((a["status"], a["sent_at"]), ("unknown", None))
        self.assertIn("other recipients", a["note"])
        self.assertNotIn("jordan.lee", a["note"])
        task = self.conn.execute("SELECT kind FROM human_tasks WHERE action_id = ?", (a["id"],)).fetchone()
        self.assertEqual(task["kind"], "resolve_unknown")
        self.assertEqual(self.conn.execute("SELECT status FROM drafts WHERE id = ?", (d,)).fetchone()[0], "approved")

    def test_readback_parser(self):
        rb = gate.email_readback("Subject: A: b\nTO: x@y.example\n\nBody: here\n\nmore")
        self.assertEqual((rb["subject"], rb["to"], rb["cc"], rb["bcc"], rb["body"]),
                         ("A: b", "x@y.example", None, None, "Body: here\n\nmore"))
        for bad in ("Subject: a\nSubject: b\n\nbody", "To: x@y.example\n\nbody", "Hi Alex,\n\nbody"):
            with self.subTest(text=bad):
                self.assertDenied("E_VALIDATION", gate.email_readback, bad)


if __name__ == "__main__":
    import unittest
    unittest.main()
