# bt-remote · 香橙派蓝牙管理网关

Android 手机通过经典蓝牙 SPP 管理 Orange Pi 3 LTS。支持系统指标、多无线网卡 Netplan 配置、磁盘/U 盘挂载与文件访问、USB 外设查看、RTC 设置、mihomo 订阅与代理控制、App 内文件管理、多标签交互 root 终端和单次 `term.exec` 命令。

原创代码使用 **Apache-2.0**，第三方代码保留原许可证，详见 [LICENSE](LICENSE) 与 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 一键安装（在板子上运行）

需要已安装蓝牙驱动的 Debian 系发行版、systemd、Python 3.8+、可用的经典蓝牙控制器 `hci0`。手机最低 Android 7 / API 24，运行时需授予附近设备权限。BLE-only 设备和 iOS 不支持本项目的 SPP 客户端。

把完整源码放到板子上，在项目目录运行：

```bash
sudo bash install.sh
```

安装器会安装依赖、检查环境、安装网关与管理命令、生成本机独立令牌、启用开机自启和蓝牙定时守护。运行文件按版本存放在 `/opt/bt-remote/releases/`，通过 `current` 一次切换；升级后执行本地鉴权健康检查，失败自动切回 `previous`。重复执行保留已有令牌与配置，静态文件旧版仍备份到 `/etc/bt-gateway/install-backups/`。安装期间会重启网关，当前终端会断开。

```bash
# 先查看安装计划，不改系统
bash install.sh --dry-run
# 已有完整依赖时离线升级
sudo bash install.sh --no-deps
```

完成一次 2.0 基础安装后，后续可在 App“服务 → 版本与网关升级”中选择更新包：

```bash
python3 tools/build_release.py
# 可选：使用离线保管的 Ed25519 私钥签名
python3 tools/build_release.py --sign-key /安全位置/release-ed25519-private.pem
```

默认输出在 `release/`。本地未签名包只通过已鉴权蓝牙链路安装；若把对应公钥放到板端
`/etc/bt-gateway/update-public.pem`，板端会拒绝一切未签名或签名不匹配的更新包。私钥不要
放进项目、APK 或板子。App 更新本身仍由 Android 安装包管理器处理；APK 可交给 GitHub
Releases/Obtainium 分发，Android 不允许普通 App 静默替换自身。

安装依赖需要软件源可达；运行期间不需要外网。安装器不会写入或切换当前 WiFi 配置，不会覆盖 SSH 配置，也不会自动重启或关机。若存在运行中的旧 `bt-agent` / `bt-ctl` 服务，会提示先处理冲突。

## 首次连接

```bash
sudo bt-remote pair on 300   # 开放 5 分钟配对窗口
sudo bt-remote token         # 在本机查看令牌，填入 App；不要公开
sudo bt-remote pair off      # 手机配对完成后关闭窗口
sudo bt-remote doctor        # 自启、运行状态与本地鉴权探测
```

手机系统蓝牙中完成配对，再打开 App 选择该设备并填写令牌。App 使用 `json-mux-v1` 在同一蓝牙连接上同时运行最多 4 个独立 PTY，终端期间卡片和心跳仍可刷新。协议见 [终端并行协议](docs/terminal-json-mux-v1.md)，Android 源码构建方式见 [android/README.md](android/README.md)。

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
# 可选：mihomo external-controller
BTG_MIHOMO_API=http://127.0.0.1:9090
BTG_MIHOMO_SECRET_FILE=/etc/mihomo/secret
BTG_MIHOMO_GROUP=PROXY
BTG_MIHOMO_CONFIG=/etc/mihomo/config.yaml
```

代理配置默认留空；请按实际 mihomo 配置填写 controller、secret 文件、Selector 策略组和配置文件路径，随后执行 `sudo bt-remote restart`。App 可保存至多 12 个 Mihomo/Clash proxy-provider 订阅并选择应用，选择框只显示用户填写的机场名称，不回显订阅链接；关闭代理只切到直连，不删除订阅或节点选择。TUN 透明代理使用独立开关，关闭后会移除 Mihomo 虚拟网卡；此时只有显式使用 HTTP/SOCKS 端口的流量进入 Mihomo，ICMP `ping` 不会经过代理。“绕过大陆”会持久写入私网和中国大陆直连规则。写入前会备份原配置到 `/etc/bt-gateway/mihomo-backups/`，控制器拒绝重载时自动恢复。已有 `gateway.env` 升级时不会被覆盖，需要手工补充这些变量。

网关提供通用 `term.exec`，可在协议台中执行临时系统查询，不必再为每个查询增加专用网关命令。`term.exec` 与交互终端一样拥有 root 权限，具体协议见 [网关协议 v2](docs/gateway-protocol-v2.md)。App 概览不额外显示“自定义系统信息”和网络接口卡片。

App 的“文件”页可浏览板端目录，提供返回历史、上一级、文件/目录复制粘贴、上传、下载、新建目录、重命名、删除空目录或文件，以及预览和编辑小型 UTF-8 文本。复制项会保留在页内剪贴板，可进入目标目录后粘贴；同名时先要求新名称。新版传输使用 2560 字节、4 请求窗口流水线，旧网关自动退回 1536 字节停等；上传先写同目录临时文件，SHA256 校验成功后原子替换，断线时清理临时文件。Android 通过系统文件选择器访问手机文件，不申请整个存储空间权限。日志协议和板端 `bt-remote logs` 仍保留，但 App 不显示独立日志选项卡。

“网络”页枚举当前所有接口，并把板载无线和 USB 无线网卡分别标识。选择任意已被内核驱动识别的无线接口后，可用该接口扫描、连接和忘记 WiFi；所选接口会作为独立 Netplan `wifis` 条目写入，不覆盖其它无线接口。“外设”页从 sysfs 读取 USB 型号、VID:PID、速率、驱动及关联的网卡、磁盘和串口。

概览页通过 `lsblk` 每块物理存储只显示一项：系统盘、板载闪存和 USB 存储分别选择其主文件系统，不再展开所有分区；eMMC boot/RPMB 和 zram/ram 等实现设备会被隐藏。未挂载的主文件系统可挂载到 `/mnt/bt-remote/`；挂载后会出现在“文件”页快捷入口。FAT/exFAT 会跳过不支持的 `chmod`，仍保留原子替换流程。“服务”页不再列出 systemd 服务，代理与可折叠的电源/RTC 控制分开显示。App 不再采集或显示进程 Top，顶部始终显示蓝牙 RSSI、手机→板子上传和板子→手机下载的实时速率；终端全屏时仍保留多终端选项卡状态栏。

WiFi 修改功能只支持已有 Netplan 配置，不会把 NetworkManager 或其他网络后端自动迁移到 Netplan。USB 无线网卡仍需系统内核具备对应驱动/固件；若“外设”页可见但没有驱动和网卡名，需先解决板端驱动。默认 Netplan 配置文件不存在时仍可使用系统信息、终端等功能；请先明确实际网络配置再启用 WiFi 修改。

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

令牌可开启 root 终端和 root 文件管理，必须按管理员密码保管。不要公开真实令牌、Netplan 配置、WiFi 密码和签名密钥；详见 [SECURITY.md](SECURITY.md)。源代码目录可以包含被忽略的本地构建产物，但发布前必须检查 Git 暂存区和历史。

守护配置依据 [systemd service 文档](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml)，配对窗口使用 [BlueZ Adapter 属性](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Adapter.rst)。
