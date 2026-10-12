// Type declarations for busymeter (hand-written; keep in step with lib/client.js and lib/tokens.js).

/** Star counts 0-6 (0 = lip accent only) or the red/blue Flash. */
export type Level = 0 | 1 | 2 | 3 | 4 | 5 | 6 | 'flash';
/** What status replies may say: a level, or "off" (paused or meter off). */
export type ShownLevel = Level | 'off';
export type Want = 'signals' | 'status' | 'taps' | 'effects';
export type Effect = 'lip-pulse' | 'star-sweep' | 'flash-short';
export type SourceKind = 'camera' | 'microphone' | 'teams' | 'slack' | 'calendar' | 'plugin' | 'manual' | 'none';

export const DEFAULT_PORT: 47820;
export const API_VERSION: 1;
export const LEVELS: readonly Level[];
export const WANTS: readonly Want[];
export const EFFECTS: readonly Effect[];
export const MAX_TTL_SECONDS: 600;
export const CONNECT_TIMEOUT_MS: 1000;
export const MAX_RESPONSE_BYTES: 65536;

// ----- errors -----

/** An error from the API (or the connection). `code` is the API's error code, `status` the HTTP status. */
export class BusyMeterError extends Error {
  constructor(message: string, code?: string, status?: number | null, retryAfter?: number | null, options?: { cause?: unknown });
  /** A message you can show as is. */
  message: string;
  code: string;
  status: number | null;
  /** Seconds, when the app said (429). */
  retryAfter: number | null;
}
/** Nothing answers on the port: the app isn't running or External connections is off. */
export class NotListeningError extends BusyMeterError {}
/** Something accepted the connection but didn't answer in time, or the connection broke mid-request. */
export class AppNotRespondingError extends BusyMeterError {}
/** 401: no token, or Busy Meter doesn't know it (revoked or removed). */
export class UnauthorizedError extends BusyMeterError {}
/** 403: switched off, or not allowed by the approval. */
export class ForbiddenError extends BusyMeterError {}
/** 409: a request is already waiting, too many plugins, or an alert was ignored. */
export class ConflictError extends BusyMeterError {}
/** 429: too fast. `retryAfter` is in seconds when the app said. */
export class RateLimitedError extends BusyMeterError {}
/** The user clicked Deny (code "denied"), or didn't answer within 2 minutes (code "expired"). */
export class DeniedError extends BusyMeterError {}
/** Stopped waiting for approval because the AbortSignal aborted. The request stays saved (see withdraw()). */
export class CancelledError extends BusyMeterError {}
/** Still waiting for the user after the time this client was told to wait. */
export class ApprovalTimeoutError extends BusyMeterError {}

// ----- discovery and helpers -----

export class Endpoint {
  constructor(port: number, fromFile: boolean, explicit?: boolean);
  readonly port: number;
  readonly fromFile: boolean;
  readonly explicit: boolean;
  /** True when the port came from the app's discovery file (or the caller named it). Tokens go only there. */
  readonly trusted: boolean;
  readonly baseUrl: string;
}

/** %LOCALAPPDATA%\BusyMeter\plugin-api.json, or BUSYMETER_DISCOVERY_FILE. */
export function discoveryPath(): string;
/** The port from the discovery file, or 47820 (untrusted) when there is none. The host is always 127.0.0.1. */
export function discover(file?: string | null): Endpoint;
/** Seconds from 30, "30", "30s", "10m", "1.5h". At least 1 second. Throws TypeError. */
export function parseDuration(value: number | string): number;
/** 0-6 or "flash" (also "0".."6" as text). Throws TypeError. */
export function normalizeLevel(level: number | string): Level;
/** 1-600, else TypeError. */
export function checkTtl(ttlSeconds: number): number;
export function isValidPluginId(pluginId: unknown): boolean;

export interface SignalDeclaration {
  /** 1-48 of a-z, 0-9 and '-', not starting with "level-". */
  name: string;
  label?: string;
  suggest?: Level;
}
/** A signal as an object, a [name, label?, suggest?] array, or a plain name. */
export type SignalInput = string | { name: string; label?: string | null; suggest?: Level | string | null } | [string, (string | null)?, (Level | string | null)?];
export function signalDeclarations(signals: Iterable<SignalInput> | null | undefined): SignalDeclaration[];

// ----- token store -----

/** The token folder: BUSYMETER_TOKEN_DIR, else %LOCALAPPDATA%\busymeter-plugins\tokens (Windows) or $XDG_CONFIG_HOME/busymeter-plugins/tokens. */
export function defaultTokenDir(): string;

export interface TokenFile {
  pluginId: string;
  token?: string;
  signals?: SignalDeclaration[];
  wants?: string[];
  savedAt?: number;
  pendingRequestId?: string;
  requestedSignals?: SignalDeclaration[];
  requestedWants?: string[];
  requestedAt?: number;
  [key: string]: unknown;
}

export interface ProgramResult {
  status: number | null;
  stdout: string;
  stderr: string;
  error?: Error;
}

/** Per-user token files, in the same format and folder as the Python client. */
export class TokenStore {
  constructor(
    directory?: string | null,
    options?: {
      /** Narrow the folder to the current user before every write (default true). */
      restrict?: boolean;
      /** Replaces the program runner (whoami, icacls), for tests. */
      run?: (file: string, args: string[], timeoutMs: number) => ProgramResult;
      /** Replaces the warning when the folder can't be narrowed (default process.emitWarning, type PermissionWarning). */
      warn?: (message: string) => void;
    },
  );
  readonly directory: string;
  path(pluginId: string): string;
  load(pluginId: string): TokenFile | null;
  savePending(pluginId: string, requestId: string, signals: SignalDeclaration[], wants: string[]): void;
  clearPending(pluginId: string): void;
  saveToken(pluginId: string, token: string): void;
  dropToken(pluginId: string): void;
  delete(pluginId: string): void;
  pluginIds(): string[];
  ensureDir(): void;
}

