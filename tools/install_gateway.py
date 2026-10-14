#!/usr/bin/env python3
"""Debian/systemd installer with versioned releases, health checks and rollback."""
import argparse
import datetime
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RELEASE_FILES = {
    "board/bin/bt-gateway.py": ("bin/bt-gateway", 0o755),
    "board/bin/bt-pair.sh": ("bin/bt-pair", 0o755),
    "board/bin/bt-connectable.sh": ("bin/bt-connectable", 0o755),
    "board/bin/bt-mgmt-mode.py": ("bin/bt-mgmt-mode.py", 0o755),
    "tools/manage_gateway.py": ("bin/bt-remote", 0o755),
    "tools/apply_update.py": ("bin/bt-remote-apply-update", 0o755),
}
SYSTEM_FILES = {
    "LICENSE": ("usr/share/doc/bt-remote/LICENSE", 0o644),
    "NOTICE": ("usr/share/doc/bt-remote/NOTICE", 0o644),
    "THIRD_PARTY_NOTICES.md": ("usr/share/doc/bt-remote/THIRD_PARTY_NOTICES.md", 0o644),
    "board/etc/wifi-guard.sh": ("etc/bt-gateway/wifi-guard.sh", 0o700),
    "board/systemd/bt-gateway.service": ("etc/systemd/system/bt-gateway.service", 0o644),
    "board/systemd/bt-connectable.service": ("etc/systemd/system/bt-connectable.service", 0o644),
    "board/systemd/bt-connectable.timer": ("etc/systemd/system/bt-connectable.timer", 0o644),
}
# Kept for release tooling and callers which inspect the complete source set.
FILES = dict(RELEASE_FILES)
FILES.update(SYSTEM_FILES)
COMMAND_LINKS = {
    "usr/local/bin/bt-gateway": "bin/bt-gateway",
    "usr/local/bin/bt-pair": "bin/bt-pair",
    "usr/local/bin/bt-connectable": "bin/bt-connectable",
    "usr/local/bin/bt-remote": "bin/bt-remote",
    "usr/local/bin/bt-remote-apply-update": "bin/bt-remote-apply-update",
}
PACKAGES = ["python3", "python3-cryptography", "python3-dbus", "python3-gi", "python3-yaml", "bluez",
            "iproute2", "iw", "iputils-ping", "util-linux", "netplan.io", "openssl"]
INSTALL_ROOT = "opt/bt-remote"


def run(argv, timeout=120, check=True):
    print("+", " ".join(argv), flush=True)
    return subprocess.run(argv, check=check, timeout=timeout).returncode


def target(prefix, relative, reject_symlink=True):
    result = prefix / relative
    try:
        result.parent.resolve().relative_to(prefix.resolve())
    except ValueError:
        raise RuntimeError("Destination escapes installation root: " + str(result))
    if reject_symlink and result.is_symlink():
        raise RuntimeError("Refusing to overwrite symlink: " + str(result))
    return result


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bt-remote-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_symlink(path, destination):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (".bt-remote-link-" + secrets.token_hex(5))
    try:
        os.symlink(destination, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def ensure_token(path):
    if path.is_symlink():
        raise RuntimeError("Token must not be a symlink")
    if path.exists():
        if not path.read_bytes().strip():
            raise RuntimeError("Existing token is empty; refusing to replace credentials silently")
        os.chmod(path, 0o600)
        return
    atomic_write(path, (secrets.token_hex(32) + "\n").encode(), 0o600)


def gateway_version():
    source = (ROOT / "board/bin/bt-gateway.py").read_text(encoding="utf-8")
    match = re.search(r'^GATEWAY_VERSION\s*=\s*"([0-9A-Za-z._-]+)"', source, re.MULTILINE)
    if not match:
        raise RuntimeError("GATEWAY_VERSION is missing")
    return match.group(1)


def release_id():
    digest = hashlib.sha256()
    for name in sorted(FILES):
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes())
    return gateway_version() + "-" + digest.hexdigest()[:12]


