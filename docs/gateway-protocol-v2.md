# 网关协议 v2 增量

本文只描述 v1 基础协议之上的新增命令。传输仍为 UTF-8 NDJSON，认证、
4096 字节帧上限、响应 `id` 匹配和 `escaped-v1` 终端相位均保持不变。

`hello` 和认证成功响应都带网关语义版本、构建号、兼容协议范围及按功能划分的版本；
旧客户端可继续只读取 `ver/caps`：

```json
{"t":"auth","ok":true,"ver":2,"gateway_version":"2.0.2","build":"20261014","protocol_min":1,"protocol_max":2,"features":{"sys":2,"term":2,"file":2,"update":1},"caps":["sys","svc","net","pwr","log","term","proxy","file","device","storage","multiwifi","update"]}
```

## 单次通用命令

`sys.metrics` 的 `bluetooth.rssi_dbm` 通过 HCI Read RSSI 主动读取当前已鉴权 SPP 对端的 BR/EDR RSSI；
本地 socket 或控制器暂不支持连接信息时为 `null`。

`term.exec` 在 root 身份下通过不加载 profile 的 `/bin/bash` 执行一条命令。
它不是白名单或沙箱，权限边界与交互 root 终端相同。命令最长 2048 字节，
超时最大 120 秒，保留的合并输出最大 2048 字节；进程超时后会终止其进程组。

```json
{"t":"cmd","id":1,"name":"term.exec","args":{"command":"uname -m","timeout_s":8,"max_output_bytes":1024}}
```

```json
{"t":"res","id":1,"ok":true,"data":{"exit_code":0,"output_b64":"YWFyY2g2NAo=","output_bytes":8,"truncated":false,"timed_out":false,"duration_ms":12}}
```

`output_b64` 是 stdout 与 stderr 合并后的原始字节。`output_bytes` 是网关实际
读取到的总字节数，可能大于保留长度；此时 `truncated=true`。客户端应先 Base64
解码，再按 UTF-8 显示或按命令约定解析。

`term.caps` 返回交互终端和单次执行能力：

```json
{"interactive_root":true,"restricted":false,"protocols":["escaped-v1","json-mux-v1"],"shells":["/bin/bash","/bin/sh","/bin/dash"],"allow":["*"],"max_out_kb":2,"max_sessions":4,"exec":{"name":"term.exec","max_command_bytes":2048,"max_output_bytes":2048,"timeout_s_max":120}}
```

## 系统服务分页

带 `offset` 的 `svc.list` 会重新发现已安装或已加载的 systemd service，并分页返回，
避免全部服务超过单帧 4096 字节限制。第一页可传 `refresh=true` 清除短时发现缓存：

```json
{"name":"svc.list","args":{"offset":0,"limit":8,"refresh":true}}
```

响应为 `services/offset/next_offset/eof/total`。每个服务的 `managed` 表示它是否在
网关操作白名单内；`managed=false` 只允许查看，`svc.act` 仍会拒绝对它进行启停或
自启修改。省略 `offset` 时仍返回旧版白名单数组，以兼容旧 APK。

## Mihomo 代理

板端环境变量：

- `BTG_MIHOMO_API`：external-controller，例如 `http://127.0.0.1:9090`。
- `BTG_MIHOMO_SECRET_FILE`：包含 controller secret 的 root 可读文件。
- `BTG_MIHOMO_GROUP`：App 控制的 Selector 策略组；留空时网关尝试选择常见组名。
- `BTG_MIHOMO_CONFIG`：需要持久修改的 Mihomo YAML 配置路径。
- `BTG_MIHOMO_TEST_URL`：节点延迟测试 URL。

未配置 `BTG_MIHOMO_API` 时，`proxy.status` 正常返回 `configured=false`，
其它修改命令返回 `E_PROXY_CONFIG`。

### 状态与刷新延迟

```json
{"t":"cmd","id":2,"name":"proxy.status","args":{"refresh":false}}
```

```json
{"configured":true,"reachable":true,"running":true,"version":"...","mode":"rule","tun_enabled":true,"tun_device":"Meta","tun_configurable":true,"group":"PROXY","current":"node-a","nodes":[{"name":"node-a","type":"Shadowsocks","delay":42,"selected":true}]}
```

