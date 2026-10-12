import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { BusyMeter, TokenStore } from '../index.js';
import { FakeApi } from './fake_api.mjs';

// Never the real token folder or discovery file, whatever the environment says.
process.env.BUSYMETER_TOKEN_DIR = path.join(os.tmpdir(), 'bm-js-tests-never-used');
process.env.BUSYMETER_DISCOVERY_FILE = path.join(os.tmpdir(), 'bm-js-tests-no-such-discovery.json');

export function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'bm-js-'));
}

export function writeDiscovery(dir, port) {
  const file = path.join(dir, 'plugin-api.json');
  fs.writeFileSync(file, JSON.stringify({ port, baseUrl: `http://127.0.0.1:${port}/v1/`, apiVersion: 1 }));
  return file;
}

/** Programs the store would run, answered without running anything: whoami gives a SID, icacls succeeds. */
export function fakeRunner(sid = 'S-1-5-21-1-2-3-1001', icaclsStatus = 0) {
  const calls = [];
  const run = (file, args) => {
    calls.push([file, args]);
    if (file.toLowerCase().endsWith('whoami.exe')) {
      return sid ? { status: 0, stdout: `"desktop\\someone","${sid}"\r\n`, stderr: '' } : { status: 1, stdout: '', stderr: 'no' };
    }
    return { status: icaclsStatus, stdout: '', stderr: icaclsStatus ? 'Access is denied.' : '' };
  };
  return { run, calls };
}

/** A running fake API, a discovery file pointing at it, and a meter with its own token folder. */
export async function setup(t, { pluginId = 'com.example.test', meterOptions = {} } = {}) {
  const api = await new FakeApi().start();
  const dir = tempDir();
  const discoveryFile = writeDiscovery(dir, api.port);
  const warnings = [];
  const store = new TokenStore(path.join(dir, 'tokens'), { run: fakeRunner().run, warn: (m) => warnings.push(m) });
  const meter = new BusyMeter(pluginId, { store, discoveryFile, ...meterOptions });
  t.after(async () => {
    await api.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  return { api, dir, discoveryFile, store, meter, warnings };
}

/** Waits until `fn()` is truthy (polls every 10 ms, up to 5 s). */
export async function eventually(fn, what = 'condition') {
  const end = Date.now() + 5000;
  while (Date.now() < end) {
    if (await fn()) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error('timed out waiting for ' + what);
}
