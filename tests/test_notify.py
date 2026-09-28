"""Notifications (design 1.5, M8): checked delivery, back-off, quiet hours, desktop fallback, channel none."""
from __future__ import annotations

import io
import json
import os
import types
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import canon, cli, db, notify
from jobhunter import status as S
from jobhunter.commands import notify as notify_cmd
from tests.fakes.u5 import FakeDesktop, FakeSender, config, seed
from tests.helpers import HomeTestCase

AWAKE = config(owner__notify__quiet_hours=["23:00", "23:30"])


class NotifyCase(HomeTestCase):
    def add(self, key, priority="normal", kind="info", text=None):
        with db.tx(self.conn):
            db.enqueue_notification(self.conn, key, priority, kind, text or ("item " + key))

    def row(self, key):
        return self.conn.execute("SELECT * FROM notifications WHERE dedupe_key = ?", (key,)).fetchone()


class TestFlush(NotifyCase):
    def test_backoff_schedule(self):
        self.assertEqual([notify.backoff_minutes(n) for n in range(0, 7)], [0, 5, 10, 20, 40, 60, 60])

    def test_preview_changes_nothing(self):
        self.add("a")
        out = notify.flush(self.conn, False, config=AWAKE)
        self.assertEqual(out["count"], 1)
        self.assertIn("item a", out["text"])
        self.assertIsNone(self.row("a")["delivered_at"])
        self.assertEqual(self.row("a")["attempts"], 0)

    def test_delivery_marks_rows_only_on_success_highest_priority_first(self):
        self.add("low", "low")
        self.add("normal", "normal")
        self.add("high", "high", "alert", "LinkedIn stopped")
        sender, desk = FakeSender(), FakeDesktop()
        out = notify.flush(self.conn, True, sender=sender, desktop=desk, config=AWAKE)
        self.assertTrue(out["sent"])
        self.assertEqual(out["delivered"], 3)
        channel, target, text = sender.sent[0]
        self.assertEqual((channel, target), ("whatsapp", "test-owner"))
        self.assertTrue(text.startswith("Job Hunter: 3 updates"))
        self.assertLess(text.index("LinkedIn stopped"), text.index("item normal"))
        self.assertLess(text.index("item normal"), text.index("item low"))
        for key in ("low", "normal", "high"):
            self.assertEqual(self.row(key)["delivered_via"], "whatsapp")
        self.assertEqual(desk.shown, [("Job Hunter", "LinkedIn stopped")])
        self.assertTrue(self.row("high")["desktop_shown_at"])
        self.assertEqual(notify.flush(self.conn, True, sender=sender, config=AWAKE)["delivered"], 0)
        # temp message files are removed
        self.assertEqual(os.listdir(os.path.join(self.home.dir, "state", "tmp")), [])

    def test_failure_backs_off_and_retries_later(self):
        self.add("h", "high", "alert")
        bad = FakeSender(ok=False)
        desk = FakeDesktop()
        out = notify.flush(self.conn, True, sender=bad, desktop=desk, config=AWAKE)
        self.assertFalse(out["sent"])
        self.assertEqual(out["failed"], 1)
        r = self.row("h")
        self.assertEqual(r["attempts"], 1)
        self.assertEqual(r["next_attempt_at"], canon.ts_add(self.clock.now(), minutes=5))
        self.assertIn("no active listener", r["last_error"])
        self.assertEqual(S.undelivered_high_count(self.conn), 1)
        # not retried before next_attempt_at; the desktop notification is shown only once
        self.assertEqual(notify.flush(self.conn, True, sender=bad, desktop=desk, config=AWAKE)["failed"], 0)
        self.clock.advance(minutes=5)
        notify.flush(self.conn, True, sender=bad, desktop=desk, config=AWAKE)
        r = self.row("h")
        self.assertEqual(r["attempts"], 2)
        self.assertEqual(r["next_attempt_at"], canon.ts_add(self.clock.now(), minutes=10))
        self.assertEqual(len(desk.shown), 1)
        self.clock.advance(minutes=10)
        good = FakeSender()
        out = notify.flush(self.conn, True, sender=good, desktop=desk, config=AWAKE)
        self.assertEqual(out["delivered"], 1)
        self.assertEqual(S.undelivered_high_count(self.conn), 0)

    def test_quiet_hours_send_only_high_items(self):
        quiet = config(owner__notify__quiet_hours=["22:00", "08:00"])   # the test clock is 05:00 UTC
        self.assertTrue(notify.in_quiet_hours(quiet))
        self.assertFalse(notify.in_quiet_hours(AWAKE))
        self.add("n", "normal")
        sender = FakeSender()
        out = notify.flush(self.conn, True, sender=sender, desktop=FakeDesktop(), config=quiet)
        self.assertEqual(out["delivered"], 0)
        self.assertEqual(sender.sent, [])
        self.add("h", "high", "alert")
        out = notify.flush(self.conn, True, sender=sender, desktop=FakeDesktop(), config=quiet)
        self.assertEqual(out["delivered"], 1)
        self.assertIsNone(self.row("n")["delivered_at"])

    def test_max_items_and_message_size(self):
        for i in range(12):
            self.add("k%02d" % i)
        sender = FakeSender()
        out = notify.flush(self.conn, True, max_items=8, sender=sender, config=AWAKE)
        self.assertEqual(out["delivered"], 8)
        self.add("big", text="x" * (notify.MAX_MESSAGE_CHARS + 50))
        text, n = notify.compose([self.row("big"), self.row("k09")])
        self.assertEqual(n, 1)
        self.assertLessEqual(len(text), notify.MAX_MESSAGE_CHARS + 20)

    def test_channel_none_suppresses_after_desktop(self):
        self.add("h", "high", "alert")
        desk = FakeDesktop()
        out = notify.flush(self.conn, True, sender=FakeSender(), desktop=desk,
                           config=config(owner__notify__channel="none", owner__notify__quiet_hours=["23:00", "23:30"]))
        self.assertEqual(out["suppressed"], 1)
        self.assertEqual(len(desk.shown), 1)
        self.assertEqual(S.undelivered_high_count(self.conn), 0)

    def test_example_number_is_not_sent_to_and_says_it_was_never_set(self):
        self.add("h", "high", "alert", "LinkedIn stopped")
        sender, desk = FakeSender(), FakeDesktop()
        cfg = config(owner__notify__to="+10000000000", owner__notify__quiet_hours=["23:00", "23:30"])
        out = notify.flush(self.conn, True, sender=sender, desktop=desk, config=cfg)
        self.assertEqual(sender.sent, [])
        self.assertEqual((out["suppressed"], out["failed"], out["sent"]), (1, 0, False))
        self.assertIn("never set", out["not_sent_reason"])
        self.assertIn("+10000000000", out["not_sent_reason"])
        self.assertEqual(desk.shown, [("Job Hunter", "LinkedIn stopped")])
        r = self.row("h")
        self.assertEqual((r["suppressed"], r["last_error"], r["attempts"]), (1, "chat number not set", 0))
        self.assertEqual(S.undelivered_high_count(self.conn), 0)

    def test_empty_or_blank_number_is_not_sent_to(self):
        for to in ("", "  "):
            res = notify.send_text("x", config(owner__notify__to=to), sender=FakeSender())
            self.assertFalse(res["ok"])
            self.assertTrue(res["not_configured"])
            self.assertIn("owner.notify.to is empty", res["error"])

    def test_send_text_refuses_the_example_number(self):
        sender = FakeSender()
        res = notify.send_text("x", config(owner__notify__to="+10000000000"), sender=sender)
        self.assertEqual(sender.sent, [])
        self.assertFalse(res["ok"])
        self.assertTrue(res["not_configured"])
        self.assertIn("never set", res["error"])
        self.assertEqual(res["channel"], "whatsapp")

    def test_chat_target_follows_the_install_placeholder_rule(self):
        from jobhunter import install
        for to in install.OWNER_PLACEHOLDERS:
            cfg = config(owner__notify__to=to)
            self.assertIsNone(install.notify_target(cfg))
            self.assertIsNotNone(notify.chat_target(cfg)[2], to)
        cfg = config(owner__notify__to="+15555550100")
        self.assertEqual(notify.chat_target(cfg), ("whatsapp", "+15555550100", None))
        self.assertEqual(install.notify_target(cfg), ("whatsapp", "+15555550100"))

    def test_enqueue_dedupes(self):
        self.assertTrue(notify.enqueue(self.conn, "info", "normal", "one", "dup:1")["queued"])
        self.assertFalse(notify.enqueue(self.conn, "info", "normal", "two", "dup:1")["queued"])
        self.assertEqual(self.row("dup:1")["text"], "one")

    def test_default_sender_uses_ocrun_message_send(self):
        got = []

        def send(ch, to, path):
            with open(path, "r", encoding="utf-8") as fh:
                got.append((ch, to, fh.read()))
            return {"ok": True}
        fake = types.SimpleNamespace(message_send=send)
        with mock.patch.dict("sys.modules", {"jobhunter.ocrun": fake}):
            res = notify.send_text("hello", AWAKE)
        self.assertTrue(res["ok"])
        self.assertEqual(got, [("whatsapp", "test-owner", "hello")])
        with mock.patch.dict("sys.modules", {"jobhunter.ocrun": None}):
            res = notify.send_text("hello", AWAKE)
        self.assertFalse(res["ok"])
        self.assertIn("not available", res["error"])

    def test_desktop_notify_passes_text_as_arguments(self):
        with mock.patch.object(notify.shutil, "which", return_value="/usr/bin/osascript"), \
                mock.patch.object(notify.sys, "platform", "darwin"), \
                mock.patch.object(notify.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(notify.desktop_notify("Job Hunter", 'Stop "now" & $(rm -rf x)'))
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[-1], 'Stop "now" & $(rm -rf x)')
        self.assertNotIn("shell", run.call_args[1])
        with mock.patch.object(notify.shutil, "which", return_value=None), \
                mock.patch.object(notify.sys, "platform", "linux"):
            self.assertFalse(notify.desktop_notify("a", "b"))


