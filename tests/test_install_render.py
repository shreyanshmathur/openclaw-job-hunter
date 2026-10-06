"""Install renderers (U7): cron commands, workspaces, config patches, approvals, manifest, CLI surface, and the
CLI route pieces (explicit exec policy, per-agent argPattern, cron specs and drift repair, guard config, the
cli_route switches, verify-exec-policy, run-preflight, probe stamp)."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import cli, db, install as ins, ocrun, paths
from jobhunter.commands import install as install_cmds
from jobhunter.errors import Denied
from tests import helpers
from tests.helpers import HomeTestCase

AGENT_IDS = ["jobhunter-scout", "jobhunter-evaluator", "jobhunter-applier", "jobhunter-outreach", "jobhunter-qc"]


def make_template_tree(root: str, agents: list[dict]) -> None:
    """A complete fictional template tree for every agent in agents.json (skills in skills-src/)."""
    for a in agents:
        tdir = os.path.join(root, a["template_dir"])
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, "AGENTS.template.md"), "w") as fh:
            fh.write("# %s\nRun __PY__ __REPO__/scripts/jh.py preflight. Work in __WS__/work. Id __AGENT_ID__.\n"
                     % a["role"])
        for name in ("SOUL.md", "IDENTITY.md"):
            with open(os.path.join(tdir, name), "w") as fh:
                fh.write("# %s for __ROLE__\n" % name)
        for sk in a["skills"]:
            sdir = os.path.join(root, "skills-src", sk)
            os.makedirs(sdir, exist_ok=True)
            with open(os.path.join(sdir, "SKILL.template.md"), "w") as fh:
                fh.write('---\nname: %s\ndescription: Fictional skill for tests\nuser-invocable: false\n'
                         'metadata: {"openclaw": {"requires": {"bins": ["python3"]}, "os": ["darwin", "linux"]}}\n'
                         '---\nUse __PY__ __REPO__/scripts/jh.py from __WS__.\n' % sk)
        for r in a["ref"]:
            src = r["from"].replace("*", "detect_page")
            p = os.path.join(root, src)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write("ref %s for __ROLE__\n" % src)
    os.makedirs(os.path.join(root, "prompts"), exist_ok=True)
    with open(os.path.join(root, "prompts", "reviewer.md"), "w") as fh:
        fh.write("You review one draft. Return JSON.\n")


class SourceTree:
    """A temp source root holding the real openclaw/ declarations plus a fictional template tree."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="jh-src-")
        shutil.copytree(os.path.join(paths.REPO, "openclaw"), os.path.join(self.root, "openclaw"),
                        ignore=shutil.ignore_patterns("plugins"))
        shutil.copytree(os.path.join(paths.REPO, "macos"), os.path.join(self.root, "macos"))
        make_template_tree(self.root, ins.load_agents(self.root))
        ins.use_test_source(self.root)

    def close(self):
        ins.use_test_source(None)
        shutil.rmtree(self.root, ignore_errors=True)


REPO = "/opt/jh/openclaw-job-hunter"
PROOF_EVAL = "jhp2.jobhunter-evaluator.1790000000.0123456789abcdef.fedcba9876543210." + "a1" * 32
PROOF_SCOUT = "jhp2.jobhunter-scout.1790000000.0123456789abcdef.fedcba9876543210." + "a1" * 32


def arg_pattern_cases() -> dict:
    """argv[1:] joined by spaces, as OpenClaw matches it: the guard-rewritten commands that must pass, and the
    shapes that must not (design 11.1)."""
    jh = "-I %s/scripts/jh.py --agent-proof %s " % (REPO, PROOF_EVAL)
    ok = [jh + w for w in ("preflight --lane evaluator", "whoami", "eval stats",
                           "--cycle C20260927T041500Z7Q2K --quiet cycle end --cycle C20260927T041500Z7Q2K",
                           "approvals list", "skipper", "draft create --file /w/x.json", "enrich find --contact P1",
                           "enrich find --contact P1 --target contact:P1", "enrich budget", "answers lookup --field x",
                           "installer", "profile infer-record --file /w/ws/evaluator/work/onboarding/inference.json",
                           "job show J1 --x=y")]
    bad = [
        "%s/scripts/jh.py --agent-proof %s whoami" % (REPO, PROOF_EVAL),              # no -I
        "-I %s/scripts/jh.py whoami" % REPO,                                           # no proof
        "-I %s/scripts/jh.py --agent-proof %s whoami" % (REPO, PROOF_SCOUT),           # the other agent's proof
        "-I %s/scripts/jh.py --agent-proof jhp2.jobhunter-evaluator.17900.x whoami" % REPO,   # malformed proof
        "-I %s/scripts/jh.py --agent-proof %s --home /tmp/x status" % (REPO, PROOF_EVAL),
        "-I /other/scripts/jh.py --agent-proof %s whoami" % PROOF_EVAL,
        "-I %sX/scripts/jh.py --agent-proof %s whoami" % (REPO, PROOF_EVAL),
        jh + "whoami; id", jh + "whoami $(id)", jh + "whoami `id`", jh + "job show 'J1'", jh + 'job show "J1"',
        jh + "job show =id", jh + "job show J1 | sh", jh + "job show ~/x", jh + "job show a\\b",
    ] + [jh + w for w in (
        "approve A7K2", "skip A7K2", "edit A7K2 --text x", "unpause", "config raise a 1", "breaker reset --scope gmail",
        "mail connect", "exclusions remove --type company", "exclusions import --deactivate", "companies merge K1 K2",
        "--cycle C1 approve A7K2", "auth set-pin", "forget --email a@example.com", "enrich connect hunter",
        "enrich disconnect --all", "enrich retry P1", "enrich test hunter", "enrich find --contact P1 --include-reserve",
        "enrich find --include-res --contact P1", "answers add --key k --value v", "qc smoke", "qc golden",
        "browser consent grant --site gmail", "install consent-record --grant gmail --method manual_login",
        "install consent-revoke --all", "--cycle C1 install shell-env", "--human status")]
    return {"ok": ok, "bad": bad}


