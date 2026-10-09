# bt-remote · 香橙派蓝牙管理网关

Android 手机通过经典蓝牙 SPP 管理 Orange Pi 3 LTS，无需板子接入互联网。支持系统指标、服务管理、Netplan WiFi 配置、日志、RTC 设置和交互 root 终端。

原创代码使用 **Apache-2.0**，第三方代码保留原许可证，详见 [LICENSE](LICENSE) 与 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 一键安装（在板子上运行）

需要已安装蓝牙驱动的 Debian 系发行版、systemd、Python 3.9+、可用的经典蓝牙控制器 `hci0`。手机最低 Android 7 / API 24，运行时需授予附近设备权限。BLE-only 设备和 iOS 不支持本项目的 SPP 客户端。

把完整源码放到板子上，在项目目录运行：

```bash
sudo bash install.sh
```

安装器会安装依赖、检查环境、安装网关与管理命令、生成本机独立令牌、启用开机自启和蓝牙定时守护。重复执行可升级，保留已有令牌与配置，覆盖的旧程序保存在 `/etc/bt-gateway/install-backups/`。安装期间会重启网关，当前终端会断开。

```bash
# 先查看安装计划，不改系统
bash install.sh --dry-run
# 已有完整依赖时离线升级
sudo bash install.sh --no-deps
```

安装依赖需要软件源可达；运行期间不需要外网。安装器不会写入或切换当前 WiFi 配置，不会覆盖 SSH 配置，也不会自动重启或关机。若存在运行中的旧 `bt-agent` / `bt-ctl` 服务，会提示先处理冲突。

## 首次连接

```bash
sudo bt-remote pair on 300   # 开放 5 分钟配对窗口
sudo bt-remote token         # 在本机查看令牌，填入 App；不要公开
sudo bt-remote pair off      # 手机配对完成后关闭窗口
sudo bt-remote doctor        # 自启、运行状态与本地鉴权探测
```

手机系统蓝牙中完成配对，再打开 App 选择该设备并填写令牌。App 与板端需配套使用 `escaped-v1` 终端协议。Android 源码构建方式见 [android/README.md](android/README.md)。

板端另支持可选的 `json-mux-v1`，可在终端运行期间处理卡片请求。当前 App 尚未接入，继续使用旧协议；协议格式、并发限制和接入要求见 [终端并行协议](docs/terminal-json-mux-v1.md)。

## 自动启动与守护的边界

| 场景 | 行为 |
|---|---|
| 板子已经上电并启动 Linux | systemd 自动启动网关和蓝牙守护 timer |
| 网关崩溃、异常退出 | 5 秒后重启 |
| 网关主循环卡住 | 45 秒内未收到 watchdog 心跳，systemd 终止并重启 |
| Bluetooth 服务重启 | 网关检测 BlueZ owner 变化，退出并重新注册 |
| 蓝牙 connectable 状态丢失 | 每轮检查结束后约 20 秒再次检查、尝试恢复 |
| 板子已关机或断电 | 软件无法靠已停止工作的蓝牙开机；定时唤醒依赖 RTC、供电和固件实际支持 |
| 整个 Linux 内核挂死 | 本项目的进程守护无法代替硬件 watchdog |

RTC 功能是按设备能力设置唤醒闹钟，不保证所有系统和硬件都能从关机状态启动。安装脚本不会擅自配置定时开机。

## 常用命令

```bash
sudo bt-remote status
sudo bt-remote logs          # Ctrl+C 退出日志查看
sudo bt-remote restart
sudo bt-remote stop          # 本次停止；下次开机仍会自启
sudo bt-remote start
sudo bt-remote uninstall     # 卸载程序和自启单元，保留令牌、配置、配对和网络
```

永久取消自启：`sudo systemctl disable --now bt-gateway.service bt-connectable.timer`。

## 配置与兼容范围

编辑 `/etc/bt-gateway/gateway.env`，然后执行 `sudo bt-remote restart`：

```ini
BTG_WLAN=wlan0
BTG_NETPLAN_WIFI=/etc/netplan/30-wifis-dhcp.yaml
BTG_DEV_NAME="Orange Pi Bluetooth Gateway"
```

WiFi 修改功能只支持已有 Netplan 配置，不会把 NetworkManager 或其他网络后端自动迁移到 Netplan。默认配置文件不存在时仍可使用系统信息、终端等功能；请先明确实际网络配置再启用 WiFi 修改。

当前适配单个 `hci0` 控制器，蓝牙驱动/固件需由系统提供。内存上限默认 256 MiB，包含终端启动的子进程；可通过 `systemctl edit bt-gateway` 调整。

## 仓库结构

- `install.sh`：板端一键安装入口。
- `board/bin/`、`board/etc/`、`board/systemd/`：网关、配对、恢复脚本和自启单元。
- `tools/install_gateway.py`、`tools/manage_gateway.py`：可重复安装与管理命令。
- `android/`：Kotlin / WebView 客户端、Gradle Wrapper、终端组件。
- `.github/workflows/check.yml`：源码、安装器和 shell 自动检查。

## 开发与验证

```bash
python3 tools/check_source.py
python3 tools/install_gateway.py --destdir .stage
```

`--destdir` 只生成检查用目录，不安装依赖、不启动宿主服务、不生成令牌。板端部署后执行 `sudo bt-remote doctor` 检查服务与本地认证，并通过 App 验证终端交互。

本次安装器在 Windows 工作区做了隔离测试，尚未在真实板子上执行安装，也未实测开机自启或 watchdog 恢复。CI 只做无硬件检查，不等同于真机认证。贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 安全与发布

令牌可开启 root 终端，必须按管理员密码保管。不要公开真实令牌、Netplan 配置、WiFi 密码和签名密钥；详见 [SECURITY.md](SECURITY.md)。源代码目录可以包含被忽略的本地构建产物，但发布前必须检查 Git 暂存区和历史。当前工作区尚未建立 Git 仓库，也未发布到任何托管平台。

守护配置依据 [systemd service 文档](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml)，配对窗口使用 [BlueZ Adapter 属性](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Adapter.rst)。
