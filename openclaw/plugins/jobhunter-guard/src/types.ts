// Shared types for the jobhunter-guard plugin. Erasable TypeScript only (Node 24 strips the types).

export type ProofCarrier = "argv" | "env";

export type GuardConfig = {
  repo: string;
  python: string;
  homeFile: string;
  publicReadonlyAgents: string[];
  ownerFallback?: Array<{ channel: string; senderId: string }>;
  // Optional keys of the Claude subscription route (design CLI route 6.4); parseConfig fills the defaults.
  claudeNativeTools?: "deny" | "gate"; // "gate" only in the reduced-protection mode N
  pinToolSurface?: boolean; // before_prompt_build narrows jobhunter runs to their ACL tools
  proofCarriers?: ProofCarrier[]; // where the agent identity proof goes: the argv rewrite, the exec env, or both
  recordEvents?: boolean; // logs/guard-events-<yyyymmdd>.jsonl (test profiles only)
  protectedRoots?: { read: string[]; write: string[] }; // R7: absolute roots other agents may not read or write
  qcVerdictFile?: boolean; // fallback F-QC: jobhunter-qc may write its verdict file under work/verdict/
};

export type AclAgent = {
  tools: string[];
  commands: Record<string, Record<string, string>>;
};

export type Acl = {
  version?: number;
  value_classes: Record<string, string>;
  agents: Record<string, AclAgent>;
  public_readonly: string[];
  chat?: string[];
  human_only?: string[];
};

export type PlatformEntry = {
  key: string;
  scope: string;
  hosts: string[];
  aliases: string[];
  consent: string | null; // the private/consent.json site these hosts need (null: public pages, no login)
  hostPatterns: RegExp[]; // optional "host_patterns": whole-host regexes tested after every suffix host
};

export type HostsConfig = {
  neverHosts: string[];
  neverUrlPatterns: RegExp[];
  allowedSchemes: string[];
  allowAboutBlank: boolean;
  blockLoopback: boolean;
  blockPrivateNetworks: boolean;
  platforms: PlatformEntry[];
  harmlessNames: RegExp[];
  riskyNames: RegExp;
  prepareNames: Record<string, RegExp>;
  multilineNames: Record<string, RegExp>; // per token kind: textbox names where a typed newline is a line break
  forbiddenNames: RegExp[]; // a click on a ref with such a name is G_TOOL_DENIED (social sign-in, CAPTCHA widgets)
  secretFieldNames: RegExp; // a type or fill into a textbox with such a name is G_SECRET_FIELD
};

export type OpenToken = {
  token: string;
  kind: string;
  platform: string;
  status: string; // reserved | armed
  armedAt: string | null;
  expiresAt: string;
};

export type DwellLock = {
  acquiredAt: string;
  expiresAt: string;
};

export type RefInfo = { role: string; name: string };

// Everything decide() needs besides the event and context. Built by the runtime (src/runtime.ts)
// from files, the ledger and in-memory session state; built by hand in tests.
export type Snapshot = {
  nowMs: number;
  health: { ok: boolean; reason: string };
  config: GuardConfig;
  acl: Acl | null;
  hosts: HostsConfig | null;
  wsRoot: string;
  driverHashes: Set<string>;
  sessionStopped: boolean;
  writeBlocked: Set<string>; // sites (platform key or host) with a job-level stop in this session
  // private/consent.json; absent = no consent. capabilities: capability name -> sites with an active row.
  consent?: { ok: boolean; sites: Set<string>; reason: string; capabilities?: Map<string, Set<string>> };
  // the addressed tab's focus is in a secret field (a click on a password, code or PIN textbox); absent = false
  secretFocus?: boolean;
  paused: boolean;
  openBreakers: Set<string>;
  token: OpenToken | null;
  stagedPath: string | null;
  dwell: DwellLock | null;
  commitsUsed: number;
  currentUrl: string | null;
  lookupRef: (ref: string) => RefInfo | undefined;
  realpath: (p: string) => string;
  // R2: mint an argv proof for this call; verify one this guard minted (a second decision of one call)
  mintProof: (agentId: string, sessionKey: string, rest: string[]) => string;
  verifyOwnProof: (token: string, agentId: string, sessionKey: string, rest: string[]) => boolean;
  // R7: the gateway user's home folder (`~`) and a bounded glob expansion of an absolute pattern on disk
  homeDir: string;
  glob: (absPattern: string) => string[];
};

export type ToolEvent = {
  toolName: string;
  params: Record<string, unknown>;
  derivedPaths?: readonly string[];
  toolKind?: string;
  runId?: string;
  toolCallId?: string;
};

export type ToolCtx = {
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  runId?: string;
  toolName?: string;
  toolKind?: string;
  workspaceDir?: string; // the agent's workspace (OpenClaw resolves relative tool paths against it)
  cwd?: string; // native Claude Code tools: the run's working folder
};

// One fill or commit action that the runtime appends to state/guard/<token>.jsonl (12.17).
export type TokenRecord = {
  class: "fill" | "commit";
  action: string;
  ref: string | null;
  role: string | null;
  name: string | null;
};

// `native` marks a call in Claude Code's native tool shape (logged as a `native_tool` guard log line).
export type Decision =
  | { kind: "pass"; command?: string; native?: boolean }
  | {
      kind: "allow";
      params?: Record<string, unknown>;
      records?: TokenRecord[];
      token?: string;
      actionClass?: string;
      command?: string;
      host?: string | null;
      native?: boolean;
      secretFocus?: boolean; // browser: the addressed tab's new secretFocus flag (absent: unchanged)
    }
  | { kind: "block"; code: string; reason: string; command?: string; actionClass?: string; host?: string | null; native?: boolean };

export const BLOCK_CODES = [
  "G_GUARD_UNHEALTHY",
  "G_TOOL_DENIED",
  "G_EXEC_SHAPE",
  "G_EXEC_ACL",
  "G_EXEC_PARAM",
  "G_PATH_DENIED",
  "G_BROWSER_PROFILE",
  "G_HOST_NEVER",
  "G_BREAKER_OPEN",
  "G_NO_TOKEN",
  "G_NOT_ARMED",
  "G_COMMIT_BUDGET",
  "G_SCRIPT_NOT_ALLOWED",
  "G_UPLOAD_PATH",
  "G_STOPPED",
  "G_NOT_OWNER",
  "G_NO_CONSENT",
  "G_PAGE_UNKNOWN",
  "G_OTHER_AGENT_DENIED",
  "G_SECRET_FIELD",
] as const;
