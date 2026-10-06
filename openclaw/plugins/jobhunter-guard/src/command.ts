// The owner-only /jh chat command (design 1.4 R6, 1.5 and 12.19). No model is involved: the plugin
// parses the text, signs a chat grant and runs jh.py with execFile.

import { makeGrant } from "./grant.ts";
import type { JhRunner } from "./jhcall.ts";

export type OwnerRule = { channel: string; senderId: string };

export type CommandCtx = {
  senderId?: string;
  from?: string;
  channel?: string;
  channelId?: string;
  senderIsOwner?: boolean;
  isAuthorizedSender?: boolean;
  args?: string;
  commandBody?: string;
};

export type JhCommand = {
  command: string; // command words, e.g. "profile answer"
  args: string[]; // argv tokens after the command words
};

export const HELP_TEXT = [
  "Job hunter commands:",
  "/jh approve <code>  send the draft with this approval code",
  "/jh skip <code> [reason]  drop the draft",
  "/jh edit <code> <new text>  replace the draft text (it goes through QC again)",
  "/jh answer <Qid> <text>  answer a question the agent asked",
  "/jh pause [all|linkedin|gmail|applications|site:<name>]  stop now",
  "/jh status  what is running and what is blocked",
  "/jh inbox  approvals and questions waiting for you",
  "/jh lower <setting> <value>  tighten a limit (raising needs the terminal and your PIN)",
  "/jh continue <code>  after you solved a CAPTCHA in the agent's browser window",
  "Unpause, breaker reset and raising limits are terminal only: ./jobhunter <command>",
].join("\n");

const CODE_RE = /^[ACDEFGHJKMNPQRTUVWXY34679]{4}$/;
const QID_RE = /^[A-Za-z0-9_.-]{1,40}$/;
const AREA_RE = /^(all|linkedin|gmail|applications|site:[a-z0-9_.-]{2,40})$/;
const PATH_RE = /^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$/;
const VALUE_RE = /^[A-Za-z0-9_.:+-]{1,64}$/;
const MAX_TEXT = 4000;

function cleanText(s: string): string {
  // keep newlines and tabs, drop other control characters; the linter checks the rest
  return s.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "").trim();
}

// Parse the text after "/jh". Returns the jh.py command, "help", or an error sentence.
export function parseJh(raw: string | undefined): JhCommand | { help: true } | { error: string } {
  const text = (raw || "").replace(/^\s+/, "");
  if (text.length === 0) return { help: true };
  const m = /^(\S+)(?:\s+([\s\S]*))?$/.exec(text);
  const sub = (m ? m[1] : "").toLowerCase();
  const rest = m && m[2] ? m[2] : "";
  const words = rest.trim().split(/\s+/).filter((w) => w.length > 0);
  switch (sub) {
    case "help":
      return { help: true };
    case "status":
    case "inbox":
      if (words.length) return { error: "/jh " + sub + " takes no arguments" };
      return { command: sub, args: [] };
    case "approve": {
      const code = (words[0] || "").toUpperCase();
      if (words.length !== 1 || !CODE_RE.test(code)) return { error: "usage: /jh approve <4-character code>" };
      return { command: "approve", args: [code, "--by", "chat"] };
    }
    case "skip": {
      const code = (words[0] || "").toUpperCase();
      if (!CODE_RE.test(code)) return { error: "usage: /jh skip <code> [reason]" };
      const reason = cleanText(rest.trim().slice(words[0].length));
      if (reason.length > MAX_TEXT) return { error: "the reason is longer than " + MAX_TEXT + " characters" };
      return { command: "skip", args: reason ? [code, "--reason", reason] : [code] };
    }
    case "edit": {
      const code = (words[0] || "").toUpperCase();
      if (!CODE_RE.test(code)) return { error: "usage: /jh edit <code> <new text>" };
      const body = cleanText(rest.trim().slice(words[0].length));
      if (!body) return { error: "usage: /jh edit <code> <new text>" };
      if (body.length > MAX_TEXT) return { error: "the text is longer than " + MAX_TEXT + " characters" };
      return { command: "edit", args: [code, "--text", body] };
    }
    case "answer": {
      const qid = words[0] || "";
      if (!QID_RE.test(qid)) return { error: "usage: /jh answer <question id> <text>" };
      const value = cleanText(rest.trim().slice(qid.length));
      if (!value) return { error: "usage: /jh answer <question id> <text>" };
      if (value.length > MAX_TEXT) return { error: "the answer is longer than " + MAX_TEXT + " characters" };
      return { command: "profile answer", args: ["--field", qid, "--value", value] };
    }
    case "pause": {
      if (words.length > 1) return { error: "usage: /jh pause [all|linkedin|gmail|applications|site:<name>]" };
      const area = (words[0] || "all").toLowerCase();
      if (!AREA_RE.test(area)) return { error: "usage: /jh pause [all|linkedin|gmail|applications|site:<name>]" };
      return { command: "pause", args: ["--scope", area] };
    }
    case "continue": {
      // the CAPTCHA hand-off (jh.py continue <code>): the owner solved the CAPTCHA in the agent's window
      const code = (words[0] || "").toUpperCase();
      if (words.length !== 1 || !CODE_RE.test(code)) return { error: "usage: /jh continue <4-character code>" };
      return { command: "continue", args: [code] };
    }
    case "lower": {
      if (words.length !== 2 || !PATH_RE.test(words[0]) || !VALUE_RE.test(words[1]) || words[1].startsWith("-")) {
        return { error: "usage: /jh lower <dotted.setting> <value>" };
      }
      return { command: "config lower", args: [words[0], words[1]] };
    }
    default:
      return { error: "unknown command. " + HELP_TEXT };
  }
}

