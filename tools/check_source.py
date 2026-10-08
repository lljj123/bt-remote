#!/usr/bin/env python3
"""Check publishable source paths and obvious credentials; never print credential values."""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".gradle", ".idea", "build", "apk", "backups", "release", ".stage", "test-classes", "__pycache__"}


def forbidden(name):
    p = Path(name)
    return (p.name in {"token", "local.properties", "keystore.properties", "gateway.env", "state.json", "SHA256.json"}
            or p.suffix.lower() in {".apk", ".zip", ".jks", ".keystore", ".pem", ".key"}
            or (p.name.startswith(".env") and not p.name.endswith(".example"))
            or any(part in SKIP for part in p.parts))


def sources():
    try:
        probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True, text=True)
        if probe.returncode == 0 and Path(probe.stdout.strip()).resolve() == ROOT:
            result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
            return [p for p in result.stdout.decode("utf-8").split("\0") if p], True
    except (OSError, subprocess.SubprocessError):
        pass
    names = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for name in files:
            rel = (Path(base) / name).relative_to(ROOT).as_posix()
            if not forbidden(rel): names.append(rel)
    return names, False


def main():
    files, tracked = sources()
    errors = []
    patterns = [re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
                re.compile(r'''(?i)(?:token|password|api_key)\s*[:=]\s*["'][0-9a-f]{32,}["']''')]
    for name in files:
        if forbidden(name):
            errors.append(name + ": private/generated file is tracked")
            continue
        path = ROOT / name
        if not path.is_file() or path.suffix in {".jar", ".png"}: continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in patterns):
                errors.append(f"{name}:{number}: possible credential (value omitted)")
    for name in ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "android/app/src/main/assets/vendor/LICENSE-xterm.txt"]:
        if not (ROOT / name).is_file(): errors.append("Missing notice: " + name)
    for error in errors: print("FAIL:", error)
    print(f"Checked {len(files)} {'tracked' if tracked else 'source candidate'} files; {len(errors)} issue(s).")
    if not tracked: print("No root Git repository detected: rerun after git add, and review Git history separately.")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
