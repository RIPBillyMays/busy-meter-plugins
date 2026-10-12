# busymeter (JavaScript, Node)

Client for the Busy Meter status light's local plugin API. MIT licensed. Node 18.3+, ES modules, **no
dependencies**, TypeScript declarations included (`index.d.ts`). Same features, messages and token files as the
Python client (pip `busymeter`).

Before anything works, turn on **Settings → Sources → External connections** in the Busy Meter app. When it's off
(or the app isn't running) every call fails with `NotListeningError` and a message saying exactly that.

## Install

`npm install busymeter` in your project, or `npm install -g busymeter` for the `busymeter` command.

## Client

```js
import { BusyMeter } from 'busymeter';

const meter = new BusyMeter('com.example.heartrate');   // your plugin id: 3-64 of a-z 0-9 . -
await meter.connect({                                    // first time: registers and waits for Allow in the tray
  name: 'Heart-rate heat',
  signals: [{ name: 'hr-high', label: 'Heart rate 140+', suggest: 5 }],
  wants: ['signals', 'status'],
});
await meter.setSignals(['hr-high'], 30);                 // TTL in seconds (1-600); refresh before it runs out
await meter.setLevel(4, 600);                            // or a level: 0-6 stars, 'flash'
console.log(await meter.status());                       // { level: 4, paused: false, source: 'plugin', ... }
await meter.alert('lip-pulse', { color: '#F0C850' });    // needs 'effects' in wants; one every 10 s
await meter.clear();
```

- **Port:** read from `%LOCALAPPDATA%\BusyMeter\plugin-api.json` (written by the app only while it listens). Only the
  port is taken from the file; the host is always `127.0.0.1`. `BUSYMETER_DISCOVERY_FILE` overrides the file's path,
  `new BusyMeter(id, { port })` names a port. Without the file the client still probes and registers on 47820, but
  never sends a token there: calls that need one fail with "Busy Meter isn't running, or External connections is off".
- **Requests** use `node:http` with the client's own Agent, never the global `fetch`: the app refuses any request with
  an `Origin` header, and only the headers the client sets are sent. No query strings, JSON bodies only, connecting
  gives up after 1 s, at most 64 KB of a reply is read, `timeoutMs` (default 10000) per request.
- **Errors:** every failure is a `BusyMeterError` with `.code` (the API's error code), `.status`, `.retryAfter` and a
  `.message` you can show as is: `NotListeningError` (turn on External connections), `AppNotRespondingError`
  (connected, but no answer in time or the connection broke), `UnauthorizedError` (401: register again),
  `ForbiddenError` (403: switched off, or not in `wants`), `ConflictError` (409), `RateLimitedError` (429, with
  `.retryAfter` in seconds), `DeniedError` (the user clicked Deny, code `denied`, or 2 minutes passed, code `expired`),
  `CancelledError` (your `AbortSignal` fired), `ApprovalTimeoutError`. Bad arguments (a level of 7, a TTL of 601, a bad
  plugin id) throw a `TypeError` before anything is sent.
- **Waiting for Allow:** `connect()` uses the saved token if the app still knows it, else keeps waiting on a saved
  request, else registers and waits (`timeoutMs`, default 125000; `onWait` is called once while it waits). Pass
  `signal` (an `AbortSignal`) to stop: it cuts off the token check, the polls and the waits with `CancelledError`
  (never a registration already sent, so its request id is always saved), and the next `connect()` waits on the
  saved request instead of asking again. Just after a withdrawal the app takes no new request from the same id for
  5 s (`429 withdraw_pause`); `connect()` waits that out once and retries.
- **Withdraw:** `await meter.withdraw()` takes back a waiting request, so its prompt in the tray closes
  (`DELETE /v1/register/{requestId}`, default: the saved request). It resolves `{ state: 'withdrawn' }`; when the user
  answered first, the poll's answer (`{ state: 'approved', pluginId, token }` with the token saved, or
  `{ state: 'denied', reason }`); `{ state: 'not_found' }` when the app no longer knows the request; `{ state: 'none' }`
  when nothing is waiting. A withdrawal isn't a Deny, but the same id can't register again for 5 s, and a 3rd
  withdrawal within 10 minutes counts as one.
- **Revoked tokens:** a 401 deletes the saved token. `meter.reconnecting(() => meter.setLevel(4, 60))` registers again
  with the last `connect()` options (the user clicks Allow again) and retries once. `register()`, `poll()`,
  `waitForApproval()`, `remember()`, `verifyToken()`, `isListening()`, `forgetToken()` and `forget()` work like the
  Python client's.
- **Longer than 10 minutes:** the API caps a TTL at 600 s so a crashed plugin can't leave the meter stuck. Keep running
  and refresh (the `busymeter` command does this for `--for` over 10 minutes).

### Where the token is kept

The same files as the Python client, so both can share a plugin id's token:

- Windows: `%LOCALAPPDATA%\busymeter-plugins\tokens\<plugin-id>.json`. Before every write the folder (also one set
  with `BUSYMETER_TOKEN_DIR`) loses its inherited permissions and grants only the current user, by SID from
  `whoami /user` (`icacls`; both run by their full System32 path). If that fails, a `PermissionWarning` process
  warning says so and the file is still written.
- macOS/Linux: `$XDG_CONFIG_HOME/busymeter-plugins/tokens/` (else `~/.config/...`); the folder is set to 0700 (also an
  existing looser one), files 0600.
- `BUSYMETER_TOKEN_DIR` overrides the folder. Files are replaced atomically (written to a temp file, then renamed).

The token is a bearer secret: whoever reads it can set this plugin's signals until the user revokes the plugin under
Settings → Sources → External connections. `meter.forget()` deletes the file.

## Command line

```
busymeter level 5 --for 10m
busymeter level flash --for 30s
busymeter level 4 --for 2h     # stays running and refreshes; Ctrl+C clears
busymeter clear
busymeter status
busymeter --id com.example.mine status
```

The first run asks you to click Allow in the Busy Meter tray. The recipes are in the Python package only.

## Tests

From this folder: `npm test` (`node:test`, against a small fake API, no dependencies). In the source repository a
token-file test also checks the files against the Python client next to this package (skipped without Python 3.10+
unless `BUSYMETER_REQUIRE_PYTHON=1`), and the Busy Meter app's own tests run `tests/e2e_drive.mjs` against the real
plugin host.

## Publishing (maintainers)

- `npm pack --dry-run` lists what is published (`index.js`, `index.d.ts`, `lib/`, `bin/`, `README.md`, `LICENSE`).
