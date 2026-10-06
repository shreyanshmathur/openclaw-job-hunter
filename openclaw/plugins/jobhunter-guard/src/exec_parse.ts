// Strict tokenizer and ACL argument matcher for exec calls (design 1.4 R2 and R7, acl.json 3.3).
// Pure functions: no file system, no clock.

import path from "node:path";

// A token may not start with "=" (zsh expands "=word" to the path of a program).
export const TOKEN_RE = /^(?!=)[A-Za-z0-9_./:@+=,%-]+$/;

// The interpreter flag the R2 rewrite always puts before jh.py (isolated mode: no user site, no PYTHON* env).
export const ISOLATED_FLAG = "-I";
// `--agent-proof <T>` is written only by the guard (R2 rewrite). Every token that starts like it, as typed by
// a model, is refused (argparse would read an abbreviation such as `--agent-p` as the full option).
export const AGENT_PROOF_FLAG = "--agent-proof";
export const AGENT_PROOF_PREFIX = "--agent-p";

// Flags that no agent may ever pass (3.1): the home override does not exist, the PIN and the grant
// are human and chat authority, --human changes the output the agent programs parse.
export const FORBIDDEN_FLAGS = ["--home", "--pin-stdin", "--grant", "--human"];

export type ParseFail = { ok: false; code: "G_EXEC_SHAPE" | "G_EXEC_ACL" | "G_EXEC_PARAM"; reason: string };
export type Parsed = {
  ok: true;
  command: string; // "gate reserve"
  args: string[]; // tokens after the command words
  cycle: string | null; // global --cycle value, if any
  rest: string[]; // every jh.py argument (globals, command words, args), without a stripped own proof pair
  isolated: boolean; // the command already had "-I" before jh.py
};

// Split a command on single spaces. Every token must match TOKEN_RE; empty tokens (double spaces,
// leading or trailing spaces) and anything else (quotes, pipes, redirects, $, ;, &, newlines) fail.
export function tokenize(command: unknown): string[] | null {
  if (typeof command !== "string" || command.length === 0 || command.length > 4000) return null;
  const tokens = command.split(" ");
  for (const t of tokens) {
    if (t.length === 0 || !TOKEN_RE.test(t)) return null;
  }
  return tokens;
}

export function jhPath(repo: string): string {
  return trimSlash(repo) + "/scripts/jh.py";
}

export function trimSlash(p: string): string {
  let s = p;
  while (s.length > 1 && s.endsWith("/")) s = s.slice(0, -1);
  return s;
}

// Longest command key (3, 2 or 1 words) of `keys` that prefixes `rest`.
export function matchCommand(rest: string[], keys: Iterable<string>): { command: string; args: string[] } | null {
  const set = new Set(keys);
  for (let n = 3; n >= 1; n--) {
    if (rest.length < n) continue;
    const words = rest.slice(0, n);
    if (words.some((w) => w.startsWith("-"))) continue;
    const key = words.join(" ");
    if (set.has(key)) return { command: key, args: rest.slice(n) };
  }
  return null;
}

type ArgSpec = { name: string; optional: boolean; kind: "flag" | "enum" | "regex" | "path"; values?: Set<string>; re?: RegExp };

function parseSpec(name: string, spec: string, classes: Record<string, string>): ArgSpec | null {
  if (typeof spec !== "string" || spec.length === 0) return null;
  const optional = spec.endsWith("?");
  const base = optional ? spec.slice(0, -1) : spec;
  if (base.startsWith("enum:")) {
    const values = base.slice(5).split("|").filter((v) => v.length > 0);
    if (values.length === 0) return null;
    return { name, optional, kind: "enum", values: new Set(values) };
  }
  const cls = classes[base];
  if (cls === undefined) return null; // unknown class: fail closed
  if (cls === "FLAG" || base === "flag") return { name, optional, kind: "flag" };
  if (cls === "WORKDIR" || base === "path_work") return { name, optional, kind: "path" };
  let re: RegExp;
  try {
    re = new RegExp(cls);
  } catch {
    return null;
  }
  return { name, optional, kind: "regex", re };
}

// An agent file argument: absolute, already normalized (no "..", no "//", no trailing slash), under
// one of the roots and not a root itself. jh.py re-checks the realpath (paths.ensure_agent_path).
export function isWorkPath(value: string, roots: string[]): boolean {
  if (!path.isAbsolute(value)) return false;
  if (path.posix.normalize(value) !== value) return false;
  if (value.endsWith("/")) return false;
  for (const root of roots) {
    const r = trimSlash(root);
    if (value.startsWith(r + "/") && value.length > r.length + 1) return true;
  }
  return false;
}