`refresh=true` 会并发测试最多 32 个普通节点，命令可能需要数秒。`running`
表示 controller 可达且模式不是 `direct`，不是 mihomo systemd 服务状态。

### 切换节点、模式和开关

```json
{"t":"cmd","id":3,"name":"proxy.switch","args":{"name":"node-b"}}
{"t":"cmd","id":4,"name":"proxy.mode","args":{"mode":"global"}}
{"t":"cmd","id":5,"name":"proxy.onoff","args":{"on":false}}
{"t":"cmd","id":6,"name":"proxy.tun","args":{"on":true}}
```

`proxy.onoff true` 切到 `rule`，`false` 切到 `direct`。关闭只改变模式，不会删除
已保存订阅、provider 配置或当前节点选择。`proxy.tun` 独立控制并持久化 TUN；开启时
补齐自动路由、DNS 劫持、Fake-IP 和局域网排除配置，关闭后 Mihomo 会移除 TUN 虚拟
网卡，但不会改变节点、模式或订阅。

切换到 `rule`（App 显示为“绕过大陆”）时，网关会把私有地址与中国大陆
GEOSITE/GEOIP 规则置于最前，最后使用 `MATCH,<BTG_MIHOMO_GROUP>`，并保留已有的
非 MATCH 自定义规则。配置重载失败会自动恢复备份。

还提供底层兼容命令：

切换到 `global` 时，网关会先把 Mihomo 内置的 `GLOBAL` Selector 指向
`BTG_MIHOMO_GROUP` 对应的策略组，再切换运行模式，避免界面显示全局而实际仍为
`DIRECT`。成功响应会额外返回 `global_target`。

- `proxy.groups {}`
- `proxy.nodes {"group":"PROXY"}`
- `proxy.select {"group":"PROXY","name":"node-b"}`
- `proxy.delay {"name":"node-b","timeout_ms":3000}`

代理修改和延迟测试使用互斥锁；冲突返回 `E_BUSY`。controller 相关错误码为
`E_PROXY_AUTH/CONFIG/CONNECT/DATA/HTTP/NOTFOUND/SIZE/TIMEOUT`。

### 订阅链接

- `proxy.sub.list {}`：只返回已保存订阅的名称和当前选择，不返回订阅链接或 URL 提示。
- `proxy.sub.save {"name":"机场 A","url":"https://..."}`：保存订阅，最多 12 个。
- `proxy.sub.apply {"id":"..."}`：写入专用 `bt-remote-subscription` proxy-provider，并把 provider 加入选定策略组。
- `proxy.sub.remove {"id":"..."}`：删除非当前订阅记录。

应用订阅前会备份 `BTG_MIHOMO_CONFIG`，然后通过 `/configs?force=true` 传递新 YAML
让 Mihomo 重载；重载失败时恢复文件并尝试加载旧配置。订阅链接作为 root 私密状态和
Mihomo 配置保存，列表响应只显示不含查询参数的提示。

## 多无线网卡

`net.ifaces {}` 返回当前所有非 loopback 网络接口。每项包含 `name/type/bus/external/driver/state/ipv4/mac`，无线接口还包含 `ssid/bssid/signal_dbm/freq`。USB 无线网卡通常为 `type=wifi,bus=usb,external=true`。

以下命令都接受 `iface`；省略时为兼容旧客户端而使用 `BTG_WLAN`（默认 `wlan0`）：

```json
{"name":"net.wifi.status","args":{"iface":"wlx001122334455"}}
{"name":"net.wifi.scan","args":{"iface":"wlx001122334455","rescan":true}}
{"name":"net.wifi.saved","args":{"iface":"wlx001122334455"}}
{"name":"net.wifi.connect","args":{"iface":"wlx001122334455","ssid":"Example","psk":"password","timeout_s":30}}
{"name":"net.wifi.forget","args":{"iface":"wlx001122334455","ssid":"Example"}}
```

网关验证接口确实存在且为无线设备。连接配置写入所选接口自己的 Netplan 条目，并保留其它无线接口配置；连接失败仍使用独立 systemd 看门狗回滚。

## USB 外接设备