def backup_file(prefix, backup, relative):
    path = target(prefix, relative, reject_symlink=False)
    if not path.exists() and not path.is_symlink():
        return
    saved = backup / relative
    saved.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        os.symlink(os.readlink(path), saved)
    elif path.is_file():
        shutil.copy2(path, saved)


def install_static(prefix, backup):
    for src, (relative, mode) in SYSTEM_FILES.items():
        destination = target(prefix, relative)
        data = (ROOT / src).read_bytes().replace(b"\r\n", b"\n")
        if destination.exists() and destination.read_bytes() == data:
            os.chmod(destination, mode)
            continue
        if destination.exists():
            backup_file(prefix, backup, relative)
        atomic_write(destination, data, mode)


def stage_release(prefix, identifier):
    releases = target(prefix, INSTALL_ROOT + "/releases")
    releases.mkdir(parents=True, exist_ok=True)
    final = releases / identifier
    if final.exists():
        for src, (relative, _mode) in RELEASE_FILES.items():
            expected = (ROOT / src).read_bytes().replace(b"\r\n", b"\n")
            if not (final / relative).is_file() or (final / relative).read_bytes() != expected:
                raise RuntimeError("Existing release directory does not match source: " + str(final))
        return final
    temporary = releases / ("." + identifier + "." + secrets.token_hex(4))
    try:
        for src, (relative, mode) in RELEASE_FILES.items():
            atomic_write(temporary / relative, (ROOT / src).read_bytes().replace(b"\r\n", b"\n"), mode)
        atomic_write(temporary / "RELEASE", (identifier + "\n").encode(), 0o644)
        os.replace(temporary, final)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return final


def migrate_legacy_release(prefix):
    """Capture a pre-v2 install so the first atomic upgrade can also roll back."""
    current = target(prefix, INSTALL_ROOT + "/current", reject_symlink=False)
    if current.is_symlink() or prefix != Path("/"):
        return None
    old_gateway = target(prefix, "usr/local/bin/bt-gateway", reject_symlink=False)
    if not old_gateway.is_file() or old_gateway.is_symlink():
        return None
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    legacy = target(prefix, INSTALL_ROOT + "/releases/legacy-" + stamp)
    mapping = {
        "usr/local/bin/bt-gateway": "bin/bt-gateway",
        "usr/local/bin/bt-pair": "bin/bt-pair",
        "usr/local/bin/bt-connectable": "bin/bt-connectable",
        "usr/local/bin/bt-remote": "bin/bt-remote",
    }
    for source_name, release_name in mapping.items():
        source = target(prefix, source_name, reject_symlink=False)
        if source.is_file() and not source.is_symlink():
            destination = legacy / release_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    if not (legacy / "bin/bt-gateway").exists():
        shutil.rmtree(legacy, ignore_errors=True)
        return None
    atomic_symlink(current, "/" + str(legacy.relative_to(prefix)))
    return os.readlink(current)


def install(prefix, live):
    config = target(prefix, "etc/bt-gateway/gateway.env")
    token = target(prefix, "etc/bt-gateway/token")
    config.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(config.parent, 0o700)
    if live:
        ensure_token(token)
    if not config.exists():
        atomic_write(config, (ROOT / "board/etc/gateway.env.example").read_bytes(), 0o600)

    identifier = release_id()
    release = stage_release(prefix, identifier)
    if live:
        migrate_legacy_release(prefix)
    current = target(prefix, INSTALL_ROOT + "/current", reject_symlink=False)
    previous = target(prefix, INSTALL_ROOT + "/previous", reject_symlink=False)
    if current.exists() and not current.is_symlink():
        raise RuntimeError("Current release pointer is not a symlink: " + str(current))
    old_target = os.readlink(current) if current.is_symlink() else None

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    backup = target(prefix, "etc/bt-gateway/install-backups/" + stamp)
    install_static(prefix, backup)
    release_target = "/" + str(release.relative_to(prefix))
    if old_target and old_target != release_target:
        atomic_symlink(previous, old_target)
    atomic_symlink(current, release_target)

    for relative, release_name in COMMAND_LINKS.items():
        destination = target(prefix, relative, reject_symlink=False)
        expected = "/opt/bt-remote/current/" + release_name
        if destination.is_symlink() and os.readlink(destination) == expected:
            continue
        if destination.exists() or destination.is_symlink():
            backup_file(prefix, backup, relative)
            if destination.is_dir() and not destination.is_symlink():
                raise RuntimeError("Refusing to replace directory: " + str(destination))
        atomic_symlink(destination, expected)

    if backup.exists():
        print("Previous installed files saved in:", backup)
    for path in (target(prefix, "var/lib/bt-gateway/incoming"),
                 target(prefix, "var/lib/bt-gateway/update-work")):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    return {"release_id": identifier, "release_target": os.readlink(current),
            "previous_target": old_target}


