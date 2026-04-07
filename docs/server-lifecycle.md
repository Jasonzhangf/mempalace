# MemPalace Server + Thin CLI Architecture

## 设计目标

- **干净生命周期**：无孤儿进程、无僵尸进程
- **自动关闭**：idle 时自动关闭，节省资源
- **项目隔离**：多项目共享 server，数据隔离（wing）
- **零配置**：client 自动检测/拉起 server

## 架构图

```
┌────────────────────────────────────────────────────────────────┐
│                     Server (单实例)                              │
│                                                                │
│  ┌───────────┐  ┌──────────────┐  ┌──────────────────┐        │
│  │ ChromaDB  │  │ Embedding    │  │ Idle Timer       │        │
│  │ Palace    │  │ Model Cache  │  │ (auto shutdown)  │        │
│  └───────────┘  └──────────────┘  └──────────────────┐        │
│                                                                │
│  PID: ~/.mempalace/server.pid                                  │
│  Socket: ~/.mempalace/server.sock                              │
│  Last Activity: ~/.mempalace/server.last_activity              │
└──────────────────────┬─────────────────────────────────────────┘
                       │ Unix Socket / HTTP
       ┌───────────────┼───────────────┬───────────────┐
       │               │               │               │
  ┌────┴────┐    ┌��───┴────┐    ┌────┴────┐    ┌────┴────┐
  │ Client  │    │ Client  │    │ Client  │    │ Client  │
  │ Wing A  │    │ Wing B  │    │ Wing C  │    │ Wing D  │
  │ (thin)  │    │ (thin)  │    │ (thin)  │    │ (thin)  │
  └─────────┘    └─────────┘    └─────────┘    └─────────┘
  cd ~/A         cd ~/B         cd ~/C         cd ~/D
  mem search     mem search     mem search     mem search
```

## 生命周期状态机

```
                 ┌─────────────┐
                 │   STOPPED   │
                 └─────────────┘
                       │
          client 检测无 server
                       │
                       ▼
                 ┌─────────────┐
     ┌──────────│   RUNNING   │◄──────────┐
     │          └─────────────┘           │
     │                │                   │
     │    idle > 300s │   client activity │
     │                ▼                   │
     │          ┌─────────────┐           │
     └──────────│   IDLE      │───────────┘
                └─────────────┘
                      │
           idle > 600s (shutdown)
                      │
                      ▼
                ┌─────────────┐
                │   STOPPED   │
                └─────────────┘
```

## 防孤儿机制

### 1. PID File + Stale Detection

```
~/.mempalace/server.pid  →  PID: 12345, Started: 2025-04-07T20:00:00
~/.mempalace/server.sock →  Unix socket (or localhost:7654)
```

Client 每次请求前：
1. 读取 PID file
2. 检查进程是否存活 (`os.kill(pid, 0)`)
3. 检查 socket 是否可连
4. 若 stale → 删除 PID + socket → 启动新 server

### 2. Server 自检（Parent Watchdog）

Server 启动时记录：
- `started_by_client_pid`: 启动它的 client PID
- 可选：`parent_pid`: 父进程 PID（若 client fork）

Server 定期检查：
- 若 `started_by_client_pid` 已死 → 进入 idle shutdown
- 若收到 SIGTERM/SIGINT → graceful shutdown

### 3. Idle Timer（兜底）

```
IDLE_THRESHOLD = 300s  # 5 分钟无活动 → 进入 idle
SHUTDOWN_THRESHOLD = 600s  # 10 分钟无活动 → shutdown
```

每次 client 请求 → 更新 `last_activity`
Server 定���检查 → 若超时 → shutdown

### 4. Keep-Alive（可选）

Long-running client（如 `mempalace mine` 大目录）：
- 每 60s 发送 `POST /keepalive`
- Server 收到 → 重置 idle timer

### 5. Graceful Shutdown

