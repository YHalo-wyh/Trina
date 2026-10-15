# ADR-0002：动作协议用文本 JSON，不用 function-calling

- 状态：已采纳（M1）
- 相关：ADR-0001

## 背景
模型需要输出"下一步调用哪个工具、参数是什么"。可选：原生 function-calling，或文本协议（约定输出 JSON）。

## 决定
约定模型每步只输出一个 JSON 对象：

```json
{"thought": "...", "test_id": "sqli_probe", "action": "sqli_test", "params": {"url": "...", "param": "id"}}
```

结束用 `{"thought": "...", "final": {...}}`。解析器做多级容错（整段 → 平衡括号候选 → 代码块 → 报错回灌），
并把 `test_id` 允许出现在 `params` 里（模型习惯）由执行前统一摘出。

## 后果
- 正面：换模型不改协议（本项目默认 omp/deepseek，可换任意 OpenAI 兼容端点）；动作可被审计记录成"思考-动作-参数"三元组；
  `--record/--replay` 能完整复现决策序列（离线回归的基础设施）。
- 负面：需要容错解析；模型偶尔输出非法 JSON（用"只输出 JSON"纠正提示 + 计数处理）。
- 已知边界：文本协议下无法强制结构化输出，故所有参数仍必须过 `ToolRegistry` 校验。

## 被否决的方案
- 原生 function-calling：跨模型/跨 providers 行为差异大，且工具 schema 会随模型变化，难以做统一审计与回放。
- 让模型直接写 shell 命令：与"不接受任意 Shell 字符串"的授权要求直接冲突。
