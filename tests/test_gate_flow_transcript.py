"""U6 browser gate flow from recorded fake transcripts (tests/fixtures/browser/*.json).

1. A checker applies the rules the gate and stop skills teach (and the guard enforces, 1.4 R2 to R5) to every
   recorded tool call: exec shape and ACL argument classes, profile jobhunter, the browser call shapes the
   guard knows (openclaw/plugins/jobhunter-guard/src/browser.ts classifyBrowserCall: act kinds, upload, refs
   from the latest snapshot of the same tab), drivers from the manifest passed as their exact text, fill only
   under a reserved token for the platform of the page's host, commit only after arm and dwell (at most 2),
   type with slowly, uploads only of the staged file, confirm only after a success toast, nothing but detect
   and cycle end after a stop.
2. A replay runs the recorded jh.py calls: U6 commands through the real CLI (agent caller), U1 gate calls
   through the fake gate in tests/fakes/u6 (which calls the real hooks.on_confirm -> threads.on_confirm),
   other units' commands as no-ops. The ledger and threads must end in the documented state.
3. Mutated transcripts (click before arm, no dwell, fast typing, unknown script, shell tricks, a click after a
   stop, a human-only command, shapes the guard does not know, stale refs, a company-domain form) must each be
   flagged.

In the fixtures "{DRIVER:<name>}" stands for the exact text of drivers/<name>.js (the replay puts the file text
in), so a regenerated driver never makes a transcript stale.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import unittest

import tests  # noqa: F401
from jobhunter import canon, cli, db, paths
from tests.fakes.u6 import U6TestCase, deps
from tests.helpers import insert_company, insert_contact, insert_job

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "browser")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:@+=,%-]+$")
DRIVER_RE = re.compile(r"^\{DRIVER:([a-z_]+)\}$")
# mirrors of openclaw/plugins/jobhunter-guard/src/browser.ts (U8); the name patterns come from guard-hosts.json
READ_ACTIONS = ("doctor", "status", "start", "stop", "profiles", "tabs", "open", "focus", "close", "snapshot",
                "screenshot", "navigate", "console", "requests", "errors", "text", "emulate", "pdf", "waitfordownload")
READ_ACT_KINDS = ("hover", "scrollIntoView", "resize", "close")
FILL_ROLES = ("checkbox", "radio", "combobox", "option", "textbox", "tab", "searchbox", "spinbutton")
READ_KEYS = ("pagedown", "pageup", "arrowdown", "arrowup", "arrowleft", "arrowright", "home", "end", "escape", "esc")
ROLE_LINE_RE = re.compile(r'^[ \t]*-[ \t]+([A-Za-z][\w-]*)(?:[ \t]+("(?:[^"\\\n]|\\.)*"))?[^\n]*?\[ref=([A-Za-z0-9_]+)\]',
                          re.M)
UNKNOWN = ("unknown browser action", "unknown act kind")


def load(name: str) -> dict:
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def manifest() -> dict:
    with open(os.path.join(paths.REPO, "drivers", "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def driver_text(name: str) -> str:
    with open(os.path.join(paths.REPO, "drivers", name + ".js"), encoding="utf-8") as fh:
        return fh.read()


def guard_hosts() -> dict:
    with open(os.path.join(paths.REPO, "openclaw", "guard-hosts.json"), encoding="utf-8") as fh:
        return json.load(fh)


def snapshot_refs(result) -> dict:
    """ref -> (role, name) from a snapshot result (the guard's parseSnapshotRefs: role lines and JSON nodes)."""
    out = {}
    if isinstance(result, str):
        for m in ROLE_LINE_RE.finditer(result):
            try:
                name = json.loads(m.group(2)) if m.group(2) else ""
            except ValueError:
                name = m.group(2)[1:-1]
            out[m.group(3)] = (m.group(1).lower(), name)
        return out

    def walk(v):
        if isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            if isinstance(v.get("ref"), str) and isinstance(v.get("role"), str):
                out[v["ref"]] = (v["role"].lower(), v.get("name") if isinstance(v.get("name"), str) else "")
            for x in v.values():
                walk(x)
    walk(result)
    return out


def host_of(url: str | None) -> str | None:
    m = re.match(r"^https?://([^/:?#]+)", url or "")
    return m.group(1).lower().rstrip(".") if m else None


# ---------------------------------------------------------------- the checker
class Checker:
    def __init__(self, agent: str, ws: str, py: str, repo: str):
        with open(paths.ACL_FILE, encoding="utf-8") as fh:
            self.acl = json.load(fh)
        self.agent = agent
        self.cmds = self.acl["agents"][agent]["commands"]
        self.classes = self.acl["value_classes"]
        self.ws = ws
        self.py = py
        self.repo = repo
        self.drivers = {h: name for name, h in manifest().items()}
        gh = guard_hosts()
        self.platforms = gh["platforms"]
        self.harmless = [re.compile(p, re.I) for p in gh["harmless_names"]]
        self.risky = re.compile(gh["risky_names"], re.I)
        self.prepare = {k: re.compile(p, re.I) for k, p in gh["prepare_names"].items()}

    def parse_exec(self, command: str):
        """(command name, args dict) or raise ValueError with the reason."""
        toks = command.split(" ")
        if any(not TOKEN_RE.match(t) for t in toks):
            raise ValueError("unsafe token in %r" % command)
        if toks[:2] != [self.py, self.repo + "/scripts/jh.py"]:
            raise ValueError("not a jh.py call")
        rest = toks[2:]
        if rest[:1] == ["--cycle"]:
            if len(rest) < 2 or not re.match(self.classes["cycle"], rest[1]):
                raise ValueError("bad --cycle")
            rest = rest[2:]
        if "--quiet" in rest:
            rest.remove("--quiet")
        for bad in ("--home", "--pin-stdin", "--grant"):
            if bad in rest:
                raise ValueError("forbidden flag %s" % bad)
        name = None
        for n in (3, 2, 1):
            cand = " ".join(rest[:n])
            if len(rest) >= n and cand in self.cmds:
                name, rest = cand, rest[n:]
                break
        if name is None:
            raise ValueError("command not in the %s ACL: %r" % (self.agent, " ".join(rest[:2])))
        spec = self.cmds[name]
        args, pos, i = {}, 0, 0
        while i < len(rest):
            t = rest[i]
            if t.startswith("--"):
                if t not in spec:
                    raise ValueError("flag %s not allowed for %s" % (t, name))
                if spec[t].rstrip("?") == "flag":
                    args[t] = True
                    i += 1
                    continue
                if i + 1 >= len(rest):
                    raise ValueError("flag %s needs a value" % t)
                args[t] = rest[i + 1]
                i += 2
            else:
                pos += 1
                args["_%d" % pos] = t
                i += 1
        for flag, cls in spec.items():
            opt = cls.endswith("?")
            base = cls.rstrip("?")
            if flag not in args:
                if not opt:
                    raise ValueError("%s needs %s" % (name, flag))
                continue
            val = args[flag]
            if base.startswith("enum:"):
                ok = val in base[5:].split("|")
            elif base == "flag":
                ok = val is True
            elif base == "path_work":
                real = os.path.realpath(val)
                ok = any(real.startswith(os.path.realpath(os.path.join(self.ws, sub)) + os.sep)
                         for sub in ("work", "inbox"))
            else:
                ok = bool(re.match(self.classes[base], str(val)))
            if not ok:
                raise ValueError("%s %s=%r fails class %s" % (name, flag, val, cls))
        extra = [k for k in args if k not in spec]
        if extra:
            raise ValueError("unexpected arguments %s" % extra)
        return name, args

    # ---------------------------------------------------------- browser call classes (guard mirror)
    def platform_of(self, url: str | None):
        host = host_of(url)
        if not host:
            return None
        for key, p in self.platforms.items():
            if any(host == d or host.endswith("." + d) for d in p["hosts"]):
                return dict(p, key=key)
        return None

    def token_covers(self, token_platform: str, url: str | None) -> bool:
        p = self.platform_of(url)
        if p is None:
            return False
        return token_platform == p["key"] or token_platform in p.get("aliases", []) or \
            (p["scope"].startswith("site:") and token_platform == p["scope"])

    def classify_click(self, ref, refs: dict, kind: str | None) -> dict:
        if not ref:
            return {"cls": "commit", "why": "click without a snapshot ref"}
        if ref not in refs:
            return {"cls": "commit", "why": "ref not in the last snapshot", "ref": ref}
        role, name = refs[ref]
        name = (name or "").strip()
        prep = self.prepare.get(kind) if kind else None
        if role == "link":
            if prep and prep.search(name):
                return {"cls": "fill", "why": "preparatory link"}
            if self.risky.search(name):
                return {"cls": "commit", "why": "link with an action name"}
            return {"cls": "nav_click", "why": "link"}
        if role in FILL_ROLES:
            return {"cls": "fill", "why": "form control"}
        if role == "button":
            if prep and prep.search(name):
                return {"cls": "fill", "why": "preparatory button"}
            if any(h.search(name) for h in self.harmless) and not self.risky.search(name):
                return {"cls": "nav_click", "why": "harmless button"}
        return {"cls": "commit", "why": "%s click" % role}

    def classify_act(self, req: dict, refs: dict, kind: str | None, depth: int) -> list:
        k = req.get("kind") if isinstance(req.get("kind"), str) else ""
        if k == "click":
            return [dict(self.classify_click(req.get("ref"), refs, kind), action="click")]
        if k == "clickCoords":
            return [{"cls": "commit", "action": "click", "why": "coordinate click"}]
        if k == "type":
            if req.get("submit") is True:
                return [{"cls": "commit", "action": "type", "why": "type with submit"}]
            return [{"cls": "fill", "action": "type", "why": "type", "slowly": req.get("slowly")}]
        if k == "press":
            key = str(req.get("key") or "").lower()
            if re.search(r"(^|\+)(enter|return)$", key):
                return [{"cls": "commit", "action": "press", "why": "Enter key"}]
            return [{"cls": "read" if key in READ_KEYS else "fill", "action": "press", "why": "key"}]
        if k in ("select", "fill"):
            return [{"cls": "fill", "action": k, "why": k}]
        if k == "drag":
            return [{"cls": "commit", "action": "drag", "why": "drag"}]
        if k in ("wait", "evaluate"):
            fn = req.get("fn")
            if k == "wait" and fn in (None, ""):
                return [{"cls": "read", "action": "wait", "why": "wait"}]
            name = self.drivers.get(hashlib.sha256(fn.strip().encode("utf-8")).hexdigest()) \
                if isinstance(fn, str) and fn else None
            if name is None:
                return [{"cls": "script_denied", "action": k, "why": "script is not an allowlisted driver"}]
            return [{"cls": "driver", "action": k, "why": "allowlisted driver", "driver": name}]
        if k == "batch":
            if depth > 2 or not isinstance(req.get("actions"), list):
                return [{"cls": "commit", "action": "batch", "why": "malformed batch"}]
            out = []
            for a in req["actions"]:
                if not isinstance(a, dict):
                    return [{"cls": "commit", "action": "batch", "why": "malformed batch item"}]
                out.extend(self.classify_act(a, refs, kind, depth + 1))
            return out or [{"cls": "read", "action": "batch", "why": "empty batch"}]
        if k in READ_ACT_KINDS:
            return [{"cls": "read", "action": k, "why": k}]
        return [{"cls": "commit", "action": k or "act", "why": "unknown act kind"}]

    def classify_call(self, p: dict, refs: dict, kind: str | None) -> list:
        action = p.get("action") if isinstance(p.get("action"), str) else ""
        if action in READ_ACTIONS:
            return [{"cls": "read", "action": action, "why": action}]
        if action == "upload":
            paths_ = [str(x) for x in p["paths"]] if isinstance(p.get("paths"), list) else []
            return [{"cls": "fill", "action": "upload", "why": "upload", "paths": paths_}]
        if action == "dialog":
            return [{"cls": "commit" if p.get("accept") is True else "nav_click", "action": "dialog", "why": "dialog"}]
        if action == "act":
            req = p["request"] if isinstance(p.get("request"), dict) else p
            return self.classify_act(req, refs, kind, 0)
        return [{"cls": "commit", "action": action or "unknown", "why": "unknown browser action"}]

    def check(self, steps: list, kind_of_token: str | None = None, staged: str | None = None) -> list[str]:
        v = []
        state, dwell, commits, kind, platform = None, False, 0, kind_of_token, None
        success_since_commit = False
        stopped = False
        tabs, last_tab = {}, "_"
        for n, st in enumerate(steps):
            tool, p = st["tool"], st.get("params", {})
            where = "step %d (%s)" % (n, tool)
            if stopped:
                ok = tool == "write" or (tool == "browser" and p.get("action") == "close")
                if tool == "exec":
                    try:
                        name, _ = self.parse_exec(p["command"])
                        ok = name in ("detect", "cycle end", "breaker trip")
                    except ValueError:
                        ok = False
                if not ok:
                    v.append("%s: action after a stop page" % where)
                continue
            if tool == "exec":
                if p.get("timeoutSeconds") != 90:
                    v.append("%s: timeoutSeconds must be 90" % where)
                try:
                    name, args = self.parse_exec(p["command"])
                except ValueError as exc:
                    v.append("%s: %s" % (where, exc))
                    continue
                if name == "gate reserve":
                    state, dwell, commits, kind, platform = "reserved", False, 0, args["--kind"], args["--platform"]
                elif name == "gate arm":
                    if state != "reserved":
                        v.append("%s: arm without a reserved token" % where)
                    state = "armed"
                elif name == "pace wait" and args.get("--kind") == "dwell":
                    if state == "armed":
                        dwell = True
                elif name == "gate confirm":
                    if state != "armed" or commits == 0:
                        v.append("%s: confirm without an armed token and one commit" % where)
                    if not success_since_commit:
                        v.append("%s: confirm without a success toast after the click" % where)
                    state = None
                elif name in ("gate fail", "gate unknown"):
                    state = None
            elif tool == "browser":
                if p.get("profile") != "jobhunter":
                    v.append("%s: browser profile must be jobhunter" % where)
                action = p.get("action")
                tab_id = p.get("targetId") or (p["request"].get("targetId") if isinstance(p.get("request"), dict)
                                                else None) or last_tab
                tab = tabs.setdefault(tab_id, {"refs": {}, "url": None})
                items = self.classify_call(p, tab["refs"], kind)
                for it in items:
                    if it["why"] in UNKNOWN:
                        v.append("%s: %r is not a browser call the guard knows (it counts as a submit)"
                                 % (where, it["action"]))
                    elif it["why"] == "ref not in the last snapshot":
                        v.append("%s: ref %s is not in the latest snapshot of this tab" % (where, it["ref"]))
                    elif it["cls"] == "script_denied":
                        v.append("%s: evaluate of a script that is not in drivers/manifest.json" % where)
                    elif it["cls"] == "driver":
                        res = st.get("result") or {}
                        if it["driver"] == "detect_page" and res.get("hint") == "stop":
                            stopped = True
                        if it["driver"] == "read_toast" and res.get("success_phrases") and commits:
                            success_since_commit = True
                    if it["cls"] not in ("fill", "commit"):
                        continue
                    if it["action"] == "type" and it.get("slowly") is not True:
                        v.append("%s: text must be typed with slowly" % where)
                    if it["action"] == "upload" and it["paths"] != [staged]:
                        v.append("%s: upload of a file that is not the staged resume" % where)
                    if state is None:
                        v.append("%s: %s action without a reserved token" % (where, it["cls"]))
                        continue
                    if not self.token_covers(platform, tab["url"]):
                        v.append("%s: %s on %s, a page the token for platform %s does not cover"
                                 % (where, it["cls"], host_of(tab["url"]) or "an unknown page", platform))
                    if it["cls"] == "commit":
                        if state != "armed":
                            v.append("%s: commit action before gate arm" % where)
                        elif not dwell:
                            v.append("%s: commit action before the dwell" % where)
                        commits += 1
                        success_since_commit = False
                        if commits > 2:
                            v.append("%s: more than 2 commit actions for one token" % where)
                # after the call: the guard's RefCache (runtime.ts observe)
                if action in ("navigate", "open"):
                    tab["url"] = p.get("targetUrl") or p.get("url")
                if action in ("snapshot", "navigate", "open", "act"):
                    refs = snapshot_refs(st.get("result")) if st.get("result") is not None else {}
                    if refs:
                        tab["refs"] = refs
                    elif action in ("navigate", "open"):
                        tab["refs"] = {}
                if action in ("snapshot", "navigate", "open"):
                    last_tab = tab_id
        last = steps[-1] if steps else {}
        if not (last.get("tool") == "exec" and " cycle end " in last["params"]["command"] + " "):
            v.append("the cycle must end with cycle end")
        return v


# ---------------------------------------------------------------- replay
class Replay:
    NOOP = ("preflight", "detect", "usage add", "usage gauge", "pace wait", "cycle end", "gate precheck-plan",
            "resume unstage", "lock renew")
    CLI = ("outreach next", "apply next", "thread list", "reply record", "followup due", "contact add",
           "research add", "reply pending", "outreach skip")

    def __init__(self, case, transcript: dict, values: dict):
        self.case = case
        self.t = transcript
        self.agent = transcript["agent"]
        self.role = paths.role_for_agent(self.agent)
        self.ws = paths.ws_dir(self.role)
        self.values = values
        self.last_result = None
        self.outputs = []
        self.env = {"OPENCLAW_SHELL": "1", "JH_AGENT_ID": self.agent}

    def sub(self, obj):
        if isinstance(obj, str):
            if obj == "{FIELDS}":
                return self.t["fields"]
            m = DRIVER_RE.match(obj)
            if m:
                return driver_text(m.group(1))
            for key in re.findall(r"\{([A-Z]+)\}", obj):
                val = self.values.get(key)
                if callable(val):
                    val = val()
                    self.values[key] = val
                if val is not None:
                    obj = obj.replace("{%s}" % key, str(val))
            return obj
        if isinstance(obj, list):
            return [self.sub(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.sub(x) for k, x in obj.items()}
        return obj

    def steps(self) -> list:
        return [self.sub(copy.deepcopy(s)) for s in self.t["steps"]]

    def run(self) -> list:
        conn = self.case.conn
        for raw in self.t["steps"]:
            st = self.sub(copy.deepcopy(raw))
            tool, p = st["tool"], st.get("params", {})
            if st.get("result") is not None:
                self.last_result = st["result"]
            if tool == "write":
                content = p.get("content")
                if "content_from" in p:
                    key = p["content_from"].split(".", 1)[1]
                    content = self.last_result[key]
                os.makedirs(os.path.dirname(p["path"]), exist_ok=True)
                with open(p["path"], "w", encoding="utf-8") as fh:
                    fh.write(content if isinstance(content, str) else json.dumps(content))
                continue
            if tool != "exec":
                continue
            toks = p["command"].split(" ")[2:]
            if toks[:1] == ["--cycle"]:
                toks = toks[2:]
            name = " ".join(toks[:2]) if " ".join(toks[:2]) in self.NOOP + self.CLI + (
                "gate precheck", "gate reserve", "gate arm", "gate confirm", "resume stage") else toks[0]
            args = dict(zip(toks[2::2], toks[3::2])) if name.count(" ") else {}
            if name in self.NOOP:
                continue
            if name in self.CLI:
                out = io.StringIO()
                rc = cli.main(p["command"].split(" ")[2:], env=self.env, stdin=io.StringIO(""), stdout=out)
                env = json.loads(out.getvalue())
                self.case.assertEqual(rc, 0, env)
                self.outputs.append((name, env))
                continue
            with db.tx(conn):
                if name == "gate precheck":
                    with open(args["--file"], encoding="utf-8") as fh:
                        ev = json.load(fh)
                    checks = {c["name"]: c["value"] for c in ev["checks"]}
                    clear = checks.get("profile_button") == "Connect" if ev["kind"] == "li_invite" else \
                        not any(checks.values())
                    cur = conn.execute(
                        "INSERT INTO prechecks (kind, platform, source, contact_id, job_id, result, checks_json, "
                        "created_at) VALUES (?, ?, 'agent', ?, ?, ?, ?, ?)",
                        (ev["kind"], ev["platform"], self.values.get("CONTACT_ID"), self.values.get("JOB_ID"),
                         "clear" if clear else "already_done", json.dumps(ev["checks"]), canon.now()))
                    self.values["PRECHECK"] = cur.lastrowid
                elif name == "gate reserve":
                    draft_id = conn.execute("SELECT id FROM drafts WHERE draft_uid = ?", (args["--draft"],)).fetchone()[0]
                    res = deps.gate_reserve(conn, kind=args["--kind"], draft_id=draft_id,
                                            precheck_id=int(args["--precheck"]), platform=args["--platform"],
                                            agent_id=self.agent, route="browser", job_id=self.values.get("JOB_ID"),
                                            contact_id=self.values.get("CONTACT_ID"),
                                            li_note=1 if args["--kind"] == "li_invite" else 0)
                    self.values["TOKEN"] = res["token"]
                elif name == "resume stage":
                    self.values["UPLOAD"] = "/tmp/openclaw/uploads/Alex_Rivera_Resume.pdf"
                elif name == "gate arm":
                    token = toks[2]
                    with open(args.get("--observed-file") or toks[4], encoding="utf-8") as fh:
                        ok = deps.gate_arm(conn, token, fh.read())
                    self.case.assertTrue(ok, "observed text differs from the approved text")
                elif name == "gate confirm":
                    self.outputs.append((name, deps.gate_confirm(conn, toks[2], "recorded evidence")))
        return self.outputs


class TestTranscripts(U6TestCase):
    def setUp(self):
        super().setUp()
        self.co = insert_company(self.conn, name="Kestrel Commerce", domain="kestrel.invalid")
        self.cycle = "C20260928T100000ZAAAA"

    def checker(self, agent: str) -> Checker:
        return Checker(agent, paths.ws_dir(paths.role_for_agent(agent)), "/usr/bin/python3", paths.REPO)

    def base_values(self, transcript: dict) -> dict:
        return {"PY": "/usr/bin/python3", "REPO": paths.REPO, "CYCLE": self.cycle, "NOW": canon.now(),
                "WS": paths.ws_dir(paths.role_for_agent(transcript["agent"]))}

    # ---------------------------------------------------------- LinkedIn invitation
    def li_setup(self, t: dict) -> dict:
        self.enable_linkedin()
        ct = insert_contact(self.conn, company_id=self.co, full_name="Alex Rivera", email=None,
                            linkedin_url="https://www.linkedin.com/in/example-alex-rivera")
        self.conn.execute("UPDATE contacts SET li_slug = 'example-alex-rivera' WHERE id = ?", (ct,))
        job = insert_job(self.conn, company_id=self.co, status="eligible")
        self.conn.execute("INSERT INTO job_hiring_team (job_id, contact_id, relation) VALUES (?, ?, 'hiring_manager')",
                          (job, ct))
        note = t["note"]

        def make_draft():
            uid = canon.new_uid("D")
            stamp = canon.now()
            text = canon.canonical_send_text("li_invite_note", None, note, None, None)
            self.conn.execute(
                "INSERT INTO drafts (draft_uid, kind, channel, send_route, contact_id, company_id, job_id, recipient, "
                "body, payload_json, text_sha256, status, approved_by, approved_at, created_at, updated_at) VALUES "
                "(?, 'li_invite_note', 'li_connect', 'browser', ?, ?, ?, 'example-alex-rivera', ?, '{}', ?, 'approved', "
                "'human:chat', ?, ?, ?)", (uid, ct, self.co, job, note, canon.sha256_text(text), stamp, stamp, stamp))
            return uid
        v = self.base_values(t)
        v.update({"CONTACT": self.uid("contacts", ct), "CONTACT_ID": ct, "NOTE": note, "DRAFT": make_draft})
        return v

    def test_li_invite_transcript_is_clean_and_replays(self):
        t = load("li_invite_cycle.json")
        values = self.li_setup(t)
        rp = Replay(self, t, values)
        outputs = rp.run()
        targets = dict(outputs)["outreach next"]["data"]["targets"]
        self.assertEqual(targets[0]["contact_uid"], values["CONTACT"])
        self.assertEqual(targets[0]["route"], "linkedin")
        self.assertEqual(self.checker(t["agent"]).check(rp.steps(), staged=None), [])
        a = self.row("SELECT * FROM actions WHERE token = ?", values["TOKEN"])
        self.assertEqual((a["status"], a["kind"], a["li_note"], a["li_msg_seq"], a["first_touch"]),
                         ("sent", "li_invite", 1, 1, 1))
        key = "li:" + values["CONTACT"]
        t_row = self.row("SELECT * FROM threads WHERE thread_key = ?", key)
        self.assertEqual((t_row["state"], t_row["first_action_id"]), ("invite_pending", a["id"]))
        self.assertEqual(self.row("SELECT status FROM drafts WHERE id = ?", a["draft_id"])[0], "sent")
        self.assertEqual(self.row("SELECT contact_state FROM companies WHERE id = ?", self.co)[0], "contacted")
        self.assertEqual(dict(outputs)["gate confirm"]["thread_key"], key)

        # replies lane: acceptance recorded, then the post-accept message becomes an outreach target
        t2 = load("replies_accept_cycle.json")
        values2 = self.base_values(t2)
        values2["THREAD"] = key
        rp2 = Replay(self, t2, values2)
        out2 = dict(rp2.run())
        self.assertEqual([x["thread_key"] for x in out2["thread list"]["data"]["threads"]], [key])
        self.assertEqual(self.checker(t2["agent"]).check(rp2.steps()), [])
        self.assertEqual(self.row("SELECT state FROM threads WHERE thread_key = ?", key)[0], "invite_accepted")
        self.clock.advance(days=3, minutes=5)
        out = io.StringIO()
        cli.main(["outreach", "next"], env=rp.env, stdin=io.StringIO(""), stdout=out)
        targets = json.loads(out.getvalue())["data"]["targets"]
        self.assertEqual([(x["kind"], x["thread_key"]) for x in targets], [("post_accept_message", key)])

    def test_observed_mismatch_is_caught(self):
        t = load("li_invite_cycle.json")
        values = self.li_setup(t)
        for st in t["steps"]:
            if isinstance(st.get("result"), dict) and st["result"].get("note_text") == "{NOTE}":
                st["result"]["note_text"] = "{NOTE} Also, can you refer me?"
        with self.assertRaises(AssertionError):
            Replay(self, t, values).run()

    # ---------------------------------------------------------- ATS application
    def test_ats_transcript_is_clean_and_replays(self):
        t = load("ats_apply_cycle.json")
        job = insert_job(self.conn, company_id=self.co, status="apply_queued", source="greenhouse",
                         url="https://job-boards.greenhouse.io/example/jobs/1")
        self.conn.execute("UPDATE jobs SET apply_route = 'ats_form' WHERE id = ?", (job,))
        pkg = {"job_uid": self.uid("jobs", job), "resume_variant_uid": canon.new_uid("V"), "fields": t["fields"],
               "resume": t["resume"]}
        text = canon.canonical_send_text("application_package", None, None, t["fields"], None, {"filename": t["resume"]})
        uid = canon.new_uid("D")
        stamp = canon.now()
        self.conn.execute(
            "INSERT INTO drafts (draft_uid, kind, channel, send_route, job_id, company_id, payload_json, text_sha256, "
            "status, approved_by, approved_at, created_at, updated_at) VALUES (?, 'application_package', 'form', "
            "'browser', ?, ?, ?, ?, 'approved', 'human:cli', ?, ?, ?)",
            (uid, job, self.co, json.dumps(pkg), canon.sha256_text(text), stamp, stamp, stamp))
        values = self.base_values(t)
        values.update({"JOB": self.uid("jobs", job), "JOB_ID": job, "DRAFT": uid, "VARIANT": pkg["resume_variant_uid"]})
        rp = Replay(self, t, values)
        outputs = dict(rp.run())
        items = outputs["apply next"]["data"]["items"]
        self.assertEqual([(i["job_uid"], i["needs"], i["package_draft"]) for i in items], [(values["JOB"], "submit", uid)])
        self.assertEqual(self.checker(t["agent"]).check(rp.steps(), staged=values["UPLOAD"]), [])
        a = self.row("SELECT * FROM actions WHERE token = ?", values["TOKEN"])
        self.assertEqual((a["status"], a["kind"], a["first_touch"]), ("sent", "application", 0))
        self.assertIsNone(outputs["gate confirm"]["thread_key"], "a form application opens no thread")
        self.assertEqual(self.row("SELECT count(*) FROM threads")[0], 0)

    # ---------------------------------------------------------- stop page
    def test_stop_transcript(self):
        t = load("stop_page_cycle.json")
        rp = Replay(self, t, self.base_values(t))
        self.assertEqual(self.checker(t["agent"]).check(rp.steps()), [])
        bad = copy.deepcopy(t)
        bad["steps"].insert(5, {"tool": "browser", "params": {"action": "navigate", "profile": "jobhunter",
                                                             "targetUrl": "https://www.linkedin.com/feed/"}})
        v = self.checker(t["agent"]).check(Replay(self, bad, self.base_values(t)).steps())
        self.assertTrue(any("after a stop page" in x for x in v), v)

    # ---------------------------------------------------------- recorded shapes
    def test_every_recorded_browser_call_has_a_shape_the_guard_knows(self):
        kinds = {"click", "type", "press", "select", "fill", "drag", "wait", "evaluate", "batch", "hover"}
        for name in sorted(f for f in os.listdir(FIX) if f.endswith("_cycle.json")):   # transcripts, not page fixtures
            for st in load(name)["steps"]:
                if st["tool"] != "browser":
                    continue
                p = st["params"]
                with self.subTest(file=name, params=p):
                    self.assertIn(p["action"], ("navigate", "snapshot", "act", "upload", "close"))
                    if p["action"] == "act":
                        self.assertIn(p["kind"], kinds)
                        self.assertNotIn("role", p, "role and name come from the snapshot, not from the call")
                    if p.get("kind") == "evaluate":
                        m = DRIVER_RE.match(p["fn"])
                        self.assertIsNotNone(m, "drivers are passed as their exact file text")
                        text = driver_text(m.group(1))
                        self.assertEqual(hashlib.sha256(text.strip().encode("utf-8")).hexdigest(),
                                         manifest()[m.group(1)])
                    if p["action"] == "upload":
                        self.assertEqual(p["paths"], ["{UPLOAD}"])
                        self.assertTrue(p.get("ref") or p.get("inputRef"))

    # ---------------------------------------------------------- the checker catches violations
    def mutated(self, fn) -> list:
        t = load("li_invite_cycle.json")
        values = self.li_setup(t)
        values.update({"DRAFT": "DAAAAAAA", "TOKEN": "TAAAAAAAAAAA", "PRECHECK": 1})
        fn(t["steps"])
        return self.checker(t["agent"]).check(Replay(self, t, values).steps())

    def mutated_ats(self, fn) -> list:
        t = load("ats_apply_cycle.json")
        values = self.base_values(t)
        upload = "/tmp/openclaw/uploads/Alex_Rivera_Resume.pdf"
        values.update({"JOB": "JAAAAAAA", "DRAFT": "DAAAAAAA", "PRECHECK": 1, "VARIANT": "VAAAAAAA",
                       "TOKEN": "TAAAAAAAAAAA", "UPLOAD": upload})
        fn(t["steps"])
        return self.checker(t["agent"]).check(Replay(self, t, values).steps(), staged=upload)

    @staticmethod
    def index(steps, pred):
        return next(i for i, s in enumerate(steps) if pred(s))

    @staticmethod
    def act(kind, ref=None):
        return lambda s: s["params"].get("action") == "act" and s["params"].get("kind") == kind and \
            (ref is None or s["params"].get("ref") == ref)

    @staticmethod
    def driver(name):
        return lambda s: s["params"].get("fn") == "{DRIVER:%s}" % name

    def test_violations_are_flagged(self):
        send = self.act("click", "e47")
        arm = lambda s: " gate arm " in s["params"].get("command", "")  # noqa: E731
        snapshot = lambda s: s["params"].get("action") == "snapshot"  # noqa: E731

        def click_before_arm(steps):
            steps.insert(self.index(steps, arm), steps.pop(self.index(steps, send)))

        def no_dwell(steps):
            steps.pop(self.index(steps, lambda s: "--kind dwell" in s["params"].get("command", "")))

        def fast_typing(steps):
            steps[self.index(steps, self.act("type"))]["params"]["slowly"] = False

        def unknown_script(steps):
            steps.insert(3, {"tool": "browser", "params": {"action": "act", "kind": "evaluate", "profile": "jobhunter",
                                                           "fn": "() => document.cookie"}})

        def shell_trick(steps):
            steps.insert(1, {"tool": "exec", "params": {"command": "{PY} {REPO}/scripts/jh.py status | sh",
                                                        "timeoutSeconds": 90}})

        def human_only(steps):
            steps.insert(1, {"tool": "exec", "params": {"command": "{PY} {REPO}/scripts/jh.py approve A7K2",
                                                        "timeoutSeconds": 90}})

        def fill_before_reserve(steps):
            first = steps[self.index(steps, snapshot)]
            steps[3:3] = [copy.deepcopy(first), copy.deepcopy(steps[self.index(steps, self.act("click", "e31"))])]

        def other_profile(steps):
            steps[2]["params"]["profile"] = "user"

        def confirm_without_toast(steps):
            steps.pop(self.index(steps, self.driver("read_toast")))

        def three_commits(steps):
            i = self.index(steps, send)
            steps[i + 1:i + 1] = [copy.deepcopy(steps[i]), copy.deepcopy(steps[i])]

        def outside_work(steps):
            i = self.index(steps, lambda s: " detect --file " in s["params"].get("command", ""))
            steps[i]["params"]["command"] = "{PY} {REPO}/scripts/jh.py detect --file /etc/hosts"

        def top_level_evaluate(steps):
            steps[self.index(steps, self.driver("detect_page"))]["params"] = {
                "action": "evaluate", "profile": "jobhunter", "driver": "detect_page"}

        def top_level_click(steps):
            steps[self.index(steps, self.act("click", "e31"))]["params"] = {
                "action": "click", "profile": "jobhunter", "ref": "e31"}

        def top_level_type(steps):
            i = self.index(steps, self.act("type"))
            steps[i]["params"] = dict(steps[i]["params"], action="type")
            del steps[i]["params"]["kind"]

        def stale_ref(steps):   # the dialog changed but no new snapshot was taken before the click
            i = self.index(steps, self.act("type"))
            steps.pop(max(j for j in range(i) if snapshot(steps[j])))

        def other_tab(steps):
            steps[self.index(steps, send)]["params"]["targetId"] = "T2"

        cases = [("commit action before gate arm", click_before_arm), ("commit action before the dwell", no_dwell),
                 ("typed with slowly", fast_typing), ("not in drivers/manifest.json", unknown_script),
                 ("unsafe token", shell_trick), ("not in the jobhunter-outreach ACL", human_only),
                 ("action without a reserved token", fill_before_reserve),
                 ("profile must be jobhunter", other_profile),
                 ("confirm without a success toast", confirm_without_toast),
                 ("more than 2 commit actions", three_commits), ("fails class path_work", outside_work),
                 ("'evaluate' is not a browser call the guard knows", top_level_evaluate),
                 ("'click' is not a browser call the guard knows", top_level_click),
                 ("'type' is not a browser call the guard knows", top_level_type),
                 ("ref e47 is not in the latest snapshot of this tab", stale_ref),
                 ("ref e47 is not in the latest snapshot of this tab", other_tab)]
        for expect, fn in cases:
            with self.subTest(case=fn.__name__):
                v = self.mutated(fn)
                self.assertTrue(any(expect in x for x in v), v)

    def test_ats_violations_are_flagged(self):
        self.assertEqual(self.mutated_ats(lambda steps: None), [])
        checkbox = self.act("click", "e25")

        def check_kind(steps):
            steps[self.index(steps, checkbox)]["params"]["kind"] = "check"

        def other_file(steps):
            steps[self.index(steps, lambda s: s["params"].get("action") == "upload")]["params"]["paths"] = [
                "/tmp/openclaw/uploads/other.pdf"]

        def company_domain(steps):   # a careers page on the company's own domain: detect says platform 'ats'
            for st in steps:
                p = st["params"]
                if p.get("action") == "navigate":
                    p["targetUrl"] = "https://careers.kestrel.example/jobs/1"
                if st["tool"] == "exec":
                    p["command"] = p["command"].replace("--platform greenhouse", "--platform ats")

        def submit_by_enter(steps):
            steps[self.index(steps, self.act("click", "e30"))]["params"] = {
                "action": "act", "profile": "jobhunter", "kind": "press", "key": "Enter"}
            steps.insert(self.index(steps, arm_step), steps.pop(self.index(steps, self.act("press"))))

        arm_step = lambda s: " gate arm " in s["params"].get("command", "")  # noqa: E731
        cases = [("'check' is not a browser call the guard knows", check_kind),
                 ("upload of a file that is not the staged resume", other_file),
                 ("a page the token for platform ats does not cover", company_domain),
                 ("commit action before gate arm", submit_by_enter)]
        for expect, fn in cases:
            with self.subTest(case=fn.__name__):
                v = self.mutated_ats(fn)
                self.assertTrue(any(expect in x for x in v), v)

if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- the programs that teach this flow
TEMPLATES = {
    "jobhunter-applier": ["agent-templates/applier/AGENTS.template.md",
                          "agent-templates/applier/skills/jobhunter-apply-ats/SKILL.template.md",
                          "agent-templates/applier/skills/jobhunter-apply-boards/SKILL.template.md",
                          "agent-templates/applier/skills/jobhunter-apply-email/SKILL.template.md"],
    "jobhunter-outreach": ["agent-templates/outreach/AGENTS.template.md",
                           "agent-templates/outreach/skills/jobhunter-research/SKILL.template.md",
                           "agent-templates/outreach/skills/jobhunter-linkedin/SKILL.template.md",
                           "agent-templates/outreach/skills/jobhunter-replies/SKILL.template.md"],
}
SHARED = {"skills-src/jobhunter-gate/SKILL.template.md": ("jobhunter-applier", "jobhunter-outreach"),
          "skills-src/jobhunter-stop-detect/SKILL.template.md": ("jobhunter-scout", "jobhunter-applier",
                                                                 "jobhunter-outreach")}
OTHER_U6 = ["agent-templates/applier/SOUL.md", "agent-templates/applier/IDENTITY.md",
            "agent-templates/outreach/SOUL.md", "agent-templates/outreach/IDENTITY.md", "prompts/reply_classifier.md",
            "drivers/README.md"]


class TestPrograms(unittest.TestCase):
    def setUp(self):
        with open(paths.ACL_FILE, encoding="utf-8") as fh:
            self.acl = json.load(fh)

    def read(self, rel):
        with open(os.path.join(paths.REPO, rel), encoding="utf-8") as fh:
            return fh.read()

    def commands_in(self, text):
        """(command, flags) for every backticked jh.py command in a template."""
        known = set()
        for spec in self.acl["agents"].values():
            known |= set(spec["commands"])
        out = []
        for span in re.findall(r"`([^`\n]+)`", text):
            s = span.replace("__PY__ __REPO__/scripts/jh.py ", "").strip()
            s = re.sub(r"^--cycle <cycle_id> ", "", s)
            words = s.split()
            for n in (3, 2, 1):
                if len(words) >= n and " ".join(words[:n]) in known:
                    out.append((" ".join(words[:n]), [w for w in words[n:] if w.startswith("--")]))
                    break
        return out

    def assert_allowed(self, rel, agents):
        text = self.read(rel)
        found = self.commands_in(text)
        self.assertTrue(found, rel)
        for cmd, flags in found:
            ok = [a for a in agents if cmd in self.acl["agents"][a]["commands"] and
                  all(f.split("=")[0] in self.acl["agents"][a]["commands"][cmd] for f in flags)]
            self.assertTrue(ok, "%s teaches `%s %s`, which no agent using it may run" % (rel, cmd, " ".join(flags)))

    def test_every_taught_command_is_in_the_acl(self):
        for agent, files in TEMPLATES.items():
            for rel in files:
                with self.subTest(file=rel):
                    self.assert_allowed(rel, (agent,))
        for rel, agents in SHARED.items():
            with self.subTest(file=rel):
                self.assert_allowed(rel, agents)

    def test_text_rules(self):
        files = [f for fs in TEMPLATES.values() for f in fs] + list(SHARED) + OTHER_U6
        for rel in files:
            with self.subTest(file=rel):
                text = self.read(rel)
                text.encode("ascii")
                self.assertNotIn(" - ", text)
                self.assertNotIn(" -- ", text)
                self.assertNotRegex(text, r"\{(PY|REPO|WS)\}", "use the renderer placeholders __PY__ __REPO__ __WS__")
                if rel.endswith("SKILL.template.md"):
                    head = text.split("---")[1]
                    name = re.search(r"^name: (\S+)$", head, re.M).group(1)
                    self.assertEqual(name, rel.split("/")[-2])
                    self.assertRegex(head, r"(?m)^description: .{10,}$")
                    self.assertIn("user-invocable: false", head)
                    self.assertIn('"requires": {"bins": ["python3"]}', head)

    def test_browser_call_examples_use_guard_shapes(self):
        """Every JSON browser call a U6 program shows is a shape the guard knows; none teaches the old ones."""
        checker = Checker("jobhunter-applier", "/x", "/usr/bin/python3", paths.REPO)
        files = [f for fs in TEMPLATES.values() for f in fs] + list(SHARED) + ["drivers/README.md"]
        seen = 0
        for rel in files:
            text = self.read(rel)
            with self.subTest(file=rel):
                # a call in the old shapes (the programs may name them bare, as {"action": "click"}, to forbid them)
                self.assertNotRegex(text, r'"action": "(click|type|evaluate|check|fill|select)",')
                self.assertNotRegex(text, r'"kind": "check"')
                self.assertNotIn("--input-ref", text)
                self.assertNotRegex(text, r"evaluate with the exact file text")
                for m in re.finditer(r'\{"action": [^\n`]*\}', text):
                    try:
                        call = json.loads(m.group(0))
                    except ValueError:
                        continue   # an example with a placeholder outside a JSON string
                    if len(call) == 1:
                        continue   # a bare shape named to forbid it
                    seen += 1
                    self.assertEqual(call.get("profile"), "jobhunter", m.group(0))
                    items = checker.classify_call(call, {"e10": ("button", "Apply")}, "application")
                    self.assertFalse([i for i in items if i["why"] in UNKNOWN], m.group(0))
        self.assertGreaterEqual(seen, 6)
        gate = self.read("skills-src/jobhunter-gate/SKILL.template.md")
        flat = " ".join(gate.split())
        for needle in ('"kind": "evaluate"', '"action": "upload"', '"paths": [', "latest `snapshot` of the same tab",
                       "platform `ats`", "refuses a token for `ats`", "`checkbox` or `radio`", "no `check` kind"):
            self.assertIn(needle, flat)
        self.assertIn('"kind": "evaluate"', self.read("skills-src/jobhunter-stop-detect/SKILL.template.md"))

    def test_inmail_read_back_includes_the_subject(self):
        """The InMail recipe reads the subject back with the body ("Subject: <subject>", blank line, body), the
        shape gate arm parses for kind inmail, and never the box text alone."""
        li = " ".join(self.read("agent-templates/outreach/skills/jobhunter-linkedin/SKILL.template.md").split())
        sec = li[li.index("## InMail"):]
        sec = sec[:sec.index("## ", 3)]
        for needle in ("`gate precheck-plan --kind inmail", "`gate reserve --kind inmail", "--field subject`",
                       "subject_field_present", "`observed_text` exactly to `__WS__/work/<cycle_id>/observed-<token>.txt`",
                       "the line `Subject: <subject>`, a blank line, then the body", "Never write `text` alone",
                       "`gate arm <token> --observed-file <f>`", "`slowly: true`"):
            self.assertIn(needle, sec)
        gate = " ".join(self.read("skills-src/jobhunter-gate/SKILL.template.md").split())
        self.assertIn("| inmail | `conversation_has_our_message` | `read_compose.js` |", gate)
        self.assertIn("InMail and an email it is `Subject: <subject>`, a blank line, then the body", gate)

    def test_web_email_confirm_passes_the_sent_read_back(self):
        """A web-route email is confirmed with the Sent-folder read-back as --observed-file, so the confirm-time
        hash check runs; exit 6 there already made the token unknown, so the skills forbid gate unknown and a
        resend."""
        gate = " ".join(self.read("skills-src/jobhunter-gate/SKILL.template.md").split())
        sec = gate[gate.index("* Web-route email:"):]
        sec = sec[:sec.index("* Anything else after the click")]
        for needle in ("`gate confirm <token> --evidence-file <f> --platform-ref-file <ref> --observed-file "
                       "<readback file>`", "Exit 6 there means the token is already unknown",
                       "do not run `gate unknown` and never send again"):
            self.assertIn(needle, sec)
        email = " ".join(self.read(
            "agent-templates/applier/skills/jobhunter-apply-email/SKILL.template.md").split())
        step = email[email.index("4. Dwell, one click on Send"):]
        for needle in ("`gate confirm` with the evidence, the Sent row's URL and `--observed-file` holding that "
                       "`readback_text`", "(exit 6: already unknown, never resend)"):
            self.assertIn(needle, step)
        for flag in ("--observed-file", "--platform-ref-file", "--evidence-file"):
            self.assertIn(flag, self.acl["agents"]["jobhunter-applier"]["commands"]["gate confirm"])

    def test_agents_md_size(self):
        for rel in ("agent-templates/applier/AGENTS.template.md", "agent-templates/outreach/AGENTS.template.md"):
            text = self.read(rel).replace("__PY__", "/usr/bin/python3").replace(
                "__REPO__", "/opt/someone/openclaw-job-hunter").replace(
                "__WS__", "/opt/someone/.openclaw-job-hunter/I7Q2KX4MZ/workspaces/outreach")
            self.assertLess(len(text), 12000, rel)

    def test_drivers_have_no_render_placeholders(self):
        for name in os.listdir(os.path.join(paths.REPO, "drivers")):
            if name.endswith(".js"):
                self.assertNotRegex(self.read("drivers/" + name), r"__[A-Z][A-Z0-9_]*__", name)
