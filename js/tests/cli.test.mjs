import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, test } from 'node:test';

import { BusyMeter, BusyMeterError, TokenStore, UnauthorizedError } from '../index.js';
import { DEFAULT_ID, holdLevel, main } from '../lib/cli.js';
import { setup, tempDir, writeDiscovery } from './helpers.mjs';
import { FakeApi } from './fake_api.mjs';

const BIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'bin', 'busymeter.js');

class Recorder {
  constructor() {
    this.calls = [];
  }
  async setLevel(level, ttl) {
    this.calls.push(['set', level, ttl]);
    return {};
  }
  async clear() {
    this.calls.push(['clear']);
    return {};
  }
}

function sink() {
  const out = { text: '', write: (s) => (out.text += s) };
  return out;
}

describe('holdLevel', () => {
  test('up to 10 minutes is one call', async () => {
    const m = new Recorder();
    await holdLevel(m, 5, 600);
    assert.deepEqual(m.calls, [['set', 5, 600]]);
  });

  test('longer refreshes every 30 s with a 60 s TTL, then clears', async () => {
    const m = new Recorder();
    let now = 0;
    const slept = [];
    await holdLevel(m, 4, 700, {
      clock: () => now,
      sleep: async (ms) => {
        slept.push(ms);
        now += ms / 1000;
        return false;
      },
    });
    const sets = m.calls.filter((c) => c[0] === 'set');
    assert.equal(sets.length, Math.ceil(700 / 30));
    assert.ok(sets.slice(0, -2).every((c) => c[2] === 60));
    assert.deepEqual(sets.slice(-2).map((c) => c[2]), [40, 10]); // never past the end
    assert.equal(sets.reduce((a, c) => a + (c[2] === 60 ? 30 : 0), 0) + 40, 700);
    assert.deepEqual(m.calls.at(-1), ['clear']);
    assert.ok(slept.every((ms) => ms <= 30000));
  });

  test('an abort (Ctrl+C) stops refreshing and clears', async () => {
    const m = new Recorder();
    const controller = new AbortController();
    let n = 0;
    await holdLevel(m, 'flash', 7200, {
      signal: controller.signal,
      sleep: async () => {
        if (++n === 2) controller.abort();
        return controller.signal.aborted;
      },
    });
    assert.deepEqual(m.calls, [['set', 'flash', 60], ['set', 'flash', 60], ['clear']]);
  });

  test('a clear that fails is ignored; a revoked token reconnects', async (t) => {
    const { api, meter } = await setup(t);
    api.answer = true;
    await meter.connect({ intervalMs: 10 });
    api.tokens.clear(); // revoked
    await holdLevel(meter, 3, 30);
    assert.equal(api.requests.filter((r) => r.method === 'POST').length, 2);
    const m = new Recorder();
    m.clear = async () => {
      throw new UnauthorizedError('x', 'unauthorized', 401);
    };
    let now = 0;
    await holdLevel(m, 1, 601, { clock: () => now, sleep: async (ms) => ((now += ms / 1000), false) });
    assert.ok(BusyMeterError);
  });
});

