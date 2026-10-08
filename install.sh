#!/usr/bin/env bash
# Run on the board: sudo bash install.sh. See --help for staging and preview.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    echo "Runnable Python 3.9+ required. Run this installer on the Linux board." >&2
    echo "On Debian: sudo apt-get install python3. On Windows, preview with python tools/install_gateway.py --dry-run." >&2
    exit 1
fi
exec python3 "$ROOT/tools/install_gateway.py" "$@"
