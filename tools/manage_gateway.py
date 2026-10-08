#!/usr/bin/env python3
"""Installed as bt-remote: manage the gateway without depending on the source checkout."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

UNITS = ["bt-gateway.service", "bt-connectable.timer"]
REMOVE = ["/usr/local/bin/bt-gateway", "/usr/local/bin/bt-pair", "/usr/local/bin/bt-connectable",
          "/usr/share/doc/bt-remote/LICENSE", "/usr/share/doc/bt-remote/NOTICE",
          "/usr/share/doc/bt-remote/THIRD_PARTY_NOTICES.md",
          "/usr/local/bin/bt-remote", "/etc/systemd/system/bt-gateway.service",
          "/etc/systemd/system/bt-connectable.service", "/etc/systemd/system/bt-connectable.timer"]


def execute(args, check=True, timeout=90):
    return subprocess.run(args, check=check, timeout=timeout).returncode


def require_root():
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Run this command with sudo on the board")


def healthcheck():
    token = Path("/etc/bt-gateway/token").read_text().strip()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect("/run/bt-gateway.sock")
        with sock.makefile("rb") as reader:
            def read():
                data = reader.readline(4098)
                if len(data) > 4097 or not data.endswith(b"\n"):
                    raise RuntimeError("Invalid local gateway response")
                return json.loads(data)
            if read().get("t") != "hello":
                raise RuntimeError("Local gateway hello missing")
            sock.sendall((json.dumps({"t": "auth", "token": token}) + "\n").encode())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = read()
                if msg.get("t") == "auth":
                    if msg.get("ok") is not True:
                        raise RuntimeError("Local gateway authentication failed")
                    return
            raise RuntimeError("Authentication timeout")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "logs", "doctor", "pair", "token", "uninstall"])
    parser.add_argument("args", nargs="*")
    options = parser.parse_args(argv)
    action = options.action
    if action != "pair" and options.args:
        parser.error("Unexpected arguments")
    if action in ("start", "stop", "restart"):
        require_root()
        # Stop the timer first so it cannot relaunch a oneshot during shutdown.
        if action == "stop":
            execute(["systemctl", "stop", "bt-connectable.timer", "bt-connectable.service", "bt-gateway.service"])
        else:
            execute(["systemctl", action, *UNITS])
    elif action == "status":
        return execute(["systemctl", "status", "--no-pager", *UNITS], check=False)
    elif action == "logs":
        return execute(["journalctl", "-u", "bt-gateway", "-u", "bt-connectable", "-n", "100", "-f"], check=False, timeout=None)
    elif action == "pair":
        require_root()
        return execute(["/usr/local/bin/bt-pair", *options.args], check=False)
    elif action == "token":
        require_root()
        print(Path("/etc/bt-gateway/token").read_text().strip())
    elif action == "doctor":
        failures = []
        for unit in ("bluetooth.service", *UNITS):
            active = execute(["systemctl", "is-active", "--quiet", unit], check=False, timeout=10) == 0
            enabled = execute(["systemctl", "is-enabled", "--quiet", unit], check=False, timeout=10) == 0
            print(unit, "active=" + str(active), "enabled=" + str(enabled))
            if not active or not enabled: failures.append(unit)
        if not Path("/sys/class/bluetooth/hci0").exists():
            failures.append("hci0 missing: check Bluetooth driver/firmware")
        if Path("/run/bt-gateway/wifi-switch-flag").exists():
            failures.append("WiFi rollback pending")
        require_root()
        try:
            healthcheck()
            print("Local authenticated gateway probe: OK (token not displayed)")
        except (OSError, RuntimeError, ValueError) as error:
            failures.append(str(error))
        for failure in failures: print("FAIL:", failure)
        return 1 if failures else 0
    elif action == "uninstall":
        require_root()
        if Path("/run/bt-gateway/wifi-switch-flag").exists():
            raise RuntimeError("Finish pending WiFi rollback before uninstalling")
        execute(["systemctl", "disable", "--now", *UNITS])
        execute(["systemctl", "stop", "bt-connectable.service"], check=False)
        for name in REMOVE:
            path = Path(name)
            if path.is_file() or path.is_symlink(): path.unlink()
        execute(["systemctl", "daemon-reload"])
        print("Removed gateway binaries/units. Preserved /etc/bt-gateway, WiFi, Bluetooth pairing and BlueZ.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("ERROR:", error, file=sys.stderr)
        sys.exit(1)
