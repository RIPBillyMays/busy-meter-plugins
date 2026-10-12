import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, test } from 'node:test';

import { BusyMeter, TokenStore, defaultTokenDir } from '../index.js';
import { currentUserSid, ensurePrivateDir, pythonJson, restrictWindowsDir, system32 } from '../lib/tokens.js';
import { fakeRunner, tempDir } from './helpers.mjs';

const PYTHON_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'python');

function withEnv(vars, fn) {
  const before = {};
  for (const k of Object.keys(vars)) before[k] = process.env[k];
  try {
    for (const [k, v] of Object.entries(vars)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    return fn();
  } finally {
    for (const [k, v] of Object.entries(before)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }
}

function findPython() {
  for (const name of ['python', 'python3', 'py']) {
    const r = spawnSync(name, ['-c', 'import sys; print(sys.version_info >= (3, 10))'], { encoding: 'utf8', windowsHide: true });
    if (r.status === 0 && r.stdout.trim() === 'True') return name;
  }
  return null;
}

describe('token store', () => {
  test('the folder: BUSYMETER_TOKEN_DIR, else per platform like the Python client', () => {
    withEnv({ BUSYMETER_TOKEN_DIR: 'X:\\tokens' }, () => assert.equal(defaultTokenDir(), 'X:\\tokens'));
    withEnv({ BUSYMETER_TOKEN_DIR: undefined, LOCALAPPDATA: 'C:\\L', XDG_CONFIG_HOME: '/xdg' }, () => {
      const expected = process.platform === 'win32' ? path.join('C:\\L', 'busymeter-plugins', 'tokens') : path.join('/xdg', 'busymeter-plugins', 'tokens');
      assert.equal(defaultTokenDir(), expected);
      assert.equal(new TokenStore().directory, expected);
    });
  });

  test('file location and content (the Python format)', () => {
    const dir = tempDir();
    const store = new TokenStore(dir, { run: fakeRunner().run });
    store.savePending('com.example.a', 'req1', [{ name: 'x', label: 'Ex \u2192 y', suggest: 2 }], ['signals']);
    const file = path.join(dir, 'com.example.a.json');
    let data = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.deepEqual(Object.keys(data), ['pluginId', 'pendingRequestId', 'requestedSignals', 'requestedWants', 'requestedAt']);
    assert.equal(data.pendingRequestId, 'req1');
    assert.ok(Number.isInteger(data.requestedAt));
    assert.ok(fs.readFileSync(file, 'utf8').includes('"Ex \\u2192 y"'), 'non-ASCII escaped like Python');
    store.saveToken('com.example.a', 'tok');
    data = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.deepEqual(Object.keys(data), ['pluginId', 'signals', 'wants', 'token', 'savedAt']); // Python's key order
    assert.deepEqual(data.signals, [{ name: 'x', label: 'Ex \u2192 y', suggest: 2 }]);
    assert.deepEqual(data.wants, ['signals']);
    assert.equal(data.token, 'tok');
    assert.equal(data.pendingRequestId, undefined);
    assert.deepEqual(fs.readdirSync(dir), ['com.example.a.json'], 'no temp files left');
    assert.deepEqual(store.pluginIds(), ['com.example.a']);
  });

  test('a file for another id is ignored', () => {
    const dir = tempDir();
    fs.writeFileSync(path.join(dir, 'com.example.a.json'), JSON.stringify({ pluginId: 'com.example.b', token: 't' }));
    fs.writeFileSync(path.join(dir, 'com.example.c.json'), '[1]');
    const store = new TokenStore(dir, { restrict: false });
    assert.equal(store.load('com.example.a'), null);
    assert.equal(store.load('com.example.c'), null);
    assert.equal(store.load('com.example.none'), null);
  });

  test('ids can not escape the folder', () => {
    const store = new TokenStore(tempDir(), { restrict: false });
    for (const bad of ['../x', '..\\x', 'a/b', 'C:x', 'ab', 'A.B.C']) {
      assert.throws(() => store.path(bad), TypeError);
      assert.equal(store.load(bad), null);
    }
  });

  test('clearPending and dropToken keep what is still needed', () => {
    const store = new TokenStore(tempDir(), { restrict: false });
    store.savePending('com.example.a', 'r', [], []);
    store.clearPending('com.example.a');
    assert.equal(store.load('com.example.a'), null, 'nothing left: file deleted');
    store.saveToken('com.example.a', 't');
    store.savePending('com.example.a', 'r2', [], ['signals']);
    store.clearPending('com.example.a');
    assert.equal(store.load('com.example.a').token, 't');
    store.dropToken('com.example.a');
    assert.equal(store.load('com.example.a'), null);
    store.delete('com.example.a'); // no file: fine
  });

  test('POSIX: folder 0700 (also a loose existing one), files 0600', { skip: process.platform === 'win32' && 'POSIX only' }, () => {
    const dir = path.join(tempDir(), 'tokens');
    fs.mkdirSync(dir, { mode: 0o755 });
    fs.chmodSync(dir, 0o755);
    new TokenStore(dir).saveToken('com.example.a', 't');
    assert.equal(fs.statSync(dir).mode & 0o777, 0o700);
    assert.equal(fs.statSync(path.join(dir, 'com.example.a.json')).mode & 0o777, 0o600);
  });

  test('Windows: the folder is narrowed to the user by SID, programs by full System32 path', () => {
    const dir = tempDir();
    const { run, calls } = fakeRunner('S-1-5-21-9-8-7-1001');
    assert.equal(restrictWindowsDir(dir, { run, warn: () => assert.fail('no warning expected') }), true);
    assert.deepEqual(calls[0], [system32('whoami.exe'), ['/user', '/fo', 'csv', '/nh']]);
    assert.deepEqual(calls[1], [system32('icacls.exe'), [dir, '/inheritance:r', '/grant:r', '*S-1-5-21-9-8-7-1001:(OI)(CI)F', '/Q']]);
    assert.equal(calls.length, 2, 'empty folder: no reset of files');
    fs.writeFileSync(path.join(dir, 'x.json'), '{}');
    calls.length = 0;
    restrictWindowsDir(dir, { run });
    assert.deepEqual(calls[2], [system32('icacls.exe'), [path.join(dir, '*'), '/reset', '/C', '/Q']]);
    assert.ok(path.isAbsolute(system32('icacls.exe')));
    assert.match(system32('icacls.exe'), /System32[\\/]icacls\.exe$/);
  });

  test('Windows: every write narrows the folder again', () => {
    const dir = tempDir();
    const { run, calls } = fakeRunner();
    const store = new TokenStore(dir, { run });
    store.savePending('com.example.a', 'r', [], []);
    store.saveToken('com.example.a', 't');
    const icacls = calls.filter(([f]) => f.endsWith('icacls.exe'));
    if (process.platform === 'win32') assert.ok(icacls.length >= 2);
    else assert.equal(calls.length, 0, 'POSIX uses chmod');
  });

  test('icacls failure warns and still writes; no SID warns', () => {
    const dir = tempDir();
    const warnings = [];
    ensurePrivateDir(dir, { platform: 'win32', run: fakeRunner(undefined, 5).run, warn: (m) => warnings.push(m) });
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /icacls 5\): Access is denied\./);
    ensurePrivateDir(dir, { platform: 'win32', run: fakeRunner(null).run, warn: (m) => warnings.push(m) });
    assert.match(warnings[1], /Couldn't find the current user's SID/);
    if (process.platform === 'win32') {
      const store = new TokenStore(dir, { run: fakeRunner(undefined, 5).run, warn: (m) => warnings.push(m) });
      store.saveToken('com.example.a', 't');
      assert.equal(store.load('com.example.a').token, 't');
    }
  });

  test('currentUserSid reads the CSV line', () => {
    assert.equal(currentUserSid(fakeRunner('S-1-5-21-1-2').run), 'S-1-5-21-1-2');
    assert.equal(currentUserSid(() => ({ status: 0, stdout: 'garbage' })), null);
    assert.equal(currentUserSid(() => ({ status: 1, stdout: '"x","S-1-5-1"' })), null);
    assert.equal(currentUserSid(() => { throw new Error('no'); }), null);
  });

  test('Windows: the real whoami gives this user a SID', { skip: process.platform !== 'win32' && 'Windows only' }, () => {
    assert.match(currentUserSid() || '', /^S-1-5-/);
  });

  test('pythonJson matches json.dump(indent=2)', () => {
    assert.equal(pythonJson({ a: [], b: { c: 'é' }, d: [1, { e: null }] }), '{\n  "a": [],\n  "b": {\n    "c": "\\u00e9"\n  },\n  "d": [\n    1,\n    {\n      "e": null\n    }\n  ]\n}');
  });
});

