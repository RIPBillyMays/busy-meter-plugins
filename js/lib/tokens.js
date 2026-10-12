// Where plugin tokens are kept: one small JSON file per plugin id, readable by the current user only.
// Same folder and file format as the Python client (busymeter.tokens), so both can share a plugin's token.
//
// Default folder (override with the BUSYMETER_TOKEN_DIR environment variable):
//
// - Windows: %LOCALAPPDATA%\busymeter-plugins\tokens. Before every write the folder (also one named by
//   BUSYMETER_TOKEN_DIR) loses its inherited permissions and grants only the current user, by SID from
//   `whoami /user` (icacls, both run by full System32 path). If that fails a warning says so and the file is still
//   written.
// - macOS/Linux: $XDG_CONFIG_HOME/busymeter-plugins/tokens (else ~/.config/...); the folder is set to 0700 (also when
//   it already existed with looser modes), files 0600.
//
// A file looks like {"pluginId": "...", "token": "...", "signals": [...], "wants": [...]}, plus pendingRequestId while
// a registration waits for the user. The token is a bearer secret: anyone who reads it can drive this plugin's
// signals until the user revokes it under Settings > Sources > External connections.

import { spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const ID = /^[a-z0-9.-]{3,64}$/;
const PENDING_KEYS = ['pendingRequestId', 'requestedSignals', 'requestedWants', 'requestedAt'];

export function defaultTokenDir() {
  const override = process.env.BUSYMETER_TOKEN_DIR;
  if (override) return override;
  if (process.platform === 'win32') {
    const base = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
    return path.join(base, 'busymeter-plugins', 'tokens');
  }
  const base = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config');
  return path.join(base, 'busymeter-plugins', 'tokens');
}

/** Full path, so a same-named program earlier on PATH (e.g. Git's whoami) isn't run instead. */
export function system32(exe) {
  return path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', exe);
}

/** Runs a program without a shell: {status, stdout, stderr, error}. Replaceable in tests. */
export function runProgram(file, args, timeoutMs) {
  const result = spawnSync(file, args, { encoding: 'utf8', timeout: timeoutMs, windowsHide: true, shell: false });
  return { status: result.status, stdout: result.stdout || '', stderr: result.stderr || '', error: result.error };
}

function defaultWarn(message) {
  process.emitWarning(message, { type: 'PermissionWarning', code: 'BUSYMETER_PERMISSIONS' });
}

/** The current user's SID from `whoami /user` (Windows), or null. */
export function currentUserSid(run = runProgram) {
  let out;
  try {
    out = run(system32('whoami.exe'), ['/user', '/fo', 'csv', '/nh'], 15000);
  } catch {
    return null;
  }
  if (!out || out.error || out.status !== 0) return null;
  const match = /"(S-1-[0-9-]+)"/.exec(out.stdout || '');
  return match ? match[1] : null;
}

/** Remove inherited ACEs and grant only the current user (by SID) full control. False (and a warning) on failure. */
export function restrictWindowsDir(directory, { run = runProgram, warn = defaultWarn } = {}) {
  const sid = currentUserSid(run);
  if (sid === null) {
    warn(`Couldn't find the current user's SID; ${directory} keeps its inherited permissions.`);
    return false;
  }
  // The folder: no inherited entries, only the user (inherited by files and folders created in it). Files already
  // there are reset so they inherit exactly that (never /T with the folder's grant: on files it leaves no access).
  const commands = [[system32('icacls.exe'), [directory, '/inheritance:r', '/grant:r', `*${sid}:(OI)(CI)F`, '/Q']]];
  if (fs.readdirSync(directory).length > 0) {
    commands.push([system32('icacls.exe'), [path.join(directory, '*'), '/reset', '/C', '/Q']]);
  }
  for (const [file, args] of commands) {
    let result;
    try {
      result = run(file, args, 30000);
    } catch (e) {
      warn(`Couldn't narrow the permissions of ${directory}: ${e && e.message}`);
      return false;
    }
    if (result.error) {
      warn(`Couldn't narrow the permissions of ${directory}: ${result.error.message}`);
      return false;
    }
    if (result.status !== 0) {
      const detail = (result.stderr || result.stdout || '').trim();
      warn(`Couldn't narrow the permissions of ${directory} (icacls ${result.status}): ${detail}`);
      return false;
    }
  }
  return true;
}

/** Create `directory` if needed and narrow it to the current user. */
export function ensurePrivateDir(directory, { restrict = true, run, warn, platform = process.platform } = {}) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  if (!restrict) return;
  if (platform === 'win32') {
    restrictWindowsDir(directory, { run, warn });
  } else {
    const mode = fs.statSync(directory).mode & 0o777;
    if (mode !== 0o700) fs.chmodSync(directory, 0o700);
  }
}

