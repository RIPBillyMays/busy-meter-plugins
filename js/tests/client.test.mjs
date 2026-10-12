import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { test, describe } from 'node:test';

import {
  AppNotRespondingError,
  ApprovalTimeoutError,
  BusyMeter,
  BusyMeterError,
  CancelledError,
  ConflictError,
  DEFAULT_PORT,
  DeniedError,
  ForbiddenError,
  NotListeningError,
  RateLimitedError,
  TokenStore,
  UnauthorizedError,
  checkTtl,
  discover,
  discoveryPath,
  normalizeLevel,
  parseDuration,
  signalDeclarations,
} from '../index.js';
import { eventually, setup, tempDir, writeDiscovery } from './helpers.mjs';

const fast = { intervalMs: 10 };
const bodyOf = (r) => JSON.parse(r.body.toString('utf8'));

describe('discovery', () => {
  test('the port comes from the discovery file', () => {
    const dir = tempDir();
    const ep = discover(writeDiscovery(dir, 51234));
    assert.equal(ep.port, 51234);
    assert.equal(ep.fromFile, true);
    assert.equal(ep.trusted, true);
    assert.equal(ep.baseUrl, 'http://127.0.0.1:51234/v1/');
  });

  test('no file falls back to 47820, untrusted', () => {
    const ep = discover(path.join(tempDir(), 'missing.json'));
    assert.equal(ep.port, DEFAULT_PORT);
    assert.equal(ep.fromFile, false);
    assert.equal(ep.trusted, false);
  });

  test('bad files fall back', () => {
    const dir = tempDir();
    const file = path.join(dir, 'd.json');
    for (const text of ['not json', '[]', 'null', '{"port":"51234"}', '{"port":80}', '{"port":70000}', '{"port":1.5}', '{"port":true}', '{}']) {
      fs.writeFileSync(file, text);
      const ep = discover(file);
      assert.equal(ep.port, DEFAULT_PORT, text);
      assert.equal(ep.fromFile, false, text);
    }
  });

  test('only the port is taken, never another host', async (t) => {
    const { api, dir, meter } = await setup(t);
    fs.writeFileSync(path.join(dir, 'plugin-api.json'), JSON.stringify({ port: api.port, baseUrl: 'http://evil.example:1/v1/', host: 'evil.example' }));
    assert.equal(meter.endpoint.port, api.port);
    assert.equal(await meter.isListening(), true);
    assert.equal(api.requests[0].headers.host, `127.0.0.1:${api.port}`);
  });

  test('BUSYMETER_DISCOVERY_FILE overrides the path', () => {
    const before = process.env.BUSYMETER_DISCOVERY_FILE;
    try {
      const file = writeDiscovery(tempDir(), 40001);
      process.env.BUSYMETER_DISCOVERY_FILE = file;
      assert.equal(discoveryPath(), file);
      assert.equal(discover().port, 40001);
      assert.equal(new BusyMeter('com.example.x', { store: new TokenStore(tempDir()) }).endpoint.port, 40001);
      delete process.env.BUSYMETER_DISCOVERY_FILE;
      const before2 = process.env.LOCALAPPDATA;
      process.env.LOCALAPPDATA = 'C:\\Local';
      assert.equal(discoveryPath(), path.join('C:\\Local', 'BusyMeter', 'plugin-api.json'));
      if (before2 === undefined) delete process.env.LOCALAPPDATA;
      else process.env.LOCALAPPDATA = before2;
    } finally {
      process.env.BUSYMETER_DISCOVERY_FILE = before;
    }
  });

  test('an explicit port is trusted', () => {
    const ep = new BusyMeter('com.example.x', { port: 40002, store: new TokenStore(tempDir()) }).endpoint;
    assert.equal(ep.port, 40002);
    assert.equal(ep.explicit, true);
    assert.equal(ep.trusted, true);
  });

  test('no discovery file: no token is sent anywhere (no request at all)', async (t) => {
    const { api, meter, dir } = await setup(t, { meterOptions: { token: 'secret-token' } });
    fs.rmSync(path.join(dir, 'plugin-api.json'));
    assert.equal(meter.endpoint.trusted, false);
    for (const fn of [() => meter.setLevel(3, 30), () => meter.status(), () => meter.clear(), () => meter.setSignals(['a'], 30), () => meter.alert('lip-pulse')]) {
      await assert.rejects(fn(), (e) => e instanceof NotListeningError && e.code === 'not_listening' && /External\s+connections/.test(e.message));
    }
    assert.equal(api.requests.length, 0);
  });
});

