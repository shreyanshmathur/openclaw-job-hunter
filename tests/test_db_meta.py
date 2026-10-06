"""U1 day-1 core: paths, canon, errors, db (connect, tx, migrations, meta write guard), events, cli."""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import types
import unittest

import tests  # noqa: F401
from jobhunter import canon, cli, db, errors, events, paths
from jobhunter.commands import Result, add_command
from jobhunter.errors import Denied
from tests.helpers import HomeTestCase, TempHome, clear_meta, insert_company

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- errors
class TestErrors(unittest.TestCase):
    def test_codes_frozen(self):
        self.assertEqual(len(errors.CODES), 77)
        self.assertEqual(errors.CODES["OK"], 0)
        self.assertEqual(errors.CODES["PENDING"], 0)
        self.assertEqual(errors.CODES["E_INTERNAL"], 1)
        self.assertEqual(errors.CODES["E_USAGE"], 2)
        self.assertEqual(errors.CODES["E_AGENCY_CAP"], 3)
        self.assertEqual(errors.CODES["E_TOO_EARLY"], 4)
        self.assertEqual(errors.CODES["E_CLOCK_SKEW"], 5)
        self.assertEqual(errors.CODES["E_OBSERVED_MISMATCH"], 6)
        self.assertEqual(errors.CODES["E_FOLLOWUP_BINDING"], 7)
        self.assertEqual(errors.CODES["E_CLAIMED"], 8)
        self.assertEqual(errors.CODES["E_NOT_FOUND"], 9)
        self.assertEqual(errors.CODES["E_PATH_NOT_ALLOWED"], 10)
        self.assertEqual(errors.CODES["E_BAD_TRANSITION"], 11)
        self.assertEqual(errors.CODES["E_NETWORK"], 12)
        self.assertEqual(errors.E_CEILING, "E_CEILING")
        # change requests: the email finder (exit 7 and 4) and per-site browser consent (exit 7)
        self.assertEqual(errors.CODES["E_NOT_TARGET"], 7)
        self.assertEqual(errors.CODES["E_ENRICH_UNAVAILABLE"], 4)
        self.assertEqual(errors.CODES["E_CONSENT_MISSING"], 7)
        # the claude-cli route: an OpenClaw cron job that drifted from the install manifest (exit 11)
        self.assertEqual(errors.CODES["E_CRON_DRIFT"], 11)

    def test_denied(self):
        d = Denied("E_CEILING", "cap reached", retry_after=60, data={"used": 5})
        self.assertEqual((d.code, d.exit_code, d.retry_after, d.data["used"]), ("E_CEILING", 4, 60, 5))
        bad = Denied("E_MADE_UP", "x")
        self.assertEqual(bad.code, "E_INTERNAL")
        self.assertEqual(bad.data["unknown_code"], "E_MADE_UP")

    def test_map_sqlite_error(self):
        m = errors.map_sqlite_error
        self.assertEqual(m(sqlite3.IntegrityError("E_COMPANY_COOLDOWN")).code, "E_COMPANY_COOLDOWN")
        self.assertEqual(m(sqlite3.IntegrityError("UNIQUE constraint failed: actions.contact_id")).code,
                         "E_DUP_PERSON")
        self.assertEqual(m(sqlite3.IntegrityError("UNIQUE constraint failed: actions.contact_id, actions.li_msg_seq")
                           ).code, "E_DUP_LI_TOUCH")
        self.assertEqual(m(sqlite3.IntegrityError("UNIQUE constraint failed: index 'u_application_job'")).code,
                         "E_DUP_JOB")
        self.assertEqual(m(sqlite3.IntegrityError("UNIQUE constraint failed: actions.token")).code, "E_INTERNAL")
        self.assertEqual(m(sqlite3.IntegrityError("CHECK constraint failed: x")).code, "E_INTERNAL")
        self.assertEqual(m(sqlite3.IntegrityError("E_NOT_A_CODE")).code, "E_INTERNAL")
        self.assertEqual(m(sqlite3.OperationalError("database is locked")).code, "E_LOCKED")

    def test_map_sqlite_error_enrich(self):
        m = errors.map_sqlite_error
        for target, code in (("enrich_calls.request_id, enrich_calls.provider", "E_ALREADY_DONE"),
                             ("enrich_requests.retry_of", "E_ALREADY_DONE"),
                             ("enrich_request_keys.key_hash", "E_ALREADY_DONE"),
                             ("enrich_requests.contact_id", "E_LOCKED"),
                             ("index 'u_enrich_find_once'", "E_ALREADY_DONE"),
                             ("index 'u_enrich_running_contact'", "E_LOCKED")):
            with self.subTest(target=target):
                self.assertEqual(m(sqlite3.IntegrityError("UNIQUE constraint failed: " + target)).code, code)


