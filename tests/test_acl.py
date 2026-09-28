"""acl.json shape (design 3.3) and consistency with the registered argparse commands.

STRICT = True: every command in an agent's ACL map, and every chat, public_readonly and human_only command, must be
registered by some unit's command module."""
from __future__ import annotations

import argparse
import json
import re
import unittest

import tests  # noqa: F401
from jobhunter import cli, paths

STRICT = True
AGENTS = ("jobhunter-scout", "jobhunter-evaluator", "jobhunter-applier", "jobhunter-outreach", "jobhunter-qc")
LANE_COMMANDS = ("preflight", "cycle end", "home show", "budget", "breaker status")


def load_acl() -> dict:
    with open(paths.ACL_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def parse_class(spec: str) -> tuple[str, bool, list | None]:
    optional = spec.endswith("?")
    base = spec[:-1] if optional else spec
    if base.startswith("enum:"):
        return "enum", optional, base[5:].split("|")
    return base, optional, None


class TestAclShape(unittest.TestCase):
    def setUp(self):
        self.acl = load_acl()

    def test_top_level(self):
        self.assertEqual(self.acl["version"], 3)
        self.assertEqual(tuple(self.acl["agents"]), AGENTS)
        for key in ("public_readonly", "chat", "human_only"):
            self.assertTrue(all(isinstance(x, str) and x for x in self.acl[key]))
        self.assertEqual(self.acl["agents"]["jobhunter-qc"], {"tools": [], "commands": {}})
        self.assertNotIn("browser", self.acl["agents"]["jobhunter-evaluator"]["tools"])

    def test_value_classes_compile(self):
        for name, rx in self.acl["value_classes"].items():
            if rx in ("WORKDIR", "FLAG"):
                continue
            with self.subTest(name=name):
                self.assertTrue(rx.startswith("^") and rx.endswith("$"))
                re.compile(rx)

    def test_argument_schemas(self):
        classes = set(self.acl["value_classes"])
        for agent, spec in self.acl["agents"].items():
            for command, args in spec["commands"].items():
                with self.subTest(agent=agent, command=command):
                    self.assertRegex(command, r"^[a-z]+(-[a-z]+)*( [a-z]+(-[a-z]+)*){0,2}$")
                    positions = sorted(int(k[1:]) for k in args if k.startswith("_"))
                    self.assertEqual(positions, list(range(1, len(positions) + 1)))
                    for flag, cls in args.items():
                        self.assertTrue(flag.startswith("--") or re.match(r"^_[0-9]$", flag), flag)
                        self.assertNotIn(flag, ("--home", "--pin-stdin", "--grant", "--quiet", "--human"))
                        base, _opt, values = parse_class(cls)
                        if base == "enum":
                            self.assertTrue(values and all(re.match(r"^[A-Za-z0-9_.:-]+$", v) for v in values))
                        else:
                            self.assertIn(base, classes)

    def test_agents_have_lane_basics_and_no_human_commands(self):
        forbidden = set(self.acl["human_only"]) | {"approve", "skip", "edit", "config lower", "pause", "unpause",
                                                   "approval set", "breaker reset"}
        for agent in AGENTS[:4]:
            cmds = self.acl["agents"][agent]["commands"]
            for c in LANE_COMMANDS:
                self.assertIn(c, cmds, (agent, c))
            self.assertFalse(forbidden & set(cmds), agent)
            self.assertEqual(cmds["preflight"]["--lane"].split(":")[0], "enum")
        for agent in ("jobhunter-scout", "jobhunter-evaluator"):
            self.assertFalse([c for c in self.acl["agents"][agent]["commands"] if c.startswith("gate ")], agent)

    def test_ids_match_value_classes(self):
        from jobhunter import canon
        vc = self.acl["value_classes"]
        self.assertRegex(canon.new_token(), vc["token"])
        self.assertRegex(canon.new_cycle_id(), vc["cycle"])
        self.assertRegex("em:" + canon.new_token(), vc["thread"])
        self.assertRegex("li:" + canon.new_uid("P"), vc["thread"])
        self.assertRegex("job:" + canon.new_uid("J"), vc["target"])


class TestAclVsArgparse(unittest.TestCase):
    def test_registered_commands_accept_acl_flags(self):
        acl = load_acl()
        parser = cli.build_parser()
        self.assertEqual(cli.discovery_errors(), [])
        registered = cli.registered_commands(parser)
        missing = []
        for agent, spec in acl["agents"].items():
            for command, args in spec["commands"].items():
                leaf = registered.get(command)
                if leaf is None:
                    missing.append(command)
                    continue
                with self.subTest(agent=agent, command=command):
                    self.assertIn("A", leaf.get_default("_jh_callers"))
                    options = {}
                    positionals = []
                    for action in leaf._actions:
                        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                            continue
                        if action.option_strings:
                            for o in action.option_strings:
                                options[o] = action
                        elif not action.dest.startswith("_"):
                            positionals.append(action)
                    for flag, cls in args.items():
                        if flag.startswith("_"):
                            self.assertGreaterEqual(len(positionals), int(flag[1:]), flag)
                            continue
                        self.assertIn(flag, options, flag)
                        base, optional, _ = parse_class(cls)
                        if not optional:
                            self.assertTrue(options[flag].required, "%s should be required" % flag)
                        if base == "flag":
                            self.assertEqual(options[flag].nargs, 0, "%s should be a switch" % flag)
        for command in ("public_readonly", "chat"):
            for c in acl[command]:
                if c in registered:
                    self.assertTrue(set(registered[c].get_default("_jh_callers")) & set("RC"), c)
                elif STRICT:
                    missing.append(c)
        for entry in acl["human_only"]:
            c, _sep, flag = entry.partition(" --")
            if c not in registered:
                if STRICT:
                    missing.append(entry)
                continue
            if flag:      # "command --flag": the flag is human only (auth._human_only_hit), the command is not
                opts = {o for a in registered[c]._actions for o in a.option_strings}
                self.assertIn("--" + flag, opts, entry)
                continue
            callers = set(registered[c].get_default("_jh_callers"))
            self.assertIn("H", callers, entry)
            self.assertNotIn("A", callers, entry)
        if STRICT:
            self.assertEqual(sorted(set(missing)), [])


if __name__ == "__main__":
    unittest.main()
