// Thin client for the Busy Meter plugin API (spec: ../../spec/openapi.yaml). Node built-ins only.
//
// The Busy Meter Windows app listens on 127.0.0.1 only, and only while the user has turned on
// Settings > Sources > External connections. A program registers, the user clicks Allow in the Busy Meter tray, and
// the program gets a token, which this client keeps in a per-user file (see ./tokens.js). Nothing here can approve a
// request: only the user can.
//
// Requests go through node:http with its own Agent (never the global fetch): the app refuses any request that
// carries an Origin header, and node:http only sends the headers set here.

import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { performance } from 'node:perf_hooks';

import { TokenStore } from './tokens.js';

export const DEFAULT_PORT = 47820;
export const API_VERSION = 1;
export const LEVELS = Object.freeze([0, 1, 2, 3, 4, 5, 6, 'flash']);
export const WANTS = Object.freeze(['signals', 'status', 'taps', 'effects']);
export const EFFECTS = Object.freeze(['lip-pulse', 'star-sweep', 'flash-short']);
export const MAX_TTL_SECONDS = 600;
export const CONNECT_TIMEOUT_MS = 1000;
export const MAX_RESPONSE_BYTES = 64 * 1024;

export const NOT_RUNNING_MESSAGE =
  "Busy Meter isn't running, or External connections is off (there's no %LOCALAPPDATA%\\BusyMeter\\" +
  'plugin-api.json, which the app writes while it listens). Turn on Settings → Sources → External ' +
  'connections in the Busy Meter app.';
export const NOT_RESPONDING_MESSAGE =
  "Busy Meter didn't answer in time (the app may be busy or stuck). Try again; if it keeps happening, restart the " +
  'Busy Meter app.';
export const TURN_ON_MESSAGE =
  "Busy Meter isn't accepting connections. Turn on Settings → Sources → External connections " +
  'in the Busy Meter app (and make sure the app is running).';
const STOPPED_MESSAGE = 'Stopped before Busy Meter answered.';
const BROKE_MESSAGE =
  'The connection to Busy Meter broke before it answered. Try again; if it keeps happening, restart the Busy Meter app.';

const ID = /^[a-z0-9.-]{3,64}$/;
const SIGNAL = /^[a-z0-9-]{1,48}$/;
const REQUEST_ID = /^[A-Za-z0-9_-]{1,64}$/;
const DURATION = /^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?)?\s*$/i;

// One Agent for every request, without keep-alive: each call is its own short loopback connection.
const agent = new http.Agent({ keepAlive: false });

// ----- errors -----

