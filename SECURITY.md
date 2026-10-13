# Security

An authenticated terminal and `term.exec` provide root access. The token is a credential, not a device identifier.

- Installation generates a unique 256-bit token on the board, stored in `/etc/bt-gateway/token` with mode 0600. Reinstallation preserves it.
- Pairing is disabled by default. Use a bounded `bt-remote pair on 300` window, then close it after pairing.
- The default pairing agent uses Just Works. Avoid pairing in an untrusted environment. Android connection fallback can use insecure RFCOMM; the application token is not an independent encrypted transport.
- The Android client encrypts saved tokens using Android Keystore. Do not post tokens, netplan files, WiFi passwords, APK signing keys or private logs in public issues.
- `term.exec` deliberately accepts arbitrary shell syntax after token authentication. It is bounded by command length, output size and timeout, but it is not a sandbox or command whitelist.
- The authenticated `file.*` API can read and modify the root filesystem. Uploads are atomic and deletion is limited to regular files, symlinks and empty directories, but it is not a path sandbox. Protect the token as a root credential.
- `device.usb.list` is read-only but exposes attached-device model, VID:PID, serial number and Linux device names to an authenticated client. Treat hardware inventory as private operational data.
- Keep the mihomo external controller on loopback. Store its secret in a root-readable file and point `BTG_MIHOMO_SECRET_FILE` at that file; do not embed the secret in the APK.
- `bt-remote token` deliberately displays the token on the local console; status/doctor and installation do not.
- To change a compromised token, stop the gateway, replace the root-owned token file with a newly generated random value, restore mode 0600, restart, and update the phone. Remove untrusted Bluetooth pairings too.

Report vulnerabilities privately using the hosting platform's private vulnerability reporting feature once the maintainer enables it. No reporting mailbox is configured yet. Do not post exploit details with live credentials in public issues.

`tools/check_source.py` checks publishable file names and a few credential patterns. It is a guard against common mistakes, not a comprehensive security audit or Git history scanner. Review history before publishing an existing repository.
