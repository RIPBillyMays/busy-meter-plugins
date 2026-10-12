# BusyMeter.Plugins (.NET)

Client for the Busy Meter status light's local plugin API. MIT licensed. .NET 8 or later, no third-party packages
(System.Text.Json only). A port of the [Python client](https://github.com/RIPBillyMays/busy-meter-plugins/tree/main/python) with the same rules, token files and
messages, plus `WithdrawAsync`.

Before anything works, turn on **Settings → Sources → External connections** in the Busy Meter app. When it's off
(or the app isn't running) every call fails with `NotListeningException` and a message saying exactly that.

## Install

`dotnet add package BusyMeter.Plugins` (.NET 8 or later).

## Client

```csharp
using BusyMeter.Plugins;

using var meter = new BusyMeterClient("com.example.heartrate");   // your plugin id: 3-64 of a-z 0-9 . -
await meter.ConnectAsync(                                           // first time: registers and waits for Allow in the tray
    name: "Heart-rate heat",
    signals: [new SignalDeclaration("hr-high", "Heart rate 140+", Suggest: 5)],
    wants: ["signals", "status"]);
await meter.SetSignalsAsync(["hr-high"], ttlSeconds: 30);          // refresh before the TTL runs out
await meter.SetLevelAsync(4, ttlSeconds: 600);                      // or a level: 0-6 stars, MeterLevel.Flash
Console.WriteLine(await meter.StatusAsync());                       // {"level":4,"paused":false,"source":"plugin",...}
await meter.AlertAsync("lip-pulse");                                // needs "effects" in wants; one every 10 s
await meter.ClearAsync();
```

- **Port:** read from `%LOCALAPPDATA%\BusyMeter\plugin-api.json` (written by the app only while it listens). Only the
  port is taken from the file; the host is always `127.0.0.1`. `BUSYMETER_DISCOVERY_FILE` overrides the file's path.
  Without the file the client still probes and registers on 47820, but never sends a token there: calls that need
  one fail with "Busy Meter isn't running, or External connections is off".
- **Never sends an `Origin` or `Cookie` header** (the app refuses `Origin`), no proxy, no query strings, JSON bodies
  only. Connects time out after 1 s, answers are read up to 64 KB.
- **Errors:** every failure is a `BusyMeterException` with `.Code` (the API's error code), `.Status` and a `.Message`
  you can show as is: `NotListeningException` (turn on External connections), `AppNotRespondingException` (connected,
  but no answer in time or the connection broke), `UnauthorizedException` (401: register again),
  `ForbiddenException` (403: switched off, or not in `wants`), `ConflictException` (409), `RateLimitedException` (429,
  with `.RetryAfter`), `DeniedException` (the user clicked Deny, or 2 minutes passed), `ApprovalTimeoutException`,
  `CancelledException` (your `CancellationToken` stopped the wait). Bad arguments (an id, level, TTL or signal name
  the API would refuse) throw `ArgumentException` before anything is sent.
- **Revoked tokens:** a 401 deletes the saved token. `ConnectAsync` checks a saved token before using it and registers
  again when the app no longer knows it; `meter.ReconnectingAsync(ct => meter.SetLevelAsync(4, 60, ct))` does the same
  in the middle of a run (the user clicks Allow again) and retries once.
- **Stopping a wait:** cancel the token passed to `ConnectAsync` / `WaitForApprovalAsync`. The request stays in the tray
  (and saved, so the next connect waits on it) until the user answers or it expires. `await meter.WithdrawAsync()`
  closes it instead: it returns `Withdrawn`, or, when the user had already answered, `Approved` (the token is saved)
  or `Denied`; `NotFound` when the app no longer knows it; `None` when nothing was waiting. A withdrawal isn't a Deny,
  but the same id can't register again for 5 s (`RateLimitedException` with code `withdraw_pause`; `ConnectAsync`
  waits that out once by itself) and a 3rd withdrawal within 10 minutes counts as one. Once the registration has been
  sent, cancelling doesn't drop it: its request id is still saved, so a withdraw can close it.
- **Longer than 10 minutes:** the API caps a TTL at 600 s so a crashed plugin can't leave the meter stuck. Keep running
  and refresh.
- `IsListeningAsync()` (External connections on?), `VerifyTokenAsync()`, `Validation.ParseDuration("10m")`,
  `MeterLevel.Parse("flash")`.

### Where the token is kept

The same files as the Python client, so a .NET and a Python program with the same plugin id share one approval:

- Windows: `%LOCALAPPDATA%\busymeter-plugins\tokens\<plugin-id>.json`. Before every write the folder (also one set
  with `BUSYMETER_TOKEN_DIR`) loses its inherited permissions and grants only the current user (by SID) full control.
  If that fails, `TokenStore.Warning` is called (default: standard error) and the file is still written.
- macOS/Linux: `$XDG_CONFIG_HOME/busymeter-plugins/tokens/` (else `~/.config/...`); the folder is set to 0700, files
  0600.
- `BUSYMETER_TOKEN_DIR` overrides the folder.

The token is a bearer secret: whoever reads it can set this plugin's signals until the user revokes the plugin under
Settings → Sources → External connections. `meter.Forget()` deletes the file. While a registration waits, the file
also holds its request id, so the next run keeps waiting instead of asking again.

## Tests

`dotnet test tests/BusyMeter.Plugins.Tests` (a fake API in the test project). The Busy Meter app's own tests also run
this client against the real host.
