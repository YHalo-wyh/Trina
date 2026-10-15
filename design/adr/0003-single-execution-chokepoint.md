# ADR-0003：工具执行单一收口（ToolRegistry.execute）

- 状态：已采纳（M1）

## 背景
17 个工具里既有内置 Python 引擎，也有外部二进制（nmap/sqlmap/nuclei/hydra）。若每个工具自行发请求或执行命令，
护栏与审计必然出现"漏网之鱼"——而护栏只要有第二个入口就等于没有。

## 决定
所有工具执行走同一个执行链：

```
参数校验 → 控制器（授权：注册/预算/撤权）→ 护栏（范围/等级/破坏性/硬禁止）→ 审计 tool.start
        → 带超时执行 → 事实吸收/证据入账 → 审计 tool.end → 结果归一
```

工具内部再发子请求时，必须用本次调用被放行的**有效等级**（`ctx.effective_level`），不能用硬编码等级。

## 后果
- 正面：新增工具自动获得校验、护栏、审计、超时、限速与事实吸收；越界行为不可能"绕开"。
- 负面：工具不能自由发挥（要按 `ToolSpec` 声明参数与等级）——这是刻意的约束。
- 教训：早期版本里工具内部硬编码 `exploit` 等级，导致"外层按 verify 放行、内层自拒"（已修，见 `tests/test_eval_report_lab.py::test_internal_subrequest_uses_effective_level`）。

## 被否决的方案
允许工具直接 `requests.get` / `subprocess.run`：开发更快，但审计与护栏无法保证，且评测的"被拒动作数"指标失去意义。