class TestDeclarations(unittest.TestCase):
    def test_agents_json_matches_acl_and_roles(self):
        agents = ins.load_agents(paths.REPO)
        self.assertEqual([a["id"] for a in agents], AGENT_IDS)
        with open(paths.ACL_FILE) as fh:
            acl = json.load(fh)
        for a in agents:
            self.assertEqual(sorted(a["tools_allow"]), sorted(acl["agents"][a["id"]]["tools"]), a["id"])
            for denied in ("message", "cron", "process", "gateway", "web_fetch", "web_search", "edit", "apply_patch",
                           "sessions_*", "subagents"):
                self.assertIn(denied, a["tools_deny"])
            self.assertEqual(a["exec_policy"], "deny" if a["id"] == "jobhunter-qc" else "allowlist", a["id"])
        qc = agents[-1]
        self.assertEqual((qc["tools_allow"], qc["skills"]), ([], []))

    def test_agents_json_validation(self):
        good = ins.load_agents(paths.REPO)
        root = tempfile.mkdtemp(prefix="jh-decl-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "openclaw"))
        for broken in ({"exec_policy": None}, {"exec_policy": "full"}, {"exec_policy": "deny"},
                       {"tools_deny": ["message"]}):
            doc = json.loads(json.dumps({"agents": good}))
            a = doc["agents"][0]
            a.update(broken)
            if a["exec_policy"] is None:
                del a["exec_policy"]
            with open(os.path.join(root, "openclaw", "agents.json"), "w") as fh:
                json.dump(doc, fh)
            with self.subTest(broken=broken):
                with self.assertRaises(Denied):
                    ins.load_agents(root)

    def test_skill_assignment_follows_design_table(self):
        by_id = {a["id"]: set(a["skills"]) for a in ins.load_agents(paths.REPO)}
        self.assertEqual(by_id["jobhunter-scout"], {"jobhunter-stop-detect", "jobhunter-browser-search",
                                                    "jobhunter-linkedin-posts", "jobhunter-salary"})
        self.assertEqual(by_id["jobhunter-evaluator"], {"jobhunter-evaluate", "jobhunter-profile"})
        for shared in ("jobhunter-gate", "jobhunter-qc-loop", "jobhunter-gmail-web", "jobhunter-stop-detect"):
            self.assertIn(shared, by_id["jobhunter-applier"])
            self.assertIn(shared, by_id["jobhunter-outreach"])
        self.assertIn("jobhunter-write", by_id["jobhunter-outreach"])
        self.assertIn("jobhunter-upload", by_id["jobhunter-applier"])

    def test_crons_json_is_the_design_list(self):
        crons = ins.load_crons(paths.REPO)
        keys = [j["key"] for j in crons["jobs"]]
        self.assertEqual(len(keys), 20)
        self.assertEqual(len(ins.declared_jobs(crons)), 19)
        self.assertEqual(set(ins.LANE_JOBS.values()) - set(keys), set())
        for new in ("jobhunter:onboard-salary", "jobhunter:onboard-profile", "jobhunter:probe-scout",
                    "jobhunter:probe-evaluator", "jobhunter:probe-applier", "jobhunter:probe-outreach",
                    "jobhunter:qc-review"):
            self.assertIn(new, keys)
        for j in crons["jobs"]:
            if j["kind"] == "command":
                continue
            tools = ins.tools_list(j)
            self.assertNotIn("*", tools)
            if j["kind"] == "agent-oneshot":
                self.assertEqual((j["key"], j["agent"], tools, j["purpose"]), ("jobhunter:qc-review", "jobhunter-qc",
                                                                               [], "qc"))
                continue
            self.assertEqual(j["schedule"], {"cron": "0 0 1 1 *"})       # never enabled; runners start them
            word = ins.FINAL_WORDS[j["purpose"]]
            self.assertTrue(j["message"].endswith("reply with the single word %s." % word), j["key"])
            self.assertNotIn("NO_REPLY", j["message"])
            if j["purpose"] == "lane":
                self.assertIn("preflight --lane", j["message"])
            if j["purpose"] == "probe":
                role = j["agent"].split("-", 1)[1]
                self.assertIn("{PY} {REPO}/scripts/jh.py whoami", j["message"])
                self.assertIn("{WS_ROOT}/%s/work/probe/ok.txt" % role, j["message"])
                lane = [x for x in crons["jobs"] if x.get("purpose") == "lane" and x["agent"] == j["agent"]][0]
                self.assertEqual(j["tools"], lane["tools"])                 # the role's lane tool list
        sal = ins._job(crons, "jobhunter:onboard-salary")
        self.assertEqual((sal["agent"], sal["tools"]), ("jobhunter-scout", "exec,read,write,browser"))
        self.assertIn("{WS_ROOT}/scout/work/onboarding/salary_inputs.json", sal["message"])
        prof = ins._job(crons, "jobhunter:onboard-profile")
        self.assertEqual((prof["agent"], prof["tools"]), ("jobhunter-evaluator", "exec,read,write"))

    def test_crons_json_validation(self):
        root = tempfile.mkdtemp(prefix="jh-decl-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "openclaw"))
        base = ins.load_crons(paths.REPO)
        lane = [j for j in base["jobs"] if j["key"] == "jobhunter:scout"][0]
        for change in ({"tools": "*"}, {"tools": "exec,*"}, {"tools": ""}, {"tools": None},
                       {"message": lane["message"].replace("CYCLE_DONE", "NO_REPLY")},
                       {"message": "Run one cycle."}, {"purpose": None}, {"purpose": "probe"}):
            doc = json.loads(json.dumps(base))
            job = [j for j in doc["jobs"] if j["key"] == "jobhunter:scout"][0]
            for k, v in change.items():
                if v is None:
                    job.pop(k, None)
                else:
                    job[k] = v
            with open(os.path.join(root, "openclaw", "crons.json"), "w") as fh:
                json.dump(doc, fh)
            with self.subTest(change=change):
                with self.assertRaises(Denied):
                    ins.load_crons(root)

    def test_json5_strip(self):
        text = '// c\n{"a": "x // not a comment", /* b */ "b": [1, 2,],\n}\n'
        self.assertEqual(ins.loads_json5(text), {"a": "x // not a comment", "b": [1, 2]})
        tmpl = ins._template("agents.patch.json5.tmpl", paths.REPO)
        self.assertEqual(tmpl["tools"]["fs"], {"workspaceOnly": True})
        self.assertEqual((tmpl["tools"]["exec"], tmpl["tools"]["elevated"]), ("__EXEC_POLICY__", "__ELEVATED__"))
        appr = ins._template("exec-approvals.json5.tmpl", paths.REPO)
        for entry in ("exec_agent", "no_exec_agent"):
            self.assertEqual((appr[entry]["ask"], appr[entry]["askFallback"]), ("off", "deny"))


class TestPathAndVersion(unittest.TestCase):
    def test_check_path(self):
        home = "/tmp/jh-home-x"
        ok = ins.check_path(home + "/openclaw-job-hunter", home)
        self.assertEqual(ok["repo"], home + "/openclaw-job-hunter")
        for bad in (home + "/My Code/openclaw-job-hunter", home + "/Documents/openclaw-job-hunter",
                    home + "/Desktop/x", home + "/Downloads/repo", home + "/Library/Mobile Documents/x",
                    home + "/caf" + chr(0xE9) + "/repo", home + "/a;b"):
            with self.subTest(bad=bad):
                with self.assertRaises(Denied) as cm:
                    ins.check_path(bad, home)
                self.assertEqual(cm.exception.code, "E_PRECONDITION")
                self.assertIn("~/openclaw-job-hunter", cm.exception.message)

    def test_version(self):
        self.assertTrue(ins.version_ok("OpenClaw 2026.9.5 (ec9c1a1)"))
        self.assertTrue(ins.version_ok("OpenClaw 2026.10.1"))
        self.assertFalse(ins.version_ok("OpenClaw 2026.9.4"))
        self.assertFalse(ins.version_ok("garbage"))

    def test_timezone(self):
        self.assertEqual(ins.detect_timezone({"timezone": "Asia/Kolkata"}), "Asia/Kolkata")
        self.assertEqual(ins.detect_timezone({"timezone": "auto"}, {"TZ": "Europe/Berlin"}), "Europe/Berlin")
        self.assertTrue(ins.detect_timezone({"timezone": "auto"}, {}))


class TestCronRender(unittest.TestCase):
    def setUp(self):
        self.crons = ins.load_crons(paths.REPO)

    def render(self, **kw):
        return ins.render_cron_commands(self.crons, py="/usr/bin/python3", repo="/opt/jh/openclaw-job-hunter",
                                        ws_root="/w/ws", tz="Asia/Kolkata", **kw)

    def test_every_job_is_disabled_silent_and_keyed(self):
        cmds = self.render()
        self.assertEqual(len(cmds), 19)
        for c in cmds:
            self.assertEqual(c[:2], ["cron", "add"])
            self.assertEqual(c[-2:], ["--no-deliver", "--disabled"])
            key = c[c.index("--declaration-key") + 1]
            self.assertTrue(key.startswith("jobhunter:"))
            self.assertNotIn("--announce", c)
            self.assertNotEqual(key, "jobhunter:qc-review")                  # the one-shot spec is not created
            if "--agent" in c:
                tools = c[c.index("--tools") + 1]
                self.assertTrue(tools and "*" not in tools, key)           # always a restricted run
                for placeholder in ("{PY}", "{REPO}", "{WS_ROOT}"):
                    self.assertNotIn(placeholder, c[c.index("--message") + 1])

    def test_probe_and_onboarding_jobs(self):
        cmds = {c[c.index("--declaration-key") + 1]: c for c in self.render()}
        probe = cmds["jobhunter:probe-evaluator"]
        msg = probe[probe.index("--message") + 1]
        self.assertIn("1. Run /usr/bin/python3 /opt/jh/openclaw-job-hunter/scripts/jh.py whoami with the exec tool", msg)
        self.assertIn("/w/ws/evaluator/work/probe/ok.txt", msg)
        self.assertTrue(msg.endswith("reply with the single word PROBE_DONE."))
        self.assertEqual(probe[probe.index("--tools") + 1], "exec,read,write")
        self.assertEqual(probe[probe.index("--timeout-seconds") + 1], "300")
        sal = cmds["jobhunter:onboard-salary"]
        self.assertIn("/w/ws/scout/work/onboarding/salary_inputs.json", sal[sal.index("--message") + 1])
        self.assertEqual(sal[sal.index("--agent") + 1], "jobhunter-scout")
        # only lane jobs get a schedule in cron_schedule mode; probes and onboarding are never scheduled
        fb = {c[c.index("--declaration-key") + 1]: c for c in self.render(dispatch_mode="cron_schedule")}
        self.assertEqual(fb["jobhunter:probe-scout"][fb["jobhunter:probe-scout"].index("--cron") + 1], "0 0 1 1 *")
        self.assertIn("--stagger", fb["jobhunter:scout"])

    def test_tools_star_or_absent_refused(self):
        for bad in ("*", "exec,*", "", None):
            crons = json.loads(json.dumps(self.crons))
            job = [j for j in crons["jobs"] if j["key"] == "jobhunter:evaluate"][0]
            if bad is None:
                del job["tools"]
            else:
                job["tools"] = bad
            with self.subTest(tools=bad):
                with self.assertRaises(Denied):
                    ins.render_cron_commands(crons, py="/p", repo="/r", ws_root="/w", tz="UTC")

    def test_command_job_shape(self):
        mailer = [c for c in self.render() if "jobhunter:mailer" in c][0]
        argv = json.loads(mailer[mailer.index("--command-argv") + 1])
        # jh.py starts without Claude Code's markers, even when the Gateway was started from Claude Code
        self.assertEqual(argv, ["/usr/bin/env", "-u", "CLAUDECODE", "-u", "CLAUDE_CODE_ENTRYPOINT", "/usr/bin/python3",
                                "/opt/jh/openclaw-job-hunter/scripts/jh.py", "mail", "run", "--quiet"])
        self.assertEqual(mailer[mailer.index("--every") + 1], "5m")
        self.assertEqual(mailer[mailer.index("--command-cwd") + 1], "/opt/jh/openclaw-job-hunter")
        self.assertEqual(mailer[mailer.index("--timeout-seconds") + 1], "150")
        self.assertEqual(mailer[mailer.index("--no-output-timeout-seconds") + 1], "120")
        self.assertNotIn("--agent", mailer)
        digest = [c for c in self.render() if "jobhunter:digest" in c][0]
        self.assertEqual(digest[digest.index("--cron") + 1:digest.index("--cron") + 4],
                         ["0 9,19 * * *", "--tz", "Asia/Kolkata"])

    def test_agent_job_shape(self):
        apply_ = [c for c in self.render() if "jobhunter:apply" in c][0]
        self.assertEqual(apply_[apply_.index("--agent") + 1], "jobhunter-applier")
        self.assertEqual(apply_[apply_.index("--session") + 1], "isolated")
        self.assertEqual(apply_[apply_.index("--tools") + 1], "exec,read,write,browser")
        self.assertEqual(apply_[apply_.index("--model") + 1], "anthropic/claude-opus-5-5")
        self.assertEqual(apply_[apply_.index("--fallbacks") + 1], "anthropic/claude-sonnet-5")
        msg = apply_[apply_.index("--message") + 1]
        self.assertIn("First command: /usr/bin/python3 /opt/jh/openclaw-job-hunter/scripts/jh.py preflight --lane "
                      "applier.", msg)
        self.assertTrue(msg.endswith("When the cycle is over, reply with the single word CYCLE_DONE."))
        scout = [c for c in self.render() if "jobhunter:scout" in c][0]
        self.assertEqual(scout[scout.index("--fallbacks") + 1], "")          # strict: no fallback model
        self.assertEqual(scout[scout.index("--cron") + 1], "0 0 1 1 *")

    def test_fallback_mode_gives_agent_jobs_a_staggered_schedule(self):
        lanes = {"scout": {"cycles_per_day": [2, 3], "window": ["10:00", "19:00"], "days": [1, 2, 3, 4, 5]},
                 "replies": {"cycles_per_day": [1, 2], "window": ["11:00", "20:00"], "days": [1, 2, 3, 4, 5, 6, 7]}}
        cmds = self.render(dispatch_mode="cron_schedule", lanes=lanes)
        scout = [c for c in cmds if "jobhunter:scout" in c][0]
        self.assertEqual(scout[scout.index("--cron") + 1], "0 10,13,16 * * 1,2,3,4,5")
        self.assertEqual(scout[scout.index("--stagger") + 1], "30m")
        replies = [c for c in cmds if "jobhunter:replies" in c][0]
        self.assertEqual(replies[replies.index("--cron") + 1], "0 11,15 * * 0,1,2,3,4,5,6")
        self.assertIn("--disabled", scout)

    def test_shell_lines_round_trip(self):
        import shlex
        cmds = self.render()
        lines = ins.shell_lines(cmds).split("\n")
        self.assertEqual([shlex.split(l) for l in lines], cmds)

    def test_failure_alerts(self):
        ids = {j["key"]: "id-%d" % i for i, j in enumerate(self.crons["jobs"])}
        self.assertEqual(ins.render_failure_alerts(self.crons, ids, {"owner": {"notify": {"channel": "none"}}}), [])
        self.assertEqual(ins.render_failure_alerts(
            self.crons, ids, {"owner": {"notify": {"channel": "whatsapp", "to": "+10000000000"}}}), [])
        edits = ins.render_failure_alerts(self.crons, ids,
                                          {"owner": {"notify": {"channel": "whatsapp", "to": "+12025550123"}}})
        wanted = [j["key"] for j in self.crons["jobs"] if j.get("failure_alert")]
        self.assertEqual(len(edits), len(wanted))
        e = edits[0]
        self.assertEqual(e[:4], ["cron", "edit", ids[wanted[0]], "--failure-alert"])
        for flag, val in (("--failure-alert-channel", "whatsapp"), ("--failure-alert-to", "+12025550123"),
                          ("--failure-alert-after", "2"), ("--failure-alert-cooldown", "1h"),
                          ("--failure-alert-mode", "announce")):
            self.assertEqual(e[e.index(flag) + 1], val)

    def test_job_sets(self):
        resume = ins.select_jobs(self.crons, "resume")
        self.assertIn("jobhunter:dispatch", resume)
        self.assertNotIn("jobhunter:apply", resume)
        pause = ins.select_jobs(self.crons, "pause")
        for keep in ("jobhunter:sheet-sync", "jobhunter:notify", "jobhunter:digest", "jobhunter:housekeeping"):
            self.assertNotIn(keep, pause)
        for stop in ("jobhunter:dispatch", "jobhunter:sources-api", "jobhunter:mailer", "jobhunter:qc-worker"):
            self.assertIn(stop, pause)
        self.assertIn("jobhunter:apply", ins.select_jobs(self.crons, "resume", dispatch_mode="cron_schedule"))
        self.assertEqual(ins.select_jobs(self.crons, "lane", "evaluator"), ["jobhunter:evaluate"])
        with self.assertRaises(Denied):
            ins.select_jobs(self.crons, "lane", "nope")
        # probe and onboarding jobs are never enabled by resume, in either dispatch mode
        for mode in ("dispatcher", "cron_schedule"):
            resume = ins.select_jobs(self.crons, "resume", dispatch_mode=mode)
            self.assertFalse([k for k in resume if "probe" in k or "onboard" in k or "qc-review" in k], mode)
        self.assertNotIn("jobhunter:qc-review", ins.select_jobs(self.crons, "all"))


class TestPatches(unittest.TestCase):
    def setUp(self):
        self.agents = ins.load_agents(paths.REPO)

    def test_agents_patch_scope_and_values(self):
        patch = ins.render_agents_patch(self.agents, ws_root="/w/ws", repo="/opt/r", py="/usr/bin/python3",
                                        root=paths.REPO)
        self.assertEqual(list(patch), ["agents"])
        self.assertEqual(sorted(patch["agents"]["entries"]), sorted(AGENT_IDS))
        e = patch["agents"]["entries"]["jobhunter-applier"]
        self.assertEqual(e["workspace"], "/w/ws/applier")
        self.assertEqual(e["model"], "anthropic/claude-opus-5-5")
        self.assertEqual(e["tools"]["allow"], ["exec", "read", "write", "browser"])
        self.assertIn("edit", e["tools"]["deny"])
        self.assertIn("apply_patch", e["tools"]["deny"])
        self.assertTrue(e["tools"]["fs"]["workspaceOnly"])
        # mode only (OpenClaw refuses mode next to security or ask); null deletes an older install's keys
        self.assertEqual(e["tools"]["exec"], {"mode": "allowlist", "security": None, "ask": None,
                                              "host": "gateway", "strictInlineEval": True, "safeBins": [],
                                              "safeBinTrustedDirs": []})
        self.assertEqual(e["tools"]["elevated"], {"enabled": False})
        qc = patch["agents"]["entries"]["jobhunter-qc"]["tools"]
        self.assertEqual(qc["exec"], {"mode": "deny", "security": None, "ask": None, "safeBins": [],
                                      "safeBinTrustedDirs": []})
        self.assertEqual((qc["allow"], qc["elevated"]), ([], {"enabled": False}))
        self.assertEqual(e["models"]["anthropic/claude-sonnet-5"], {"agentRuntime": {"id": "claude-cli"}})
        self.assertIn("jobhunter-gate", e["skills"])
        self.assertEqual(patch["agents"]["entries"]["jobhunter-qc"]["skills"], [])
        api = ins.render_agents_patch(self.agents, ws_root="/w/ws", repo="/opt/r", py="/p", route="api_key",
                                      root=paths.REPO)
        self.assertNotIn("models", api["agents"]["entries"]["jobhunter-applier"])
        blob = json.dumps(patch)
        for forbidden in ("bindings", "channels", "defaults", "defaultProfile"):
            self.assertNotIn(forbidden, blob)

    def test_scope_guard(self):
        with self.assertRaises(Denied):
            ins.assert_patch_scope({"bindings": []}, AGENT_IDS)
        with self.assertRaises(Denied):
            ins.assert_patch_scope({"agents": {"defaults": {}}}, AGENT_IDS)
        with self.assertRaises(Denied):
            ins.assert_patch_scope({"agents": {"entries": {"main": {}}}}, AGENT_IDS)
        with self.assertRaises(Denied):
            ins.assert_patch_scope({"browser": {"defaultProfile": "jobhunter"}}, AGENT_IDS)
        ins.assert_patch_scope(ins.uninstall_patch(self.agents), AGENT_IDS)
        self.assertIsNone(ins.uninstall_patch(self.agents)["agents"]["entries"]["jobhunter-scout"])

    def test_mode_n_only_with_both_flags_and_fqc(self):
        for kw in ({"cli_tools": "native"}, {"cli_tools": "other", "accept_reduced": True}):
            with self.subTest(kw=kw):
                with self.assertRaises(Denied):
                    ins.render_agents_patch(self.agents, ws_root="/w", repo="/r", py="/p", root=paths.REPO, **kw)
        n = ins.render_agents_patch(self.agents, ws_root="/w", repo="/r", py="/p", root=paths.REPO,
                                    cli_tools="native", accept_reduced=True)["agents"]["entries"]
        self.assertEqual((n["jobhunter-scout"]["tools"]["exec"]["security"], n["jobhunter-scout"]["tools"]["exec"]["mode"],
                          n["jobhunter-scout"]["tools"]["exec"]["ask"]), (None, "full", None))
        self.assertEqual(n["jobhunter-qc"]["tools"]["exec"]["mode"], "deny")          # qc never gets exec
        f = ins.render_agents_patch(self.agents, ws_root="/w", repo="/r", py="/p", root=paths.REPO,
                                    qc_reply="file")["agents"]["entries"]["jobhunter-qc"]["tools"]
        self.assertEqual(f["allow"], ["write"])                           # F-QC: the verdict file only
        self.assertNotIn("write", f["deny"])
        self.assertIn("exec", f["deny"])
        self.assertEqual(f["exec"]["mode"], "deny")

    def test_guard_config(self):
        p = ins.render_guard_config(repo="/opt/r", py="/usr/bin/python3", cfg={}, ws_root="/w/ws",
                                    state_dir="/h/.openclaw")
        conf = p["plugins"]["entries"]["jobhunter-guard"]["config"]
        self.assertEqual(conf, {"repo": "/opt/r", "python": "/usr/bin/python3", "homeFile": "/opt/r/private/home.json",
                                "publicReadonlyAgents": ["main"], "claudeNativeTools": "deny", "pinToolSurface": True,
                                "proofCarriers": ["argv", "env"], "recordEvents": False,
                                "protectedRoots": {"read": ["/opt/r/private", "/h/.openclaw"],
                                                   "write": ["/opt/r", "/w/ws", "/h/.openclaw"]},
                                "qcVerdictFile": False})
        n = ins.render_guard_config(repo="/r", py="/p", cfg={}, ws_root="/w", state_dir="/s", carriers=["argv"],
                                    cli_tools="native", qc_verdict_file=True)["plugins"]["entries"]["jobhunter-guard"]
        self.assertEqual((n["config"]["claudeNativeTools"], n["config"]["pinToolSurface"], n["config"]["proofCarriers"],
                          n["config"]["qcVerdictFile"]), ("gate", False, ["argv"], True))
        e = ins.render_guard_config(repo="/r", py="/p", cfg={}, ws_root="/w", state_dir="/s", carriers=["env"],
                                    qc_verdict_file=True)["plugins"]["entries"]["jobhunter-guard"]
        self.assertEqual(e["config"]["proofCarriers"], ["env"])
        # mode N: Claude Code's own Bash never gets the env proof, so the env carrier is refused there
        for carriers in (["env"], ["argv", "env"]):
            with self.subTest(carriers=carriers):
                with self.assertRaises(Denied) as cm:
                    ins.render_guard_config(repo="/r", py="/p", cfg={}, carriers=carriers, cli_tools="native")
                self.assertIn("argv identity carrier", cm.exception.message)
        self.assertEqual(ins.oc_state_dir("jhtest", "/h"), os.path.realpath("/h/.openclaw-jhtest"))
        self.assertEqual(ins.oc_state_dir(None, "/h"), os.path.realpath("/h/.openclaw"))
        p = ins.render_guard_config(repo="/opt/r", py="/p",
                                    cfg={"owner": {"notify": {"channel": "whatsapp", "to": "+12025550123"}}})
        self.assertEqual(p["plugins"]["entries"]["jobhunter-guard"]["config"]["ownerFallback"],
                         [{"channel": "whatsapp", "senderId": "+12025550123"}])

    def test_arg_pattern(self):
        cases = arg_pattern_cases()
        for carrier in ("argv+env", "argv"):
            rx = re.compile(ins.arg_pattern(REPO, "jobhunter-evaluator", carrier))
            for ok in cases["ok"]:
                self.assertTrue(rx.search(ok), ok)
            for bad in cases["bad"]:
                self.assertIsNone(rx.search(bad), bad)
        # the env carrier (F-ENV): the same shape without the proof pair, -I still required
        rx = re.compile(ins.arg_pattern(REPO, "jobhunter-evaluator", "env"))
        self.assertTrue(rx.search("-I %s/scripts/jh.py --cycle C1 eval stats" % REPO))
        self.assertIsNone(rx.search("%s/scripts/jh.py eval stats" % REPO))
        self.assertIsNone(rx.search("-I %s/scripts/jh.py approve A7K2" % REPO))
        self.assertIsNone(rx.search("-I %s/scripts/jh.py eval stats; id" % REPO))
        with self.assertRaises(Denied):
            ins.arg_pattern(REPO, "main")
        with self.assertRaises(Denied):
            ins.arg_pattern(REPO, "jobhunter-scout", "both")

    def test_arg_pattern_follows_the_acl(self):
        with open(paths.ACL_FILE) as fh:
            acl = json.load(fh)
        for aid, spec in acl["agents"].items():
            if "exec" not in spec.get("tools", []):
                continue
            rx = re.compile(ins.arg_pattern(REPO, aid))
            head = "-I %s/scripts/jh.py --agent-proof jhp2.%s.1790000000.0123456789abcdef.fedcba9876543210.%s " % (
                REPO, aid, "a1" * 32)
            for cmd in spec.get("commands") or {}:
                self.assertTrue(rx.search(head + cmd), "%s %s" % (aid, cmd))      # every ACL command passes L2
            for cmd in acl["human_only"]:
                self.assertIsNone(rx.search(head + cmd), "%s %s" % (aid, cmd))    # no human-only command does

    def test_arg_pattern_node_cross_check(self):
        node = shutil.which("node") or os.path.expanduser("~/.openclaw/tools/node/bin/node")
        if not os.path.isfile(node):
            self.skipTest("no node binary")
        cases = arg_pattern_cases()
        for carrier in ("argv+env", "env"):
            pattern = ins.arg_pattern(REPO, "jobhunter-evaluator", carrier)
            texts = cases["ok"] + cases["bad"]
            p = subprocess.run([node, os.path.join(paths.REPO, "tests", "fixtures", "install", "argpattern.mjs")],
                               input=json.dumps({"pattern": pattern, "cases": texts}).encode(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr.decode())
            node_says = json.loads(p.stdout.decode())
            rx = re.compile(pattern)
            self.assertEqual(node_says, [bool(rx.search(t)) for t in texts], carrier)

    def test_merge_approvals_keeps_other_agents(self):
        current = {"version": 1, "defaults": {"security": "full"}, "agents": {"main": {"security": "full"}}}
        doc = ins.merge_approvals(current, self.agents, repo="/opt/r", py="/usr/bin/python3", root=paths.REPO)
        self.assertEqual(doc["defaults"], {"security": "full"})
        self.assertEqual(doc["agents"]["main"], {"security": "full"})
        app = doc["agents"]["jobhunter-applier"]
        self.assertEqual((app["security"], app["ask"], app["askFallback"]), ("allowlist", "off", "deny"))
        self.assertEqual(app["allowlist"][0]["pattern"], "/usr/bin/python3")
        self.assertEqual(app["allowlist"][0]["argPattern"], ins.arg_pattern("/opt/r", "jobhunter-applier"))
        self.assertIn("jhp2\\.jobhunter-applier\\.", app["allowlist"][0]["argPattern"])     # pinned to this agent
        self.assertIn("jhp2\\.jobhunter-scout\\.", doc["agents"]["jobhunter-scout"]["allowlist"][0]["argPattern"])
        qc = doc["agents"]["jobhunter-qc"]
        self.assertEqual((qc["security"], qc["ask"], qc["askFallback"], qc["allowlist"]), ("deny", "off", "deny", []))
        env_only = ins.merge_approvals(current, self.agents, repo="/opt/r", py="/p", root=paths.REPO, carrier=["env"])
        self.assertNotIn("--agent-proof", env_only["agents"]["jobhunter-scout"]["allowlist"][0]["argPattern"])
        again = ins.merge_approvals(doc, self.agents, repo="/opt/r", py="/usr/bin/python3", root=paths.REPO)
        self.assertEqual(again, doc)
        gone = ins.merge_approvals(doc, self.agents, repo="/opt/r", py="/p", remove=True, root=paths.REPO)
        self.assertEqual(sorted(gone["agents"]), ["main"])
        with tempfile.TemporaryDirectory() as d:                    # a symlinked python (Homebrew, pyenv)
            real = os.path.join(d, "python3.12")
            open(real, "w").close()
            link = os.path.join(d, "python3")
            os.symlink(real, link)
            linked = ins.merge_approvals(current, self.agents, repo="/opt/r", py=link, root=paths.REPO)
            self.assertEqual(linked["agents"]["jobhunter-scout"]["allowlist"][0]["pattern"], os.path.realpath(real))
        wrapped = ins.merge_approvals({"file": current, "effective": {}}, self.agents, repo="/opt/r", py="/p",
                                      root=paths.REPO)
        self.assertIn("main", wrapped["agents"])
        with self.assertRaises(Denied):
            ins.merge_approvals({"something": "else"}, self.agents, repo="/opt/r", py="/p", root=paths.REPO)

    def test_extra_dirs(self):
        patch, new = ins.extra_dirs_patch(["/x/skills"], "/opt/r")
        self.assertEqual(new, ["/x/skills", "/opt/r/shared-skills"])
        self.assertEqual(patch, {"skills": {"load": {"extraDirs": new}}})
        self.assertEqual(ins.extra_dirs_patch(new, "/opt/r"), (None, new))
        patch, rem = ins.extra_dirs_patch(new, "/opt/r", remove=True)
        self.assertEqual(rem, ["/x/skills"])
        self.assertEqual(ins.extra_dirs_patch(None, "/opt/r")[1], ["/opt/r/shared-skills"])

    def test_missing_agents_and_domains(self):
        rows = ins.missing_agents(self.agents, [{"id": "jobhunter-scout"}, {"id": "main"}], "/w")
        self.assertEqual([r["id"] for r in rows], AGENT_IDS[1:])
        self.assertEqual(rows[0]["workspace"], "/w/evaluator")
        self.assertEqual(len(ins.missing_agents(self.agents, {"agents": [{"id": i} for i in AGENT_IDS]}, "/w")), 0)
        cfg = {"boards": {"sites": {"naukri": {"discover": "browser", "apply": "off"},
                                    "cutshort": {"discover": "off", "apply": "off"},
                                    "wellfound": {"discover": "off", "apply": "browser"}}}}
        self.assertEqual(ins.enabled_board_domains(cfg), ["naukri.com", "wellfound.com"])


class TestBoardDomainsFollowTheInterview(unittest.TestCase):
    def test_sites_picked_in_the_profile_count(self):
        with open(os.path.join(paths.REPO, "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertEqual(ins.enabled_board_domains(cfg), [])                 # every board ships off
        from jobhunter import searches
        sites = searches.enabled_sites({"sites_enabled": ["naukri", "instahyre", "ats_forms"]}, cfg)
        self.assertEqual(ins.enabled_board_domains(cfg, sites), ["naukri.com", "instahyre.com"])
        cfg["boards"]["sites"]["naukri"]["discover"] = "api"                 # the config can still say api
        self.assertEqual(ins.enabled_board_domains(cfg, searches.enabled_sites({"sites_enabled": ["naukri"]}, cfg)),
                         [])


class TestWrapperRoute(unittest.TestCase):
    def setUp(self):
        parser = cli.build_parser()
        self.commands = {k: v.get_default("_jh_callers") for k, v in cli.registered_commands(parser).items()}
        with open(paths.ACL_FILE) as fh:
            self.human_only = json.load(fh)["human_only"]

    def route(self, *words):
        return ins.wrapper_route(list(words), self.commands, self.human_only)

    def test_commands_named_by_messages_and_docs(self):
        # every ./jobhunter command a product message or a doc names must be reachable
        self.assertEqual(self.route("qc", "golden"), ("pin", "qc golden"))
        self.assertEqual(self.route("answers", "add", "--key", "k", "--value", "v"), ("pin", "answers add"))
        self.assertEqual(self.route("eval", "requeue", "--since-profile-change"), ("open", "eval requeue"))
        self.assertEqual(self.route("companies", "split", "C1", "--keys", "a"), ("pin", "companies split"))
        self.assertEqual(self.route("contacts", "split", "P1", "--keys", "a"), ("pin", "contacts split"))
        self.assertEqual(self.route("profile", "answer", "--field", "Q3", "--value", "x"), ("pin", "profile answer"))
        self.assertEqual(self.route("mail", "import-history", "--days", "365"), ("pin", "mail import-history"))
        self.assertEqual(self.route("mail", "connect", "--store", "file"), ("pin", "mail connect"))
        self.assertEqual(self.route("resume", "base-review"), ("pin", "resume base-review"))
        self.assertEqual(self.route("resume", "base-build"), ("open", "resume base-build"))
        self.assertEqual(self.route("export"), ("open", "export"))
        self.assertEqual(self.route("approvals", "list"), ("open", "approvals list"))
        self.assertEqual(self.route("sheet", "ping"), ("open", "sheet ping"))
        self.assertEqual(self.route("config", "validate"), ("open", "config validate"))

    def test_flags_of_human_only_entries(self):
        self.assertEqual(self.route("exclusions", "import"), ("open", "exclusions import"))
        self.assertEqual(self.route("exclusions", "import", "--deactivate"), ("pin", "exclusions import"))

    def test_refused(self):
        how, why = self.route("gate", "reserve", "--kind", "x")
        self.assertEqual(how, "none")
        self.assertIn("agents or the automations", why)
        self.assertEqual(self.route("dispatch", "tick")[0], "none")          # automations only (S)
        how, why = self.route("breaker")
        self.assertEqual((how, why), ("none", "usage: ./jobhunter breaker reset|status|trip"))
        self.assertIn("unknown command: nope", self.route("nope", "x")[1])


class TestOwnerDetails(HomeTestCase):
    def setUp(self):
        super().setUp()
        with open(os.path.join(paths.REPO, "config.example.json")) as fh:
            self.example = json.load(fh)
        self.cfg_path = os.path.join(paths.private_dir(), "config.json")
        with open(self.cfg_path, "w") as fh:
            json.dump(self.example, fh)
        self.number = "+1000" + "0000001"

    def read(self):
        with open(self.cfg_path) as fh:
            return json.load(fh)

    def test_missing_on_the_shipped_example(self):
        self.assertEqual(ins.owner_missing(self.example)[:4], ["first_name", "last_name", "gmail_address", "notify"])
        cfg = json.loads(json.dumps(self.example))
        cfg["owner"]["signature"]["links"] = ["https://www.linkedin.com/in/your-handle"]
        self.assertIn("signature_links", ins.owner_missing(cfg))

    def test_set_owner(self):
        self.example["owner"]["signature"]["links"] = ["https://www.linkedin.com/in/your-handle"]
        with open(self.cfg_path, "w") as fh:
            json.dump(self.example, fh)
        res = ins.set_owner(first_name=" Eve ", last_name="Quorrin", gmail_address="Eve.Q@Example.com", phone="",
                            notify_channel="whatsapp", notify_to=self.number)
        cfg = self.read()
        o = cfg["owner"]
        self.assertEqual((o["first_name"], o["last_name"], o["gmail_address"]), ("Eve", "Quorrin", "eve.q@example.com"))
        self.assertEqual(o["signature"]["full_name"], "Eve Quorrin")
        self.assertEqual(o["signature"]["links"], [])                       # the your-handle placeholder is gone
        self.assertEqual(o["notify"]["to"], self.number)
        self.assertTrue(res["notify_changed"])
        self.assertEqual(res["missing"], [])
        self.assertEqual(cfg["timezone"], self.example["timezone"])          # the rest of the file is kept
        self.assertEqual(os.stat(self.cfg_path).st_mode & 0o777, 0o600)
        self.assertEqual(ins.notify_target(cfg), ("whatsapp", self.number))
        res = ins.set_owner(links=["https://evequorrin.dev/work"])
        self.assertFalse(res["notify_changed"])
        self.assertEqual(self.read()["owner"]["signature"]["links"], ["https://evequorrin.dev/work"])

    def test_set_owner_refuses_bad_values(self):
        for kw in ({"gmail_address": "you@example.com"}, {"gmail_address": "not an address"},
                   {"first_name": ""}, {"notify_channel": "whatsapp", "notify_to": "12345"},
                   {"links": ["https://www.linkedin.com/in/your-handle"]}, {"links": ["javascript:alert(1)"]},
                   {"phone": "call me"}):
            with self.subTest(kw=kw):
                self.assertDenied("E_VALIDATION", ins.set_owner, **kw)
        self.assertEqual(self.read(), self.example)                          # nothing was written

    def test_cli(self):
        out = io.StringIO()
        rc = cli.main(["--human", "install", "owner", "--missing"], env={}, stdin=io.StringIO(""), stdout=out,
                      modules=[install_cmds])
        self.assertEqual(rc, 0)
        self.assertIn("gmail_address", out.getvalue())
        out = io.StringIO()
        rc = cli.main(["--human", "install", "owner", "--first-name", "Eve", "--last-name", "Quorrin",
                       "--gmail", "eve.q@example.com", "--notify-channel", "none", "--no-links"],
                      env={}, stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("chat number changed", out.getvalue())
        self.assertEqual(ins.owner_missing(self.read()), [])

    def test_board_domains_command_uses_the_profile_sites(self):
        with mock.patch.object(install_cmds, "_browsed_sites", return_value=["naukri", "instahyre"]):
            out = io.StringIO()
            rc = cli.main(["--human", "install", "board-domains"], env={}, stdin=io.StringIO(""), stdout=out,
                          modules=[install_cmds])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "naukri.com,instahyre.com")
        self.assertEqual(install_cmds._browsed_sites(), [])                   # no confirmed profile yet


class TestWorkspaces(unittest.TestCase):
    def setUp(self):
        self.src = SourceTree()
        self.ws = tempfile.mkdtemp(prefix="jh-ws-")

    def tearDown(self):
        self.src.close()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_render_substitutes_and_is_idempotent(self):
        agents = ins.load_agents()
        res = ins.render_workspaces(ws_root=self.ws, repo="/opt/r", py="/usr/bin/python3", agents=agents)
        self.assertEqual(res["missing"], [])
        self.assertEqual(res["leftover_placeholders"], [])
        with open(os.path.join(self.ws, "applier", "AGENTS.md")) as fh:
            text = fh.read()
        self.assertIn("/usr/bin/python3 /opt/r/scripts/jh.py preflight", text)
        self.assertIn(os.path.join(self.ws, "applier") + "/work", text)
        self.assertIn("jobhunter-applier", text)
        skill = os.path.join(self.ws, "applier", "skills", "jobhunter-gate", "SKILL.md")
        self.assertTrue(os.path.isfile(skill))
        self.assertFalse(os.path.exists(os.path.join(self.ws, "applier", "skills", "jobhunter-gate",
                                                     "SKILL.template.md")))
        self.assertFalse(os.path.exists(os.path.join(self.ws, "scout", "skills", "jobhunter-gate")))
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "scout", "ref", "drivers", "detect_page.js")))
        for sub in ("work", "inbox"):
            self.assertTrue(os.path.isdir(os.path.join(self.ws, "qc", sub)))
        self.assertEqual(os.listdir(os.path.join(self.ws, "qc", "skills")), [])
        self.assertEqual(set(res["hashes"]), {"reviewer_prompt_sha256", "qc_agents_md_sha256"})
        # OpenClaw-owned files survive; a stale jobhunter skill is removed; re-render is stable
        os.makedirs(os.path.join(self.ws, "applier", "memory"))
        os.makedirs(os.path.join(self.ws, "applier", "skills", "jobhunter-old"))
        os.makedirs(os.path.join(self.ws, "applier", "skills", "my-own-skill"))
        res2 = ins.render_workspaces(ws_root=self.ws, repo="/opt/r", py="/usr/bin/python3", agents=agents)
        self.assertEqual(res2["hashes"], res["hashes"])
        self.assertTrue(os.path.isdir(os.path.join(self.ws, "applier", "memory")))
        self.assertTrue(os.path.isdir(os.path.join(self.ws, "applier", "skills", "my-own-skill")))
        self.assertFalse(os.path.exists(os.path.join(self.ws, "applier", "skills", "jobhunter-old")))
        self.assertEqual(os.stat(self.ws).st_mode & 0o777, 0o700)

    def test_missing_template_refuses_before_writing(self):
        os.remove(os.path.join(self.src.root, "skills-src", "jobhunter-gate", "SKILL.template.md"))
        with self.assertRaises(Denied) as cm:
            ins.render_workspaces(ws_root=self.ws, repo="/opt/r", py="/p", agents=ins.load_agents())
        self.assertEqual(cm.exception.code, "E_VALIDATION")
        self.assertTrue(any("jobhunter-gate" in m for m in cm.exception.data["missing"]))
        self.assertEqual(os.listdir(self.ws), [])

    def test_leftover_placeholders_reported(self):
        with open(os.path.join(self.src.root, "agent-templates", "qc", "SOUL.md"), "a") as fh:
            fh.write("unknown __NOT_A_PLACEHOLDER__\n")
        res = ins.render_workspaces(ws_root=self.ws, repo="/opt/r", py="/p", agents=ins.load_agents())
        self.assertEqual(res["leftover_placeholders"][0]["placeholders"], ["__NOT_A_PLACEHOLDER__"])


class TestInstallCli(HomeTestCase):
    """The `install` commands through jobhunter.cli.main (system caller, temp home, fixture templates)."""

    def setUp(self):
        super().setUp()
        self.src = SourceTree()
        self.conn.close()

    def tearDown(self):
        self.src.close()
        super().tearDown()

    def run_cli(self, *argv):
        out = io.StringIO()
        rc = cli.main(list(argv), env={}, stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        text = out.getvalue()
        return rc, (json.loads(text) if text.startswith("{") else text)

    def test_render_workspaces_writes_reviewer_hashes(self):
        rc, env = self.run_cli("install", "render-workspaces")
        self.assertEqual(rc, 0, env)
        conn = db.connect(write=False)
        try:
            for key in db.REQUIRED_FOR_QC:
                self.assertEqual(db.meta_get(conn, key), env["data"]["hashes"][key])
        finally:
            conn.close()
        self.assertTrue(os.path.isfile(os.path.join(paths.ws_dir("qc"), "AGENTS.md")))

    def test_render_crons_human_lines(self):
        rc, text = self.run_cli("--human", "install", "render-crons")
        self.assertEqual(rc, 0, text)
        lines = [l for l in text.strip().split("\n") if l]
        self.assertEqual(len(lines), 19)
        self.assertTrue(all(l.startswith("cron add ") for l in lines))
        self.assertIn(paths.home()["ws_root"] + "/evaluator/work/probe/ok.txt", text)

    def test_manifest_from_cron_list(self):
        crons = ins.load_crons()
        listing = {"jobs": [{"id": "j%d" % i, "declarationKey": j["key"], "enabled": False, "name": j["name"]}
                            for i, j in enumerate(ins.declared_jobs(crons))] +
                   [{"id": "x", "declarationKey": "other:job"}]}
        f = os.path.join(self.home.dir, "cron.json")
        with open(f, "w") as fh:
            json.dump(listing, fh)
        rc, env = self.run_cli("install", "manifest", "--set", f)
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["cron_jobs"]["jobhunter:dispatch"], "j0")
        self.assertNotIn("other:job", env["data"]["cron_jobs"])
        specs = ins.read_manifest()["cron_specs"]                      # 6.3.1: every job's declared fields
        self.assertEqual(set(specs), {j["key"] for j in crons["jobs"]})
        self.assertEqual(specs["jobhunter:qc-review"]["tools"], [])
        self.assertEqual(specs["jobhunter:dispatch"]["kind"], "command")
        self.assertEqual(paths.home()["cron_jobs"]["jobhunter:mailer"], "j2")
        rc, text = self.run_cli("--human", "install", "job-ids", "--which", "lane", "--lane", "applier")
        self.assertEqual((rc, text.strip()), (0, "j10"))
        rc, text = self.run_cli("--human", "install", "job-ids", "--which", "pause")
        self.assertIn("j0", text.split())
        self.assertNotIn("j4", text.split())                      # sheet-sync keeps running while paused
        # a missing declared job refuses
        listing["jobs"] = listing["jobs"][:5]
        with open(f, "w") as fh:
            json.dump(listing, fh)
        rc, env = self.run_cli("install", "manifest", "--set", f)
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        rc, env = self.run_cli("install", "manifest", "--show")
        self.assertEqual(env["data"]["cron_jobs"]["jobhunter:replies"], "j12")

    def test_manifest_merge_object(self):
        f = os.path.join(self.home.dir, "m.json")
        for obj in ({"agents": ["a"], "plugin": "p"}, {"agents": ["a", "b"], "stay_awake": True}):
            with open(f, "w") as fh:
                json.dump(obj, fh)
            rc, env = self.run_cli("install", "manifest", "--set", f)
            self.assertEqual(rc, 0, env)
        m = ins.read_manifest()
        self.assertEqual((m["agents"], m["plugin"], m["stay_awake"]), (["a", "b"], "p", True))

    def test_shell_env_and_agents(self):
        rc, text = self.run_cli("--human", "install", "shell-env")
        self.assertEqual(rc, 0)
        vals = dict(l.split("=", 1) for l in text.strip().split("\n"))
        self.assertEqual(vals["JH_HOME_EXISTS"], "1")
        self.assertEqual(vals["JH_INSTALL_ID"], paths.home()["install_id"])
        self.assertIn("JH_MODEL_ROUTE", vals)
        f = os.path.join(self.home.dir, "agents.json")
        with open(f, "w") as fh:
            fh.write('banner line\n[{"id": "jobhunter-scout"}]\n')
        rc, text = self.run_cli("--human", "install", "agents", "--missing-from", f)
        self.assertEqual(rc, 0, text)
        rows = [l.split("\t") for l in text.strip().split("\n")]
        self.assertEqual([r[0] for r in rows], AGENT_IDS[1:])

    def test_patches_written_private(self):
        for args in (("install", "render-agents-patch"), ("install", "render-guard-config"),
                     ("install", "render-uninstall-patch"), ("install", "render-headless-patch"),
                     ("install", "render-stayawake")):
            rc, env = self.run_cli(*args)
            self.assertEqual(rc, 0, env)
            self.assertEqual(os.stat(env["data"]["path"]).st_mode & 0o777, 0o600)
        with open(os.path.join(ins.install_dir(), ins.STAYAWAKE_LABEL + ".plist")) as fh:
            plist = fh.read()
        self.assertIn(paths.home()["repo"] + "/macos/stay-awake.sh", plist)
        self.assertNotIn("__", plist)

    def test_render_approvals_and_extradirs(self):
        f = os.path.join(self.home.dir, "cur.json")
        with open(f, "w") as fh:
            json.dump({"version": 1, "defaults": {}, "agents": {"main": {"security": "full"}}}, fh)
        rc, env = self.run_cli("install", "render-approvals", "--current", f)
        self.assertEqual(rc, 0, env)
        with open(env["data"]["path"]) as fh:
            doc = json.load(fh)
        self.assertIn("jobhunter-outreach", doc["agents"])
        self.assertIn("main", doc["agents"])
        with open(f, "w") as fh:
            fh.write("[]")
        rc, env = self.run_cli("install", "render-extradirs", "--current", f)
        self.assertEqual(rc, 0, env)
        with open(env["data"]["path"]) as fh:
            self.assertEqual(json.load(fh)["skills"]["load"]["extraDirs"][-1],
                             os.path.join(paths.home()["repo"], "shared-skills"))
        rc, env = self.run_cli("install", "render-extradirs", "--current", f, "--remove")
        self.assertEqual((rc, env["code"]), (0, "NOTHING_TO_DO"))

    def test_wait_guard(self):
        rc, env = self.run_cli("install", "wait-guard", "--timeout", "0")
        self.assertEqual((rc, env["code"]), (11, "E_GUARD_MISSING"))
        hb = {"install_id": paths.home()["install_id"], "beat_at": ins.now()}
        import datetime
        hb["beat_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(paths.guard_dir(), "heartbeat.json"), "w") as fh:
            json.dump(hb, fh)
        rc, env = self.run_cli("install", "wait-guard", "--timeout", "0")
        self.assertEqual(rc, 0, env)
        rc, env = self.run_cli("install", "wait-guard", "--timeout", "0", "--proof-version", "2")
        self.assertEqual((rc, env["code"], env["data"]["reason"]), (11, "E_GUARD_MISSING", "old_guard"))
        hb.update(proof_version=2, carriers=["argv", "env"])
        with open(os.path.join(paths.guard_dir(), "heartbeat.json"), "w") as fh:
            json.dump(hb, fh)
        rc, env = self.run_cli("install", "wait-guard", "--timeout", "0", "--proof-version", "2")
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["carriers"], ["argv", "env"])
        hb["install_id"] = "IOTHER000"
        with open(os.path.join(paths.guard_dir(), "heartbeat.json"), "w") as fh:
            json.dump(hb, fh)
        rc, env = self.run_cli("install", "wait-guard", "--timeout", "0")
        self.assertEqual(env["data"]["reason"], "install_id_mismatch")

    def test_pin_hook_line(self):
        """doctor: a guard heartbeat with pin_hook_seen_at null is an informational WARN that names
        allowConversationAccess; the installer never grants it (no config write here)."""
        import datetime
        rc, text = self.run_cli("--human", "install", "pin-hook")
        self.assertEqual((rc, text.strip()), (0, "info  guard tool pin hook: unknown (no guard heartbeat of this install)"))
        hb = {"install_id": paths.home()["install_id"], "proof_version": 2, "pin_tool_surface": True,
              "beat_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

        def write(**kw):
            with open(os.path.join(paths.guard_dir(), "heartbeat.json"), "w") as fh:
                json.dump(dict(hb, **kw), fh)

        write()
        rc, text = self.run_cli("--human", "install", "pin-hook")
        self.assertEqual((rc, text.strip()), (0, "info  guard tool pin hook: this guard does not report it"))
        write(pin_hook_seen_at=None)
        rc, text = self.run_cli("--human", "install", "pin-hook")
        self.assertEqual(rc, 0)
        self.assertTrue(text.startswith("WARN  the guard's tool pin hook has never run"), text)
        self.assertIn("hooks.allowConversationAccess", text)
        self.assertIn("the installer does not grant that", text)
        rc, env = self.run_cli("install", "pin-hook")
        self.assertEqual((rc, env["code"], env["data"]["never_seen"]), (0, "OK", True))
        write(pin_hook_seen_at="2026-10-06T10:00:00Z")
        rc, text = self.run_cli("--human", "install", "pin-hook")
        self.assertEqual(text.strip(), "ok    guard tool pin hook ran (last at 2026-10-06T10:00:00Z)")
        write(pin_hook_seen_at=None, pin_tool_surface=False)          # mode N: the pin is off by design
        rc, text = self.run_cli("--human", "install", "pin-hook")
        self.assertEqual(text.strip(), "info  guard tool pin: off (agent mode native)")
        # agents may not ask
        out = io.StringIO()
        rc = cli.main(["install", "pin-hook"], env={"OPENCLAW_SHELL": "1"}, stdin=io.StringIO(""), stdout=out,
                      modules=[install_cmds])
        self.assertEqual(rc, 11, out.getvalue())

    def test_version_check_and_agent_caller_refused(self):
        rc, env = self.run_cli("install", "version-check", "--text", "OpenClaw 2026.9.1")
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        rc, env = self.run_cli("install", "version-check", "--text", "OpenClaw 2026.9.6", "--minimum", "2026.9.7")
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))       # install restarts the Gateway then
        rc, env = self.run_cli("install", "version-check", "--text", "OpenClaw 2026.9.8", "--minimum", "2026.9.7")
        self.assertEqual(rc, 0, env)
        # a proven jobhunter agent (both carriers, python -I) still cannot run any install command
        for argv in (["install", "render-crons"], ["install", "cli-route", "set", "--carriers", "env"],
                     ["install", "verify-exec-policy", "--fix"], ["install", "run-preflight", "jobhunter:scout"]):
            rc, env = helpers.agent_cli("jobhunter-applier", argv, modules=[install_cmds])
            self.assertNotEqual(rc, 0, argv)
            self.assertEqual(env["code"], "E_CALLER_NOT_ALLOWED", argv)
        self.assertEqual(ins.read_cli_route()["carriers"], ["argv", "env"])
        # an agent id without the guard's proof is refused before any command runs
        out = io.StringIO()
        rc = cli.main(["install", "render-crons"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-applier"},
                      stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        self.assertNotEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["code"], "E_AUTH_FAILED")


def write_shared_skill(root: str, name: str = "jobhunter-control", extra: str = "") -> str:
    d = os.path.join(root, ins.SHARED_SKILLS_DIR, name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "SKILL.template.md")
    with open(path, "w") as fh:
        fh.write("---\nname: %s\ndescription: Fictional shared skill\n---\nRun __PY__ __REPO__/scripts/jh.py status "
                 "--human%s\n" % (name, extra))
    return path


class TestChecksAndSharedSkills(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="jh-shared-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_sqlite_minimum(self):
        self.assertTrue(ins.sqlite_ok((3, 24, 0)))
        self.assertTrue(ins.sqlite_ok((3, 45, 1)))
        self.assertFalse(ins.sqlite_ok((3, 23, 1)))
        self.assertFalse(ins.sqlite_ok((2, 99, 0)))

    def test_render_shared_skills_in_place(self):
        write_shared_skill(self.root)
        res = ins.render_shared_skills(repo=self.root, py="/usr/bin/python3", root=self.root)
        out = os.path.join(self.root, "shared-skills", "jobhunter-control", "SKILL.md")
        self.assertEqual(res["rendered"], [out])
        with open(out) as fh:
            text = fh.read()
        self.assertIn("/usr/bin/python3 %s/scripts/jh.py status --human" % self.root, text)
        self.assertIsNone(ins.PLACEHOLDER_RE.search(text.replace(self.root, "")))   # a temp name may hold "__"
        # the extraDirs entry that --chat-control adds is exactly the folder that now holds SKILL.md
        self.assertEqual(ins.extra_dirs_patch([], self.root)[1], [res["dir"]])
        # removal touches only the rendered SKILL.md, never the template or other files
        with open(os.path.join(self.root, "shared-skills", "jobhunter-control", "notes.txt"), "w") as fh:
            fh.write("mine\n")
        self.assertEqual(ins.remove_shared_skills(repo=self.root, root=self.root), [out])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "shared-skills", "jobhunter-control"))),
                         ["SKILL.template.md", "notes.txt"])
        self.assertEqual(ins.remove_shared_skills(repo=self.root, root=self.root), [])

    def test_unknown_placeholder_refuses_before_writing(self):
        write_shared_skill(self.root, "jobhunter-a")
        write_shared_skill(self.root, "jobhunter-b", extra=" in __WS__")
        with self.assertRaises(Denied) as cm:
            ins.render_shared_skills(repo=self.root, py="/p", root=self.root)
        self.assertEqual(cm.exception.code, "E_VALIDATION")
        self.assertEqual(cm.exception.data["placeholders"], ["__WS__"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "shared-skills", "jobhunter-a", "SKILL.md")))

    def test_no_shared_template_refuses(self):
        with self.assertRaises(Denied):
            ins.render_shared_skills(repo=self.root, py="/p", root=self.root)

    def test_the_committed_shared_skills_render_completely(self):
        names = ins.shared_skill_names(paths.REPO)
        self.assertIn("jobhunter-control", names)
        res = ins.render_shared_skills(repo=self.root, py="/usr/bin/python3", root=paths.REPO)
        self.assertEqual(len(res["rendered"]), len(names))
        for f in res["rendered"]:
            with open(f) as fh:
                text = fh.read()
            self.assertEqual(ins.PLACEHOLDER_RE.findall(text), [], f)
            self.assertIn(self.root + "/scripts/jh.py", text)

    def test_resolve_upload_root(self):
        repo = os.path.join(self.root, "repo")
        os.makedirs(repo)
        default = os.path.realpath(ins.DEFAULT_UPLOAD_ROOT)
        self.assertEqual(ins.resolve_upload_root(repo=repo), default)
        tmp = os.path.join(self.root, "gw-tmp")
        self.assertEqual(ins.resolve_upload_root(tmpdirs=[tmp], repo=repo), default)   # not there: the default
        os.makedirs(os.path.join(tmp, "openclaw", "uploads"))
        found = os.path.join(tmp, "openclaw", "uploads")
        self.assertEqual(ins.resolve_upload_root(tmpdirs=["", tmp], repo=repo), found)
        self.assertEqual(ins.resolve_upload_root(current="/srv/up", tmpdirs=[tmp], repo=repo),
                         os.path.realpath("/srv/up"))
        self.assertEqual(ins.resolve_upload_root(explicit=found, current="/srv/up", repo=repo), found)
        for bad in ("relative/uploads", "/", os.path.join(repo, "uploads"), repo):
            with self.assertRaises(Denied, msg=bad):
                ins.resolve_upload_root(explicit=bad, repo=repo)