describe('validation', () => {
  test('plugin ids', () => {
    for (const bad of ['ab', 'A.b.c', 'com/example', 'x'.repeat(65), '', 'com example', '..\\x', null, 5]) {
      assert.throws(() => new BusyMeter(bad, { store: new TokenStore(tempDir()) }), TypeError, String(bad));
    }
    new BusyMeter('com.example.ok-1', { store: new TokenStore(tempDir()) });
  });

  test('levels', () => {
    assert.equal(normalizeLevel(0), 0);
    assert.equal(normalizeLevel(6), 6);
    assert.equal(normalizeLevel('5'), 5);
    assert.equal(normalizeLevel(' Flash '), 'flash');
    assert.equal(normalizeLevel('FLASH'), 'flash');
    for (const bad of [7, -1, 2.5, '7', 'off', '', true, false, null, undefined, '1e1', NaN]) assert.throws(() => normalizeLevel(bad), TypeError, String(bad));
  });

  test('ttl', () => {
    assert.equal(checkTtl(1), 1);
    assert.equal(checkTtl(600), 600);
    for (const bad of [0, 601, 1.5, '30', true, null]) assert.throws(() => checkTtl(bad), TypeError, String(bad));
  });

  test('durations', () => {
    assert.equal(parseDuration(30), 30);
    assert.equal(parseDuration('30'), 30);
    assert.equal(parseDuration('30s'), 30);
    assert.equal(parseDuration('10m'), 600);
    assert.equal(parseDuration('10 min'), 600);
    assert.equal(parseDuration('1.5h'), 5400);
    assert.equal(parseDuration('2 Hours'), 7200);
    assert.equal(parseDuration('2.5'), 2); // rounds half to even, like Python's round()
    assert.equal(parseDuration('3.5'), 4);
    for (const bad of ['0.4', 0, '10x', 'm', '', '-5', true, null, Infinity]) assert.throws(() => parseDuration(bad), TypeError, String(bad));
  });

  test('signal declarations', () => {
    assert.deepEqual(signalDeclarations(['a', ['b', 'B', '5'], { name: 'c', label: null, suggest: 'flash' }, ['d']]), [
      { name: 'a' },
      { name: 'b', label: 'B', suggest: 5 },
      { name: 'c', suggest: 'flash' },
      { name: 'd' },
    ]);
    assert.deepEqual(signalDeclarations(null), []);
    for (const bad of [['level-5'], ['Upper'], ['a b'], ['x'.repeat(49)], [{ name: 'a', suggest: 9 }], [{}], [5]]) {
      assert.throws(() => signalDeclarations(bad), TypeError, JSON.stringify(bad));
    }
    assert.throws(() => signalDeclarations(Array.from({ length: 33 }, (_, i) => `s${i}`)), TypeError);
  });

  test('calls refuse bad input before sending anything', async (t) => {
    const { api, meter } = await setup(t, { meterOptions: { token: 'tok' } });
    await assert.rejects(meter.setLevel(7, 30), TypeError);
    await assert.rejects(meter.setLevel(3, 601), TypeError);
    await assert.rejects(meter.setSignals(['a'], 0), TypeError);
    await assert.rejects(meter.alert('rainbow'), TypeError);
    await assert.rejects(meter.poll('bad id!'), TypeError);
    await assert.rejects(meter.poll('x?y=1'), TypeError);
    await assert.rejects(meter.withdraw('../signals'), TypeError);
    await assert.rejects(meter.register({ signals: ['level-1'] }), TypeError);
    assert.equal(api.requests.length, 0);
  });
});