# ---------------------------------------------------------------- canon
class TestCanon(unittest.TestCase):
    def tearDown(self):
        canon.set_test_clock(None)

    def test_now_and_clock(self):
        self.assertRegex(canon.now(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        canon.set_test_clock("2026-09-27T05:00:00Z")
        self.assertEqual(canon.now(), "2026-09-27T05:00:00Z")
        self.assertEqual(canon.ts_add(canon.now(), minutes=90), "2026-09-27T06:30:00Z")
        self.assertEqual(canon.seconds_between("2026-09-27T05:00:00Z", "2026-09-27T05:01:00Z"), 60)
        self.assertRaises(ValueError, canon.parse_ts, "2026-09-27 05:00:00")

    def test_ids(self):
        with open(paths.ACL_FILE) as fh:
            acl = json.load(fh)
        vc = acl["value_classes"]
        for p in "JKPRDQH":
            self.assertRegex(canon.new_uid(p), vc["id"])
        self.assertRegex(canon.new_token(), vc["token"])
        self.assertRegex(canon.new_cycle_id(), vc["cycle"])
        self.assertRegex(canon.new_uid("I", 8), r"^I[A-Z2-7]{8}$")
        self.assertRaises(ValueError, canon.new_uid, "j")
        self.assertNotEqual(canon.new_token(), canon.new_token())

    def test_email_signature_round_trip(self):
        approved = canon.canonical_send_text("cold_email", "Pincode models", "Hi Alex,\n\nShort note.\n\nThanks,",
                                             None, "Sam Example\nsam@example.com")
        # web route: the signature is observed inside the body, with rich-text noise
        observed = canon.canonical_send_text(
            "cold_email", " Pincode models ",
            "Hi Alex,\r\n\r\nShort note.   \r\n\r\n\r\n\r\nThanks,\n\nSam\u00a0Example\nsam@example.com\n", None, None)
        self.assertEqual(approved, observed)
        self.assertTrue(approved.startswith("Subject: Pincode models\n\nHi Alex,"))

    def test_normalisation_rules(self):
        t = canon.canonical_send_text("li_message", None, "  It\u2019s \u201cgreat\u201d\u00a0 \n\n\n\nBye  ", None,
                                      "ignored signature")
        self.assertEqual(t, "It's \"great\"\n\nBye")
        self.assertEqual(canon.normalize_text("e\u0301"), "\u00e9")   # NFC
        self.assertTrue(all(ord(ch) < 128 for ch in canon.normalize_text("\u2018a\u2019 \u201cb\u201d")))

    def test_attachment_line(self):
        t = canon.canonical_send_text("application_email", "Role", "Body", None, "Sig",
                                      {"filename": "Alex_Rivera_Resume.pdf", "sha256": "ab" * 32})
        self.assertTrue(t.endswith("\n\nSig\n\nAttachment: Alex_Rivera_Resume.pdf " + "ab" * 32))

    def test_forms(self):
        approved = canon.canonical_send_text("application", None, None, [
            {"label": "Notice period", "type": "text", "value": "30 days", "answer_key": "notice"},
            {"label": "Email", "type": "text", "value": "alex@example.com"}], None,
            {"filename": "Alex_Rivera_Resume.pdf", "sha256": "x"})
        observed = canon.canonical_send_text("application", None, None, [
            {"label": "Email ", "value": "alex@example.com"}, {"label": "Notice period", "value": "30 days"}], None,
            {"filename": "Alex_Rivera_Resume.pdf"})
        self.assertEqual(approved, observed)
        self.assertEqual(json.loads(approved)["fields"][0]["label"], "Email")
        self.assertRaises(ValueError, canon.canonical_send_text, "fax", None, "x", None, None)

    def test_inmail_subject_is_part_of_the_text(self):
        with_subject = canon.canonical_send_text("inmail", "Returns forecasting", "Hi Alex,\n\nA note.", None, None)
        self.assertEqual(with_subject, "Subject: Returns forecasting\n\nHi Alex,\n\nA note.")
        other = canon.canonical_send_text("inmail", "Another subject", "Hi Alex,\n\nA note.", None, None)
        self.assertNotEqual(canon.sha256_text(with_subject), canon.sha256_text(other))
        # without a subject an InMail stays body only, like the other LinkedIn kinds
        self.assertEqual(canon.canonical_send_text("inmail", None, "Hi Alex,\n\nA note.", None, None),
                         canon.canonical_send_text("li_message", None, "Hi Alex,\n\nA note.", None, None))

    def test_sha(self):
        want = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"   # sha256 of "abc"
        self.assertEqual(canon.sha256_text("abc"), want)


# ---------------------------------------------------------------- paths
class TestPaths(unittest.TestCase):
    def setUp(self):
        self.h = TempHome(init_db=False).start()

    def tearDown(self):
        self.h.stop()

    def test_repo_derivation(self):
        self.assertEqual(os.path.realpath(paths.REPO), os.path.realpath(REPO_ROOT))
        self.assertTrue(os.path.exists(os.path.join(paths.PKG_DIR, "paths.py")))
        self.assertTrue(paths.is_test_home())
        self.assertEqual(paths.root(), os.path.realpath(self.h.dir))
        self.assertEqual(paths.db_path(), os.path.join(paths.root(), "state", "jobhunter.sqlite3"))

    def test_home_binding(self):
        h = paths.check_home_binding()
        self.assertEqual(h["repo"], paths.root())
        bad = dict(h, repo="/somewhere/else")
        with self.assertRaises(Denied) as cm:
            paths.check_home_binding(bad)
        self.assertEqual(cm.exception.code, "E_HOME_MISMATCH")
        with self.assertRaises(Denied) as cm:
            paths.check_home_binding(dict(h, db_path="/tmp/other.sqlite3"))
        self.assertEqual(cm.exception.code, "E_HOME_MISMATCH")
        os.remove(paths.home_file())
        with self.assertRaises(Denied) as cm:
            paths.home()
        self.assertEqual(cm.exception.code, "E_CONFIG_INVALID")

    def test_ws_dirs(self):
        self.assertTrue(paths.ws_dir("applier").endswith(os.path.join("ws", "applier")))
        self.assertEqual(paths.ws_dir("jobhunter-applier"), paths.ws_dir("applier"))
        cid = canon.new_cycle_id()
        self.assertTrue(paths.work_dir("scout", cid).endswith(os.path.join("scout", "work", cid)))
        for bad in ("../x", "C1", ""):
            with self.assertRaises(Denied):
                paths.work_dir("scout", bad)

    def test_ensure_agent_path(self):
        ok = self.h.write_agent_file("applier", "c1/evidence.txt", "x")
        self.assertEqual(paths.ensure_agent_path(ok, "jobhunter-applier"), os.path.realpath(ok))
        inbox = self.h.write_agent_file("applier", "note.json", "{}", sub="inbox")
        self.assertEqual(paths.ensure_agent_path(inbox, "jobhunter-applier"), os.path.realpath(inbox))
        other = self.h.write_agent_file("outreach", "c1/evidence.txt", "x")
        outside = os.path.join(self.h.dir, "private", "home.json")
        link = os.path.join(paths.ws_dir("applier"), "work", "link.json")
        os.symlink(outside, link)
        for bad in (other, outside, link, "relative/path.json", os.path.join(paths.ws_dir("applier"), "work"),
                    os.path.join(paths.ws_dir("applier"), "work", "..", "AGENTS.md"), ""):
            with self.assertRaises(Denied) as cm:
                paths.ensure_agent_path(bad, "jobhunter-applier")
            self.assertEqual(cm.exception.code, "E_PATH_NOT_ALLOWED", bad)
        with self.assertRaises(Denied):
            paths.ensure_agent_path(ok, "main")

    def test_env_warnings(self):
        self.assertEqual(paths.env_warnings({}), [])
        self.assertEqual(len(paths.env_warnings({"JOBHUNTER_HOME": "/x"})), 1)


# ---------------------------------------------------------------- db
class TestSchemaFiles(unittest.TestCase):
    def test_schema_equals_migration(self):
        with open(paths.SCHEMA_FILE, "rb") as a, open(os.path.join(paths.MIGRATIONS_DIR, "0001_init.sql"), "rb") as b:
            sa, sb = a.read(), b.read()
        self.assertEqual(sa, sb)
        sa.decode("ascii")  # pure ASCII
        self.assertNotIn(b"IN LIVE", sa)

    def test_sqlite_features(self):
        # partial indexes (3.8), upsert (3.24), strftime with 'Z' input
        self.assertGreaterEqual(tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2]), (3, 24))
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        with open(paths.SCHEMA_FILE) as fh:
            c.executescript(fh.read())
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        for t in ("t_first_touch_rules", "t_li_seq_rules", "t_followup_binding", "t_no_merged_refs",
                  "t_company_email_cooldown", "t_company_app_caps", "t_agency_caps", "t_company_li_cap",
                  "t_company_blocked", "t_contact_dnc", "t_action_status_graph", "t_job_status_graph",
                  "t_draft_status_graph", "t_actions_touch", "t_target_skips_touch"):
            self.assertIn(t, names)
        self.assertEqual(len([n for n in names if n.endswith("_touch")]), 12)


