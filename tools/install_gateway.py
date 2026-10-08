#!/usr/bin/env python3
"""Debian/systemd installer. --destdir stages files without touching the host OS."""
import argparse
import datetime
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "LICENSE": ("usr/share/doc/bt-remote/LICENSE", 0o644),
    "NOTICE": ("usr/share/doc/bt-remote/NOTICE", 0o644),
    "THIRD_PARTY_NOTICES.md": ("usr/share/doc/bt-remote/THIRD_PARTY_NOTICES.md", 0o644),
    "board/bin/bt-gateway.py": ("usr/local/bin/bt-gateway", 0o755),
    "board/bin/bt-pair.sh": ("usr/local/bin/bt-pair", 0o755),
    "board/bin/bt-connectable.sh": ("usr/local/bin/bt-connectable", 0o755),
    "tools/manage_gateway.py": ("usr/local/bin/bt-remote", 0o755),
    "board/etc/wifi-guard.sh": ("etc/bt-gateway/wifi-guard.sh", 0o700),
    "board/systemd/bt-gateway.service": ("etc/systemd/system/bt-gateway.service", 0o644),
    "board/systemd/bt-connectable.service": ("etc/systemd/system/bt-connectable.service", 0o644),
    "board/systemd/bt-connectable.timer": ("etc/systemd/system/bt-connectable.timer", 0o644),
}
PACKAGES = ["python3", "python3-dbus", "python3-gi", "python3-yaml", "bluez",
            "iproute2", "iw", "iputils-ping", "util-linux", "netplan.io"]


def run(argv, timeout=120):
    print("+", " ".join(argv), flush=True)
    subprocess.run(argv, check=True, timeout=timeout)


def target(prefix, relative):
    result = prefix / relative
    if not result.resolve().is_relative_to(prefix.resolve()):
        raise RuntimeError("Destination escapes installation root: " + str(result))
    if result.is_symlink():
        raise RuntimeError("Refusing to overwrite symlink: " + str(result))
    return result


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bt-remote-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_token(path):
    if path.is_symlink():
        raise RuntimeError("Token must not be a symlink")
    if path.exists():
        if not path.read_bytes().strip():
            raise RuntimeError("Existing token is empty; refusing to replace credentials silently")
        os.chmod(path, 0o600)
        return
    # Credentials are generated only on the target board, never in a staged package.
    atomic_write(path, (secrets.token_hex(32) + "\n").encode(), 0o600)


def install(prefix, live):
    config = target(prefix, "etc/bt-gateway/gateway.env")
    token = target(prefix, "etc/bt-gateway/token")
    # Validate every destination before making changes.
    destinations = {src: target(prefix, dest) for src, (dest, _) in FILES.items()}
    config.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(config.parent, 0o700)
    if live:
        ensure_token(token)
    if not config.exists():
        atomic_write(config, (ROOT / "board/etc/gateway.env.example").read_bytes(), 0o600)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    backup = target(prefix, "etc/bt-gateway/install-backups/" + stamp)
    for src, dest in destinations.items():
        data = (ROOT / src).read_bytes().replace(b"\r\n", b"\n")
        mode = FILES[src][1]
        if dest.exists() and dest.read_bytes() == data:
            os.chmod(dest, mode)
            continue
        if dest.exists():
            saved = backup / FILES[src][0]
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, saved)
        atomic_write(dest, data, mode)
    if backup.exists():
        print("Previous installed files saved in:", backup)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print plan; no writes, packages or services")
    parser.add_argument("--no-deps", action="store_true", help="dependencies already installed (offline update)")
    parser.add_argument("--destdir", type=Path, help="stage files under this directory; no token or system changes")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 9):
        parser.error("Python 3.9+ required")
    prefix = args.destdir.resolve() if args.destdir else Path("/")
    if args.destdir and prefix == Path(prefix.anchor):
        parser.error("--destdir must not be the filesystem root")
    for name in [*FILES, "board/etc/gateway.env.example"]:
        if not (ROOT / name).is_file():
            raise RuntimeError("Incomplete checkout: missing " + name)
    for name in FILES:
        if name.endswith(".py"):
            compile((ROOT / name).read_text(encoding="utf-8"), name, "exec")
    if args.dry_run:
        print("STAGING" if args.destdir else "LIVE INSTALL (Debian/systemd, root required)")
        for src, (dest, _) in FILES.items():
            print(src, "->", prefix / dest)
        print("Preserve token/config. Live install: generate missing token; enable gateway + timer.")
        return 0
    if args.destdir:
        install(prefix, live=False)
        print("Staged files only. No credentials generated; no host services modified.")
        return 0
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("Run on the Linux board using sudo bash install.sh")
    if not Path("/run/systemd/system").is_dir():
        raise RuntimeError("systemd must be the running init system")
    import fcntl
    # Retain this file object for the lifetime of main; child commands do not inherit it.
    install_lock = open("/run/bt-remote-install.lock", "w")
    try:
        fcntl.flock(install_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        install_lock.close()
        raise RuntimeError("Another gateway installation is already running")
    if Path("/run/bt-gateway/wifi-switch-flag").exists():
        raise RuntimeError("WiFi switch/rollback pending; finish recovery before upgrading")
    for legacy in ("bt-ctl.service", "bt-agent.service"):
        if subprocess.run(["systemctl", "is-active", "--quiet", legacy], timeout=10).returncode == 0:
            raise RuntimeError("Conflicting legacy unit: " + legacy + "; review and disable it before installation")
    if not args.no_deps:
        if not shutil.which("apt-get"):
            raise RuntimeError("Only apt-based systems supported; install dependencies manually and use --no-deps")
        run(["apt-get", "update"], timeout=600)
        run(["apt-get", "install", "-y", *PACKAGES], timeout=1200)
    run(["/usr/bin/python3", "-c", "import dbus, gi, yaml; from gi.repository import GLib"])
    for command in ("busctl", "bluetoothctl", "btmgmt", "hciconfig", "ip", "iw", "ping", "flock", "timeout", "netplan"):
        if not shutil.which(command):
            raise RuntimeError("Missing dependency: " + command)
    install(prefix, live=True)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "bluetooth.service"])
    run(["systemctl", "enable", "bt-gateway.service", "bt-connectable.timer"])
    run(["systemctl", "restart", "bt-connectable.timer"])
    run(["systemctl", "restart", "bt-gateway.service"], timeout=80)
    print("Installed; enabled at boot. Open pairing: sudo bt-remote pair on 300")
    print("Read your private token locally: sudo bt-remote token (do not publish it)")
    print("Health check: sudo bt-remote doctor")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("INSTALL FAILED:", error, file=sys.stderr)
        print("On the board: journalctl -u bt-gateway -n 60 --no-pager. Fix the cause and rerun.", file=sys.stderr)
        sys.exit(1)