def live_healthcheck(timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run(["/usr/local/bin/bt-remote", "doctor"], timeout=30, check=False) == 0:
            return
        time.sleep(2)
    raise RuntimeError("New gateway failed its authenticated health check")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print plan; no writes, packages or services")
    parser.add_argument("--no-deps", action="store_true", help="dependencies already installed (offline update)")
    parser.add_argument("--destdir", type=Path, help="stage files under this directory; no token or system changes")
    parser.add_argument("--health-timeout", type=int, default=45, help="seconds to wait before automatic rollback")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 8):
        parser.error("Python 3.8+ required")
    if not (5 <= args.health_timeout <= 180):
        parser.error("--health-timeout must be 5..180")
    prefix = args.destdir.resolve() if args.destdir else Path("/")
    if args.destdir and prefix == Path(prefix.anchor):
        parser.error("--destdir must not be the filesystem root")
    for name in [*FILES, "board/etc/gateway.env.example", "install.sh"]:
        if not (ROOT / name).is_file():
            raise RuntimeError("Incomplete checkout: missing " + name)
    for name in FILES:
        if name.endswith(".py"):
            compile((ROOT / name).read_text(encoding="utf-8"), name, "exec")
    if args.dry_run:
        print("STAGING" if args.destdir else "LIVE ATOMIC INSTALL (Debian/systemd, root required)")
        print("release:", "/opt/bt-remote/releases/" + release_id())
        for src, (dest, _mode) in FILES.items():
            print(src, "->", dest)
        print("Preserve token/config; switch current once; health-check and roll back on failure.")
        return 0
    if args.destdir:
        result = install(prefix, live=False)
        print("Staged release", result["release_id"], "only; no credentials or services changed.")
        return 0
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("Run on the Linux board using sudo bash install.sh")
    if not Path("/run/systemd/system").is_dir():
        raise RuntimeError("systemd must be the running init system")
    import fcntl
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
    for command in ("busctl", "bluetoothctl", "btmgmt", "hciconfig", "ip", "iw", "ping",
                    "flock", "timeout", "netplan", "lsblk", "mount", "umount", "systemd-run"):
        if not shutil.which(command):
            raise RuntimeError("Missing dependency: " + command)
    result = install(prefix, live=True)
    try:
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", "bluetooth.service"])
        run(["systemctl", "enable", "bt-gateway.service", "bt-connectable.timer"])
        run(["systemctl", "restart", "bt-connectable.timer"])
        run(["systemctl", "restart", "bt-gateway.service"], timeout=80)
        live_healthcheck(args.health_timeout)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        previous = result.get("previous_target")
        if previous:
            print("Health check failed; rolling back to", previous, file=sys.stderr)
            atomic_symlink(Path("/opt/bt-remote/current"), previous)
            run(["systemctl", "restart", "bt-connectable.timer", "bt-gateway.service"], timeout=80, check=False)
        raise
    print("Installed release", result["release_id"], "and passed authenticated health check.")
    print("Open pairing: sudo bt-remote pair on 300; inspect: sudo bt-remote doctor")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("INSTALL FAILED:", error, file=sys.stderr)
        print("Inspect: journalctl -u bt-gateway -n 60 --no-pager", file=sys.stderr)
        sys.exit(1)
