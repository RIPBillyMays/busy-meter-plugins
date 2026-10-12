"""The busymeter-mcp tools, called directly with a fake client (no network, no MCP SDK needed)."""

import asyncio
import os
import threading
import time
import tempfile
import unittest
from pathlib import Path

from busymeter import (
    TURN_ON_MESSAGE,
    ApprovalTimeoutError,
    DeniedError,
    ForbiddenError,
    NotListeningError,
    TokenStore,
    UnauthorizedError,
)
from busymeter_mcp.tools import DEFAULT_PLUGIN_ID, WAITING_MESSAGE, Tools

try:
    from busymeter_mcp.server import build_server

    build_server(Tools(client_factory=lambda pid: None))
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False


class FakeClient:
    """Stands in for busymeter.BusyMeter."""

    def __init__(self, store: TokenStore, plugin_id: str) -> None:
        self.store = store
        self.plugin_id = plugin_id
        self.listening = True
        self.token = None
        self.pending = None
        self.approval = "approve"  # approve | deny | wait
        self.calls = []
        self.fail = None
        self.token_valid = True

    def is_listening(self):
        return self.listening

    def _check(self):
        if not self.listening:
            raise NotListeningError(TURN_ON_MESSAGE, "not_listening")
        if self.fail:
            raise self.fail

    def status(self):
        self._check()
        self.calls.append(("status",))
        return {"level": 4, "paused": False, "source": "teams"}

    def pending_request(self):
        return self.pending

    def register(self, name=None, signals=(), wants=()):
        self._check()
        self.calls.append(("register", name, list(signals), list(wants)))
        self.pending = "req-1"
        self.store.save_pending(self.plugin_id, "req-1", list(signals), list(wants))
        return "req-1"

    def wait_for_approval(self, request_id=None, timeout=0, **_):
        self.calls.append(("wait", request_id, timeout))
        if self.approval == "wait":
            raise ApprovalTimeoutError("Still waiting for you to click Allow in the Busy Meter tray.", "pending")
        if self.approval == "deny":
            self.pending = None
            raise DeniedError("The user clicked Deny in the Busy Meter tray.", "denied")
        self.token = "t" * 43
        self.pending = None
        self.store.save_token(self.plugin_id, self.token)
        return self.token

    def verify_token(self):
        self.calls.append(("verify",))
        if not self.listening:
            raise NotListeningError(TURN_ON_MESSAGE, "not_listening")
        if not self.token_valid:  # revoked: the real client deletes the saved token on the 401
            self.token = None
            self.store.drop_token(self.plugin_id)
            return False
        return bool(self.token)

    def forget(self):
        self.token = None
        self.store.delete(self.plugin_id)

    def set_level(self, level, ttl):
        self._check()
        self.calls.append(("level", level, ttl))
        return {"active": [f"level-{level}"], "expiresInSeconds": ttl}

    def set_signals(self, active, ttl):
        self._check()
        if not 1 <= ttl <= 600:
            raise ValueError("ttl_seconds must be 1-600.")
        self.calls.append(("signals", list(active), ttl))
        return {"active": list(active), "expiresInSeconds": ttl}

    def clear(self):
        self._check()
        self.calls.append(("clear",))
        return {"active": [], "expiresInSeconds": 0}


