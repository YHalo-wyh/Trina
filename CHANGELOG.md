# Trina 更新日志

## 0.3.0 · 2026-11-10

### 默认完全信任

- 新增 `scope.trust_mode: full|restricted`，默认值和项目配置均为 `full`。
- full 模式下，合法格式的公网目标、CIDR、`exploit` 动作、破坏性 HTTP 方法、旧禁止参数/载荷和超出字典规模的尝试不再触发授权拒绝。
- DNS 预解析失败不再阻止任务进入执行链；实际连接错误由工具返回。
- 人工审批继续默认 `off`，控制台、`play`、裸 `run` 和定时任务均不逐动作等待确认。
- OMP 扩展删除 bash 二次拦截，`/pentest` 不再要求先执行授权确认。

### 兼容与可观测性

- `scope.trust_mode=restricted` 完整保留 0.2.0 的网段、公网、动作等级、破坏性列表、禁止模式、同源重定向和爆破规模规则。
- full 与 restricted 均保留 ToolRegistry 参数校验、执行超时、取消租约、速率控制、控制面校验和审计哈希链。
- 报告新增“信任模式”，避免把 full 运行误写成“仅在授权白名单内执行”。
- `--allow-exploit` 保留为 restricted 兼容参数；full 模式不再需要。

### 版本

- 包版本、终端首页和 HTTP User-Agent 升级为 `0.3.0`。
- User-Agent 改为从 `trina.__version__` 生成，避免后续版本号分散漂移。

## 0.2.0 · 2026-11-04—2026-11-10

- 引入粉紫色 TRINA 字符首页、斜杠命令索引和固定底栏。
- 供应商、请求地址、API Key、模型和思考档位改为整条配置管理。
- 加入 Goal、一次性定时任务、Swarm 首版和项目级 CTF 技能。
- 清空 system prompt 与身份提示，人工审批默认关闭，未知用量字段不再占位。
