# busymeter (Python)

Client and MCP server for the Busy Meter status light's local plugin API. MIT licensed.

- `busymeter`: the client and the bundled recipes (`busymeter.recipes`). Standard library only (no third-party
  packages). Python 3.10+.
- `busymeter_mcp`: `busymeter-mcp`, an MCP server over stdio so an AI assistant can read and set the meter. Needs the
  official MCP SDK (`mcp==2.3.0`, the `mcp` extra).

Before anything works, turn on **Settings → Sources → External connections** in the Busy Meter app. When it's off
(or the app isn't running) every call fails with `NotListeningError` and a message saying exactly that.

## Install

```
pip install --user busymeter            # client + `busymeter` command
pip install --user "busymeter[mcp]"     # also busymeter-mcp, for AI assistants
```

`--user` installs for your Windows account only; starting recipes at sign-in needs that. From a checkout of this
folder: `pip install --user ".[mcp]"`.

Or without installing: put this folder on `PYTHONPATH` and run `python -m busymeter.cli` / `python -m busymeter_mcp`.

## Client

```python
from busymeter import BusyMeter

meter = BusyMeter("com.example.heartrate")          # your plugin id: 3-64 of a-z 0-9 . -
meter.connect(                                       # first time: registers and waits for Allow in the tray
    name="Heart-rate heat",
    signals=[{"name": "hr-high", "label": "Heart rate 140+", "suggest": 5}],
    wants=["signals", "status"],
)
meter.set_signals(["hr-high"], ttl_seconds=30)      # refresh before the TTL runs out
meter.set_level(4, ttl_seconds=600)                 # or a level: 0-6 stars, "flash"
print(meter.status())                               # {"level": 4, "paused": false, "source": "plugin", ...}
meter.alert("lip-pulse")                            # needs "effects" in wants; one every 10 s
meter.clear()
```

- **Port:** read from `%LOCALAPPDATA%\BusyMeter\plugin-api.json` (written by the app only while it listens). Only the
  port is taken from the file; the host is always `127.0.0.1`. `BUSYMETER_DISCOVERY_FILE` overrides the file's path.
  Without the file the client still probes and registers on 47820, but never sends a token there: calls that need
  one fail with "Busy Meter isn't running, or External connections is off".
- **Never sends an `Origin` header** (the app refuses those), no query strings, JSON bodies only.
- **Errors:** every failure is a `BusyMeterError` with `.code` (the API's error code), `.status` and a `.message` you
  can show as is: `NotListeningError` (turn on External connections), `AppNotRespondingError` (connected, but no
  answer in time or the connection broke), `UnauthorizedError` (401: register again),
  `ForbiddenError` (403: switched off, or not in `wants`), `ConflictError` (409), `RateLimitedError` (429, with
  `.retry_after`), `DeniedError` (the user clicked Deny, or 2 minutes passed), `ApprovalTimeoutError`.
- **Withdraw:** `meter.withdraw()` takes back a registration still waiting in the tray (its prompt closes). If the
  user already answered, it collects the answer instead (a token is saved as usual). The same plugin id can't register
  again for 5 seconds afterwards. The recipe runner does this when a recipe stops.
- **Revoked tokens:** a 401 deletes the saved token. `connect()` checks a saved token before using it and registers
  again when the app no longer knows it; `meter.reconnecting(lambda: meter.set_level(4, 60))` does the same in the
  middle of a run (the user clicks Allow again) and retries once.
- **Longer than 10 minutes:** the API caps a TTL at 600 s so a crashed plugin can't leave the meter stuck. Keep running
  and refresh (the `busymeter` command does this for `--for` over 10 minutes).

### Where the token is kept

One JSON file per plugin id, readable by the current user only:

- Windows: `%LOCALAPPDATA%\busymeter-plugins\tokens\<plugin-id>.json`. Before every write the folder (also one set
  with `BUSYMETER_TOKEN_DIR`) loses its inherited permissions and grants only the current user, by SID from
  `whoami /user` (`icacls`). If that fails, a `PermissionWarning` says so and the file is still written.
- macOS/Linux: `$XDG_CONFIG_HOME/busymeter-plugins/tokens/` (else `~/.config/...`); the folder is set to 0700 (also an
  existing looser one), files 0600.
- `BUSYMETER_TOKEN_DIR` overrides the folder.

The token is a bearer secret: whoever reads it can set this plugin's signals until the user revokes the plugin under
Settings → Sources → External connections. `meter.forget()` deletes the file. While a registration waits, the file
also holds its request id, so the next run keeps waiting instead of asking again.

## Command line

```
busymeter level 5 --for 10m
busymeter level flash --for 30s
busymeter level 4 --for 2h     # stays running and refreshes; Ctrl+C clears
busymeter clear
busymeter status
busymeter run vibe_meter       # a bundled recipe: stream_status, game_state, focus_timer, vibe_meter
busymeter focus 25m            # one focus block (--pomodoro for cycles)
busymeter recipes              # bundled recipes: installed? running? settings file
busymeter stop vibe_meter      # stop one running in the background (it clears its signals)
busymeter autostart vibe_meter on   # Windows: start it every time you sign in (off by default; `off` undoes it)
busymeter autostart            # which recipes start at sign-in
```

Recipes, their settings files and what each one reads: [recipes/README.md](https://github.com/RIPBillyMays/busy-meter-plugins/blob/main/recipes/README.md). `python -m busymeter ...` works the
same as the `busymeter` command.

## MCP server (`busymeter-mcp`)

Tools: `get_status`, `list_sources`, `register_plugin`, `set_signals`, `set_level` (0-6 or `"flash"`, with a TTL such as
`"10m"`; over 10 minutes the server keeps refreshing while it runs), `clear`. The assistant can **ask** to register;
only you can approve it, by clicking **Allow** in the Busy Meter tray. `register_plugin` waits up to 110 s and returns
"Waiting for you to click Allow in the Busy Meter tray" if you haven't yet; calling it again keeps waiting on the same
request. `list_sources` says what it can't know: the API doesn't list the app's sources or mappings (those are on
Settings → Levels). The server registers as `com.thebusymeter.mcp` (`BUSYMETER_PLUGIN_ID` changes it).

Recipe tools: `list_recipes`, `install_recipe(name)` (bundled names only: writes the default settings and starts the
recipe in the background for this user; it then waits for Allow in the tray), `stop_recipe`, `uninstall_recipe`
(stops it, clears its signals, deletes its settings and saved token; revoke the approval in the app),
`get_recipe_settings` and `set_recipe_settings(name, changes)` (checked against the recipe's schema; the OBS password,
the game-state token and folder paths are never shown or accepted: the reply names the settings file to edit),
`set_recipe_autostart(name, on)` (Windows: start that recipe every time you sign in, or stop doing that; off by
default, bundled names only, needs a `pip install --user` install; the first run still needs Allow in the tray, and
after a Deny it isn't started at sign-in again until turned on again). `list_recipes` shows `startsAtSignIn` for
each, and `uninstall_recipe` removes the sign-in entry.

Claude Desktop (`claude_desktop_config.json`) or any client with the same format:

```json
{
  "mcpServers": {
    "busymeter": {
      "command": "busymeter-mcp"
    }
  }
}
```

Running from a checkout instead of an install:

```json
{
  "mcpServers": {
    "busymeter": {
      "command": "python",
      "args": ["-m", "busymeter_mcp"],
      "env": { "PYTHONPATH": "C:\\path\\to\\plugins\\python" }
    }
  }
}
```

Claude Code:

```
claude mcp add busymeter -- busymeter-mcp
```

## Tests

From this folder: `python -m unittest`. The MCP server test is skipped when the `mcp` package isn't installed, and
fails instead when `BUSYMETER_REQUIRE_PYTHON=1`.
