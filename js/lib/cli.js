// `busymeter` command: set a level from the command line (same commands as the Python client's level/clear/status).
//
//   busymeter level 5 --for 10m     # 5 stars for 10 minutes, then back to whatever the app shows
//   busymeter level flash --for 30s
//   busymeter level 4 --for 2h      # longer than 10 minutes: stays running and refreshes; Ctrl+C clears
//   busymeter clear
//   busymeter status
//
// The first run asks you to click Allow in the Busy Meter tray (needs Settings > Sources > External connections on).

import { parseArgs } from 'node:util';
import { performance } from 'node:perf_hooks';

import { BusyMeter, BusyMeterError, CancelledError, MAX_TTL_SECONDS, abortableSleep, normalizeLevel, parseDuration } from './client.js';

export const DEFAULT_ID = 'com.thebusymeter.cli';
export const REFRESH_TTL = 60;

const USAGE = `usage: busymeter [--id ID] {level,clear,status} ...

Set the Busy Meter's level from the command line.

commands:
  level LEVEL [--for DURATION]   show a level for a while: 0-6 (stars; 0 = lip accent only) or flash;
                                 how long, e.g. 30s, 10m, 2h (default 10m)
  clear                          clear this command's level now
  status                         print what the meter shows

options:
  --id ID                        plugin id to register as (default ${DEFAULT_ID})
  -h, --help                     show this help
`;

/** Run fn; if the token was revoked, register again (the user clicks Allow in the tray) and retry once. */
export function call(meter, fn) {
  return typeof meter.reconnecting === 'function' ? meter.reconnecting(fn) : fn();
}

async function connect(meter, stderr, signal) {
  await meter.connect({
    name: 'Busy Meter command line',
    wants: ['signals', 'status'],
    onWait: () => stderr.write('Waiting for you to click Allow in the Busy Meter tray...\n'),
    signal,
  });
  // Ctrl+C while connecting (e.g. during the token check or right after Allow): do nothing more.
  if (signal && signal.aborted) throw new CancelledError('Stopped.', 'cancelled');
}

/**
 * Show `level` for `seconds`. Up to 10 minutes is one call; longer refreshes every 30 s, then clears (also when
 * `signal` aborts, e.g. Ctrl+C). `sleep(ms, signal)` resolves true when aborted; `clock()` is in seconds.
 */
export async function holdLevel(meter, level, seconds, { sleep = abortableSleep, clock = () => performance.now() / 1000, signal = null } = {}) {
  if (seconds <= MAX_TTL_SECONDS) {
    await call(meter, () => meter.setLevel(level, seconds));
    return;
  }
  const end = clock() + seconds;
  try {
    for (;;) {
      const left = end - clock();
      if (left <= 0) break;
      const ttl = Math.max(1, Math.min(REFRESH_TTL, Math.trunc(left + 0.999)));
      await call(meter, () => meter.setLevel(level, ttl));
      if ((await sleep(Math.min(REFRESH_TTL / 2, Math.max(left, 0)) * 1000, signal)) || (signal && signal.aborted)) break;
    }
  } finally {
    try {
      await meter.clear();
    } catch (e) {
      if (!(e instanceof BusyMeterError)) throw e; // eslint-disable-line no-unsafe-finally
    }
  }
}

function usageError(stderr, message) {
  stderr.write(USAGE.split('\n\n')[0] + '\n');
  stderr.write(`busymeter: error: ${message}\n`);
  return 2;
}

const py = (value) => (value === undefined || value === null ? 'None' : String(value));

/**
 * Runs the command. Resolves the exit code: 0 ok, 1 a Busy Meter error (message on stderr), 2 bad arguments,
 * 130 interrupted. `signal` aborts like Ctrl+C (the bin wires SIGINT to it).
 */
export async function main(argv = process.argv.slice(2), { stdout = process.stdout, stderr = process.stderr, signal = null, makeMeter = (id) => new BusyMeter(id) } = {}) {
  let parsed;
  try {
    parsed = parseArgs({
      args: argv,
      allowPositionals: true,
      strict: true,
      options: { id: { type: 'string' }, for: { type: 'string' }, help: { type: 'boolean', short: 'h' } },
    });
  } catch (e) {
    return usageError(stderr, e.message);
  }
  const { values, positionals } = parsed;
  if (values.help) {
    stdout.write(USAGE);
    return 0;
  }
  const [command, ...rest] = positionals;
  if (!['level', 'clear', 'status'].includes(command)) {
    return usageError(stderr, command ? `invalid choice: '${command}' (choose from 'level', 'clear', 'status')` : 'the following arguments are required: command');
  }
  if (command === 'level' ? rest.length !== 1 : rest.length !== 0) {
    return usageError(stderr, command === 'level' && rest.length === 0 ? 'the following arguments are required: level' : `unrecognized arguments: ${rest.slice(command === 'level' ? 1 : 0).join(' ')}`);
  }
  if (values.for !== undefined && command !== 'level') return usageError(stderr, 'unrecognized arguments: --for');

  let meter;
  try {
    meter = makeMeter(values.id ?? DEFAULT_ID);
  } catch (e) {
    if (e instanceof TypeError) return usageError(stderr, e.message);
    throw e;
  }
  try {
    if (command === 'level') {
      let value;
      let seconds;
      try {
        value = normalizeLevel(rest[0]);
        seconds = parseDuration(values.for ?? '10m');
      } catch (e) {
        if (e instanceof TypeError) return usageError(stderr, e.message);
        throw e;
      }
      await connect(meter, stderr, signal);
      if (seconds > MAX_TTL_SECONDS) stderr.write(`Holding ${value} for ${seconds} s; keep this running (Ctrl+C clears).\n`);
      await holdLevel(meter, value, seconds, { signal });
      if (signal && signal.aborted) return 130;
      if (seconds <= MAX_TTL_SECONDS) stdout.write(`Level ${value} for ${seconds} s.\n`);
    } else if (command === 'clear') {
      await connect(meter, stderr, signal);
      await call(meter, () => meter.clear());
      stdout.write('Cleared.\n');
    } else {
      await connect(meter, stderr, signal);
      const s = await call(meter, () => meter.status());
      stdout.write(`level=${py(s.level)} paused=${py(s.paused).toLowerCase()} source=${py(s.source)}\n`);
    }
  } catch (e) {
    if (e instanceof CancelledError || (signal && signal.aborted)) return 130;
    if (e instanceof BusyMeterError) {
      stderr.write(e.message + '\n');
      return 1;
    }
    throw e;
  }
  return 0;
}
