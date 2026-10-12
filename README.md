# Busy Meter plugins (MIT)

Clients, an MCP server, recipes and the API spec for the **Busy Meter** status light's local plugin API: make the meter
react to anything (a script, a webhook, your stream, a focus timer, an AI assistant) through the Busy Meter Windows
app. Everything here is MIT licensed (see `LICENSE`). The Busy Meter app itself is not part of this repository.

Community plugins built on this are not supported by the Busy Meter authors.

| Path | What |
| --- | --- |
| `spec/openapi.yaml` | OpenAPI 3.1 spec of the local plugin API (as built). The app's own tests run it against the real app, so it can't drift |
| `spec/ws-messages.schema.json` | JSON Schema of the `/v1/ws` WebSocket messages |
| `python/busymeter/` | Python client, standard library only; `busymeter` command (`busymeter level 5 --for 10m`) |
| `python/busymeter/recipes/` | The bundled recipes and their common runner (`busymeter run <recipe>`, `busymeter focus 25m`) |
| `python/busymeter_mcp/` | `busymeter-mcp`, an MCP server over stdio for AI assistants (levels, plus installing and configuring bundled recipes) |
| `python/tests/` | `python -m unittest` (run from `python/`); `e2e_drive.py` is run against the real app by the app's own tests |
| `dotnet/` | C# client `BusyMeter.Plugins` (.NET 8+, no third-party packages): same features, rules, token files and messages as the Python client, plus `WithdrawAsync`. Tests in `dotnet/tests/` (fake API, `dotnet test dotnet/BusyMeter.Plugins.slnx`); the app's own tests also run it against the real app. See `dotnet/README.md` |
| `js/` | JavaScript client for Node 18.3+ (npm `busymeter`, no dependencies, TypeScript declarations): the Python client's features and token files, including `withdraw()`, and the `busymeter` command (level/clear/status). `npm test` from `js/`; `tests/e2e_drive.mjs` is run against the real app by the app's own tests. See `js/README.md` |
| `recipes/` | Ready-to-run scripts: stream status (OBS), game state receiver, focus timer, vibe meter (Claude Code), CLI level, local webhook bridge, mic loudness. See `recipes/README.md` |
| `llms.txt` | Agent-readable index, the source for thebusymeter.com/llms.txt |

The API in one paragraph: the Busy Meter Windows app listens on `127.0.0.1` only, and only while the user has turned
on **Settings → Sources → External connections** (off by default). The port is in
`%LOCALAPPDATA%\BusyMeter\plugin-api.json` while it listens (default 47820). A program registers, the user clicks
**Allow** in the Busy Meter tray, and the program gets a token. Plugins report signals or a level (`0`–`6` stars,
`"flash"`) with a time to live of at most 10 minutes; the app runs them through the user's level map and combines them
with its own sources. Browsers can't use it (any `Origin` header is refused).

Python package docs, the MCP client config and token storage: `python/README.md`.

**Rule for AI agents and plugins:** never drive the meter's WLED directly while the Busy Meter app runs (no HTTP or
serial to the meter, no preset numbers). The app keeps any level set on the meter from outside as the
user's manual level, so a direct write overrides their sources. Always use the busymeter MCP tools or the plugin API. If
the busymeter tools aren't available, tell the user to install or enable them (below) instead of working around it.

## Use it from Claude Desktop

Install the package with the MCP extra (`pip install "busymeter[mcp]"`, or `pip install ".[mcp]"` from `python/`), then
add the server to `claude_desktop_config.json` (on Windows `%APPDATA%\Claude\claude_desktop_config.json`; Claude
Desktop → Settings → Developer → Edit Config opens it). **Edit it only while Claude Desktop is fully quit** (closing the
window isn't enough: quit it from its tray icon): it rewrites the file while it runs, so an entry added then can be lost.
Then start Claude Desktop again:

```json
{
  "mcpServers": {
    "busymeter": {
      "command": "busymeter-mcp"
    }
  }
}
```

From a checkout without installing, use `"command": "python"`, `"args": ["-m", "busymeter_mcp"]` and
`"env": {"PYTHONPATH": "C:\\path\\to\\busy-meter-plugins\\python"}` instead (JSON needs the doubled backslashes). Then ask, for example: "Install the
vibe meter recipe and alert me at 5 parallel sessions." The assistant calls `install_recipe("vibe_meter")` and
`set_recipe_settings("vibe_meter", {"burst_sessions": 5})`; you click **Allow** in the Busy Meter tray. Tools:
`get_status`, `list_sources`, `register_plugin`, `set_signals`, `set_level`, `clear`, `list_recipes`,
`install_recipe`, `stop_recipe`, `uninstall_recipe`, `get_recipe_settings`, `set_recipe_settings`,
`set_recipe_autostart` (start a recipe at every Windows sign-in; off by default). The assistant can
ask, never approve; passwords and tokens stay in the settings files.
