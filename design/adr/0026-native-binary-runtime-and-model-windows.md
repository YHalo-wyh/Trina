# ADR-0026：原生二进制会话、平台权威验证与可配置模型窗口

- 日期：2026-11-13
- 状态：Accepted
- 关联：TR-05、TR-09、TR-19

## 背景

原有 terminal 工具能执行命令，但无法向模型提供稳定的 r2 JSON、GDB/MI 状态、地址空间或跨轮调试句柄。把 r2/GDB 操作只写进 Binary 提示词会绕过参数 Schema、工具租约、审计和超时。CTFY v3 又要求最终成功由平台提交接口确认，stdout 中的候选不能充当成绩。同时，固定 32768 上下文不能表达 DeepSeek 与已确认百万窗口模型的差异，也缺少供应商级手工设置。

## 决定

1. 在既有 ToolRegistry 中增加十个语义化 binary 工具；原生进程只由 `ManagedPipe` 经 ScopeGuard 启动。r2 使用官方 stdio 分帧协议，GDB 使用 MI3，持久会话归属单个任务与能力租约。
2. Linux 默认要求 bubblewrap。路径必须属于当前项目或任务；能力释放、超时、取消和关闭会回收进程树。恢复只继承事实和会话描述，不继承 PID。
3. 地址由 `AddressRef` 同时表达文件偏移、静态 VA、运行 VA、模块和已证实的 load bias。PIE 转换必须匹配 PT_LOAD 与实际映射。
4. Binary Role 只在绑定 binary 能力时追加到稳定 Core 后方；JSON/native 协议和两种提示词分层路径都使用同一工具契约，不固定逆向步骤。
5. CTFY 提交由 `ctfy_task` 完成，只有平台 `correct=true` 更新 solved。本地 fixture 的 accepted/rejected 分支只属于本地验证。
6. `context_window=0` 表示自动：DeepSeek 为 500,000；代码明确识别且官方文档确认具有百万窗口的 GPT、Claude、Gemini 型号为 1,000,000；未知型号保守为 32,768。`/model context` 与供应商表单可显式覆盖并随整条连接保存。

## 取舍

没有使用 `r2pipe.open()`，因为它会自行创建不受执行租约管理的子进程；当前直接实现 r2pipe 所用的 stdio 协议。没有开放 GDB console 字符串或任意表达式，而是保留语义化固定模板。这样牺牲一部分高级命令覆盖，换取机器可验证的参数和地址边界。

模型本地窗口是压缩预算，不宣称服务端一定接受该长度。已知模型表只列核对过的家族；自定义部署、mini/nano 或未知别名不会按供应商品牌猜成 1M，操作者可以显式设置。

## 兼容与限制

旧供应商 v2 记录缺少 `context_window` 时采用自动值。现有 ReAct、Commander、Worker、缓存诊断与 `/resume` 格式保持兼容。binary 会话元数据进入快照，但旧运行恢复为 EXPIRED。动态调试首版限本机 ELF、单 inferior 和文本 PTY；跨架构、PE 动态、复杂 GDB argv 与 OS checkpoint 尚未实现。

CTFY 当前只实现 instance/QA，且自动测试来自本地协议夹具；没有把它描述成线上通过。验证证据见 `evidence/20261113-native-binary/`，测试入口见 `tests/test_binary.py`、`tests/test_ctfy.py`、`tests/test_model_context.py`。
