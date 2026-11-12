# 2026-11-12 Runtime 与流式传输验证

日期：2026-11-12（Asia/Shanghai）

程序版本：0.3.0 开发工作区；基线提交 `6b14453`，本报告对应验证时的工作区改动。

执行状态：本人实际执行。

## 检查范围

OMP 模型后端删除；标准结构化消息；Chat Completions、Anthropic Messages、Responses SSE；稳定消息前缀；正文增量卡片；工具协议隐藏；Capability Fabric 发现、租约与隔离；既有执行、审计、快照与调度回归。

## 实际执行与结果

最终入口为 `python -X utf8 -m scripts.verify_contract --execute --json`。原始结果见 [verification-final.json](verification-final.json)。

| 检查 | 实际结果 |
|---|---|
| 当前机器规约 | 6/6 类交付物、21 项需求、30 条规则、35 个去重测试引用通过 |
| 课程追踪 | 31 份文档、20 项要求、8 项任务、65 个测试引用通过 |
| 完整 unittest | 442 项，39.689 秒，OK |
| 差异检查 | `git diff --check` 返回 0；存在 Git 的 CRLF 规范化提示，无差异格式错误 |
| 编译检查 | `python -X utf8 -m compileall -q trina tests scripts` 返回 0 |
| CLI 入口 | `trina run --help` 只列 `openai,replay` 后端 |

真实回环 HTTP 测试由服务器先发送正文增量并暂停，客户端回调必须在终止事件前触发；解除暂停后才完成整个请求。该检查实际经过 urllib HTTP 传输。其他协议使用确定性 SSE 样例核对消息数组、文本、用量与终止状态。终端检查运行 prompt_toolkit 输入/输出组件，断言卡片只出现一次、输入草稿保持，并检查鼠标滚轮与历史锁定。

## 首轮异常及复查

[verification-first.json](verification-first.json) 保存首轮组合检查的失败原文：442 项中 `test_upload_not_reported_on_soft404` 的第二次工具返回 `ok=False`，原断言未带具体错误。之后仅给该用例的成功断言增加 `error/summary` 诊断信息，未改变期待值或业务逻辑。

该用例单独复跑通过；第二次完整组合检查也通过。目前没有可复现证据确定首次失败根因，因此本记录保留为一次未定位的间歇失败，不宣称已修复该上传问题。

## 结论及未查范围

现有证据支持结构化消息、SSE 增量、流终止校验、卡片更新、能力租约和声明的回归组合成立。测试未调用付费模型，不证明全部供应商账户兼容、真实模型成功率或缓存收益；未采集真实桌面截图。当前仍无自动 Token 压缩、常驻 MCP 服务池或操作系统级沙箱。

系统提示词的唯一源码位于 [prompts.py](../../trina/brain/prompts.py) 的 `SECURITY_ENGINEER_SYSTEM_PROMPT`，避免另存一份文本后发生漂移。设计依据见 [ADR-0021](../../design/adr/0021-capability-fabric.md)、[ADR-0022](../../design/adr/0022-standard-messages-and-streaming.md)。
