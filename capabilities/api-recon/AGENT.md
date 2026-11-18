# API 侦察任务指南

先用 api_recon_static 获取入口 HTML、MPA 页面、JS/manifest 资源和候选路由，遵守请求/深度预算。
动态拼接与运行时权限菜单不会全部出现在静态字符串中；需要真实登录状态时沿 browser_storage 或页面登录操作取得会话。

运行时先 browser_open，再 api_recon_runtime(start)，执行正常导航、按钮与菜单操作，最后 collect/stop。
监听只覆盖开始后的当前页面，想采集首屏请求应先在空白/占位页开始，再导航目标页。
保存方法、URL、查询参数、请求体样本、响应状态、WS与SSE线索；必要时 browser_requests 读取完整交互。
用 insert_assets 合并实际入口，api_route_tree 生成路由树；权限字段只能当候选，不能把页面菜单当后端鉴权结论。

若前端必须靠 mock 菜单/配置才能展开，使用 browser_route，明确标记 synthetic。
收集后移除mock，重新载入，独立向真实后端验证权限边界；mock 的成功结果不用于 report_finding 的 verified 证明。

不把候选API逐一盲发写请求。先核对方法、参数、身份、预期行为和正常对照，再验证具体假设。
边发现边写回 record_fact/report_finding；给结论绑定真实 traffic_id 或浏览器 evidence_ref。
