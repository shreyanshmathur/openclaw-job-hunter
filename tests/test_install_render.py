"""Install renderers (U7): cron commands, workspaces, config patches, approvals, manifest, CLI surface."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import tests  # noqa: F401
from jobhunter import cli, db, install as ins, paths
from jobhunter.commands import install as install_cmds
from jobhunter.errors import Denied
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


class TestDeclarations(unittest.TestCase):
    def test_agents_json_matches_acl_and_roles(self):
        agents = ins.load_agents(paths.REPO)
        self.assertEqual([a["id"] for a in agents], AGENT_IDS)
        with open(paths.ACL_FILE) as fh:
            acl = json.load(fh)
        for a in agents:
            self.assertEqual(sorted(a["tools_allow"]), sorted(acl["agents"][a["id"]]["tools"]), a["id"])
            for denied in ("message", "cron", "process", "gateway", "web_fetch", "web_search"):
                self.assertIn(denied, a["tools_deny"])
        qc = agents[-1]
        self.assertEqual((qc["tools_allow"], qc["skills"]), ([], []))

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
        self.assertEqual(len(keys), 13)
        self.assertEqual(set(ins.LANE_JOBS.values()) - set(keys), set())
        for j in crons["jobs"]:
            if j["kind"] == "agent":
                self.assertEqual(j["schedule"], {"cron": "0 0 1 1 *"})   # never enabled; the dispatcher runs them
                self.assertIn("preflight --lane", j["message"])

    def test_json5_strip(self):
        text = '// c\n{"a": "x // not a comment", /* b */ "b": [1, 2,],\n}\n'
        self.assertEqual(ins.loads_json5(text), {"a": "x // not a comment", "b": [1, 2]})
        tmpl = ins._template("agents.patch.json5.tmpl", paths.REPO)
        self.assertEqual(tmpl["tools"]["fs"], {"workspaceOnly": True})


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
                                        tz="Asia/Kolkata", **kw)

    def test_every_job_is_disabled_silent_and_keyed(self):
        cmds = self.render()
        self.assertEqual(len(cmds), 13)
        for c in cmds:
            self.assertEqual(c[:2], ["cron", "add"])
            self.assertEqual(c[-2:], ["--no-deliver", "--disabled"])
            key = c[c.index("--declaration-key") + 1]
            self.assertTrue(key.startswith("jobhunter:"))
            self.assertNotIn("--announce", c)

    def test_command_job_shape(self):
        mailer = [c for c in self.render() if "jobhunter:mailer" in c][0]
        argv = json.loads(mailer[mailer.index("--command-argv") + 1])
        self.assertEqual(argv, ["/usr/bin/python3", "/opt/jh/openclaw-job-hunter/scripts/jh.py", "mail", "run",
                                "--quiet"])
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
        self.assertTrue(msg.endswith("/usr/bin/python3 /opt/jh/openclaw-job-hunter/scripts/jh.py preflight --lane applier"))
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
        self.assertTrue(e["tools"]["fs"]["workspaceOnly"])
        self.assertEqual(e["tools"]["exec"]["strictInlineEval"], True)
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

    def test_guard_config(self):
        p = ins.render_guard_config(repo="/opt/r", py="/usr/bin/python3", cfg={})
        conf = p["plugins"]["entries"]["jobhunter-guard"]["config"]
        self.assertEqual(conf, {"repo": "/opt/r", "python": "/usr/bin/python3", "homeFile": "/opt/r/private/home.json",
                                "publicReadonlyAgents": ["main"]})
        p = ins.render_guard_config(repo="/opt/r", py="/p",
                                    cfg={"owner": {"notify": {"channel": "whatsapp", "to": "+12025550123"}}})
        self.assertEqual(p["plugins"]["entries"]["jobhunter-guard"]["config"]["ownerFallback"],
                         [{"channel": "whatsapp", "senderId": "+12025550123"}])

    def test_arg_pattern(self):
        rx = re.compile(ins.arg_pattern("/opt/jh/openclaw-job-hunter"))
        base = "/opt/jh/openclaw-job-hunter/scripts/jh.py "
        for ok in ("preflight --lane scout", "gate reserve --kind cold_email --draft DABCDEFG",
                   "--cycle C20260927T041500Z7Q2K --quiet cycle end --cycle C20260927T041500Z7Q2K",
                   "approvals list", "skipper", "draft create --file /w/x.json",
                   "enrich find --contact P1", "enrich find --contact P1 --target contact:P1", "enrich budget",
                   "enrich show P1", "answers lookup --field x", "installer"):
            self.assertTrue(rx.match(base + ok), ok)
        for bad in ("approve A7K2", "skip A7K2", "edit A7K2 --text x", "unpause", "config raise a 1",
                    "breaker reset --scope gmail", "mail connect", "exclusions remove --type company",
                    "exclusions import --deactivate", "companies merge K1 K2", "--cycle C1 approve A7K2",
                    "auth set-pin", "forget --email a@example.com",
                    # email finder (U10): human-only commands, also with an abbreviated --include-reserve
                    "enrich connect hunter", "enrich disconnect --all", "enrich retry P1", "enrich test hunter",
                    "enrich find --contact P1 --include-reserve", "enrich find --include-res --contact P1",
                    "answers add --key k --value v",
                    # browser consent: every install helper, among them consent-record and consent-revoke
                    "install consent-record --grant gmail --method manual_login", "install consent-revoke --all",
                    "--cycle C1 install shell-env"):
            self.assertIsNone(rx.match(base + bad), bad)
        self.assertIsNone(rx.match("/opt/jh/openclaw-job-hunterX/scripts/jh.py status"))
        self.assertIsNone(rx.match("/other/scripts/jh.py status"))
        self.assertIsNone(rx.match("/opt/jh/openclaw-job-hunter/scripts/jhXpy status"))

    def test_merge_approvals_keeps_other_agents(self):
        current = {"version": 1, "defaults": {"security": "full"}, "agents": {"main": {"security": "full"}}}
        doc = ins.merge_approvals(current, self.agents, repo="/opt/r", py="/usr/bin/python3", root=paths.REPO)
        self.assertEqual(doc["defaults"], {"security": "full"})
        self.assertEqual(doc["agents"]["main"], {"security": "full"})
        app = doc["agents"]["jobhunter-applier"]
        self.assertEqual((app["security"], app["ask"], app["askFallback"]), ("allowlist", "on-miss", "deny"))
        self.assertEqual(app["allowlist"][0]["pattern"], "/usr/bin/python3")
        self.assertEqual(app["allowlist"][0]["argPattern"], ins.arg_pattern("/opt/r"))
        self.assertEqual(doc["agents"]["jobhunter-qc"]["security"], "deny")
        again = ins.merge_approvals(doc, self.agents, repo="/opt/r", py="/usr/bin/python3", root=paths.REPO)
        self.assertEqual(again, doc)
        gone = ins.merge_approvals(doc, self.agents, repo="/opt/r", py="/p", remove=True, root=paths.REPO)
        self.assertEqual(sorted(gone["agents"]), ["main"])
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
        self.assertEqual(len(lines), 13)
        self.assertTrue(all(l.startswith("cron add ") for l in lines))

    def test_manifest_from_cron_list(self):
        crons = ins.load_crons()
        listing = {"jobs": [{"id": "j%d" % i, "declarationKey": j["key"], "enabled": False, "name": j["name"]}
                            for i, j in enumerate(crons["jobs"])] + [{"id": "x", "declarationKey": "other:job"}]}
        f = os.path.join(self.home.dir, "cron.json")
        with open(f, "w") as fh:
            json.dump(listing, fh)
        rc, env = self.run_cli("install", "manifest", "--set", f)
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["cron_jobs"]["jobhunter:dispatch"], "j0")
        self.assertNotIn("other:job", env["data"]["cron_jobs"])
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
        hb["install_id"] = "IOTHER000"
        with open(os.path.join(paths.guard_dir(), "heartbeat.json"), "w") as fh:
            json.dump(hb, fh)
        rc, env = self.run_cli("install", "wait-guard", "--timeout", "0")
        self.assertEqual(env["data"]["reason"], "install_id_mismatch")

    def test_version_check_and_agent_caller_refused(self):
        rc, env = self.run_cli("install", "version-check", "--text", "OpenClaw 2026.9.1")
        self.assertEqual((rc, env["code"]), (11, "E_PRECONDITION"))
        out = io.StringIO()
        rc = cli.main(["install", "render-crons"], env={"OPENCLAW_SHELL": "1", "JH_AGENT_ID": "jobhunter-applier"},
                      stdin=io.StringIO(""), stdout=out, modules=[install_cmds])
        self.assertNotEqual(rc, 0)
        self.assertIn(json.loads(out.getvalue())["code"], ("E_CALLER_NOT_ALLOWED", "E_GUARD_MISSING"))


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
        self.assertNotIn("__", text)
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


if __name__ == "__main__":
    unittest.main()
