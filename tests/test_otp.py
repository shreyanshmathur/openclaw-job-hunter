"""U1: email codes and sign-in links (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.4, 2.5, 2.7): sender and link allowlists,
the request window, extraction, single use, caps and the failure breaker. Fictional senders and codes only."""
from __future__ import annotations

import hashlib
import unittest

import tests  # noqa: F401
from jobhunter import canon, db, otp
from jobhunter.errors import Denied
from tests.fakes.u1.fake_browser_steps import OWNER, applier_state, grant_caps, write_owner_config
from tests.helpers import HomeTestCase

ALLOWLIST_SHA256 = "d06ae163e3d1c7fee1bf2e84196ea235c8fa007816bb54e2930d555e57e2ffd1"
REQ = {"site": "workday", "host": "kestrel.wd5.myworkdayjobs.com", "want": "either", "platform": "workday"}


class TestAllowlist(unittest.TestCase):
    def test_data_file_is_pinned(self):
        with open(otp.ALLOWLIST_FILE, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(digest, ALLOWLIST_SHA256, "ats_mail.json changed: review it, then update the pin")

    def test_senders(self):
        ok = lambda d, plat="workday", doms=(), links=(): otp.sender_ok(d, plat, REQ, list(doms), list(links))
        self.assertTrue(ok("myworkday.com"))
        self.assertTrue(ok("kestrel.myworkday.com"))
        self.assertFalse(ok("myworkday.com.evil.example"))
        self.assertFalse(ok("evilmyworkday.com"))
        self.assertFalse(ok("accounts.google.com"))
        self.assertFalse(ok("gmail.com"))
        self.assertFalse(ok("examplebank.com"))
        # tenant senders: a company domain counts only with a link to a listed or tenant host
        self.assertFalse(ok("kestrel.example", "icims", ["kestrel.example"]))
        ireq = dict(REQ, site="icims", host="careers-kestrel.icims.com")
        self.assertTrue(otp.sender_ok("kestrel.example", "icims", ireq, ["kestrel.example"],
                                      ["https://careers-kestrel.icims.com/jobs/1/verify"]))
        self.assertFalse(ok("kestrel.example", "workday", ["kestrel.example"],
                            ["https://kestrel.wd5.myworkdayjobs.com/verify"]))

    def test_hard_deny_beats_data(self):
        for d in ("google.com", "linkedin.com", "login.microsoftonline.com", "icloud.com", "paypal.com",
                  "mail.kestrelbank.example"):
            self.assertTrue(otp.hard_denied(d), d)
        self.assertFalse(otp.hard_denied("myworkday.com"))

    def test_link_hosts(self):
        self.assertTrue(otp.link_host_ok("kestrel.wd5.myworkdayjobs.com", "workday", REQ))
        self.assertFalse(otp.link_host_ok("other.wd5.myworkdayjobs.com", "workday", REQ))   # another tenant
        self.assertFalse(otp.link_host_ok("click.tracker.example", "workday", REQ))
        self.assertTrue(otp.link_host_ok("job-boards.greenhouse.io", "greenhouse", dict(REQ, site="greenhouse")))
        self.assertTrue(otp.link_host_ok("kestrel.fa.us2.oraclecloud.com", "oracle_hcm",
                                         dict(REQ, site="oracle_hcm", host="kestrel.fa.us2.oraclecloud.com")))
        host_req = dict(REQ, site="host:careers.kestrel.example", host="careers.kestrel.example")
        self.assertTrue(otp.link_host_ok("careers.kestrel.example", None, host_req))
        self.assertFalse(otp.link_host_ok("kestrel.example", None, host_req))


class TestExtraction(unittest.TestCase):
    def ex(self, text="", subject="Verify", html=None, links=None, platform="workday", want="either", **kw):
        return otp.extract({"subject": subject, "text": text, "html": html, "links": links or []}, platform,
                           dict(REQ, want=want, site=platform or "host:careers.kestrel.example"), **kw)

    def test_single_code(self):
        self.assertEqual(self.ex("Your verification code is 483920.")["value"], "483920")
        self.assertEqual(self.ex("Hello,\n\n483920\n\nThanks")["value"], "483920")

    def test_two_codes_are_ambiguous(self):
        with self.assertRaises(ValueError):
            self.ex("Your code is 483920. Your old code was 112233.")

    def test_dates_phones_and_ids_are_not_codes(self):
        text = ("Requisition 102938 was posted on 2026-10-06 at 10:15. Call 555-0144 for help.\n"
                "Applied in October 2026. Suite 200 Kestrel Road.")
        self.assertIsNone(self.ex(text))
        self.assertIsNone(self.ex("Your code: see requisition 445566", req_ids=("445566",)))
        self.assertIsNone(self.ex("See https://kestrel.wd5.myworkdayjobs.com/x?id=123456 now", subject="Hello"))

    def test_greenhouse_eight_characters(self):
        c = self.ex("Copy and paste this code into the security code field: Q7RZ4M2K", platform="greenhouse")
        self.assertEqual(c["value"], "Q7RZ4M2K")
        self.assertIsNone(self.ex("password reminder: ABCDEFGH", platform="greenhouse"))

    def test_html_only_mail(self):
        html = "<html><style>.x{}</style><p>Use this code:</p><p><b>483920</b></p></html>"
        self.assertEqual(self.ex(html=html)["value"], "483920")

    def test_links(self):
        link = "https://kestrel.wd5.myworkdayjobs.com/verify?t=FIXTURETOKEN123"
        c = self.ex("Please confirm your email.", links=[{"href": link, "text": "Verify Email"}], want="link")
        self.assertEqual((c["kind"], c["value"], c["link_host"]), ("link", link, "kestrel.wd5.myworkdayjobs.com"))
        # path deny list and tracking wrappers are never followed
        self.assertIsNone(self.ex("x", links=[{"href": "https://kestrel.wd5.myworkdayjobs.com/unsubscribe",
                                               "text": "Verify"}], want="link"))
        self.assertIsNone(self.ex("x", links=[{"href": "https://click.tracker.example/r?u=" + link,
                                               "text": "Verify Email"}], want="link"))
        with self.assertRaises(ValueError):
            self.ex("x", want="link", links=[{"href": link, "text": "Verify"},
                                             {"href": link + "2", "text": "Confirm"}])
        # a code wins over a link
        self.assertEqual(self.ex("Your code is 483920", links=[{"href": link, "text": "Verify"}])["kind"], "code")


class TestRequests(HomeTestCase):
    start_ts = "2026-10-06T09:00:00Z"

    def setUp(self):
        super().setUp()
        write_owner_config()
        self.st = applier_state(self.conn)
        grant_caps(self.conn)
        with db.tx(self.conn):
            self.req = otp.open_request(self.conn, platform="workday", site="workday",
                                        host="kestrel.wd5.myworkdayjobs.com", tenant="kestrel.wd5", purpose="signin",
                                        want="either", job_id=self.st["job_id"], tab_id="TAB1", agent_id="jobhunter-applier",
                                        cycle_id=self.st["cycle_id"])

    def msg(self, i=1, sender="no-reply@myworkday.com", text="Your verification code is 483920", at=None, to=OWNER):
        return {"id": "gm:%d" % i, "from": sender, "to": [to], "subject": "Code", "text": text,
                "received_at": at or canon.ts_add(self.req["requested_at"], seconds=30)}

    def used_request(self, i: int) -> int:
        with db.tx(self.conn):
            r = otp.open_request(self.conn, platform="workday", site="workday", host="h", tenant="t", purpose="signin",
                                 want="code", job_id=self.st["job_id"], tab_id="U%d" % i, agent_id="jobhunter-applier")
            otp.end_request(self.conn, r["id"], "used", None)
        return r["id"]

    def pick(self, msgs, minute=False):
        from jobhunter import config
        return otp.select_candidate(self.conn, self.req, msgs, config.load(self.conn), minute)

    def test_window(self):
        r = self.req["requested_at"]
        self.assertIsNone(self.pick([self.msg(at=canon.ts_add(r, seconds=-1))]))
        self.assertIsNotNone(self.pick([self.msg(at=r)]))
        self.assertIsNotNone(self.pick([self.msg(at=self.req["window_ends_at"])]))
        self.assertIsNone(self.pick([self.msg(at=canon.ts_add(self.req["window_ends_at"], seconds=1))]))
        # the web route reads minute-precision dates: from the minute of the request on
        self.assertIsNotNone(self.pick([self.msg(at=r[:17] + "00Z")], minute=True))
        self.assertIsNone(self.pick([self.msg(at=canon.ts_add(r[:17] + "00Z", seconds=-60))], minute=True))

    def test_recipient_and_security_mail(self):
        self.assertIsNone(self.pick([self.msg(to="someone.else@example.com")]))
        self.assertIsNone(self.pick([self.msg(text="Security alert: new sign-in. Code 483920")]))

    def test_one_request_per_tab(self):
        with db.tx(self.conn):
            second = otp.open_request(self.conn, platform="workday", site="workday", host="h", tenant="t",
                                      purpose="signin", want="code", job_id=self.st["job_id"], tab_id="TAB1",
                                      agent_id="jobhunter-applier")
        st = self.conn.execute("SELECT status, reason FROM code_requests WHERE id = ?", (self.req["id"],)).fetchone()
        self.assertEqual(tuple(st), ("cancelled", "superseded"))
        self.assertEqual(second["status"], "waiting")
        # the unique index refuses a second waiting request on one tab
        with self.assertRaises(Denied) as cm:
            with db.tx(self.conn):
                self.conn.execute("UPDATE code_requests SET status = 'waiting' WHERE id = ?", (self.req["id"],))
        self.assertEqual(cm.exception.code, "E_LOCKED")

    def test_single_use(self):
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO code_uses (request_id, kind, value_hmac, message_hmac, sender_domain, "
                              "received_at, outcome, used_at, site) VALUES (?, 'code', ?, ?, 'myworkday.com', ?, "
                              "'accepted', ?, 'workday')", (self.req["id"], otp.hmac_of(self.conn, "483920"),
                                                           otp.hmac_of(self.conn, "gm:1"), canon.now(), canon.now()))
        # the same message again: refused (it arrived for this request), never typed twice
        with self.assertRaises(Denied) as cm:
            self.pick([self.msg(i=1)])
        self.assertEqual(cm.exception.code, "E_ALREADY_DONE")
        cases = (("message", "other", otp.hmac_of(self.conn, "gm:1"), self.used_request(1)),
                 ("value", otp.hmac_of(self.conn, "483920"), "o2", self.used_request(2)),
                 ("request", "v3", "m3", self.req["id"]))
        for what, vh, mh, rid in cases:
            with self.assertRaises(Denied) as cm:
                with db.tx(self.conn):
                    self.conn.execute("INSERT INTO code_uses (request_id, kind, value_hmac, message_hmac, "
                                      "sender_domain, received_at, outcome, used_at, site) VALUES (?, 'code', ?, ?, "
                                      "'x', 'x', 'accepted', 'x', 'workday')", (rid, vh, mh))
            self.assertEqual(cm.exception.code, "E_ALREADY_DONE", what)

    def test_hashes_only(self):
        h = otp.hmac_of(self.conn, "483920")
        self.assertEqual(len(h), 64)
        self.assertNotEqual(h, hashlib.sha256(b"483920").hexdigest())
        self.assertNotIn("483920", "\n".join(self.conn.iterdump()))

    def test_caps(self):
        from jobhunter import config
        cfg = config.load(self.conn)
        with db.tx(self.conn):
            pass
        for i in range(3):
            rid = self.used_request(10 + i)
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO code_uses (request_id, kind, value_hmac, message_hmac, sender_domain, "
                                  "received_at, outcome, used_at, site) VALUES (?, 'code', ?, ?, 'x', 'x', "
                                  "'accepted', ?, 'workday')", (rid, "v%d" % i, "m%d" % i, canon.now()))
        with self.assertRaises(Denied) as cm:
            otp.uses_caps(self.conn, cfg, "workday")
        self.assertEqual(cm.exception.code, "E_CEILING")
        self.assertEqual(cm.exception.retry_after, 86400)
        otp.uses_caps(self.conn, cfg, "icims")        # another site is under its own cap

    def test_failure_breaker(self):
        from jobhunter import config
        cfg = config.load(self.conn)
        with db.tx(self.conn):
            for i in range(3):
                r = otp.open_request(self.conn, platform="workday", site="workday", host="h", tenant="t",
                                     purpose="signin", want="code", job_id=self.st["job_id"], tab_id="T%d" % i,
                                     agent_id="jobhunter-applier")
                otp.end_request(self.conn, r["id"], "expired", "window_expired")
                otp.failure(self.conn, self.conn.execute("SELECT * FROM code_requests WHERE id = ?",
                                                         (r["id"],)).fetchone(), cfg, "window_expired")
        row = self.conn.execute("SELECT state, reason_code FROM breakers WHERE scope = 'ats:workday'").fetchone()
        self.assertEqual(tuple(row), ("open", "otp_failures"))

    def test_expire_waiting(self):
        self.clock.advance(minutes=11)
        with db.tx(self.conn):
            out = otp.expire_waiting(self.conn)
        self.assertEqual(out, [self.req["request_uid"]])


if __name__ == "__main__":
    unittest.main()