describe('requests', () => {
  test('are what the API requires: JSON bodies, bearer token, no Origin, no query strings, Host 127.0.0.1:port', async (t) => {
    const { api, meter } = await setup(t);
    const token = api.approveToken('com.example.test', ['signals', 'status', 'effects']);
    meter._token = token;
    assert.deepEqual(await meter.setLevel('flash', 30), { active: ['level-flash'], expiresInSeconds: 30 });
    assert.deepEqual(await meter.setSignals(['a', 'b'], 20), { active: ['a', 'b'], expiresInSeconds: 20 });
    assert.deepEqual(await meter.status(), { level: 4, paused: false, source: 'teams' });
    assert.deepEqual(await meter.alert('star-sweep', { color: '#F0C850', durationMs: 500 }), { shown: true });
    assert.deepEqual(await meter.clear(), { active: [], expiresInSeconds: 0 });
    await meter.isListening();

    const [put1, put2, get, alert, del, probe] = api.requests;
    assert.deepEqual([put1.method, put1.path], ['PUT', '/v1/signals']);
    assert.deepEqual(bodyOf(put1), { level: 'flash', ttlSeconds: 30 });
    assert.deepEqual(bodyOf(put2), { active: ['a', 'b'], ttlSeconds: 20 });
    assert.deepEqual([get.method, get.path], ['GET', '/v1/status']);
    assert.deepEqual(bodyOf(alert), { effect: 'star-sweep', color: '#F0C850', durationMs: 500 });
    assert.deepEqual([del.method, del.path], ['DELETE', '/v1/signals']);
    for (const r of api.requests) {
      const names = r.rawHeaders.filter((_, i) => i % 2 === 0).map((h) => h.toLowerCase());
      assert.ok(!names.includes('origin'), `${r.method} ${r.path} sent an Origin header`);
      assert.ok(!names.includes('referer'));
      assert.ok(!r.path.includes('?'));
      assert.equal(r.headers.host, `127.0.0.1:${api.port}`);
      assert.equal(r.headers.accept, 'application/json');
      if (r.body.length > 0) {
        assert.equal(r.headers['content-type'], 'application/json');
        assert.equal(r.headers['content-length'], String(r.body.length));
        assert.equal(r.headers['transfer-encoding'], undefined);
      }
    }
    for (const r of [put1, put2, get, alert, del]) assert.equal(r.headers.authorization, 'Bearer ' + token);
    assert.equal(probe.headers.authorization, undefined);
  });

  test('a request with an Origin would be refused (the fake applies the app rule)', async (t) => {
    const { api } = await setup(t);
    const http = await import('node:http');
    const status = await new Promise((resolve) => {
      http.get({ host: '127.0.0.1', port: api.port, path: '/v1/status', headers: { Origin: 'http://x' } }, (res) => {
        res.resume();
        resolve(res.statusCode);
      });
    });
    assert.equal(status, 403);
  });

  test('isListening is true on any answer, even 401', async (t) => {
    const { meter } = await setup(t);
    assert.equal(await meter.isListening(), true);
  });
});

