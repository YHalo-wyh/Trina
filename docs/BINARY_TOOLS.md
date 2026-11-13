# radare2 + GDB 原生二进制工具链

日期：2026-11-13。实现入口：`trina/binary/`、`trina/tools/binary.py`、`trina/tools/external.py`。

## 1. 运行模型

Binary Worker 仍是通用 Worker：只有任务快照包含 `category.binary` 时，Core 后方才追加 `binary-role-v1` Role，并绑定本页工具。Role 说明地址、证据和停止条件，不规定固定逆向流水线。JSON Action 与原生 Tool Calling、`prompt_layers=false|true` 均走同一 ToolRegistry。

原生程序不由工具模块直接 `Popen`。`ToolRegistry.execute` 完成参数校验、能力租约、审计和超时后，`ManagedPipe` 经 `ScopeGuard.check_argv` 启动 r2 或 GDB。Linux 默认再套 bubblewrap；缺少依赖或隔离器时返回机器错误，不模拟结果，也不静默降级。

## 2. 注册工具

| 工具 | 主要输入 | 输出与状态 |
|---|---|---|
| `binary_open` | `path`、`analysis=quick|standard|deep`，裸文件可加 `arch/bits/base_va` | r2 只读持久会话、格式、架构、入口、能力探针 |
| `binary_overview` | 会话、`summary|sections|imports|strings`、过滤和分页 | 有界索引，原始 JSON 进入证据工件 |
| `binary_functions` | 会话、过滤和分页 | 函数索引 |
| `binary_function` | 会话、严格静态地址、局部视图 | 指令、函数摘要或基本块 |
| `binary_xrefs` | 会话、严格静态地址、`to|from` | 交叉引用 |
| `debug_start` | ELF、参数、可选文本 stdin 文件、是否停入口 | GDB/MI3 持久会话；保留 ASLR |
| `debug_control` | 会话、断点/运行/继续/单步/中断/停止 | 实际 inferior 状态和最近 stop 事件 |
| `debug_inspect` | 寄存器/内存/栈/帧/反汇编/映射 | MI 结构化结果、映射与装载偏移 |
| `binary_session` | `list|close` | 当前任务会话快照或回收结果 |
| `binary_execute` | 文件、argv、stdin、时限 | 独立进程的真实退出码和有界输出；不等于平台解题成功 |

`/tool binary_open --target ./challenge --param analysis=standard` 在交互控制台中复用当前 ChatAgent，后续 `/tool binary_functions --param session_id=...` 可继续使用同一会话。裸 `trina tool` 只允许一次性 `binary_execute`，避免命令退出后返回无效持久句柄。

## 3. 结构化协议与地址

radare2 采用其官方 stdio NUL 分帧协议，命令模板固定为 JSON 查询；不用 `r2pipe.open()` 的原因是后者会在 ToolRegistry 之外自行创建进程。GDB 使用 MI3，token 响应、异步 running/stopped、console/target stream 均解析为结构化记录；`info proc mappings` 只作为 MI console 中缺少可移植映射 API 时的固定探针。

地址使用 `AddressRef` 显式给出：

```json
{
  "file_offset": "0x1080",
  "static_va": "0x2080",
  "runtime_va": "0x702080",
  "module": "/ctfy/work/challenge",
  "load_bias": "0x700000",
  "arch": "62",
  "pointer_bits": 64
}
```

PIE 的 `static_va` 只有在 inferior 停止、主模块 PT_LOAD 与真实映射匹配后才转换为 `runtime_va`。动态地址参数必须写 `static_va:0x...`、`runtime_va:0x...` 或 `symbol:name`；表达式、命令分隔符和负数不进入后端。

## 4. 生命周期、隔离与恢复

- 会话归属于 `run_id + workspace`，另一个 Worker 即使知道句柄也得到 `SESSION_EXPIRED`。
- 默认每任务最多两个 r2、一个 GDB；每个会话串行查询、闲置 600 秒回收。
- 能力租约释放、任务停止、工具超时、调试异常或 ChatAgent 关闭时终止整个进程树。
- 对话快照只保存描述性会话元数据。`/resume` 将旧句柄标为 `EXPIRED`，不恢复 PID、不重放命令。
- 文件哈希在每次使用前复核；分析中被替换后会话失效。
- Linux bubblewrap 仅挂载本任务工作区为可写，项目源码只读，隐藏其他任务目录，不共享网络或宿主 HOME。`binary.sandbox=none` 是操作者显式依赖外层沙箱，不代表 Trina 自带隔离。

完整 native 响应写入 `runs/<run_id>/binary-*.json`，返回模型的 observation 只保留摘要、分页游标和 `evidence_ref`。错误同样写工件并记录 `binary.failure`；审计记录命令数、字节数和耗时。

## 5. 本地和平台成功判定

静态分析、动态调试、候选恢复和 `binary_execute` 组成验证闭环，但 `binary_execute.code == 0` 只证明被测程序本次正常退出。工具固定返回 `verified_solve=false`，不会从 stdout 自动吸收 Flag。

CTFY v3 运行器只在平台提交 API 返回 `correct=true` 时计为解题；本地 ELF 端到端用例则同时执行正确候选和错误候选，验证夹具自己的权威分支。两种证据不能相互冒充。

## 6. 依赖、配置与限制

```yaml
binary:
  sandbox: bwrap
  analysis_timeout: 30
  query_timeout: 10
  debug_timeout: 20
  idle_timeout: 600
  max_file_bytes: 67108864
  max_output_bytes: 8388608
  max_memory_bytes: 4096
  max_r2_sessions: 2
  max_gdb_sessions: 1
```

当前真实动态测试覆盖 Linux x86-64 PIE/ASLR、断点、寄存器、内存、帧、stdin、SIGSEGV、超时、两个 Worker 和恢复失效。首版不支持附加任意 PID、跨 ISA QEMU、多个 inferior、操作系统检查点、PE 动态调试或任意二进制 stdin。GDB 在 `startup-with-shell off` 下只接受不含空白、引号和反斜线的 argv；复杂数据使用 `stdin_file` 或 `binary_execute`。Windows 单元测试验证协议和失败语义，原生 ELF 测试明确跳过。

参考：[radare2 r2pipe/stdio](https://book.rada.re/scripting/r2pipe.html)、[GDB/MI](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI.html)。实际验证入口为 `tests/test_binary.py` 和 `scripts/verify_binary_stack.py`。
