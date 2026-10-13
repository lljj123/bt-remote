# Android 客户端

Kotlin + WebView 仪表盘 + xterm.js，通过经典蓝牙 SPP 连接网关。最低 Android 7（API 24）；使用系统已配对设备列表，不做设备发现扫描。

## 构建

安装 JDK 17 与 Android SDK 34（含 Build Tools 34.0.0）。设置 `ANDROID_HOME` 指向自己的 SDK，或在本机创建 `local.properties`，例如 `sdk.dir=/your/android-sdk`；该文件不可提交。

在本目录执行：

```bash
# Linux/macOS
bash gradlew assembleDebug lintDebug
```

```powershell
# Windows
.\gradlew.bat assembleDebug lintDebug
```

Gradle Wrapper 固定为 8.7，首次运行需要下载 Gradle 与依赖。APK 输出到 `app/build/outputs/apk/debug/app-debug.apk`。Debug APK 仅用于开发；公开发行应使用自己的 release 签名配置，不要提交 keystore 或密码。

## 协议

建立 SPP 后交换 hello/auth，管理消息为 NDJSON。`term.shell` 携带 `protocol: escaped-v1`；收到 `term.ready` 后进入转义字节流，普通字节 0x01 双写，板端以 0x01 E 恢复 NDJSON 并发送 `term.exit`。关闭/缩放控制帧分别为 0x01 C 和 0x01 R + 两个大端 u16。

解析器按连接隔离，终端期间阻止管理 JSON；断线或连接任务取消后旧线程不能覆盖新连接。令牌使用 Android Keystore 加密存储，旧 localStorage 值在成功迁移后清除。

概览页的附加系统信息由 `app/src/main/assets/dashboard.html` 中的 `CUSTOM_INFO_COMMANDS` 数组驱动，调用网关 `term.exec`。以后调整显示项只需修改该数组并重新构建 APK。服务页的代理卡片调用 `proxy.status/switch/mode/onoff`，需要板端配置 mihomo external-controller。

“文件”页替代旧的“日志”页签。板端文件通过 `file.*` 分块协议管理，手机侧通过 Android Storage Access Framework 选择上传源和下载位置，因此不需要存储权限。上传及下载都校验 SHA256；小型 UTF-8 文本可以直接预览和原子保存。

“网络”页通过 `net.ifaces` 枚举全部网卡，可选择板载或 USB 无线接口，再将 `iface` 传给 `net.wifi.status/scan/saved/connect/forget`。“外设”页通过分页的 `device.usb.list` 显示 USB 设备、驱动及其网卡/磁盘/串口节点，不需要 Android USB 权限，因为枚举发生在板端。

终端输出在原生层合帧，进入页面后再按 xterm 写入完成回调逐帧消费，避免大量输出长期占用 WebView 主线程。移动端复制使用“选择”模式拖动后点击“复制”；“全选”和“粘贴”通过 Android `ClipboardManager` 桥接，不依赖 `file://` 页面剪贴板权限。

## 许可证

项目原创代码为 Apache-2.0；`app/src/main/assets/vendor/` 内 xterm.js 和 fit addon 为 MIT，完整声明随资源打包。

根目录 README 描述板端安装、首次配对和守护配置。旧版本功能说明请参考历史提交，不要按旧提示词重新生成网关。
