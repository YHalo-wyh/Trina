# ScopeSentry 任务模块

仅在当前任务要求使用 ScopeSentry 时读取本说明。平台地址和凭据取自
`TRINA_SCOPESENTRY_URL`（完整 `/mcp` 地址）与 `TRINA_SCOPESENTRY_KEY`，不要写进提示词或提交仓库。

先调用 `scopesentry_query(action=list_tools)` 获取实例的实时参数定义，再按定义查询。
项目名、模板名不能当 ObjectID；先查询取得真实 ID。节点需要来自在线节点列表。
资产查询优先按当前项目过滤，使用分页；总数用 count_assets，不为计数抓全库。

创建扫描前读取 `scopesentry_manage(action=list_tools)`，确认 targetSource、模板 ID、节点与目标来源。
只执行当前任务明确要求的扫描，记录服务器返回的任务 ID，然后查询实际终态；创建成功不等于扫描完成。
外部结果先作为情报，验证后再写入 Trina 的资产或漏洞台账。无服务地址或凭据时说明未连接，不能编造平台回执。

本说明只作为本任务 user 合同加载；模块不改变其他任务或全局 system。