function normSender(s: string): string {
  const t = s.trim().toLowerCase();
  const m = /^\+?([0-9]{6,15})(@.*)?$/.exec(t);
  return m ? m[1] : t;
}

// Owner check: the host says the sender is the owner, or channel and sender match ownerFallback.
export function isOwner(ctx: CommandCtx, fallback: OwnerRule[] | undefined): boolean {
  if (ctx.senderIsOwner === true) return true;
  const sender = typeof ctx.senderId === "string" && ctx.senderId ? ctx.senderId : typeof ctx.from === "string" ? ctx.from : "";
  if (!sender || !Array.isArray(fallback)) return false;
  const channels = [ctx.channel, ctx.channelId].filter((c): c is string => typeof c === "string" && c.length > 0).map((c) => c.toLowerCase());
  return fallback.some(
    (r) => r && typeof r.channel === "string" && typeof r.senderId === "string" && channels.includes(r.channel.toLowerCase()) && normSender(r.senderId) === normSender(sender),
  );
}

// argv for jh.py: the grant and --human first (global options), then the command words and args, so
// that "the tokens after the command words" are exactly `args`.
export function buildArgv(cmd: JhCommand, grant: string): string[] {
  return ["--grant", grant, "--human", ...cmd.command.split(" "), ...cmd.args];
}

const MAX_REPLY = 3500;

export function replyFromResult(res: { exitCode: number; stdout: string; stderr: string; error: string | null }): string {
  const out = res.stdout.trim();
  if (res.exitCode === -1) return "jh.py could not run (" + (res.error || "error") + "). Try ./jobhunter status in a terminal.";
  let text = out || (res.exitCode === 0 ? "Done." : "The command failed (exit " + res.exitCode + ").");
  if (text.length > MAX_REPLY) text = text.slice(0, MAX_REPLY) + "\n(truncated)";
  return text;
}

export type HandlerDeps = {
  key: () => Buffer | null; // null when private/guard.key cannot be read
  run: JhRunner;
  ownerFallback: OwnerRule[] | undefined;
  log?: (entry: Record<string, unknown>) => void;
  nowS?: () => number;
};

export async function handleJh(ctx: CommandCtx, deps: HandlerDeps): Promise<{ text: string }> {
  if (!isOwner(ctx, deps.ownerFallback)) {
    deps.log?.({ kind: "command", ok: false, code: "G_NOT_OWNER", channel: ctx.channel ?? null });
    return { text: "G_NOT_OWNER: /jh only works in the owner's own chat." };
  }
  const parsed = parseJh(ctx.args);
  if ("help" in parsed) return { text: HELP_TEXT };
  if ("error" in parsed) return { text: parsed.error };
  const key = deps.key();
  if (!key) {
    deps.log?.({ kind: "command", ok: false, code: "G_GUARD_UNHEALTHY", command: parsed.command });
    return { text: "G_GUARD_UNHEALTHY: private/guard.key cannot be read; run ./jobhunter doctor." };
  }
  const grant = makeGrant(key, parsed.command, parsed.args, deps.nowS ? deps.nowS() : undefined);
  const res = await deps.run(buildArgv(parsed, grant));
  deps.log?.({ kind: "command", ok: res.exitCode === 0, command: parsed.command, exit: res.exitCode });
  return { text: replyFromResult(res) };
}
