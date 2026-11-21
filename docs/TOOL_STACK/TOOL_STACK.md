# 工具、成果与复测

Trina 将通用脚本、HTTP 和浏览器结果接入统一成果链。实现独立于 ARTEX；参考其“观察—发现—流量—复测”分工，不引入其源码或角色系统。

## 安装

Windows PowerShell：

```powershell
./scripts/install-tools.ps1
```

此入口安装 Windows Playwright CLI/Chromium，并在 Ubuntu-24.04 WSL 内安装完整工具栈。
WSL 中从仓库运行 `source .venv-tools/bin/activate`，然后执行 `trina doctor` / `trina`。
Windows 的 Trina 继续使用本机程序；不会把缺失的 nmap 自动转发到 WSL。

Ubuntu/Debian：

```bash
bash scripts/install-tools.sh
source .venv-tools/bin/activate
trina doctor
```

安装项包括 nmap、dig/nslookup、curl/wget、jq、rg、nc、telnet、whois、GDB、GCC、bubblewrap、radare2、Playwright CLI 和 Chromium。
Node 低于20时从官方发行包安装固定 Node 22.21.1并校验 SHA-256；Playwright CLI固定0.1.24，浏览器由该版本安装，避免版本错配。
已有radare2时复用；缺失时构建固定5.9.8。系统包采用发行版版本；安装需要包仓库、npm、Node和GitHub下载可达。
核心 Python 包不增加新的必需依赖。ScopeSentry 是外部服务，不会在本机启动服务器或自动配置密钥。

## 成果工具

| 工具 | 行为 |
|---|---|
| `record_fact` / `record_intelligence` | 引用已有证据登记观察、推断、后续路线 |
| `report_finding` | 将脚本或浏览器发现写入 RunState 与报告；默认 detected，verified 需要独立验证证据与解释 |
| `insert_assets` / `list_assets` / `list_untested_assets` | 合并资产、方法、参数样本并查询覆盖 |
| `mark_asset_tested` | 引用已执行检查的证据记录覆盖；不表示没有漏洞 |
| `list_goals` / `prove_goal` | 复用任务关键词条件检查实际状态；不能解析的自定义目标仍依赖原始验收器 |
| `traffic_search` / `traffic_get` / `traffic_blob` | 搜索、读取元数据、分页读取正文 |
| `bind_finding_traffic` / `get_finding_traffic` | 给发现绑定流量编号，不自动提升确认状态 |
| `get_finding_retest_context` | 取得完整发现、关联流量、历史复测和证据版本token |
| `record_finding_retest_result` | 登记 reproduced/fixed/inconclusive，检查新证据、一次性token、版本、环境和正常对照 |

`terminal_exec` 返回稳定 `evidence_id`，原始命令结果保存为 JSON，并记录 SHA-256；修改工件后不能沿用旧证明。
后台命令使用 `terminal_start`；指定 job_id 查询 `terminal_status` 时，仅在进程及输出流收尾后归档稳定 `evidence_id`。工件包含真实终态、退出码和每路最多 16000 字符的输出尾部，明确标记 `output_is_tail`，完整输出事件仍在任务事件库。重复查询复用证据；运行中不生成完成证据，失败/超时/取消工件不能作为成功证明。
证据必须存在于当前任务，不能用任意路径或模型自造编号代替。浏览器证据使用 `evidence_ref`，HTTP 使用 `traffic_id`。
verified与复测结论仍由 Agent 判断语义，程序约束的是来源、版本和比较条件，不是任意漏洞的通用自动证明器。
失败路径继续保存在步骤与审计中；工具失败、请求未命中不能当修复或安全证明。

既有 `worker_tasks` 已覆盖 ARTEX 的 add_intent/steer_work/kill_work 对应行为：分别使用 spawn/message/cancel。
查询与回收用 list/result/wait，不另造一套队列或递归 Worker。

## HTTP 留痕范围