// ----- the client -----

export interface Status {
  level: ShownLevel;
  paused: boolean;
  source: SourceKind;
  /** Only when this plugin is the winning source. */
  plugin?: string;
  [key: string]: unknown;
}

export interface SignalsReply {
  active: string[];
  expiresInSeconds: number;
  /** Only for plugins with "status". */
  status?: Status;
  [key: string]: unknown;
}

export type PollReply =
  | { state: 'pending' }
  | { state: 'denied'; reason: 'denied' | 'expired' }
  | { state: 'approved'; pluginId: string; token: string };

/** withdraw(): "withdrawn", the poll's answer when the user answered first, "not_found", or "none" (nothing waiting). */
export type WithdrawResult = { state: 'withdrawn' } | PollReply | { state: 'not_found' } | { state: 'none' };

export interface RegisterOptions {
  name?: string | null;
  signals?: Iterable<SignalInput>;
  /** Default ["signals", "status"]. */
  wants?: Iterable<Want | string>;
  version?: string | null;
}

export interface WaitOptions {
  /** Default 125000 (the app expires a request after 2 minutes). */
  timeoutMs?: number;
  /** Default 1000. */
  intervalMs?: number;
  /** Called once, after the first poll that is still pending. */
  onWait?: (() => void) | null;
  /** Aborting stops the wait with CancelledError (the request stays saved). */
  signal?: AbortSignal | null;
}

export interface ConnectOptions extends RegisterOptions {
  /** How long to wait for Allow (default 125000). */
  timeoutMs?: number;
  /** Poll interval while waiting (default 1000). */
  intervalMs?: number;
  onWait?: (() => void) | null;
  signal?: AbortSignal | null;
}

export interface BusyMeterOptions {
  /** Use this port (trusted with the token) instead of the discovery file. */
  port?: number | null;
  /** Use this token instead of the saved one. */
  token?: string | null;
  store?: TokenStore | null;
  /** Read timeout per request (default 10000). Connecting has its own 1 s limit. */
  timeoutMs?: number;
  /** Read this discovery file instead of the default. */
  discoveryFile?: string | null;
}

/** One plugin's connection to the Busy Meter app. */
export class BusyMeter {
  /** pluginId: 3-64 of a-z, 0-9, '.' and '-' (e.g. com.example.heartrate). Throws TypeError otherwise. */
  constructor(pluginId: string, options?: BusyMeterOptions);
  readonly pluginId: string;
  store: TokenStore;
  timeoutMs: number;
  readonly endpoint: Endpoint;
  /** The token in memory, else the saved one, else null. */
  readonly token: string | null;

  /** True when the API answers (External connections is on), whether or not this plugin has a token. */
  isListening(): Promise<boolean>;
  /** True when Busy Meter still knows this plugin's token (a 401 also deletes the saved token). */
  verifyToken(options?: { signal?: AbortSignal | null }): Promise<boolean>;
  /** Run `call`; after a 401, connect again with the last connect() options and run it once more. */
  reconnecting<T>(call: () => T | Promise<T>): Promise<T>;

  /** Ask the user to allow this plugin. Resolves the request id (also kept in the token file). */
  register(options?: RegisterOptions): Promise<string>;
  /**
   * The raw answer. Saves the token when approved; clears the saved request when denied. An aborted `signal` stops a
   * poll the app hasn't started answering yet (CancelledError).
   */
  poll(requestId: string, options?: { signal?: AbortSignal | null }): Promise<PollReply>;
  /** The saved waiting request id, or null. */
  pendingRequest(): string | null;
  /** Take back a waiting registration (its tray prompt closes). Defaults to the saved pending request; never throws for "nothing waiting". */
  withdraw(requestId?: string | null): Promise<WithdrawResult>;
  /** Poll until the user answers; resolves the token. */
  waitForApproval(requestId?: string | null, options?: WaitOptions): Promise<string>;
  waitForApproval(options: WaitOptions & { requestId?: string | null }): Promise<string>;
  /**
   * The saved token if still known, else wait on a saved request, else register and wait. `signal` stops the token
   * check, the polls and the waits (never a registration already sent, so its request id is always saved). After a
   * withdrawal the app refuses a new request for 5 s (429 withdraw_pause): connect waits that out once and retries.
   */
  connect(options?: ConnectOptions): Promise<string>;
  /** Set what reconnecting() registers with after a revoke. */
  remember(options?: ConnectOptions): void;
  /** Drop the token (memory and file) but keep a waiting request. */
  forgetToken(): void;
  /** Delete this plugin's saved file. */
  forget(): void;

  /** Replace this plugin's active signals for ttlSeconds (1-600). An empty list clears. */
  setSignals(active: Iterable<string>, ttlSeconds: number): Promise<SignalsReply>;
  /** Show a level for ttlSeconds (1-600), through the user's level map. */
  setLevel(level: Level | number | string, ttlSeconds: number): Promise<SignalsReply>;
  clear(): Promise<SignalsReply>;
  /** Needs "status" in wants. */
  status(): Promise<Status>;
  /** Needs "effects" in wants; one every 10 s. Resolves when the effect is over. */
  alert(effect: Effect, options?: { color?: string | null; durationMs?: number | null }): Promise<{ shown: boolean }>;
}
