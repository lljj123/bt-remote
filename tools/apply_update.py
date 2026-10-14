#!/usr/bin/env python3
"""Verify and apply an authenticated local .btbundle, or roll back one release."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import subprocess
import sys
import time
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

STATE = Path("/var/lib/bt-gateway/update-status.json")
WORK_ROOT = Path("/var/lib/bt-gateway/update-work")
PUBLIC_KEY = Path("/etc/bt-gateway/update-public.pem")
CURRENT = Path("/opt/bt-remote/current")
PREVIOUS = Path("/opt/bt-remote/previous")


def write_status(state, message, **extra):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    value = {"state": state, "message": message, "updated_at": int(time.time())}
    value.update(extra)
    temporary = STATE.with_name(STATE.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, separators=(",", ":"))
        target.flush()
        os.fsync(target.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE)


def atomic_symlink(path, destination):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (".update-link-" + secrets.token_hex(5))
    try:
        os.symlink(destination, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def run(args, timeout=180):
    print("+", " ".join(str(item) for item in args), flush=True)
    subprocess.run([str(item) for item in args], check=True, timeout=timeout)


def valid_member(name):
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "" not in path.parts


def verify_and_extract(bundle, destination):
    write_status("verifying", "正在校验更新包", bundle=bundle.name)
    with zipfile.ZipFile(bundle) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise RuntimeError("更新包目录重复或缺少 manifest.json")
        manifest_bytes = archive.read("manifest.json")
        if len(manifest_bytes) > 1024 * 1024:
            raise RuntimeError("更新清单过大")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            raise RuntimeError("不支持的更新包格式")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise RuntimeError("更新清单没有文件")
        required = {"install.sh", "tools/install_gateway.py", "tools/apply_update.py",
                    "board/bin/bt-gateway.py", "board/systemd/bt-gateway.service"}
        if not required.issubset(files):
            raise RuntimeError("更新包缺少运行所需文件")
        allowed = set(files) | {"manifest.json", "manifest.sig"}
        if set(names).difference(allowed):
            raise RuntimeError("更新包包含清单外文件")

        signature_verified = False
        if PUBLIC_KEY.exists():
            if "manifest.sig" not in names:
                raise RuntimeError("板端已启用发布公钥，但更新包没有签名")
            public_key = load_pem_public_key(PUBLIC_KEY.read_bytes())
            if not isinstance(public_key, Ed25519PublicKey):
                raise RuntimeError("发布公钥不是 Ed25519 密钥")
            try:
                public_key.verify(archive.read("manifest.sig"), manifest_bytes)
            except InvalidSignature as error:
                raise RuntimeError("更新包签名无效") from error
            signature_verified = True

        for info in infos:
            name = info.filename
            if name in ("manifest.json", "manifest.sig"):
                continue
            if not valid_member(name) or name.endswith("/"):
                raise RuntimeError("更新包包含不安全路径")
            mode = (info.external_attr >> 16) & 0o177777
            if stat.S_IFMT(mode) == stat.S_IFLNK:
                raise RuntimeError("更新包不允许符号链接")
            data = archive.read(info)
            expected = files.get(name)
            if not isinstance(expected, str) or hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError("文件校验失败：" + name)
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            os.chmod(output, 0o755 if name.endswith((".sh", ".py")) or name == "install.sh" else 0o644)
    return manifest, signature_verified


def install_bundle(bundle):
    if not bundle.is_file() or bundle.suffix != ".btbundle":
        raise RuntimeError("更新包不存在或扩展名错误")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(WORK_ROOT, 0o700)
    work = WORK_ROOT / ("apply-" + secrets.token_hex(8))
    work.mkdir(mode=0o700)
    try:
        manifest, signed = verify_and_extract(bundle, work)
        version = str(manifest.get("gateway_version", "unknown"))[:80]
        write_status("installing", "校验通过，正在切换网关版本",
                     target_version=version, signature_verified=signed)
        run([work / "install.sh", "--no-deps", "--health-timeout", "45"], timeout=240)
        release = os.path.basename(os.path.realpath(CURRENT)) if CURRENT.exists() else version
        write_status("success", "网关更新成功", target_version=version,
                     release=release, signature_verified=signed)
        try:
            bundle.unlink()
        except OSError:
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)


def rollback():
    if not CURRENT.is_symlink() or not PREVIOUS.is_symlink() or not PREVIOUS.exists():
        raise RuntimeError("没有可回滚的上一版")
    current_target = os.readlink(CURRENT)
    previous_target = os.readlink(PREVIOUS)
    if current_target == previous_target:
        raise RuntimeError("当前版与上一版相同")
    write_status("rolling_back", "正在切换到上一版", target_release=os.path.basename(previous_target))
    atomic_symlink(CURRENT, previous_target)
    atomic_symlink(PREVIOUS, current_target)
    try:
        run(["systemctl", "restart", "bt-connectable.timer", "bt-gateway.service"], timeout=90)
        run(["/usr/local/bin/bt-remote", "doctor"], timeout=60)
    except Exception:
        atomic_symlink(CURRENT, current_target)
        atomic_symlink(PREVIOUS, previous_target)
        subprocess.run(["systemctl", "restart", "bt-connectable.timer", "bt-gateway.service"],
                       check=False, timeout=90)
        raise
    write_status("success", "已回滚到上一版", release=os.path.basename(previous_target))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", nargs="?", type=Path)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("must run as root")
    if args.rollback == (args.bundle is not None):
        parser.error("provide exactly one bundle or --rollback")
    if args.rollback:
        rollback()
    else:
        install_bundle(args.bundle.resolve())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile,
            subprocess.SubprocessError) as error:
        write_status("error", "更新失败：" + str(error)[:240])
        print("UPDATE FAILED:", error, file=sys.stderr)
        sys.exit(1)