// Match the argument tokens after the command words against one command schema from acl.json.
export function checkArgs(
  schema: Record<string, string>,
  args: string[],
  classes: Record<string, string>,
  workRoots: string[],
): { ok: true } | ParseFail {
  const specs = new Map<string, ArgSpec>();
  const positional: ArgSpec[] = [];
  for (const [name, raw] of Object.entries(schema || {})) {
    const spec = parseSpec(name, raw, classes);
    if (spec === null) return { ok: false, code: "G_EXEC_PARAM", reason: "argument " + name + " has an unknown value class" };
    if (/^_[0-9]+$/.test(name)) positional[Number(name.slice(1)) - 1] = spec;
    else if (name.startsWith("--")) specs.set(name, spec);
    else return { ok: false, code: "G_EXEC_PARAM", reason: "bad schema key " + name };
  }
  const seen = new Set<string>();
  let pos = 0;
  let i = 0;
  while (i < args.length) {
    const tok = args[i];
    if (tok === "--quiet") {
      i += 1;
      continue;
    }
    if (tok.startsWith("-")) {
      const spec = specs.get(tok);
      if (!spec) return { ok: false, code: "G_EXEC_PARAM", reason: "flag " + tok + " is not allowed for this command" };
      if (seen.has(tok)) return { ok: false, code: "G_EXEC_PARAM", reason: "flag " + tok + " given twice" };
      seen.add(tok);
      if (spec.kind === "flag") {
        i += 1;
        continue;
      }
      if (i + 1 >= args.length) return { ok: false, code: "G_EXEC_PARAM", reason: "flag " + tok + " needs a value" };
      const value = args[i + 1];
      const bad = checkValue(spec, value, workRoots);
      if (bad) return { ok: false, code: "G_EXEC_PARAM", reason: tok + ": " + bad };
      i += 2;
      continue;
    }
    const spec = positional[pos];
    if (!spec) return { ok: false, code: "G_EXEC_PARAM", reason: "unexpected argument " + tok };
    const bad = checkValue(spec, tok, workRoots);
    if (bad) return { ok: false, code: "G_EXEC_PARAM", reason: "argument " + (pos + 1) + ": " + bad };
    seen.add("_" + (pos + 1));
    pos += 1;
    i += 1;
  }
  for (const [name, spec] of specs) {
    if (!spec.optional && !seen.has(name)) return { ok: false, code: "G_EXEC_PARAM", reason: "missing " + name };
  }
  for (let k = 0; k < positional.length; k++) {
    const spec = positional[k];
    if (spec && !spec.optional && !seen.has("_" + (k + 1))) return { ok: false, code: "G_EXEC_PARAM", reason: "missing argument " + (k + 1) };
  }
  return { ok: true };
}

function checkValue(spec: ArgSpec, value: string, workRoots: string[]): string | null {
  if (value.startsWith("-")) return "value may not start with a dash";
  if (FORBIDDEN_FLAGS.some((f) => value.startsWith(f))) return "forbidden value";
  if (spec.kind === "enum") return spec.values && spec.values.has(value) ? null : "value not in the allowed list";
  if (spec.kind === "path") return isWorkPath(value, workRoots) ? null : "path must be under the agent's own work/ or inbox/ folder";
  if (spec.kind === "regex") return spec.re && spec.re.test(value) ? null : "value has the wrong shape";
  return "unexpected value";
}

function proofTokenFail(tokens: string[]): ParseFail | null {
  for (const t of tokens) {
    if (t.startsWith(AGENT_PROOF_PREFIX)) {
      return { ok: false, code: "G_EXEC_PARAM", reason: "--agent-proof is added only by the jobhunter-guard plugin; never type it" };
    }
  }
  return null;
}

