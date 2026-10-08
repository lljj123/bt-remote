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

## 许可证

项目原创代码为 Apache-2.0；`app/src/main/assets/vendor/` 内 xterm.js 和 fit addon 为 MIT，完整声明随资源打包。

根目录 README 描述板端安装、首次配对和守护配置。旧版本功能说明请参考历史提交，不要按旧提示词重新生成网关。
