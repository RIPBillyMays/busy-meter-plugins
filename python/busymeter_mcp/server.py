"""busymeter-mcp: an MCP server over stdio for the Busy Meter plugin API.

Run ``busymeter-mcp`` (or ``python -m busymeter_mcp``) from an MCP client. Uses the official ``mcp`` Python SDK
(MCPServer in mcp 2.x, called FastMCP in 1.x).
"""

from __future__ import annotations

from typing import Any

from .tools import DEFAULT_NAME, Tools

INSTRUCTIONS = (
    "Controls a Busy Meter status light (6 stars plus a lip accent) through the Busy Meter Windows app's local "
    "plugin API. Levels are star counts 0-6 (0 = lip accent only) and 'flash' (red/blue). First call "
    "register_plugin: the user must click Allow in the Busy Meter tray; you can ask, never approve. If a tool says "
    "External connections is off, tell the user to turn on Settings > Sources > External connections in the app. "
    "Levels you set go through the user's level map and are combined with the app's other sources, so another "
    "source may win; they expire after their TTL. Bundled recipes (list_recipes) run in the background as their own "
    "plugins: install_recipe starts one now and the user clicks Allow in the tray; set_recipe_autostart makes one "
    "start at every Windows sign-in (off by default; tell the user plainly it will start each time they sign in); "
    "get_recipe_settings / set_recipe_settings change their thresholds and levels. Passwords and tokens are never shown or set here: the "
    "user edits the settings file. Never drive the meter directly (no serial/COM port, no HTTP to the meter, no "
    "WLED presets): the app keeps such a level as the user's manual level, overriding their sources. A busy or "
    "'access denied' COM port is the app using the meter; never ask the user to quit the app or unplug the meter. "
    "Use only these tools; if they fail or are missing, tell the user how to fix or enable them instead of working "
    "around them."
)


def _server_class() -> Any:
    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x
        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x
        return FastMCP


def build_server(tools: Tools | None = None) -> Any:
    tools = tools or Tools()
    server = _server_class()("busymeter", instructions=INSTRUCTIONS)

    @server.tool()
    def get_status() -> dict[str, Any]:
        """What the Busy Meter shows now: level (0-6, "flash" or "off"), whether syncing is paused, the winning
        source's kind (camera, microphone, teams, slack, calendar, plugin, manual, none), and whether External
        connections is on."""
        return tools.get_status()

    @server.tool()
    def list_sources() -> dict[str, Any]:
        """What can be known about the meter's sources through the plugin API: the source kinds, the current
        winner, and this assistant's own signals. The API has no full list of sources or mappings; those are on
        Settings > Levels in the app."""
        return tools.list_sources()

    @server.tool()
    def register_plugin(name: str = DEFAULT_NAME, signals: list[dict[str, Any]] | None = None,
                        wants: list[str] | None = None, wait_seconds: int = 60, force: bool = False) -> dict[str, Any]:
        """Ask the user to allow this assistant as a Busy Meter source. Shows a prompt in the Busy Meter tray and
        waits up to wait_seconds (max 110) for the click; call again to keep waiting. You can't approve it yourself.
        signals: optional list of {"name": "a-z0-9-", "label": "text", "suggest": 0-6 or "flash"} for set_signals.
        wants: any of "signals", "status", "taps", "effects" (default signals + status).
        force: ask again (e.g. with new signals) even if already allowed."""
        return tools.register_plugin(name=name, signals=signals, wants=wants, wait_seconds=wait_seconds, force=force)

    @server.tool()
    def set_signals(active: list[str], ttl_seconds: int = 60) -> dict[str, Any]:
        """Replace this assistant's active signals (names declared in register_plugin, or level-0 ... level-6,
        level-flash) for ttl_seconds (1-600). An empty list clears."""
        return tools.set_signals(active=active, ttl_seconds=ttl_seconds)

    @server.tool()
    def set_level(level: int | str, ttl: int | str = 600) -> dict[str, Any]:
        """Show a level: 0-6 stars (0 = lip accent only) or "flash" (red/blue), for ttl (seconds, or text like
        "30s", "10m", "2h"; default 10 minutes). Over 10 minutes this server keeps refreshing it while it runs.
        The user's level map applies and other sources may win."""
        return tools.set_level(level=level, ttl=ttl)

    @server.tool()
    def clear() -> dict[str, Any]:
        """Clear this assistant's level and signals now."""
        return tools.clear()

    @server.tool()
    def list_recipes() -> dict[str, Any]:
        """The bundled recipes (stream_status, game_state, focus_timer, vibe_meter): what each reads and never reads,
        whether it is installed and running, and whether it starts at sign-in (startsAtSignIn). Only these can be
        installed; no other code."""
        return tools.list_recipes()

    @server.tool()
    def install_recipe(name: str) -> dict[str, Any]:
        """Install a bundled recipe by name: writes its default settings file and starts it in the background for
        this user. It then waits for the user to click Allow in the Busy Meter tray (you can't approve it). It runs
        until stop_recipe; it starts again at sign-in only if set_recipe_autostart turned that on."""
        return tools.install_recipe(name)

    @server.tool()
    def stop_recipe(name: str) -> dict[str, Any]:
        """Stop a bundled recipe running in the background; it clears its signals."""
        return tools.stop_recipe(name)

    @server.tool()
    def uninstall_recipe(name: str) -> dict[str, Any]:
        """Stop a bundled recipe, clear its signals and delete its settings, saved token and sign-in entry. The user
        can revoke its approval in the app (Settings > Sources > External connections)."""
        return tools.uninstall_recipe(name)

    @server.tool()
    def set_recipe_autostart(name: str, on: bool) -> dict[str, Any]:
        """Windows only. on=true makes a bundled recipe start in the background every time the user signs in to
        Windows (one per-user entry in the Run key, nothing else); on=false removes it. Off by default; bundled names
        only. Tell the user plainly that it will start at every sign-in. It doesn't start the recipe now (that's
        install_recipe), and a recipe that was never allowed still needs the user's Allow in the tray the first
        time; after a Deny it isn't started at sign-in again until this is turned on again. Needs the package
        installed for this user (pip install --user)."""
        return tools.set_recipe_autostart(name, on)

    @server.tool()
    def get_recipe_settings(name: str) -> dict[str, Any]:
        """A bundled recipe's settings and their schema (types, ranges, defaults). Passwords and tokens are never
        shown."""
        return tools.get_recipe_settings(name)

    @server.tool()
    def set_recipe_settings(name: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Change some of a bundled recipe's settings, e.g. {"burst_sessions": 5} or {"live_level": 6}. Checked
        against its schema: unknown keys and out-of-range values are refused, and passwords, tokens and folder paths
        can't be set here (the user edits the settings file). A running recipe picks the change up; if its signals
        or suggested levels change, the user clicks Allow again in the tray."""
        return tools.set_recipe_settings(name, changes)

    return server


def main() -> None:
    try:
        server = build_server()
    except ImportError:
        import sys

        print('busymeter-mcp needs the MCP SDK: pip install "busymeter[mcp]" (pins mcp==2.3.0).', file=sys.stderr)
        raise SystemExit(1)
    server.run()


if __name__ == "__main__":
    main()
