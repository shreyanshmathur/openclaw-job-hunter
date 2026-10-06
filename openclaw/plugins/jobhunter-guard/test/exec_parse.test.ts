import test from "node:test";
import assert from "node:assert/strict";
import { checkArgs, isWorkPath, matchCommand, parseAgentExec, parsePublicExec, tokenize } from "../src/exec_parse.ts";
import { CYCLE, JH, PY, REPO_PATH, TOKEN, WS, loadAcl } from "./_helpers.ts";

const acl = loadAcl();
const classes = acl.value_classes;
const roots = (role: string) => [WS + "/" + role + "/work", WS + "/" + role + "/inbox"];

function agentExec(agent: string, command: string) {
  const role = agent.replace("jobhunter-", "");
  return parseAgentExec(command, { python: PY, repo: REPO_PATH, commands: acl.agents[agent].commands, classes, workRoots: roots(role) });
}

test("tokenize accepts only plain single-space tokens", () => {
  assert.deepEqual(tokenize("a b c"), ["a", "b", "c"]);
  assert.deepEqual(tokenize("a b=c"), ["a", "b=c"]);
  const proof = "jhp2.jobhunter-scout.1790000000.0123456789abcdef.0123456789abcdef." + "0a".repeat(32);
  assert.deepEqual(tokenize("--agent-proof " + proof), ["--agent-proof", proof]);
  const bad = ["", " a", "a  b", "a ", "a|b", "a;b", "a && b", "a 'b'", 'a "b"', "a $HOME", "a\nb", "a > f", "a `x`", "a\tb", "a (b)", "a * b", "=python3", "a =b", "a ==b"];
  for (const s of bad) assert.equal(tokenize(s), null, JSON.stringify(s));
  assert.equal(tokenize(123 as unknown as string), null);
});

test("matchCommand prefers the longest command", () => {
  const keys = ["qc review start", "qc lint", "gate status", "preflight"];
  assert.deepEqual(matchCommand(["qc", "review", "start", "--draft", "DABCDEFG"], keys), { command: "qc review start", args: ["--draft", "DABCDEFG"] });
  assert.deepEqual(matchCommand(["preflight", "--lane", "scout"], keys), { command: "preflight", args: ["--lane", "scout"] });
  assert.equal(matchCommand(["gate", "reserve"], keys), null);
});

test("isWorkPath is lexical and strict", () => {
  const r = roots("applier");
  assert.equal(isWorkPath(WS + "/applier/work/" + CYCLE + "/a.json", r), true);
  assert.equal(isWorkPath(WS + "/applier/inbox/reply-1.json", r), true);
  assert.equal(isWorkPath(WS + "/applier/work", r), false);
  assert.equal(isWorkPath(WS + "/applier/work/", r), false);
  assert.equal(isWorkPath(WS + "/applier/work/../AGENTS.md", r), false);
  assert.equal(isWorkPath(WS + "/applier/workx/a.json", r), false);
  assert.equal(isWorkPath(WS + "/scout/work/a.json", r), false);
  assert.equal(isWorkPath("work/a.json", r), false);
  assert.equal(isWorkPath(WS + "/applier/work//a.json", r), false);
});

type Row = [string, string, string | null]; // agent, command, expected block code (null = allowed)

