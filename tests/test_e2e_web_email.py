"""E2E (INT): the default email route, gmail.route = "web_ui", end to end. A cold email is researched, drafted, QC'd
and approved by the real lanes (U3, U4, U6), then the outreach agent sends it in Gmail in the browser, replayed from
the recording tests/fixtures/e2e/guard_web_email.json through the real jobhunter-guard runtime (U8) while the real
core (U1) runs every jh.py call the guard lets through.

The Gmail pages of the recording are recorded-shape DOMs; the real read-only drivers (U6) run on them under node
and their output is what the evaluate calls return: the Sent, Outbox and Scheduled precheck counts
(read_gmail_list.js), the compose read-back (read_compose.js) that `gate arm` compares with the approved text hash,
and the Sent-folder read-back (read_gmail_message.js) after the one click on Send, which `gate confirm --observed-file` compares with the
approved text hash again. The guard refuses Compose before the token, Send before arm and dwell, and a second Send.
A compose read-back that differs from the approved text fails the token at `gate arm` and the guard then refuses
the Send. A Sent copy that differs makes `gate confirm` exit 6: the token is unknown (reconcile decides, a
resolve_unknown task names it), never sent, and no thread is made. No mailer, SMTP or IMAP is involved.
TestWebEmailConfirmCommands runs the same confirm with and without a changed Sent copy through the jh.py commands
alone (support.OutreachSteps.web_send, used by the consent and email-finder e2e tests too), so it runs without node.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import unittest
from urllib.parse import quote_plus

import tests  # noqa: F401
from jobhunter import auth, canon, gate, paths, sheets_rows
from tests.fakes.u1 import write_heartbeat
from tests.fakes.u3 import install_reviewer_hashes
from tests.fixtures.e2e.support import COLD_SUBJECT, OU, OutreachSteps, World, e2e_fixture, install_qc_fakes
from tests.test_e2e_guard_replay import GuardBridge, Replay, node_major

RECIPIENT = "jordan.blake@kestrel.example"
THREAD_ID = "18c2f0000000e001"
GMAIL = "https://mail.google.com/mail/u/0/"
NAME_RE = re.compile(r"\{([A-Z_]+)\}")


def run_driver(name: str, page: dict) -> dict:
    from tests.test_drivers_static import run_driver as run    # U6's fake DOM for the drivers under node
    return run(name, page)


def fill(obj, values: dict):
    """A recorded page with its {NAME} parts filled in."""
    if isinstance(obj, str):
        return NAME_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), obj)
    if isinstance(obj, list):
        return [fill(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: fill(v, values) for k, v in obj.items()}
    return obj


class Later:
    """A replay value computed when the step runs (for example a precheck file's observed_at)."""

    def __init__(self, fn):
        self.fn = fn

    def __str__(self) -> str:
        return self.fn()


class WebWorld(World, OutreachSteps):
    pass


@unittest.skipUnless(node_major() >= 24 and shutil.which("node"),
                     "needs node 24 or later (the guard plugin runs TypeScript directly)")
class TestWebEmailRoute(unittest.TestCase):
    def setUp(self):
        self.w = WebWorld()
        self.addCleanup(self.w.stop)
        install_qc_fakes(self)
        self.w.onboard()
        install_reviewer_hashes(self.w.conn)
        self.assertTrue(auth.create_guard_key())
        self.rec = e2e_fixture("guard_web_email.json")

    def approved_draft(self) -> dict:
        w = self.w
        self.assertEqual(w.cfg["gmail"]["route"], "web_ui", "the shipped default route")
        cyc = w.preflight("outreach", OU)
        c = w.add_contact(cyc, full_name="Jordan Blake", first_name="Jordan", company="Kestrel Commerce",
                          company_domain="kestrel.example", email=RECIPIENT, email_grade="A",
                          email_evidence_url="https://kestrel.example/team", source_url="https://kestrel.example/team")
        w.verify_email(cyc, RECIPIENT, "A", "Team page: https://kestrel.example/team")
        d = w.cold_email_draft(cyc, c["contact_uid"], "Jordan", "Kestrel Commerce")
        w.end_cycle(cyc, OU)
        self.assertEqual([a["draft_uid"] for a in w.approve_all()], [d])
        row = w.one("SELECT id, status, send_route, recipient FROM drafts WHERE draft_uid = ?", d)
        self.assertEqual(tuple(row)[1:], ("approved", "browser", RECIPIENT))
        cid = w.one("SELECT id FROM contacts WHERE contact_uid = ?", c["contact_uid"])[0]
        send_text = w.ok(["draft", "show", d, "--field", "send_text"])["value"]
        return {"draft_uid": d, "draft_id": row[0], "contact_uid": c["contact_uid"], "contact_id": cid,
                "send_text": send_text}

    def start_guard(self) -> GuardBridge:
        h = paths.home()
        g = GuardBridge({"repo": paths.REPO, "python": h["python"], "homeFile": paths.home_file(),
                         "publicReadonlyAgents": ["main"]}, canon.now())
        self.addCleanup(g.close)
        hb = os.path.join(paths.guard_dir(), "heartbeat.json")
        if os.path.exists(hb):
            os.remove(hb)
        self.assertTrue(g.ask({"op": "heartbeat"})["ok"])
        return g

    def replay_vars(self, made: dict, body_on_page: str | None = None, sent_body: str | None = None) -> dict:
        """The values of the recording, with each evaluate result produced by the real driver on its page.
        body_on_page: the compose body as a page changed it; sent_body: the body of the Sent copy."""
        w = self.w
        owner = w.cfg["owner"]["gmail_address"]
        head, _sep, body = made["send_text"].partition("\n\n")
        self.assertEqual(head, "Subject: " + COLD_SUBJECT)
        plan = gate.precheck_plan(w.conn, "cold_email", route="browser", contact_id=made["contact_id"])
        queries = {c["name"]: c["query"] for c in plan["checks"]}
        self.assertEqual(queries["sent_to_address"], "in:sent to:" + RECIPIENT)
        self.assertIsNone(queries["sent_to_other_addresses"])
        self.assertTrue(queries["sent_company_query"])
        v = {"CONTACT": made["contact_uid"], "DRAFT": made["draft_uid"], "OWNER": owner, "RECIPIENT": RECIPIENT,
             "SUBJECT": COLD_SUBJECT, "BODY_TYPED": body, "THREAD_ID": THREAD_ID, "URL_INBOX": GMAIL + "#inbox",
             "URL_THREAD": GMAIL + "#sent/" + THREAD_ID, "SNIPPET": body.split("\n")[0],
             "SENT_TITLE": "Tue, Sep 29, 2026, 9:20 AM", "SENT_SHORT": "9:20 AM",
             "URL_SENT_CHECK": GMAIL + "#search/" + quote_plus("in:sent to:%s newer_than:1d" % RECIPIENT)}
        names = {"SENT_TO": "sent_to_address", "SENT_COMPANY": "sent_company_query", "OUTBOX": "outbox_query",
                 "SCHEDULED": "scheduled_query"}
        counts = {"sent_to_other_addresses": 0}
        pages = self.rec["pages"]
        for var, check in names.items():
            v["URL_" + var] = GMAIL + "#search/" + quote_plus(queries[check])
            out = run_driver("read_gmail_list", fill(pages["search_empty"], dict(v, URL=v["URL_" + var])))
            self.assertEqual((out["loaded"], out["count"], out["query"]), (True, 0, queries[check]), out)
            counts[check] = out["count"]
            v["LIST_" + var] = json.dumps(out)
        v["PRECHECK_FILE"] = Later(lambda: json.dumps(
            {"kind": "cold_email", "platform": "gmail", "observed_at": canon.now(),
             "checks": [{"name": c["name"], "value": counts[c["name"]]} for c in plan["checks"]]}))
        det = run_driver("detect_page", fill(pages["inbox"], v))
        self.assertEqual((det["hint"], det["detect_file"]["platform"]), ("clear", "gmail"), det)
        v["DETECT_OUT"], v["DETECT_FILE"] = json.dumps(det), json.dumps(det["detect_file"])
        ident = run_driver("read_identity", fill(pages["inbox"], v))
        self.assertEqual(ident, {"platform": "gmail", "observed": {"account_email": owner}})
        v["IDENTITY_OUT"] = json.dumps(ident)
        # the compose window as the page holds it after the typing (body_on_page: what a page changed)
        compose = run_driver("read_compose", fill(pages["compose"], dict(v, BODY_TYPED=body_on_page or body)))
        self.assertEqual((compose["to"], compose["subject_source"], compose["signature_block_present"]),
                         ([RECIPIENT], "field", False), compose)
        v["COMPOSE_OUT"], v["OBSERVED"] = json.dumps(compose), compose["observed_text"]
        toast = run_driver("read_toast", fill(pages["sent_toast"], v))
        v["TOAST_OUT"] = json.dumps(toast)
        sent_list = run_driver("read_gmail_list", fill(pages["sent_search_hit"], v))
        v["SENT_LIST"] = json.dumps(sent_list)
        sent_msg = run_driver("read_gmail_message", fill(pages["sent_message"], dict(v, BODY_TYPED=sent_body or body)))
        v["SENT_MESSAGE"] = json.dumps(sent_msg)
        self.drivers = {"toast": toast, "sent_list": sent_list, "sent_message": sent_msg, "compose": compose}
        readback = [m for m in sent_msg["messages"] if m["from_owner"]][-1]["readback_text"]
        v["READBACK"] = readback
        v["EVIDENCE"] = "Toast: %s\nSent: to %s, subject %s, %s\n\n%s" % (
            toast["toasts"][0], sent_list["rows"][0]["to"][0], sent_list["rows"][0]["subject"], v["SENT_TITLE"], readback)
        return v

    def guard_lines(self, token: str) -> list:
        path = os.path.join(paths.guard_dir(), token + ".jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def test_web_email_precheck_reserve_readback_click_sent_confirm(self):
        w = self.w
        made = self.approved_draft()
        v = self.replay_vars(made)
        r = Replay(self, w, self.start_guard(), self.rec["send"], v)
        r.run()
        token = r.vars["TOKEN"]
        self.assertEqual([o for _t, o in r.outcomes if o != "allow"],
                         ["G_NO_TOKEN", "G_NOT_ARMED", "G_NOT_ARMED", "G_NO_TOKEN"])
        self.assertTrue(all(code in ("OK", "NOTHING_TO_DO") for _a, _rc, code in r.jh), r.jh)
        self.assertEqual(r.vars["APPROVED"], made["send_text"])
        confirms = [argv for argv, _rc, _code in r.jh if argv[2:4] == ["gate", "confirm"]]
        self.assertEqual(len(confirms), 1)
        self.assertIn("--observed-file", confirms[0], "confirm checks the Sent-folder read-back in code")

        # the compose read-back hashed to the approved text; the click happened only after arm and dwell
        act = w.one("SELECT kind, route, platform, status, recipient, agent_id, thread_key, approved_sha256, "
                    "observed_sha256, armed_at, sent_at FROM actions WHERE token = ?", token)
        self.assertEqual(tuple(act)[:7], ("cold_email", "browser", "gmail", "sent", RECIPIENT, OU, "em:" + token))
        self.assertEqual(act["observed_sha256"], act["approved_sha256"])
        self.assertEqual(act["approved_sha256"], canon.sha256_text(canon.normalize_text(made["send_text"])))
        self.assertTrue(act["armed_at"] < act["sent_at"], tuple(act))
        self.assertTrue(w.slept, "pace wait --kind dwell drew a dwell")
        lines = self.guard_lines(token)
        fills = [(x["action"], x["name"]) for x in lines if x["class"] == "fill"]
        self.assertEqual(fills, [("click", "Compose"), ("type", "To recipients"), ("type", "Subject"),
                                 ("type", "Message Body")])
        self.assertEqual([(x["action"], x["name"]) for x in lines if x["class"] == "commit"], [("click", "Send")])
        self.assertEqual({(x["agent"], x["host"]) for x in lines}, {(OU, "mail.google.com")})

        # the Sent folder holds exactly the approved text (subject and body with the signature)
        readback = [m for m in self.drivers["sent_message"]["messages"] if m["from_owner"]][-1]["readback_text"]
        rb = gate.email_readback(readback)
        self.assertEqual(canon.canonical_send_text("cold_email", rb["subject"], rb["body"], None, None),
                         canon.normalize_text(made["send_text"]))
        # the compose window and the Sent copy name the reserved recipient alone (one To, no Cc or Bcc)
        self.assertEqual((rb["to"], rb["cc"], rb["bcc"]), (RECIPIENT, None, None), readback)
        seen = gate.email_readback(self.drivers["compose"]["observed_text"])
        self.assertEqual((seen["to"], seen["cc"], seen["bcc"]), (RECIPIENT, None, None), seen)
        self.assertEqual(self.drivers["sent_list"]["count"], 1)
        self.assertTrue(self.drivers["toast"]["success_phrases"], self.drivers["toast"])

        # confirm created the thread with the conversation URL and a follow-up date; the draft is sent
        th = w.one("SELECT state, followup_due_at, platform_ref, contact_id FROM threads WHERE thread_key = ?",
                   "em:" + token)
        self.assertIsNotNone(th, "no thread for the confirmed web email")
        self.assertEqual((th["state"], th["platform_ref"], th["contact_id"]),
                         ("open", v["URL_THREAD"], made["contact_id"]))
        self.assertTrue(th["followup_due_at"] and th["followup_due_at"] > w.clock.now())
        self.assertEqual(w.one("SELECT status FROM drafts WHERE draft_uid = ?", made["draft_uid"])[0], "sent")
        self.assertEqual([k for k, _v in sheets_rows.build_rows(w.conn, "outreach", None)], [token])
        self.assertEqual(w.one("SELECT count(*) FROM actions WHERE route = 'mailer'")[0], 0)
        # the guard log holds its decisions, never the message text
        log_path = os.path.join(paths.logs_dir(), "guard-%s.jsonl" % canon.now()[:7])
        with open(log_path, "r", encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn("pincode-level models beat", log)

    def test_changed_readback_is_refused_and_nothing_is_sent(self):
        w = self.w
        made = self.approved_draft()
        body = made["send_text"].partition("\n\n")[2]
        changed = body.replace("from 18% to 13%", "from 18% to 12%")
        self.assertNotEqual(changed, body)
        v = self.replay_vars(made, body_on_page=changed)
        r = Replay(self, w, self.start_guard(), self.rec["mismatch"], v)
        r.run()
        token = r.vars["TOKEN"]
        self.assertEqual([o for _t, o in r.outcomes if o != "allow"], ["G_NO_TOKEN", "G_NOT_ARMED", "G_NO_TOKEN"])
        self.assertEqual([code for argv, _rc, code in r.jh if argv[2:4] == ["gate", "arm"]], ["E_OBSERVED_MISMATCH"])
        act = w.one("SELECT status, fail_reason, observed_sha256 != approved_sha256 FROM actions WHERE token = ?", token)
        self.assertEqual(tuple(act), ("failed", "observed_text_mismatch", 1))
        self.assertEqual([x for x in self.guard_lines(token) if x["class"] == "commit"], [])
        self.assertIsNone(w.one("SELECT 1 FROM threads WHERE thread_key = ?", "em:" + token))
        self.assertEqual(w.one("SELECT count(*) FROM actions WHERE status IN ('sent', 'armed')")[0], 0)

    def test_changed_sent_copy_makes_the_token_unknown_never_sent(self):
        w = self.w
        made = self.approved_draft()
        body = made["send_text"].partition("\n\n")[2]
        changed = body.replace("from 18% to 13%", "from 18% to 12%")
        self.assertNotEqual(changed, body)
        v = self.replay_vars(made, sent_body=changed)
        self.assertEqual(v["OBSERVED"].partition("\n\n")[2].rstrip(), body.rstrip(), "compose holds the approved text")
        r = Replay(self, w, self.start_guard(), self.rec["sent_changed"], v)
        r.run()
        token = r.vars["TOKEN"]
        self.assertEqual([o for _t, o in r.outcomes if o != "allow"],
                         ["G_NO_TOKEN", "G_NOT_ARMED", "G_NOT_ARMED", "G_NO_TOKEN"])
        codes = [(argv[2:4], rc, code) for argv, rc, code in r.jh if argv[2:3] == ["gate"]]
        self.assertIn((["gate", "arm"], 0, "OK"), codes)
        self.assertEqual([(rc, code) for a, rc, code in codes if a == ["gate", "confirm"]],
                         [(6, "E_OBSERVED_MISMATCH"), (11, "E_BAD_TRANSITION")])

        # the one click happened, the Sent copy is not the approved text: unknown, never sent, reconcile decides
        act = w.one("SELECT id, status, sent_at, note, observed_sha256 = approved_sha256 FROM actions WHERE token = ?",
                    token)
        self.assertEqual((act["status"], act["sent_at"], act[4]), ("unknown", None, 1))
        self.assertTrue(act["note"].startswith("sent_readback_mismatch"), act["note"])
        self.assertEqual([(x["action"], x["name"]) for x in self.guard_lines(token) if x["class"] == "commit"],
                         [("click", "Send")])
        task = w.one("SELECT kind, done_at FROM human_tasks WHERE action_id = ?", act["id"])
        self.assertEqual(tuple(task), ("resolve_unknown", None))
        self.assertEqual([t["method"] for t in r.vars["RECON"] if t["token"] == token],
                         ["web_sent_search", "web_outbox_search", "web_scheduled_search"])
        self.assertIsNone(w.one("SELECT 1 FROM threads WHERE thread_key = ?", "em:" + token))
        self.assertEqual(w.one("SELECT status FROM drafts WHERE draft_uid = ?", made["draft_uid"])[0], "approved")
        self.assertEqual(w.one("SELECT count(*) FROM actions WHERE status = 'sent'")[0], 0)
        ev_path = os.path.join(paths.logs_dir(), "events-%s.jsonl" % canon.now()[:7])
        with open(ev_path, "r", encoding="utf-8") as fh:
            events = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual([e["token"] for e in events if e["kind"] == "confirm_readback_mismatch"], [token])


class TestWebEmailConfirmCommands(unittest.TestCase):
    """The same web-route confirm through the jh.py commands alone (no guard, no node): the steps of skill
    jobhunter-gmail-web section 3 as tests.fixtures.e2e.support.OutreachSteps.web_send runs them."""

    def setUp(self):
        self.w = WebWorld()
        self.addCleanup(self.w.stop)
        install_qc_fakes(self)
        self.w.onboard()
        install_reviewer_hashes(self.w.conn)

    def approved(self, n: int, first: str, last: str, company: str, domain: str) -> dict:
        w = self.w
        cyc = w.preflight("outreach", OU)
        addr = "%s.%s@%s" % (first.lower(), last.lower(), domain)
        c = w.add_contact(cyc, full_name="%s %s" % (first, last), first_name=first, company=company,
                          company_domain=domain, email=addr, email_grade="A",
                          email_evidence_url="https://%s/team" % domain, source_url="https://%s/team" % domain)
        w.verify_email(cyc, addr, "A", "Team page: https://%s/team" % domain)
        d = w.cold_email_draft(cyc, c["contact_uid"], first, company, n=n)
        w.end_cycle(cyc, OU)
        self.assertEqual([a["draft_uid"] for a in w.approve_all()], [d])
        return {"contact_uid": c["contact_uid"], "draft_uid": d, "address": addr}

    def test_confirm_with_the_sent_readback(self):
        w = self.w
        first = self.approved(1, "Jordan", "Blake", "Kestrel Commerce", "kestrel.example")
        second = self.approved(2, "Casey", "Morgan", "Heron Freight", "heron.example")
        third = self.approved(3, "Riley", "Quinn", "Osprey Labs", "osprey.example")
        w.clock.advance(minutes=30)
        write_heartbeat()                                # the guard keeps beating while time passes
        cyc = w.preflight("outreach", OU)

        # the Sent copy is the approved text: sent, thread made
        rc, env = w.web_reserve(cyc, first["draft_uid"], first["contact_uid"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        tok1 = env["data"]["token"]
        sent = w.web_send(cyc, tok1, first["draft_uid"])
        self.assertEqual(sent["confirm"]["status"], "sent")
        self.assertEqual(w.one("SELECT state FROM threads WHERE thread_key = ?", "em:" + tok1)[0], "open")
        confirms = [argv for _who, argv, _rc, _code in w.calls if "confirm" in argv]
        self.assertIn("--observed-file", confirms[-1], "confirm carries the Sent read-back, as the skill runs it")
        with open(confirms[-1][confirms[-1].index("--observed-file") + 1], "r", encoding="utf-8") as fh:
            rb = gate.email_readback(fh.read())
        self.assertEqual((rb["to"], rb["cc"], rb["bcc"]), (first["address"], None, None), rb)

        # a Sent copy that is not the approved text: exit 6, the token is unknown, never sent, no thread
        w.clock.advance(minutes=30)
        write_heartbeat()
        rc, env = w.web_reserve(cyc, second["draft_uid"], second["contact_uid"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        tok2 = env["data"]["token"]
        text = w.ok(["draft", "show", second["draft_uid"], "--field", "send_text"], OU, cycle=cyc)["value"]
        changed = text.rstrip() + "\n\nSent from my phone"
        out = w.web_send(cyc, tok2, second["draft_uid"], readback=changed)
        self.assertEqual((out["confirm"]["rc"], out["confirm"]["code"]), (6, "E_OBSERVED_MISMATCH"), out["confirm"])
        act = w.one("SELECT id, status, sent_at FROM actions WHERE token = ?", tok2)
        self.assertEqual((act["status"], act["sent_at"]), ("unknown", None))
        self.assertEqual(w.one("SELECT kind FROM human_tasks WHERE action_id = ?", act["id"])[0], "resolve_unknown")
        self.assertIsNone(w.one("SELECT 1 FROM threads WHERE thread_key = ?", "em:" + tok2))
        self.assertEqual(w.one("SELECT status FROM drafts WHERE draft_uid = ?", second["draft_uid"])[0], "approved")
        # the skill's rule "never send again": a second confirm, even with the approved text, is refused
        rb = w.wfile("outreach", "%s/readback-again.txt" % cyc, w.with_to(text, second["address"]))
        ev = w.wfile("outreach", "%s/evidence-again.txt" % cyc, "Toast: Message sent")
        rc, env = w.run(["gate", "confirm", tok2, "--evidence-file", ev, "--observed-file", rb], OU, cycle=cyc)
        self.assertEqual((rc, env["code"]), (11, "E_BAD_TRANSITION"), env)
        self.assertEqual(w.one("SELECT status FROM actions WHERE token = ?", tok2)[0], "unknown")

        w.end_cycle(cyc, OU)

        # the approved text, but the Sent copy also went to a Cc: exit 6, unknown, never sent, no address echoed
        # (a new cycle: the cycle cap of two Gmail sends is used)
        w.clock.advance(minutes=30)
        write_heartbeat()
        cyc = w.preflight("outreach", OU)
        rc, env = w.web_reserve(cyc, third["draft_uid"], third["contact_uid"])
        self.assertEqual((rc, env["code"]), (0, "OK"), env)
        tok3 = env["data"]["token"]
        text = w.ok(["draft", "show", third["draft_uid"], "--field", "send_text"], OU, cycle=cyc)["value"]
        head, _s, body = text.partition("\n\n")
        copied = "%s\nTo: %s\nCc: someone.else@osprey.example\n\n%s" % (head, third["address"], body)
        out = w.web_send(cyc, tok3, third["draft_uid"], readback=copied)
        self.assertEqual((out["confirm"]["rc"], out["confirm"]["code"]), (6, "E_OBSERVED_MISMATCH"), out["confirm"])
        self.assertEqual(out["confirm"]["data"]["mismatch"]["recipient"], "cc_or_bcc", out["confirm"])
        self.assertNotIn("someone.else", json.dumps(out["confirm"]))
        act = w.one("SELECT id, status, sent_at FROM actions WHERE token = ?", tok3)
        self.assertEqual((act["status"], act["sent_at"]), ("unknown", None))
        self.assertEqual(w.one("SELECT kind FROM human_tasks WHERE action_id = ?", act["id"])[0], "resolve_unknown")
        self.assertIsNone(w.one("SELECT 1 FROM threads WHERE thread_key = ?", "em:" + tok3))
        w.end_cycle(cyc, OU)


if __name__ == "__main__":
    unittest.main()
