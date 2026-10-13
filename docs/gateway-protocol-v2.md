# 网关协议 v2 增量

本文只描述 v1 基础协议之上的新增命令。传输仍为 UTF-8 NDJSON，认证、
4096 字节帧上限、响应 `id` 匹配和 `escaped-v1` 终端相位均保持不变。

认证成功后的 `caps` 包括 `proxy`、`file` 和 `device`：

```json
{"t":"auth","ok":true,"caps":["sys","svc","net","pwr","log","term","proxy","file","device","multiwifi"]}
```

## 单次通用命令

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
{"interactive_root":true,"restricted":false,"protocols":["escaped-v1","json-mux-v1"],"shells":["/bin/bash","/bin/sh","/bin/dash"],"allow":["*"],"max_out_kb":2,"exec":{"name":"term.exec","max_command_bytes":2048,"max_output_bytes":2048,"timeout_s_max":120}}
```

## Mihomo 代理

板端环境变量：

- `BTG_MIHOMO_API`：external-controller，例如 `http://127.0.0.1:9090`。
- `BTG_MIHOMO_SECRET_FILE`：包含 controller secret 的 root 可读文件。
- `BTG_MIHOMO_GROUP`：App 控制的 Selector 策略组；留空时网关尝试选择常见组名。
- `BTG_MIHOMO_TEST_URL`：节点延迟测试 URL。

未配置 `BTG_MIHOMO_API` 时，`proxy.status` 正常返回 `configured=false`，
其它修改命令返回 `E_PROXY_CONFIG`。

### 状态与刷新延迟

```json
{"t":"cmd","id":2,"name":"proxy.status","args":{"refresh":false}}
```

```json
{"configured":true,"reachable":true,"running":true,"version":"...","mode":"rule","group":"PROXY","current":"node-a","nodes":[{"name":"node-a","type":"Shadowsocks","delay":42,"selected":true}]}
```

`refresh=true` 会并发测试最多 32 个普通节点，命令可能需要数秒。`running`
表示 controller 可达且模式不是 `direct`，不是 mihomo systemd 服务状态。

### 切换节点、模式和开关

```json
{"t":"cmd","id":3,"name":"proxy.switch","args":{"name":"node-b"}}
{"t":"cmd","id":4,"name":"proxy.mode","args":{"mode":"global"}}
{"t":"cmd","id":5,"name":"proxy.onoff","args":{"on":false}}
```

`proxy.onoff true` 切到 `rule`，`false` 切到 `direct`。还提供底层兼容命令：

- `proxy.groups {}`
- `proxy.nodes {"group":"PROXY"}`
- `proxy.select {"group":"PROXY","name":"node-b"}`
- `proxy.delay {"name":"node-b","timeout_ms":3000}`

代理修改和延迟测试使用互斥锁；冲突返回 `E_BUSY`。controller 相关错误码为
`E_PROXY_AUTH/CONFIG/CONNECT/DATA/HTTP/NOTFOUND/SIZE/TIMEOUT`。

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

## App 内文件管理

`file.caps` 返回文件能力、1536 字节分块上限、最大传输大小和文本预览上限。
所有路径必须是绝对路径；该接口与 root 终端权限相同，不限制在某个目录内。

目录和普通操作：

- `file.list {"path":"/root","offset":0,"limit":24}`：分页列目录。
- `file.stat {"path":"/root/a.txt"}`：读取类型、大小、时间和模式。
- `file.read {"path":"/root/a.txt","offset":0,"length":1536}`：Base64 分块读取。
- `file.hash {"path":"/root/a.txt"}`：计算 SHA256。
- `file.mkdir {"path":"/root/new-dir"}`。
- `file.rename {"path":"/root/a","target":"/root/b"}`：不覆盖已有目标。
- `file.remove {"path":"/root/a","confirm":"DELETE"}`：仅文件、符号链接或空目录。

上传为同目录临时文件加原子提交：

```json
{"name":"file.upload.begin","args":{"path":"/root/a.bin","size":2048,"overwrite":false}}
{"name":"file.upload.chunk","args":{"upload_id":"...","offset":0,"data_b64":"..."}}
{"name":"file.upload.commit","args":{"upload_id":"...","sha256":"..."}}
```

偏移必须严格连续，单块不超过 1536 字节。`commit` 只有在声明大小完整且可选
SHA256 一致时才用 `os.replace` 发布目标文件。断线或 `file.upload.abort` 会删除临时文件。
文件错误码为 `E_FILE_ARGS/EXISTS/HASH/IO/NOTFOUND/OFFSET/PERM/SIZE/STATE/TYPE`。