/** An error from the API (or the connection). `code` is the API's error code, `status` the HTTP status. */
export class BusyMeterError extends Error {
  constructor(message, code = 'error', status = null, retryAfter = null, options = undefined) {
    super(message, options);
    this.name = new.target.name;
    this.message = message;
    this.code = code;
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

/** Nothing answers on the port: the app isn't running or External connections is off. */
export class NotListeningError extends BusyMeterError {}
/** Something accepted the connection but didn't answer in time, or the connection broke mid-request. */
export class AppNotRespondingError extends BusyMeterError {}
/** 401: no token, or Busy Meter doesn't know it (revoked or removed). */
export class UnauthorizedError extends BusyMeterError {}
/** 403: switched off, not allowed by the approval, or (never from this client) an Origin header. */
export class ForbiddenError extends BusyMeterError {}
/** 409: a request is already waiting, too many plugins, or an alert was ignored. */
export class ConflictError extends BusyMeterError {}
/** 429: too fast. `retryAfter` is in seconds when the app said. */
export class RateLimitedError extends BusyMeterError {}
/** The user clicked Deny, or didn't answer within 2 minutes. */
export class DeniedError extends BusyMeterError {}
/**
 * Stopped waiting for approval because the caller's AbortSignal fired. A request already shown in the tray stays
 * there until the user answers or it expires (2 minutes); its id stays saved, so the next connect() waits on it
 * instead of asking again (or call withdraw() to close the prompt).
 */
export class CancelledError extends BusyMeterError {}
/** Still waiting for the user after the time this client was told to wait. */
export class ApprovalTimeoutError extends BusyMeterError {}

const FRIENDLY = {
  unauthorized:
    "Busy Meter doesn't know this plugin's token (it was revoked, or never approved). " +
    "Register again; you'll be asked to click Allow in the Busy Meter tray.",
  plugin_off: 'This plugin is switched off in Busy Meter. Turn it on under Settings → Sources → External connections.',
  not_allowed:
    "Busy Meter didn't allow this plugin to do that. Register again asking for it in 'wants' " +
    '(the user approves the new request in the tray).',
  already_pending:
    'A request for this plugin id is already waiting for you in the Busy Meter tray. Click Allow (or Deny) there.',
  too_many_plugins:
    'Busy Meter already has the most plugins it takes (32). Revoke one under Settings → Sources → External connections.',
  cooldown: "This plugin was denied in the last 10 minutes; Busy Meter won't ask again until then.",
  withdraw_pause: 'This plugin just withdrew a request; Busy Meter takes a new one after 5 seconds.',
  too_many_pending: 'Too many requests are waiting in the Busy Meter tray; answer those first.',
  forbidden_origin: "Busy Meter refused the request because it carried an Origin header (web pages can't use the API).",
  unknown_signal: "That signal wasn't declared when the plugin registered. Use a declared signal or a level.",
};

// ----- discovery and small helpers -----

export class Endpoint {
  constructor(port, fromFile, explicit = false) {
    this.port = port;
    this.fromFile = fromFile;
    this.explicit = explicit;
    Object.freeze(this);
  }

  /** True when the port came from the app's discovery file (or the caller named it). Tokens go only there. */
  get trusted() {
    return this.fromFile || this.explicit;
  }

  get baseUrl() {
    return `http://127.0.0.1:${this.port}/v1/`;
  }
}

/** %LOCALAPPDATA%\BusyMeter\plugin-api.json (written by the app only while it listens). */
export function discoveryPath() {
  const override = process.env.BUSYMETER_DISCOVERY_FILE;
  if (override) return override;
  const base = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  return path.join(base, 'BusyMeter', 'plugin-api.json');
}

/** The port from the discovery file, or 47820 when there is none (the app may be off). Only the port is taken. */
export function discover(file = null) {
  try {
    const data = JSON.parse(fs.readFileSync(file != null ? String(file) : discoveryPath(), 'utf8'));
    const port = data !== null && typeof data === 'object' && !Array.isArray(data) ? data.port : undefined;
    if (Number.isInteger(port) && port >= 1024 && port <= 65535) return new Endpoint(port, true);
  } catch {
    // no file, unreadable or not JSON: the app isn't listening
  }
  return new Endpoint(DEFAULT_PORT, false);
}

function roundHalfEven(x) {
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** Seconds from 30, "30", "30s", "10m", "1.5h". At least 1 second. */
export function parseDuration(value) {
  let seconds;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('A duration is a number of seconds or text like 30s, 10m, 1h.');
    seconds = value;
  } else if (typeof value === 'string') {
    const match = DURATION.exec(value);
    if (!match) throw new TypeError(`Can't read the duration '${value}'; use e.g. 30s, 10m or 1h.`);
    const number = Number(match[1]);
    const unit = (match[2] || 's').toLowerCase();
    seconds = number * (unit.startsWith('h') ? 3600 : unit.startsWith('m') ? 60 : 1);
  } else {
    throw new TypeError('A duration is a number of seconds or text like 30s, 10m, 1h.');
  }
  if (seconds < 1) throw new TypeError('A duration must be at least 1 second.');
  return roundHalfEven(seconds);
}

/** 0-6 or "flash" (also accepts "0".."6" as text). */
export function normalizeLevel(level) {
  if (typeof level === 'string') {
    const text = level.trim().toLowerCase();
    if (text === 'flash') return 'flash';
    if (/^[0-9]+$/.test(text)) level = Number(text);
  }
  if (Number.isInteger(level) && level >= 0 && level <= 6) return level;
  throw new TypeError('level must be 0-6 (stars; 0 = lip accent only) or "flash".');
}

export function checkTtl(ttlSeconds) {
  if (!Number.isInteger(ttlSeconds) || ttlSeconds < 1 || ttlSeconds > MAX_TTL_SECONDS) {
    throw new TypeError('ttlSeconds must be 1-600 (refresh before it runs out to keep a level longer).');
  }
  return ttlSeconds;
}

export function isValidPluginId(pluginId) {
  return typeof pluginId === 'string' && ID.test(pluginId);
}

function checkRequestId(requestId) {
  if (typeof requestId !== 'string' || !REQUEST_ID.test(requestId)) throw new TypeError('Not a request id.');
  return requestId;
}

/** Signals as the API wants them, from objects, [name, label, suggest] arrays or plain names. */
export function signalDeclarations(signals) {
  const out = [];
  for (const item of signals || []) {
    let entry;
    if (typeof item === 'string') {
      entry = { name: item };
    } else if (Array.isArray(item)) {
      entry = { name: item[0] };
      if (item.length > 1 && item[1] != null) entry.label = item[1];
      if (item.length > 2 && item[2] != null) entry.suggest = item[2];
    } else if (item !== null && typeof item === 'object') {
      entry = {};
      for (const key of ['name', 'label', 'suggest']) if (key in item && item[key] != null) entry[key] = item[key];
    } else {
      entry = { name: item };
    }
    const name = entry.name;
    if (typeof name !== 'string' || !SIGNAL.test(name) || name.startsWith('level-')) {
      throw new TypeError(`Signal name ${JSON.stringify(name)}: use 1-48 of a-z, 0-9 and '-', not starting with 'level-'.`);
    }
    if ('suggest' in entry) entry.suggest = normalizeLevel(entry.suggest);
    out.push(entry);
  }
  if (out.length > 32) throw new TypeError('At most 32 signals.');
  return out;
}

/** Waits `ms`; resolves true at once if `signal` is (or becomes) aborted, else false. */
export function abortableSleep(ms, signal) {
  return new Promise((resolve) => {
    if (signal && signal.aborted) return resolve(true);
    const onAbort = () => {
      clearTimeout(timer);
      resolve(true);
    };
    const timer = setTimeout(() => {
      if (signal) signal.removeEventListener('abort', onAbort);
      resolve(false);
    }, ms);
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
  });
}

function parseRetryAfter(value) {
  if (value == null) return null;
  const text = String(value).trim();
  if (text === '') return null;
  const n = Number(text);
  return Number.isFinite(n) ? n : null;
}

export function errorFor(status, payload, retryAfter) {
  const code = String(payload.error || 'http_' + status);
  const serverMessage = String(payload.message || '');
  let message = FRIENDLY[code] ?? (serverMessage || `Busy Meter answered ${status}.`);
  if (code === 'ignored') message = `Busy Meter ignored the alert (${serverMessage || 'busy'}).`;
  else if (code === 'rate_limited') message = 'Too fast: ' + (serverMessage || 'slow down.');
  else if (!(code in FRIENDLY) && serverMessage && message !== serverMessage) message = serverMessage;
  const cls = { 401: UnauthorizedError, 403: ForbiddenError, 409: ConflictError, 429: RateLimitedError }[status] || BusyMeterError;
  return new cls(message, code, status, parseRetryAfter(retryAfter));
}

/**
 * One HTTP exchange on 127.0.0.1:port. Resolves {status, raw, retryAfter}; rejects with NotListeningError (nothing
 * accepted the connection within 1 s) or AppNotRespondingError (no answer in time, or the connection broke).
 */
export function exchange(port, method, urlPath, headers, data, readTimeoutMs, signal = null) {
  return new Promise((resolve, reject) => {
    if (signal && signal.aborted) return reject(new CancelledError(STOPPED_MESSAGE, 'cancelled'));
    let settled = false;
    let connected = false;
    let timedOut = false;
    let connectTimer = null;
    const onAbort = () => {
      settle(reject, new CancelledError(STOPPED_MESSAGE, 'cancelled'));
      req.destroy();
    };
    const settle = (fn, value) => {
      if (settled) return;
      settled = true;
      if (connectTimer) clearTimeout(connectTimer);
      if (signal) signal.removeEventListener('abort', onAbort);
      fn(value);
    };
    const fail = (cause) => {
      if (!connected) settle(reject, new NotListeningError(TURN_ON_MESSAGE, 'not_listening', null, null, { cause }));
      else if (timedOut) settle(reject, new AppNotRespondingError(NOT_RESPONDING_MESSAGE, 'not_responding', null, null, { cause }));
      else settle(reject, new AppNotRespondingError(BROKE_MESSAGE, 'not_responding', null, null, { cause }));
    };
    const req = http.request({ host: '127.0.0.1', port, method, path: urlPath, headers, agent });
    req.on('socket', (socket) => {
      const onConnect = () => {
        connected = true;
        if (connectTimer) clearTimeout(connectTimer);
        connectTimer = null;
      };
      if (socket.connecting) {
        // Loopback connects are instant when something listens; don't sit through Windows' SYN retries.
        connectTimer = setTimeout(() => req.destroy(new Error('connect timed out')), Math.min(CONNECT_TIMEOUT_MS, readTimeoutMs));
        socket.once('connect', onConnect);
      } else {
        onConnect();
      }
    });
    req.setTimeout(readTimeoutMs, () => {
      timedOut = true;
      req.destroy(new Error('timed out'));
    });
    req.on('error', fail);
    req.on('response', (res) => {
      // Once the app has answered, read the answer even if the signal aborts now: it may hold a token that is
      // handed out only once.
      if (signal) signal.removeEventListener('abort', onAbort);
      const chunks = [];
      let size = 0;
      const finish = () =>
        settle(resolve, { status: res.statusCode, raw: Buffer.concat(chunks), retryAfter: res.headers['retry-after'] ?? null });
      res.on('data', (chunk) => {
        if (size >= MAX_RESPONSE_BYTES) return;
        const take = chunk.subarray(0, MAX_RESPONSE_BYTES - size);
        chunks.push(take);
        size += take.length;
        if (size >= MAX_RESPONSE_BYTES) {
          finish(); // read at most 64 KB, like the Python client
          res.destroy();
        }
      });
      res.on('end', finish);
      res.on('error', fail);
      res.on('close', () => {
        if (!settled) fail(new Error('response closed early'));
      });
    });
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
    req.end(data ?? undefined);
  });
}

function parsePayload(raw) {
  if (!raw || raw.length === 0) return {};
  try {
    const payload = JSON.parse(raw.toString('utf8'));
    return payload !== null && typeof payload === 'object' && !Array.isArray(payload) ? payload : {};
  } catch {
    return {};
  }
}

// ----- the client -----

const DEFAULT_WANTS = Object.freeze(['signals', 'status']);

/**
 * One plugin's connection to the Busy Meter app.
 *
 *   const meter = new BusyMeter('com.example.cli');
 *   await meter.connect({ name: 'My script' });   // registers and waits for Allow the first time
 *   await meter.setLevel(5, 600);
 */
export class BusyMeter {
  constructor(pluginId, { port = null, token = null, store = null, timeoutMs = 10000, discoveryFile = null } = {}) {
    if (!isValidPluginId(pluginId)) {
      throw new TypeError("pluginId must be 3-64 of a-z, 0-9, '.' and '-' (e.g. com.example.heartrate).");
    }
    this.pluginId = pluginId;
    this._port = port;
    this._discoveryFile = discoveryFile;
    this.store = store ?? new TokenStore();
    this._token = token;
    this.timeoutMs = timeoutMs;
    this._connectArgs = null;
  }

  // --- connection ---

  get endpoint() {
    if (this._port != null) return new Endpoint(this._port, false, true);
    return discover(this._discoveryFile);
  }

  get token() {
    if (this._token == null) {
      const saved = this.store.load(this.pluginId);
      if (saved && typeof saved.token === 'string') this._token = saved.token;
    }
    return this._token;
  }

  /** True when the API answers (External connections is on), whether or not this plugin has a token. */
  async isListening() {
    try {
      await this._request('GET', '/v1/status', null, { auth: false, raiseForStatus: false });
      return true;
    } catch (e) {
      if (e instanceof NotListeningError || e instanceof AppNotRespondingError) return false;
      throw e;
    }
  }

  /** True when Busy Meter still knows this plugin's token (a 401 also deletes the saved token). */
  async verifyToken({ signal = null } = {}) {
    if (!this.token) return false;
    try {
      await this._request('GET', '/v1/status', null, { signal });
      return true;
    } catch (e) {
      if (e instanceof UnauthorizedError) return false;
      if (e instanceof ForbiddenError) return true; // known token: switched off, or no "status" in the approval
      throw e;
    }
  }

  /**
   * Run `call`; if Busy Meter no longer knows the token (revoked), register again with the options of the last
   * connect() (the user clicks Allow again), then run it once more.
   */
  async reconnecting(call) {
    try {
      return await call();
    } catch (e) {
      if (!(e instanceof UnauthorizedError) || this._connectArgs === null) throw e;
      await this.connect(this._connectArgs);
      return await call();
    }
  }

  async _request(method, urlPath, body = null, { auth = true, raiseForStatus = true, timeoutMs = null, signal = null } = {}) {
    const headers = { Accept: 'application/json' }; // never an Origin header: the app refuses those
    if (auth) {
      const token = this.token;
      if (!token) {
        throw new UnauthorizedError(
          `No token for ${this.pluginId} yet. Register first; you'll be asked to click Allow in the Busy Meter tray.`,
          'unauthorized',
          401,
        );
      }
      headers.Authorization = 'Bearer ' + token;
    }
    let data = null;
    if (body !== null && body !== undefined) {
      data = Buffer.from(JSON.stringify(body), 'utf8');
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = String(data.length);
    }
    const endpoint = this.endpoint;
    if (auth && !endpoint.trusted) {
      // No discovery file: the app isn't listening. Whatever holds the default port isn't getting a token.
      throw new NotListeningError(NOT_RUNNING_MESSAGE, 'not_listening');
    }
    const { status, raw, retryAfter } = await exchange(endpoint.port, method, urlPath, headers, data, timeoutMs || this.timeoutMs, signal);
    const payload = parsePayload(raw);
    if (auth && status === 401) this.forgetToken(); // revoked or unknown: the next connect() registers again
    if (raiseForStatus && status >= 400) throw errorFor(status, payload, retryAfter);
    return { status, payload, retryAfter };
  }

  // --- registration ---

  /** Ask the user to allow this plugin. Returns the request id (also kept in the token file). */
  async register({ name = null, signals = [], wants = DEFAULT_WANTS, version = null } = {}) {
    const body = { id: this.pluginId, signals: signalDeclarations(signals), wants: Array.from(wants) };
    if (name) body.name = name;
    if (version) body.version = version;
    const { payload } = await this._request('POST', '/v1/register', body, { auth: false });
    const requestId = payload.requestId;
    if (typeof requestId !== 'string') throw new BusyMeterError('Busy Meter answered the registration without a request id.', 'bad_reply');
    this.store.savePending(this.pluginId, requestId, body.signals, body.wants);
    return requestId;
  }

  /** The raw answer: {state: "pending" | "denied" | "approved", ...}. Saves the token when approved. */
  async poll(requestId, { signal = null } = {}) {
    checkRequestId(requestId);
    const { payload } = await this._request('GET', '/v1/register/' + requestId, null, { auth: false, signal });
    if (payload.state === 'approved' && typeof payload.token === 'string') {
      this._token = payload.token;
      this.store.saveToken(this.pluginId, payload.token);
    } else if (payload.state === 'denied') {
      this.store.clearPending(this.pluginId);
    }
    return payload;
  }

  pendingRequest() {
    const saved = this.store.load(this.pluginId) || {};
    return typeof saved.pendingRequestId === 'string' ? saved.pendingRequestId : null;
  }

  /**
   * Take back a registration that still waits for the user (its prompt in the tray closes). Defaults to the saved
   * waiting request. Resolves:
   * - {state: "withdrawn"}: it was pending; the saved request id is cleared.
   * - the poll's answer ({state: "approved", pluginId, token} with the token saved, or {state: "denied", reason}):
   *   the user answered first.
   * - {state: "not_found"}: Busy Meter no longer knows it; the saved request id is cleared.
   * - {state: "none"}: nothing is waiting.
   * The same id can't register again for 5 seconds afterwards.
   */
  async withdraw(requestId = null) {
    requestId = requestId || this.pendingRequest();
    if (!requestId) return { state: 'none' };
    checkRequestId(requestId);
    const { status, payload, retryAfter } = await this._request('DELETE', '/v1/register/' + requestId, null, {
      auth: false,
      raiseForStatus: false,
    });
    if (status === 200) {
      this._clearPendingIf(requestId);
      return { state: 'withdrawn' };
    }
    if (status === 404) {
      this._clearPendingIf(requestId);
      return { state: 'not_found' };
    }
    if (status === 409 && payload.error === 'already_answered') {
      try {
        return await this.poll(requestId); // collects the token once, or clears a denied request
      } catch (e) {
        if (!(e instanceof BusyMeterError) || e.status !== 404) throw e;
        this._clearPendingIf(requestId); // answered and already collected (or older than 10 minutes)
        return { state: 'not_found' };
      }
    }
    throw errorFor(status, payload, retryAfter);
  }

  _clearPendingIf(requestId) {
    if (this.pendingRequest() === requestId) this.store.clearPending(this.pluginId);
  }

  /**
   * Poll until the user answers. Resolves the token; rejects with DeniedError or ApprovalTimeoutError, or
   * CancelledError as soon as `signal` (an AbortSignal) aborts.
   * Call as waitForApproval(requestId?, options?) or waitForApproval(options).
   */
  async waitForApproval(requestId = null, options = {}) {
    if (requestId !== null && typeof requestId === 'object') {
      options = requestId;
      requestId = options.requestId ?? null;
    }
    const { timeoutMs = 125000, intervalMs = 1000, onWait = null, signal = null } = options;
    requestId = requestId || this.pendingRequest();
    if (!requestId) throw new BusyMeterError('Nothing to wait for: register first.', 'no_request');
    const deadline = performance.now() + timeoutMs;
    let told = false;
    for (;;) {
      let reply;
      try {
        // Already aborted: still poll once (like the Python client), so an answer that is there is collected.
        reply = await this.poll(requestId, { signal: signal && !signal.aborted ? signal : null });
      } catch (e) {
        if (e instanceof BusyMeterError && e.status === 404) {
          this.store.clearPending(this.pluginId);
          throw new BusyMeterError(
            'Busy Meter no longer knows this request (answered over 10 minutes ago, or its token was already collected). Register again.',
            'not_found',
            404,
            null,
            { cause: e },
          );
        }
        throw e;
      }
      const state = reply.state;
      if (state === 'approved') return this._token;
      if (state === 'denied') {
        const reason = reply.reason || 'denied';
        throw new DeniedError(
          reason === 'expired'
            ? 'The request expired: nobody clicked Allow in the Busy Meter tray within 2 minutes. Try again.'
            : 'The user clicked Deny in the Busy Meter tray.',
          reason,
        );
      }
      if (!told && onWait) {
        onWait();
        told = true;
      }
      if (performance.now() >= deadline) throw new ApprovalTimeoutError('Still waiting for you to click Allow in the Busy Meter tray.', 'pending');
      if (await abortableSleep(intervalMs, signal)) throw new CancelledError('Stopped waiting for Allow.', 'cancelled');
    }
  }

  /**
   * A token for this plugin: the saved one if Busy Meter still knows it, else wait on a saved request, else register
   * and wait. A revoked token is deleted and the plugin registers again. `signal` (an AbortSignal) stops the wait
   * (CancelledError), and no new request is made once it has aborted.
   */
  async connect(options = {}) {
    const { name = null, signals = [], wants = DEFAULT_WANTS, version = null, timeoutMs = 125000, intervalMs = 1000, onWait = null, signal = null } = options;
    this.remember(options);
    if (this.token && (await this.verifyToken({ signal }))) return this.token;
    if (this.pendingRequest()) {
      try {
        return await this.waitForApproval(null, { timeoutMs, intervalMs, onWait, signal });
      } catch (e) {
        if (!(e instanceof BusyMeterError) || !['not_found', 'expired'].includes(e.code)) throw e;
      }
    }
    if (signal && signal.aborted) throw new CancelledError('Stopped before asking for Allow.', 'cancelled');
    let requestId;
    try {
      requestId = await this.register({ name, signals, wants, version });
    } catch (e) {
      // Just withdrew a request for this id: the app takes a new one after a few seconds. Wait once and retry.
      if (!(e instanceof RateLimitedError) || e.code !== 'withdraw_pause') throw e;
      const seconds = Math.min(e.retryAfter != null && e.retryAfter > 0 ? e.retryAfter : 5, 5);
      if (await abortableSleep(seconds * 1000, signal)) throw new CancelledError('Stopped before asking for Allow.', 'cancelled');
      requestId = await this.register({ name, signals, wants, version });
    }
    return await this.waitForApproval(requestId, { timeoutMs, intervalMs, onWait, signal });
  }

  /**
   * Set what reconnecting() registers with after a revoke. connect() sets it; call it after a new registration
   * (e.g. new signals) was approved through register() and poll().
   */
  remember({ name = null, signals = [], wants = DEFAULT_WANTS, version = null, timeoutMs = 125000, intervalMs = 1000, onWait = null, signal = null } = {}) {
    this._connectArgs = { name, signals: Array.from(signals || []), wants: Array.from(wants), version, timeoutMs, intervalMs, onWait, signal };
  }

  /** Drop the token (memory and file) but keep a waiting request. */
  forgetToken() {
    this._token = null;
    this.store.dropToken(this.pluginId);
  }

  /** Delete this plugin's saved token (the user can also Revoke it in the app). */
  forget() {
    this._token = null;
    this.store.delete(this.pluginId);
  }

  // --- signals, status, effects ---

  /** Replace this plugin's active signals for ttlSeconds (1-600). An empty list clears. */
  async setSignals(active, ttlSeconds) {
    const names = Array.from(active);
    const { payload } = await this._request('PUT', '/v1/signals', { active: names, ttlSeconds: checkTtl(ttlSeconds) });
    return payload;
  }

  /** Show a level (0-6 stars or "flash") for ttlSeconds (1-600), through the user's level map. */
  async setLevel(level, ttlSeconds) {
    const { payload } = await this._request('PUT', '/v1/signals', { level: normalizeLevel(level), ttlSeconds: checkTtl(ttlSeconds) });
    return payload;
  }

  async clear() {
    const { payload } = await this._request('DELETE', '/v1/signals');
    return payload;
  }

  /** {level: 0-6 | "flash" | "off", paused, source, [plugin: own id]}. Needs "status". */
  async status() {
    const { payload } = await this._request('GET', '/v1/status');
    return payload;
  }

  /** One short effect (lip-pulse, star-sweep, flash-short). Needs "effects"; one every 10 s. */
  async alert(effect, { color = null, durationMs = null } = {}) {
    if (!EFFECTS.includes(effect)) throw new TypeError('effect must be lip-pulse, star-sweep or flash-short.');
    const body = { effect };
    if (color != null) body.color = color;
    if (durationMs != null) body.durationMs = durationMs;
    const { payload } = await this._request('POST', '/v1/alert', body, { timeoutMs: Math.max(this.timeoutMs, 10000) });
    return payload;
  }
}