class TestStaleItems(NotifyCase):
    """Approvals already decided and questions already answered are not sent (e2e: 'Approve YXCM?' after the
    send, 'Your profile draft is ready' after the profile was confirmed)."""

    def setUp(self):
        super().setUp()
        self.s = seed(self.conn, self.clock)
        with db.tx(self.conn):
            self.conn.execute("UPDATE notifications SET delivered_at = ? WHERE dedupe_key = 'breaker:linkedin:x'",
                              (self.clock.now(),))
        sha = self.conn.execute("SELECT text_sha256 FROM drafts WHERE id = ?", (self.s.d_wait,)).fetchone()[0]
        self.akey = "approval:A7K2:%s" % sha[:8]
        self.add(self.akey, kind="approval", text="Approve A7K2? Cold email to Sam L.")

    def task(self, kind, question, done=False):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO human_tasks (task_uid, kind, question, created_at, done_at) "
                              "VALUES (?, ?, ?, ?, ?)", ("H%07d" % self.conn.execute(
                                  "SELECT count(*) + 100 FROM human_tasks").fetchone()[0], kind, question,
                                  self.clock.now(), self.clock.now() if done else None))

    def test_open_approval_is_sent(self):
        out = notify.flush(self.conn, False, config=AWAKE)
        self.assertEqual((out["count"], out["stale"]), (1, 0))
        self.assertIn("Approve A7K2?", out["text"])

    def test_closed_approval_is_dropped_and_suppressed_on_deliver(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE approval_codes SET closed_at = ? WHERE code = 'A7K2'", (self.clock.now(),))
        self.add("info:1", text="Three new jobs")
        out = notify.flush(self.conn, False, config=AWAKE)
        self.assertEqual((out["count"], out["stale"]), (1, 1))
        self.assertNotIn("A7K2", out["text"])
        self.assertEqual(self.row(self.akey)["suppressed"], 0)   # a preview changes nothing
        sender = FakeSender()
        out = notify.flush(self.conn, True, sender=sender, desktop=FakeDesktop(), config=AWAKE)
        self.assertEqual((out["delivered"], out["stale"]), (1, 1))
        self.assertNotIn("A7K2", sender.sent[0][2])
        r = self.row(self.akey)
        self.assertEqual((r["suppressed"], r["delivered_at"]), (1, None))
        self.assertEqual(r["last_error"], "no longer needed: approval A7K2 is closed")
        self.assertEqual(notify.flush(self.conn, True, sender=sender, config=AWAKE)["delivered"], 0)

    def test_approval_no_longer_waiting_or_text_changed(self):
        self.add("approval:A7K2:00000000", kind="approval", text="Approve A7K2? old text")
        out = notify.flush(self.conn, False, config=AWAKE)
        self.assertEqual((out["count"], out["stale"]), (1, 1))
        self.assertNotIn("old text", out["text"])
        with db.tx(self.conn):
            self.conn.execute("UPDATE drafts SET status = 'approved', approved_by = 'human:chat', updated_at = ? "
                              "WHERE id = ?",
                              (self.clock.ago(seconds=2), self.s.d_wait))
        out = notify.flush(self.conn, False, config=AWAKE)
        self.assertEqual((out.get("count"), out["stale"]), (None, 2))
        # an unknown code (queued by hand) is kept
        self.add("approval:ZZZZ", kind="approval", text="Approve ZZZZ?")
        self.assertIn("ZZZZ", notify.flush(self.conn, False, config=AWAKE)["text"])

    def test_profile_questions_dropped_once_the_profile_is_confirmed(self):
        key = "profile:questions:2026-09-27"
        self.add(key, kind="question", text="Your profile draft is ready. 27 questions are waiting")
        self.assertIn("27 questions", notify.flush(self.conn, False, config=AWAKE)["text"])   # no task: kept
        self.task("confirm_profile", "Answer the profile questions")
        self.assertIn("27 questions", notify.flush(self.conn, False, config=AWAKE)["text"])   # open: kept
        with db.tx(self.conn):
            self.conn.execute("UPDATE human_tasks SET done_at = ?, resolution = 'confirmed' "
                              "WHERE kind = 'confirm_profile'", (self.clock.now(),))
        sender = FakeSender()
        out = notify.flush(self.conn, True, sender=sender, desktop=FakeDesktop(), config=AWAKE)
        self.assertEqual(out["stale"], 1)
        self.assertNotIn("27 questions", sender.sent[0][2])
        self.assertEqual(self.row(key)["suppressed"], 1)
        self.assertIn("profile questions are answered", self.row(key)["last_error"])

    def test_filter_question_follows_its_own_task(self):
        key = "feasibility:years_required:2026-W39"
        self.add(key, kind="question", text="40% of recently rejected jobs failed on: years. Widen it?")
        self.task("relax_gate", "Many recent jobs fail the years_required check (years). Widen it, or keep it?",
                  done=True)
        self.task("relax_gate", "Many recent jobs fail the location check (place). Widen it, or keep it?")
        out = notify.flush(self.conn, False, config=AWAKE)
        self.assertEqual(out["stale"], 1)
        self.assertNotIn("Widen it?", out["text"])
        self.task("relax_gate", "Many recent jobs fail the years_required check (years). Widen it, or keep it?")
        self.assertIn("Widen it?", notify.flush(self.conn, False, config=AWAKE)["text"])

    def test_stale_rows_do_not_take_message_slots(self):
        with db.tx(self.conn):
            self.conn.execute("UPDATE approval_codes SET closed_at = ? WHERE code = 'A7K2'", (self.clock.now(),))
        self.add("info:1", text="one")
        out = notify.flush(self.conn, False, 1, config=AWAKE)
        self.assertEqual((out["count"], out["text"]), (1, "Job Hunter: one"))


class TestNotifyCli(NotifyCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        rc = cli.main(list(argv), env={}, stdin=io.StringIO(""), stdout=out, modules=[notify_cmd])
        return rc, out.getvalue().strip()

    def test_enqueue_and_flush(self):
        path = os.path.join(self.home.dir, "note.txt")
        with open(path, "w") as fh:
            fh.write("Your approval is waiting.")
        rc, text = self.run_cli("notify", "enqueue", "--kind", "approval", "--priority", "normal", "--text-file", path,
                                "--dedupe", "approval:A7K2")
        self.assertEqual(rc, 0, text)
        sender = FakeSender()
        with mock.patch.object(S, "load_config", return_value=AWAKE), \
                mock.patch.object(notify, "_default_sender", sender), \
                mock.patch.object(notify, "desktop_notify", FakeDesktop()):
            rc, text = self.run_cli("notify", "flush", "--deliver", "--quiet")
            self.assertEqual((rc, text), (0, "NO_REPLY"))
            rc, text = self.run_cli("notify", "flush", "--deliver")
            self.assertEqual(json.loads(text)["code"], "NOTHING_TO_DO")
        self.assertIn("Your approval is waiting.", sender.sent[0][2])

    def test_failed_delivery_exits_12(self):
        self.add("h", "high", "alert")
        with mock.patch.object(S, "load_config", return_value=AWAKE), \
                mock.patch.object(notify, "_default_sender", FakeSender(ok=False)), \
                mock.patch.object(notify, "desktop_notify", FakeDesktop()):
            rc, text = self.run_cli("notify", "flush", "--deliver")
        self.assertEqual(rc, 12)
        self.assertEqual(json.loads(text)["code"], "E_OPENCLAW_CALL")

    def test_flush_with_the_example_number_says_so_and_exits_0(self):
        self.add("h", "high", "alert")
        sender = FakeSender()
        cfg = config(owner__notify__to="+10000000000", owner__notify__quiet_hours=["23:00", "23:30"])
        with mock.patch.object(S, "load_config", return_value=cfg), \
                mock.patch.object(notify, "_default_sender", sender), \
                mock.patch.object(notify, "desktop_notify", FakeDesktop()):
            rc, text = self.run_cli("notify", "flush", "--deliver")
        self.assertEqual(rc, 0, text)
        out = json.loads(text)
        self.assertIn("never set", out["message"])
        self.assertEqual(out["data"]["suppressed"], 1)
        self.assertEqual(sender.sent, [])

    def test_test_message_is_human_only(self):
        rc, text = self.run_cli("notify", "test")
        self.assertEqual(rc, 11)


if __name__ == "__main__":
    unittest.main()