class TestDb(HomeTestCase):
    def test_init_meta(self):
        meta = db.meta_all(self.conn)
        for k in db.REQUIRED_META:
            self.assertIn(k, meta)
        self.assertEqual(meta["schema_version"], str(db.latest_version()))
        self.assertEqual(meta["install_id"], paths.home()["install_id"])
        self.assertEqual(meta["approval_mode"], "human")
        self.assertEqual(meta["channel_linkedin_enabled"], "0")
        self.assertEqual(meta["profile_version"], "")
        self.assertEqual(db.missing_meta(self.conn, db.REQUIRED_FOR_QC), list(db.REQUIRED_FOR_QC))

    def test_init_idempotent(self):
        db.meta_set(self.conn, "company_email_cooldown_days", "120", "config_apply")
        seed = db.meta_get(self.conn, "jitter_seed")
        c2 = db.init_db()
        try:
            self.assertEqual(db.meta_get(c2, "company_email_cooldown_days"), "120")
            self.assertEqual(db.meta_get(c2, "jitter_seed"), seed)
            self.assertEqual(db.migrate(c2), [])
        finally:
            c2.close()

    def test_connect_and_pragmas(self):
        c = db.connect(write=True)
        try:
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
            self.assertEqual(c.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
            self.assertIsInstance(c.execute("SELECT 1 AS x").fetchone(), sqlite3.Row)
        finally:
            c.close()
        r = db.connect(write=False)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                r.execute("DELETE FROM meta")
        finally:
            r.close()

    def test_write_guard_missing_meta(self):
        for key in ("company_email_cooldown_days", "install_id", "max_seen_ts"):
            with self.subTest(key=key):
                clear_meta(self.conn, [key])
                with self.assertRaises(Denied) as cm:
                    db.connect(write=True)
                self.assertEqual(cm.exception.code, "E_CONFIG_INVALID")
                self.conn.execute("INSERT INTO meta VALUES (?, ?, ?, 'init')",
                                  (key, paths.home()["install_id"] if key == "install_id" else "1", canon.now()))
        db.connect(write=True).close()
        clear_meta(self.conn, ["agency_apps_per_30d"])
        db.connect(write=False).close()   # readers are not blocked by a missing limit row

    def test_install_id_mismatch(self):
        self.conn.execute("UPDATE meta SET value = 'IAAAAAAAA' WHERE key = 'install_id'")
        for write in (True, False):
            with self.assertRaises(Denied) as cm:
                db.connect(write=write)
            self.assertEqual(cm.exception.code, "E_HOME_MISMATCH")

    def test_home_repo_mismatch(self):
        h = paths.home()
        h["repo"] = os.path.join(os.path.dirname(h["repo"] or "/"), "other-clone")
        with open(paths.home_file(), "w") as fh:
            json.dump(h, fh)
        with self.assertRaises(Denied) as cm:
            db.connect()
        self.assertEqual(cm.exception.code, "E_HOME_MISMATCH")

    def test_missing_database(self):
        self.conn.close()
        self.home.conn = None
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(paths.db_path() + suffix):
                os.remove(paths.db_path() + suffix)
        with self.assertRaises(Denied) as cm:
            db.connect()
        self.assertEqual(cm.exception.code, "E_CONFIG_INVALID")
        self.assertFalse(os.path.exists(paths.db_path()))

    def test_newer_schema_refused(self):
        self.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        with self.assertRaises(Denied) as cm:
            db.connect(write=True)
        self.assertEqual(cm.exception.code, "E_CONFIG_INVALID")

    def test_tx_commit_rollback_and_mapping(self):
        with db.tx(self.conn):
            insert_company(self.conn, name="Kestrel Commerce")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM companies").fetchone()[0], 1)
        with self.assertRaises(RuntimeError):
            with db.tx(self.conn):
                insert_company(self.conn, name="Second Co")
                raise RuntimeError("boom")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM companies").fetchone()[0], 1)
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO companies (company_uid, display_name, created_at, updated_at) "
                                  "SELECT company_uid, 'dup', 'x', 'x' FROM companies")
        self.assertEqual(cm.exception.code, "E_INTERNAL")   # UNIQUE on company_uid is not a mapped index

    def test_tx_nested_and_helper_commit(self):
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                with db.tx(self.conn):
                    pass
        self.assertEqual(cm.exception.code, "E_INTERNAL")
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                insert_company(self.conn)
                self.conn.execute("COMMIT")   # a helper that commits breaks the contract
        self.assertEqual(cm.exception.code, "E_INTERNAL")

    def test_meta_set_validation(self):
        db.meta_set(self.conn, "raise:gmail.ceilings.conservative.cold_day", "18", "human")
        self.assertEqual(db.meta_get(self.conn, "raise:gmail.ceilings.conservative.cold_day"), "18")
        row = self.conn.execute("SELECT updated_by, updated_at FROM meta WHERE key = 'approval_mode'").fetchone()
        self.assertEqual(row[0], "init")
        for key, value, by in (("company_apps_per_day", "one", "config_apply"), ("company_apps_per_day", "-1", "init"),
                               ("approval_mode", "yolo", "human"), ("approval_mode", "auto", "agent"),
                               ("tier_gmail", 3, "human")):
            with self.assertRaises(Denied):
                db.meta_set(self.conn, key, value, by)
        self.assertEqual(db.meta_get(self.conn, "nope", "dflt"), "dflt")
        with self.assertRaises(Denied):
            db.meta_delete(self.conn, "install_id")
        db.meta_delete(self.conn, "raise:gmail.ceilings.conservative.cold_day")
        self.assertIsNone(db.meta_get(self.conn, "raise:gmail.ceilings.conservative.cold_day"))


# ---------------------------------------------------------------- events
class TestEvents(HomeTestCase):
    def _events(self):
        path = os.path.join(paths.logs_dir(), "events-%s.jsonl" % canon.now()[:7])
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def test_log_after_commit_only(self):
        with db.tx(self.conn):
            events.log_event(self.conn, "demo", n=1)
            self.assertEqual(self._events(), [])
        self.assertEqual([(e["kind"], e["n"]) for e in self._events()], [("demo", 1)])
        with self.assertRaises(RuntimeError):
            with db.tx(self.conn):
                events.log_event(self.conn, "demo", n=2)
                raise RuntimeError("rollback")
        self.assertEqual(len(self._events()), 1)
        events.log_event(self.conn, "outside", n=3)
        self.assertEqual(self._events()[-1]["kind"], "outside")
        self.assertEqual(self._events()[-1]["ts"], canon.now())

    def test_notifications_dedupe(self):
        with db.tx(self.conn):
            events.enqueue_notification(self.conn, "breaker:linkedin:2026-09-27", "high", "alert", "LinkedIn paused")
            events.enqueue_notification(self.conn, "breaker:linkedin:2026-09-27", "high", "alert", "again")
        rows = self.conn.execute("SELECT text FROM notifications").fetchall()
        self.assertEqual([r[0] for r in rows], ["LinkedIn paused"])
        with self.assertRaises(Denied):
            events.enqueue_notification(self.conn, "k", "urgent", "alert", "x")

    def test_human_task(self):
        with db.tx(self.conn):
            cid = insert_company(self.conn)
            a = events.open_human_task(self.conn, "confirm_agency", "Is this an agency?", company_id=cid)
            b = events.open_human_task(self.conn, "confirm_agency", "Is this an agency?", company_id=cid)
            c = events.open_human_task(self.conn, "confirm_agency", "Is this an agency?", company_id=cid + 1,
                                       detail="other")
        self.assertRegex(a, r"^H[A-Z2-7]{7}$")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        with self.assertRaises(Denied):
            events.open_human_task(self.conn, "not_a_kind", "q")
        with self.assertRaises(Denied):
            events.open_human_task(self.conn, "relogin", "q", user_id=3)


