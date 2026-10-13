import json
import os
import socket
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from unittest import mock

import busymeter.client
import busymeter.tokens
from busymeter import (
    NOT_RUNNING_MESSAGE,
    AppNotRespondingError,
    ApprovalTimeoutError,
    BusyMeter,
    BusyMeterError,
    ConflictError,
    DeniedError,
    ForbiddenError,
    NotListeningError,
    RateLimitedError,
    TokenStore,
    UnauthorizedError,
    discover,
    normalize_level,
    parse_duration,
)
from busymeter.cli import hold_level

from .fake_api import FakeApi


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TokenStore(Path(self.tmp.name) / "tokens", restrict=False)
        self.api = FakeApi().__enter__()
        self.addCleanup(self.api.__exit__)

    def meter(self, plugin_id: str = "com.example.test", **kw) -> BusyMeter:
        kw.setdefault("port", self.api.port)
        return BusyMeter(plugin_id, store=self.store, **kw)


class DiscoveryTests(unittest.TestCase):
    def write(self, content: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "plugin-api.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_port_from_the_discovery_file(self) -> None:
        endpoint = discover(self.write('{"port":51234,"baseUrl":"http://127.0.0.1:51234/v1/","apiVersion":1}'))
        self.assertEqual((51234, True), (endpoint.port, endpoint.from_file))
        self.assertEqual("http://127.0.0.1:51234/v1/", endpoint.base_url)

    def test_no_file_falls_back_to_47820(self) -> None:
        endpoint = discover(Path(tempfile.gettempdir()) / "no-such-dir-bm" / "plugin-api.json")
        self.assertEqual((47820, False), (endpoint.port, endpoint.from_file))

    def test_bad_files_fall_back(self) -> None:
        for content in ("not json", "[]", '{"port":"80"}', '{"port":80}', '{"port":70000}', '{"port":true}'):
            self.assertEqual(47820, discover(self.write(content)).port, content)

    def test_only_the_port_is_taken_never_another_host(self) -> None:
        endpoint = discover(self.write('{"port":50000,"baseUrl":"http://evil.example:50000/v1/"}'))
        self.assertEqual("http://127.0.0.1:50000/v1/", endpoint.base_url)

    def test_environment_override(self) -> None:
        path = self.write('{"port":50001}')
        old = os.environ.get("BUSYMETER_DISCOVERY_FILE")
        os.environ["BUSYMETER_DISCOVERY_FILE"] = str(path)
        try:
            self.assertEqual(50001, BusyMeter("com.example.x", store=TokenStore(path.parent, restrict=False)).endpoint.port)
        finally:
            if old is None:
                del os.environ["BUSYMETER_DISCOVERY_FILE"]
            else:
                os.environ["BUSYMETER_DISCOVERY_FILE"] = old

    def test_client_uses_the_discovered_port(self) -> None:
        with FakeApi() as api:
            path = self.write(json.dumps({"port": api.port}))
            meter = BusyMeter("com.example.x", discovery_file=path, store=TokenStore(path.parent / "t", restrict=False))
            self.assertTrue(meter.is_listening())


class RegisterTests(Base):
    def test_register_wait_and_token_is_saved(self) -> None:
        meter = self.meter()
        request_id = meter.register(name="Test", signals=[("hr-high", "Heart rate 140+", 5), "hr-low"], wants=["signals", "status"])
        self.assertEqual(request_id, meter.pending_request())
        self.assertEqual({"state": "pending"}, meter.poll(request_id))

        sent = json.loads(self.api.requests[0]["body"])
        self.assertEqual({"id": "com.example.test", "name": "Test", "wants": ["signals", "status"],
                          "signals": [{"name": "hr-high", "label": "Heart rate 140+", "suggest": 5}, {"name": "hr-low"}]}, sent)

        self.api.answer = True
        token = meter.wait_for_approval(timeout=5, interval=0.01)
        self.assertEqual(43, len(token))
        saved = self.store.load("com.example.test")
        self.assertEqual(token, saved["token"])
        self.assertNotIn("pendingRequestId", saved)
        self.assertEqual(["signals", "status"], saved["wants"])

        # A new client (another run) finds the token in the store.
        again = self.meter()
        self.assertEqual(token, again.token)
        self.assertEqual(4, again.status()["level"])

    def test_connect_registers_once_and_tells_the_user_to_click_allow(self) -> None:
        told = []
        self.api.answer = True
        meter = self.meter()
        token = meter.connect(name="Test", on_wait=lambda: told.append(1))
        self.assertTrue(token)
        self.assertEqual(token, self.meter().connect(name="Test"))
        self.assertEqual(1, sum(1 for r in self.api.requests if r["path"] == "/v1/register"))

    def test_a_saved_pending_request_is_resumed_not_repeated(self) -> None:
        meter = self.meter()
        meter.register(name="Test")
        self.api.answer = True
        self.meter().connect(name="Test", timeout=5)
        self.assertEqual(1, sum(1 for r in self.api.requests if r["path"] == "/v1/register"))

    def test_waiting_times_out_with_a_clear_message(self) -> None:
        meter = self.meter()
        meter.register()
        with self.assertRaises(ApprovalTimeoutError) as e:
            meter.wait_for_approval(timeout=0.05, interval=0.01)
        self.assertIn("click Allow in the Busy Meter tray", e.exception.message)

    def test_deny_and_expiry(self) -> None:
        meter = self.meter()
        meter.register()
        self.api.answer = False
        with self.assertRaises(DeniedError) as e:
            meter.wait_for_approval(timeout=5, interval=0.01)
        self.assertEqual("denied", e.exception.code)
        self.assertIsNone(self.store.load("com.example.test"))

        self.api.answer = None
        self.api.expire = True
        meter.register()
        with self.assertRaises(DeniedError) as e:
            meter.wait_for_approval(timeout=5, interval=0.01)
        self.assertEqual("expired", e.exception.code)
        self.assertIn("within 2 minutes", e.exception.message)

    def test_already_pending_is_409(self) -> None:
        self.meter().register()
        other_store = TokenStore(Path(self.tmp.name) / "other", restrict=False)
        with self.assertRaises(ConflictError) as e:
            BusyMeter("com.example.test", port=self.api.port, store=other_store).register()
        self.assertEqual("already_pending", e.exception.code)
        self.assertIn("already waiting", e.exception.message)

    def test_bad_ids_and_signals_are_refused_locally(self) -> None:
        with self.assertRaises(ValueError):
            BusyMeter("Bad Id", store=self.store)
        with self.assertRaises(ValueError):
            self.meter().register(signals=["level-4"])
        with self.assertRaises(ValueError):
            self.meter().register(signals=[("ok", "x", 9)])


class CancelTests(Base):
    def test_cancel_stops_the_wait_and_keeps_the_request(self) -> None:
        import threading as _threading

        from busymeter import CancelledError

        cancel = _threading.Event()
        meter = BusyMeter("com.example.cancel", port=self.api.port, store=self.store)
        _threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(CancelledError):
            meter.connect(name="x", timeout=30, cancel=cancel)
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(meter.pending_request())
        with self.assertRaises(CancelledError):  # already cancelled: no second request
            meter.connect(name="x", timeout=30, cancel=cancel)
        self.assertEqual(1, sum(1 for r in self.api.requests if r["path"] == "/v1/register"))


    # ----- withdraw -----

    def test_withdraw_takes_back_the_waiting_request(self) -> None:
        meter = BusyMeter("com.example.wd", port=self.api.port, store=self.store)
        request_id = meter.register(name="x")
        self.assertEqual({"state": "withdrawn"}, meter.withdraw())
        self.assertEqual([request_id], self.api.withdrawn)
        self.assertIsNone(meter.pending_request())
        self.assertIsNone(self.store.load("com.example.wd"))
        delete = [r for r in self.api.requests if r["method"] == "DELETE"][0]
        self.assertEqual("/v1/register/" + request_id, delete["path"])
        self.assertNotIn("Authorization", delete["headers"])
        self.assertNotIn("Origin", delete["headers"])

    def test_withdraw_after_the_user_allowed_collects_the_token(self) -> None:
        meter = BusyMeter("com.example.wd", port=self.api.port, store=self.store)
        meter.register(name="x")
        self.api.answer = True
        reply = meter.withdraw()
        self.assertEqual("approved", reply["state"])
        self.assertEqual(reply["token"], self.store.load("com.example.wd")["token"])
        self.assertIsNone(meter.pending_request())
        self.assertEqual([], self.api.withdrawn)

    def test_withdraw_after_a_deny_clears_the_request(self) -> None:
        meter = BusyMeter("com.example.wd", port=self.api.port, store=self.store)
        meter.register(name="x")
        self.api.answer = False
        self.assertEqual("denied", meter.withdraw()["state"])
        self.assertIsNone(self.store.load("com.example.wd"))

    def test_withdraw_of_an_unknown_request_clears_it(self) -> None:
        meter = BusyMeter("com.example.wd", port=self.api.port, store=self.store)
        self.store.save_pending("com.example.wd", "gone-request", [], ["signals"])
        self.assertEqual({"state": "not_found"}, meter.withdraw())
        self.assertIsNone(meter.pending_request())

    def test_withdraw_with_nothing_waiting_sends_nothing(self) -> None:
        meter = BusyMeter("com.example.wd", port=self.api.port, store=self.store)
        self.assertEqual({"state": "none"}, meter.withdraw())
        self.assertEqual([], self.api.requests)
        with self.assertRaises(ValueError):
            meter.withdraw("../status")

    def test_withdraw_keeps_a_saved_token(self) -> None:
        meter = BusyMeter("com.example.wd", port=self.api.port, store=self.store)
        self.store.save_token("com.example.wd", "saved-token")
        meter.register(name="x")
        meter.withdraw()
        self.assertEqual("saved-token", self.store.load("com.example.wd")["token"])


class TokenStoreTests(Base):
    def test_file_location_and_content(self) -> None:
        self.store.save_token("com.example.test", "tok")
        path = Path(self.tmp.name) / "tokens" / "com.example.test.json"
        self.assertEqual(path, self.store.path("com.example.test"))
        self.assertEqual({"pluginId": "com.example.test", "token": "tok"}, {k: v for k, v in json.loads(path.read_text()).items() if k != "savedAt"})
        self.assertEqual(["com.example.test"], self.store.plugin_ids())
        self.assertEqual([], [p.name for p in path.parent.iterdir() if p.name.startswith(".tmp-")])

    @unittest.skipIf(sys.platform == "win32", "POSIX modes; on Windows the folder's ACL is narrowed with icacls")
    def test_posix_permissions(self) -> None:
        self.store.save_token("com.example.test", "tok")
        self.assertEqual(0o600, stat.S_IMODE(self.store.path("com.example.test").stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.store.directory.stat().st_mode))

    @unittest.skipUnless(sys.platform == "win32", "Windows ACL")
    def test_windows_folder_is_narrowed_to_the_user(self) -> None:
        store = TokenStore(Path(self.tmp.name) / "acl")
        store.save_token("com.example.test", "tok")
        self.assertEqual("tok", store.load("com.example.test")["token"])
        import subprocess
        out = subprocess.run(["icacls", str(store.directory)], capture_output=True, text=True).stdout
        self.assertNotIn("(I)", out)  # nothing inherited
        self.assertNotIn("Everyone", out)

    def test_ids_can_not_escape_the_folder(self) -> None:
        for bad in ("../x", "..", "a/b", "C:\\x", ""):
            with self.assertRaises(ValueError):
                self.store.path(bad)

    def test_forget(self) -> None:
        meter = self.meter()
        self.store.save_token(meter.plugin_id, "tok")
        meter.forget()
        self.assertIsNone(self.store.load(meter.plugin_id))


class CallTests(Base):
    def connected(self, wants=None) -> BusyMeter:
        token = self.api.approve_token("com.example.test", wants)
        return self.meter(token=token)

    def test_requests_are_what_the_api_requires(self) -> None:
        meter = self.connected()
        meter.set_level(5, 600)
        meter.set_level("flash", 10)
        meter.set_signals(["hr-high"], 30)
        meter.clear()
        meter.status()
        meter.alert("lip-pulse", "#F0C850", 1500)
        for r in self.api.requests:
            headers = {k.lower(): v for k, v in r["headers"].items()}
            self.assertNotIn("origin", headers)
            self.assertEqual(f"127.0.0.1:{self.api.port}", headers["host"])
            self.assertNotIn("?", r["path"])
            self.assertTrue(headers["authorization"].startswith("Bearer "))
            if r["body"]:
                self.assertEqual("application/json", headers["content-type"])
        bodies = [json.loads(r["body"]) for r in self.api.requests if r["body"]]
        self.assertEqual({"level": 5, "ttlSeconds": 600}, bodies[0])
        self.assertEqual({"level": "flash", "ttlSeconds": 10}, bodies[1])
        self.assertEqual({"active": ["hr-high"], "ttlSeconds": 30}, bodies[2])
        self.assertEqual({"effect": "lip-pulse", "color": "#F0C850", "durationMs": 1500}, bodies[3])
        self.assertEqual("DELETE", self.api.requests[3]["method"])

    def test_local_validation(self) -> None:
        meter = self.connected()
        for bad in (7, -1, "loud", True, 2.5):
            with self.assertRaises(ValueError):
                meter.set_level(bad, 30)
        for ttl in (0, 601, "10m", True):
            with self.assertRaises(ValueError):
                meter.set_level(3, ttl)
        with self.assertRaises(ValueError):
            meter.alert("rainbow")
        self.assertEqual([], self.api.requests)

    def test_401_says_register_again(self) -> None:
        meter = self.meter(token="revoked")
        with self.assertRaises(UnauthorizedError) as e:
            meter.status()
        self.assertEqual(401, e.exception.status)
        self.assertIn("Register again", e.exception.message)

    def test_no_token_yet_is_a_clear_error_without_a_request(self) -> None:
        with self.assertRaises(UnauthorizedError) as e:
            self.meter().set_level(3, 30)
        self.assertIn("click Allow", e.exception.message)
        self.assertEqual([], self.api.requests)

    def test_403_plugin_off_and_not_allowed(self) -> None:
        meter = self.connected()
        self.api.forced["PUT /v1/signals"] = (403, "plugin_off", "x")
        with self.assertRaises(ForbiddenError) as e:
            meter.set_level(3, 30)
        self.assertIn("switched off", e.exception.message)
        self.api.forced["GET /v1/status"] = (403, "not_allowed", "x")
        with self.assertRaises(ForbiddenError) as e:
            meter.status()
        self.assertEqual("not_allowed", e.exception.code)

    def test_409_and_429(self) -> None:
        meter = self.connected()
        self.api.forced["POST /v1/alert"] = (409, "ignored", "paused")
        with self.assertRaises(ConflictError) as e:
            meter.alert("lip-pulse")
        self.assertIn("paused", e.exception.message)
        self.api.forced["PUT /v1/signals"] = (429, "rate_limited", "At most 10 updates a second.")
        with self.assertRaises(RateLimitedError) as e:
            meter.set_level(3, 30)
        self.assertEqual(1.0, e.exception.retry_after)
        self.api.forced["POST /v1/register"] = (429, "cooldown", "x")
        with self.assertRaises(RateLimitedError) as e:
            meter.register()
        self.assertIn("denied in the last 10 minutes", e.exception.message)

    def test_connection_refused_means_turn_on_external_connections(self) -> None:
        meter = BusyMeter("com.example.test", port=free_port(), store=self.store, token="t", timeout=2)
        self.assertFalse(meter.is_listening())
        for call in (meter.status, lambda: meter.set_level(2, 30), meter.register):
            with self.assertRaises(NotListeningError) as e:
                call()
            self.assertIn("Settings \u2192 Sources \u2192 External connections", e.exception.message)

    def test_errors_share_a_base_class(self) -> None:
        self.assertTrue(issubclass(NotListeningError, BusyMeterError))


class RevokedTokenTests(Base):
    def test_a_401_deletes_the_saved_token_but_keeps_a_waiting_request(self) -> None:
        meter = self.meter()
        self.store.save_token(meter.plugin_id, "revoked-token")
        with self.assertRaises(UnauthorizedError):
            meter.set_level(3, 30)
        self.assertIsNone(self.store.load(meter.plugin_id))
        self.assertIsNone(meter.token)

        meter.register()
        self.store.save_token(meter.plugin_id, "x")  # a token next to a waiting request (e.g. force re-register)
        meter.store.save_pending(meter.plugin_id, "req", [], ["signals"])
        meter._token = None
        with self.assertRaises(UnauthorizedError):
            meter.status()
        saved = self.store.load(meter.plugin_id)
        self.assertNotIn("token", saved)
        self.assertEqual("req", saved["pendingRequestId"])

    def test_connect_registers_again_when_the_saved_token_was_revoked(self) -> None:
        self.store.save_token("com.example.test", "revoked-token")
        self.api.answer = True
        token = self.meter().connect(name="Test", timeout=5)
        self.assertNotEqual("revoked-token", token)
        self.assertEqual(token, self.store.load("com.example.test")["token"])
        self.assertEqual(1, sum(1 for r in self.api.requests if r["path"] == "/v1/register"))

    def test_connect_keeps_a_token_the_app_still_knows(self) -> None:
        token = self.api.approve_token("com.example.test", ["signals"])  # no "status": 403, still a known token
        self.store.save_token("com.example.test", token)
        self.assertEqual(token, self.meter().connect(name="Test"))
        self.assertEqual(0, sum(1 for r in self.api.requests if r["path"] == "/v1/register"))

    def test_reconnecting_registers_again_and_retries_once(self) -> None:
        self.api.answer = True
        meter = self.meter()
        old = meter.connect(name="Test", timeout=5)
        del self.api.tokens[old]  # Revoke in the app
        reply = meter.reconnecting(lambda: meter.set_level(3, 30))
        self.assertEqual(["level-3"], reply["active"])
        self.assertNotEqual(old, meter.token)
        self.assertEqual(2, sum(1 for r in self.api.requests if r["path"] == "/v1/register"))

    def test_reconnecting_without_connect_just_raises(self) -> None:
        meter = self.meter(token="revoked")
        with self.assertRaises(UnauthorizedError):
            meter.reconnecting(meter.status)

    def test_cli_and_recipes_reconnect(self) -> None:
        from busymeter.cli import call

        self.api.answer = True
        meter = self.meter()
        old = meter.connect(name="Test", timeout=5)
        del self.api.tokens[old]
        self.assertEqual(["level-2"], call(meter, lambda: meter.set_level(2, 30))["active"])


class TransportTests(Base):
    def test_a_silent_app_is_not_responding_not_off(self) -> None:
        with socket.socket() as server:  # accepts the connection (backlog) but never answers
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            meter = BusyMeter("com.example.test", port=server.getsockname()[1], store=self.store, token="t", timeout=0.3)
            with self.assertRaises(AppNotRespondingError) as e:
                meter.status()
            self.assertNotIsInstance(e.exception, NotListeningError)
            self.assertIn("didn't answer in time", e.exception.message)
            self.assertFalse(meter.is_listening())

    def test_garbage_or_a_dropped_connection_is_not_responding(self) -> None:
        import threading

        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(4)

            def answer() -> None:
                try:
                    conn, _ = server.accept()
                    conn.recv(4096)
                    conn.sendall(b"NOT HTTP AT ALL\r\n\r\n")
                    conn.close()
                except OSError:
                    pass

            threading.Thread(target=answer, daemon=True).start()
            meter = BusyMeter("com.example.test", port=server.getsockname()[1], store=self.store, token="t", timeout=2)
            with self.assertRaises(AppNotRespondingError):
                meter.status()

    def test_no_discovery_file_sends_no_token(self) -> None:
        missing = Path(self.tmp.name) / "none" / "plugin-api.json"
        with mock.patch.object(busymeter.client, "DEFAULT_PORT", self.api.port):
            meter = BusyMeter("com.example.test", store=self.store, token="secret-token", discovery_file=missing)
            self.assertFalse(meter.endpoint.trusted)
            for call in (meter.status, lambda: meter.set_level(2, 30), meter.clear):
                with self.assertRaises(NotListeningError) as e:
                    call()
                self.assertEqual(NOT_RUNNING_MESSAGE, e.exception.message)
            self.assertEqual([], self.api.requests)  # nothing reached whatever holds the default port
            self.assertTrue(meter.is_listening())  # unauthenticated probes still go there


class PermissionTests(Base):
    @unittest.skipIf(sys.platform == "win32", "POSIX modes")
    def test_a_loose_existing_folder_is_tightened(self) -> None:
        folder = Path(self.tmp.name) / "loose"
        folder.mkdir(mode=0o755)
        os.chmod(folder, 0o755)
        TokenStore(folder).save_token("com.example.test", "tok")
        self.assertEqual(0o700, stat.S_IMODE(folder.stat().st_mode))

    @unittest.skipUnless(sys.platform == "win32", "Windows ACL")
    def test_an_existing_folder_is_narrowed_to_the_user_by_sid(self) -> None:
        import subprocess

        folder = Path(self.tmp.name) / "existing"
        folder.mkdir()  # inherits the temp folder's permissions
        (folder / "old.txt").write_text("x")
        sid = busymeter.tokens.current_user_sid()
        self.assertRegex(sid, r"^S-1-")
        store = TokenStore(folder)
        store.save_token("com.example.test", "tok")
        store.save_token("com.example.other", "tok2")  # a second write narrows again: earlier files must stay readable
        self.assertEqual("tok", store.load("com.example.test")["token"])
        self.assertEqual("tok2", store.load("com.example.other")["token"])
        self.assertEqual("x", (folder / "old.txt").read_text())
        user = os.environ.get("USERNAME", "")
        for path in (folder, folder / "com.example.test.json", folder / "old.txt"):
            out = subprocess.run([busymeter.tokens._system32("icacls.exe"), str(path)], capture_output=True, text=True).stdout
            # Only the user is granted. SYSTEM, Administrators and OWNER RIGHTS may stay where the folder got them
            # explicitly (Python 3.12.4+ creates temp folders that way, as on CI runners): they can read any file anyway.
            for other in ("Everyone", "BUILTIN\\Users", "Authenticated Users", "INTERACTIVE"):
                self.assertNotIn(other, out, path)
            self.assertIn(user, out, path)  # the user's own entry is there
            self.assertIn("(F)", out, path)
            if path == folder:
                self.assertNotIn("(I)", out)  # nothing inherited from above

    @unittest.skipUnless(sys.platform == "win32", "Windows ACL")
    def test_icacls_failure_warns_and_still_writes(self) -> None:
        import subprocess

        failed = subprocess.CompletedProcess([], 5, stdout="", stderr="Access is denied.")
        real_run = subprocess.run

        def fake_run(args, **kw):
            return failed if "icacls" in str(args[0]).lower() else real_run(args, **kw)

        with mock.patch.object(busymeter.tokens.subprocess, "run", side_effect=fake_run):
            store = TokenStore(Path(self.tmp.name) / "warn")
            with self.assertWarns(busymeter.tokens.PermissionWarning):
                store.save_token("com.example.test", "tok")
        self.assertEqual("tok", store.load("com.example.test")["token"])

    @unittest.skipUnless(sys.platform == "win32", "Windows ACL")
    def test_no_sid_warns(self) -> None:
        with mock.patch.object(busymeter.tokens, "current_user_sid", return_value=None):
            with self.assertWarns(busymeter.tokens.PermissionWarning):
                TokenStore(Path(self.tmp.name) / "nosid").save_token("com.example.test", "tok")


class HelperTests(unittest.TestCase):
    def test_parse_duration(self) -> None:
        self.assertEqual(600, parse_duration("10m"))
        self.assertEqual(30, parse_duration("30s"))
        self.assertEqual(30, parse_duration("30"))
        self.assertEqual(5400, parse_duration("1.5h"))
        self.assertEqual(90, parse_duration(90))
        for bad in ("", "soon", "0s", "-5m", True):
            with self.assertRaises(ValueError):
                parse_duration(bad)

    def test_normalize_level(self) -> None:
        self.assertEqual(0, normalize_level(0))
        self.assertEqual(6, normalize_level("6"))
        self.assertEqual("flash", normalize_level("Flash"))

    def test_hold_level_short_is_one_call(self) -> None:
        meter = FakeMeter()
        hold_level(meter, 5, 600)
        self.assertEqual([("level", 5, 600)], meter.calls)

    def test_hold_level_long_refreshes_then_clears(self) -> None:
        meter, now = FakeMeter(), [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        hold_level(meter, 4, 3600, sleep=sleep, clock=lambda: now[0])
        levels = [c for c in meter.calls if c[0] == "level"]
        self.assertEqual(120, len(levels))  # every 30 s for an hour
        self.assertTrue(all(c[2] <= 60 for c in levels))
        self.assertEqual(("clear",), meter.calls[-1])


class FakeMeter:
    def __init__(self) -> None:
        self.calls = []

    def set_level(self, level, ttl):
        self.calls.append(("level", level, ttl))

    def clear(self):
        self.calls.append(("clear",))


if __name__ == "__main__":
    unittest.main()
