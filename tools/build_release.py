#!/usr/bin/env python3
"""Build a reproducible gateway .btbundle; optionally sign its manifest with Ed25519."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

import install_gateway

ROOT = Path(__file__).resolve().parents[1]


def build(output, sign_key=None):
    names = sorted(set(install_gateway.FILES) |
                   {"install.sh", "tools/install_gateway.py",
                    "board/etc/gateway.env.example"})
    files = {}
    payloads = {}
    for name in names:
        data = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        payloads[name] = data
        files[name] = hashlib.sha256(data).hexdigest()
    manifest = {
        "format": 1,
        "product": "bt-remote-gateway",
        "gateway_version": install_gateway.gateway_version(),
        "release_id": install_gateway.release_id(),
        "protocol_min": 1,
        "protocol_max": 2,
        "files": files,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")) + "\n").encode("utf-8")
    signature = None
    if sign_key:
        private_key = load_pem_private_key(sign_key.read_bytes(), password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError("--sign-key must contain an Ed25519 private key")
        signature = private_key.sign(manifest_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as archive:
            def add(name, data, mode):
                info = zipfile.ZipInfo(name, date_time=(2026, 10, 14, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (mode & 0xffff) << 16
                archive.writestr(info, data)
            add("manifest.json", manifest_bytes, 0o644)
            if signature is not None:
                add("manifest.sig", signature, 0o644)
            for name in names:
                mode = 0o755 if name.endswith((".sh", ".py")) or name == "install.sh" else 0o644
                add(name, payloads[name], mode)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(output)
    print("sha256", hashlib.sha256(output.read_bytes()).hexdigest())
    print("signature", "ed25519" if signature is not None else "local-authenticated")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sign-key", type=Path, help="OpenSSL Ed25519 private key (never bundled)")
    args = parser.parse_args(argv)
    output = args.output or ROOT / "release" / (
        "bt-remote-gateway-" + install_gateway.gateway_version() + ".btbundle")
    build(output.resolve(), args.sign_key.resolve() if args.sign_key else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