class TestInstallHelperCli(HomeTestCase):
    """check-sqlite, render-shared-skills, upload-root, mail-state and forget-mail-secret."""

    def setUp(self):
        super().setUp()
        self.src = SourceTree()
        write_shared_skill(self.src.root)
        self.conn.close()
        self.repo = paths.home()["repo"]

    def tearDown(self):
        from jobhunter import mail
        mail._security_runner = None
        self.src.close()
        super().tearDown()

    def run_cli(self, *argv):
        out = io.StringIO()
        rc = cli.main(list(argv), env={}, stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        text = out.getvalue()
        return rc, (json.loads(text) if text.startswith("{") else text)

    def test_check_sqlite(self):
        rc, env = self.run_cli("install", "check-sqlite")
        self.assertEqual(rc, 0, env)
        orig = ins.sqlite_ok
        ins.sqlite_ok = lambda version_info=None: False
        try:
            rc, env = self.run_cli("install", "check-sqlite")
        finally:
            ins.sqlite_ok = orig
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        self.assertIn("3.24.0", env["message"])

    def test_render_shared_skills_follows_chat_control(self):
        out = os.path.join(self.repo, "shared-skills", "jobhunter-control", "SKILL.md")
        rc, env = self.run_cli("install", "render-shared-skills", "--if-enabled")
        self.assertEqual((rc, env["code"]), (0, "NOTHING_TO_DO"))
        self.assertFalse(os.path.exists(out))
        rc, env = self.run_cli("install", "render-shared-skills")
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["rendered"], [out])
        with open(out) as fh:
            self.assertIn("/usr/bin/python3 %s/scripts/jh.py" % self.repo, fh.read())
        f = os.path.join(self.home.dir, "cur.json")
        with open(f, "w") as fh:
            fh.write("[]")
        rc, env = self.run_cli("install", "render-extradirs", "--current", f)     # records shared_skills_dir
        self.assertEqual(rc, 0, env)
        os.remove(out)
        rc, env = self.run_cli("install", "render-shared-skills", "--if-enabled")  # a later ./install.sh re-run
        self.assertEqual((rc, env["data"]["rendered"]), (0, [out]))
        rc, env = self.run_cli("install", "render-shared-skills", "--remove")
        self.assertEqual((rc, env["data"]["removed"]), (0, [out]))
        self.assertFalse(os.path.exists(out))
        self.assertIsNone(ins.read_manifest().get("shared_skills_dir"))
        rc, env = self.run_cli("install", "render-shared-skills", "--if-enabled")
        self.assertEqual(env["code"], "NOTHING_TO_DO")

    def test_upload_root_recorded_in_home(self):
        from jobhunter import resume as R
        rc, text = self.run_cli("--human", "install", "upload-root")
        self.assertEqual(rc, 0, text)
        self.assertEqual(paths.home()["upload_root"], os.path.realpath(ins.DEFAULT_UPLOAD_ROOT))
        other = tempfile.mkdtemp(prefix="jh-gw-")
        self.addCleanup(shutil.rmtree, other, True)
        mine = os.path.join(other, "gw", "uploads")
        rc, env = self.run_cli("install", "upload-root", "--dir", mine)
        self.assertEqual(rc, 0, env)
        self.assertEqual(paths.home()["upload_root"], os.path.realpath(mine))
        self.assertEqual(ins.read_manifest()["upload_root"], os.path.realpath(mine))
        self.assertEqual(R.upload_root(), os.path.realpath(mine))              # what resume stage uses (U4)
        rc, env = self.run_cli("install", "upload-root", "--tmpdir", "/nonexistent")  # a re-run keeps it
        self.assertEqual(env["data"]["upload_root"], os.path.realpath(mine))
        rc, env = self.run_cli("install", "upload-root", "--dir", os.path.join(self.repo, "uploads"))
        self.assertEqual(env["code"], "E_VALIDATION")
        self.assertNotEqual(rc, 0)

    def _secrets(self, doc):
        with open(os.path.join(paths.private_dir(), "secrets.json"), "w") as fh:
            json.dump(doc, fh)

    def test_mail_state(self):
        rc, text = self.run_cli("--human", "install", "mail-state")
        self.assertEqual((rc, text.strip()), (0, "route=web_ui connected=0"))      # the default route
        with open(os.path.join(paths.private_dir(), "config.json"), "w") as fh:
            json.dump({"gmail": {"route": "app_password"}}, fh)                    # the optional route
        rc, text = self.run_cli("--human", "install", "mail-state")
        self.assertEqual((rc, text.strip()), (0, "route=app_password connected=0"))
        self._secrets({"mail_account": "alex.rivera@example.com", "mail_store": "file",
                       "mail_app_password": "abcdefghijklmnop"})
        rc, text = self.run_cli("--human", "install", "mail-state")
        self.assertEqual(text.strip(), "route=app_password connected=0")        # never tested: not connected
        conn = db.connect()
        try:
            with db.tx(conn):
                db.meta_set(conn, "mail_connected_at", "2026-09-27T05:00:00Z", "human")
        finally:
            conn.close()
        rc, text = self.run_cli("--human", "install", "mail-state")
        self.assertEqual(text.strip(), "route=app_password connected=1")
        with open(os.path.join(paths.private_dir(), "config.json"), "w") as fh:
            json.dump({"gmail": {"route": "web_ui"}}, fh)
        rc, text = self.run_cli("--human", "install", "mail-state")
        self.assertTrue(text.strip().startswith("route=web_ui "), text)

    def test_forget_mail_secret(self):
        from jobhunter import mail
        calls = []
        mail._security_runner = lambda args, stdin=None: (calls.append(list(args)) or (0, b""))
        rc, env = self.run_cli("install", "forget-mail-secret")
        self.assertEqual((rc, env["code"], calls), (0, "NOTHING_TO_DO", []))    # nothing recorded
        self._secrets({"mail_account": "alex.rivera@example.com", "mail_store": "file",
                       "mail_app_password": "abcdefghijklmnop"})
        rc, env = self.run_cli("install", "forget-mail-secret")
        self.assertEqual((env["code"], calls), ("NOTHING_TO_DO", []))           # file store: purge deletes it
        self._secrets({"mail_account": "alex.rivera@example.com", "mail_store": "keychain"})
        rc, env = self.run_cli("install", "forget-mail-secret")
        self.assertEqual(rc, 0, env)
        self.assertTrue(env["data"]["deleted"])
        service = "openclaw-job-hunter.%s" % paths.home()["install_id"]
        self.assertEqual(calls, [["delete-generic-password", "-a", "alex.rivera@example.com", "-s", service]])
        mail._security_runner = lambda args, stdin=None: (44, b"")
        rc, env = self.run_cli("install", "forget-mail-secret")
        self.assertEqual((rc, env["code"], env["data"]["deleted"]), (0, "NOTHING_TO_DO", False))


