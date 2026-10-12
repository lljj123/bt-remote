# 网关协议 v2 增量

本文只描述 v1 基础协议之上的新增命令。传输仍为 UTF-8 NDJSON，认证、
4096 字节帧上限、响应 `id` 匹配和 `escaped-v1` 终端相位均保持不变。

认证成功后的 `caps` 新增 `proxy`：

```json
{"t":"auth","ok":true,"caps":["sys","svc","net","pwr","log","term","proxy"]}
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