# ---------------------------------------------------------------- cli
def _fake_module(name, register):
    m = types.ModuleType(name)
    m.register = register
    return m


def _reg_basic(sub):
    p = add_command(sub, "demo ok", lambda a, c: {"echo": a.value, "cycle": c.cycle_id}, callers="SHA")
    p.add_argument("--value", default=None)
    add_command(sub, "demo deny", lambda a, c: (_ for _ in ()).throw(Denied("E_CEILING", "cap", retry_after=30)),
                callers="S")
    add_command(sub, "demo crash", lambda a, c: 1 / 0, callers="S")
    add_command(sub, "demo human", lambda a, c: {}, callers="H")
    add_command(sub, "demo pending", lambda a, c: Result(code="PENDING", data={"state": "queued"},
                                                         message="still running"), callers="S")
    p = add_command(sub, "demo file", lambda a, c: {"doc": c.read_json(a.file)}, callers="SA")
    p.add_argument("--file", required=True)
    p = add_command(sub, "cycle end", lambda a, c: {"cycle": a.cycle}, callers="SA")
    p.add_argument("--cycle", required=True)
    add_command(sub, "home show", lambda a, c: {"who": c.caller.cls, "agent": c.caller.agent_id}, callers="SHRA")


def _reg_second(sub):
    add_command(sub, "demo second", lambda a, c: {"from": "second"}, callers="S")
    add_command(sub, "qc review start", lambda a, c: {}, callers="A")


def _reg_dup(sub):
    add_command(sub, "demo ok", lambda a, c: {}, callers="S")