def listed(job_spec_kw: dict, crons: dict, *, enabled: bool = False) -> dict:
    """A `cron list --all --json` document whose rows carry exactly what install declared (OpenClaw 2026.9.5
    field names), built from the declarations, not from the code under test."""
    m = {"PY": job_spec_kw["py"], "REPO": job_spec_kw["repo"], "WS_ROOT": job_spec_kw["ws_root"]}

    def sub(t):
        for k, v in m.items():
            t = t.replace("{%s}" % k, v)
        return t
    rows = []
    for i, j in enumerate(ins.declared_jobs(crons)):
        row = {"id": "job-%03d" % i, "name": j["name"], "declarationKey": j["key"], "enabled": enabled,
               "delivery": {"mode": "none"}}
        if j["kind"] == "command":
            row["payload"] = {"kind": "command", "argv": ["/usr/bin/env", "-u", "CLAUDECODE", "-u",
                                                          "CLAUDE_CODE_ENTRYPOINT"] + [sub(a) for a in j["argv"]],
                              "cwd": m["REPO"], "env": {},
                              "timeoutSeconds": j["timeout_s"]}
        else:
            row.update(agentId=j["agent"], sessionTarget="isolated")
            row["payload"] = {"kind": "agentTurn", "message": sub(j["message"]), "model": j["model"],
                              "fallbacks": [f for f in j["fallbacks"].split(",") if f], "thinking": j["thinking"],
                              "timeoutSeconds": j["timeout_s"], "toolsAllow": j["tools"].split(",")}
        rows.append(row)
    return {"jobs": rows}


