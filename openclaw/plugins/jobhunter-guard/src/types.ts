// Shared types for the jobhunter-guard plugin. Erasable TypeScript only (Node 24 strips the types).

export type GuardConfig = {
  repo: string;
  python: string;
  homeFile: string;
  publicReadonlyAgents: string[];
  ownerFallback?: Array<{ channel: string; senderId: string }>;
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
  consent?: { ok: boolean; sites: Set<string>; reason: string }; // private/consent.json; absent = no consent
  paused: boolean;
  openBreakers: Set<string>;
  token: OpenToken | null;
  stagedPath: string | null;
  dwell: DwellLock | null;
  commitsUsed: number;
  currentUrl: string | null;
  lookupRef: (ref: string) => RefInfo | undefined;
  realpath: (p: string) => string;
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
};

// One fill or commit action that the runtime appends to state/guard/<token>.jsonl (12.17).
export type TokenRecord = {
  class: "fill" | "commit";
  action: string;
  ref: string | null;
  role: string | null;
  name: string | null;
};

export type Decision =
  | { kind: "pass"; command?: string }
  | {
      kind: "allow";
      params?: Record<string, unknown>;
      records?: TokenRecord[];
      token?: string;
      actionClass?: string;
      command?: string;
      host?: string | null;
    }
  | { kind: "block"; code: string; reason: string; command?: string; actionClass?: string; host?: string | null };

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
] as const;
