# Third-party software

The project's Apache-2.0 license does not replace third-party licenses.

| Component | Use | License / source |
|---|---|---|
| xterm.js, fit addon, CSS | Vendored Android assets | MIT; [upstream](https://github.com/xtermjs/xterm.js), full notice in `android/app/src/main/assets/vendor/LICENSE-xterm.txt` |
| AndroidX AppCompat / Core | Android build dependencies | Apache-2.0; [AndroidX source](https://android.googlesource.com/platform/frameworks/support/) |
| Kotlin standard library | Android build dependency | Apache-2.0; [Kotlin source](https://github.com/JetBrains/kotlin) |

BlueZ, systemd, Python and other board dependencies are installed by the operating system package manager; they are not copied into this source repository. Their respective package licenses continue to apply.

Earlier design notes mention SimpleBluetoothTerminal as an architectural reference. The current connection implementation is in `SppClient.kt`; no third-party reference repository is bundled. Preserve the original upstream notices when adding or upgrading vendored code.