describe('token files shared with the Python client', () => {
  const python = findPython();
  const required = process.env.BUSYMETER_REQUIRE_PYTHON === '1';

  test('Python writes, JavaScript reads; JavaScript writes, Python reads', { skip: !python && !required && 'no Python 3.10+ on PATH' }, () => {
    assert.ok(python, 'BUSYMETER_REQUIRE_PYTHON=1 but no Python 3.10+ on PATH');
    const dir = tempDir();
    const script = [
      'import json, sys',
      'from busymeter.tokens import TokenStore',
      's = TokenStore(sys.argv[1], restrict=False)',
      'if sys.argv[2] == "write":',
      '    s.save_pending("com.example.py", "req-py", [{"name": "deep-work", "label": "Deep \\u2192 work", "suggest": 2}], ["signals", "status"])',
      '    s.save_token("com.example.py", "py-token")',
      '    s.save_pending("com.example.py", "req-py-2", [], ["signals"])',
      'else:',
      '    print(json.dumps(s.load("com.example.js")))',
    ].join('\n');
    const env = { ...process.env, PYTHONPATH: PYTHON_DIR, PYTHONIOENCODING: 'utf-8' };
    let r = spawnSync(python, ['-c', script, dir, 'write'], { encoding: 'utf8', env, windowsHide: true });
    assert.equal(r.status, 0, r.stderr);

    const text = fs.readFileSync(path.join(dir, 'com.example.py.json'), 'utf8');
    assert.equal(pythonJson(JSON.parse(text), os.EOL), text, 'same bytes as Python writes');
    const meter = new BusyMeter('com.example.py', { store: new TokenStore(dir, { restrict: false }), port: 1 });
    assert.equal(meter.token, 'py-token');
    assert.equal(meter.pendingRequest(), 'req-py-2');
    assert.deepEqual(meter.store.load('com.example.py').signals, [{ name: 'deep-work', label: 'Deep \u2192 work', suggest: 2 }]);

    const js = new TokenStore(dir, { restrict: false });
    js.savePending('com.example.js', 'req-js', [{ name: 'a', suggest: 'flash' }], ['signals']);
    js.saveToken('com.example.js', 'js-token');
    r = spawnSync(python, ['-c', script, dir, 'read'], { encoding: 'utf8', env, windowsHide: true });
    assert.equal(r.status, 0, r.stderr);
    const loaded = JSON.parse(r.stdout);
    assert.equal(loaded.token, 'js-token');
    assert.deepEqual(loaded.signals, [{ name: 'a', suggest: 'flash' }]);
    assert.deepEqual(loaded.wants, ['signals']);
  });
});
