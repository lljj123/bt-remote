import base64
import hashlib
import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import tempfile
import types
import unittest
from unittest import mock
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
    reloaded_payload = ""

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
        parsed = urlparse(self.path)
        if parsed.path == "/configs":
            type(self).reloaded_payload = self.body().get("payload", "")
            return self.no_content()
        if parsed.path == "/proxies/PROXY":
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
        FakeMihomo.reloaded_payload = ""

    def test_storage_inventory_lists_internal_flash_and_usb(self):
        tree = [{
            "name": "mmcblk2", "path": "/dev/mmcblk2", "type": "disk",
            "size": 8000000000, "model": "8GB eMMC", "rm": False,
            "children": [{"name": "mmcblk2p1", "path": "/dev/mmcblk2p1",
                          "type": "part", "size": 7900000000, "fstype": "ext4",
                          "label": "EMMC", "mountpoints": [None]}]
        }, {
            "name": "sda", "path": "/dev/sda", "type": "disk",
            "size": 64000000000, "model": "USB Disk", "rm": True, "tran": "usb",
            "children": [{"name": "sda1", "path": "/dev/sda1", "type": "part",
                          "size": 63900000000, "fstype": "exfat", "label": "U盘",
                          "mountpoints": ["/media/usb"]}]
        }]
        with mock.patch.object(GATEWAY, "_storage_lsblk", return_value=tree), \
                mock.patch.object(GATEWAY, "_storage_space", return_value=(100, 40, 60)):
            rows = GATEWAY.storage_rows()
        by_path = {row["path"]: row for row in rows}
        self.assertEqual(by_path["/dev/mmcblk2"]["size_bytes"], 8000000000)
        self.assertTrue(by_path["/dev/mmcblk2p1"]["can_mount"])
        self.assertTrue(by_path["/dev/sda1"]["external"])
        self.assertEqual(by_path["/dev/sda1"]["mountpoint"], "/media/usb")

    def test_storage_lsblk_falls_back_for_util_linux_234(self):
        fallback = {"blockdevices": [{"name": "sda", "path": "/dev/sda",
                                      "type": "disk", "size": 1000}]}
        with mock.patch.object(GATEWAY, "run", side_effect=[
                (1, "", "unknown column MOUNTPOINTS"),
                (0, json.dumps(fallback), "")]) as runner:
            rows = GATEWAY._storage_lsblk()
        self.assertEqual(rows[0]["path"], "/dev/sda")
        self.assertIn("MOUNTPOINTS", runner.call_args_list[0].args[0][-1])
        self.assertIn("MOUNTPOINT", runner.call_args_list[1].args[0][-1])

    def test_storage_mount_accepts_only_discovered_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            row = {"path": "/dev/sdz1", "name": "sdz1", "mountpoint": "",
                   "can_mount": True}
            with mock.patch.object(GATEWAY, "STORAGE_MOUNT_ROOT", directory), \
                    mock.patch.object(GATEWAY, "storage_rows", return_value=[row]), \
                    mock.patch.object(GATEWAY, "run", return_value=(0, "", "")) as runner:
                result = GATEWAY.cmd_storage_mount(None, {"path": "/dev/sdz1"})
            self.assertTrue(result["mounted"])
            self.assertTrue(result["mountpoint"].startswith(directory + os.sep))
            runner.assert_called_once()
            with mock.patch.object(GATEWAY, "storage_rows", return_value=[row]):
                with self.assertRaises(GATEWAY.CmdError):
                    GATEWAY.cmd_storage_mount(None, {"path": "/dev/not-present"})

    def test_proxy_subscription_save_list_and_apply(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            config = root / "config.yaml"
            backup = root / "gateway"
            config.write_text("proxy-groups:\n  - name: PROXY\n    type: select\n    proxies: [DIRECT]\n")
            patches = [
                mock.patch.object(GATEWAY, "STATE_FILE", str(state)),
                mock.patch.object(GATEWAY, "BACKUP_ROOT", str(backup)),
                mock.patch.object(GATEWAY, "MIHOMO_CONFIG", str(config)),
                mock.patch.object(GATEWAY, "MIHOMO_GROUP", "PROXY"),
            ]
            with patches[0], patches[1], patches[2], patches[3]:
                saved = GATEWAY.cmd_proxy_sub_save(None, {
                    "name": "机场 A", "url": "https://sub.example/path?token=secret-token"})
                listed = GATEWAY.cmd_proxy_sub_list(None, {})
                self.assertNotIn("secret-token", json.dumps(listed, ensure_ascii=False))
                applied = GATEWAY.cmd_proxy_sub_apply(None, {"id": saved["id"]})
                data = yaml.safe_load(config.read_text())
                state_data = json.loads(state.read_text())
            self.assertTrue(applied["applied"])
            provider = data["proxy-providers"][GATEWAY.MIHOMO_PROVIDER]
            self.assertEqual(provider["url"], "https://sub.example/path?token=secret-token")
            self.assertIn(GATEWAY.MIHOMO_PROVIDER, data["proxy-groups"][0]["use"])
            self.assertEqual(state_data["proxy_subscriptions"]["active_id"], saved["id"])
            self.assertIn("proxy-providers", FakeMihomo.reloaded_payload)

    def test_service_discovery_and_paginated_listing(self):
        discovered = ["bt-gateway.service", "bluetooth.service", "new-worker.service"]
        props = {
            name: {"Description": name + " description", "ActiveState": "active",
                   "UnitFileState": "enabled", "MemoryCurrent": "4096"}
            for name in discovered
        }
        with mock.patch.object(GATEWAY, "service_units", return_value=discovered), \
                mock.patch.object(GATEWAY, "unit_props", return_value=props):
            first = GATEWAY.cmd_svc_list(None, {"offset": 0, "limit": 2, "refresh": True})
            second = GATEWAY.cmd_svc_list(None, {"offset": 2, "limit": 2})
        self.assertEqual(first["total"], 3)
        self.assertFalse(first["eof"])
        self.assertEqual(first["next_offset"], 2)
        self.assertTrue(first["services"][0]["managed"])
        self.assertEqual(second["services"][0]["unit"], "new-worker.service")
        self.assertFalse(second["services"][0]["managed"])
        self.assertTrue(second["eof"])

    def test_service_unit_discovery_reads_installed_and_loaded_units(self):
        GATEWAY._svc_units_cache = (0.0, [])
        outputs = [
            (0, "new-worker.service enabled enabled\n", ""),
            (0, "● failed-extra.service loaded failed failed Example\n", ""),
        ]
        with mock.patch.object(GATEWAY, "run", side_effect=outputs):
            units = GATEWAY.service_units(refresh=True)
        self.assertIn("new-worker.service", units)
        self.assertIn("failed-extra.service", units)
        self.assertTrue(set(GATEWAY.SVC_WHITELIST).issubset(units))

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

    def test_external_wifi_interface_can_be_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            net_root = Path(directory) / "net"
            adapter = net_root / "wlx001122334455"
            (adapter / "wireless").mkdir(parents=True)
            with mock.patch.object(GATEWAY, "SYS_CLASS_NET", str(net_root)), \
                    mock.patch.object(GATEWAY, "run", return_value=(0, "", "")), \
                    mock.patch.object(GATEWAY, "wifi_link", return_value={
                        "ssid": "USB-NET", "bssid": "00:11:22:33:44:55",
                        "signal_dbm": -48, "freq": 5180}), \
                    mock.patch.object(GATEWAY, "iface_ipv4", return_value="10.0.0.8"), \
                    mock.patch.object(GATEWAY, "default_gateway", return_value="10.0.0.1"), \
                    mock.patch.object(GATEWAY, "netplan_dns", return_value=["1.1.1.1"]):
                self.assertEqual(GATEWAY.wifi_iface_arg({"iface": adapter.name}), adapter.name)
                status = GATEWAY.cmd_net_wifi_status(None, {"iface": adapter.name})
            self.assertEqual(status["iface"], adapter.name)
            self.assertEqual(status["ssid"], "USB-NET")
            self.assertEqual(status["ip"], "10.0.0.8")

    def test_netplan_targets_selected_wifi_without_replacing_onboard(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "wifi.yaml"
            config.write_text(
                "network:\n  version: 2\n  wifis:\n    wlan0:\n"
                "      dhcp4: true\n      access-points:\n        onboard:\n"
                "          password: old-password\n")
            with mock.patch.object(GATEWAY, "NETPLAN_WIFI", str(config)):
                GATEWAY._netplan_set_ap("wlx001122334455", "USB-NET", "new-password")
            data = yaml.safe_load(config.read_text())
            wifis = data["network"]["wifis"]
            self.assertIn("onboard", wifis["wlan0"]["access-points"])
            self.assertEqual(
                wifis["wlx001122334455"]["access-points"]["USB-NET"]["password"],
                "new-password")
            self.assertTrue(wifis["wlx001122334455"]["dhcp4"])

    def test_open_network_connect_uses_selected_external_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            net_root = root / "net"
            iface = "wlx001122334455"
            (net_root / iface / "wireless").mkdir(parents=True)
            config = root / "wifi.yaml"
            config.write_text("network:\n  version: 2\n  wifis: {}\n")
            with mock.patch.object(GATEWAY, "SYS_CLASS_NET", str(net_root)), \
                    mock.patch.object(GATEWAY, "NETPLAN_WIFI", str(config)), \
                    mock.patch.object(GATEWAY, "BACKUP_ROOT", str(root)), \
                    mock.patch.object(GATEWAY, "GUARD_FLAG", str(root / "guard")), \
                    mock.patch.object(GATEWAY, "wireless_ifaces", return_value=[iface]), \
                    mock.patch.object(GATEWAY, "iface_ipv4", return_value=None), \
                    mock.patch.object(GATEWAY, "default_gateway", return_value=None), \
                    mock.patch.object(GATEWAY, "_guard_arm"), \
                    mock.patch.object(GATEWAY, "_guard_disarm"), \
                    mock.patch.object(GATEWAY, "_netplan_set_ap") as set_ap, \
                    mock.patch.object(GATEWAY, "_wifi_ok", return_value=(True, "10.0.0.9")), \
                    mock.patch.object(GATEWAY, "run", return_value=(0, "", "")):
                result = GATEWAY.cmd_net_wifi_connect(None, {
                    "iface": iface, "ssid": "Cafe", "psk": "", "timeout_s": 10})
            set_ap.assert_called_once_with(iface, "Cafe", None)
            self.assertTrue(result["ok"])
            self.assertEqual(result["iface"], iface)

    def test_usb_device_inventory_includes_bound_wifi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usb_root = root / "usb"
            net_root = root / "net"
            block_root = root / "block"
            tty_root = root / "tty"
            driver_root = root / "drivers" / "rtl8xxxu"
            device = usb_root / "1-2"
            interface_real = device / "interfaces" / "1-2:1.0"
            interface_real.mkdir(parents=True)
            driver_root.mkdir(parents=True)
            net_root.mkdir()
            block_root.mkdir()
            tty_root.mkdir()
            for name, value in {
                "idVendor": "0bda", "idProduct": "8179", "manufacturer": "Realtek",
                "product": "802.11n WLAN Adapter", "busnum": "1", "devnum": "4",
                "speed": "480", "version": "2.00", "bDeviceClass": "00",
                "authorized": "1",
            }.items():
                (device / name).write_text(value)
            (interface_real / "bInterfaceClass").write_text("ff")
            (interface_real / "driver").symlink_to(driver_root, target_is_directory=True)
            (usb_root / "1-2:1.0").symlink_to(interface_real, target_is_directory=True)
            net_adapter = net_root / "wlx001122334455"
            net_adapter.mkdir()
            (net_adapter / "device").symlink_to(interface_real, target_is_directory=True)

            with mock.patch.object(GATEWAY, "SYS_USB_DEVICES", str(usb_root)), \
                    mock.patch.object(GATEWAY, "SYS_CLASS_NET", str(net_root)), \
                    mock.patch.object(GATEWAY, "SYS_CLASS_BLOCK", str(block_root)), \
                    mock.patch.object(GATEWAY, "SYS_CLASS_TTY", str(tty_root)):
                result = GATEWAY.cmd_device_usb_list(None, {"offset": 0, "limit": 8})
            self.assertEqual(result["count"], 1)
            item = result["devices"][0]
            self.assertEqual(item["vendor_id"], "0bda")
            self.assertEqual(item["kind"], "network")
            self.assertEqual(item["drivers"], ["rtl8xxxu"])
            self.assertEqual(item["net_ifaces"], ["wlx001122334455"])

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

    def test_file_browse_read_hash_and_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "hello.txt"
            first.write_bytes(b"hello file manager")
            (Path(directory) / "folder").mkdir()

            listed = GATEWAY.cmd_file_list(None, {"path": directory})
            self.assertEqual([row["name"] for row in listed["entries"]],
                             ["folder", "hello.txt"])
            self.assertTrue(listed["eof"])

            read = GATEWAY.cmd_file_read(None, {
                "path": str(first), "offset": 6, "length": 4})
            self.assertEqual(base64.b64decode(read["data_b64"]), b"file")
            digest = GATEWAY.cmd_file_hash(None, {"path": str(first)})
            self.assertEqual(digest["sha256"], hashlib.sha256(first.read_bytes()).hexdigest())

            created = Path(directory) / "created"
            renamed = Path(directory) / "renamed"
            GATEWAY.cmd_file_mkdir(None, {"path": str(created)})
            GATEWAY.cmd_file_rename(None, {"path": str(created), "target": str(renamed)})
            GATEWAY.cmd_file_remove(None, {"path": str(renamed), "confirm": "DELETE"})
            self.assertFalse(renamed.exists())

    def test_file_upload_is_atomic_and_hash_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            session = types.SimpleNamespace(file_uploads={})
            target = Path(directory) / "upload.bin"
            payload = os.urandom(GATEWAY.FILE_CHUNK + 17)
            begun = GATEWAY.cmd_file_upload_begin(session, {
                "path": str(target), "size": len(payload), "overwrite": False})
            upload_id = begun["upload_id"]
            offset = 0
            for start in range(0, len(payload), GATEWAY.FILE_CHUNK):
                chunk = payload[start:start + GATEWAY.FILE_CHUNK]
                result = GATEWAY.cmd_file_upload_chunk(session, {
                    "upload_id": upload_id, "offset": offset,
                    "data_b64": base64.b64encode(chunk).decode()})
                offset = result["offset"]
            self.assertFalse(target.exists())
            committed = GATEWAY.cmd_file_upload_commit(session, {
                "upload_id": upload_id, "sha256": hashlib.sha256(payload).hexdigest()})
            self.assertTrue(committed["committed"])
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(session.file_uploads)

            bad = GATEWAY.cmd_file_upload_begin(session, {
                "path": str(target), "size": 3, "overwrite": True})
            GATEWAY.cmd_file_upload_chunk(session, {
                "upload_id": bad["upload_id"], "offset": 0,
                "data_b64": base64.b64encode(b"bad").decode()})
            with self.assertRaises(GATEWAY.CmdError) as caught:
                GATEWAY.cmd_file_upload_commit(session, {
                    "upload_id": bad["upload_id"], "sha256": "0" * 64})
            self.assertEqual(caught.exception.code, "E_FILE_HASH")
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(session.file_uploads)

    def test_file_upload_commit_tolerates_fat_without_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            session = types.SimpleNamespace(file_uploads={})
            target = Path(directory) / "usb-file.bin"
            begun = GATEWAY.cmd_file_upload_begin(session, {
                "path": str(target), "size": 3, "overwrite": False})
            GATEWAY.cmd_file_upload_chunk(session, {
                "upload_id": begun["upload_id"], "offset": 0,
                "data_b64": base64.b64encode(b"usb").decode()})
            with mock.patch.object(GATEWAY.os, "chmod", side_effect=OSError(1, "not supported")):
                committed = GATEWAY.cmd_file_upload_commit(session, {
                    "upload_id": begun["upload_id"],
                    "sha256": hashlib.sha256(b"usb").hexdigest()})
            self.assertTrue(committed["committed"])
            self.assertEqual(target.read_bytes(), b"usb")


if __name__ == "__main__":
    unittest.main(verbosity=2)