class TestCli(HomeTestCase):
    mods = [_fake_module("m1", _reg_basic), _fake_module("m2", _reg_second)]

    def run_cli(self, argv, env=None, mods=None):
        out = io.StringIO()
        rc = cli.main(argv, env=env or {}, stdin=io.StringIO(""), stdout=out, modules=mods or self.mods)
        text = out.getvalue()
        self.assertEqual(text.count("\n"), 1, text)
        return rc, text.strip()

    def env_json(self, argv, **kw):
        rc, text = self.run_cli(argv, **kw)
        return rc, json.loads(text)

    def test_envelope_ok(self):
        cid = canon.new_cycle_id()
        rc, env = self.env_json(["--cycle", cid, "demo", "ok", "--value", "x"])
        self.assertEqual(rc, 0)
        self.assertEqual(set(env), {"ok", "code", "data", "message", "next", "retry_after_s", "cycle_id"})
        self.assertEqual((env["ok"], env["code"], env["data"]["echo"], env["cycle_id"]), (True, "OK", "x", cid))
        self.assertEqual(env["data"]["cycle"], cid)

    def test_quiet_anywhere_and_human(self):
        self.assertEqual(self.run_cli(["demo", "ok", "--quiet"]), (0, "NO_REPLY"))
        self.assertEqual(self.run_cli(["--quiet", "demo", "ok"]), (0, "NO_REPLY"))
        rc, text = self.run_cli(["demo", "deny", "--quiet"])
        self.assertEqual((rc, json.loads(text)["code"]), (4, "E_CEILING"))
        rc = cli.main(["demo", "ok", "--human", "--value", "v"], env={}, stdout=io.StringIO(), modules=self.mods)
        self.assertEqual(rc, 0)

    def test_default_hints_name_the_final_word(self):
        """CLI route M6, V16: no hint tells an agent to reply NO_REPLY; the stop hints name the final word in a
        neutral form, because onboarding runs (ending with ONBOARD_DONE) see them too."""
        for code in errors.CODES:
            self.assertNotIn("NO_REPLY", cli._default_next(code), code)
        self.assertEqual(errors.FINAL_WORD_HINT, "reply with your final word (CYCLE_DONE in a cycle)")
        for code in ("E_INTERNAL", "E_STOP_DETECTED", "E_PAUSED", "E_LOCKED", "E_CLAIMED"):
            self.assertTrue(cli._default_next(code).endswith(errors.FINAL_WORD_HINT), code)
        rc, env = self.env_json(["demo", "crash"])
        self.assertEqual((rc, env["next"]), (1, "stop the cycle and " + errors.FINAL_WORD_HINT))
        # the --quiet success line is for system command jobs only and stays as it was
        self.assertEqual(self.run_cli(["demo", "ok", "--quiet"]), (0, "NO_REPLY"))

    def test_denied_and_codes(self):
        rc, env = self.env_json(["demo", "deny"])
        self.assertEqual((rc, env["ok"], env["code"], env["retry_after_s"]), (4, False, "E_CEILING", 30))
        self.assertTrue(env["next"])
        rc, env = self.env_json(["demo", "pending"])
        self.assertEqual((rc, env["ok"], env["code"]), (0, True, "PENDING"))
        rc, env = self.env_json(["demo", "crash"])
        self.assertEqual((rc, env["code"]), (1, "E_INTERNAL"))
        with open(os.path.join(paths.logs_dir(), "jh.log")) as fh:
            self.assertIn("E_INTERNAL", fh.read())

    def test_usage_errors(self):
        for argv in (["nosuch"], ["demo"], ["demo", "ok", "--bogus"], [], ["--home", "/x", "demo", "ok"],
                     ["demo", "file"], ["--grant"]):
            with self.subTest(argv=argv):
                rc, env = self.env_json(argv)
                self.assertEqual((rc, env["code"]), (2, "E_USAGE"))

    def test_command_cycle_flag_not_swallowed(self):
        cid = canon.new_cycle_id()
        rc, env = self.env_json(["cycle", "end", "--cycle", cid])
        self.assertEqual((rc, env["data"]["cycle"], env["cycle_id"]), (0, cid, cid))

    def test_groups_shared_and_duplicates(self):
        rc, env = self.env_json(["demo", "second"])
        self.assertEqual(env["data"], {"from": "second"})
        parser = cli.build_parser(self.mods + [_fake_module("m3", _reg_dup)])
        self.assertEqual([e["module"] for e in cli.discovery_errors()], ["m3"])
        cmds = cli.registered_commands(parser)
        self.assertIn("qc review start", cmds)
        self.assertIn("demo ok", cmds)

    def agent_json(self, argv, agent="jobhunter-scout", mods=None):
        """One call as a proven jobhunter agent: both identity carriers, as python -I (CLI route 5)."""
        from tests import helpers
        full = helpers.agent_argv(paths.root(), agent, argv)
        env = helpers.agent_env(paths.root(), agent)
        with helpers.as_isolated():
            return self.env_json(full, env=env, mods=mods)

    def test_caller_classes(self):
        rc, env = self.env_json(["demo", "human"])
        self.assertEqual((rc, env["code"]), (11, "E_HUMAN_ONLY"))
        rc, env = self.env_json(["--pin-stdin", "demo", "human"])
        self.assertEqual((rc, env["code"]), (11, "E_AUTH_FAILED"))
        rc, env = self.env_json(["demo", "ok"], env={"OPENCLAW_SHELL": "1"})
        self.assertEqual((rc, env["code"]), (11, "E_GUARD_MISSING"))
        rc, env = self.agent_json(["demo", "ok"])                    # not in the scout's acl map
        self.assertEqual((rc, env["code"]), (11, "E_CALLER_NOT_ALLOWED"))
        rc, env = self.agent_json(["demo", "second"])                # system-only command
        self.assertEqual((rc, env["code"]), (11, "E_CALLER_NOT_ALLOWED"))
        rc, env = self.agent_json(["home", "show"])
        self.assertEqual((rc, env["data"]), (0, {"who": "agent", "agent": "jobhunter-scout"}))
        # a jobhunter agent id without the guard's proofs is refused
        rc, env = self.env_json(["home", "show"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-scout"})
        self.assertEqual((rc, env["code"]), (11, "E_AUTH_FAILED"))
        # other agents (for example main) and guard-less shells: unproven, public read-only commands only (R7)
        rc, env = self.env_json(["home", "show"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"})
        self.assertEqual((rc, env["data"]), (0, {"who": "agent", "agent": None}))
        rc, env = self.env_json(["home", "show"], env={"OPENCLAW_SHELL": "1"})
        self.assertEqual(rc, 0)
        rc, env = self.env_json(["demo", "ok"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "main"})
        self.assertEqual((rc, env["code"]), (11, "E_GUARD_MISSING"))

    def test_agent_file_confinement(self):
        mods = [_fake_module("m4", lambda sub: add_command(sub, "job add", lambda a, c: {"doc": c.read_json(a.file)},
                                                             callers="SA").add_argument("--file", required=True))]
        good = self.home.write_agent_file("scout", "c1/job.json", json.dumps({"a": 1}))
        rc, env = self.agent_json(["job", "add", "--file", good], mods=mods)
        self.assertEqual((rc, env["data"]), (0, {"doc": {"a": 1}}))
        bad = os.path.join(self.home.dir, "private", "home.json")
        rc, env = self.agent_json(["job", "add", "--file", bad], mods=mods)
        self.assertEqual((rc, env["code"]), (10, "E_PATH_NOT_ALLOWED"))
        rc, env = self.env_json(["job", "add", "--file", bad], mods=mods)        # system caller: any path
        self.assertEqual(rc, 0)
        broken = self.home.write_agent_file("scout", "c1/bad.json", "{not json")
        rc, env = self.agent_json(["job", "add", "--file", broken], mods=mods)
        self.assertEqual((rc, env["code"]), (10, "E_SCHEMA"))

    def test_acl_args(self):
        mods = [_fake_module("m5", _reg_acl)]
        parser = cli.build_parser(mods)
        leaf = cli.registered_commands(parser)["thread list"]
        ns = parser.parse_args(["thread", "list", "--needs-check"])
        self.assertEqual(cli.acl_args(leaf, ns), {"--needs-check": True})
        leaf = cli.registered_commands(parser)["gate arm"]
        ns = parser.parse_args(["gate", "arm", "TAAAAAAAAAAA", "--observed-file", "/x"])
        self.assertEqual(cli.acl_args(leaf, ns), {"_1": "TAAAAAAAAAAA", "--observed-file": "/x"})

    def test_real_discovery_and_entry_point(self):
        cli.build_parser()
        self.assertEqual(cli.discovery_errors(), [])
        proc = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "scripts", "jh.py"), "no-such-command"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO_ROOT, timeout=60,
                              env={"PATH": os.environ.get("PATH", ""), "HOME": self.home.dir})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.decode())["code"], "E_USAGE")


def _reg_acl(sub):
    p = add_command(sub, "thread list", lambda a, c: {}, callers="SA")
    p.add_argument("--needs-check", action="store_true")
    p.add_argument("--state")
    p = add_command(sub, "gate arm", lambda a, c: {}, callers="A")
    p.add_argument("token")
    p.add_argument("--observed-file", required=True)


