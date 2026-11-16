# 通知浮层隐藏态渲染冒烟

- 日期：2026-11-16（Asia/Shanghai）
- 状态：本地真实渲染循环 + VT100 字节流 + kitty-vt 回放；未调用模型、网络或靶机。
- 设计：[ADR-0035](../../design/adr/0035-repository-shell-notifications.md)
- 代码：HEAD `c868c1c`，暂存树 `584af18`。渲染路径（`trina/console_ui.py`、`trina/terminal.py`、`trina/transcript.py`、`trina/notifications.py`）与暂存树一致；工作区未暂存改动不涉及这些文件。
- 环境：Windows 10.0.26300 x64；Python 3.10.11；prompt_toolkit 3.0.52；终端尺寸 120×36。

## 目的与方法

主界面的完成提醒是 `FloatContainer` 里的一个条件浮层。要回答的问题只有一个：它隐藏时会不会给正常输出增加每帧成本。单元测试和全量回归证明行为正确，但不覆盖渲染热路径。

[`smoke.py`](smoke.py) 使用生产 `EnhancedConsoleInput`，通过 `create_pipe_input()` 与 `Vt100_Output` 把 120×36 终端的 ANSI 写进缓冲区，按 40ms 节拍回放固定序列：6 段历史、16 段累计思考、32 段累计正文/代码、12 次工具卡原位更新，共 60 个测量帧。`before_render` 到 `after_render` 记一帧总耗时；再包裹 `FloatContainer._draw_float`，记录这一帧花在浮层上的时间；并包裹通知条的 `FormattedTextControl` 文本回调，统计它是否被调用。

三种布局：

- `current`：当前实现，`FloatContainer` 包住主布局，通知浮层隐藏；
- `control`：去掉浮层包装的同一棵树，作为 A/B 对照；
- `visible`：通知可见，用于证明浮层确实在绘制，并给出显示时的成本。

先按同一序列预热一轮并丢弃，再跑 3 轮，每轮依次 `current → control → visible`。

## 隐藏态结果

| 模式 | 帧 P50 / P95（ms） | 浮层 P50 / P95（ms） | 浮层合计/轮（ms） | 占帧耗时 | 通知文本回调 | ANSI 字节 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| current | 10.191 / 16.830 | 0.0299 / 0.0422 | 1.957 | 0.30% | 0 | 70,499 |
| control | 9.754 / 17.198 | — | 0 | — | 0 | 70,499 |
| visible | 10.515 / 17.210 | 0.0507 / 0.1247 | 3.498 | 0.55% | 186 | 71,108 |

隐藏态下，处理浮层的成本每帧中位数 0.030ms、P95 0.042ms；60 帧合计约 2.0ms，约占该轮 650ms 渲染时间的 0.30%。通知文本回调一次都没有触发，说明隐藏时连通知字符串都没有生成。

`current` 与 `control` 的完整 ANSI 流哈希相同（`03a2d353…`，70,499 字节，6 次运行全部一致），输出逐字节相同。显示态对照中，通知文本回调每帧触发（62 次/轮），浮层成本 0.051ms/帧，并多出 609 字节；这部分成本只在通知真正显示时出现。

原始值见 [`smoke.json`](smoke.json)，逐轮数据如下：

| 轮次 | 模式 | 帧 P50 / P95 | 浮层 P50 / P95 | 浮层合计 | 占比 | 回调 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | current | 10.706 / 17.765 | 0.0302 / 0.0503 | 1.957ms | 0.298% | 0 |
| 1 | control | 9.169 / 16.881 | — | 0 | — | 0 |
| 1 | visible | 9.359 / 16.240 | 0.0497 / 0.1272 | 3.498ms | 0.557% | 62 |
| 2 | current | 9.457 / 16.180 | 0.0297 / 0.0422 | 1.778ms | 0.294% | 0 |
| 2 | control | 9.754 / 17.399 | — | 0 | — | 0 |
| 2 | visible | 10.515 / 17.210 | 0.0515 / 0.1247 | 3.785ms | 0.546% | 62 |
| 3 | current | 10.191 / 16.830 | 0.0299 / 0.0369 | 1.990ms | 0.306% | 0 |
| 3 | control | 9.763 / 17.198 | — | 0 | — | 0 |
| 3 | visible | 10.568 / 17.316 | 0.0507 / 0.0556 | 3.109ms | 0.456% | 62 |

同机同代码的跨轮帧耗时会随 CPU 频率档位波动（本组 P50 在 8.4–10.7ms 之间），因此上表只作为背景，判定依据是应用内直接计量的浮层成本、通知回调次数和输出字节一致性。

## 真实终端回放

把三份帧交给评测同款 kitty-vt-wasm 0.2.0（120×36、20k 回滚、truecolor）解析：

| 模式 | 帧数 | 解析耗时 | 画面 SHA-256 |
| --- | ---: | ---: | --- |
| current | 61 | 1.70ms | `bd11a27b…` |
| control | 61 | 5.79ms | `bd11a27b…` |
| visible | 62 | 1.48ms | `db1b8bd6…` |

`current` 与 `control` 的 36 行画面完全相同；`visible` 画面不同，且包含 `Trina · 编译完成 · 3 项工具`；隐藏画面不含 `✦ Trina`。画面文本见 [`screens/`](screens/)，校验结果见 [`screens.json`](screens.json)。

## 端到端短场景

用评测目录的固定夹具再跑一遍真实子进程路径（`trina_child.py` + kitty-vt + 40ms 输入）：

| 指标 | 本次 | 同日仓库配色冒烟（同夹具） |
| --- | ---: | ---: |
| 输出标记 | 64/64 | 64/64 |
| 帧数 | 74 | 74 |
| ANSI 字节 | 112,590 | 112,590 |
| 帧 P50 / P95 | 5.402 / 12.750ms | 5.405 / 12.190ms |
| 输出可见 P50 / P95 | 7.990 / 15.166ms | 8.403 / 14.031ms |
| CPU / 峰值 RSS | 531.3ms / 42.8MiB | 562.5ms / 42.8MiB |

输出体积和帧数完全一致，延迟差异在单轮波动范围内。结果见 [`terminal-smoke/`](terminal-smoke/)。

## 复现

```powershell
.\.venv\Scripts\python.exe -X utf8 evidence\20261116-notification-overlay\smoke.py --trials 3
& '.\tmp\omp-output-benchmark\node_modules\@oven\bun-windows-x64\bin\bun.exe' '.\evidence\20261116-notification-overlay\screen.ts'
$env:TRINA_BENCH_TRIALS='1'; $env:TRINA_BENCH_SCENARIOS='short'; $env:TRINA_BENCH_ENGINES='trina'
$env:TRINA_BENCH_OUTPUT='../../evidence/20261116-notification-overlay/terminal-smoke'
& '.\tmp\omp-output-benchmark\node_modules\@oven\bun-windows-x64\bin\bun.exe' '.\评测\20261115-omp-output-benchmark\run.ts'
```

## 边界

- 覆盖生产 prompt_toolkit 渲染循环与 VT100 输出，不覆盖 Windows Terminal/ConPTY 的桌面像素、GPU 或显示器刷新。
- 单机、3 轮、每轮 60 个测量帧；只回放固定流式输出，不含模型、网络或工具真实执行。
- 显示态只核对捕获画面中的位置与内容，未穷举滚动、缩放或长时间驻留场景。
- 同日端到端对比不是严格的改前/改后 A/B，仅用于确认正常输出路径仍按原体积绘制。