describe('busymeter command', () => {
  async function run(t, argv, { answerAfterMs = 0 } = {}) {
    const { api, store, discoveryFile } = await setup(t);
    if (answerAfterMs) setTimeout(() => (api.answer = true), answerAfterMs);
    else api.answer = true;
    const stdout = sink();
    const stderr = sink();
    const code = await main(argv, { stdout, stderr, makeMeter: (id) => new BusyMeter(id, { store, discoveryFile }) });
    return { code, stdout: stdout.text, stderr: stderr.text, api, store };
  }

  test('level 5 --for 10m', async (t) => {
    const r = await run(t, ['level', '5', '--for', '10m'], { answerAfterMs: 50 }); // one 1 s poll wait
    assert.equal(r.code, 0, r.stderr);
    assert.equal(r.stdout, 'Level 5 for 600 s.\n');
    assert.match(r.stderr, /Waiting for you to click Allow in the Busy Meter tray\.\.\./);
    const reg = JSON.parse(r.api.requests.find((x) => x.method === 'POST').body);
    assert.deepEqual(reg, { id: DEFAULT_ID, signals: [], wants: ['signals', 'status'], name: 'Busy Meter command line' });
    const put = r.api.requests.find((x) => x.method === 'PUT');
    assert.deepEqual(JSON.parse(put.body), { level: 5, ttlSeconds: 600 });
  });

  test('defaults to 10 minutes; flash; --id', async (t) => {
    let r = await run(t, ['level', 'flash']);
    assert.equal(r.stdout, 'Level flash for 600 s.\n');
    r = await run(t, ['--id', 'com.example.mine', 'level', '2', '--for', '30s']);
    assert.equal(r.stdout, 'Level 2 for 30 s.\n');
    assert.equal(JSON.parse(r.api.requests[0].body).id, 'com.example.mine');
    assert.ok(r.store.load('com.example.mine').token);
  });

  test('status and clear', async (t) => {
    let r = await run(t, ['status']);
    assert.equal(r.code, 0);
    assert.equal(r.stdout, 'level=4 paused=false source=teams\n');
    r = await run(t, ['clear']);
    assert.equal(r.stdout, 'Cleared.\n');
    assert.ok(r.api.requests.some((x) => x.method === 'DELETE' && x.path === '/v1/signals'));
  });

  test('bad arguments exit 2 without a request', async (t) => {
    for (const argv of [[], ['dance'], ['level'], ['level', '7'], ['level', '3', '--for', 'soon'], ['level', '3', 'extra'], ['clear', '--for', '1m'], ['--id', 'BAD', 'status'], ['--nope']]) {
      const r = await run(t, argv);
      assert.equal(r.code, 2, JSON.stringify(argv));
      assert.match(r.stderr, /usage: busymeter/);
      assert.equal(r.api.requests.length, 0);
    }
  });

  test('--help', async (t) => {
    const r = await run(t, ['--help']);
    assert.equal(r.code, 0);
    assert.match(r.stdout, /level LEVEL \[--for DURATION\]/);
  });

  test('a Busy Meter error prints its message and exits 1', async () => {
    const dir = tempDir();
    const api = await new FakeApi().start();
    const discoveryFile = writeDiscovery(dir, api.port);
    await api.stop(); // nothing listens on that port now
    const stderr = sink();
    const code = await main(['status'], { stdout: sink(), stderr, makeMeter: (id) => new BusyMeter(id, { store: new TokenStore(path.join(dir, 't'), { restrict: false }), discoveryFile }) });
    assert.equal(code, 1);
    assert.match(stderr.text, /Turn on Settings → Sources → External connections/);
  });

  test('an abort while waiting for Allow exits 130', async (t) => {
    const { store, discoveryFile } = await setup(t);
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 50);
    const code = await main(['level', '3'], { stdout: sink(), stderr: sink(), signal: controller.signal, makeMeter: (id) => new BusyMeter(id, { store, discoveryFile }) });
    assert.equal(code, 130);
  });

  test('an abort that lands as connect returns sets no level (exit 130)', async () => {
    const controller = new AbortController();
    const calls = [];
    const stub = {
      connect: async () => {
        controller.abort(); // Ctrl+C right after Allow
        return 'tok';
      },
      setLevel: async (...a) => calls.push(['set', ...a]),
      clear: async () => calls.push(['clear']),
      status: async () => calls.push(['status']),
    };
    for (const argv of [['level', '4'], ['level', '4', '--for', '2h'], ['clear'], ['status']]) {
      const code = await main(argv, { stdout: sink(), stderr: sink(), signal: controller.signal, makeMeter: () => stub });
      assert.equal(code, 130, JSON.stringify(argv));
    }
    assert.deepEqual(calls, []);
  });

  test('the bin runs and needs no dependencies', async (t) => {
    const dir = tempDir();
    const api = await new FakeApi().start();
    t.after(() => api.stop());
    api.answer = true;
    const env = { ...process.env, BUSYMETER_DISCOVERY_FILE: writeDiscovery(dir, api.port), BUSYMETER_TOKEN_DIR: path.join(dir, 'tokens') };
    const child = spawn(process.execPath, [BIN, 'level', '6', '--for', '45s'], { env, windowsHide: true });
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    const code = await new Promise((resolve) => child.on('exit', resolve));
    assert.equal(code, 0, err);
    assert.equal(out, 'Level 6 for 45 s.\n');
    assert.deepEqual(JSON.parse(api.requests.find((x) => x.method === 'PUT').body), { level: 6, ttlSeconds: 45 });
  });
});