class TestU1Commands(HomeTestCase):
    """Smoke run of the U1 commands through the real CLI (system and human callers)."""
    start_ts = "2026-09-29T12:00:00Z"

    def run_cli(self, argv, env=None, stdin=""):
        out = io.StringIO()
        rc = cli.main(argv, env=env or {}, stdin=io.StringIO(stdin), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_commands(self):
        from jobhunter import auth
        rc, env = self.run_cli(["init"])
        self.assertEqual(rc, 0, env)
        self.assertTrue(os.path.exists(os.path.join(paths.private_dir(), "config.json")))
        self.assertTrue(os.path.exists(os.path.join(paths.private_dir(), "guard.key")))
        self.assertEqual(self.run_cli(["init"])[0], 0)
        for argv in (["home", "show"], ["config", "validate"], ["config", "show", "--effective"], ["config", "apply"],
                     ["config", "lower", "gmail.max_links", "1"], ["budget"], ["budget", "--platform", "linkedin"],
                     ["breaker", "status"], ["dispatch", "plan"], ["audit", "run"], ["exclusions", "list"],
                     ["exclusions", "add", "--type", "company", "--value", "Northwind Traders"],
                     ["exclusions", "import"], ["reconcile", "list"], ["gate", "status"], ["pause", "--scope", "gmail"],
                     ["housekeeping", "--only", "locks"], ["dedup", "check", "--kind", "company", "--company",
                                                            "KAAAAAAA"]):
            with self.subTest(argv=argv):
                rc, env = self.run_cli(argv)
                self.assertIn(env["code"], ("OK", "E_NOT_FOUND"), env)
        rc, env = self.run_cli(["selftest", "--offline"])
        names = {c["name"]: c["ok"] for c in env["data"]["checks"]}
        for n in ("db", "meta", "migrations", "trigger fixtures", "keys fixtures", "config clamp"):
            self.assertTrue(names[n], env["data"]["checks"])
        auth.set_pin(None, "482915")
        rc, env = self.run_cli(["--pin-stdin", "unpause", "--scope", "gmail"], stdin="482915\n")
        self.assertEqual(rc, 0, env)
        rc, env = self.run_cli(["--pin-stdin", "config", "raise", "gmail.max_links", "3"], stdin="482915\n")
        self.assertEqual((rc, env["data"]["new"]), (0, 3))
        rc, env = self.run_cli(["--pin-stdin", "tier", "set", "--platform", "linkedin", "--tier", "moderate"],
                               stdin="482915\n")
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        rc, env = self.run_cli(["--pin-stdin", "linkedin", "enable", "--ack", "I understand LinkedIn may restrict my "
                                "account", "--account-type", "free", "--account-age-years", "3"], stdin="482915\n")
        self.assertEqual((rc, env["data"]["enabled"]), (0, True))
        rc, env = self.run_cli(["--pin-stdin", "approval", "set", "auto", "--ack", "x"], stdin="482915\n")
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        rc, env = self.run_cli(["breaker", "reset", "--scope", "gmail", "--note", "x"])
        self.assertEqual((rc, env["code"]), (11, "E_HUMAN_ONLY"))
        rc, env = self.run_cli(["dispatch", "tick"])
        self.assertIn(rc, (0, 12))


class TestInitBinaries(HomeTestCase):
    """init writes the OpenClaw binary and Python that install.sh hands over (JH_OC_BIN, JH_PYTHON)."""

    def test_install_values_are_used(self):
        from jobhunter.commands import core
        with open(paths.home_file(), encoding="utf-8") as fh:
            saved = fh.read()
        fake_oc = os.path.join(self.home.dir, "fake-openclaw")
        with open(fake_oc, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake_oc, 0o700)
        try:
            os.unlink(paths.home_file())
            h, made = core._write_home({"JH_OC_BIN": fake_oc, "JH_PYTHON": sys.executable, "JH_OC_PROFILE": "jhtest"})
            self.assertTrue(made)
            self.assertEqual((h["oc_bin"], h["python"], h["oc_profile"]), (fake_oc, sys.executable, "jhtest"))
            os.unlink(paths.home_file())
            for bad in ({"JH_OC_BIN": "openclaw"}, {"JH_PYTHON": os.path.join(self.home.dir, "missing-python")}):
                with self.subTest(bad=bad):
                    self.assertDenied("E_VALIDATION", core._write_home, bad)
                    self.assertFalse(os.path.exists(paths.home_file()))
            h, made = core._write_home({})
            self.assertEqual((h["python"], h["oc_profile"]), (sys.executable, ""))
        finally:
            with open(paths.home_file(), "w", encoding="utf-8") as fh:
                fh.write(saved)


class TestSelftestCliRoute(HomeTestCase):
    """CLI route 8: agent identity (heartbeat proof version 2, probe files), claude settings key names, cli mode,
    the openclaw checks skipped offline, --only and --probe-since."""
    start_ts = "2026-09-29T12:00:00Z"

    def setUp(self):
        super().setUp()
        from tests import helpers
        from tests.fakes.u1 import write_config, write_heartbeat
        write_config()
        write_heartbeat()
        helpers.ensure_agent_setup()
        self.t0 = int(canon.utcnow().timestamp())

    def run_cli(self, argv):
        out = io.StringIO()
        rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=out)
        return rc, json.loads(out.getvalue())

    def probe_all(self):
        from tests import helpers
        for agent in ("jobhunter-scout", "jobhunter-evaluator", "jobhunter-applier", "jobhunter-outreach"):
            rc, env = helpers.agent_cli(agent, ["whoami"])
            self.assertEqual(rc, 0, env)
            self.home.write_agent_file(agent.split("-")[1], "probe/ok.txt", "OK\n")

    def test_agent_identity_with_probes(self):
        from jobhunter import selftest
        from tests.fakes.u1 import write_heartbeat
        check = selftest.check_agent_identity()
        self.assertTrue(check["ok"], check)
        write_heartbeat(proof_version=None)
        self.assertIn("old guard", selftest.check_agent_identity()["detail"]["problems"][0])
        write_heartbeat(carriers=["env"])
        self.assertFalse(selftest.check_agent_identity()["ok"])
        write_heartbeat()
        rc, env = self.run_cli(["selftest", "--offline", "--probe-since", str(self.t0)])
        self.assertEqual((rc, env["code"]), (1, "E_INTERNAL"))            # no probe files yet
        self.assertIn("agent identity", env["message"])
        self.probe_all()
        rc, env = self.run_cli(["selftest", "--offline", "--probe-since", str(self.t0), "--only", "agent identity"])
        self.assertEqual(rc, 0, env)
        self.assertEqual([c["name"] for c in env["data"]["checks"]], ["agent identity"])
        # a probe from before the test, a missing ok.txt, or a native tool call in the guard log fails it
        rc, env = self.run_cli(["selftest", "--offline", "--probe-since", str(self.t0 + 60), "--only",
                                "agent identity"])
        self.assertEqual(rc, 1)
        os.unlink(os.path.join(paths.ws_dir("scout"), "work", "probe", "ok.txt"))
        check = selftest.check_agent_identity(self.t0)
        self.assertEqual(check["detail"]["agents"]["jobhunter-scout"], "work/probe/ok.txt was not written")
        self.home.write_agent_file("scout", "probe/ok.txt", "OK")
        os.makedirs(paths.logs_dir(), exist_ok=True)
        with open(os.path.join(paths.logs_dir(), "guard-2026-09.jsonl"), "a") as fh:
            fh.write(json.dumps({"ts": "2026-09-29T11:00:00Z", "kind": "native_tool"}) + "\n")
        self.assertTrue(selftest.check_agent_identity(self.t0)["ok"])          # older than the test
        with open(os.path.join(paths.logs_dir(), "guard-2026-09.jsonl"), "a") as fh:
            fh.write(json.dumps({"ts": "2026-09-29T12:00:01Z", "kind": "native_tool"}) + "\n")
        self.assertIn("not restricted", " ".join(selftest.check_agent_identity(self.t0)["detail"]["problems"]))

    def test_heartbeat_fields(self):
        from jobhunter import gate
        from tests.fakes.u1 import write_heartbeat
        hb = gate.guard_heartbeat()
        self.assertEqual((hb["fresh"], hb["proof_version"], hb["carriers"], hb["native_tools"], hb["pin_tool_surface"],
                          hb["guard_version"]), (True, 2, ["argv", "env"], "deny", True, "2.1.0"))
        write_heartbeat(proof_version=None)
        hb = gate.guard_heartbeat()
        self.assertEqual((hb["fresh"], hb["proof_version"], hb["carriers"]), (True, None, None))
        os.unlink(os.path.join(paths.guard_dir(), "heartbeat.json"))
        self.assertEqual((gate.guard_heartbeat()["present"], gate.guard_heartbeat()["proof_version"]), (False, None))

    def test_settings_mode_and_offline(self):
        from jobhunter import selftest
        path = os.path.join(self.home.dir, "settings.json")
        with open(path, "w") as fh:
            json.dump({"permissions": {"allow": ["Bash(*)"], "defaultMode": "acceptEdits"}, "hooks": {"x": 1},
                       "theme": "dark", "mcpServers": {}}, fh)
        check = selftest.check_claude_settings(path)
        self.assertEqual((check["ok"], check["warnings"]), (True, ["permissions.allow", "permissions.defaultMode",
                                                                   "hooks"]))
        self.assertNotIn("Bash(*)", json.dumps(check))
        self.assertTrue(selftest.check_claude_settings(path + ".missing")["ok"])
        self.assertTrue(selftest.check_cli_mode()["ok"])
        h = paths.home()
        h["cli_route"] = dict(h["cli_route"], cli_tools="native")
        with open(paths.home_file(), "w") as fh:
            json.dump(h, fh)
        self.assertEqual((selftest.check_cli_mode()["ok"], selftest.check_cli_mode()["red"]), (False, True))
        names = {c["name"]: c for c in selftest.route_checks(offline=True)}
        for n in ("exec policy", "identity boundary", "other plugins"):
            self.assertTrue(names[n]["skipped"])
        # online, against an openclaw that is not there: exec policy fails red, the boundary never fails
        names = {c["name"]: c for c in selftest.route_checks(offline=False)}
        self.assertEqual((names["exec policy"]["ok"], names["exec policy"]["red"]), (False, True))
        self.assertTrue(names["identity boundary"]["ok"])
        rc, env = self.run_cli(["selftest", "--offline", "--only", "no such check"])
        self.assertEqual((rc, env["code"]), (2, "E_USAGE"))


