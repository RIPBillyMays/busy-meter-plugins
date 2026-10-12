"""Drives a real Busy Meter plugin host with the Python client and the MCP tools, once.

Run by the Busy Meter app's own end-to-end tests, which start the real loopback host with a test
approver that clicks Allow, and points this script at it with BUSYMETER_DISCOVERY_FILE and BUSYMETER_TOKEN_DIR.
Prints one JSON object with what each call returned. ``off`` mode checks the messages when nothing listens.
"""

import json
import sys

from busymeter import BusyMeter, BusyMeterError
from busymeter_mcp.tools import Tools


def on() -> dict:
    results: dict = {}
    meter = BusyMeter("com.example.e2e")
    results["endpoint"] = {"port": meter.endpoint.port, "fromFile": meter.endpoint.from_file}
    results["listening"] = meter.is_listening()
    token = meter.connect(name="End-to-end test", signals=[{"name": "deep-work", "label": "Deep work", "suggest": 2}],
                          wants=["signals", "status", "effects"], timeout=20)
    results["tokenLength"] = len(token)
    results["savedToken"] = meter.store.load("com.example.e2e").get("token") == token
    results["setLevel"] = meter.set_level(5, 60)
    results["status"] = meter.status()
    results["setSignals"] = meter.set_signals(["deep-work"], 30)
    try:
        meter.set_signals(["not-declared"], 30)
    except BusyMeterError as e:
        results["unknownSignal"] = {"status": e.status, "code": e.code}
    results["alert"] = meter.alert("lip-pulse", "#F0C850", 200)
    results["clear"] = meter.clear()

    tools = Tools(plugin_id="com.example.e2e-mcp")
    results["mcpRegister"] = tools.register_plugin(name="End-to-end MCP", wait_seconds=20)
    results["mcpLevel"] = tools.set_level("flash", "30s")
    results["mcpStatus"] = tools.get_status()
    results["mcpSources"] = tools.list_sources()
    results["mcpClear"] = tools.clear()
    return results


def withdraw() -> dict:
    """Register while nobody answers the prompt, then take it back."""
    meter = BusyMeter("com.example.e2e-withdraw")
    results: dict = {"requestId": meter.register(name="Withdraw test")}
    results["withdraw"] = meter.withdraw()
    results["pendingAfter"] = meter.pending_request()
    try:
        meter.poll(results["requestId"])
    except BusyMeterError as e:
        results["pollAfter"] = {"status": e.status, "code": e.code}
    return results


def off() -> dict:
    meter = BusyMeter("com.example.e2e", timeout=3)
    results: dict = {"listening": meter.is_listening()}
    try:
        meter.set_level(3, 30)
    except BusyMeterError as e:
        results["setLevel"] = {"code": e.code, "message": e.message}
    results["mcpStatus"] = Tools(plugin_id="com.example.e2e-mcp").get_status()
    return results


if __name__ == "__main__":
    if not BusyMeter("com.example.e2e").endpoint.from_file:  # never fall back to the default port of a real app
        sys.exit("BUSYMETER_DISCOVERY_FILE must name the test host's discovery file")
    mode = sys.argv[1] if sys.argv[1:] else "on"
    print(json.dumps({"on": on, "off": off, "withdraw": withdraw}[mode]()))
