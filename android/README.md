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

建立 SPP 后交换 hello/auth，管理和终端消息都使用 NDJSON。`term.shell` 携带 `protocol: json-mux-v1`；输入、输出、缩放和关闭都带有独立 `sid`。App 可同时打开最多 4 个 PTY 选项卡，各自保留 xterm 缓冲区与状态，不会阻止卡片刷新；进入全屏后仍显示紧凑的多终端状态栏。

解析器按连接隔离，断线或连接任务取消后旧线程不能覆盖新连接。原生 SPP 层每秒统计实际收发字节，顶部持续显示蓝牙上传/下载速率。令牌使用 Android Keystore 加密存储，旧 localStorage 值在成功迁移后清除。

概览页不显示额外的“自定义系统信息”卡片；通用 `term.exec` 仍可从协议台调用。“服务”页只保留代理与电源/RTC：代理卡片调用 `proxy.status/switch/mode/onoff/tun` 和 `proxy.sub.*`，可保存并应用 Mihomo/Clash proxy-provider 订阅，选择框只显示机场名称；关闭代理只切换直连模式，不清除订阅或节点。TUN 透明代理有独立开关，开启时创建 Mihomo 虚拟网卡并让板端程序自动经过代理，关闭后移除虚拟网卡。需要板端配置 mihomo external-controller 与配置文件路径。

“文件”页替代旧的“日志”页签，提供返回历史、上一级和页内文件剪贴板，可复制文件、符号链接或目录后进入目标目录粘贴。板端文件通过 `file.*` 分块协议管理，手机侧通过 Android Storage Access Framework 选择上传源和下载位置，因此不需要存储权限。上传及下载都校验 SHA256；小型 UTF-8 文本可以直接预览和原子保存。`storage.*` 每块板载闪存或 USB 存储只返回一个主文件系统，隐藏 eMMC boot/RPMB 与 zram/ram 实现设备；App 挂载后会成为文件页快捷入口。

“网络”页通过 `net.ifaces` 枚举全部网卡，可选择板载或 USB 无线接口，再将 `iface` 传给 `net.wifi.status/scan/saved/connect/forget`。“外设”页通过分页的 `device.usb.list` 显示 USB 设备、驱动及其网卡/磁盘/串口节点，不需要 Android USB 权限，因为枚举发生在板端。

终端输出在原生层合帧，进入页面后再按 xterm 写入完成回调逐帧消费，避免大量输出长期占用 WebView 主线程。移动端复制使用“选择”模式拖动后点击“复制”；“全选”和“粘贴”通过 Android `ClipboardManager` 桥接，不依赖 `file://` 页面剪贴板权限。

## 许可证

项目原创代码为 Apache-2.0；`app/src/main/assets/vendor/` 内 xterm.js 和 fit addon 为 MIT，完整声明随资源打包。

根目录 README 描述板端安装、首次配对和守护配置。旧版本功能说明请参考历史提交，不要按旧提示词重新生成网关。