const W = WS + "/applier/work/" + CYCLE;
const table: Row[] = [
  ["jobhunter-scout", JH + " preflight --lane scout", null],
  ["jobhunter-scout", JH + " --quiet preflight --lane scout", null],
  ["jobhunter-scout", JH + " preflight --lane applier", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " cycle end --cycle " + CYCLE, null],
  ["jobhunter-scout", JH + " --cycle " + CYCLE + " job add --file " + WS + "/scout/work/" + CYCLE + "/naukri.json", null],
  ["jobhunter-scout", JH + " job add --file " + WS + "/applier/work/x.json", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " job add --file /etc/passwd", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " job add", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " gate reserve --kind application", "G_EXEC_ACL"],
  ["jobhunter-scout", JH + " approve A7K2", "G_EXEC_ACL"],
  ["jobhunter-scout", JH + " usage add --platform naukri --metric page_view", null],
  ["jobhunter-scout", JH + " usage add --platform naukri --metric page_view --n 2", null],
  ["jobhunter-scout", JH + " usage add --platform naukri --metric page_view --n -2", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " usage add --platform --grant --metric page_view", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " --home /tmp preflight --lane scout", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " preflight --lane scout --pin-stdin", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " preflight --lane scout --grant 1.2.3", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " --human status", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " preflight --lane=scout", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " preflight --lane scout --lane scout", "G_EXEC_PARAM"],
  ["jobhunter-scout", "sqlite3 /opt/jobhunter-test/openclaw-job-hunter/state/jobhunter.sqlite3", "G_EXEC_SHAPE"],
  ["jobhunter-scout", "python3 " + REPO_PATH + "/scripts/jh.py preflight --lane scout", "G_EXEC_SHAPE"],
  ["jobhunter-scout", PY + " /tmp/jh.py preflight --lane scout", "G_EXEC_SHAPE"],
  ["jobhunter-scout", PY + " -c import", "G_EXEC_SHAPE"],
  ["jobhunter-scout", JH + " preflight --lane scout && rm -rf /", "G_EXEC_SHAPE"],
  ["jobhunter-scout", JH + " preflight --lane scout | cat", "G_EXEC_SHAPE"],
  ["jobhunter-scout", "rm " + REPO_PATH + "/state/PAUSED", "G_EXEC_SHAPE"],
  ["jobhunter-applier", JH + " gate reserve --kind application --draft DABCDEFG --precheck 12 --platform greenhouse --job JABCDEFG", null],
  ["jobhunter-applier", JH + " gate reserve --kind cold_email --draft DABCDEFG --precheck 12 --platform gmail", "G_EXEC_PARAM"],
  ["jobhunter-applier", JH + " gate arm " + TOKEN + " --observed-file " + W + "/observed.json", null],
  ["jobhunter-applier", JH + " gate arm TBAD --observed-file " + W + "/observed.json", "G_EXEC_PARAM"],
  ["jobhunter-applier", JH + " gate fail " + TOKEN + " --reason not_attempted --evidence-file " + W + "/e.txt", null],
  ["jobhunter-applier", JH + " gate fail " + TOKEN + " --reason human_confirmed_not_sent --evidence-file " + W + "/e.txt", "G_EXEC_PARAM"],
  ["jobhunter-applier", JH + " qc review start --draft DABCDEFG", null],
  ["jobhunter-applier", JH + " qc review wait --job QABCDEFG --max 45", null],
  ["jobhunter-applier", JH + " resume stage --variant VABCDEFG --token " + TOKEN, null],
  ["jobhunter-applier", JH + " draft show DABCDEFG --field body", null],
  ["jobhunter-applier", JH + " draft show DABCDEFG extra", "G_EXEC_PARAM"],
  ["jobhunter-applier", JH + " approvals list", null],
  ["jobhunter-applier", JH + " approve A7K2 --by chat", "G_EXEC_ACL"],
  ["jobhunter-applier", JH + " reconcile confirm-not-sent " + TOKEN, "G_EXEC_ACL"],
  ["jobhunter-applier", JH + " config raise linkedin.x 5", "G_EXEC_ACL"],
  ["jobhunter-outreach", JH + " thread list --needs-check", null],
  ["jobhunter-outreach", JH + " thread list --needs-check yes", "G_EXEC_PARAM"],
  ["jobhunter-outreach", JH + " outreach skip contact:PABCDEFG --reason no_hook", null],
  ["jobhunter-outreach", JH + " email verify --address alex@example.com --grade A", null],
  ["jobhunter-evaluator", JH + " eval record --job JABCDEFG --file " + WS + "/evaluator/work/" + CYCLE + "/JABCDEFG.json", null],
  ["jobhunter-qc", JH + " home show", "G_EXEC_ACL"],
  // -I and the --agent-p* prefix (the guard writes --agent-proof itself)
  ["jobhunter-scout", PY + " -I " + REPO_PATH + "/scripts/jh.py preflight --lane scout", null],
  ["jobhunter-scout", PY + " -I -I " + REPO_PATH + "/scripts/jh.py preflight --lane scout", "G_EXEC_SHAPE"],
  ["jobhunter-scout", PY + " -E " + REPO_PATH + "/scripts/jh.py preflight --lane scout", "G_EXEC_SHAPE"],
  ["jobhunter-scout", PY + " -I", "G_EXEC_SHAPE"],
  ["jobhunter-scout", JH + " --agent-proof x preflight --lane scout", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " --agent-proof=x preflight --lane scout", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " --agent-p x preflight --lane scout", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " --agent-pr preflight --lane scout", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " preflight --lane scout --agent-proof x", "G_EXEC_PARAM"],
  ["jobhunter-scout", PY + " -I " + REPO_PATH + "/scripts/jh.py --agent-proof x preflight --lane scout", "G_EXEC_PARAM"],
  ["jobhunter-scout", JH + " preflight --lane =scout", "G_EXEC_SHAPE"],
];

