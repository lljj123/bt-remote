import ast
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import install_gateway as installer


class InstallTests(unittest.TestCase):
    def test_dry_run_makes_no_changes_or_service_calls(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "unused"
            with patch.object(installer, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(installer.main(["--dry-run", "--destdir", str(target)]), 0)
            run.assert_not_called()
            self.assertFalse(target.exists())

    def test_staging_is_complete_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            root = Path(d)
            installer.install(root, live=False)
            config = root / "etc/bt-gateway/gateway.env"
            config.write_text("BTG_WLAN=custom0\n")
            token = root / "etc/bt-gateway/token"
            self.assertFalse(token.exists())
            token.write_text("private-test-fixture\n")
            binary = root / "usr/local/bin/bt-gateway"
            original = binary.stat().st_mtime_ns
            installer.install(root, live=False)
            self.assertEqual(binary.stat().st_mtime_ns, original)
            self.assertEqual(config.read_text(), "BTG_WLAN=custom0\n")
            self.assertEqual(token.read_text(), "private-test-fixture\n")
            for _, (name, _) in installer.FILES.items():
                self.assertTrue((root / name).is_file(), name)
            self.assertFalse((root / "etc/bt-gateway/install-backups").exists())

    def test_upgrade_backs_up_changed_file(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            root = Path(d)
            installer.install(root, live=False)
            binary = root / "usr/local/bin/bt-gateway"
            binary.write_text("old-version")
            installer.install(root, live=False)
            backups = list((root / "etc/bt-gateway/install-backups").rglob("bt-gateway"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "old-version")

    def test_token_generated_once_and_empty_existing_token_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "token"
            installer.ensure_token(p)
            token = p.read_text().strip()
            self.assertEqual(len(bytes.fromhex(token)), 32)
            installer.ensure_token(p)
            self.assertEqual(token, p.read_text().strip())
            p.write_text("")
            with self.assertRaises(RuntimeError): installer.ensure_token(p)

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError): installer.target(Path(d), "../outside")

    def test_root_destdir_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            installer.main(["--destdir", str(Path.cwd().anchor), "--dry-run"])


class GatewayRegressionTests(unittest.TestCase):
    def definitions(self, names):
        source = installer.ROOT / "board/bin/bt-gateway.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        ns = {"json": json, "logging": logging, "os": os, "socket": socket, "MAX_LINE": 4096, "log": Mock()}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in names:
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)
        return ns

    def test_oversized_single_log_terminates(self):
        ns = self.definitions({"jdump", "_shrink"})
        data, wire = ns["_shrink"]({"data": {"lines": ["x" * 6000]}})
        self.assertLessEqual(len(wire), 4096)
        self.assertTrue(data["data"]["truncated"])

    def test_systemd_notify_sends_abstract_socket_datagram(self):
        ns = self.definitions({"systemd_notify"})
        notifier = MagicMock()
        notifier.__enter__.return_value = notifier
        with patch.object(socket, "AF_UNIX", 1, create=True), patch.dict(os.environ, {"NOTIFY_SOCKET": "@gateway-test"}), patch.object(socket, "socket", return_value=notifier):
            self.assertTrue(ns["systemd_notify"]("READY=1\nWATCHDOG=1"))
        notifier.sendto.assert_called_once_with(b"READY=1\nWATCHDOG=1", "\0gateway-test")

    def test_notify_failure_is_not_success(self):
        ns = self.definitions({"systemd_notify"})
        with patch.object(socket, "AF_UNIX", 1, create=True), patch.dict(os.environ, {"NOTIFY_SOCKET": "/missing"}), patch.object(socket, "socket", side_effect=OSError("unavailable")):
            self.assertFalse(ns["systemd_notify"]("WATCHDOG=1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
