# 临时任务模块

靶场、比赛平台和一次性 Harness 不进入 Trina 核心，也不把平台合同追加到全局 system。它们以仓库外 Capability 包存在，任务结束后可以直接移除目录。

## 目录约定

```text
temporary-platform/
├─ capability.yaml
├─ AGENT.md
└─ adapter.py
```

- `capability.yaml` 声明接口、运行时、动作等级、依赖和版本。
- 同目录存在 `AGENT.md` 且清单未显式设置 `guidance` 时，Catalog 自动把它作为本模块 Guidance。
- `adapter.py` 可以实现 CLI 或 MCP stdio 适配器；执行仍必须经过 Capability Runtime、`ToolRegistry`、护栏、审计和超时。
- 令牌、平台地址和题目状态由适配器自己的受控输入提供，不写入 `AGENT.md`。

示例清单：

```yaml
id: temporary.platform.submit
version: 1.0.0
description: 读取当前题目并提交候选答案
provides: [platform.task, platform.submit]

interface:
  name: platform_submit
  params:
    answer:
      type: str
      required: true
      description: 待验证的候选答案

runtime:
  type: cli
  command: [python, ./adapter.py]
  input: json
  output: json

policy:
  action_level: verify
  filesystem: manifest
  timeout: 30

requires:
  tools: [python]
```

## 加载与生命周期

模块目录放在以下任一位置即可发现：

- `<runtime_root>/capabilities`
- `~/.trina/capabilities`
- `TRINA_CAPABILITY_DIRS` 指定的临时目录

任务开始时，Capability Fabric 按 Intent 选择最小能力集合并签发 Worker/Task 租约；只有被选中的模块才读取 `AGENT.md`、暴露工具 Schema。任务结束、租约过期或显式释放后，旧能力不可继续调用。删除临时目录即可卸载，不需要修改 Commander、Worker 或核心工具注册表。

## 提示词边界

`AGENT.md` 是任务数据，不是身份或全局规则：

1. Catalog 发现阶段只记录相对路径，不读取全部模块说明。
2. Worker 获得租约时才读取对应 `AGENT.md`。
3. Guidance 通过 `extra_rules` 进入首条 `user` 任务合同；不会拼入 system。
4. 动态题目、Token、目标和提交状态继续通过运行上下文或工具结果传递。

因此，一个平台模块不能永久改变普通对话、其他题目或之后复用的 Worker。

## 验证

```powershell
python -m unittest tests.test_capabilities tests.test_swarm tests.test_worker_dashboard -v
```

回归覆盖自动发现 `AGENT.md`、租约时 JIT 读取、system 隔离、能力释放，以及多个无依赖任务并发占用多个 Worker。

架构取舍、核心移除范围和主动并行边界见 [ADR-0037](../design/adr/0037-proactive-workers-and-pluggable-task-modules.md)。