class TestSelftestMailCheck(HomeTestCase):
    """selftest runs the mail connection test only on the app password route once mail is connected."""

    def run_checks(self):
        from unittest import mock
        from jobhunter import selftest
        calls = []
        with mock.patch.object(selftest, "_mail", lambda: (calls.append("mail") or True, "fake")), \
                mock.patch.object(selftest, "_sheet", lambda: (True, "fake")):
            checks = {c["name"]: c for c in selftest.run_checks(offline=False)}
        return checks["mail connection"], calls

    def test_skipped_on_web_ui_and_before_connect(self):
        from tests.fakes.u1 import write_config
        write_config({"gmail.route": "web_ui"})
        check, calls = self.run_checks()
        self.assertEqual((check["ok"], check.get("skipped"), calls), (True, True, []))
        self.assertIn("web_ui", check["detail"])
        write_config({"gmail.route": "app_password"})
        check, calls = self.run_checks()
        self.assertEqual((check.get("skipped"), calls), (True, []))
        with db.tx(self.conn):
            db.meta_set(self.conn, "mail_connected_at", canon.now(), "system")
        check, calls = self.run_checks()
        self.assertEqual((check["ok"], check.get("skipped"), calls), (True, None, ["mail"]))


class TestFinderAndRouteIntegration(HomeTestCase):
    """The email finder's lines in selftest and `budget`, and the shipped email route (change requests)."""

    def run_cli(self, argv, env=None, stdin=""):
        out = io.StringIO()
        rc = cli.main(argv, env=env or {}, stdin=io.StringIO(stdin), stdout=out)
        return rc, json.loads(out.getvalue())

    def test_selftest_has_the_finder_lines(self):
        from unittest import mock
        from jobhunter import enrich, selftest
        names = [c["name"] for c in selftest.run_checks(offline=True)]
        for n in ("enrich migration objects", "enrich trigger fixtures", "enrich keystore", "enrich dependencies"):
            self.assertIn(n, names)
        with mock.patch.object(enrich, "selftest_checks", side_effect=RuntimeError("boom")):
            bad = [c for c in selftest.run_checks(offline=True) if c["name"] == "email finder"]
        self.assertEqual((bad[0]["ok"], "boom" in bad[0]["detail"]), (False, True))
        import builtins
        real = builtins.__import__

        def no_finder(name, globals=None, locals=None, fromlist=(), level=0):
            if (level and fromlist and "enrich" in fromlist) or name.startswith("jobhunter.enrich"):
                raise ImportError("no finder")
            return real(name, globals, locals, fromlist, level)
        with mock.patch.object(builtins, "__import__", no_finder):
            skipped = [c for c in selftest.run_checks(offline=True) if c["name"] == "email finder"]
        self.assertEqual((skipped[0]["ok"], skipped[0]["skipped"]), (True, True))
        self.assertIn("warning", skipped[0]["detail"])

    def test_budget_shows_the_finder_summary(self):
        from unittest import mock
        from jobhunter.enrich import budget as enrich_budget
        from tests.fakes.u1 import write_config
        write_config()
        with mock.patch.object(enrich_budget, "summary", lambda conn: {"enabled": False, "providers": []}):
            rc, env = self.run_cli(["budget"])
        self.assertEqual((rc, env["data"]["enrich"]), (0, {"enabled": False, "providers": []}))
        with mock.patch.object(enrich_budget, "summary", side_effect=RuntimeError("bad row")):
            rc, env = self.run_cli(["budget"])
        self.assertEqual(rc, 0)
        self.assertIn("bad row", env["data"]["enrich"]["error"])
        rc, env = self.run_cli(["budget", "--platform", "linkedin"])
        self.assertNotIn("enrich", env["data"])

    def test_enrich_config_one_source_validate_and_apply(self):
        from jobhunter import config, hardmax
        from jobhunter.enrich import settings as es
        # hardmax takes the finder's numbers from jobhunter.enrich.settings (one source)
        self.assertEqual(hardmax.HARD_MAX["enrich.max_lookups_per_day"], es.HARD_MAX["max_lookups_per_day"])
        self.assertEqual(hardmax.HARD_MAX["enrich.providers.hunter.budget_31d"],
                         es.PROVIDER_HARD_MAX["hunter"]["budget_31d"])
        self.assertEqual(hardmax.key_class("enrich.min_confidence"), "F")
        self.assertEqual(hardmax.key_class("enrich.timeout_s"), "bounded")
        self.assertEqual(hardmax.ENRICH_ITEM_SETS["enrich.chain"], es.CHAIN_ITEMS)

        def write(block):
            with open(config.config_file(), "w", encoding="utf-8") as fh:
                json.dump({"enrich": block}, fh)
        write({"chain": ["hunter", "hunter", "scraperx"], "verifiers": "zerobounce", "key_store": "cloud"})
        errs = " | ".join(config.validate(self.conn)["errors"])
        for part in ("scraperx", "more than once", "enrich.verifiers must be a list", "enrich.key_store"):
            self.assertIn(part, errs)
        # apply: the budget rows are 0 while the finder is off, the clamped values once it is on
        write({"enabled": False})
        with db.tx(self.conn):
            config.apply(self.conn)
        self.assertEqual(db.meta_get(self.conn, "enrich_budget_31d:hunter"), "0")
        write({"enabled": True, "max_lookups_per_day": 500,
               "providers": {"hunter": {"enabled": True, "budget_31d": 9999}, "tomba": {"budget_31d": 10}}})
        with db.tx(self.conn):
            config.apply(self.conn)
        # a limit key is only lowered by the file (raising needs `config raise`, never past the hard maximum)
        self.assertEqual(db.meta_get(self.conn, "enrich_budget_31d:hunter"),
                         str(es.DEFAULTS["providers"]["hunter"]["budget_31d"]))
        self.assertEqual(db.meta_get(self.conn, "enrich_budget_31d:tomba"), "10")
        self.assertLessEqual(config.load(self.conn)["enrich"]["max_lookups_per_day"],
                             es.HARD_MAX["max_lookups_per_day"])
        self.assertEqual(db.meta_get(self.conn, "enrich_budget_31d:anymailfinder"), "0")    # reserve stays off

    def test_shipped_email_route_is_the_browser(self):
        from jobhunter import config, hardmax
        with open(os.path.join(paths.REPO_DIR if hasattr(paths, "REPO_DIR") else
                               os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))),
                               "config.example.json"), encoding="utf-8") as fh:
            example = json.load(fh)
        self.assertEqual(example["gmail"]["route"], "web_ui")
        self.assertEqual(hardmax.defaults()["gmail"]["route"], "web_ui")
        self.assertEqual(config.load(self.conn)["gmail"]["route"], "web_ui")      # no private/config.json
        with open(config.config_file(), "w", encoding="utf-8") as fh:
            json.dump({"gmail": {"route": "carrier_pigeon"}}, fh)
        self.assertTrue(any("gmail.route" in e for e in config.validate(self.conn)["errors"]))
        with open(config.config_file(), "w", encoding="utf-8") as fh:
            json.dump({"gmail": {"route": "app_password"}}, fh)
        self.assertEqual(config.validate(self.conn)["errors"], [])


