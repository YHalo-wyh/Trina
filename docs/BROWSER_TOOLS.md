# Playwright 浏览器工具链

实现入口：`trina/browser/`、`trina/tools/browser.py`。浏览器使用固定版本 `@playwright/cli@0.1.24`，所有命令仍由 `ToolRegistry → run_external → ScopeGuard` 执行，不从工具模块直接创建进程。

## 能力

| 工具 | 作用 |
|---|---|
| `browser_open` | 打开 Chromium/Chrome/Firefox/WebKit/Edge，返回当前任务内的不透明会话句柄 |
| `browser_snapshot` | 获取可访问性树和稳定元素 `ref`，完整 JSON 落证据工件 |
| `browser_interact` | 导航、点击、填写、按键、选择、勾选和标签页操作 |
| `browser_storage` | 读取或修改 Cookie、localStorage、sessionStorage |
| `browser_requests` | 列出真实页面请求，读取请求/响应头和正文 |
| `browser_screenshot` | 保存视口、完整页面或元素 PNG |
| `browser_close` | 关闭浏览器并回收会话 |

页面显著变化或导航后应重新调用 `browser_snapshot`；旧 `ref` 可能失效。工具没有暴露任意 JavaScript `eval` 或 `run-code`，需要页面数据时优先使用快照、存储和网络证据。

## 安装

本机需要 Node.js/npm，二选一即可启动：全局 `playwright-cli`，或 `npx` 动态调用固定版本。首次安装 Chromium：

```powershell
npx --yes --package @playwright/cli@0.1.24 playwright-cli install-browser chromium
python -m trina doctor
```

CTFY 完整镜像在 `Dockerfile.ctfy` 中安装 Node.js、Playwright CLI、Chromium、radare2、GDB、GCC 与 bubblewrap。轻量托管评测镜像 `Dockerfile` 不预装这些大型原生依赖。

## 会话、证据与回收

- 默认每任务最多两个浏览器会话，单命令 45 秒、空闲 600 秒；在 `browser` 配置段调整。
- CLI 内部名称不向模型暴露，模型只拿到 `web-…` 句柄；句柄不能跨 Worker 或恢复后复活。
- 原始命令结果写入 `runs/<run_id>/browser/<session_id>/NNNN-<command>.json`；截图和二进制响应体保存在同一目录，返回值只给摘要与 `evidence_ref`。
- `open` 是唯一允许受管子进程在启动命令返回后继续存在的动作；超时、取消、能力租约释放、任务结束和显式 `browser_close` 都会关闭对应会话。
- `input_value` 与 `storage_value` 在事件和审计日志中脱敏，实际值只传给浏览器命令。

## 范围边界

浏览器页面可自动加载脚本、图片和跨源子资源，CLI 目前不能在每个子请求发出前调用 Trina 的逐 URL 裁决。因此浏览器工具仅在默认 `scope.trust_mode=full` 下运行；切到 `restricted` 时明确返回 `RESTRICTED_UNSUPPORTED`，避免把初始 URL 校验误当成完整的跨源边界。

显式 URL 仍经过目标格式、动作等级、速率和审计检查。资料检索使用 Google / Bing 找到来源后，应打开原文核对；搜索引擎选择不写入授权提示词。

## 验证

```bash
python -m unittest tests.test_browser -v
python -m unittest discover -s tests
```

`tests/test_browser.py` 覆盖注册 Schema、会话隔离、限制、证据、截图、敏感值脱敏和受管子进程执行边界。真实冒烟链路为 `open → snapshot → requests → screenshot → close`。
