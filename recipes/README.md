# Recipes

Small, ready-to-run plugins for the Busy Meter, built on the Python client in `../python/busymeter` (standard library
only). Each one runs from this folder without installing anything (they find `../python` themselves), or after
`pip install busymeter`.

Before the first run: turn on **Settings → Sources → External connections** in the Busy Meter app. The first run of
each recipe shows a prompt in the Busy Meter tray; click **Allow**. Each recipe registers under its own plugin id, so
you can switch it off or revoke it on its own, and remap its levels on Settings → Levels.

| Recipe | Run | What it does |
| --- | --- | --- |
| `stream_status.py` | `busymeter run stream_status` | OBS streaming → `live` (5 stars), recording only → `recording` (4 stars) |
| `game_state.py` | `busymeter run game_state` | A `127.0.0.1` endpoint for games with an official "game state" push feature; your rules map fields to signals |
| `focus_timer.py` | `busymeter focus 25m` or `busymeter run focus_timer` | Focus while a block runs (2 stars); Pomodoro cycles with a long break every 4th |
| `vibe_meter.py` | `busymeter run vibe_meter` | Active Claude Code sessions (or their token rate) as stars; Vibe alert (Flash) on a burst |
| `level_from_cli.py` | `python level_from_cli.py level 5 --for 10m` | Sets a level (0-6 stars or `flash`) for a while. Same as the `busymeter` command |
| `webhook_bridge.py` | `python webhook_bridge.py` | An HTTP endpoint on `127.0.0.1:47821` that maps `POST /level {"level": 5, "for": "10m"}` and `POST /clear` to the meter. Needs a shared secret (`Authorization: Bearer <secret>`, made on first run, `--show-secret` prints it). Loopback only; web pages (requests with an `Origin` header) are refused |
| `mic_loudness.py` | `python mic_loudness.py` | The louder the room, the more stars (`--levels=-45:2,-35:4,-25:6`). Measures loudness only: audio is never recorded, stored or sent. Needs the optional `sounddevice` package (`pip install sounddevice==0.5.1`); `--dry-run` shows the numbers without the meter |

Notes:

- Levels you set go through the user's level map and are combined with the app's own sources (calendar, calls,
  camera...), so a higher level from another source can still win.
- A level or signal lasts at most 10 minutes per call (the TTL). Scripts that hold a level longer keep running and
  refresh it; stopping them clears it within a minute at most.
- These are examples, not supported products. Read a script before you run it.
- Recipes (and AI agents writing new ones) go through the plugin API only: never send HTTP or serial commands or
  preset numbers to the meter's WLED while the Busy Meter app runs. The app keeps such a level as the user's manual
  level, overriding their sources.

## Bundled recipes (`busymeter run <recipe>`)

The first four recipes share one runner (`busymeter/recipes/runner.py`). It:

- reads the recipe's **settings file** (written with the defaults on the first run), checks it against the recipe's
  schema (types, ranges; unknown keys are errors that name the key), and **re-reads it when it changes** (a broken
  edit is reported and the previous settings stay);
- **registers** the recipe with its signals and **suggested levels**, so you can remap each signal on Settings →
  Levels; when a settings change alters the signals or suggested levels, it **asks again** in the tray (click
  Allow for the new ones to apply; until then the old ones keep working);
- sends the active signals with a 30 s TTL and refreshes them every 15 s, so a crashed recipe clears itself;
- **registers again after a Revoke** (you click Allow again), waits while External connections is off, and stops if
  you click Deny;
- **clears its signals on exit**: Ctrl+C, `busymeter stop <recipe>`, or the stop file.

Settings files are in the per-user recipes folder next to the plugin tokens, narrowed to your user the same way:
`%LOCALAPPDATA%\busymeter-plugins\recipes\<recipe>.json` on Windows (`$XDG_CONFIG_HOME/busymeter-plugins/recipes/`
elsewhere; `BUSYMETER_RECIPES_DIR` overrides). `busymeter recipes` lists them. Keys starting with `_` are comments.

**Running in the background.** `busymeter run <recipe>` runs in the console. An AI assistant's `install_recipe`
(busymeter-mcp) starts the same recipe as a background process for your user (`pythonw.exe -E -P`, no window), with
the recipes folder as its working folder and that folder kept off Python's module path (so nothing planted there can
be imported), logging to `<recipe>.log` in the same folder. Stopping it while it waits for Allow ends the wait at
once; the prompt already in the tray stays until you answer it or it expires (2 minutes), and the next run waits on
that same request instead of asking again. Stop it with `busymeter stop <recipe>`, the assistant's `stop_recipe`, by creating
an empty `<recipe>.stop` file there, or by ending its `pythonw.exe` in Task Manager (then its signals expire within
30 s). `busymeter recipes` shows which are running.