class TestTextRules(unittest.TestCase):
    def test_owned_files_ascii(self):
        owned = ["scripts/jh.py", "scripts/jobhunter/acl.json", "scripts/jobhunter/schema.sql",
                 "scripts/jobhunter/migrations/0001_init.sql", "tests/__init__.py", "tests/helpers.py",
                 "config.example.json", "private.example/exclusions.example.csv",
                 "private.example/company_aliases.example.csv", "docs/CONFIG-REFERENCE.md",
                 "tests/fakes/u1/__init__.py"]
        for name in ("__init__", "cli", "paths", "errors", "canon", "events", "db", "jobstate", "hooks", "auth", "config",
                     "hardmax", "keys", "companies", "people", "exclusions", "locks", "cycles", "gate", "reconcile",
                     "ceilings", "pacing", "breakers", "identity", "dispatch", "audit", "housekeeping", "selftest",
                     "ocrun"):
            owned.append("scripts/jobhunter/%s.py" % name)
        for name in ("__init__", "core", "gate", "limits", "maint"):
            owned.append("scripts/jobhunter/commands/%s.py" % name)
        for d in ("scripts/jobhunter/detect", "scripts/jobhunter/data", "tests/fixtures/core"):
            for f in sorted(os.listdir(os.path.join(REPO_ROOT, d))):
                if not f.startswith(".") and not f.endswith(".pyc") and f != "__pycache__":
                    owned.append("%s/%s" % (d, f))
        for name in ("test_db_meta", "test_triggers", "test_jobstate", "test_acl", "test_keys_jobs", "test_keys_company",
                     "test_keys_person", "test_companies_resolve", "test_people_resolve", "test_gate", "test_reconcile",
                     "test_ceilings", "test_pacing", "test_breakers_detect", "test_exclusions", "test_auth",
                     "test_dispatch", "test_audit", "test_housekeeping"):
            owned.append("tests/%s.py" % name)
        for rel in owned:
            with open(os.path.join(REPO_ROOT, rel), "rb") as fh:
                data = fh.read()
            with self.subTest(rel=rel):
                data.decode("ascii")
                self.assertIsNone(re.search(rb"\xe2\x80[\x93\x94]", data))


class TestConfigReferenceDoc(unittest.TestCase):
    """docs/CONFIG-REFERENCE.md names the sending rules no key changes, with the codes the code uses."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "docs", "CONFIG-REFERENCE.md"), encoding="ascii") as fh:
            cls.text = " ".join(fh.read().split())

    def test_token_lanes(self):
        from jobhunter import dispatch, gate
        self.assertEqual(set(gate.TOKEN_LANES), {"outreach", "applier"})
        self.assertIn("Only the outreach and applier lanes hold a token.", self.text)
        self.assertIn("replies", dispatch.LANES)
        self.assertIn("The replies lane reads untrusted mail and messages, so it never reserves and never sends",
                      self.text)
        self.assertIn("E_CALLER_NOT_ALLOWED`, exit %d" % errors.CODES["E_CALLER_NOT_ALLOWED"], self.text)

    def test_stale_tokens_of_a_dead_cycle(self):
        import inspect
        from jobhunter import cycles
        src = inspect.getsource(cycles.close_stale)
        self.assertIn('"stale_cycle: ', src)
        self.assertIn("marks it `unknown` (note `stale_cycle: ...`)", self.text)
        self.assertIn("under `stale` (`tokens`, `cycles`)", self.text)

    def test_web_email_recipient_codes(self):
        from jobhunter import gate
        act = {"recipient": "alex@kestrel.example"}
        cases = [{}, {"to": "alex@kestrel.example", "cc": "b@kestrel.example"},
                 {"to": "alex@kestrel.example, b@kestrel.example"}, {"to": "b@kestrel.example"}]
        codes = {gate.recipient_problem(act, rb) for rb in cases}
        self.assertEqual(codes, {"to_missing", "cc_or_bcc", "to_not_one_address", "to_other_address"})
        self.assertIsNone(gate.recipient_problem(act, {"to": "Alex <ALEX@kestrel.example>"}))
        for code in sorted(codes):
            self.assertIn("`%s`" % code, self.text)
        self.assertIn("E_OBSERVED_MISMATCH`, exit %d" % errors.CODES["E_OBSERVED_MISMATCH"], self.text)

    def test_logged_out_codes(self):
        from jobhunter import breakers
        codes = sorted(c for c in breakers.POLICIES if c.endswith("_logged_out"))
        self.assertEqual(codes, ["gmail_logged_out", "li_logged_out", "site_logged_out"])
        for code in codes:
            self.assertIn("`%s`" % code, self.text)
        for code in ("gmail_logged_out", "site_logged_out"):
            pol = breakers.POLICIES[code]
            self.assertEqual((pol["cooldown"], pol["resume"], pol.get("clamp")), (0, None, None))
        self.assertIn("have no waiting time and no resume clamp", self.text)
        self.assertEqual(breakers.POLICIES["li_logged_out"]["clamp"], ("linkedin", 0.5, 1))


if __name__ == "__main__":
    unittest.main()
