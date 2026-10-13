# 终端与卡片并行协议（板端）

当前 App 显式使用 json-mux-v1，可在终端运行期间刷新卡片，并在
同一蓝牙连接上同时运行最多 4 个独立 PTY。网关另提供独立的单次命令接口
`term.exec`，它不打开 PTY；没有改变现有令牌认证或 root 权限边界。

## 协商与兼容

连接仍采用 UTF-8 NDJSON，单行上限 4096 字节（不含换行）。hello 保留
`term_protocol: "escaped-v1"`，新增
`term_protocols: ["escaped-v1", "json-mux-v1"]`。
客户端必须先完成原有令牌认证。

打开终端：

```json
{"t":"cmd","id":1,"name":"term.shell","args":{"protocol":"json-mux-v1","shell":"/bin/bash","cols":80,"rows":24}}
```

成功响应 data 包含原有字段，以及 `protocol` 和不透明会话标识 `sid`。
之后发送 `term.ready` 事件，data 包含 sid、protocol、cols、rows。
先响应、再 ready、再输出。不要把 pid 当作会话标识。

选择新协议后，该连接始终使用 NDJSON；终端退出后也不切回原始字节流。
每次 `term.shell` 获得新 sid，同一连接最多同时 4 个；达到上限返回 `E_BUSY`。
若需使用旧协议，必须重新连接。

## 终端消息

下列示例中的 SESSION 替换为打开终端时返回的 sid。

```json
{"t":"cmd","id":2,"name":"term.input","args":{"sid":"SESSION","b64":"bHMK"}}
{"t":"cmd","id":3,"name":"term.resize","args":{"sid":"SESSION","cols":100,"rows":30}}
{"t":"cmd","id":4,"name":"term.close","args":{"sid":"SESSION"}}
```

输入采用严格 Base64，每条解码后最多 2048 字节。成功响应
`{"accepted":3}` 表示进入板端输入缓冲，并不表示命令已执行。
输入缓冲上限 65536 字节，满时整块拒绝并返回 E_BUSY；客户端应限制未确认输入，
只在明确收到 E_BUSY 后重试该块。断线或确认丢失时不能自动重放输入。
resize、close 返回 `{"accepted":true}`；close 的完成以 term.exit 为准。
resize 在 PTY 线程应用，沿用原有尺寸范围裁剪。
错误或过期 sid、已关闭的会话返回 E_STATE；无效参数返回 E_ARGS。

板端事件：

```json
{"t":"ev","name":"term.output","data":{"sid":"SESSION","b64":"aGVsbG8NCg=="}}
{"t":"ev","name":"term.exit","data":{"sid":"SESSION","code":0,"reason":"exit"}}
```

输出每块最多 2048 原始字节，完整二进制保真，不解析控制字符。
客户端应先 Base64 解码，交给终端字节流处理器，不能逐块独立解码 UTF-8
（多字节字符可能跨块）。exit 的 reason 可能为 exit、close、pty_closed；
Linux PTY 在退出时可能以 EIO 表示关闭，因此正常退出也可能为 pty_closed。
退出码来自 waitpid，负数表示信号或无法取得退出状态。
客户端根据 sid 隔离会话，按 id 对应响应，不能假定响应按请求顺序到达。

## 并发、背压与断线

新协议下普通卡片命令进入有界 FIFO 队列，由独立工作线程逐个执行；队列最多
保留 32 个请求，只有队列确实已满时才返回 E_BUSY。这样文件页同时发出的
`storage.list`、`file.caps` 和 `file.list` 不会被误报为“终端任务在跑”。
终端输入、尺寸调整、关闭和 dev.ping 继续由接收线程处理。订阅事件仍可推送。
客户端须继续发送心跳（建议沿用 20 秒 dev.ping）；终端输出不替代客户端心跳。

每个终端由独立 PTY 线程收发，每个输出块释放发送锁并让出执行时间，
允许响应和事件交错发送。没有无界输出队列：连接慢时暂停读取 PTY，
可能使大量输出的命令变慢。共享连接仍存在带宽竞争和队头阻塞，
不保证硬实时延迟，也不保证在发送超时断线时保留未发送的输出。

关闭或断线时停止终端，向前台进程组发送 SIGHUP，并回收 shell。
这不是完整的作业沙箱：主动脱离终端的后台守护进程不保证被终止。
已经开始的普通系统操作不强行取消，仍执行其原有超时或回滚流程，
结果不会发到新连接。客户端不得在断线后盲目重试修改操作。
终端中的任意 root 命令不受卡片操作锁约束。

现有普通命令的大响应仍沿用原有截断规则。本协议仅对交互终端数据分块，
不提供通用大响应分块。`term.exec` 的输出单独受 2048 字节上限约束并以
Base64 返回，不能用它传输大文件。

## 验证

板端部署后运行 `sudo bt-remote doctor` 检查服务与本地认证。
协议联调需验证旧协议兼容、慢命令期间心跳和终端输入、输入缓冲上限、
旧 sid 拒绝、终端退出和重开，以及断线后的进程回收。
仍需在实际蓝牙板子上验证刷屏时延、移动端接入和长时间连接稳定性。
