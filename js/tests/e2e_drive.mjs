// Drives a real Busy Meter plugin host with the JavaScript client and the `busymeter` command, once.
//
// Run by the Busy Meter app's own end-to-end tests, which start the real loopback host with a test
// approver that clicks Allow, and points this script at it with BUSYMETER_DISCOVERY_FILE and BUSYMETER_TOKEN_DIR.
// Prints one JSON object with what each call returned. `off` mode checks the messages when nothing listens.

import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { BusyMeter, BusyMeterError } from '../index.js';

const ID = 'com.example.e2e-js';
const CLI_ID = 'com.example.e2e-js-cli';
const BIN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'bin', 'busymeter.js');

function cli(...args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [BIN, '--id', CLI_ID, ...args], { windowsHide: true });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += d));
    child.stderr.on('data', (d) => (stderr += d));
    child.on('error', reject);
    child.on('exit', (code) => resolve({ code, stdout: stdout.trim(), stderr: stderr.trim() }));
  });
}

async function on() {
  const results = {};
  const meter = new BusyMeter(ID);
  results.endpoint = { port: meter.endpoint.port, fromFile: meter.endpoint.fromFile };
  results.listening = await meter.isListening();
  const token = await meter.connect({
    name: 'End-to-end test (JavaScript)',
    signals: [{ name: 'deep-work', label: 'Deep work', suggest: 2 }],
    wants: ['signals', 'status', 'effects'],
    timeoutMs: 20000,
  });
  results.tokenLength = token.length;
  results.savedToken = meter.store.load(ID).token === token;
  results.verified = await meter.verifyToken();
  results.setLevel = await meter.setLevel(5, 60);
  results.status = await meter.status();
  results.setSignals = await meter.setSignals(['deep-work'], 30);
  try {
    await meter.setSignals(['not-declared'], 30);
  } catch (e) {
    if (!(e instanceof BusyMeterError)) throw e;
    results.unknownSignal = { status: e.status, code: e.code, message: e.message };
  }
  results.alert = await meter.alert('lip-pulse', { color: '#F0C850', durationMs: 200 });
  results.clear = await meter.clear();

  // The command: registers as its own plugin (auto-allowed), then reuses the saved token.
  results.cliLevel = await cli('level', '3', '--for', '30s');
  results.cliStatus = await cli('status');
  results.cliClear = await cli('clear');
  return results;
}

/** Register while nobody answers the prompt, then take it back. */
async function withdraw() {
  const meter = new BusyMeter('com.example.e2e-js-withdraw');
  const results = { requestId: await meter.register({ name: 'Withdraw test (JavaScript)' }) };
  results.pendingBefore = meter.pendingRequest();
  results.withdraw = await meter.withdraw();
  results.pendingAfter = meter.pendingRequest();
  try {
    await meter.poll(results.requestId);
  } catch (e) {
    if (!(e instanceof BusyMeterError)) throw e;
    results.pollAfter = { status: e.status, code: e.code };
  }
  results.again = await meter.withdraw(); // nothing waiting now
  return results;
}

async function off() {
  const meter = new BusyMeter(ID, { timeoutMs: 3000 });
  const results = { listening: await meter.isListening() };
  try {
    await meter.setLevel(3, 30);
  } catch (e) {
    if (!(e instanceof BusyMeterError)) throw e;
    results.setLevel = { code: e.code, message: e.message, name: e.name };
  }
  return results;
}

if (!new BusyMeter(ID).endpoint.fromFile) {
  // never fall back to the default port of a real app
  process.stderr.write('BUSYMETER_DISCOVERY_FILE must name the test host\'s discovery file\n');
  process.exit(2);
}
const modes = { on, off, withdraw };
const mode = process.argv[2] || 'on';
if (!modes[mode]) {
  process.stderr.write(`unknown mode ${mode}\n`);
  process.exit(2);
}
const results = await modes[mode]();
process.stdout.write(JSON.stringify(results) + '\n');