/**
 * JSON like Python's json.dump(data, f, indent=2): non-ASCII written as \uXXXX escapes. Python writes the file in
 * text mode, so its lines end in os.EOL (\r\n on Windows); pass `eol` to match.
 */
export function pythonJson(data, eol = '\n') {
  const text = JSON.stringify(data, null, 2).replace(/[\u0080-\uffff]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
  return eol === '\n' ? text : text.replace(/\n/g, eol); // JSON strings never hold a raw newline
}

const now = () => Math.floor(Date.now() / 1000);

export class TokenStore {
  /**
   * Per-user token files. `directory` defaults to defaultTokenDir().
   * `run` and `warn` replace the program runner and the warning (tests).
   */
  constructor(directory = null, { restrict = true, run, warn } = {}) {
    this.directory = directory != null ? String(directory) : defaultTokenDir();
    this._restrict = restrict;
    this._run = run;
    this._warn = warn;
  }

  path(pluginId) {
    if (typeof pluginId !== 'string' || !ID.test(pluginId)) throw new TypeError('Not a plugin id.');
    return path.join(this.directory, `${pluginId}.json`);
  }

  load(pluginId) {
    let data;
    try {
      data = JSON.parse(fs.readFileSync(this.path(pluginId), 'utf8'));
    } catch {
      return null; // no file, unreadable, not JSON, or not a plugin id
    }
    return data !== null && typeof data === 'object' && !Array.isArray(data) && data.pluginId === pluginId ? data : null;
  }

  savePending(pluginId, requestId, signals, wants) {
    const data = this.load(pluginId) || { pluginId };
    Object.assign(data, { pendingRequestId: requestId, requestedSignals: signals, requestedWants: wants, requestedAt: now() });
    this._write(pluginId, data);
  }

  clearPending(pluginId) {
    const data = this.load(pluginId);
    if (data === null) return;
    for (const key of PENDING_KEYS) delete data[key];
    if (data.token) this._write(pluginId, data);
    else this.delete(pluginId);
  }

  saveToken(pluginId, token) {
    const data = this.load(pluginId) || { pluginId };
    if ('requestedSignals' in data) {
      data.signals = data.requestedSignals;
      delete data.requestedSignals;
    }
    if ('requestedWants' in data) {
      data.wants = data.requestedWants;
      delete data.requestedWants;
    }
    delete data.pendingRequestId;
    delete data.requestedAt;
    data.token = token;
    data.savedAt = now();
    this._write(pluginId, data);
  }

  /** Remove the token but keep a waiting request (and what it asked for). */
  dropToken(pluginId) {
    const data = this.load(pluginId);
    if (data === null || !('token' in data)) return;
    delete data.token;
    delete data.savedAt;
    if (data.pendingRequestId) this._write(pluginId, data);
    else this.delete(pluginId);
  }

  delete(pluginId) {
    try {
      fs.unlinkSync(this.path(pluginId));
    } catch (e) {
      if (e.code !== 'ENOENT') throw e;
    }
  }

  pluginIds() {
    try {
      return fs
        .readdirSync(this.directory)
        .filter((name) => name.endsWith('.json'))
        .map((name) => name.slice(0, -'.json'.length))
        .filter((stem) => ID.test(stem))
        .sort();
    } catch {
      return [];
    }
  }

  /** Create the folder if needed and narrow it to the current user, every time (it may have been loosened). */
  ensureDir() {
    ensurePrivateDir(this.directory, { restrict: this._restrict, run: this._run, warn: this._warn });
  }

  _write(pluginId, data) {
    this.ensureDir();
    const target = this.path(pluginId);
    const tmp = path.join(this.directory, '.tmp-' + randomBytes(8).toString('hex'));
    try {
      fs.writeFileSync(tmp, pythonJson(data, os.EOL), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
      if (process.platform !== 'win32') fs.chmodSync(tmp, 0o600);
      fs.renameSync(tmp, target);
    } catch (e) {
      try {
        fs.unlinkSync(tmp);
      } catch {
        // already gone
      }
      throw e;
    }
  }
}
