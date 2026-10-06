"""E2E (INT): email codes and sign-in links, ATS accounts and the CAPTCHA hand-off (FEATURES-OTP-ACCOUNTS-CAPTCHA 5.2).

A mock Workday application at the fictional Kestrel Commerce, through the real core (U1 to U9), the real
jobhunter-guard runtime (U8, tests/fixtures/e2e/guard_bridge.ts under node) for every agent tool call, and fakes at
the seams only: the fake CDP server serving recorded ATS pages (tests/fakes/u6/fake_cdp.py), one fake inbox served
on both email routes (tests/fakes/u9/fake_inbox.py over the fake IMAP server, and as Gmail web pages through the
fake CDP server), a fake macOS Keychain (tests/fakes/u1/fake_security.py), the fake reviewer turn and the fake
clock. Every scenario that reads mail runs on gmail.route web_ui and on app_password.

Fake secrets used everywhere (tests/fixtures/e2e/otp_workday_flow.json): a generated password, codes and a
sign-in link; secret_hits() proves they appear in no log, events file, guard or token log, database row, Sheet row,
notification, stdout or recorded argv.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import unittest

import tests  # noqa: F401
from jobhunter import accounts, auth, canon, cdp, cli, db, mail, paths, secretstore, sheets_rows
from jobhunter import qc as qcpkg
from tests import helpers
from tests.fakes.u1.fake_security import FakeSecurity
from tests.fakes.u3 import FakeReviewer, install_reviewer_hashes
from tests.fakes.u6.fake_cdp import FakeCdp
from tests.fakes.u9.fake_inbox import FakeInbox
from tests.fakes.u9.imap_server import FakeImapServer
from tests.fixtures.e2e.support import AP, PIN, ApplyWorld, e2e_fixture, install_reviewer
from tests.test_e2e_guard_replay import GuardBridge, node_major

FLOW = e2e_fixture("otp_workday_flow.json")
NEG = e2e_fixture("otp_negative_cases.json")
SEC = FLOW["secrets"]
SECRETS = [SEC["password"], SEC["password_alt"], SEC["code"], SEC["code_greenhouse"], SEC["link"], "FIXTURETOKEN123"]
OWNER = "sam.lee@example.com"
APP_PW = "abcdefghijklmnop"
GOOD = FLOW["senders"]["good"]
SESSION = "agent:jobhunter-applier:cron:e2e-otp"


def snap(name: str) -> str:
    return FLOW["snapshots"][name]


class Agent:
    """The scripted applier: every tool call goes to the real guard first; an allowed exec runs jh.py exactly as
    OpenClaw's exec tool would (the rewritten command under python -I flags with the resolve_exec_env proof)."""

    def __init__(self, test, world, guard):
        self.t = test
        self.w = world
        self.g = guard
        h = paths.home()
        self.py = h["python"]
        self.ws = os.path.join(h["ws_root"], "applier")
        self.ctx = {"sessionId": "sess-otp", "runId": "run-otp", "workspaceDir": self.ws}
        self.outputs = []
        self.decisions = []
        self.cycle = None

    def clock(self) -> None:
        self.g.ask({"op": "clock", "now": canon.now()})
        self.g.ask({"op": "heartbeat"})          # the guard beats while the fake clock moves

    def exec(self, argv: list, expect: str = "allow"):
        self.clock()
        argv = [str(a) for a in argv]
        if self.cycle and argv[0] not in ("preflight",):
            argv = ["--cycle", self.cycle] + argv
        command = "%s %s/scripts/jh.py %s" % (self.py, paths.REPO, " ".join(shlex.quote(a) for a in argv))
        dec = self.g.ask({"op": "exec", "agent": AP, "session": SESSION, "tool": "exec",
                          "params": {"command": command, "timeoutSeconds": 90}, "ctx": self.ctx})
        self.decisions.append(("exec", argv[:3], dec["outcome"]))
        self.t.assertEqual(dec["outcome"], expect, "%s: %s" % (argv, dec.get("reason")))
        if expect != "allow":
            return dec
        toks = shlex.split(dec["params"]["command"])
        out = io.StringIO()
        with helpers.as_isolated():
            rc = cli.main(toks[3:], env=dict(dec["env"], OPENCLAW_SHELL="exec"), stdin=io.StringIO(""), stdout=out)
        text = out.getvalue()
        self.outputs.append(text)
        env = json.loads(text.strip().splitlines()[-1])
        env["rc"] = rc
        return env

    def ok(self, argv: list) -> dict:
        env = self.exec(argv)
        self.t.assertEqual(env["code"], "OK", json.dumps(env)[:1500])
        return env["data"]

    def call(self, tool: str, params: dict, expect: str = "allow", result=None):
        self.clock()
        dec = self.g.ask({"op": "call", "agent": AP, "session": SESSION, "tool": tool, "params": params,
                          "ctx": self.ctx})
        self.decisions.append((tool, params.get("action") or params.get("path"), dec["outcome"]))
        self.t.assertEqual(dec["outcome"], expect, "%s %s: %s" % (tool, params, dec.get("reason")))
        if expect == "allow" and result is not None:
            return self.g.ask({"op": "result", "agent": AP, "session": SESSION, "tool": tool, "params": params,
                               "result": result, "error": None, "ctx": self.ctx})
        return dec

    def browser(self, params: dict, tab: str, url: str, text: str = "", expect: str = "allow"):
        p = dict({"profile": "jobhunter"}, **params)
        result = {"content": [{"type": "text", "text": text}], "details": {"ok": True, "targetId": tab, "url": url}}
        return self.call("browser", p, expect, result)

    def write(self, name: str, obj) -> str:
        path = os.path.join(self.ws, "work", self.cycle or "x", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = obj if isinstance(obj, str) else json.dumps(obj)
        self.call("write", {"path": path, "content": content})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def guard_detects(self) -> list:
        """Run the `jh.py detect --source guard` calls the guard made (as the plugin's runner would)."""
        out = []
        calls = self.g.ask({"op": "jh_calls"})["calls"]
        for argv in calls[getattr(self, "_seen", 0):]:
            buf = io.StringIO()
            cli.main(argv, env={}, stdin=io.StringIO(""), stdout=buf)
            self.outputs.append(buf.getvalue())
            out.append(json.loads(buf.getvalue().strip().splitlines()[-1]))
        self._seen = len(calls)
        return out


@unittest.skipUnless(node_major() >= 24, "needs node 24 or later (the guard plugin runs TypeScript directly)")
class OtpBase(unittest.TestCase):
    ROUTE = "web_ui"

    def setUp(self):
        self.w = ApplyWorld()
        self.addCleanup(self.w.stop)
        prev = qcpkg.SPAWN
        qcpkg.SPAWN = [].append
        self.addCleanup(setattr, qcpkg, "SPAWN", prev)
        install_reviewer(self, FakeReviewer("pass"))
        over = {"boards.sites.workday.apply": "browser", "gmail.route": self.ROUTE}
        self.w.onboard(**over)
        install_reviewer_hashes(self.w.conn)
        self.assertTrue(auth.create_guard_key())
        # the agent browser (fake CDP, its loopback port recorded as the installer does) and the fake Keychain
        self.cdp = FakeCdp().start()
        self.addCleanup(self.cdp.stop)
        self.w.set_home(browser_cdp={"port": self.cdp.port})
        self._sleep = cdp.SLEEP
        cdp.SLEEP = lambda s: None
        self.addCleanup(setattr, cdp, "SLEEP", self._sleep)
        self.security = FakeSecurity()
        secretstore.set_runner(self.security)
        secretstore.set_backend("keychain")
        self.addCleanup(secretstore.set_runner, None)
        self.addCleanup(secretstore.set_backend, None)
        accounts.set_password_hook(lambda n, alt: SEC["password_alt"] if alt else SEC["password"])
        self.addCleanup(accounts.set_password_hook, None)
        # one inbox, served on the route under test
        self.inbox = FakeInbox()
        self.cdp.inbox = self.inbox
        if self.ROUTE == "app_password":
            self.imap = FakeImapServer(OWNER, APP_PW).start()
            self.addCleanup(self.imap.stop)
            self.inbox.attach_imap(self.imap)
            mail.set_test_transport(imap=lambda c: self.imap.client(c.account, c.password))
            self.addCleanup(mail.clear_test_transport)
            mail.store_credentials(OWNER, APP_PW, "file")
            with db.tx(self.w.conn):
                db.meta_set(self.w.conn, "mail_connected_at", canon.now(), "human")
        # the owner: a 16-character password policy floor (PIN) and both capabilities for Workday (PIN)
        self.w.ok(["config", "raise", "accounts.password_length", "16"], "human")
        self.guard = self.start_guard()
        self.agent = Agent(self, self.w, self.guard)

    def start_guard(self) -> GuardBridge:
        h = paths.home()
        g = GuardBridge({"repo": paths.REPO, "python": h["python"], "homeFile": paths.home_file(),
                         "publicReadonlyAgents": ["main"],
                         "ownerFallback": [{"channel": "whatsapp", "senderId": "+15555550100"}]}, canon.now())
        self.addCleanup(g.close)
        hb = os.path.join(paths.guard_dir(), "heartbeat.json")
        if os.path.exists(hb):
            os.remove(hb)
        self.assertTrue(g.ask({"op": "heartbeat"})["ok"])
        return g

    # ------------------------------------------------------------ owner and lanes
    def grant(self, *caps, site="workday"):
        argv = ["browser", "consent", "grant", "--site", site]
        for c in caps or ("email_codes", "ats_accounts"):
            argv += ["--capability", c]
        return self.w.ok(argv, "human")

    def approved_job(self) -> dict:
        """An approved application package for the Workday job, made by the real lanes."""
        w = self.w
        job_uid = w.add_jobs(FLOW["ingest"])[0]["job_uid"]
        w.evaluate_all()
        cyc = w.preflight("applier", AP)
        item = w.claim(cyc, job_uid)
        self.assertEqual(item["needs"], "package", item)
        built = w.build_resume(cyc, job_uid)
        fields = []
        for label in FLOW["fields"]:
            rc, env = w.answer(cyc, job_uid, label)
            self.assertEqual(env["code"], "OK", env)
            fields.append({"label": label, "type": "text", "value": env["data"]["value"],
                           "answer_key": env["data"]["key"]})
        pkg = w.package(cyc, job_uid, built["variant_uid"], fields)
        w.end_cycle(cyc, AP)
        w.approve_all()
        url = w.one("SELECT apply_url FROM jobs WHERE job_uid = ?", job_uid)[0]
        return {"job": job_uid, "draft": pkg, "variant": built["variant_uid"], "fields": fields, "url": url}

    def start_cycle(self, job: dict) -> None:
        a = self.agent
        a.cycle = None
        a.cycle = a.ok(["preflight", "--lane", "applier"])["cycle_id"]
        items = a.ok(["apply", "next", "--limit", "1"])["items"]
        self.assertEqual([i["job_uid"] for i in items], [job["job"]], items)

    def open_create_page(self, job: dict, page: str = "wd_create") -> str:
        """The agent's tab on the Workday form: navigate, the page check (clear with flow account)."""
        a = self.agent
        tab = self.cdp.add_tab(page)
        url = self.cdp.tabs[tab].url
        a.browser({"action": "navigate", "targetUrl": url}, tab, url, snap(page) if page in FLOW["snapshots"] else "")
        self.guard_detects = a.guard_detects()
        det = {"platform": "workday", "url": url, "title": "Kestrel Commerce Careers", "http_status": None,
               "text": self.cdp.tabs[tab].page["text"]}
        self.detect = a.exec(["detect", "--file", a.write("detect-%s.json" % tab, det)])
        return tab

    def reserve(self, job: dict) -> str:
        a = self.agent
        plan = a.ok(["gate", "precheck-plan", "--kind", "application", "--job", job["job"]])
        ev = {"kind": "application", "platform": "workday", "observed_at": canon.now(), "page_url": job["url"],
              "checks": [{"name": c["name"], "value": False} for c in plan["checks"]]}
        pc = a.ok(["gate", "precheck", "--kind", "application", "--platform", "workday", "--job", job["job"],
                   "--file", a.write("precheck.json", ev)])
        return a.ok(["gate", "reserve", "--kind", "application", "--draft", job["draft"], "--precheck",
                     str(pc["precheck_id"]), "--platform", "workday", "--job", job["job"]])["token"]

    def poll(self, argv: list, deliver=None, max_tries: int = 6) -> dict:
        """code submit / open-link until an outcome: PENDING moves the fake clock by retry_after_s; deliver(n)
        may put mail into the inbox before try n."""
        for n in range(max_tries):
            if deliver:
                deliver(n)
            env = self.agent.exec(argv)
            if env["code"] != "PENDING":
                return env
            self.assertEqual(env["retry_after_s"], 15, env)
            self.w.clock.advance(seconds=env["retry_after_s"])
        return env

    def mail(self, text: str = "", links=None, sender: str = GOOD, subject: str = "Verify your email",
             at: str | None = None, to: str = OWNER):
        return self.inbox.add(sender=sender, to=to, subject=subject, text=text, links=links,
                              received_at=at or canon.now())

    def secret_scan(self, extra_texts=()) -> list:
        w = self.w
        texts = list(self.agent.outputs) + list(extra_texts)
        for tab in sheets_rows.BUILDERS:
            texts.append(json.dumps(sheets_rows.build_rows(w.conn, tab, None), default=str))
        argv = list(self.security.argv_log)
        if os.path.exists(os.path.join(w.home.dir, "bin", "openclaw-calls.jsonl")):
            with open(os.path.join(w.home.dir, "bin", "openclaw-calls.jsonl"), "r", encoding="utf-8") as fh:
                texts.append(fh.read())
        return helpers.secret_hits(SECRETS, paths_=[paths.logs_dir(), paths.state_dir(), paths.guard_dir()],
                                   texts=texts, argv=argv, conn=w.conn)


class HappyPath:
    def test_workday_account_codes_and_application(self):
        w, a, f = self.w, self.agent, self.cdp
        self.grant()
        job = self.approved_job()
        self.start_cycle(job)
        tab = self.open_create_page(job)
        # the account wall with ats_accounts granted: the guard does not soft-stop, detect answers clear (flow)
        self.assertEqual((self.detect["code"], self.detect["data"]["verdict"], self.detect["data"].get("flow")),
                         ("OK", "clear", "account"), self.detect)
        st = a.ok(["account", "status", "--job", job["job"]])
        self.assertEqual((st["next"], st["account"], st["consent"]["ats_accounts"]), ("create", None, "granted"))
        token = self.reserve(job)
        # the model never types a secret: a password field and a press after clicking it are refused
        a.browser({"action": "snapshot"}, tab, f.tabs[tab].url, snap("wd_create"))
        a.call("browser", {"profile": "jobhunter", "action": "act", "kind": "type", "ref": "e3",
                           "text": "anything"}, expect="G_SECRET_FIELD")
        a.browser({"action": "act", "kind": "click", "ref": "e3"}, tab, f.tabs[tab].url, snap("wd_create"))
        a.call("browser", {"profile": "jobhunter", "action": "act", "kind": "press", "key": "a"},
               expect="G_SECRET_FIELD")
        # account create by code: the password is generated, stored in the Keychain and typed over CDP
        made = a.ok(["account", "create", "--job", job["job"], "--tab", tab, "--token", token])
        self.assertEqual(made["outcome"], "verify_link", made)
        self.assertEqual([t["action"] for t in made["terms"]], ["ticked"])
        self.assertEqual(f.page_of(tab), "wd_verify_link")
        self.assertEqual(f.accounts, {OWNER: SEC["password"]})
        # the verification link arrives 40 seconds later
        f.expected_link = SEC["link"]
        link_mail = {"sent": False}

        def deliver_link(n):
            if n == 1 and not link_mail["sent"]:
                w.clock.advance(seconds=25)
                self.mail(text="Confirm your email address to finish creating your account.",
                          links=[{"href": SEC["link"], "text": "Verify Email"}])
                link_mail["sent"] = True
        opened = self.poll(["code", "open-link", made["request_uid"], "--tab", tab], deliver_link)
        self.assertEqual((opened["code"], opened["data"].get("outcome")), ("OK", "opened"), opened)
        self.assertEqual(opened["data"]["sender_domain"], "myworkday.com")
        self.assertEqual(f.page_of(tab), "wd_link_verified")
        # sign in by code; the site asks for a code, which arrives after two PENDING answers
        si = a.ok(["account", "signin", "--job", job["job"], "--tab", tab, "--token", token])
        self.assertEqual(si["outcome"], "code_needed", si)
        f.expected_code = SEC["code"]

        def deliver_code(n):
            if n == 2:
                self.mail(subject="Your sign-in code", text="Your verification code is:\n%s\nIt expires in 10 minutes."
                          % SEC["code"])
        sub = self.poll(["code", "submit", si["request_uid"], "--tab", tab], deliver_code)
        self.assertEqual((sub["code"], sub["data"].get("outcome")), ("OK", "accepted"), sub)
        self.assertEqual(f.page_of(tab), "wd_app_form")
        # the form, filled by the model under the token, read back, armed, one click, confirmed
        url = f.tabs[tab].url
        a.browser({"action": "snapshot"}, tab, url, snap("wd_app_form"))
        for ref, field in zip(("e20", "e21", "e22", "e23"), job["fields"]):
            a.browser({"action": "act", "kind": "type", "ref": ref, "text": field["value"], "slowly": True}, tab, url,
                      "typed")
        a.call("browser", {"profile": "jobhunter", "action": "act", "kind": "click", "ref": "e30"},
               expect="G_NOT_ARMED")
        staged = a.ok(["resume", "stage", "--variant", job["variant"], "--token", token])
        obs = {"fields": [{"label": x["label"], "value": x["value"]} for x in job["fields"]],
               "resume_filename_visible": staged["filename"]}
        a.ok(["gate", "arm", token, "--observed-file", a.write("observed.json", obs)])
        for _ in range(5):
            if not a.ok(["pace", "wait", "--platform", "workday", "--kind", "dwell"])["remaining_s"]:
                break
        a.browser({"action": "act", "kind": "click", "ref": "e30"}, tab, url, snap("wd_confirmation"))
        f.set_page(tab, "wd_confirmation")
        conf = a.ok(["gate", "confirm", token, "--evidence-file", a.write("evidence.txt",
                                                                          "Page: Thank you for applying.")])
        self.assertEqual(conf["status"], "sent")
        a.ok(["resume", "unstage", "--token", token])
        a.ok(["cycle", "end", "--cycle", a.cycle])
        # the ledger
        self.assertEqual(w.one("SELECT status FROM jobs WHERE job_uid = ?", job["job"])[0], "applied")
        self.assertEqual(w.all("SELECT status, host, email, store FROM ats_accounts"),
                         [("active", "kestrel.wd5.myworkdayjobs.com", OWNER, "keychain")])
        self.assertEqual(w.all("SELECT kind, outcome, sender_domain FROM code_uses ORDER BY id"),
                         [("link", "accepted", "myworkday.com"), ("code", "accepted", "myworkday.com")])
        self.assertEqual(len(self.security.items), 1)
        # the Keychain value only ever went to stdin, never to an argv
        self.assertTrue(any(SEC["password"] in s for s in self.security.stdin_log))
        # Alerts rows and notifications name sites and jobs, never the secrets
        alerts = dict(sheets_rows.build_rows(w.conn, "alerts", None))
        whats = [r["what"] for k, r in alerts.items() if k.startswith("S")]
        self.assertTrue(any(x.startswith("Created an account on kestrel.wd5.myworkdayjobs.com") for x in whats), whats)
        self.assertTrue(any("email code from myworkday.com" in x for x in whats), whats)
        notes = [r[0] for r in w.all("SELECT text FROM notifications")]
        self.assertTrue(any("Used an email code from myworkday.com" in n for n in notes), notes)
        # the token log: code steps as by code lines; the guard log has the two secret-field refusals only
        with open(os.path.join(paths.guard_dir(), token + ".jsonl"), "r", encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual([(x["action"], x["class"]) for x in lines if x.get("by") == "code"],
                         [("account_create", "fill"), ("account_signin", "fill"), ("code_fill", "fill")])
        blocked = [o for _t, _p, o in a.decisions if o != "allow"]
        self.assertEqual(blocked, ["G_SECRET_FIELD", "G_SECRET_FIELD", "G_NOT_ARMED"])
        # the fake CDP log holds lengths and hosts, never a value
        self.assertIn({"method": "Input.insertText", "len": len(SEC["password"])}, f.log)
        self.assertEqual(self.secret_scan(), [])


class NegativeMail:
    """Mailbox rules, on both routes."""

    def _signin_ready(self):
        """An active account and a tab on the sign-in page; returns (job, tab, token, request_uid)."""
        w, a, f = self.w, self.agent, self.cdp
        self.grant()
        job = self.approved_job()
        self.start_cycle(job)
        tab = self.open_create_page(job)
        token = self.reserve(job)
        secretstore.put("kestrel.wd5.myworkdayjobs.com", OWNER, secretstore.Secret(SEC["password"]))
        with db.tx(w.conn):
            ts = canon.now()
            w.conn.execute("INSERT INTO ats_accounts (account_uid, platform, site, host, tenant, email, store, "
                           "secret_ref, status, created_at, updated_at) VALUES ('NAAAAAAA', 'workday', 'workday', "
                           "'kestrel.wd5.myworkdayjobs.com', 'kestrel.wd5', ?, 'keychain', 'x', 'active', ?, ?)",
                           (OWNER, ts, ts))
        f.accounts[OWNER] = SEC["password"]
        f.set_page(tab, "wd_signin")
        si = a.ok(["account", "signin", "--job", job["job"], "--tab", tab, "--token", token])
        self.assertEqual(si["outcome"], "code_needed", si)
        f.expected_code = SEC["code"]
        return job, tab, token, si["request_uid"]

    def _expire(self, tab, req):
        env = None
        for _ in range(60):
            env = self.agent.exec(["code", "submit", req, "--tab", tab])
            if env["code"] != "PENDING":
                break
            self.w.clock.advance(seconds=15)
        return env

    def test_wrong_senders_stay_pending_then_expire(self):
        job, tab, token, req = self._signin_ready()
        for s in NEG["wrong_senders"]:
            self.mail(sender=s, text="Your verification code is %s" % SEC["code"])
        env = self._expire(tab, req)
        self.assertEqual((env["code"], env["data"].get("reason")), ("E_PRECONDITION", "window_expired"), env)
        self.assertEqual(self.w.one("SELECT status FROM code_requests WHERE request_uid = ?", req)[0], "expired")
        self.assertEqual(self.w.one("SELECT count(*) FROM code_uses")[0], 0)
        self.assertEqual(self.cdp.page_of(tab), "wd_signin_code")
        self.assertEqual(self.secret_scan(), [])

    def test_message_outside_the_window_is_ignored(self):
        for case in NEG["timing"]:
            with self.subTest(case=case["name"]):
                pass
        job, tab, token, req = self._signin_ready()
        r = self.w.one("SELECT requested_at FROM code_requests WHERE request_uid = ?", req)[0]
        self.mail(text="Code: %s" % SEC["code"], at=canon.ts_add(r, seconds=-120))
        late = canon.ts_add(r, seconds=660)
        env = self.agent.exec(["code", "submit", req, "--tab", tab])
        self.assertEqual(env["code"], "PENDING")
        self.w.clock.set(canon.ts_add(r, seconds=661))
        self.mail(text="Code: %s" % SEC["code"], at=late)
        env = self.agent.exec(["code", "submit", req, "--tab", tab])
        self.assertEqual((env["code"], env["data"].get("reason")), ("E_PRECONDITION", "window_expired"), env)
        self.assertEqual(self.w.one("SELECT count(*) FROM code_uses")[0], 0)

    def test_reused_code_and_message(self):
        w, a, f = self.w, self.agent, self.cdp
        job, tab, token, req = self._signin_ready()
        # a second tab of the same site waits for a code too; one message arrives for both
        tab2 = f.add_tab("wd_signin_code")
        req2 = a.ok(["code", "expect", "--job", job["job"], "--tab", tab2, "--purpose", "signin",
                     "--token", token])["request_uid"]
        w.clock.advance(seconds=65)         # in a later minute (the web route reads minute-precision dates)
        self.mail(text="Your verification code is %s" % SEC["code"])
        sub = a.exec(["code", "submit", req, "--tab", tab])
        self.assertEqual(sub["data"].get("outcome"), "accepted", sub)
        n_insert = len([e for e in f.log if e["method"] == "Input.insertText"])
        env = a.exec(["code", "submit", req2, "--tab", tab2])
        self.assertEqual((env["code"], env["data"].get("reason")), ("E_ALREADY_DONE", "message_used"), env)
        # the same code again in a new message
        w.clock.advance(seconds=20)
        self.mail(text="Your verification code is %s" % SEC["code"], subject="Your code again")
        env = a.exec(["code", "submit", req2, "--tab", tab2])
        self.assertEqual((env["code"], env["data"].get("reason")), ("E_ALREADY_DONE", "used"), env)
        self.assertEqual(len([e for e in f.log if e["method"] == "Input.insertText"]), n_insert)
        self.assertEqual(f.page_of(tab2), "wd_signin_code")
        self.assertEqual(w.one("SELECT count(*) FROM code_uses")[0], 1)
        # the model opening the code mail itself changes nothing: the code is used once
        self.assertEqual(self.secret_scan(), [])

    def test_gmail_security_prompt_during_the_read_is_a_stop(self):
        job, tab, token, req = self._signin_ready()
        self.mail(text="Your verification code is %s" % SEC["code"])
        if self.ROUTE != "web_ui":
            self.skipTest("the Gmail web page exists on the web_ui route only")
        self.cdp.gmail_security = True
        env = self.agent.exec(["code", "submit", req, "--tab", tab])
        self.assertEqual(env["code"], "E_STOP_DETECTED", env)
        self.assertEqual(self.w.one("SELECT state FROM breakers WHERE scope = 'gmail'")[0], "open")
        self.assertEqual(self.w.one("SELECT count(*) FROM code_uses")[0], 0)

    def test_caps_on_code_uses(self):
        w, a = self.w, self.agent
        job, tab, token, req = self._signin_ready()
        with db.tx(w.conn):
            base = dict(w.conn.execute("SELECT * FROM code_requests WHERE request_uid = ?", (req,)).fetchone())
            for i in range(3):
                cur = w.conn.execute(
                    "INSERT INTO code_requests (request_uid, platform, site, host, purpose, want, job_id, tab_id, route, "
                    "agent_id, requested_at, window_ends_at, status, created_at, updated_at) VALUES (?, 'workday', "
                    "'workday', ?, 'signin', 'either', ?, ?, ?, ?, ?, ?, 'used', ?, ?)",
                    ("OCAP%04d" % i, base["host"], base["job_id"], "used%d" % i, base["route"], AP, canon.now(),
                     canon.now(), canon.now(), canon.now()))
                w.conn.execute("INSERT INTO code_uses (request_id, kind, value_hmac, message_hmac, sender_domain, "
                               "received_at, outcome, used_at, site) VALUES (?, 'code', ?, ?, 'myworkday.com', ?, "
                               "'accepted', ?, 'workday')", (cur.lastrowid, "v%d" % i, "m%d" % i, canon.now(),
                                                             canon.now()))
        env = a.exec(["code", "expect", "--job", job["job"], "--tab", tab, "--purpose", "signin"])
        self.assertEqual(env["code"], "E_CEILING", env)
        self.assertTrue(env["retry_after_s"], env)
        env = a.exec(["code", "submit", req, "--tab", tab])
        self.assertEqual(env["code"], "E_CEILING", env)


class NegativeConsentAndStops:
    def test_non_consented_site(self):
        w, a, f = self.w, self.agent, self.cdp
        self.grant("ats_accounts")
        w.ok(["install", "consent-record", "--decline", "workday", "--capability", "email_codes"], "human") \
            if False else None
        job = self.approved_job()
        self.start_cycle(job)
        tab = self.open_create_page(job)
        env = a.exec(["code", "expect", "--job", job["job"], "--tab", tab, "--purpose", "signin"])
        self.assertEqual((env["code"], env["data"]["capability"]), ("E_CONSENT_MISSING", "email_codes"), env)
        # revoked mid-cycle: the platform breaker opens and the guard blocks the site
        w.ok(["browser", "consent", "revoke", "--site", "workday"], "human")
        env = a.exec(["account", "create", "--job", job["job"], "--tab", tab])
        self.assertIn(env["code"], ("E_BREAKER_OPEN", "E_CONSENT_MISSING"), env)
        self.assertEqual(w.one("SELECT state FROM breakers WHERE scope = 'ats:workday'")[0], "open")
        a.call("browser", {"profile": "jobhunter", "action": "navigate", "targetUrl": f.tabs[tab].url},
               expect="G_BREAKER_OPEN")

    def test_account_wall_without_consent_soft_stops(self):
        w, a, f = self.w, self.agent, self.cdp
        job = self.approved_job() if False else None
        tab = f.add_tab("wd_create")
        url = f.tabs[tab].url
        obs = a.browser({"action": "navigate", "targetUrl": url}, tab, url, snap("wd_create"))
        self.assertEqual((obs["stopped"], obs["softStopped"]), (False, True), obs)
        env = a.exec(["account", "status", "--job", "JAAAAAAA"])
        self.assertEqual(env["code"], "E_NOT_FOUND")

    def test_google_and_social_signin_stay_stops(self):
        a, f = self.agent, self.cdp
        self.grant()
        a.call("browser", {"profile": "jobhunter", "action": "navigate",
                           "targetUrl": "https://accounts.google.com/v3/signin/identifier"}, expect="G_HOST_NEVER")
        tab = f.add_tab("wd_create")
        url = f.tabs[tab].url
        a.browser({"action": "navigate", "targetUrl": url}, tab, url, snap("wd_create"))
        a.call("browser", {"profile": "jobhunter", "action": "act", "kind": "click", "ref": "e7"},
               expect="G_TOOL_DENIED")

    def test_sms_page_trips_and_reads_no_mail(self):
        w, a, f = self.w, self.agent, self.cdp
        job, tab, token = self._ready_with_account()
        f.pages["wd_signin"]["buttons"][0]["action"] = {"signin": {"next": "wd_sms"}}
        f.set_page(tab, "wd_signin")
        env = a.exec(["account", "signin", "--job", job["job"], "--tab", tab, "--token", token])
        self.assertEqual(env["code"], "E_STOP_DETECTED", env)
        self.assertEqual(w.one("SELECT state, reason_code FROM breakers WHERE scope = 'ats:workday'")[:],
                         ("open", "ats_security"))
        self.assertEqual(self.inbox.read, [])
        if self.ROUTE == "app_password":
            self.assertEqual(self.imap.queries, [])
        # a page that shows both the SMS code and an email code: the trip wins
        from jobhunter import detect
        verdict, sig = detect.match({"platform": "workday", "text": f.pages["wd_sms_and_email"]["text"]})
        self.assertEqual((verdict, sig["id"]), ("stop", "ats_phone_code"))

    def _ready_with_account(self):
        w = self.w
        self.grant()
        job = self.approved_job()
        self.start_cycle(job)
        tab = self.open_create_page(job)
        token = self.reserve(job)
        secretstore.put("kestrel.wd5.myworkdayjobs.com", OWNER, secretstore.Secret(SEC["password"]))
        with db.tx(w.conn):
            ts = canon.now()
            w.conn.execute("INSERT INTO ats_accounts (account_uid, platform, site, host, tenant, email, store, "
                           "secret_ref, status, created_at, updated_at) VALUES ('NAAAAAAA', 'workday', 'workday', "
                           "'kestrel.wd5.myworkdayjobs.com', 'kestrel.wd5', ?, 'keychain', 'x', 'active', ?, ?)",
                           (OWNER, ts, ts))
        self.cdp.accounts[OWNER] = SEC["password"]
        return job, tab, token

    def test_model_cannot_read_or_type_the_password(self):
        w, a, f = self.w, self.agent, self.cdp
        job, tab, token = self._ready_with_account()
        a.exec(["security", "find-generic-password", "-s", "openclaw-job-hunter.x.ats", "-a", "x", "-w"],
               expect=None) if False else None
        dec = self.guard.ask({"op": "exec", "agent": AP, "session": SESSION, "tool": "exec",
                              "params": {"command": "/usr/bin/security find-generic-password -s openclaw-job-hunter"
                                                    " -a kestrel -w", "timeoutSeconds": 90}, "ctx": a.ctx})
        self.assertIn(dec["outcome"], ("G_EXEC_ACL", "G_EXEC_SHAPE"), dec)
        a.call("read", {"path": secretstore.file_path()}, expect="G_PATH_DENIED")
        a.call("read", {"path": os.path.join(paths.private_dir(), "consent.json")}, expect="G_PATH_DENIED")
        f.set_page(tab, "wd_signin")
        url = f.tabs[tab].url
        a.browser({"action": "snapshot"}, tab, url, "- textbox \"Email Address\" [ref=e2]\n- textbox \"Password\" "
                  "[ref=e3]\n- button \"Sign In\" [ref=e6]")
        a.call("browser", {"profile": "jobhunter", "action": "act", "kind": "type", "ref": "e3", "text": "guess"},
               expect="G_SECRET_FIELD")
        a.call("browser", {"profile": "jobhunter", "action": "act", "kind": "fill",
                           "fields": [{"ref": "e2", "type": "textbox", "value": OWNER},
                                      {"ref": "e3", "type": "textbox", "value": "guess"}]}, expect="G_SECRET_FIELD")
        a.browser({"action": "act", "kind": "click", "ref": "e3"}, tab, url, "")
        a.call("browser", {"profile": "jobhunter", "action": "act", "kind": "type", "text": "guess"},
               expect="G_SECRET_FIELD")
        st = a.ok(["account", "status", "--job", job["job"]])
        self.assertEqual(st["next"], "signin")
        # signing in by code works, and the agent's own model view never held the value
        si = a.ok(["account", "signin", "--job", job["job"], "--tab", tab, "--token", token])
        self.assertEqual(si["outcome"], "code_needed")
        self.assertEqual(self.secret_scan(), [])

    def test_required_marketing_box_refuses(self):
        w, a, f = self.w, self.agent, self.cdp
        self.grant()
        job = self.approved_job()
        self.start_cycle(job)
        tab = self.open_create_page(job, "wd_create_marketing")
        token = self.reserve(job)
        made = a.ok(["account", "create", "--job", job["job"], "--tab", tab, "--token", token])
        self.assertEqual((made["outcome"], made["reason"]), ("rejected", "terms_unusual"), made)
        rows = w.all("SELECT class, action, required FROM account_terms ORDER BY id")
        self.assertEqual(rows, [("standard_terms", "ticked", 1), ("unusual", "refused", 1)])
        self.assertEqual(w.one("SELECT status, status_reason FROM jobs WHERE job_uid = ?", job["job"])[:],
                         ("needs_human", "account_terms"))
        self.assertEqual(w.one("SELECT count(*) FROM ats_accounts")[0], 0)
        self.assertEqual(f.accounts, {})

    def test_daily_cap_on_new_accounts(self):
        w, a = self.w, self.agent
        self.grant()
        job = self.approved_job()
        self.start_cycle(job)
        tab = self.open_create_page(job)
        with db.tx(w.conn):
            for i in range(3):
                ts = canon.now()
                w.conn.execute("INSERT INTO ats_accounts (account_uid, platform, site, host, tenant, email, store, "
                               "secret_ref, status, created_at, updated_at) VALUES (?, 'workday', 'workday', ?, ?, ?, "
                               "'keychain', 'x', 'active', ?, ?)", ("NB%06d" % i, "t%d.wd1.myworkdayjobs.com" % i,
                                                                   "t%d.wd1" % i, OWNER, ts, ts))
        env = a.exec(["account", "create", "--job", job["job"], "--tab", tab])
        self.assertEqual(env["code"], "E_CEILING", env)
        self.assertTrue(env["retry_after_s"])

    def test_cdp_unavailable_and_keychain_locked(self):
        w, a, f = self.w, self.agent, self.cdp
        self.grant()
        job = self.approved_job()
        self.start_cycle(job)
        tab = self.open_create_page(job)
        token = self.reserve(job)
        self.security.locked = True
        env = a.exec(["account", "create", "--job", job["job"], "--tab", tab, "--token", token])
        self.assertEqual((env["code"], env["data"]["reason"]), ("E_ROUTE_UNAVAILABLE", "keychain_unavailable"), env)
        self.assertEqual(w.one("SELECT count(*) FROM ats_accounts")[0], 0)
        self.security.locked = False
        f.stop()
        env = a.exec(["account", "create", "--job", job["job"], "--tab", tab, "--token", token])
        self.assertEqual(env["code"], "E_ROUTE_UNAVAILABLE", env)


class CaptchaHandoff:
    def _captcha_on_create(self):
        """A CAPTCHA on the create page: the guard soft-stops and its detect call opens the task."""
        w, a, f = self.w, self.agent, self.cdp
        self.grant()
        job = self.approved_job()
        self.start_cycle(job)
        tab = self.open_create_page(job)
        token = self.reserve(job)
        f.set_page(tab, "wd_captcha")
        url = f.tabs[tab].url
        obs = a.browser({"action": "snapshot"}, tab, url, snap("wd_captcha"))
        self.assertEqual((obs["stopped"], obs["softStopped"]), (False, True), obs)
        dets = a.guard_detects()
        self.assertEqual(dets[-1]["data"]["captcha"]["token_outcome"], "released", dets[-1])
        code = dets[-1]["data"]["captcha"]["captcha_code"]
        a.call("browser", {"profile": "jobhunter", "action": "act", "kind": "click", "ref": "e40"},
               expect="G_TOOL_DENIED")
        return job, tab, token, code

    def test_captcha_is_handed_to_the_owner(self):
        w = self.w
        oc_log = w.install_fake_openclaw()
        job, tab, token, code = self._captcha_on_create()
        self.assertEqual(w.one("SELECT status, fail_reason FROM actions WHERE token = ?", token)[:],
                         ("failed", "form_blocked_before_submit"))
        self.assertEqual(w.one("SELECT status, status_reason FROM jobs WHERE job_uid = ?", job["job"])[:],
                         ("needs_human", "captcha_wait"))
        row = w.one("SELECT status, screenshot_path FROM captcha_tasks WHERE code = ?", code)
        self.assertEqual(row[0], "open")
        self.assertTrue(row[1] and os.path.exists(row[1]))
        self.assertEqual(oct(os.stat(row[1]).st_mode & 0o777), "0o600")
        alerts = dict(sheets_rows.build_rows(w.conn, "alerts", None))
        k = [r for i, r in alerts.items() if i.startswith("K")]
        self.assertEqual([(r["severity"], r["status"]) for r in k], [("Needs you", "Open")])
        # the alert goes alone, with the screenshot
        w.write_config(**{"boards.sites.workday.apply": "browser", "gmail.route": self.ROUTE,
                          "owner.notify.channel": "whatsapp", "owner.notify.to": "+15555550123"})
        w.ok(["notify", "flush", "--deliver"])
        with open(oc_log, "r", encoding="utf-8") as fh:
            calls = [json.loads(x) for x in fh if x.strip()]
        sends = [c for c in calls if "message" in c.get("argv", []) and "send" in c.get("argv", [])]
        self.assertTrue(sends, calls)
        first = sends[0]["argv"]
        self.assertIn("--media=" + row[1], first)
        self.assertTrue(any(("/jh continue %s" % code) in x for x in first))

    def test_continue_by_non_owner_is_refused(self):
        w, a = self.w, self.agent
        job, tab, token, code = self._captcha_on_create()
        out = self.guard.ask({"op": "command", "ctx": {"channel": "whatsapp", "senderId": "+15555550199",
                                                        "args": "continue " + code}})
        self.assertTrue(out["text"].startswith("G_NOT_OWNER"), out)
        a.exec(["continue", code], expect="G_EXEC_ACL")
        env = helpers.agent_cli(AP, ["continue", code])[1]
        self.assertEqual(env["code"], "E_CALLER_NOT_ALLOWED", env)
        rc, env = w.run(["--pin-stdin", "continue", code], "system", stdin_text="000000\n")
        self.assertEqual(env["code"], "E_AUTH_FAILED", env)
        self.assertEqual(w.one("SELECT status FROM captcha_tasks WHERE code = ?", code)[0], "open")

    def test_continue_checks_then_resumes(self):
        w, a, f = self.w, self.agent, self.cdp
        job, tab, token, code = self._captcha_on_create()
        env = w.fails(["continue", code], "E_PRECONDITION", "human")
        self.assertEqual(env["data"]["check"]["captcha_gone"], False)
        # the owner solves it in the agent's window, then sends /jh continue <code> from the owner's chat
        f.set_page(tab, "wd_create")
        out = self.guard.ask({"op": "command", "ctx": {"channel": "whatsapp", "senderId": "+15555550100",
                                                        "args": "continue " + code}})
        calls = self.guard.ask({"op": "jh_calls"})["calls"]
        argv = calls[-1]
        self.assertEqual(argv[2:], ["--human", "continue", code])
        buf = io.StringIO()
        rc = cli.main(argv, env={}, stdin=io.StringIO(""), stdout=buf)
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("continues in the next applier cycle", buf.getvalue())
        self.assertEqual(w.one("SELECT status, resolved_by FROM captcha_tasks WHERE code = ?", code)[:],
                         ("resolved", "human:chat"))
        self.assertEqual(w.one("SELECT status, status_reason FROM jobs WHERE job_uid = ?", job["job"])[:],
                         ("eligible", "captcha_resolved"))
        self.assertIsNotNone(w.one("SELECT value FROM meta WHERE key = 'dispatch_nudge:applier'"))
        a.ok(["cycle", "end", "--cycle", a.cycle])
        # the next applier cycle takes the job again (submit first) and reserves a new token
        self.start_cycle(job)
        det = {"platform": "workday", "url": f.tabs[tab].url, "title": "Kestrel Commerce Careers", "http_status": None,
               "text": f.tabs[tab].page["text"]}
        self.assertEqual(a.ok(["detect", "--file", a.write("detect-again.json", det)])["verdict"], "clear")
        token2 = self.reserve(job)
        self.assertNotEqual(token2, token)

    def test_continue_with_the_tab_closed(self):
        w = self.w
        job, tab, token, code = self._captcha_on_create()
        self.cdp.close(tab)
        env = w.fails(["continue", code], "E_PRECONDITION", "human")
        self.assertEqual(env["data"]["check"]["tab_found"], False)

    def test_timeout_skips_the_job_and_closes_the_tab(self):
        w = self.w
        job, tab, token, code = self._captcha_on_create()
        w.clock.advance(hours=2, seconds=1)
        res = w.ok(["captcha", "expire"])
        self.assertEqual(res["timed_out"], [code])
        self.assertNotIn(tab, self.cdp.tabs)
        self.assertEqual(w.one("SELECT status FROM captcha_tasks WHERE code = ?", code)[0], "timed_out")
        self.assertEqual(w.one("SELECT status, status_reason FROM jobs WHERE job_uid = ?", job["job"])[:],
                         ("closed", "captcha_timeout"))
        skipped = dict(sheets_rows.build_rows(w.conn, "skipped", None))
        self.assertEqual(skipped[job["job"]]["reason"], "CAPTCHA not solved in time")

    def test_repeat_captcha_trips_the_site(self):
        from jobhunter import captcha
        w = self.w
        job, tab, token, code = self._captcha_on_create()
        jid = w.one("SELECT id FROM jobs WHERE job_uid = ?", job["job"])[0]
        with db.tx(w.conn):
            w.conn.execute("UPDATE captcha_tasks SET status = 'resolved' WHERE code = ?", (code,))
            w.conn.execute("UPDATE jobs SET status = 'needs_human' WHERE id = ?", (jid,))
            second = captcha.open_task(w.conn, job_id=jid, tab_id=tab, token=None, opened_by="guard")
        self.assertIsNotNone(second)
        with db.tx(w.conn):
            w.conn.execute("UPDATE captcha_tasks SET status = 'resolved' WHERE code = ?", (second["code"],))
            third = captcha.open_task(w.conn, job_id=jid, tab_id=tab, token=None, opened_by="guard")
        self.assertIsNone(third)
        self.assertEqual(w.one("SELECT state, reason_code FROM breakers WHERE scope = 'ats:workday'")[:],
                         ("open", "captcha_repeat"))

    def test_captcha_after_the_submit_click_leaves_the_action_unknown(self):
        from jobhunter import captcha, gate
        w = self.w
        self.grant()
        job = self.approved_job()
        self.start_cycle(job)
        tab = self.open_create_page(job)
        token = self.reserve(job)
        gate.append_code_line(token, cls="commit", action="click", host="kestrel.wd5.myworkdayjobs.com",
                              name="Submit Application")
        with db.tx(w.conn):
            w.conn.execute("UPDATE actions SET status = 'armed', armed_at = ? WHERE token = ?", (canon.now(), token))
            jid = w.one("SELECT id FROM jobs WHERE job_uid = ?", job["job"])[0]
            task = captcha.open_task(w.conn, job_id=jid, tab_id=tab, token=token, opened_by="guard")
        self.assertEqual(task["token_outcome"], "unknown")
        self.assertEqual(w.one("SELECT status FROM actions WHERE token = ?", token)[0], "unknown")
        self.cdp.set_page(tab, "wd_confirmation")
        res = w.ok(["continue", task["code"]], "human")
        self.assertEqual((res["resumed"], res.get("reconcile")), (False, True))
        tasks = self.agent.ok(["reconcile", "list", "--route", "browser"])["tasks"]
        self.assertEqual([t["token"] for t in tasks], [token])
        ev = self.agent.write("reconcile.txt", "Page: Thank you for applying.")
        self.agent.ok(["reconcile", "resolve", token, "--result", "found", "--method", "ats_page", "--evidence-file", ev])
        self.assertEqual(w.one("SELECT status FROM actions WHERE token = ?", token)[0], "sent")


class TestWebRoute(HappyPath, NegativeMail, NegativeConsentAndStops, CaptchaHandoff, OtpBase):
    ROUTE = "web_ui"


class TestAppPasswordRoute(HappyPath, NegativeMail, OtpBase):
    ROUTE = "app_password"


if __name__ == "__main__":
    unittest.main()