describe('errors', () => {
  async function failing(t, status, body, call = (m) => m.setLevel(3, 30)) {
    const { api, meter } = await setup(t);
    meter._token = api.approveToken('com.example.test');
    api.forced.set('PUT /v1/signals', [status, body]);
    api.forced.set('GET /v1/status', [status, body]);
    api.forced.set('POST /v1/alert', [status, body]);
    try {
      await call(meter);
    } catch (e) {
      return { e, meter, api };
    }
    assert.fail('no error');
  }

  test('401 says register again and deletes the saved token', async (t) => {
    const { api, meter, store } = await setup(t);
    store.saveToken('com.example.test', 'revoked');
    const err = await meter.setLevel(3, 30).catch((e) => e);
    assert.ok(err instanceof UnauthorizedError);
    assert.equal(err.status, 401);
    assert.equal(err.code, 'unauthorized');
    assert.match(err.message, /Register again; you'll be asked to click Allow/);
    assert.equal(store.load('com.example.test'), null);
    assert.equal(meter.token, null);
    assert.equal(api.requests.length, 1);
  });

  test('no token yet is a clear error without a request', async (t) => {
    const { api, meter } = await setup(t);
    const err = await meter.status().catch((e) => e);
    assert.ok(err instanceof UnauthorizedError);
    assert.match(err.message, /No token for com\.example\.test yet/);
    assert.equal(api.requests.length, 0);
  });

  test('403 plugin_off and not_allowed', async (t) => {
    let { e } = await failing(t, 403, { error: 'plugin_off', message: 'x' });
    assert.ok(e instanceof ForbiddenError);
    assert.match(e.message, /switched off in Busy Meter/);
    ({ e } = await failing(t, 403, { error: 'not_allowed', message: 'x' }, (m) => m.status()));
    assert.ok(e instanceof ForbiddenError);
    assert.equal(e.code, 'not_allowed');
    assert.match(e.message, /in 'wants'/);
    ({ e } = await failing(t, 403, { error: 'forbidden_origin', message: 'x' }));
    assert.match(e.message, /Origin header/);
  });

  test('409 and 429', async (t) => {
    let { e } = await failing(t, 409, { error: 'ignored', message: 'paused' }, (m) => m.alert('lip-pulse'));
    assert.ok(e instanceof ConflictError);
    assert.equal(e.message, 'Busy Meter ignored the alert (paused).');
    ({ e } = await failing(t, 409, { error: 'ignored' }, (m) => m.alert('lip-pulse')));
    assert.equal(e.message, 'Busy Meter ignored the alert (busy).');
    ({ e } = await failing(t, 429, { error: 'rate_limited', message: '10 updates a second.' }));
    assert.ok(e instanceof RateLimitedError);
    assert.equal(e.retryAfter, 1);
    assert.equal(e.message, 'Too fast: 10 updates a second.');
    ({ e } = await failing(t, 429, { error: 'rate_limited' }));
    assert.equal(e.message, 'Too fast: slow down.');
    ({ e } = await failing(t, 429, { error: 'cooldown', message: 'x' }));
    assert.ok(e instanceof RateLimitedError);
    assert.match(e.message, /denied in the last 10 minutes/);
  });

  test('friendly messages for register errors', async (t) => {
    const cases = {
      already_pending: [409, ConflictError, /already waiting for you/],
      too_many_plugins: [409, ConflictError, /most plugins it takes \(32\)/],
      too_many_pending: [429, RateLimitedError, /Too many requests are waiting/],
    };
    for (const [code, [status, cls, re]] of Object.entries(cases)) {
      const { api, meter } = await setup(t);
      api.forced.set('POST /v1/register', [status, { error: code, message: 'server text' }]);
      const e = await meter.register().catch((x) => x);
      assert.ok(e instanceof cls, code);
      assert.equal(e.code, code);
      assert.match(e.message, re);
    }
  });

  test('unknown codes keep the server message; no body says the status', async (t) => {
    let { e } = await failing(t, 400, { error: 'unknown_signal', message: 'x' }, (m) => m.setSignals(['zz'], 30));
    assert.equal(e.constructor, BusyMeterError);
    assert.equal(e.status, 400);
    assert.equal(e.code, 'unknown_signal');
    assert.match(e.message, /wasn't declared/);
    ({ e } = await failing(t, 400, { error: 'bad_ttl', message: 'ttlSeconds must be 1-600.' }));
    assert.equal(e.message, 'ttlSeconds must be 1-600.');
    ({ e } = await failing(t, 503, {}));
    assert.equal(e.code, 'http_503');
    assert.equal(e.message, 'Busy Meter answered 503.');
    assert.equal(e.retryAfter, null);
  });

  test('every error shares the base class and has a name', () => {
    for (const cls of [NotListeningError, AppNotRespondingError, UnauthorizedError, ForbiddenError, ConflictError, RateLimitedError, DeniedError, CancelledError, ApprovalTimeoutError]) {
      const e = new cls('m', 'c', 1, 2);
      assert.ok(e instanceof BusyMeterError);
      assert.ok(e instanceof Error);
      assert.equal(e.name, cls.name);
      assert.deepEqual([e.message, e.code, e.status, e.retryAfter], ['m', 'c', 1, 2]);
    }
    const e = new BusyMeterError('m');
    assert.deepEqual([e.code, e.status, e.retryAfter], ['error', null, null]);
  });
});

describe('transport', () => {
  test('connection refused means turn on External connections', async (t) => {
    const { api, meter } = await setup(t);
    meter._token = 'tok';
    await api.stop();
    api.stop = async () => {};
    const e = await meter.setLevel(3, 30).catch((x) => x);
    assert.ok(e instanceof NotListeningError);
    assert.equal(e.code, 'not_listening');
    assert.match(e.message, /Turn on Settings \u2192 Sources \u2192 External connections/);
    assert.equal(await meter.isListening(), false);
  });

  test('a silent app is not responding, not off', async (t) => {
    const { api, meter } = await setup(t, { meterOptions: { timeoutMs: 200 } });
    meter._token = 'tok';
    api.raw = () => {}; // never answers
    const e = await meter.status().catch((x) => x);
    assert.ok(e instanceof AppNotRespondingError, String(e));
    assert.match(e.message, /didn't answer in time/);
    api.raw = () => {};
    assert.equal(await meter.isListening(), false);
  });

  test('garbage or a dropped connection is not responding', async (t) => {
    const { api, meter } = await setup(t);
    meter._token = 'tok';
    api.raw = (req) => req.socket.end('this is not http\r\n\r\n');
    let e = await meter.status().catch((x) => x);
    assert.ok(e instanceof AppNotRespondingError, String(e));
    assert.match(e.message, /connection to Busy Meter broke/);
    api.raw = (req) => req.socket.destroy();
    e = await meter.status().catch((x) => x);
    assert.ok(e instanceof AppNotRespondingError, String(e));
    api.raw = (req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': '100' });
      res.write('{"lev');
      setTimeout(() => req.socket.destroy(), 20);
    };
    e = await meter.status().catch((x) => x);
    assert.ok(e instanceof AppNotRespondingError, String(e));
  });

  test('a body that is not a JSON object reads as {}', async (t) => {
    const { api, meter } = await setup(t);
    meter._token = 'tok';
    api.raw = (req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('[1,2]');
    };
    assert.deepEqual(await meter.status(), {});
    api.raw = (req, res) => {
      res.writeHead(500);
      res.end('<html>');
    };
    const e = await meter.status().catch((x) => x);
    assert.equal(e.code, 'http_500');
  });

  test('at most 64 KB of a reply is read', async (t) => {
    const { api, meter } = await setup(t);
    meter._token = 'tok';
    api.raw = (req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"level":4,"pad":"' + 'x'.repeat(200 * 1024) + '"}');
    };
    assert.deepEqual(await meter.status(), {}); // cut at 64 KB, so not valid JSON
    const small = '{"level":2,"pad":"' + 'x'.repeat(60 * 1024) + '"}';
    api.raw = (req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(small);
    };
    assert.equal((await meter.status()).level, 2);
  });

  test('a plain TCP server that accepts and never answers is not responding', async (t) => {
    const server = net.createServer(() => {});
    await new Promise((r) => server.listen(0, '127.0.0.1', r));
    t.after(() => server.close());
    const dir = tempDir();
    const meter = new BusyMeter('com.example.test', { store: new TokenStore(path.join(dir, 't')), discoveryFile: writeDiscovery(dir, server.address().port), timeoutMs: 300, token: 'tok' });
    const started = Date.now();
    const e = await meter.status().catch((x) => x);
    assert.ok(e instanceof AppNotRespondingError, String(e));
    assert.ok(Date.now() - started < 3000);
  });
});

describe('registration', () => {
  test('register, wait, and the token is saved', async (t) => {
    const { api, meter, store } = await setup(t);
    const id = await meter.register({ name: 'Test', signals: [{ name: 'hr-high', label: 'HR', suggest: 5 }], wants: ['signals'], version: '1.0' });
    assert.equal(meter.pendingRequest(), id);
    assert.deepEqual(bodyOf(api.requests[0]), { id: 'com.example.test', signals: [{ name: 'hr-high', label: 'HR', suggest: 5 }], wants: ['signals'], name: 'Test', version: '1.0' });
    assert.deepEqual(await meter.poll(id), { state: 'pending' });
    api.answer = true;
    const token = await meter.waitForApproval(id, fast);
    assert.equal(meter.token, token);
    const saved = store.load('com.example.test');
    assert.equal(saved.token, token);
    assert.deepEqual(saved.signals, [{ name: 'hr-high', label: 'HR', suggest: 5 }]);
    assert.deepEqual(saved.wants, ['signals']);
    assert.equal(saved.pendingRequestId, undefined);
    assert.equal(saved.requestedSignals, undefined);
    assert.equal(typeof saved.savedAt, 'number');
  });

  test('register defaults: wants signals + status, no name or version', async (t) => {
    const { api, meter } = await setup(t);
    await meter.register();
    assert.deepEqual(bodyOf(api.requests[0]), { id: 'com.example.test', signals: [], wants: ['signals', 'status'] });
  });

  test('a reply without a request id is an error', async (t) => {
    const { api, meter } = await setup(t);
    api.forced.set('POST /v1/register', [202, { state: 'pending' }]);
    const e = await meter.register().catch((x) => x);
    assert.equal(e.code, 'bad_reply');
  });

  test('connect registers once and tells the user to click Allow', async (t) => {
    const { api, meter } = await setup(t);
    let told = 0;
    setTimeout(() => (api.answer = true), 60);
    const token = await meter.connect({ name: 'X', onWait: () => told++, intervalMs: 10 });
    assert.equal(typeof token, 'string');
    assert.equal(told, 1);
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 1);
    // The next connect uses the saved token (one status check, no registration).
    const again = new BusyMeter('com.example.test', { store: meter.store, discoveryFile: meter._discoveryFile });
    assert.equal(await again.connect(), token);
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 1);
  });

  test('a saved pending request is resumed, not repeated', async (t) => {
    const { api, meter, store } = await setup(t);
    const id = await meter.register();
    const second = new BusyMeter('com.example.test', { store, discoveryFile: meter._discoveryFile });
    api.answer = true;
    await second.connect();
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 1);
    assert.ok(api.requests.some((r) => r.path === '/v1/register/' + id));
  });

  test('an expired or forgotten saved request registers again', async (t) => {
    const { api, meter, store } = await setup(t);
    store.savePending('com.example.test', 'gone-request', [], ['signals']);
    api.answer = true;
    await meter.connect({ timeoutMs: 2000 });
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 1);
    assert.ok(store.load('com.example.test').token);
  });

  test('waiting times out with a clear message', async (t) => {
    const { meter } = await setup(t);
    const id = await meter.register();
    const e = await meter.waitForApproval(id, { timeoutMs: 50, intervalMs: 10 }).catch((x) => x);
    assert.ok(e instanceof ApprovalTimeoutError);
    assert.equal(e.code, 'pending');
    assert.match(e.message, /Still waiting for you to click Allow/);
    assert.equal(meter.pendingRequest(), id);
  });

  test('deny and expiry', async (t) => {
    const { api, meter, store } = await setup(t);
    await meter.register();
    api.answer = false;
    let e = await meter.waitForApproval(fast).catch((x) => x);
    assert.ok(e instanceof DeniedError);
    assert.equal(e.code, 'denied');
    assert.equal(e.message, 'The user clicked Deny in the Busy Meter tray.');
    assert.equal(store.load('com.example.test'), null);

    api.answer = null;
    api.expire = true;
    await meter.register();
    e = await meter.waitForApproval(fast).catch((x) => x);
    assert.ok(e instanceof DeniedError);
    assert.equal(e.code, 'expired');
    assert.match(e.message, /within 2 minutes/);
  });

  test('a denied saved request is not asked again by connect', async (t) => {
    const { api, meter } = await setup(t);
    await meter.register();
    api.answer = false;
    await assert.rejects(meter.connect(fast), DeniedError);
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 1);
  });

  test('already pending is 409', async (t) => {
    const { meter } = await setup(t);
    await meter.register();
    const e = await meter.register().catch((x) => x);
    assert.ok(e instanceof ConflictError);
    assert.equal(e.code, 'already_pending');
  });

  test('nothing to wait for', async (t) => {
    const { meter } = await setup(t);
    const e = await meter.waitForApproval().catch((x) => x);
    assert.equal(e.code, 'no_request');
  });

  test('a 404 while waiting clears the saved request', async (t) => {
    const { meter, store } = await setup(t);
    store.savePending('com.example.test', 'unknown', [], []);
    const e = await meter.waitForApproval(fast).catch((x) => x);
    assert.equal(e.code, 'not_found');
    assert.equal(e.status, 404);
    assert.match(e.message, /Register again/);
    assert.equal(meter.pendingRequest(), null);
  });
});