KW = {"py": "/usr/bin/python3", "repo": "/opt/jh/openclaw-job-hunter", "ws_root": "/w/ws"}


class TestCronSpecsAndDrift(unittest.TestCase):
    def setUp(self):
        self.crons = ins.load_crons(paths.REPO)
        self.doc = listed(KW, self.crons)
        self.specs = ins.cron_specs(self.crons, **KW)

    def row(self, key):
        return [r for r in self.doc["jobs"] if r["declarationKey"] == key][0]

    def test_spec_shapes(self):
        ev = self.specs["jobhunter:evaluate"]
        self.assertEqual(set(ev), {"agent", "session", "kind", "tools", "model", "fallbacks", "thinking", "timeout_s",
                                   "message_sha256", "delivery"})
        self.assertEqual((ev["agent"], ev["session"], ev["kind"], ev["tools"], ev["fallbacks"], ev["delivery"]),
                         ("jobhunter-evaluator", "isolated", "agent", ["exec", "read", "write"], [], "none"))
        msg = self.row("jobhunter:evaluate")["payload"]["message"]
        import hashlib
        self.assertEqual(ev["message_sha256"], hashlib.sha256(msg.encode()).hexdigest())
        ap = self.specs["jobhunter:apply"]
        self.assertEqual((ap["tools"], ap["fallbacks"]), (["browser", "exec", "read", "write"],
                                                          ["anthropic/claude-sonnet-5"]))
        d = self.specs["jobhunter:dispatch"]
        self.assertEqual(set(d), {"kind", "argv", "cwd", "env_sha256", "timeout_s"})
        self.assertEqual(d["argv"], list(ins.COMMAND_ENV_PREFIX) + ["/usr/bin/python3",
                                     "/opt/jh/openclaw-job-hunter/scripts/jh.py", "dispatch", "tick", "--quiet"])
        self.assertEqual(d["env_sha256"], hashlib.sha256(b"{}").hexdigest())
        qc = self.specs["jobhunter:qc-review"]
        self.assertEqual((qc["kind"], qc["tools"], qc["message_sha256"], qc["agent"]),
                         ("agent-oneshot", [], None, "jobhunter-qc"))
        self.assertEqual(ins.cron_specs(self.crons, qc_reply="file", **KW)["jobhunter:qc-review"]["tools"], ["write"])

    def test_no_drift_when_listing_matches(self):
        self.assertEqual(ins.cron_drift(self.crons, self.doc, **KW), {})
        res = ins.repair_commands(self.crons, self.doc, tz="UTC", **KW)
        self.assertEqual(res, {"commands": [], "drift": {}})

    def test_every_field_is_compared(self):
        changes = [
            ("jobhunter:scout", lambda r: r["payload"].pop("toolsAllow"), "tools"),               # --clear-tools
            ("jobhunter:scout", lambda r: r["payload"].update(toolsAllow=["*"]), "tools"),
            ("jobhunter:evaluate", lambda r: r["payload"].update(message="do something else"), "message_sha256"),
            ("jobhunter:apply", lambda r: r["payload"].update(model="other/model"), "model"),
            ("jobhunter:apply", lambda r: r["payload"].update(fallbacks=[]), "fallbacks"),
            ("jobhunter:replies", lambda r: r.update(agentId="jobhunter-scout"), "agent"),
            ("jobhunter:replies", lambda r: r.update(sessionTarget="main"), "session"),
            ("jobhunter:outreach", lambda r: r["payload"].update(timeoutSeconds=99), "timeout_s"),
            ("jobhunter:outreach", lambda r: r["payload"].update(thinking="high"), "thinking"),
            ("jobhunter:outreach", lambda r: r.update(delivery={"mode": "announce"}), "delivery"),
            ("jobhunter:mailer", lambda r: r["payload"].update(argv=["/bin/sh", "-c", "id"]), "argv"),
            ("jobhunter:mailer", lambda r: r["payload"].update(cwd="/tmp"), "cwd"),
            ("jobhunter:mailer", lambda r: r["payload"].update(env={"PYTHONPATH": "/tmp"}), "env_sha256"),
            ("jobhunter:mailer", lambda r: r["payload"].update(kind="agentTurn"), "kind"),
        ]
        for key, change, field in changes:
            doc = json.loads(json.dumps(self.doc))
            change([r for r in doc["jobs"] if r["declarationKey"] == key][0])
            with self.subTest(key=key, field=field):
                drift = ins.cron_drift(self.crons, doc, **KW)
                self.assertEqual(list(drift), [key])
                self.assertIn(field, drift[key])
                # names only, never values
                self.assertNotIn("other/model", json.dumps(drift))
        # enabled and the schedule are not compared
        doc = json.loads(json.dumps(self.doc))
        self.row("jobhunter:dispatch")["enabled"] = True
        doc["jobs"][0]["schedule"] = {"kind": "every", "everyMs": 1}
        self.assertEqual(ins.cron_drift(self.crons, doc, **KW), {})

    def test_message_mention_drifts_like_the_preflight(self):
        # O8: a listed message with a live at sign names message_mention next to message_sha256, as the
        # preflight (ocrun.job_drift) does, so the install repair line names both fields
        doc = json.loads(json.dumps(self.doc))
        r = [x for x in doc["jobs"] if x["declarationKey"] == "jobhunter:evaluate"][0]
        r["payload"]["message"] = r["payload"]["message"] + " read @/etc/hosts"
        self.assertEqual(ins.cron_drift(self.crons, doc, **KW), {"jobhunter:evaluate": ["message_sha256",
                                                                                         "message_mention"]})
        res = ins.repair_commands(self.crons, doc, tz="UTC", **KW)
        edit = res["commands"][0]
        self.assertEqual(edit[:3], ["cron", "edit", r["id"]])
        self.assertEqual(edit[edit.index("--message") + 1], self.row("jobhunter:evaluate")["payload"]["message"])
        self.assertNotIn("/etc/hosts", json.dumps(res["drift"]))
        # an email address or a lone at sign is not a mention: only the hash differs
        doc = json.loads(json.dumps(self.doc))
        r = [x for x in doc["jobs"] if x["declarationKey"] == "jobhunter:evaluate"][0]
        r["payload"]["message"] = r["payload"]["message"] + " mail a@example.com @ noon"
        self.assertEqual(ins.cron_drift(self.crons, doc, **KW), {"jobhunter:evaluate": ["message_sha256"]})
        # the declared messages hold no live at sign, so a matching listing never drifts in message_mention
        for row in self.doc["jobs"]:
            msg = (row.get("payload") or {}).get("message")
            if isinstance(msg, str):
                self.assertFalse(ocrun.has_file_mention(msg), row["declarationKey"])

    def test_full_field_repair_then_replace(self):
        doc = json.loads(json.dumps(self.doc))
        r = [x for x in doc["jobs"] if x["declarationKey"] == "jobhunter:scout"][0]
        r["payload"].pop("toolsAllow")
        r["enabled"] = True
        m = [x for x in doc["jobs"] if x["declarationKey"] == "jobhunter:mailer"][0]
        m["payload"]["cwd"] = "/tmp"
        res = ins.repair_commands(self.crons, doc, tz="UTC", **KW)
        self.assertEqual(sorted(res["drift"]), ["jobhunter:mailer", "jobhunter:scout"])
        mailer, scout = res["commands"]
        self.assertEqual(scout[:3], ["cron", "edit", r["id"]])
        for flag, val in (("--agent", "jobhunter-scout"), ("--session", "isolated"),
                          ("--tools", "exec,read,write,browser"), ("--model", "anthropic/claude-sonnet-5"),
                          ("--thinking", "low"), ("--timeout-seconds", "1500")):
            self.assertEqual(scout[scout.index(flag) + 1], val, flag)
        self.assertIn("--clear-fallbacks", scout)
        self.assertIn("--no-deliver", scout)
        self.assertEqual(scout[scout.index("--message") + 1], self.row("jobhunter:scout")["payload"]["message"])
        self.assertEqual(mailer[:3], ["cron", "edit", m["id"]])
        self.assertEqual(json.loads(mailer[mailer.index("--command-argv") + 1]),
                         self.row("jobhunter:mailer")["payload"]["argv"])
        self.assertEqual(mailer[mailer.index("--command-cwd") + 1], KW["repo"])
        rep = ins.repair_commands(self.crons, doc, tz="UTC", replace=True, **KW)["commands"]
        self.assertEqual(rep[0], ["cron", "rm", m["id"]])
        self.assertEqual(rep[1][:2], ["cron", "add"])
        self.assertIn("--disabled", rep[1])                              # was disabled: stays disabled
        self.assertEqual(rep[2], ["cron", "rm", r["id"]])
        self.assertNotIn("--disabled", rep[3])                           # was enabled: re-added enabled
        self.assertEqual(rep[3][rep[3].index("--tools") + 1], "exec,read,write,browser")


