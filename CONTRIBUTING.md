# Contributing

Use Python 3.9+, Node.js and Bash to run the hardware-free checks:

```sh
python3 -m unittest discover -s tools -p 'test_*.py' -v
python3 tools/check_source.py
bash -n install.sh board/bin/bt-pair.sh board/bin/bt-connectable.sh board/etc/wifi-guard.sh
python3 tools/install_gateway.py --dry-run
```

For Android, install JDK 17 and Android SDK 34, then run `./gradlew assembleDebug lintDebug` in `android/`. Keep `local.properties` and signing credentials out of Git.

Use `python3 tools/install_gateway.py --destdir .stage` to inspect an installation without changing host services or generating a token. Do not confuse this staging directory with a bootable/root filesystem.

Changes to terminal protocol must update both endpoints and preserve split-frame handling. Changes to networking must test guard creation failure and rollback. Hardware tests must state board model, OS, BlueZ version and phone Android version; never include credentials.

Contributions to original project code are under Apache-2.0. Keep third-party notices intact. No contributor license agreement is currently required.