describe('abort', () => {
  test('aborting stops the wait and keeps the request', async (t) => {
    const { meter } = await setup(t);
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 50);
    const started = Date.now();
    const e = await meter.connect({ signal: controller.signal }).catch((x) => x);
    assert.ok(e instanceof CancelledError);
    assert.equal(e.code, 'cancelled');
    assert.ok(Date.now() - started < 900, 'stopped within the 1 s poll interval');
    assert.ok(meter.pendingRequest());
  });

  test('an aborted signal makes no new request', async (t) => {
    const { api, meter } = await setup(t);
    const controller = new AbortController();
    controller.abort();
    const e = await meter.connect({ signal: controller.signal }).catch((x) => x);
    assert.ok(e instanceof CancelledError);
    assert.equal(e.message, 'Stopped before asking for Allow.');
    assert.equal(api.requests.length, 0);
  });

  test('an aborted signal still polls a saved request once', async (t) => {
    const { api, meter } = await setup(t);
    await meter.register();
    const controller = new AbortController();
    controller.abort();
    const e = await meter.connect({ signal: controller.signal }).catch((x) => x);
    assert.ok(e instanceof CancelledError);
    assert.equal(e.message, 'Stopped waiting for Allow.');
    assert.equal(api.requests.filter((r) => r.method === 'GET').length, 1);
  });

  test('an abort cuts off a token check or poll the app is not answering', async (t) => {
    const { api, meter, store } = await setup(t);
    store.saveToken('com.example.test', 'tok');
    api.raw = () => {}; // the app accepts and says nothing
    let controller = new AbortController();
    setTimeout(() => controller.abort(), 50);
    let started = Date.now();
    let e = await meter.connect({ signal: controller.signal }).catch((x) => x);
    assert.ok(e instanceof CancelledError, String(e));
    assert.ok(Date.now() - started < 2000, 'not the 10 s read timeout');
    assert.equal(store.load('com.example.test').token, 'tok', 'an abort is not a 401');

    store.delete('com.example.test');
    store.savePending('com.example.test', 'req', [], []);
    api.raw = () => {};
    controller = new AbortController();
    setTimeout(() => controller.abort(), 50);
    started = Date.now();
    e = await meter.connect({ signal: controller.signal }).catch((x) => x);
    assert.ok(e instanceof CancelledError, String(e));
    assert.ok(Date.now() - started < 2000);
    assert.equal(meter.pendingRequest(), 'req');
  });

  test('a registration already sent is not cut off by an abort', async (t) => {
    const { api, meter } = await setup(t);
    const controller = new AbortController();
    const real = api.route.bind(api);
    api.route = (req, res, raw) => {
      if (req.method === 'POST') {
        controller.abort(); // Ctrl+C while the app handles the registration
        return setTimeout(() => real(req, res, raw), 50);
      }
      return real(req, res, raw);
    };
    const e = await meter.connect({ signal: controller.signal }).catch((x) => x);
    assert.ok(e instanceof CancelledError);
    assert.ok(meter.pendingRequest(), 'the request id was saved');
  });

  test('an approved answer wins over a later abort', async (t) => {
    const { api, meter } = await setup(t);
    await meter.register();
    api.answer = true;
    const controller = new AbortController();
    controller.abort();
    assert.equal(typeof (await meter.waitForApproval({ signal: controller.signal })), 'string');
  });
});

