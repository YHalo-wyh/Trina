# 工具栈与成果链验证

日期：2026-11-18。说明与入口见[工具、成果与复测](../../docs/TOOL_STACK/TOOL_STACK.md)，决策见[ADR-0038](../../design/adr/0038-tool-evidence-and-retest.md)。

## 环境

- Windows：Python 3.10，Playwright CLI 0.1.24及其Chromium，现有本机GDB/r2保留。
- Ubuntu-24.04 WSL：Node 22.21.1、Playwright CLI 0.1.24、Chrome for Testing 158.0.8127.18（Chromium build1254，含headless shell）。
- WSL工具：nmap 7.94SVN、DiG 9.18.39、jq 1.7、ripgrep 14.1、GDB 15.1；复用已有radare2 5.5.0。curl/wget/nc/telnet/whois均可从PATH解析。
- WSL中在`.venv-tools`安装当前Trina；Windows安装当前源码。两个环境均加载70项核心工具，无模块加载错误。

网络工具通过Ubuntu包管理器安装。WSL浏览器的单连接下载过慢，改用同一官方Google发行地址的分段下载；合并后比对服务器返回的归档哈希，再提取到用户Playwright缓存。实际启动再次验证浏览器版本。

## 自动测试

```text
python -m unittest discover -s tests -q
Ran 681 tests
OK (skipped=10)
```

10项环境条件跳过不计为执行通过。新增和关联工具测试：

```text
python -m unittest tests.test_browser tests.test_casebook tests.test_eval_report_lab -q
Ran 60 tests
OK
```

安装脚本通过`bash -n`与PowerShell语法解析。`git diff --check`无空白错误。

## 真实浏览器

Windows和WSL均执行`python scripts/verify_api_browser_stack.py`，使用回环HTTP夹具，输出相同：

```json
{
  "ok": true,
  "checks": ["api_recon_runtime", "api_recon_static", "browser_close", "browser_evaluate", "browser_interact", "browser_open", "browser_recording", "browser_route", "browser_upload", "list_assets", "record_fact"],
  "request_count": 1,
  "assets": 3,
  "mock_proof_rejected": true
}
```

流程实际生成trace工件、完成文件上传、采集JS发起的API请求，并拒绝将mock响应登记为真实漏洞证明。移除mock后需重载才能解除synthetic。
本轮也修复了Windows npm.cmd对多行JS的二次解析，以及CLI缺省寻找系统Chrome导致WSL启动失败的问题。

## 验证范围

ScopeSentry通过真实回环HTTP MCP夹具验证初始化、实时Schema、调用、会话回收、参数缺失拒绝、查询/创建允许列表分离与restricted写操作拒绝。
用户的真实ScopeSentry实例未配置，因此不能声称已连通。浏览器视频录制接口已接入，真实冒烟采用trace；WS仅做采集监听接口，SSE不解析事件流。
没有运行收费模型、远端扫描任务或同预算ARTEX性能对照；本记录证明工具链和状态契约，不证明通用漏洞语义或真实模型得分。