`ToolRegistry` 执行窗口内经 `ctx.http.request/get/post` 与 `arequest` 发出的交互写入
`runs/<run_id>/traffic.sqlite3`，包括同步重定向的实际各跳；HTTP结果携带 `traffic_id`。
请求和响应正文各最多保存200000字符（异步响应最多200000字节解码），超过上限返回截断标记；它不是无限保存的原始PCAP。
SQLite 按任务和工具调用编号隔离，可在历史运行目录中读取，不恢复旧进程。

浏览器网络仍由 `browser_requests` 和浏览器工件读取；运行时API样本按需进入资产台账。
任意外部子命令、额外线程、第三方扫描器及浏览器的全部流量不会自动进入这份 HTTP SQLite，当前没有全局 MITM 代理。
常用认证头不进入元数据摘要，但本地流量库及原始浏览器工件可能包含任务凭据，按运行工件管理。

## API 侦察与浏览器

`api_recon_static(url,max_requests,max_depth)` 有界抓取同源 HTML/MPA、脚本、相对 chunk 与 manifest 链接，提取API候选、路由树及权限字段线索。
跨源资源只跳过，不自动跟随；表单不提交，API候选不盲发写请求。动态拼接、压缩运行时映射和复杂webpack chunk名称不能保证穷尽。

运行时流程：

1. `browser_open` 打开页面，`api_recon_runtime(start)` 安装当前页面监听。
2. 正常导航、登录、展开菜单、触发操作。
3. `api_recon_runtime(collect/stop)` 读取方法、URL、查询参数、body样本、状态、WS样本帧；真实HTTP入口自动合并资产。
4. 需要完整请求/响应时调用 `browser_requests`；对候选接口独立验证。

监听开始前的请求无法追补；需要首屏流量时先打开同源占位页再开始监听并导航。
最多记录1000次请求、100条WebSocket、每条20个接收帧。SSE记录请求类型，不解析事件流。新增标签页需要自己的采集。
`api_route_tree` 可从候选路径生成树。外置指南见 `capabilities/api-recon/AGENT.md`。

新增 `browser_evaluate`（页面JS/Playwright）、`browser_upload`、`browser_route`、`browser_recording`（trace/交互代码/视频）。
mock启用后的结果标记synthetic；移除mock后仍需重新导航/载入才解除标记。模拟结果只能用于侦察，不能作为真实后端漏洞证明。
所有浏览器操作维持 full 模式约束；restricted 模式使用逐请求受控HTTP。

## ScopeSentry 外置模块

清单与说明位于 `capabilities/scopesentry/`，可移出仓库，通过 `TRINA_CAPABILITY_DIRS` 临时发现。
配置完整 `/mcp` 地址和密钥：

```powershell
$env:TRINA_SCOPESENTRY_URL='http://your-instance:8082/mcp'
$env:TRINA_SCOPESENTRY_KEY='<your-key>'
```

Worker按需领取 `scopesentry.query` / `scopesentry.manage`，先 `list_tools` 获取实时schema再call。
查询和创建扫描分开允许列表，远端写操作只在full模式执行。原生Streamable HTTP使用 `ctx.http`，JSON/SSE响应匹配JSON-RPC ID，调用后回收MCP会话。
没有实例和凭据时标为不可用；模块AGENT.md只进本任务user合同，不进入system。
本仓库的回环MCP测试不证明用户真实ScopeSentry服务已连接。

## 验证

```bash
python -m unittest tests.test_casebook tests.test_browser tests.test_registry tests.test_capabilities -q
python scripts/verify_api_browser_stack.py
python -m unittest discover -s tests -q
```

真实浏览器冒烟覆盖本地HTTP页面、JS API请求采集、资产写回、mock证据拒绝、移除后重载、文件上传、trace工件与审计链。
架构取舍见 [ADR-0038](../../ADR/0038-tool-evidence-and-retest.md)。