class TestCronRepairCli(HomeTestCase):
    def setUp(self):
        super().setUp()
        self.conn.close()
        self.h = paths.home()
        self.kw = {"py": self.h["python"], "repo": self.h["repo"], "ws_root": self.h["ws_root"]}
        self.crons = ins.load_crons()

    def run_cli(self, *argv):
        out = io.StringIO()
        rc = cli.main(list(argv), env={}, stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        text = out.getvalue()
        return rc, (json.loads(text) if text.startswith("{") else text)

    def write(self, doc):
        f = os.path.join(self.home.dir, "cron.json")
        with open(f, "w") as fh:
            json.dump(doc, fh)
        return f

    def test_repair_lines_and_check(self):
        doc = listed(self.kw, self.crons)
        f = self.write(doc)
        rc, text = self.run_cli("--human", "install", "render-crons", "--repair", f)
        self.assertEqual((rc, text.strip()), (0, ""))
        rc, env = self.run_cli("install", "render-crons", "--repair", f, "--check")
        self.assertEqual(rc, 0, env)
        doc["jobs"][-1]["payload"]["message"] = "changed by someone"
        f = self.write(doc)
        rc, text = self.run_cli("--human", "install", "render-crons", "--repair", f)
        self.assertEqual(rc, 0, text)
        lines = [l for l in text.strip().split("\n") if l]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("cron edit %s " % doc["jobs"][-1]["id"]))
        rc, env = self.run_cli("install", "render-crons", "--repair", f, "--check")
        self.assertEqual((rc, env["code"]), (11, "E_CRON_DRIFT"))
        self.assertNotIn("changed by someone", json.dumps(env))
        rc, env = self.run_cli("install", "render-crons", "--check")
        self.assertEqual(env["code"], "E_USAGE")

    def test_report_names_what_differs_and_what_is_missing(self):
        doc = listed(self.kw, self.crons)
        rc, text = self.run_cli("--human", "install", "render-crons", "--repair", self.write(doc), "--report")
        self.assertEqual((rc, text.strip()), (0, ""))
        rc, text = self.run_cli("--human", "install", "render-crons", "--repair", self.write({"jobs": []}), "--report")
        self.assertEqual((rc, text.strip()), (0, ""))                        # a first install: nothing is missing
        rows = {r["declarationKey"]: r for r in doc["jobs"]}
        rows["jobhunter:scout"]["payload"].pop("toolsAllow")
        rows["jobhunter:evaluate"]["payload"]["message"] = "changed by someone"
        rows["jobhunter:evaluate"]["payload"]["model"] = "anthropic/other"
        rows["jobhunter:apply"]["payload"]["message"] = "read @/etc/hosts"
        doc["jobs"] = [r for r in doc["jobs"] if r["declarationKey"] != "jobhunter:replies"]
        rc, text = self.run_cli("--human", "install", "render-crons", "--repair", self.write(doc), "--report")
        self.assertEqual(rc, 0, text)
        self.assertEqual(text.strip().split("\n"), [
            "jobhunter:apply (message_sha256, message_mention differed from its declaration)",
            "jobhunter:evaluate (model, message_sha256 differed from its declaration)",
            "jobhunter:scout (tools differed from its declaration)",
            "jobhunter:replies (it was missing)"])
        self.assertNotIn("changed by someone", text)
        self.assertNotIn("/etc/hosts", text)
        rc, env = self.run_cli("install", "render-crons", "--repair", self.write(doc), "--report")
        self.assertEqual(env["data"]["missing"], ["jobhunter:replies"])
        self.assertEqual(sorted(env["data"]["drift"]), ["jobhunter:apply", "jobhunter:evaluate", "jobhunter:scout"])
        for bad in (("--report",), ("--repair", self.write(doc), "--report", "--check"),
                    ("--repair", self.write(doc), "--report", "--replace")):
            rc, env = self.run_cli("install", "render-crons", *bad)
            self.assertEqual(env["code"], "E_USAGE", bad)

    def test_cron_id(self):
        doc = listed(self.kw, self.crons)
        doc["jobs"][0]["enabled"] = True                                   # jobhunter:dispatch
        f = self.write(doc)
        rc, text = self.run_cli("--human", "install", "cron-id", "jobhunter:dispatch", "--from-list", f, "--if-enabled")
        self.assertEqual((rc, text.strip()), (0, "job-000"))
        doc["jobs"][0]["enabled"] = False
        f = self.write(doc)
        rc, text = self.run_cli("--human", "install", "cron-id", "jobhunter:dispatch", "--from-list", f, "--if-enabled")
        self.assertEqual((rc, text.strip()), (0, ""))
        rc, env = self.run_cli("install", "cron-id", "jobhunter:dispatch")
        self.assertEqual(env["code"], "E_NOT_FOUND")
        self.assertIn("run ./install.sh again", env["message"])
        rc, env = self.run_cli("install", "cron-id", "jobhunter:nope")
        self.assertEqual(env["code"], "E_NOT_FOUND")

    def test_run_preflight_goes_through_ocrun(self):
        seen = []

        def fake(key):
            seen.append(key)
            if key == "jobhunter:scout":
                raise Denied("E_CRON_DRIFT", "jobhunter:scout differs from the install manifest: tools")
            return "job-007"
        with mock.patch.object(ocrun, "preflight", fake, create=True):
            rc, text = self.run_cli("--human", "install", "run-preflight", "jobhunter:probe-evaluator")
            self.assertEqual((rc, text.strip()), (0, "job-007"))
            rc, text = self.run_cli("--human", "install", "run-preflight", "jobhunter:onboard-salary", "--with-wait")
            self.assertEqual((rc, text.strip()), (0, "job-007\t20m"))          # 900 s plus 5 minutes
            rc, text = self.run_cli("--human", "install", "run-preflight", "jobhunter:scout", "--with-wait")
            self.assertEqual(rc, 11)
            rc, env = self.run_cli("install", "run-preflight", "jobhunter:qc-review")
            self.assertEqual(env["code"], "E_USAGE")
        self.assertEqual(seen, ["jobhunter:probe-evaluator", "jobhunter:onboard-salary", "jobhunter:scout"])
        with mock.patch.object(ocrun, "preflight", None, create=True):
            rc, env = self.run_cli("install", "run-preflight", "jobhunter:probe-scout")
            self.assertEqual(env["code"], "E_PRECONDITION")                   # never skipped silently