```
Signal Handler:
  SIGTERM → shutdown_flag = True
  SIGINT  → shutdown_flag = True

Shutdown Sequence:
  1. 停止接受新请求
  2. 等待现有请求完成（最多 30s）
  3. 关闭 ChromaDB 连接
  4. 删除 PID file + socket file
  5. 退出
```

## 项目隔离（Wing 模型）

| 项目 | Wing 名称 | 数据位置 |
------|----------|---------|
| ~/projects/appA | `appA` | ChromaDB: wing="appA" |
| ~/projects/appB | `appB` | ChromaDB: wing="appB" |
| ~/projects/appC | `appC` | ChromaDB: wing="appC" |

**共享**：
- Server 进程（单实例）
- Embedding model（加载一次）
- ChromaDB 文件（`~/.mempalace/palace/`）

**隔离**：
- 每个 wing 的数据在 ChromaDB metadata 中标记
- 搜索时按 wing 过滤
- 可配置不同 palace 目录（多 server 实例）

## Client 行为

```python
def ensure_server_running():
    pid_file = Path("~/.mempalace/server.pid")
    socket_file = Path("~/.mempalace/server.sock")

    # 1. 检查 PID file
    if pid_file.exists():
        pid = read_pid(pid_file)
        if is_process_alive(pid) and socket_is_connectable(socket_file):
            return  # server 已运行
        else:
            # stale → 清理
            pid_file.unlink()
            socket_file.unlink(missing_ok=True)

    # 2. 启动 server
    subprocess.Popen(
        ["mempalace", "serve", "--daemon"],
        start_new_session=True  # detach
    )

    # 3. 等待 server ready
    wait_for_socket(socket_file, timeout=10)

def send_request(endpoint, data):
    ensure_server_running()
    # 通过 Unix socket 发送请求
    ...
```

## Server API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Server status + wings overview |
| `/search` | POST | Semantic search |
| `/mine` | POST | Mine files |
| `/add` | POST | Add drawer manually |
| `/kg/add` | POST | Add knowledge triple |
| `/kg/query` | POST | Query knowledge graph |
| `/keepalive` | POST | Reset idle timer |
| `/shutdown` | POST | Graceful shutdown |

## 配置

```bash
# 默认配置
MEMPALACE_SOCKET=~/.mempalace/server.sock
MEMPALACE_PID=~/.mempalace/server.pid
MEMPALACE_IDLE_TIMEOUT=300  # seconds
MEMPALACE_SHUTDOWN_TIMEOUT=600  # seconds
MEMPALACE_PALACE=~/.mempalace/palace
```

## 命令

```bash
# 启动 server（手动）
mempalace serve                    # 前台运行
mempalace serve --daemon           # 后台运行
mempalace serve --port 7654        # HTTP mode
mempalace serve --socket ~/custom.sock  # 自定义 socket

# 管理 server
mempalace stop                     # 发送 shutdown 请求
mempalace status                   # Server + wings 状态
mempalace restart                  # stop + serve

# 项目操作（自动拉起 server）
mempalace init ~/projectA          # 创建 wing
mempalace mine ~/projectA          # 挖掘 → 自动确保 server
mempalace search "query"           # 搜索 → 自动确保 server
mempalace add "content"            # 添加 → 自动确保 server
mempalace wake-up                  # 加载 → 自动确保 server
```

## 崩溃恢复

| 场景 | 处理 |
|------|------|
| Server crash | PID file stale → client 检测 → 清理 → 重启 |
| Client crash | Server idle timer → 自动关闭 |
| 系统重启 | PID file stale → client 检测 → 清理 → 重启 |
| Force kill server | PID file stale → client 检测 → 清理 → 重启 |

## 实现文件

```
mempalace/
  lifecycle.py    # PID management, stale detection, idle timer
  server.py       # HTTP/Socket server, ChromaDB management
  client.py       # Thin client, auto-ensure-server
  cli.py          # serve/stop + 其他命令改用 client
```
