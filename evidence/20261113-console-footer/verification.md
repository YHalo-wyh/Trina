# 控制台与比赛提示词验证

日期：2026-11-13。

## 行为检查

- 本地 HTTP 服务发送 401、403、402、429 错误后保持连接开启，后端在连接关闭前返回状态码、错误类别和脱敏详情；错误正文完全不发送时也能报告 403。
- SSE 字符串错误、对象错误和独立 error 事件保留额度信息；三种 API 的终止事件到达后即可完成，不依赖 EOF。
- 鉴权和余额/额度错误只请求一次；临时错误在退避前显示错误与重试次数。事件重放不重复显示同一请求的错误。
- 控制台收到模型错误或请求前的上下文预算错误后回到空闲；原有输入草稿保留。20、28、40、88、170 列下活动行与仪表横线各占一行，目录包含 📁 并靠右，普通输入不显示常驻发送/停止/上翻提示。
- 首条有效请求使用隔离的短标题协议命名，会话名固定在活动行右侧；上下文仪表使用当前输入量与模型窗口，累计 Token 保持独立。
- 聊天允许自然语言直接回答；任务仍遵循动作协议。系统定位包含多类 CTF，二进制说明仅在相关任务中适用。基础 system 为 1292 个字符，本地预算估算 1691 Token；修改前为 1327 字符、估算 1716 Token。

## 验证入口

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -X utf8 -m compileall -q trina tests
.\.venv\Scripts\python.exe -X utf8 scripts/verify_contract.py
.\.venv\Scripts\python.exe -X utf8 scripts/verify_course_pack.py
git -c core.safecrlf=false diff --check
.\.venv\Scripts\python.exe -X utf8 evidence/20261113-console-footer/render_preview.py
```

全量测试共 563 项：553 项通过、10 项跳过，用时 119.684 秒；退出码为 0。输出保存为 [regression.log](regression.log)。机器规约、课程引用检查、编译与 diff 检查均通过。

[footer-preview.png](footer-preview.png) 从实际 prompt_toolkit 屏幕缓冲渲染，模型、任务和耗时为演示值。预览不发送模型请求，未测量真实供应商响应速度。sapper-v2 的参考来源和适配说明见 [提示词说明](../../docs/PROMPTS.md)。