class TestCliRoute(HomeTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        rc = cli.main(list(argv), env={}, stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        text = out.getvalue()
        return rc, (json.loads(text) if text.startswith("{") else text)

    def test_round_trip(self):
        self.assertEqual(ins.read_cli_route(), {"carriers": ["argv", "env"], "qc_reply": "run",
                                                "cli_tools": "restricted", "accepted_reduced_protection": False})
        rc, text = self.run_cli("--human", "install", "cli-route", "set", "--carriers", "env", "--qc-reply", "file")
        self.assertEqual((rc, text.strip()), (0, "carriers=env qc_reply=file cli_tools=restricted"))
        self.assertEqual(paths.home()["cli_route"]["carriers"], ["env"])      # what jh.py requires (U1)
        from jobhunter import auth
        self.assertEqual(auth.required_carriers(), ["env"])
        rc, text = self.run_cli("--human", "install", "cli-route", "get", "qc_reply")
        self.assertEqual(text.strip(), "file")
        rc, env = self.run_cli("install", "cli-route", "set", "--cli-tools", "native")
        self.assertEqual(env["code"], "E_USAGE")                              # both flags or nothing
        rc, env = self.run_cli("install", "cli-route", "set", "--cli-tools", "native", "--i-accept-reduced-protection")
        self.assertEqual(rc, 0, env)
        self.assertEqual((env["data"]["cli_tools"], env["data"]["accepted_reduced_protection"]), ("native", True))
        rc, env = self.run_cli("install", "cli-route", "set", "--cli-tools", "restricted")
        self.assertEqual((env["data"]["cli_tools"], env["data"]["accepted_reduced_protection"]), ("restricted", False))
        ins.set_cli_route(qc_reply="run", carriers="argv+env")
        self.assertEqual(ins.read_cli_route()["carriers"], ["argv", "env"])

    def test_native_mode_uses_the_argv_carrier_only(self):
        """Mode N: Claude Code's own Bash never gets the env proof (resolve_exec_env does not run for it), so every
        jh.py call would be refused "missing env proof" with the env carrier. Native selects argv on its own and
        refuses env named with it; every check goes through install.check_cli_tools."""
        rc, env = self.run_cli("install", "cli-route", "set", "--cli-tools", "native", "--i-accept-reduced-protection")
        self.assertEqual(rc, 0, env)
        self.assertEqual((env["data"]["cli_tools"], env["data"]["carriers"]), ("native", ["argv"]))
        from jobhunter import auth
        self.assertEqual(auth.required_carriers(), ["argv"])
        for carriers in ("env", "argv+env"):
            with self.subTest(carriers=carriers):
                rc, env = self.run_cli("install", "cli-route", "set", "--carriers", carriers)       # stays native
                self.assertEqual((rc, env["code"]), (2, "E_USAGE"), env)
                self.assertIn("argv identity carrier", env["message"])
                rc, env = self.run_cli("install", "cli-route", "set", "--carriers", carriers, "--cli-tools", "native",
                                       "--i-accept-reduced-protection")
                self.assertEqual(env["code"], "E_USAGE")
        self.assertEqual(ins.read_cli_route()["carriers"], ["argv"])
        # back to restricted: the carriers can be widened again
        ins.set_cli_route(cli_tools="restricted", carriers="argv+env")
        self.assertEqual(ins.read_cli_route()["carriers"], ["argv", "env"])
        # a stored native route with the env carrier (an install from before this check) is refused, not guessed
        h = paths.home()
        h["cli_route"] = {"carriers": ["argv", "env"], "cli_tools": "native", "accepted_reduced_protection": True}
        ins.write_json(paths.home_file(), h)
        d = self.assertDenied("E_CONFIG_INVALID", ins.read_cli_route)
        self.assertIn("run ./install.sh", d.message)

    def test_check_cli_tools_is_the_one_check(self):
        self.assertEqual(ins.check_cli_tools("restricted"), "restricted")
        self.assertEqual(ins.check_cli_tools("restricted", False, ["argv", "env"]), "restricted")
        self.assertEqual(ins.check_cli_tools("native", True, ["argv"]), "native")
        self.assertEqual(ins.check_cli_tools("native"), "native")        # nothing else to check here
        for args, needle in ((("all",), "restricted or native"), (("native", False), "--i-accept-reduced-protection"),
                             (("native", True, ["env"]), "argv identity carrier"),
                             (("native", None, "argv+env"), "argv identity carrier")):
            with self.subTest(args=args):
                d = self.assertDenied("E_USAGE", ins.check_cli_tools, *args)
                self.assertIn(needle, d.message)
        real = ins.check_cli_tools
        agent = ins.load_agents(paths.REPO)[0]
        for name, fn in (("exec_policy", lambda: ins.exec_policy(agent, "restricted")),
                         ("guard config", lambda: ins.render_guard_config(repo="/r", py="/p", cfg={})),
                         ("read_cli_route", ins.read_cli_route), ("set_cli_route", lambda: ins.set_cli_route(qc_reply="run"))):
            calls = []
            with mock.patch.object(ins, "check_cli_tools", side_effect=lambda *a, **k: calls.append(a) or real(*a, **k)):
                fn()
            with self.subTest(name):
                self.assertTrue(calls)

    def test_model_route_is_recorded_and_falls_back_to_the_last_probe_stamp(self):
        h = paths.home()
        h.pop("model_route", None)
        ins.write_json(paths.home_file(), h)
        rc, env = self.run_cli("install", "model-route", "get")
        self.assertEqual((rc, env["data"]["model_route"]), (0, ""))             # unknown: not guessed
        # an install from before the record: the route of the last passed identity checks
        ins.write_probe_stamp(ins.stamp_versions(openclaw="OpenClaw 2026.9.5", route="api_key"))
        self.assertEqual(ins.recorded_model_route(), "api_key")
        rc, text = self.run_cli("--human", "install", "model-route", "set", "cli")
        self.assertEqual((rc, text.strip()), (0, "cli"))
        self.assertEqual(paths.home()["model_route"], "cli")                    # the record wins over the stamp
        self.assertIn("JH_MODEL_ROUTE=cli", ins.shell_env(paths.home()))
        rc, env = self.run_cli("install", "model-route", "set")
        self.assertEqual(env["code"], "E_USAGE")
        rc, env = self.run_cli("install", "model-route", "set", "bedrock")
        self.assertNotEqual(rc, 0)
        self.assertEqual(ins.shell_env({}).count("JH_MODEL_ROUTE=''"), 1)       # no install: empty

    def test_bad_values_are_refused(self):
        for bad in ({"carriers": []}, {"carriers": ["argv", "pin"]}, {"qc_reply": "chat"}, {"cli_tools": "native"},
                    {"cli_tools": "all"}):
            h = paths.home()
            h["cli_route"] = bad
            ins.write_json(paths.home_file(), h)
            with self.subTest(bad=bad):
                self.assertDenied("E_CONFIG_INVALID", ins.read_cli_route)

    def test_renderers_follow_the_route(self):
        self.conn.close()
        src = SourceTree()
        self.addCleanup(src.close)
        ins.set_cli_route(carriers="env", qc_reply="file")
        rc, env = self.run_cli("install", "render-guard-config")
        self.assertEqual(rc, 0, env)
        with open(env["data"]["path"]) as fh:
            conf = json.load(fh)["plugins"]["entries"]["jobhunter-guard"]["config"]
        self.assertEqual((conf["proofCarriers"], conf["qcVerdictFile"]), (["env"], True))
        self.assertEqual(conf["protectedRoots"]["write"][1], paths.home()["ws_root"])
        f = os.path.join(self.home.dir, "cur.json")
        with open(f, "w") as fh:
            json.dump({"agents": {}}, fh)
        rc, env = self.run_cli("install", "render-approvals", "--current", f)
        with open(env["data"]["path"]) as fh:
            pattern = json.load(fh)["agents"]["jobhunter-scout"]["allowlist"][0]["argPattern"]
        self.assertTrue(pattern.startswith("^-I "))
        self.assertNotIn("--agent-proof", pattern)
        rc, env = self.run_cli("install", "render-agents-patch")
        with open(env["data"]["path"]) as fh:
            qc = json.load(fh)["agents"]["entries"]["jobhunter-qc"]["tools"]
        self.assertEqual(qc["allow"], ["write"])


def eff(security="allowlist", ask="off", mode="allowlist", elevated=False, approvals_ask="off",
        approvals_fallback="deny") -> dict:
    return {"security": security, "ask": ask, "mode": mode, "elevated": elevated, "approvals_ask": approvals_ask,
            "approvals_fallback": approvals_fallback}


class TestExecPolicyChecks(unittest.TestCase):
    def setUp(self):
        self.agents = {a["id"]: a for a in ins.load_agents(paths.REPO)}

    def test_problems(self):
        scout, qc = self.agents["jobhunter-scout"], self.agents["jobhunter-qc"]
        self.assertEqual(ins.exec_policy_problems(scout, eff()), [])
        self.assertEqual(ins.exec_policy_problems(qc, eff("deny", mode="deny")), [])
        self.assertEqual(ins.exec_policy_problems(scout, eff(mode=None)), [])         # mode may be unreported
        for bad in (eff(ask="always"), eff(ask="on-miss"), eff(security="full"), eff(elevated=True),
                    eff(elevated={"enabled": True}), eff(approvals_ask="on-miss"), eff(approvals_fallback="allow"),
                    eff(security=None), eff(ask=None), eff(elevated=None), {}, None, eff(mode="ask")):
            with self.subTest(bad=bad):
                self.assertTrue(ins.exec_policy_problems(scout, bad))                  # fail closed
        self.assertTrue(ins.exec_policy_problems(qc, eff()))                           # qc must be deny
        self.assertEqual(ins.exec_policy_problems(scout, eff("full", mode="full"), "native"), [])

    def test_stale_keys(self):
        scout, qc = self.agents["jobhunter-scout"], self.agents["jobhunter-qc"]
        cur = dict(ins.EXEC_ALLOWLIST, reviewer={"x": 1}, pathPrepend=["/tmp"])
        self.assertEqual(ins.stale_exec_keys(cur, scout), ["pathPrepend", "reviewer"])
        self.assertEqual(ins.stale_exec_keys(dict(ins.EXEC_DENY, strictInlineEval=True), qc), ["strictInlineEval"])
        self.assertEqual(ins.stale_exec_keys(None, qc), [])
        # an older install's security and ask are stale now that the renderer writes mode only
        legacy = dict(ins.EXEC_ALLOWLIST, security="allowlist", ask="off")
        self.assertEqual(ins.stale_exec_keys(legacy, scout), ["ask", "security"])

    def test_rendered_exec_is_mode_only(self):
        """OpenClaw 2026.9.5 and later refuse mode with security or ask in one exec object
        (addExecPolicyModeConflictIssue); a mode alone maps to the same policy: allowlist is security allowlist with
        ask off, deny is deny with ask off, full is full with ask off."""
        for cli_tools, acc in (("restricted", False), ("native", True)):
            for a in self.agents.values():
                with self.subTest(agent=a["id"], cli_tools=cli_tools):
                    pol = ins.exec_policy(a, cli_tools, acc)
                    self.assertNotIn("security", pol)
                    self.assertNotIn("ask", pol)
                    want = "deny" if a["exec_policy"] == "deny" else ("full" if cli_tools == "native" else "allowlist")
                    self.assertEqual(pol["mode"], want)
                    self.assertEqual((pol["safeBins"], pol["safeBinTrustedDirs"]), ([], []))
                    patch_value = ins.exec_patch_value(a, cli_tools, acc)
                    self.assertEqual({k: patch_value[k] for k in ins.LEGACY_EXEC_KEYS}, {"security": None, "ask": None})
                    self.assertEqual({k: v for k, v in patch_value.items() if v is not None}, pol)

    def test_unconfined(self):
        """A per-agent mode replaces security and ask, so it is judged first (security deny next to mode full is
        still unconfined); without a mode, security and ask decide."""
        for e in (eff(), eff(mode="deny"), eff("full", mode="deny", ask="always"), eff("full", mode="allowlist"),
                  eff("deny", ask="always", mode=None), eff("allowlist", mode=None)):
            with self.subTest(eff=e):
                self.assertIsNone(ins.unconfined(e))
        for e, why in ((eff("deny", mode="full"), "mode full"), (eff("deny", mode="auto"), "mode auto"),
                       (eff("deny", mode="ask"), "mode ask"), (eff(mode="auto"), "mode auto"),
                       (eff(ask="on-miss"), "mode allowlist with ask on-miss"),
                       (eff("deny", ask=None), "mode allowlist with ask unknown"), (eff(mode="weird"), "mode weird"),
                       (eff("full", mode=None), "security full"), (eff(ask="on-miss", mode=None), "ask on-miss"),
                       (eff("weird", mode=None), "security weird"), ({}, "exec policy could not be read")):
            with self.subTest(eff=e):
                self.assertEqual(ins.unconfined(e), why)
        self.assertIn("agent main has an unconfined shell", ins.boundary_line("main", "security full"))

    def test_claude_flags(self):
        good = "Options:\n  --tools <tools...>  x\n  --strict-mcp-config  y\n  --setting-sources <sources>  z\n"
        self.assertEqual(ins.claude_flags_missing(good), [])
        self.assertEqual(ins.claude_flags_missing("  --toolsx  --strict-mcp-config\n"), ["--tools", "--setting-sources"])


DOCTOR_98 = {
    "schemaVersion": 1, "ok": False, "checksRun": 37, "checksSkipped": 30, "findings": [
        {"checkId": "core/doctor/browser", "severity": "warning",
         "message": "System browser profile cookie import is enabled (browser.allowSystemProfileImport)."},
        {"checkId": "core/doctor/skill-workshop-tool-policy", "severity": "warning",
         "message": 'agents.entries.jobhunter-applier.tools.allow does not include "skill_workshop".',
         "path": "agents.entries.jobhunter-applier.tools.allow", "target": "jobhunter-applier"},
        {"checkId": "core/doctor/skill-workshop-tool-policy", "severity": "warning",
         "message": 'agents.entries.jobhunter-scout.tools.allow does not include "skill_workshop".',
         "path": "agents.entries.jobhunter-scout.tools.allow"},
        {"checkId": "core/doctor/plugins", "severity": "info", "message": "2 plugins loaded"}]}


class TestClaudeVersionAndDoctorVerdict(unittest.TestCase):
    def test_versions_and_models(self):
        self.assertEqual(ins.parse_claude_version("2.1.270 (Claude Code)"), (2, 1, 270))
        self.assertEqual(ins.parse_claude_version("claude 10.0.3"), (10, 0, 3))
        self.assertIsNone(ins.parse_claude_version("Claude Code"))
        self.assertEqual(ins.model_base("anthropic/claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertEqual(ins.model_base("claude-sonnet-5"), "claude-sonnet-5")
        used = ins.jobhunter_models(ins.load_agents(), ins.load_crons())
        self.assertEqual(used["anthropic/claude-opus-5-5"], ["jobhunter-applier", "jobhunter-outreach"])
        self.assertIn("jobhunter-qc", used["anthropic/claude-sonnet-5"])

    def test_model_check(self):
        agents = [{"id": "jobhunter-scout", "model": "anthropic/claude-sonnet-5", "fallbacks": ["anthropic/m-new"]}]
        crons = {"jobs": [{"key": "jobhunter:x", "kind": "agent", "agent": "jobhunter-qc", "model": "anthropic/m-new",
                           "fallbacks": ""}, {"key": "jobhunter:y", "kind": "command", "model": "anthropic/m-old"}]}
        table = {"m-new": (2, 1, 300), "m-old": (9, 0, 0)}
        res = ins.claude_model_check("2.1.299 (Claude Code)", agents, crons, minimum=table)
        self.assertEqual(res["too_old"], [{"model": "anthropic/m-new", "needs": "2.1.300",
                                           "agents": ["jobhunter-qc", "jobhunter-scout"]}])
        self.assertEqual(res["version"], "2.1.299")
        self.assertEqual(ins.claude_model_check("2.1.300", agents, crons, minimum=table)["too_old"], [])
        res = ins.claude_model_check("", agents, crons, minimum=table)
        self.assertEqual((res["too_old"], [r["model"] for r in res["unknown"]]), ([], ["anthropic/m-new"]))
        self.assertEqual(ins.claude_model_check("2.0.0", agents, crons, minimum={}), {
            "version": "2.0.0", "too_old": [], "unknown": []})

    def test_doctor_verdict_by_severity(self):
        v = ins.doctor_verdict(DOCTOR_98, 1, AGENT_IDS)
        self.assertTrue(v["ok"])                              # ok false and exit 1, but warnings only
        self.assertEqual((len(v["warnings"]), v["info"]), (1, 1))
        self.assertEqual(v["expected"], {"core/doctor/skill-workshop-tool-policy": ["jobhunter-applier",
                                                                                   "jobhunter-scout"]})
        cases = (
            ("error", {"checkId": "c", "severity": "error", "message": "m"}, False),
            ("critical", {"checkId": "c", "severity": "critical", "message": "m"}, False),
            ("no severity", {"checkId": "c", "message": "m"}, False),
            ("not an object", "something broke", False),
            ("warning", {"checkId": "c", "severity": "WARNING", "message": "m"}, True),
            # the expected check only for a jobhunter agent, and only as a warning
            ("other agent", {"checkId": "core/doctor/skill-workshop-tool-policy", "severity": "warning",
                             "message": "m", "target": "main"}, True),
            ("expected as error", {"checkId": "core/doctor/skill-workshop-tool-policy", "severity": "error",
                                   "message": "m", "target": "jobhunter-qc"}, False),
        )
        for name, finding, ok in cases:
            with self.subTest(case=name):
                v = ins.doctor_verdict({"ok": False, "findings": [finding]}, 1, AGENT_IDS)
                self.assertEqual(v["ok"], ok, v)
                if name == "other agent":
                    self.assertEqual((v["expected"], len(v["warnings"])), ({}, 1))
        long = ins.doctor_verdict({"findings": [{"checkId": "c", "severity": "error", "message": "x" * 500}]}, 1,
                                  AGENT_IDS)
        self.assertLessEqual(len(long["lines"][0]), 220)
        # no findings list: the ok field, then the exit status decides
        self.assertTrue(ins.doctor_verdict({"ok": True}, 1, AGENT_IDS)["ok"])
        self.assertFalse(ins.doctor_verdict({"ok": False}, 0, AGENT_IDS)["ok"])
        self.assertTrue(ins.doctor_verdict(None, 0, AGENT_IDS)["ok"])
        v = ins.doctor_verdict(None, 2, AGENT_IDS, ["", "boom", "  "])
        self.assertEqual((v["ok"], v["lines"]), (False, ["boom"]))
        self.assertEqual(ins.doctor_verdict(None, 2, AGENT_IDS)["lines"],
                         ["openclaw doctor --lint --json exited with status 2"])


class TestVerifyExecPolicyCli(HomeTestCase):
    """install verify-exec-policy on top of ocrun.effective_exec (U1): pass, fail and --fix, a global ask always."""

    def setUp(self):
        super().setUp()
        self.conn.close()
        self.effective = {}
        self.config = {}
        self.approvals = None
        self.calls = []

    def fake_run(self, argv, timeout_s):
        self.calls.append(argv)
        args = argv[1:]
        if args[:2] == ["approvals", "get"]:
            doc = self.approvals
            return {"ok": doc is not None, "stdout": json.dumps(doc) if doc is not None else "", "stderr": "",
                    "returncode": 0 if doc is not None else 1, "error": None if doc is not None else "exit 1"}
        if args[:2] == ["config", "get"]:
            aid = args[2].split(".")[2]
            doc = self.config.get(aid)
            return {"ok": doc is not None, "stdout": json.dumps(doc) if doc is not None else "", "stderr": "",
                    "returncode": 0 if doc is not None else 1, "error": None}
        if args[:2] == ["config", "unset"]:
            parts = args[2].split(".")
            self.config[parts[2]].pop(parts[-1])
            if parts[-1] == "ask":
                self.effective[parts[2]]["ask"] = "off"
            return {"ok": True, "stdout": "", "stderr": "", "returncode": 0, "error": None}
        raise AssertionError("unexpected openclaw call %s" % argv)

    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(ocrun, "effective_exec", lambda a: dict(self.effective[a]), create=True), \
                mock.patch.object(ocrun, "run", self.fake_run):
            rc = cli.main(list(argv), env={}, stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        text = out.getvalue()
        return rc, (json.loads(text) if text.startswith("{") else text)

    def good(self):
        for a in ins.load_agents():
            deny = a["exec_policy"] == "deny"
            self.effective[a["id"]] = eff("deny", mode="deny") if deny else eff()
            self.config[a["id"]] = dict(ins.EXEC_DENY if deny else ins.EXEC_ALLOWLIST)
        h = paths.home()
        current = {"version": 1, "defaults": {"autoAllowSkills": True},
                   "agents": {"main": {"security": "full", "allowlist": [{"pattern": "/bin/ls"}]}}}
        self.approvals = ins.merge_approvals(current, ins.load_agents(), repo=h.get("repo") or paths.root(),
                                             py=h["python"], carrier=ins.read_cli_route(h)["carriers"])

    def test_pass(self):
        self.good()
        rc, text = self.run_cli("--human", "install", "verify-exec-policy")
        self.assertEqual(rc, 0, text)
        self.assertIn("ok    jobhunter-qc: security deny, ask off", text)
        self.assertFalse([c for c in self.calls if "unset" in c])

    def test_global_ask_always_fails_closed(self):
        self.good()
        # OpenClaw ranks the global ask above the agent's: the read-back shows it and install stops
        self.effective["jobhunter-evaluator"]["ask"] = "always"
        rc, env = self.run_cli("install", "verify-exec-policy", "--fix")
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        self.assertIn("jobhunter-evaluator", env["message"])
        self.assertIn("ask is always", env["message"])
        self.assertEqual(env["data"]["unset"], [])                         # nothing stale: nothing removed

    def test_fix_unsets_stale_keys_and_reads_back(self):
        self.good()
        self.config["jobhunter-scout"]["reviewer"] = {"mode": "auto"}
        self.config["jobhunter-qc"]["ask"] = "off"
        self.config["jobhunter-qc"]["strictInlineEval"] = True
        rc, env = self.run_cli("install", "verify-exec-policy")
        self.assertEqual(env["code"], "E_PRECONDITION")
        self.assertIn("exec keys the installer does not write: reviewer", env["message"])
        rc, env = self.run_cli("install", "verify-exec-policy", "--fix")
        self.assertEqual(rc, 0, env)
        self.assertEqual(sorted(env["data"]["unset"]), ["agents.entries.jobhunter-qc.tools.exec.ask",
                                                        "agents.entries.jobhunter-qc.tools.exec.strictInlineEval",
                                                        "agents.entries.jobhunter-scout.tools.exec.reviewer"])
        self.assertNotIn("reviewer", self.config["jobhunter-scout"])
        # install.sh prints the human text: every key it removed is named
        self.config["jobhunter-evaluator"]["pathPrepend"] = ["/tmp/x"]
        rc, text = self.run_cli("--human", "install", "verify-exec-policy", "--fix")
        self.assertEqual(rc, 0, text)
        self.assertEqual(text.split("\n")[0], "unset agents.entries.jobhunter-evaluator.tools.exec.pathPrepend (an "
                                              "exec key the installer does not write)")
        self.assertNotIn("/tmp/x", text)
        rc, text = self.run_cli("--human", "install", "verify-exec-policy", "--fix")
        self.assertNotIn("unset", text)

    def test_approvals_add_nothing_for_a_jobhunter_agent(self):
        """OpenClaw prepends agents["*"].allowlist to every agent's allowlist and resolves autoAllowSkills as the
        agent's, else the wildcard's, else defaults' value: the read-back fails on anything that widens L2."""
        self.good()
        rc, env = self.run_cli("install", "verify-exec-policy")
        self.assertEqual(rc, 0, env)                    # defaults autoAllowSkills true: our explicit false wins
        for entry in self.approvals["agents"].values():
            if entry is not self.approvals["agents"]["main"]:
                self.assertIs(entry["autoAllowSkills"], False)
        cases = (
            ("wildcard entry", lambda d: d["agents"].update({"*": {"allowlist": [{"pattern": "/bin/cat"}]}}),
             'agents["*"] adds 1 allowlist entries to every agent (/bin/cat)', AGENT_IDS),
            ("bare wildcard", lambda d: d["agents"].update({"*": {"allowlist": ["*"]}}),
             'agents["*"] adds 1 allowlist entries', AGENT_IDS),
            ("own extra entry", lambda d: d["agents"]["jobhunter-scout"]["allowlist"].append({"pattern": "/bin/cat"}),
             "allowlist entries the installer does not write: /bin/cat", ["jobhunter-scout"]),
            ("qc entry", lambda d: d["agents"]["jobhunter-qc"]["allowlist"].append({"pattern": "/usr/bin/curl"}),
             "allowlist entries the installer does not write: /usr/bin/curl", ["jobhunter-qc"]),
            ("autoAllowSkills via wildcard",
             lambda d: (d["agents"]["jobhunter-evaluator"].pop("autoAllowSkills"),
                        d["agents"].update({"*": {"autoAllowSkills": True}})),
             "autoAllowSkills is true", ["jobhunter-evaluator"]),
            ("autoAllowSkills via defaults", lambda d: d["agents"]["jobhunter-applier"].pop("autoAllowSkills"),
             "autoAllowSkills is true", ["jobhunter-applier"]),
            ("entry replaced", lambda d: d["agents"]["jobhunter-outreach"].update(allowlist=[]),
             "the jh.py allowlist entry is missing", ["jobhunter-outreach"]),
        )
        for name, seed, text, who in cases:
            with self.subTest(case=name):
                self.good()
                seed(self.approvals)
                rc, env = self.run_cli("install", "verify-exec-policy", "--fix")
                self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"), env)
                self.assertIn(text, env["message"])
                failed = sorted(a for a, r in env["data"]["agents"].items() if r["problems"])
                self.assertEqual(failed, sorted(who))
        self.good()
        self.approvals = None                                               # unreadable: fail closed
        rc, env = self.run_cli("install", "verify-exec-policy")
        self.assertEqual(env["code"], "E_PRECONDITION")
        self.assertIn("approvals get --json could not be read", env["message"])

    def test_missing_core_function_refuses(self):
        self.good()
        out = io.StringIO()
        with mock.patch.object(ocrun, "effective_exec", None, create=True):
            rc = cli.main(["install", "verify-exec-policy"], env={}, stdin=io.StringIO(""), stdout=out,
                          modules=[install_cmds])
        self.assertEqual(json.loads(out.getvalue())["code"], "E_PRECONDITION")

    def test_identity_boundary(self):
        self.good()
        self.effective.update({"main": eff("full", mode="full"), "helper": eff(), "ops": eff(ask="on-miss")})
        with mock.patch.object(ocrun, "agents_list", lambda: ["main", "helper", "ops", "jobhunter-scout"], create=True):
            rc, text = self.run_cli("--human", "install", "identity-boundary")
        self.assertEqual(rc, 0, text)                                       # information only: exit 0
        lines = text.strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("agent main has an unconfined shell (mode full)"))
        self.assertTrue(lines[1].startswith("agent ops has an unconfined shell (mode allowlist with ask on-miss)"))


class TestConfineWorkshopForeignJobs(unittest.TestCase):
    """The fail-closed patch of a stopped install, the Skill Workshop switch and jobs outside the manifest."""

    def setUp(self):
        self.agents = ins.load_agents(paths.REPO)

    def test_confine_patch(self):
        self.assertIsNone(ins.confine_patch(self.agents, ["main"]))
        patch = ins.confine_patch(self.agents, ["main", "jobhunter-scout", "jobhunter-qc"])
        self.assertEqual(sorted(patch["agents"]["entries"]), ["jobhunter-qc", "jobhunter-scout"])  # absent: left out
        t = patch["agents"]["entries"]["jobhunter-scout"]["tools"]
        self.assertEqual(t["exec"], {"mode": "deny", "security": None, "ask": None, "safeBins": [],
                                     "safeBinTrustedDirs": []})
        self.assertEqual((t["elevated"], t["fs"]), ({"enabled": False}, {"workspaceOnly": True}))
        self.assertLessEqual({"exec", "read", "write", "browser", "edit", "apply_patch", "process", "cron"},
                             set(t["deny"]))
        self.assertNotIn("allow", t)
        self.assertEqual(ins.agent_ids_from_list([{"id": "main"}, {"id": "jobhunter-qc"}, "x", {}]),
                         ["main", "jobhunter-qc", "x"])
        self.assertEqual(ins.agent_ids_from_list({"agents": [{"id": "a"}]}), ["a"])
        self.assertEqual(ins.agent_ids_from_list(None), [])

    def test_workshop_mode_and_patch(self):
        for text, mode in (('"propose"', "propose"), ("off\n", "off"), ('{"value": "auto"}', "auto"), ("", "auto"),
                           ("garbage", "auto"), ('"sometimes"', "auto"), (None, "auto"), ("'off'", "off")):
            with self.subTest(text=text):
                self.assertEqual(ins.workshop_mode(text), mode)
        self.assertEqual(ins.workshop_patch(), {"skills": {"workshop": {"autonomous": {"mode": "propose"}}}})
        with self.assertRaises(Denied):
            ins.workshop_patch("auto")
        ins.assert_patch_scope({"skills": {"workshop": {"autonomous": {"mode": "off"}}}}, [])
        for bad in ({"skills": {"workshop": {"autonomous": {"mode": "auto"}}}},
                    {"skills": {"workshop": {"approvalPolicy": "auto"}}},
                    {"skills": {"workshop": {"autonomous": {"mode": "propose"}}, "load": {"extraDirs": []}}},
                    {"skills": {"entries": {}}}):
            with self.subTest(bad=bad):
                with self.assertRaises(Denied):
                    ins.assert_patch_scope(bad, [])

    def test_foreign_agent_jobs(self):
        ids = [a["id"] for a in self.agents]
        doc = {"jobs": [
            {"id": "j1", "declarationKey": "jobhunter:scout", "agentId": "jobhunter-scout", "enabled": False},
            {"id": "s1", "declarationKey": "skill-collection-review:jobhunter-scout", "agentId": "jobhunter-scout",
             "enabled": True, "name": "skill-collection-review-jobhunter-scout"},
            {"id": "s2", "declarationKey": "skill-collection-review:main", "agentId": "main", "enabled": True},
            {"id": "q1", "declarationKey": "jobhunter:qc-review-abc", "agentId": "jobhunter-qc", "enabled": False},
            {"id": "h1", "name": "by hand", "agentId": "jobhunter-applier"},
            {"id": "c1", "declarationKey": "jobhunter:mailer", "enabled": True}]}
        got = ins.foreign_agent_jobs(doc, ids, {"jobhunter:scout": "j1", "jobhunter:mailer": "c1"})
        self.assertEqual([(j["id"], j["kind"], j["enabled"]) for j in got],
                         [("h1", "other", True), ("q1", "qc_oneshot", False), ("s1", "skill_review", True)])
        self.assertEqual(ins.foreign_agent_jobs([], ids, {}), [])
        self.assertEqual(ins.foreign_agent_jobs(None, ids, {}), [])


class TestForeignJobsCli(HomeTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        rc = cli.main(list(argv), env={}, stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        text = out.getvalue()
        return rc, (json.loads(text) if text.startswith("{") else text)

    def write(self, name, obj):
        f = os.path.join(self.home.dir, name)
        with open(f, "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))
        return f

    def test_foreign_jobs_check(self):
        ins.merge_manifest({"cron_jobs": {"jobhunter:scout": "j1"}})
        rows = [{"id": "j1", "declarationKey": "jobhunter:scout", "agentId": "jobhunter-scout", "enabled": True},
                {"id": "s1", "declarationKey": "skill-collection-review:jobhunter-qc", "agentId": "jobhunter-qc",
                 "enabled": False}]
        f = self.write("cron.json", {"jobs": rows})
        rc, text = self.run_cli("--human", "install", "foreign-jobs", "--from-list", f, "--check")
        self.assertEqual(rc, 0, text)
        self.assertIn("1 disabled automations outside the install name a jobhunter agent (skill_review)", text)
        rows[1]["enabled"] = True
        f = self.write("cron.json", {"jobs": rows})
        rc, env = self.run_cli("install", "foreign-jobs", "--from-list", f, "--check")
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        self.assertIn("skill-collection-review:jobhunter-qc (s1) runs jobhunter-qc outside the install", env["message"])
        self.assertIn("skills.workshop.autonomous.mode to propose", env["message"])
        rc, env = self.run_cli("install", "foreign-jobs", "--from-list", f)      # without --check: information
        self.assertEqual(rc, 0, env)
        self.assertEqual([j["id"] for j in env["data"]["enabled"]], ["s1"])
        # without --from-list it lists through openclaw (read only); a failed list fails closed
        with mock.patch.object(ocrun, "cron_list", lambda: {"ok": False, "doc": None, "error": "gateway down"}):
            rc, env = self.run_cli("install", "foreign-jobs", "--check")
        self.assertEqual(env["code"], "E_PRECONDITION")
        with mock.patch.object(ocrun, "cron_list", lambda: {"ok": True, "doc": {"jobs": rows[:1]}, "error": None}):
            rc, env = self.run_cli("install", "foreign-jobs", "--check")
        self.assertEqual(rc, 0, env)

    def test_workshop_and_confine_commands(self):
        rc, text = self.run_cli("--human", "install", "workshop-mode", "--current", self.write("w.json", ""))
        self.assertEqual(text.strip(), "auto")
        rc, env = self.run_cli("install", "workshop-mode", "--current", self.write("w.json", '"propose"'))
        self.assertEqual(env["data"], {"mode": "propose", "weekly_reviews": False})
        rc, env = self.run_cli("install", "render-workshop-patch")
        with open(env["data"]["path"]) as fh:
            self.assertEqual(json.load(fh), {"skills": {"workshop": {"autonomous": {"mode": "propose"}}}})
        rc, env = self.run_cli("install", "render-confine-patch", "--present", self.write("a.json", [{"id": "main"}]))
        self.assertEqual((rc, env["code"], env["data"]["path"]), (0, "NOTHING_TO_DO", None))
        rc, env = self.run_cli("install", "render-confine-patch", "--present",
                               self.write("a.json", [{"id": "main"}, {"id": "jobhunter-applier"}]))
        self.assertEqual(env["data"]["agents"], ["jobhunter-applier"])
        with open(env["data"]["path"]) as fh:
            self.assertEqual(json.load(fh)["agents"]["entries"]["jobhunter-applier"]["tools"]["exec"]["mode"], "deny")


class TestProbeStampAndClaudeCheck(HomeTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        rc = cli.main(list(argv), env={}, stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        text = out.getvalue()
        return rc, (json.loads(text) if text.startswith("{") else text)

    def test_stamp(self):
        v = ("--openclaw", "OpenClaw 2026.9.8 (abc)", "--claude", "2.1.0 (Claude Code)")
        rc, env = self.run_cli("install", "probe-stamp", "read", "--match", *v)
        self.assertEqual(env["code"], "E_PRECONDITION")
        rc, text = self.run_cli("--human", "install", "probe-stamp", "read")
        self.assertEqual(text.strip(), "never passed")
        rc, env = self.run_cli("install", "probe-stamp", "write", *v)
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["versions"]["openclaw"], "2026.9.8")
        rc, env = self.run_cli("install", "probe-stamp", "read", "--match", *v)
        self.assertEqual(rc, 0, env)
        # any change runs the probes again: OpenClaw, claude, the identity carriers, the agent mode
        for other in (("--openclaw", "OpenClaw 2026.9.9", "--claude", v[3]), ("--openclaw", v[1], "--claude", "2.2.0")):
            rc, env = self.run_cli("install", "probe-stamp", "read", "--match", *other)
            self.assertEqual(env["code"], "E_PRECONDITION", other)
        ins.set_cli_route(carriers="argv")
        rc, env = self.run_cli("install", "probe-stamp", "read", "--match", *v)
        self.assertEqual(env["code"], "E_PRECONDITION")

    def test_claude_check(self):
        f = os.path.join(self.home.dir, "help.txt")
        with open(f, "w") as fh:
            fh.write("  --print\n  --tools <tools...>\n")
        rc, env = self.run_cli("install", "claude-check", "--help-file", f)
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        self.assertEqual(env["data"]["missing"], ["--strict-mcp-config", "--setting-sources"])
        with open(f, "a") as fh:
            fh.write("  --strict-mcp-config\n  --setting-sources <sources>\n")
        rc, env = self.run_cli("install", "claude-check", "--help-file", f)
        self.assertEqual(rc, 0, env)

    def test_claude_check_compares_the_version_with_the_models(self):
        f = os.path.join(self.home.dir, "help.txt")
        with open(f, "w") as fh:
            fh.write("  --tools <tools...>\n  --strict-mcp-config\n  --setting-sources <sources>\n")
        rc, env = self.run_cli("install", "claude-check", "--help-file", f, "--version-text", "2.1.270 (Claude Code)")
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        self.assertEqual(env["message"], "Claude Code 2.1.270 is too old for anthropic/claude-opus-5-5 of "
                                         "jobhunter-applier, jobhunter-outreach (2.1.280 or newer is needed); "
                                         "update it: claude update, then run ./install.sh again")
        for ok in ("2.1.280 (Claude Code)", "2.2.0 (Claude Code)", "3.0.1"):
            rc, text = self.run_cli("--human", "install", "claude-check", "--help-file", f, "--version-text", ok)
            self.assertEqual((rc, text.strip()), (0, "ok"), ok)
        rc, text = self.run_cli("--human", "install", "claude-check", "--help-file", f, "--version-text", "")
        self.assertEqual(rc, 0, text)                       # unknown version: said, not refused (flags checked)
        self.assertEqual(text.strip(), "the Claude Code version is unknown; anthropic/claude-opus-5-5 of "
                                       "jobhunter-applier, jobhunter-outreach needs 2.1.280 or newer")
        # the flags are checked first
        with open(f, "w") as fh:
            fh.write("  --print\n")
        rc, env = self.run_cli("install", "claude-check", "--help-file", f, "--version-text", "9.9.9")
        self.assertIn("too old for restricted agent runs", env["message"])

    def test_oc_doctor(self):
        def run(doc_text, rc_value=1, err=""):
            f = os.path.join(self.home.dir, "doctor.json")
            e = os.path.join(self.home.dir, "doctor.err")
            with open(f, "w") as fh:
                fh.write(doc_text)
            with open(e, "w") as fh:
                fh.write(err)
            return self.run_cli("--human", "install", "oc-doctor", "--file", f, "--stderr-file", e,
                                "--rc", str(rc_value))
        rc, text = run(json.dumps(DOCTOR_98))
        self.assertEqual(rc, 0, text)
        lines = text.strip().split("\n")
        self.assertEqual(lines[0], "warning core/doctor/browser: System browser profile cookie import is enabled "
                                   "(browser.allowSystemProfileImport).")
        self.assertEqual(lines[-1], "expected core/doctor/skill-workshop-tool-policy for jobhunter-applier, "
                                    "jobhunter-scout (the jobhunter agents never get the skill_workshop tool; "
                                    "install step 7b sets the Skill Workshop to propose)")
        bad = dict(DOCTOR_98, findings=DOCTOR_98["findings"] + [
            {"checkId": "core/doctor/config", "severity": "error", "message": "config is invalid"}])
        rc, text = run("Config warnings: x\n" + json.dumps(bad))
        self.assertEqual(rc, 11, text)
        self.assertTrue(text.startswith("error core/doctor/config: config is invalid\nwarning core/doctor/browser"))
        rc, text = run("not json at all", rc_value=1, err="gateway unreachable\n")
        self.assertEqual(rc, 11)
        self.assertEqual(text.strip(), "not json at all\ngateway unreachable")
        rc, text = run("", rc_value=0)
        self.assertEqual((rc, text.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