**Starting at sign-in (Windows, off by default).** `busymeter autostart vibe_meter on` makes that recipe start in the
background every time you sign in to Windows; `busymeter autostart vibe_meter off` stops that, and `busymeter
autostart` lists each recipe's state. Each recipe gets one per-user value, `Busy Meter recipe <recipe>`, under
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (no admin rights; nothing else there is touched). It runs this
environment's `pythonw.exe -E -P` (no window) on a small fixed launcher, `sign-in.pyw` in the recipes folder (Windows
allows only 260 characters per sign-in entry); the launcher is removed when no recipe uses it. At sign-in the recipe
waits for the Busy Meter app and for External connections like any run, starts nothing if that recipe is already
running, and the first time still asks for **Allow** in the tray. If you click **Deny**, it won't start at sign-in
again until you turn it on again (`busymeter autostart` shows it as skipped; the reason is in `<recipe>.log`).
`uninstall_recipe` removes the entry too. It needs the package installed for your user (inside your user folder:
`pip install --user busymeter`, or a Python installed just for you), so no other account can change what runs at
your sign-in; otherwise it refuses and changes nothing. A broken install is logged to `<recipe>.log`. Overrides set
only in a shell (`BUSYMETER_RECIPES_DIR`, `BUSYMETER_TOKEN_DIR`) aren't seen at sign-in. If you move or reinstall
Python or this package, turn it off and on again.

### `stream_status`

Shows when you're live or recording in **OBS Studio** (28 or later), through OBS's own built-in WebSocket server.

- **Set up OBS:** Tools → WebSocket Server Settings → Enable WebSocket server (port 4455). If "Enable Authentication"
  is on, copy the password into `"password"` in `stream_status.json`.
- **Reads:** only whether OBS is streaming or recording (`GetStreamStatus`, `GetRecordStatus` and the output
  events), from `127.0.0.1:<port>`.
- **Never reads:** scenes, sources, audio, video, chat or stream keys; it never changes anything in OBS. The password
  is never printed or logged, and never shown or accepted through the MCP server; only a SHA-256 challenge answer
  goes over the socket (obs-websocket v5 authentication).
- **Signals:** `live` (suggested 5) while streaming; `recording` (suggested 4) while recording and not streaming (a
  paused recording counts as not recording). OBS closed or the server off: nothing, and it tries again every 5 s.
- **Settings:** `port` (4455), `password` (file only), `live_level` (5), `recording_level` (4), `show_recording`
  (true).
- **WebSocket client:** a small standard-library client (`busymeter/recipes/_ws.py`) instead of a package: it only
  talks to one local program, and keeps busymeter free of third-party code.

### `game_state`