class _WithTools(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TokenStore(Path(tmp.name), restrict=False)
        self.client = FakeClient(self.store, DEFAULT_PLUGIN_ID)
        self.tools = Tools(client_factory=lambda pid: self.client)
        self.addCleanup(self.tools._stop_hold)


class ToolTests(_WithTools):
    def test_register_waits_for_the_user(self) -> None:
        self.client.approval = "wait"
        reply = self.tools.register_plugin(name="Heart rate", signals=[{"name": "hr-high", "suggest": 5}], wait_seconds=1)
        self.assertEqual("pending", reply["state"])
        self.assertIn("click Allow in the Busy Meter tray", reply["message"])

        # Calling again keeps waiting on the same request (no second prompt).
        self.client.approval = "approve"
        reply = self.tools.register_plugin(name="Heart rate", wait_seconds=1)
        self.assertEqual("approved", reply["state"])
        self.assertEqual(1, sum(1 for c in self.client.calls if c[0] == "register"))
        self.assertEqual(("wait", "req-1", 1), self.client.calls[-1])

    def test_register_without_waiting_returns_the_waiting_message(self) -> None:
        reply = self.tools.register_plugin(wait_seconds=0)
        self.assertEqual({"ok": True, "state": "pending", "requestId": "req-1", "message": WAITING_MESSAGE}, reply)

    def test_wait_is_capped(self) -> None:
        self.tools.register_plugin(wait_seconds=10_000)
        self.assertEqual(110, self.client.calls[-1][2])

    def test_register_denied(self) -> None:
        self.client.approval = "deny"
        reply = self.tools.register_plugin(wait_seconds=1)
        self.assertFalse(reply["ok"])
        self.assertEqual("denied", reply["state"])

    def test_already_registered_and_force(self) -> None:
        self.tools.register_plugin(wait_seconds=1)
        self.assertIn("Already allowed", self.tools.register_plugin()["message"])
        self.tools.register_plugin(signals=[{"name": "new-one"}], force=True, wait_seconds=1)
        self.assertEqual(2, sum(1 for c in self.client.calls if c[0] == "register"))

    def test_the_agent_can_not_approve(self) -> None:
        public = [n for n in dir(self.tools) if not n.startswith("_")]
        self.assertFalse([n for n in public if "approv" in n or "allow" in n], public)

    def test_every_tool_says_turn_on_external_connections(self) -> None:
        self.client.listening = False
        for reply in (self.tools.get_status(), self.tools.register_plugin(wait_seconds=0), self.tools.set_level(3),
                      self.tools.set_signals(["x"]), self.tools.clear()):
            self.assertFalse(reply["ok"])
            self.assertIn("Settings → Sources → External connections", reply["message"])
        self.assertIn("External connections", self.tools.list_sources()["message"])

    def test_get_status(self) -> None:
        self.assertFalse(self.tools.get_status()["registered"])
        self.tools.register_plugin(wait_seconds=1)
        reply = self.tools.get_status()
        self.assertEqual((True, 4, "teams", "on"), (reply["ok"], reply["level"], reply["source"], reply["externalConnections"]))
        self.assertEqual("The meter shows 4 stars; winning source: teams.", reply["message"])

    def test_status_errors_are_relayed(self) -> None:
        self.tools.register_plugin(wait_seconds=1)
        self.client.fail = ForbiddenError("Busy Meter didn't allow this plugin to do that.", "not_allowed", 403)
        self.assertEqual("not_allowed", self.tools.get_status()["error"])
        self.client.fail = UnauthorizedError("Register again", "unauthorized", 401)
        self.assertFalse(self.tools.get_status()["registered"])

    def test_list_sources_is_honest_about_what_it_knows(self) -> None:
        self.tools.register_plugin(signals=[{"name": "hr-high", "suggest": 5}], wait_seconds=1)
        reply = self.tools.list_sources()
        self.assertFalse(reply["complete"])
        self.assertIn("Settings → Levels", reply["message"])
        self.assertEqual("teams", reply["currentWinner"])
        self.assertEqual([{"name": "hr-high", "suggest": 5}], reply["thisPlugin"]["signals"])
        self.assertIn("level-flash", reply["thisPlugin"]["levelShortcuts"])

    def test_set_level(self) -> None:
        self.assertTrue(self.tools.set_level(5, "10m")["ok"])
        self.assertTrue(self.tools.set_level("flash", 30)["ok"])
        self.assertEqual([("level", 5, 600), ("level", "flash", 30)], self.client.calls)
        for bad in (7, "loud", -1):
            self.assertEqual("bad_request", self.tools.set_level(bad)["error"])
        self.assertEqual("bad_request", self.tools.set_level(3, "never")["error"])

    def test_long_level_is_refreshed_and_clear_stops_it(self) -> None:
        reply = self.tools.set_level(6, "2h")
        self.assertIn("refreshes", reply["message"])
        self.assertEqual(("level", 6, 60), self.client.calls[-1])
        self.assertIsNotNone(self.tools._hold)
        self.tools.clear()
        self.assertIsNone(self.tools._hold)
        self.assertEqual(("clear",), self.client.calls[-1])

    def test_set_signals_and_clear(self) -> None:
        self.assertEqual(["hr-high"], self.tools.set_signals(["hr-high"], 30)["active"])
        self.assertEqual("bad_request", self.tools.set_signals(["hr-high"], 9999)["error"])
        self.assertEqual([], self.tools.clear()["active"])


class RevokeAndForceTests(_WithTools):
    def test_a_revoked_token_is_not_already_allowed(self) -> None:
        self.tools.register_plugin(wait_seconds=1)
        self.client.token_valid = False  # the user clicked Revoke
        self.client.approval = "wait"
        reply = self.tools.register_plugin(wait_seconds=1)
        self.assertEqual("pending", reply["state"])
        self.assertEqual(2, sum(1 for c in self.client.calls if c[0] == "register"))
        self.assertIsNone((self.store.load(DEFAULT_PLUGIN_ID) or {}).get("token"))

    def test_force_keeps_the_working_token_until_the_new_one_arrives(self) -> None:
        self.tools.register_plugin(wait_seconds=1)
        old = self.client.token
        self.client.approval = "wait"
        self.assertEqual("pending", self.tools.register_plugin(signals=[{"name": "new-one"}], force=True, wait_seconds=1)["state"])
        self.assertEqual(old, self.client.token)
        self.assertEqual(old, self.store.load(DEFAULT_PLUGIN_ID)["token"])
        self.client.approval = "deny"
        self.assertEqual("denied", self.tools.register_plugin(force=True, wait_seconds=1)["state"])
        self.assertEqual(old, self.client.token)  # a Deny leaves the working token alone

    def test_overlapping_long_levels_leave_one_refresher(self) -> None:
        threads = [threading.Thread(target=self.tools.set_level, args=(i % 7, "2h")) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, self.tools.active_holds())
        deadline = time.monotonic() + 5
        while sum(1 for t in threading.enumerate() if t.name == "busymeter-hold" and t.is_alive()) > 1:
            self.assertLess(time.monotonic(), deadline, "stopped refreshers didn't exit")
            time.sleep(0.01)
        self.tools.clear()
        self.assertEqual(0, self.tools.active_holds())


REQUIRE = os.environ.get("BUSYMETER_REQUIRE_PYTHON") == "1"


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        if not HAVE_MCP:
            if REQUIRE:
                self.fail("BUSYMETER_REQUIRE_PYTHON=1 but the mcp package isn't installed (pip install --user mcp==2.3.0)")
            self.skipTest("the mcp package isn't installed (pip install --user mcp==2.3.0)")

    def test_the_server_exposes_exactly_these_tools(self) -> None:
        server = build_server(Tools(client_factory=lambda pid: None))
        names = sorted(t.name for t in asyncio.run(server.list_tools()))
        self.assertEqual(["clear", "get_recipe_settings", "get_status", "install_recipe", "list_recipes",
                          "list_sources", "register_plugin", "set_level", "set_recipe_autostart", "set_recipe_settings", "set_signals",
                          "stop_recipe", "uninstall_recipe"], names)
        self.assertNotIn("approve", " ".join(names))  # the agent can ask, never approve


if __name__ == "__main__":
    unittest.main()