`device.usb.list {"offset":0,"limit":8}` 从 sysfs 分页枚举当前外接 USB 设备（不包含主控根 Hub）。响应包含总数、下一偏移以及 `devices`；每台设备包括 VID:PID、厂商、产品、可选序列号、USB 速率、设备类别、已绑定驱动和关联的网卡/磁盘/串口名。该命令只读，不执行 USB 授权切换、断电或存储卸载。

## 磁盘与 U 盘

`storage.list {"offset":0,"limit":8}` 通过 `lsblk` 每块物理磁盘、板载闪存或
USB 存储只返回一项。主文件系统优先级为根分区、App 管理的挂载、容量最大的文件系统；
eMMC 的 boot/RPMB 节点和 zram/ram 压缩交换设备不会作为存储盘返回；
响应包含整盘容量、`disk_path`、实际操作用的 `path`、文件系统、卷标、挂载点和已用空间。
`storage.mount {"path":"/dev/sda1"}` 只接受当前枚举出的带文件系统设备，挂载到
`/mnt/bt-remote/` 并使用 `nosuid,nodev`。`storage.unmount` 只允许卸载由 App 挂载到
该目录下的文件系统，不会卸载系统原有挂载点。

## App 内文件管理

`file.caps` 返回兼容的 `stopwait-v1`（1536 字节）以及 `window-v2`（默认每块
2560 字节、4 个在途请求）、最大传输大小和文本预览上限。
所有路径必须是绝对路径；该接口与 root 终端权限相同，不限制在某个目录内。

目录和普通操作：

- `file.list {"path":"/root","offset":0,"limit":24}`：分页列目录。
- `file.stat {"path":"/root/a.txt"}`：读取类型、大小、时间和模式。
- `file.read {"path":"/root/a.txt","offset":0,"length":1536}`：Base64 分块读取。
- `file.hash {"path":"/root/a.txt"}`：计算 SHA256。
- `file.mkdir {"path":"/root/new-dir"}`。
- `file.copy {"path":"/root/a","target":"/mnt/data/a"}`：复制文件、符号链接或目录，
  不覆盖已有目标，也不允许把目录复制到自身或其子目录。
- `file.rename {"path":"/root/a","target":"/root/b"}`：不覆盖已有目标。
- `file.remove {"path":"/root/a","confirm":"DELETE"}`：仅文件、符号链接或空目录。

上传为同目录临时文件加原子提交：

```json
{"name":"file.upload.begin","args":{"path":"/root/a.bin","size":2048,"overwrite":false,"protocol":"window-v2"}}
{"name":"file.upload.chunk","args":{"upload_id":"...","offset":0,"data_b64":"..."}}
{"name":"file.upload.commit","args":{"upload_id":"...","sha256":"..."}}
```

`window-v2` 上传偏移仍严格连续，但客户端可先发送服务端返回的 `window_chunks` 个
请求再集中等待响应；单块不超过服务端返回的 `chunk_bytes`。下载用
`file.download.begin/chunk/end` 固定一次打开的文件句柄，chunk 请求可采用相同窗口。
旧客户端的 `file.read` 与 1536 字节限制保持不变。`commit` 只有在声明大小完整且可选
SHA256 一致时才用 `os.replace` 发布目标文件。断线或 `file.upload.abort` 会删除临时文件。
文件错误码为 `E_FILE_ARGS/EXISTS/HASH/IO/NOTFOUND/OFFSET/PERM/SIZE/STATE/TYPE`。

## 网关原子升级

- `update.status {}`：返回当前版本目录、最近一次升级状态和是否可回滚。
- `update.apply {"path":"/var/lib/bt-gateway/incoming/a.btbundle","confirm":"UPDATE"}`：
  只接受经当前鉴权连接上传到 incoming 目录的更新包。
- `update.rollback {"confirm":"ROLLBACK"}`：切换到 `previous`，失败则恢复原版本。

`.btbundle` 内的清单列出所有文件 SHA256。若板端存在
`/etc/bt-gateway/update-public.pem`，更新助手还会强制验证 `manifest.sig` 的 Ed25519
签名。安装文件先写入 `/opt/bt-remote/releases/<release-id>`，再原子切换
`/opt/bt-remote/current`；启动后执行本地 socket 鉴权和功能探测，失败自动切回上一版。