// Parse "<python> [-I] <repo>/scripts/jh.py [--cycle <id>] [--quiet] <command words> <args>" for a jobhunter
// agent and check it against that agent's command map. `ownProof(token, restAfter)` tells whether a
// `-I <jh.py> --agent-proof <token>` pair is this guard's own proof for the same call (a second decision of a
// rewritten call); only such a pair is stripped. Any other token starting with --agent-p is refused.
export function parseAgentExec(
  command: unknown,
  opts: {
    python: string;
    repo: string;
    commands: Record<string, Record<string, string>>;
    classes: Record<string, string>;
    workRoots: string[];
    ownProof?: (token: string, restAfter: string[]) => boolean;
  },
): Parsed | ParseFail {
  const tokens = tokenize(command);
  if (tokens === null) {
    return { ok: false, code: "G_EXEC_SHAPE", reason: "one plain jh.py command only: no quotes, pipes, redirects, variables or double spaces" };
  }
  const head = checkHead(tokens, opts.python, opts.repo);
  if (head) return head;
  const isolated = tokens[1] === ISOLATED_FLAG;
  let rest = tokens.slice(isolated ? 3 : 2);
  if (isolated && rest[0] === AGENT_PROOF_FLAG && rest.length >= 2 && opts.ownProof && opts.ownProof(rest[1], rest.slice(2))) {
    rest = rest.slice(2);
  }
  const proofFail = proofTokenFail(rest);
  if (proofFail) return proofFail;
  for (const t of rest) {
    if (FORBIDDEN_FLAGS.includes(t) || FORBIDDEN_FLAGS.some((f) => t.startsWith(f + "="))) {
      return { ok: false, code: "G_EXEC_PARAM", reason: t + " is never allowed for agents" };
    }
  }
  const all = [...tokens.slice(0, isolated ? 3 : 2), ...rest];
  let i = isolated ? 3 : 2;
  let cycle: string | null = null;
  const cycleRe = safeRe(opts.classes["cycle"]);
  while (i < all.length && all[i].startsWith("-")) {
    const t = all[i];
    if (t === "--quiet") {
      i += 1;
      continue;
    }
    if (t === "--cycle") {
      const v = all[i + 1];
      if (v === undefined || cycle !== null || !cycleRe || !cycleRe.test(v)) {
        return { ok: false, code: "G_EXEC_PARAM", reason: "--cycle needs one cycle id" };
      }
      cycle = v;
      i += 2;
      continue;
    }
    return { ok: false, code: "G_EXEC_PARAM", reason: "global option " + t + " is not allowed" };
  }
  const m = matchCommand(all.slice(i), Object.keys(opts.commands || {}));
  if (m === null) {
    const words = all.slice(i, i + 3).filter((w) => !w.startsWith("-")).join(" ");
    return { ok: false, code: "G_EXEC_ACL", reason: "command '" + (words || "(none)") + "' is not in this agent's allowlist" };
  }
  const check = checkArgs(opts.commands[m.command], m.args, opts.classes, opts.workRoots);
  if (!check.ok) return check;
  return { ok: true, command: m.command, args: m.args, cycle, rest, isolated };
}

// "<python> <jh.py> ..." or "<python> -I <jh.py> ..." (no other interpreter flag).
function checkHead(tokens: string[], python: string, repo: string): ParseFail | null {
  const j = tokens[1] === ISOLATED_FLAG ? 2 : 1;
  if (tokens.length < j + 2) return { ok: false, code: "G_EXEC_SHAPE", reason: "expected '<python> <repo>/scripts/jh.py <command>'" };
  if (tokens[0] !== python) return { ok: false, code: "G_EXEC_SHAPE", reason: "the program must be " + python };
  if (tokens[j] !== jhPath(repo)) return { ok: false, code: "G_EXEC_SHAPE", reason: "the script must be " + jhPath(repo) };
  return null;
}

function safeRe(src: string | undefined): RegExp | null {
  if (typeof src !== "string") return null;
  try {
    return new RegExp(src);
  } catch {
    return null;
  }
}

// Argument schemas of the public read-only commands (R7). acl.json lists only their names.
export const PUBLIC_SCHEMAS: Record<string, Record<string, string>> = {
  status: {},
  inbox: {},
  "approvals list": {},
  budget: { "--platform": "platform?", "--kind": "metric?" },
  "breaker status": {},
  "home show": {},
};

// R7: an exec by an agent outside jobhunter-* that mentions jh.py. Allowed only as a plain public
// read-only command ("<python> [-I] <repo>/scripts/jh.py [--quiet|--human] <public command> [args]").
export function parsePublicExec(
  command: unknown,
  opts: { python: string; repo: string; publicCommands: string[]; classes: Record<string, string> },
): Parsed | ParseFail {
  const tokens = tokenize(command);
  if (tokens === null) return { ok: false, code: "G_EXEC_SHAPE", reason: "jh.py may only be run as one plain read-only command" };
  const head = checkHead(tokens, opts.python, opts.repo);
  if (head) return head;
  const isolated = tokens[1] === ISOLATED_FLAG;
  const proofFail = proofTokenFail(tokens);
  if (proofFail) return proofFail;
  let i = isolated ? 3 : 2;
  while (i < tokens.length && (tokens[i] === "--quiet" || tokens[i] === "--human")) i += 1;
  const allowed = opts.publicCommands.filter((c) => Object.prototype.hasOwnProperty.call(PUBLIC_SCHEMAS, c));
  const m = matchCommand(tokens.slice(i), allowed);
  if (m === null) return { ok: false, code: "G_EXEC_ACL", reason: "only read-only jh.py commands are allowed here" };
  const rest = m.args.filter((t) => t !== "--human");
  for (const t of rest) {
    if (FORBIDDEN_FLAGS.includes(t) || FORBIDDEN_FLAGS.some((f) => t.startsWith(f + "="))) {
      return { ok: false, code: "G_EXEC_PARAM", reason: t + " is not allowed" };
    }
  }
  const check = checkArgs(PUBLIC_SCHEMAS[m.command], rest, opts.classes, []);
  if (!check.ok) return check;
  return { ok: true, command: m.command, args: m.args, cycle: null, rest: tokens.slice(isolated ? 3 : 2), isolated };
}