test("agent exec decision table (R2)", () => {
  for (const [agent, command, want] of table) {
    const r = agentExec(agent, command);
    if (want === null) assert.equal(r.ok, true, agent + " " + command + " -> " + JSON.stringify(r));
    else {
      assert.equal(r.ok, false, agent + " " + command + " should be blocked");
      if (!r.ok) assert.equal(r.code, want, agent + " " + command + ": " + r.reason);
    }
  }
});

test("parseAgentExec returns the jh.py arguments and strips only an own proof pair", () => {
  const opts = { python: PY, repo: REPO_PATH, commands: acl.agents["jobhunter-scout"].commands, classes, workRoots: roots("scout") };
  const plain = parseAgentExec(JH + " --cycle " + CYCLE + " preflight --lane scout", opts);
  assert.equal(plain.ok, true);
  if (plain.ok) {
    assert.deepEqual(plain.rest, ["--cycle", CYCLE, "preflight", "--lane", "scout"]);
    assert.equal(plain.isolated, false);
    assert.equal(plain.cycle, CYCLE);
  }
  const seen: Array<[string, string[]]> = [];
  const own = (tok: string, after: string[]) => {
    seen.push([tok, after]);
    return tok === "jhp2.own";
  };
  const cmd = PY + " -I " + REPO_PATH + "/scripts/jh.py --agent-proof jhp2.own preflight --lane scout";
  const stripped = parseAgentExec(cmd, { ...opts, ownProof: own });
  assert.equal(stripped.ok, true);
  if (stripped.ok) {
    assert.deepEqual(stripped.rest, ["preflight", "--lane", "scout"]);
    assert.equal(stripped.isolated, true);
  }
  assert.deepEqual(seen, [["jhp2.own", ["preflight", "--lane", "scout"]]]);
  const other = parseAgentExec(cmd.replace("jhp2.own", "jhp2.other"), { ...opts, ownProof: own });
  assert.equal(other.ok, false);
  if (!other.ok) assert.equal(other.code, "G_EXEC_PARAM");
  // without -I the pair is never stripped
  const noI = parseAgentExec(JH + " --agent-proof jhp2.own preflight --lane scout", { ...opts, ownProof: own });
  assert.equal(noI.ok, false);
  // a second pair after the own one is refused
  const twice = parseAgentExec(PY + " -I " + REPO_PATH + "/scripts/jh.py --agent-proof jhp2.own --agent-proof jhp2.own preflight --lane scout", { ...opts, ownProof: own });
  assert.equal(twice.ok, false);
});

test("checkArgs fails closed on an unknown value class", () => {
  const r = checkArgs({ "--x": "no_such_class" }, ["--x", "1"], classes, []);
  assert.equal(r.ok, false);
});

test("public read-only exec for other agents (R7)", () => {
  const opts = { python: PY, repo: REPO_PATH, publicCommands: acl.public_readonly, classes };
  assert.equal(parsePublicExec(JH + " status", opts).ok, true);
  assert.equal(parsePublicExec(JH + " --human inbox", opts).ok, true);
  assert.equal(parsePublicExec(JH + " budget --platform linkedin", opts).ok, true);
  assert.equal(parsePublicExec(JH + " approvals list", opts).ok, true);
  assert.equal(parsePublicExec(PY + " -I " + REPO_PATH + "/scripts/jh.py status", opts).ok, true);
  for (const bad of [JH + " approve A7K2", JH + " unpause", JH + " status --grant 1.a.b", JH + " config lower a.b 1", "cd /x && " + JH + " approve A7K2", JH + " breaker reset --scope linkedin",
                     JH + " --agent-proof x status", JH + " status --agent-p x", JH + " --agent-proof=x status", PY + " -S " + REPO_PATH + "/scripts/jh.py status"]) {
    assert.equal(parsePublicExec(bad, opts).ok, false, bad);
  }
});
