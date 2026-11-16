# 仓库能力、配色与输出回归证据

- 日期：2026-11-16（Asia/Shanghai）
- 状态：本人实际执行
- 环境：Windows 10.0.26300、Python 3.10.11、Intel i9-14900HX、32 GiB 内存
- 基线提交：`c868c1c`

## 覆盖范围

- `workspace_git` 的状态、Diff、暂存、提交、分支、远端与 worktree 操作；
- `terminal_exec` 的 Bash argv 调用；
- `workspace_search` 的真实 ripgrep 路径与内置有界降级；
- `trina` / `contrast` / `mono` 语义配色；
- Agent 完成与后台任务终态通知。

本次完整工作树回归执行 `python -m unittest discover -s tests`，结果为 646 项通过、10 项因当前 Windows 环境缺少 Linux ELF 工具链而跳过。`python -m scripts.verify_contract` 同时验证 6/6 交付物、24 项需求、68 条机器规则和 182 条测试引用。

## 终端回放

`performance/summary.json` 来自 kitty-vt-wasm 0.2.0 的真实 ANSI 回放，单轮结果如下：

| 场景 | 观察标记 | 输出 P95 | 帧 P95 | 滚动 P95 |
| --- | ---: | ---: | ---: | ---: |
| short | 64/64 | 14.031 ms | 12.190 ms | — |
| history | 64/64 | 19.282 ms | 18.497 ms | 24.368 ms |
| four_streams | 69/96 | 15.235 ms | 13.586 ms | — |

四流压力场景没有观察到全部 96 个夹具标记，因此这里只把它保留为真实边界记录，不将其描述为“全量标记通过”。同一评测的既有 OMP 对照也未观察到全部标记；该指标需要后续调整回放完成条件后再用于严格通过判定。

通知浮层的独立 A/B 渲染证据位于 `../20261116-notification-overlay/`：隐藏态通知文本回调为 0，当前布局与无浮层控制组的 ANSI 字节流一致，说明完成提醒未改变普通输出内容。

## 未覆盖范围

- 没有实际触发每一种终端的桌面通知服务；协议单测覆盖 OSC 99、OSC 9、BEL 和重定向静默路径。
- 没有在远端仓库上执行强制推送；实现明确不提供 force push。
- 性能数字是单机回放结果，不代表模型、网络或真实工具执行耗时。
