# 控制台底部上下文栏

日期：2026-11-13。

底栏用不同图案显示运行状态、模型及步骤；活动行左侧显示当前动作，右侧显示短会话名。输入框横线嵌入当前上下文占用仪表，工作目录固定在横线最右；累计 Token 仍在独立用量行。宽屏并排显示，窄屏分行；路径按中文显示宽度省略并保留末尾目录。模型切换、历史恢复、新建会话及任务启动会同步更新上下文，输入草稿保持；上翻时不再显示常驻提示文字。

[预览图片](footer-preview.png)与[屏幕文本](footer-preview.txt)来自实际 `EnhancedConsoleInput` 的 prompt_toolkit 屏幕缓冲。模型、任务名和计时为演示值；未调用在线模型。复现：

```powershell
.\.venv\Scripts\python.exe -X utf8 evidence/20261113-console-footer/render_preview.py
```

验证命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_console_session tests.test_interactive tests.test_terminal_ui tests.test_chat_resume -q
```

结果：相关终端测试 83 项通过，全量 563 项中 553 项通过、10 项按环境跳过。渲染检查覆盖 170、88、40、28、20 列宽度，确认模型与短会话名更新、上下文仪表、右侧中文目录及草稿保留。课程工件检查、修改模块编译和 `git diff --check` 通过。
