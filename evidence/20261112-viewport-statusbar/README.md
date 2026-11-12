# 2026-11-12：会话视口、文本复制与 Agent 状态栏验证

执行环境：Windows，Python 3.10.11。运行版本为基线 `b285218de597b6220223885e3c9577d90712360a` 加本次提交中的变更；测试在提交前的工作区执行，最终修订以包含本目录的 Git 提交为准。

本次实际执行本地程序、prompt_toolkit 输入链路、单元回归和机器规约检查。模型调用使用可控回放后端，没有访问付费模型，也没有对外部目标开展渗透测试。

## 检查与结果位置

| 检查对象 | 实际检查办法 | 结果与范围 |
|---|---|---|
| 长历史滚动 | 100 个历史块，各 20 行；重复滚动 1000 次 | 历史块不重新排版，光标定位读取缓存行数 |
| 流式更新 | 100 次正文增量先到达，再触发一帧渲染 | 渲染一次变化块；稳定历史不重排 |
| 鼠标选取 | 向实际运行的 prompt_toolkit Application 注入 SGR 按下、拖动、释放事件 | 跨行文本进入应用剪贴板，输入草稿保持；Unicode 另有直接鼠标事件测试 |
| 剪贴板通道 | 隔离调用 Windows Unicode Clipboard API 和 OSC 52 输出 | 校验窗口 owner、内存字节、协议编码及 flush；操作系统 API 使用替身，没有覆写用户剪贴板 |
| 模型状态栏 | 截取 ChatAgent 每次发送的最后一条结构化消息 | 进度和工具计数来自真实变量；前缀保持；后台状态能从 running 变为 completed |
| 后台执行 | 实际启动 Python 程序，读取输出及退出状态 | 覆盖退出码 0、非零 7、超时、取消、子进程回收及跨会话拒绝 |
| 启动失败回收 | 启动后使该次工具超时、返回失败或违反返回类型约定 | 本次作业回收，其他已存在作业继续运行 |
| 规约 | 执行项目白名单总关卡 | 460 项测试全部通过；六类交付物 6/6，21 条需求、33 条规则、48 个测试引用，真实输出保存在 [verification.json](verification.json) |

测试入口为 [test_transcript.py](../../tests/test_transcript.py) 和 [test_runtime_status.py](../../tests/test_runtime_status.py)。背景程序的具体命令、模型请求快照、输出与退出码保存在 [background-trace.json](background-trace.json)，其审计哈希链校验结果为 true。

## 滚动计算路径对比

比较同一机器上的旧版视口类与当前工作区类，使用相同的输入、DummyOutput 和操作次数。完整脚本见 [benchmark.py](benchmark.py)，原始结果见 [benchmark.json](benchmark.json)。

| 指标 | 修改前 | 修改后 |
|---|---:|---:|
| 1000 次滚动及光标定位计算 | 12414.129 ms | 1.554 ms |
| 20 次流更新及兼容文本读取 | 1328.423 ms | 55.557 ms |
| 流更新引发的稳定历史块重排 | 2000 次 | 0 次 |

这份微基准测量内部计算路径，未包含终端模拟器、远程链路和屏幕绘制延迟。修改前后末尾空行处理差一行，不影响历史块规模；时间会随机器负载变化，稳定历史不重排由测试单独约束。

## 复现

在仓库根目录执行：

```powershell
python -X utf8 -m scripts.verify_contract --execute --json
python -X utf8 evidence/20261112-viewport-statusbar/benchmark.py
python -X utf8 evidence/20261112-viewport-statusbar/background_probe.py
```

后台探针使用临时目录和当前 Python 解释器，经过真实 ChatAgent、ToolRegistry、run_external 与后台作业表；回放仅代替模型决策。运行标识、作业 ID、耗时及临时目录每次变化。

## 未检查范围

尚未由用户在日常终端中人工确认滚轮手感与系统剪贴板粘贴；OSC 52 是否被远程终端接受取决于其设置。模型状态栏在下一次请求末尾提供，不能更新已经发出的请求，也没有后台完成后自动唤醒模型的机制。作业不跨重启恢复，进程树回收不提供文件系统或网络沙箱。