A local receiver for games that have an **official "game state" HTTP push** feature (the game posts its state as
JSON to a URL you set in the game's own config file). No game is named here; check your game's documentation for the
feature and its field names.

- **Set up the game:** point its state feature at `http://127.0.0.1:47822/` and give it the token from
  `game_state.json` (`busymeter run game_state --show-token` prints it). Most such features send it as `auth.token`
  in the JSON body; an `Authorization: Bearer <token>` or `X-Busy-Meter-Token` header works too.
- **Rules** (in `game_state.json`): a dotted `path` (a number picks a list item), one condition (`equals` text /
  number / true / false / null, `lt` or `gt` a number, `exists` true / false), a `signal` name, and optionally a
  `suggest`ed level and a `label`. Several rules may raise the same signal. Example (also the default, and
  `examples/game_state.example.json`; change the paths to your game's fields):

  ```json
  {"path": "round.phase", "equals": "live", "signal": "in-match", "suggest": 4}
  {"path": "player.state.health", "lt": 30, "signal": "low-health", "suggest": "flash"}
  ```

- **Expiry:** when the game stops posting for `stale_seconds` (30), the signals clear.
- **Security:** bound to `127.0.0.1` only; `Host` must be `127.0.0.1:<port>` or `localhost:<port>`; any request with
  an `Origin` header (a web page) gets 403; POST only; no chunked bodies; bodies over `max_body_kb` (64) are refused
  unread; at most 8 connections at once (more get 503), each with a 10 s read timeout; the token is compared in
  constant time (a malformed one is just a 401) and never printed, logged or shown through MCP. A port change to a
  port that's taken is reported and the receiver stays on the old port.
- **Reads:** only what the game posts, with the token. **Never reads** game files, memory or the screen. Keeps only
  the active signal names and the time of the last post.
- **Settings:** `port` (47822), `token` (made on first run and written into the file, so it stays the same; file
  only), `max_body_kb` (64), `stale_seconds` (30),
  `rules`.
- **Try it without a game:**
  `curl -X POST http://127.0.0.1:47822/ -H "Content-Type: application/json" -d "{\"auth\":{\"token\":\"<token>\"},\"round\":{\"phase\":\"live\"}}"`

### `focus_timer`

- `busymeter focus 25m`: one focus block, then it clears and exits. `busymeter focus --pomodoro`: focus / short break
  cycles with a long break every 4th (`--cycles 4`, `--break 5m`, `--long-break 15m` override the settings for that
  run). `busymeter run focus_timer` (and `install_recipe`) runs the cycles from the settings file.
- The first block starts once Busy Meter accepts the recipe (after the Allow), so waiting for the click doesn't eat
  into it. Prints the time left (a live countdown in a console, a line a minute in the log). Ctrl+C clears.
- **Reads:** only the clock.
- **Signal:** `focus` (suggested 2) during focus blocks; breaks clear it.
- **Settings:** `focus_minutes` (25), `short_break_minutes` (5), `long_break_minutes` (15), `long_break_every` (4),
  `cycles` (0 = until stopped), `focus_level` (2).

### `vibe_meter`

"See what happens when we're vibing": the meter fills up as you run Claude Code sessions in parallel.

- **Reads:** the names, sizes and modification times of Claude Code's transcript files,
  `%USERPROFILE%\.claude\projects\<project>\<session>.jsonl` (or `$CLAUDE_CONFIG_DIR\projects`), and from lines
  appended to active ones only the assistant message's `message.usage` token counts (input, output, cache creation,
  cache read) and its id, found by walking the line's top-level keys (a `usage` or `type` inside a tool call never
  counts); the rest of a line (message text, prompts, tool calls, file contents) is skipped by position and never
  decoded, kept, printed or sent.
- **Never:** writes to those files (opened read-only), re-reads whole files (it keeps an offset per file; files that
  exist when it starts are read from their end, so history isn't counted), or sends anything anywhere except signal
  names to the Busy Meter app on `127.0.0.1`.
- **Active session:** a transcript that changed in the last `active_minutes` (3).
- **Stars:** `driver` = `"sessions"` (default): `session_thresholds` [1, 2, 3, 4, 5, 6] (1 session = 1 star … 6+ =
  6 stars), or `"tokens"`: tokens per minute over the last 60 s against `token_thresholds` [2000, 10000, 25000,
  50000, 100000, 200000]. Cache-read tokens are left out of the rate unless `count_cache_reads` is true.
- **Vibe alert:** `vibe-alert` (suggested Flash, "Vibe alert" in the Gamer names) for `alert_seconds` (20) when
  parallel sessions reach `burst_sessions` (4) or the rate reaches `burst_tokens_per_minute` (150000; 0 turns either
  off), then quiet for `alert_cooldown_seconds` (300).
- **Signals:** `vibe-1` … `vibe-6` (suggested 1 … 6 stars; `star_levels` false = no suggestion) and `vibe-alert`.
- **Other settings:** `scan_seconds` (5), `alert_level` ("flash"), `projects_dir` (empty = default; file only).

## From an AI assistant (busymeter-mcp)

`list_recipes`, `install_recipe(name)`, `stop_recipe(name)`, `uninstall_recipe(name)`, `get_recipe_settings(name)`,
`set_recipe_settings(name, changes)`, `set_recipe_autostart(name, on)`. Only the four bundled names are accepted (no
paths, code or downloads). `set_recipe_autostart` turns starting at sign-in on or off (above); its reply says plainly
that the recipe will start every time you sign in, and `list_recipes` shows `startsAtSignIn` for each.
`install_recipe` writes the default settings and starts the recipe in the background; it then waits for **you** to
click Allow in the tray (the assistant can't). `set_recipe_settings` is checked against the schema: unknown keys and
out-of-range values are refused, and the OBS password, the game-state token and `projects_dir` can't be read or set
through it. `uninstall_recipe` stops the recipe, clears its signals, and deletes its settings, saved token and
sign-in entry; the
app keeps listing the approval until you Revoke it under Settings → Sources → External connections.
