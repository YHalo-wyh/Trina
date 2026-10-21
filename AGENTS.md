# AGENTS.md · 项目约定

本仓库是 **Trina（全自动渗透测试 Agent）**。在本目录工作时：

## 快速定位

- 想知道能做什么 → 读 `.omp/skills/pentest-agent/SKILL.md`（已作为技能自动加载）；或 `python3 -m trina tools`
- 想跑一次渗透 → 优先用扩展工具 `trina_run`（等价于 `trina play --target ...`）；想边聊边干 → `trina`（对话式控制台，说人话即可，它会自己调工具）
- 改代码前 → 先读 `docs/ARCHITECTURE.md` 的模块契约；护栏相关改动必读 `docs/GUARDRAILS.md`

## 不可协商的规则

1. **不要在 `trina/scope.py` 之外实现网络请求或命令执行**。所有出网/执行必须经 `ctx.http` / `run_external` / `ctx.guard`；
   否则会绕过审计与护栏，属于架构破坏。
2. **新工具必须注册进 `ToolRegistry`**（子模块导出 `TOOLS: list[Tool]`，由 `trina/tools/__init__.py#build_registry` 装配），
   并声明 `action_level` 与参数表 —— 参数校验、护栏、审计、超时都由执行链统一处理。
3. **不要把授权边界写进提示词**。提示词里的范围描述来自 `TrinaConfig.scope_prompt_block()`，与真实判据同源；
   任何"临时放开一下"的想法都应改为修改 `configs/trina.yaml` 并留下变更记录。
4. **测试**：护栏/审计/注册表/大脑的改动必须带单元测试（`python3 -m unittest discover -s tests`）。
   禁止为了让测试通过而放宽护栏；测试断言的是"必须被拒绝"的行为。
5. **报告与审计是交付物**：`reports/<run_id>_report.{md,html}`、`logs/<run_id>.jsonl`、`runs/<run_id>/state.json`。

## 代码风格

- 中文 docstring/注释；`from __future__ import annotations`；dataclass；类型注解。
- 只要 Python 标准库 + 已装依赖（requests / paramiko / bs4 / aiohttp / pyyaml / rich）。
- 单文件聚焦一个主题；新增能力优先扩展已有模块而不是再造一套约定。

## 常见坑

- 靶机没起 → `python3 -m trina ranges status lab`，或 `python3 -m trina lab --http --port 9099`
  （`trina eval run` 会按场景里的 `fixtures` 自动拉起/回收 lab 靶机，评测不需要手工起）
- 端口 9099 被占/被清理 → 换端口，并同步更新 `configs/ranges/lab.yaml` 与 `trina/eval/scenarios/*.yaml` 的目标
- WSL 无 docker → Vulhub/VulnStack 用宿主机或 Docker Desktop 集成
- LLM 成本 → `trina run --max-steps 8` 做小规模冒烟；纯离线验证用 `--replay <脚本>`（`--record` 可录制）
