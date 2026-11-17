# 动作预算收尾与最新输出尾随验证

- 日期：2026-11-16（Asia/Shanghai）
- 状态：本人实际执行
- 基线：`4e440cf`
- 范围：Chat 动作预算收尾、流式内容自动尾随、主动历史锁定

## 实际结果

1. Replay Chat 将工具动作上限设为 1。第一次标准请求携带 tools 并执行真实注册工具；预算耗尽后第二次请求不携带 tools，返回的 `final.summary` 被转换为正式自然语言答复。实际模型调用 2 次、工具调用 1 次，消息历史没有未配对工具调用。
2. 内容区写入超过一屏的历史后持续更新流式尾块，视口首行随内容增长且 `_scrollback=0`；主动向上滚动 3 行后继续增长尾块，视口首行保持不变；滚到底后恢复尾随并显示最新行。
3. 既有真实 VT 鼠标滚轮、终端滚动区域、Unicode 拖选复制、流式增量和固定底栏回归继续通过。

## 验证命令

```powershell
python -m unittest tests.test_interactive.TestChatAgent.test_tool_budget_reserves_a_tool_free_summary_call
python -m unittest tests.test_transcript tests.test_terminal_ui tests.test_llm_streaming tests.test_stream_latency
python -m scripts.verify_contract
python -m unittest discover -s tests
```

完整回归结果：648 项通过，10 项因当前 Windows 环境缺少 Linux ELF 工具链而跳过。机器规约结果：6/6 交付物、24 项需求、69 条规则、184 个测试引用通过。

## 边界

- 收尾请求仍会产生一次真实模型调用和相应用量，只在工具动作预算刚好耗尽且尚无答复时发生。
- CTFY 等平台调用次数属于硬预算的入口不增加该请求。
- 用户主动上翻历史时不会自动跳到底；滚到底、`Ctrl+End` 或发送下一条消息后恢复。