describe('revoked tokens', () => {
  test('a 401 deletes the saved token but keeps a waiting request', async (t) => {
    const { meter, store } = await setup(t);
    store.saveToken('com.example.test', 'old');
    const id = await meter.register();
    await assert.rejects(meter.status(), UnauthorizedError);
    const saved = store.load('com.example.test');
    assert.equal(saved.token, undefined);
    assert.equal(saved.pendingRequestId, id);
  });

  test('connect registers again when the saved token was revoked', async (t) => {
    const { api, meter, store } = await setup(t);
    store.saveToken('com.example.test', 'revoked');
    api.answer = true;
    const token = await meter.connect(fast);
    assert.notEqual(token, 'revoked');
    assert.equal(store.load('com.example.test').token, token);
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 1);
  });

  test('connect keeps a token the app still knows, also a switched-off one', async (t) => {
    const { api, meter, store } = await setup(t);
    const token = api.approveToken('com.example.test');
    store.saveToken('com.example.test', token);
    assert.equal(await meter.connect(), token);
    api.tokens.get(token).enabled = false;
    assert.equal(await new BusyMeter('com.example.test', { store, discoveryFile: meter._discoveryFile }).connect(), token);
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 0);
  });

  test('reconnecting registers again and retries once', async (t) => {
    const { api, meter } = await setup(t);
    api.answer = true;
    const first = await meter.connect({ name: 'R', ...fast });
    api.tokens.delete(first); // the user revoked it
    const reply = await meter.reconnecting(() => meter.setLevel(2, 30));
    assert.deepEqual(reply.active, ['level-2']);
    assert.notEqual(meter.token, first);
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 2);
    assert.equal(bodyOf(api.requests.filter((r) => r.method === 'POST')[1]).name, 'R');
  });

  test('reconnecting without connect just rethrows', async (t) => {
    const { meter } = await setup(t);
    meter._token = 'unknown';
    await assert.rejects(meter.reconnecting(() => meter.status()), UnauthorizedError);
  });

  test('remember sets what reconnecting registers with', async (t) => {
    const { api, meter } = await setup(t);
    meter._token = 'unknown';
    meter.remember({ name: 'Remembered', signals: ['s1'], wants: ['signals'], intervalMs: 10 });
    api.answer = true;
    await meter.reconnecting(() => meter.clear());
    const post = api.requests.find((r) => r.method === 'POST');
    assert.deepEqual(bodyOf(post), { id: 'com.example.test', signals: [{ name: 's1' }], wants: ['signals'], name: 'Remembered' });
  });

  test('forget and forgetToken', async (t) => {
    const { meter, store } = await setup(t);
    store.savePending('com.example.test', 'req', [], []);
    store.saveToken('com.example.test', 'tok');
    store.savePending('com.example.test', 'req2', [], []);
    meter.forgetToken();
    assert.equal(meter.token, null);
    assert.equal(store.load('com.example.test').pendingRequestId, 'req2');
    meter.forget();
    assert.equal(store.load('com.example.test'), null);
  });
});

