import base64
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import types
import unittest
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]


def load_gateway():
    """Import the gateway without requiring BlueZ/PyGObject on the build host."""
    dbus = types.ModuleType("dbus")
    dbus_service = types.ModuleType("dbus.service")
    dbus_mainloop = types.ModuleType("dbus.mainloop")
    dbus_glib = types.ModuleType("dbus.mainloop.glib")
    dbus_glib.DBusGMainLoop = object
    dbus.service = dbus_service
    gi = types.ModuleType("gi")
    gi_repository = types.ModuleType("gi.repository")
    gi_repository.GLib = object()
    old = {name: sys.modules.get(name) for name in (
        "dbus", "dbus.service", "dbus.mainloop", "dbus.mainloop.glib",
        "gi", "gi.repository")}
    sys.modules.update({
        "dbus": dbus, "dbus.service": dbus_service,
        "dbus.mainloop": dbus_mainloop, "dbus.mainloop.glib": dbus_glib,
        "gi": gi, "gi.repository": gi_repository,
    })
    try:
        spec = importlib.util.spec_from_file_location(
            "bt_gateway_test", ROOT / "board/bin/bt-gateway.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


GATEWAY = load_gateway()


class FakeMihomo(BaseHTTPRequestHandler):
    mode = "rule"
    selected = "node-a"

    def log_message(self, _format, *_args):
        pass

    def json(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def no_content(self):
        self.send_response(204)
        self.end_headers()

    @classmethod
    def proxies(cls):
        return {"proxies": {
            "PROXY": {"type": "Selector", "now": cls.selected,
                      "all": ["node-a", "node-b"]},
            "node-a": {"type": "Shadowsocks", "history": [{"delay": 42}]},
            "node-b": {"type": "Vmess", "history": [{"delay": 81}]},
        }}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/configs":
            return self.json({"mode": type(self).mode})
        if parsed.path == "/proxies":
            return self.json(type(self).proxies())
        if parsed.path == "/version":
            return self.json({"version": "test-version"})
        if parsed.path.startswith("/proxies/") and parsed.path.endswith("/delay"):
            name = unquote(parsed.path[len("/proxies/"):-len("/delay")])
            return self.json({"delay": 45 if name == "node-a" else 82})
        return self.json({"error": "not found"}, 404)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_PATCH(self):
        if self.path == "/configs":
            type(self).mode = self.body()["mode"]
            return self.no_content()
        return self.json({"error": "not found"}, 404)

    def do_PUT(self):
        if self.path == "/proxies/PROXY":
            type(self).selected = self.body()["name"]
            return self.no_content()
        return self.json({"error": "not found"}, 404)


class GatewayCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMihomo)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        GATEWAY.MIHOMO_API = "http://127.0.0.1:%d" % cls.server.server_port
        GATEWAY.MIHOMO_SECRET_FILE = ""
        GATEWAY.MIHOMO_GROUP = "PROXY"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        FakeMihomo.mode = "rule"
        FakeMihomo.selected = "node-a"

    def test_proxy_status_switch_mode_and_delay(self):
        status = GATEWAY.cmd_proxy_status(None, {})
        self.assertTrue(status["configured"])
        self.assertTrue(status["reachable"])
        self.assertEqual(status["current"], "node-a")
        self.assertEqual(status["nodes"][0]["delay"], 42)
        self.assertEqual(status["version"], "test-version")

        selected = GATEWAY.cmd_proxy_switch(None, {"name": "node-b"})
        self.assertEqual(selected["name"], "node-b")
        self.assertEqual(FakeMihomo.selected, "node-b")
        GATEWAY.cmd_proxy_mode(None, {"mode": "global"})
        self.assertEqual(FakeMihomo.mode, "global")
        GATEWAY.cmd_proxy_onoff(None, {"on": False})
        self.assertEqual(FakeMihomo.mode, "direct")
        self.assertEqual(GATEWAY.cmd_proxy_delay(
            None, {"name": "node-b", "timeout_ms": 1000})["delay"], 82)

    def test_proxy_status_refreshes_delays(self):
        status = GATEWAY.cmd_proxy_status(None, {"refresh": True})
        self.assertEqual([node["delay"] for node in status["nodes"]], [45, 82])

    def test_term_exec_and_caps(self):
        caps = GATEWAY.cmd_term_caps(None, {})
        self.assertEqual(caps["exec"]["name"], "term.exec")
        result = GATEWAY.cmd_term_exec(None, {"command": "printf hello"})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(base64.b64decode(result["output_b64"]), b"hello")
        self.assertFalse(result["truncated"])

    def test_term_exec_bounds_output_and_timeout(self):
        result = GATEWAY.cmd_term_exec(None, {
            "command": "head -c 4096 /dev/zero", "max_output_bytes": 512})
        self.assertEqual(len(base64.b64decode(result["output_b64"])), 512)
        self.assertTrue(result["truncated"])
        timed = GATEWAY.cmd_term_exec(None, {"command": "sleep 2", "timeout_s": 0.1})
        self.assertTrue(timed["timed_out"])
        self.assertNotEqual(timed["exit_code"], 0)

    def test_term_exec_rejects_invalid_commands(self):
        for command in ("", "bad\0command"):
            with self.assertRaises(GATEWAY.CmdError) as caught:
                GATEWAY.cmd_term_exec(None, {"command": command})
            self.assertEqual(caught.exception.code, "E_ARGS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
