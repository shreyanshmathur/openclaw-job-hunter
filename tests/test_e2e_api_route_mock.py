"""E2E (INT): the API-key (embedded) route against a scripted local model, through the real guard and jh.py.

CLI-ROUTE-DESIGN 11.3 and 12 (M10, V17). tests/fixtures/e2e/mock_provider.py is an OpenAI-compatible server on
127.0.0.1 (random port, no API key, no network) whose "model" answers with the scripted tool calls of the probe
job plus the T3a, T4a and T5b refusals. The test plays OpenClaw's embedded runtime for one isolated cron turn of
jobhunter-evaluator: it sends the chat-completion requests, hands every tool call to the real jobhunter-guard
runtime (tests/fixtures/e2e/guard_bridge.ts; exec goes through before_tool_call and resolve_exec_env like
OpenClaw's exec tool), runs what the guard allows (exec: the rewritten command as a child `python -I` with
exactly the exec environment; read and write: the file) and returns each result or block reason to the model.
On this route the same code path proves the agent with both carriers: the jh.py calls run as
jobhunter-evaluator, the refusals arrive as guard blocks, and no call is ever `system`. Skipped without Node 24.
The same mock serves the live T12 run on the jhtest profile (see its module doc). Fictional data only.
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import unittest
import urllib.request

import tests  # noqa: F401
from jobhunter import canon, paths
from jobhunter import install as ins
from tests import helpers
from tests.fixtures.e2e.mock_provider import FORGED_PROOF, MockProvider
from tests.fixtures.e2e.support import REPO, World, exec_tool_env, run_jh_child, split_rewritten
from tests.test_e2e_guard_replay import GuardBridge, node_major

EV = "jobhunter-evaluator"
SESSION = "agent:jobhunter-evaluator:cron:probe-evaluator"
TOOLS = [{"type": "function", "function": {"name": name, "description": desc,
                                           "parameters": {"type": "object", "properties": props}}}
         for name, desc, props in (
             ("exec", "Run one command", {"command": {"type": "string"}, "timeoutSeconds": {"type": "number"}}),
             ("read", "Read a file", {"path": {"type": "string"}}),
             ("write", "Write a whole file", {"path": {"type": "string"}, "content": {"type": "string"}}))]


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TestMockProvider(unittest.TestCase):
    """The mock itself: loopback only, stateless turn choice, JSON and SSE answers in the OpenAI shapes."""

    def setUp(self):
        self.mp = MockProvider("probe", {"PY": "/usr/bin/python3", "REPO": "/r", "WS_ROOT": "/w"}).start()
        self.addCleanup(self.mp.stop)

    def test_loopback_turns_and_shapes(self):
        self.assertEqual(self.mp.httpd.server_address[0], "127.0.0.1")
        with urllib.request.urlopen(self.mp.base_url + "/models", timeout=30) as resp:
            self.assertEqual(json.loads(resp.read().decode("utf-8"))["data"][0]["id"], "scripted")
        first = post(self.mp.base_url + "/chat/completions", {"model": "scripted", "messages": [
            {"role": "user", "content": "Tool check."}]})
        msg = first["choices"][0]["message"]
        self.assertEqual(first["choices"][0]["finish_reason"], "tool_calls")
        call = msg["tool_calls"][0]
        self.assertEqual((call["type"], call["function"]["name"]), ("function", "exec"))
        self.assertEqual(json.loads(call["function"]["arguments"])["command"], "/usr/bin/python3 /r/scripts/jh.py whoami")
        # the same history gets the same answer (no server state); one more assistant turn moves on
        again = post(self.mp.base_url + "/chat/completions", {"model": "scripted", "messages": [
            {"role": "user", "content": "Tool check."}]})
        self.assertEqual(again["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"],
                         call["function"]["arguments"])
        second = post(self.mp.base_url + "/chat/completions", {"model": "scripted", "messages": [
            {"role": "user", "content": "Tool check."}, msg,
            {"role": "tool", "tool_call_id": call["id"], "content": "{}"}]})
        names = [c["function"]["name"] for c in second["choices"][0]["message"]["tool_calls"]]
        self.assertEqual(names, ["read", "write"])
        self.assertEqual(len(self.mp.requests), 3)
        self.assertTrue(all(r["auth_header"] for r in self.mp.requests))
        self.assertNotIn("Bearer", json.dumps(self.mp.requests))

    def test_streamed_answer(self):
        req = urllib.request.Request(self.mp.base_url + "/chat/completions", method="POST",
                                     data=json.dumps({"model": "scripted", "stream": True, "messages": [
                                         {"role": "user", "content": "x"}]}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            lines = [ln for ln in resp.read().decode("utf-8").split("\n") if ln.startswith("data: ")]
        self.assertEqual(lines[-1], "data: [DONE]")
        chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
        calls = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if c["choices"][0]["delta"].get("tool_calls")]
        self.assertEqual([c["function"]["name"] for c in calls], ["exec"])
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")


class TestApiRouteConfig(unittest.TestCase):
    """What install renders for the API-key route is what it renders for claude-cli, except the model runtime:
    the same tools, exec policy (allowlist, ask off), workspace confinement, approvals allowlist and cron jobs
    (the route is not an input of either), so the claude-cli fix changes nothing else on the API-key route."""

    def test_the_routes_differ_only_in_the_model_runtime(self):
        agents = ins.load_agents(REPO)
        kw = dict(ws_root="/w/ws", repo="/opt/r", py="/usr/bin/python3", root=REPO)
        cli = ins.render_agents_patch(agents, route="cli", **kw)["agents"]["entries"]
        api = ins.render_agents_patch(agents, route="api_key", **kw)["agents"]["entries"]
        self.assertEqual(sorted(cli), sorted(api))
        for agent_id, entry in cli.items():
            models = entry.pop("models")
            self.assertTrue(models, agent_id)
            self.assertTrue(all(m == {"agentRuntime": {"id": "claude-cli"}} for m in models.values()), agent_id)
            self.assertNotIn("models", api[agent_id])
            self.assertEqual(entry, api[agent_id], agent_id)
            # `mode` only on both routes (CLI route 6.1, D2): mode allowlist is security allowlist with ask off,
            # mode deny is security deny with ask off, and the nulls delete an older install's security/ask
            ex = entry["tools"]["exec"]
            want = "allowlist" if "exec" in entry["tools"]["allow"] else "deny"
            self.assertEqual((ex["mode"], ex["security"], ex["ask"]), (want, None, None), agent_id)
        for fn in (ins.merge_approvals, ins.cron_add_args):
            self.assertNotIn("route", inspect.signature(fn).parameters)


@unittest.skipUnless(node_major() >= 24 and shutil.which("node"),
                     "needs node 24 or later (the guard plugin runs TypeScript directly)")
class TestApiRouteProbe(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.stop)
        self.w.write_config()
        helpers.ensure_agent_setup(["argv", "env"])
        h = paths.home()
        self.py, self.ws_root = h["python"], h["ws_root"]
        self.ws = os.path.join(self.ws_root, "evaluator")
        os.makedirs(os.path.join(self.ws, "ref"), exist_ok=True)
        with open(os.path.join(self.ws, "ref", "profile_inference.md"), "w", encoding="utf-8") as fh:
            fh.write("# Profile inference\nRead the inputs, write the inference file.\n")
        self.g = GuardBridge({"repo": REPO, "python": self.py, "homeFile": paths.home_file(),
                              "publicReadonlyAgents": ["main"]}, canon.now())
        self.addCleanup(self.g.close)
        self.assertTrue(self.g.ask({"op": "heartbeat"})["ok"])
        self.mp = MockProvider("probe", {"PY": self.py, "REPO": REPO, "WS_ROOT": self.ws_root}).start()
        self.addCleanup(self.mp.stop)
        self.classified = []
        self.ran = []

    def tool(self, name: str, args: dict) -> dict:
        """One tool call as the embedded runtime runs it: the guard first, then the tool."""
        ctx = {"sessionId": "sess-api-1", "runId": "run-api-1", "workspaceDir": self.ws}
        self.g.ask({"op": "clock", "now": canon.now()})
        dec = self.g.ask({"op": "exec" if name == "exec" else "call", "agent": EV, "session": SESSION,
                          "tool": name, "params": args, "ctx": ctx})
        if dec["outcome"] != "allow":
            return {"blocked": dec["outcome"], "reason": dec["reason"]}
        params = dec["params"] or args
        if name == "exec":
            argv = split_rewritten(params["command"])
            res = run_jh_child(argv, exec_tool_env(dec["env"]), cwd=params["workdir"])
            self.classified += res["classify"]
            self.ran.append(params["command"])
            return {"exitCode": res["rc"], "command": params["command"], "stdout": json.dumps(res["envelope"])}
        if name == "write":
            os.makedirs(os.path.dirname(params["path"]), exist_ok=True)
            with open(params["path"], "w", encoding="utf-8") as fh:
                fh.write(params["content"])
            return {"ok": True}
        with open(params["path"], "r", encoding="utf-8") as fh:
            return {"content": fh.read()}

    def run_turn(self) -> tuple:
        messages = [{"role": "system", "content": "You are jobhunter-evaluator."},
                    {"role": "user", "content": "Tool check. Run the probe steps, then reply PROBE_DONE."}]
        results = {}
        for _ in range(10):
            out = post(self.mp.base_url + "/chat/completions", {"model": "scripted", "messages": messages,
                                                                 "tools": TOOLS})
            choice = out["choices"][0]
            msg = choice["message"]
            messages.append(msg)
            if choice["finish_reason"] != "tool_calls":
                return msg.get("content"), results
            for call in msg["tool_calls"]:
                args = json.loads(call["function"]["arguments"])
                r = self.tool(call["function"]["name"], args)
                results[(call["function"]["name"], args.get("command") or args.get("path"))] = r
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(r)})
        self.fail("the scripted turn did not end")

    def test_probe_and_refusals_on_the_api_route(self):
        final, results = self.run_turn()
        self.assertEqual(final, "PROBE_DONE")
        jh = "%s %s/scripts/jh.py" % (self.py, REPO)
        who = results[("exec", jh + " whoami")]
        data = json.loads(who["stdout"])["data"]
        self.assertEqual((who["exitCode"], data["class"], data["agent_id"], data["carriers"], data["isolated"]),
                         (0, "agent", EV, ["argv", "env"], True))
        self.assertEqual(data["markers"], ["OPENCLAW_SHELL"])
        self.assertTrue(who["command"].startswith("%s -I %s/scripts/jh.py --agent-proof jhp2.%s." % (self.py, REPO, EV)))
        self.assertEqual(results[("read", self.ws + "/ref/profile_inference.md")]["content"].split("\n")[0],
                         "# Profile inference")
        self.assertEqual(results[("write", self.ws + "/work/probe/ok.txt")], {"ok": True})
        self.assertEqual(results[("read", self.ws + "/work/probe/ok.txt")], {"content": "OK"})
        forged = "%s -I %s/scripts/jh.py --agent-proof %s whoami" % (self.py, REPO, FORGED_PROOF)
        self.assertEqual(results[("exec", forged)]["blocked"], "G_EXEC_PARAM")
        self.assertEqual(results[("read", "~/.ssh/id_ed25519")]["blocked"], "G_PATH_DENIED")
        self.assertEqual(results[("read", "/etc/hosts")]["blocked"], "G_PATH_DENIED")
        self.assertEqual(results[("exec", "/bin/ls /")]["blocked"], "G_EXEC_SHAPE")
        self.assertEqual(results[("exec", jh + " whoami && /usr/bin/id")]["blocked"], "G_EXEC_SHAPE")
        self.assertEqual(results[("exec", jh + " dispatch tick")]["blocked"], "G_EXEC_ACL")
        # the probe record jh.py keeps for selftest --probe-since
        with open(os.path.join(paths.state_dir(), "probe", EV + ".json"), "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["carriers"], ["argv", "env"])
        # exactly one jh.py call ran, as the evaluator; nothing was ever system
        self.assertEqual(self.classified, [{"class": "agent", "agent_id": EV}])
        # what the model was shown: the mock's request log has the rewritten command with -I and the proof,
        # and the refusals as guard codes
        self.assertEqual(len(self.mp.requests), 6)
        self.assertEqual(self.mp.requests[0]["tools"], ["exec", "read", "write"])
        seen = json.dumps(self.mp.requests[-1]["messages"])
        self.assertIn(" -I %s/scripts/jh.py --agent-proof jhp2.%s." % (REPO, EV), seen)
        for code in ("G_EXEC_PARAM", "G_PATH_DENIED", "G_EXEC_SHAPE", "G_EXEC_ACL"):
            self.assertIn(code, seen)
        self.assertNotIn("guard.key", seen)


if __name__ == "__main__":
    unittest.main()