describe('withdraw', () => {
  test('a pending request is withdrawn and the saved id cleared', async (t) => {
    const { api, meter, store } = await setup(t);
    const id = await meter.register();
    assert.deepEqual(await meter.withdraw(), { state: 'withdrawn' });
    const del = api.requests.at(-1);
    assert.deepEqual([del.method, del.path, del.body.length], ['DELETE', '/v1/register/' + id, 0]);
    assert.equal(del.headers.authorization, undefined);
    assert.equal(store.load('com.example.test'), null);
    assert.equal(api.registrations.size, 0);
  });

  test('withdrawing keeps a saved token', async (t) => {
    const { meter, store } = await setup(t);
    store.saveToken('com.example.test', 'tok');
    const id = await meter.register();
    assert.deepEqual(await meter.withdraw(id), { state: 'withdrawn' });
    const saved = store.load('com.example.test');
    assert.equal(saved.token, 'tok');
    assert.equal(saved.pendingRequestId, undefined);
  });

  test('already approved: the token is collected and saved', async (t) => {
    const { api, meter, store } = await setup(t);
    await meter.register({ signals: ['s'] });
    api.answer = true;
    const result = await meter.withdraw();
    assert.deepEqual(Object.keys(result), ['state', 'pluginId', 'token'], 'the raw poll reply, like Python');
    assert.equal(result.state, 'approved');
    assert.equal(result.pluginId, 'com.example.test');
    assert.equal(typeof result.token, 'string');
    assert.equal(meter.token, result.token);
    const saved = store.load('com.example.test');
    assert.equal(saved.token, result.token);
    assert.deepEqual(saved.signals, [{ name: 's' }]);
    assert.equal(saved.pendingRequestId, undefined);
    assert.deepEqual(api.requests.slice(-2).map((r) => r.method), ['DELETE', 'GET']);
  });

  test('already denied: the saved request is cleared', async (t) => {
    const { api, meter, store } = await setup(t);
    await meter.register();
    api.answer = false;
    assert.deepEqual(await meter.withdraw(), { state: 'denied', reason: 'denied' });
    assert.equal(store.load('com.example.test'), null);
    api.answer = null;
    api.expire = true;
    await meter.register();
    assert.deepEqual(await meter.withdraw(), { state: 'denied', reason: 'expired' });
  });

  test('already answered but collected: not_found, cleared', async (t) => {
    const { api, meter, store } = await setup(t);
    const id = await meter.register();
    api.forced.set('DELETE /v1/register/' + id, [409, { error: 'already_answered', state: 'approved', message: 'x' }]);
    api.registrations.clear();
    assert.deepEqual(await meter.withdraw(), { state: 'not_found' });
    assert.equal(store.load('com.example.test'), null);
  });

  test('unknown request: not_found, and the saved id is cleared', async (t) => {
    const { meter, store } = await setup(t);
    store.savePending('com.example.test', 'unknown', [], []);
    assert.deepEqual(await meter.withdraw(), { state: 'not_found' });
    assert.equal(store.load('com.example.test'), null);
  });

  test('a different request id leaves the saved one alone', async (t) => {
    const { meter } = await setup(t);
    const id = await meter.register();
    assert.deepEqual(await meter.withdraw('someone-else'), { state: 'not_found' });
    assert.equal(meter.pendingRequest(), id);
  });

  test('other answers are errors (cooldown, busy)', async (t) => {
    const { api, meter } = await setup(t);
    const id = await meter.register();
    api.forced.set('DELETE /v1/register/' + id, [429, { error: 'rate_limited', message: 'x' }]);
    const e = await meter.withdraw().catch((x) => x);
    assert.ok(e instanceof RateLimitedError);
    assert.equal(e.retryAfter, 1);
    assert.equal(meter.pendingRequest(), id);
    api.forced.set('DELETE /v1/register/' + id, [409, { error: 'something_else', message: 'Other.' }]);
    const c = await meter.withdraw().catch((x) => x);
    assert.ok(c instanceof ConflictError);
    assert.equal(c.message, 'Other.');
  });

  test('nothing to withdraw: state none, no request', async (t) => {
    const { api, meter } = await setup(t);
    assert.deepEqual(await meter.withdraw(), { state: 'none' });
    assert.equal(api.requests.length, 0);
  });

  test('withdraw_pause: friendly message; connect waits it out once and registers', async (t) => {
    const { api, meter } = await setup(t);
    api.forced.set('POST /v1/register', [429, { error: 'withdraw_pause', message: 'x' }]);
    const e = await meter.register().catch((x) => x);
    assert.ok(e instanceof RateLimitedError);
    assert.equal(e.message, 'This plugin just withdrew a request; Busy Meter takes a new one after 5 seconds.');
    assert.equal(e.retryAfter, 1); // the fake says Retry-After: 1 on every 429

    api.forced.set('POST /v1/register', [429, { error: 'withdraw_pause', message: 'x' }]);
    api.answer = true;
    const started = Date.now();
    assert.equal(typeof (await meter.connect(fast)), 'string');
    assert.ok(Date.now() - started >= 900, 'waited the Retry-After');
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 3);
  });

  test('withdraw_pause twice is an error; an abort during the pause cancels', async (t) => {
    const { api, meter } = await setup(t);
    api.forced.set('POST /v1/register', [429, { error: 'withdraw_pause', message: 'x' }]);
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 30);
    const c = await meter.connect({ signal: controller.signal }).catch((x) => x);
    assert.ok(c instanceof CancelledError);
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 1);

    let calls = 0;
    const real = api.route.bind(api);
    api.route = (req, res, raw) => {
      if (req.method === 'POST' && ++calls <= 2) return api.send(res, 429, { error: 'withdraw_pause', message: 'x' });
      return real(req, res, raw);
    };
    const e = await meter.connect(fast).catch((x) => x);
    assert.ok(e instanceof RateLimitedError);
    assert.equal(e.code, 'withdraw_pause');
  });

  test('after an abort, withdraw closes the prompt', async (t) => {
    const { api, meter } = await setup(t);
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 30);
    await assert.rejects(meter.connect({ signal: controller.signal }), CancelledError);
    assert.deepEqual(await meter.withdraw(), { state: 'withdrawn' });
    await eventually(() => api.registrations.size === 0, 'withdrawn');
  });
});
