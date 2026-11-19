# 按需工具 Schema

Trina 的内置工具实现继续由 `ToolRegistry` 统一注册，但模型看到的 Schema 和实际可调用工具由任务级白名单决定。这样保留执行兼容性，同时避免把企业渗透、资产图、完整交通证据和所有浏览器动作一次性注入 CTF 上下文。

## 默认 CTF 面

默认保留命令行、工作区读写、`http_fingerprint`、`web_search`、目标验证、必要的漏洞验证、事实/发现交接以及浏览器 `open/close`。`terminal_exec` 是命令行主入口，`workspace_*` 继续提供代码读写闭环。

默认隐藏的工具包括端口和服务盲扫、完整交通证据链、企业资产图、共享 Trace、后台终端控制、Flag 快捷探测、RCE 快捷入口以及浏览器高级动作。隐藏工具仍可由显式 Capability 租约或自定义注册表提供。

## 动态加入

- Web/API 目标加入 API 静态分析、目录枚举、参数反射和 Web 面枚举。
- Binary/Pwn/Reverse 目标加入 radare2 和 GDB 原生工具。
- 任务明确浏览器或 Playwright 时加入浏览器交互、请求、存储、快照、截图和脚本工具。
- 自定义 `ToolRegistry` 如果没有内置 CTF 工具标识，保持原样，保证单元测试、插件能力和外部 Worker 兼容。

每次 Agent 初始化都会把白名单写入 `ToolContext.extra["tool_allowlist"]`。Schema 导出和执行入口共用这份集合。模型猜测隐藏工具名时会收到结构化拒绝结果，不能绕过白名单。

命令行 `python -m trina tools` 默认显示 CTF 按需工具面。需要审计完整注册表时使用 `python -m trina tools --all`，也可以用 `--route web` 或 `--route binary` 查看题型扩展。

