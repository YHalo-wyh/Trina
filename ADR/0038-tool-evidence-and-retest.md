# ADR-0038：通用成果、HTTP 留痕与复测工具链

- 日期：2026-11-18
- 状态：Accepted
- 关联：[ADR-0021](0021-capability-fabric.md)、[ADR-0031](0031-unified-agent-runtime-and-state.md)、[ADR-0037](0037-proactive-workers-and-pluggable-task-modules.md)

## 背景

Trina 已能调试二进制、运行脚本并操控浏览器，但通用脚本结果不能直接进入结构化发现，HTTP证据只保留短摘要，复核工具也不能记录真正执行后的复测结果。
对 ARTEX 的只读比较显示，成果登记、流量关联、资产覆盖和独立复核值得补齐。其代码不引入本项目；执行和状态继续复用已有契约。

## 决策

1. 新增casebook工具组：事实/情报/发现登记、资产覆盖、目标条件查询和证明、流量检索绑定、复测上下文和结果。数据位于同一RunState.casebook，不引入第二个事实数据库。
2. terminal_exec保存命令工件与SHA-256，HTTP结果通过执行窗口ContextVar进入任务独立SQLite。流量记录属于原始工件；事实仍通过注册表的工作副本与正常提交进入RunState。
3. 新工具均经ToolRegistry；出网复用ctx.http，浏览器复用run_external，保留ScopeGuard、取消、超时和执行租约。迟到工具不能回写事实或继续请求。
4. 发现默认detected，verified要求独立复核来源和解释。复测要求读取当前发现版本、采集新证据、可比环境与独立正常对照；环境或证据不足登记inconclusive。复测不删除原发现。
5. API侦察采用有界同源静态资源递归和按需浏览器监听。路由/权限标签是候选，mock标记synthetic；模拟成功不构成后端漏洞证据。
6. 浏览器新增脚本执行、上传、mock、trace/代码/视频录制；Windows绕过npm的.cmd包装，直接用Node启动同一固定CLI，避免多行JS和引号再次被cmd解析。启动显式传递browser，避免CLI默认寻找系统Chrome而忽略已安装Chromium。
7. ScopeSentry用外置Capability和Streamable HTTP适配接入，实时读取Schema，查询与写操作允许列表分离。AGENT.md继续按租约进入user；服务和密钥不放入system或清单。
8. 七模块system补充机理不同路线、材料性新证据才重开失败路线、强依赖分段派发、及时写回、独立复核与证据版本重读；不新增平台身份或固定专业Worker。
9. 安装脚本为WSL/Linux准备常规网络、二进制及浏览器程序，Windows安装本机浏览器并调用WSL安装完整栈；核心Python依赖保持不变。

## 边界

这不是全局MITM：任意子命令和浏览器流量不会全部写入HTTP SQLite。正文有上限和截断标记，浏览器完整证据仍从独立工件读取。
证据来源、时间、版本和对照可以程序检查；任意漏洞的语义结论仍依赖Agent分析与具体复现。目标证明复用既有关键词规则，不能覆盖全部自然语言目标。
ScopeSentry需要已有服务和凭据；回环夹具不能冒充真实服务连接。浏览器和远端扫描写操作不能逐子请求裁决，restricted下继续拒绝。
没有在同预算真实模型任务上测得性能提升，新增提示词不据此宣称速度或得分变化。

## 验证

`tests/test_casebook.py`覆盖真实HTTP重定向、正文分页/截断、伪造来源拒绝、工件修改、失败命令、发现去重、资产覆盖、状态往返、目标条件、复测新证据/对照/版本、静态资源预算、迟到请求拒绝及MCP读写分离。
`tests/test_browser.py`覆盖新Schema、会话和既有执行边界；真实冒烟入口为`scripts/verify_api_browser_stack.py`。
完成全量单元测试后再推送；环境和真实结果记录于 [工具栈说明](../docs/TOOL_STACK/TOOL_STACK.md) 及对应验证目录。
