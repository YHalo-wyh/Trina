# ADR-0006：计划器与控制器分离，只接受注册 target_id / test_id

- 状态：已采纳（P3 要求）

## 背景
"自主"与"受控"天然冲突。课程 P3 的硬要求是：**计划器（模型）提出，独立控制器批准**；
只接受注册的 `target_id` 与 `test_id`，不接受任意 URL 或任意 Shell 字符串；
授权检查发生在**执行层**，而不是任务开始时读一次。

## 决定
- `configs/ranges/*.yaml` 增加两个段（人写入）：
  - `targets:` → 注册目标（`id` / `host` / `port` / `url`）
  - `tests:` → 注册检查项（`id` / `target_id` / `tool` / `level` / `params`）
- `trina/policy.py#AuthorizationController` 在执行层逐动作裁决：注册 → 预算 → 撤权；
  模式 `free` / `guided`（默认，按工具反查注册项并统计覆盖，未注册动作仍放行）/ `strict`（必须给出注册 `test_id`）。
- 撤权语义：`revoke(target_id)` 之后对该目标的检查一律拒绝，并提示"停受影响任务、如实报告未覆盖项、
  新目标必须重新批准"——**减权，不绕路**（换地址/换工具都会被拒）。
- 模型没有写权限：注册表与预算是文件，模型只能用 `test_id` 提议。

## 后果
- 正面：越界请求在执行层被阻断并记录 `boundary_events`；覆盖统计（已测/未测/原因）自动产出；
  撤权演练可复现（`tests/test_policy.py`）。
- 负面：strict 模式下需要先维护注册表（这是课程要求的"教师注册表"角色）；
  模型有时把 `test_id` 写进 `params`（已兼容）。
- 已验证：未注册 `test_id` → 拒绝；未注册目标 → 拒绝；命中注册项 → `executed` 记录并计入预算。

## 被否决的方案
把注册表塞进提示词让模型"自觉遵守"：无法阻止模型发明新的 `test_id` 或换 URL，也无法统计覆盖。
