# pwno-mcp（本机已接入 OMP）

pwno-mcp 是一个状态化的 **GDB + pwndbg MCP 服务器**：把持久调试会话、寄存器/栈/堆上下文、
内存读写、进程与 pwncli 会话暴露成 MCP 工具，让 Agent 直接驱动调试器，而不用手搓 tmux + gdb。

- OMP 配置：`~/.omp/agent/mcp.json` → `http://127.0.0.1:5500/mcp`
- Codex 配置：`~/.codex/config.toml` → `[mcp_servers.pwno-mcp]`
- 服务：systemd user unit `pwno-mcp.service`（`enable` + `Linger=yes`，开机自启）
  - `systemctl --user {status,restart,stop} pwno-mcp`
  - 手动兜底启动：`~/bin/start-pwno-mcp`
- 源码：`/home/wyh/mcp-tools/pwno-mcp`（editable 安装）
- 解释器：`/home/wyh/.venvs/pwno-mcp/bin/python`

> **MCP 服务器在会话启动时绑定。** 改完 `mcp.json` 或刚启动服务后，已在运行的会话看不到工具，
> 需要 `/mcp reload`（OMP 斜杠命令）或重启会话。新会话会自动带上（实测报告 36 个工具）。

## 工具清单（36 个）

| 分组 | 工具 |
|---|---|
| 会话 | `create_debug_session` `list_debug_sessions` `close_debug_session` `get_session_info` |
| 加载/执行 | `set_file` `run` `attach` `step_control` `finish` `jump` `until` `return_from_function` |
| 断点/检查 | `set_breakpoint` `get_context` `get_memory` `gdb_poll` `gdb_interrupt` |
| 任意 GDB 命令 | `execute` |
| 进程 | `spawn_process` `list_processes` `get_process` `kill_process` `run_command` |
| Python 沙箱 | `execute_python_code` `execute_python_script` `install_python_packages` `list_python_packages` |
| Exploit 驱动 | `pwncli` `sendinput` `checkoutput` `checkevents` `pwncli_stop` `list_pwncli_sessions` |
| 其他 | `fetch_repo` `get_retdec_status` `get_decompiled_code` |

会话默认名 `default`；`set_file` 之后才能下断点。

## 典型调用序列

```python
create_debug_session()
set_file(session_id="default", binary_path="/mnt/f/pwn宝/<题>/pwn")
set_breakpoint(session_id="default", location="main")
run(session_id="default")
get_context(session_id="default", context_type="registers")   # 也可 stack/disasm/backtrace/all
get_memory(session_id="default", address="0x7fffffffde00", count=64)
execute(session_id="default", command="heap")                 # 任意 pwndbg 命令
```

## 两个必须知道的环境坑（本机已修好）

1. **debuginfod 会卡死 MI 通道。** gdb 默认尝试联网下载 separate debug info，一旦未命中就长时间阻塞，
   导致每个 MCP 调试调用报 `GdbTimeoutError: Did not get response from gdb after 7.0 seconds`。
   修法：用 `~/bin/pwndbg-nodb` 作为 `PWNO_GDB_COMMAND`（内部
   `-iex "set debuginfod enabled off"` + `DEBUGINFOD_URLS=""`）。**不要**把这个 wrapper 让用户手工
   `gdb`/`pwndbg` 用，只给 MCP。
2. **`PWNO_WORKSPACE_ROOT` 别设成 `/`。** `create_services()` 会 `makedirs` 这个目录，`/` 不可写会直接
   启动失败（`PermissionError: '/workspace'`）。指向真实工作目录（当前为 `/mnt/f/pwn宝`）。

另外：
- **中文路径没问题**（`/mnt/f/pwn宝/...` 实测可用），前提是 debuginfod 已关闭。
- **一次只留一个 inferior。** 上一个进程还在跑（比如停在菜单 `>> ` 等输入）时，它的输出会污染 MI 通道，
  下一个 `set_file` 会报 `Missing closing quote in '^error,msg=">> 1. Add/'`。换题前先
  `kill_process(pid=...)` 或 `execute("kill")`，别让旧 inferior 挂着。
- 传 HTTP 时带 `Accept: application/json, text/event-stream`，并用 `initialize` 返回的
  `mcp-session-id` 头做后续调用。
- **不要用 stdio 传输**：pwnomcp 的日志走 `RichHandler`（rich 默认输出 stdout），会污染
  stdio 的 JSON-RPC 帧。HTTP 传输没有这个问题。

## 与 ctf-pwn skill 的分工

- 需要**结构化调试状态**（寄存器/堆/内存/断点逐步）→ 用 pwno-mcp 工具。
- 需要**技术选型**（glibc 版本决策、House-of/FSOP 路线）→ 读 `upstreams/ctf-skills/ctf-pwn/SKILL.md`
  与其 references，或用独立 `ctf-pwn` skill 的 tmux 持久 GDB 工作流。
- AWDP 修补（`update.sh` / patch 二进制）→ 读 `awdp-pwn.md`。
