# 缓存感知运行时

日期：2026-11-12。适用入口：ChatAgent、PentestAgent 及由其执行的 Swarm Worker。

## 诊断口径

缓存命中率采用供应商报告的缓存读取 Token 除以对应请求的总输入 Token。77% 本身不能判断设计质量，也不能把剩余 23% 全部分配给新增输入或前缀变化。供应商的分词、缓存落盘、路由、失效时间和隐式模板均可能影响结果。

`/context` 查看当前请求的本地估算、输入预算、继承检查点次数、前缀变化原因、公共前缀字节及供应商实际未命中输入。`/usage` 保留原有真实用量统计。

| 原因 | 判据 | 能证明的范围 |
|---|---|---|
| cold | 本任务尚无前一个请求 | 冷启动，没有可比较的历史 |
| append | 当前可见消息以原消息序列为前缀 | 既有可见消息未修改，新增部分可单独计量 |
| prefix_changed | 原有可见消息内容变化 | 可见消息发生前缀变更 |
| request_settings_changed | 地址、模型、协议、思考、采样、缓存设置或工具集改变 | 请求条件变化，需要重新评价缓存 |
| compaction | 创建了新的上下文检查点 | 动态历史按明确生命周期替换，完整原件已归档 |

字节诊断基于规范化的可见消息序列，不是供应商 Chat Template 的最终 Token。`suffix_visible_bytes` 是公共前缀之后的全部后缀；仅在 append 时，`appended_visible_bytes` 才表示真正追加的可见字节，前缀修改时保持未知。`uncached_input_tokens` 仅在总输入和缓存读取都有实际值时计算；缺失值保持未知。不产生“因前缀变化浪费了多少 Token”这样的推算结果。

审计事件为 `context.request`、`context.compacted`、`context.budget_error`；运行状态保存在 `RunState.context_stats`。请求诊断记录摘要、哈希、字节数和用量，不记录完整提示词或 API Key。

## 状态增量

`trina/conversation.py` 维护当前任务的结构化事实投影。第一轮和每隔 16 轮发送完整 State Checkpoint；其余调用发送基于前一 revision 的 JSON Pointer `set/remove`。例如，仅假设状态改变时：

```json
{"kind":"delta","revision":18,"base":17,"set":{"/hypotheses/H03/status":"confirmed"},"remove":[]}
```

未变化的事实、注册检查项、Commander 情报和假设条目不会逐轮重发。工具观察、当前预算和 Agent 状态栏继续放在本轮尾部；运行中的时长及计数不进入事实差量，避免时间变化使整份事实重复。证据投影保留索引与 SHA-256，原始响应仍由证据工件保存。

## 继承检查点

本地默认窗口为 32768，输出预算、工具 Schema 和额外 1024 Token 余量先扣除；输入估算达到剩余额度的 80% 时触发。估算采用 UTF-8 字节数和消息开销，不宣称具有精确 tokenizer；窗口需依据实际模型容量配置。

压缩保留首条 system、初始合同、全部已登记用户约束、当前结构化事实、证据索引及最近执行链。原生 assistant/tool_calls 与 tool 结果不得从中间切断。压缩后尽量降到触发线的 75%，减少连续重建缓存；保留链无法达到该目标时，只要求满足硬预算。

替换前先将完整旧消息、下一版消息、用户约束和 RunState 原子写入 `runs/<run_id>/context/epoch-<序号>-<上下文ID>.json`。上下文 ID 避免重新创建上下文时覆盖旧原件。写入失败保留原历史；用户约束、当前事实和最近执行链无法完整放入预算时停止请求，明确报告上下文预算不足。任务结论保留 `context_budget_exhausted`，不把未执行检查解释为正常通过。

`/compact` 可在空闲聊天中手动创建检查点。后台任务只在模型请求边界自动压缩。当前使用确定性的事实与约束继承，不调用额外模型生成摘要；旧正文按归档位置复查。2026-11-13 起，`/resume` 可从自动保存的聊天记录恢复当前活动消息、压缩 epoch、全部已登记约束、差量基线、诊断和累计用量。独立导入任意 `epoch-*.json` 归档尚未提供；操作系统后台进程不能通过历史文件恢复。使用方式见 [历史对话](CLI_UI.md#历史对话2026-11-13)。

## 供应商策略

`trina/llm/cache.py` 生成内部 `stable_prefix_id`，随后按地址、协议、模型和显式能力配置转换到实际请求字段。

| 连接 | 策略 |
|---|---|
| OpenAI 官方、GPT-5.6 及以后 | 使用 `prompt_cache_options`；稳定 system 和初始合同可设内容断点。Chat TTL 为 30m，Responses 可用 30m 或 12h |
| OpenAI 较早模型 | 使用路由键；显式配置保留参数时使用 `prompt_cache_retention`，具体模型支持范围仍由服务端裁决 |
| Anthropic 官方 Messages | `cache_control` 标记稳定 system 与合同；auto 同时启用请求级自动缓存，TTL 为 5m 或 1h |
| DeepSeek 官方 | 使用供应商自动前缀缓存，不附加 OpenAI 或 Anthropic 缓存字段 |
| 未知 OpenAI 兼容中转 | 默认仅保持稳定消息；缓存扩展须通过连接上的 `cache_features` 声明后协商 |

协议依据：[OpenAI Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)、[Anthropic Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)、[DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)。核对日期为 2026-11-12。

未知中转使用用户已请求的业务调用协商，不额外发起预热或付费探测。只有在未产生正文或思考增量、HTTP 400/422 明确指出缓存参数不受支持时，撤掉缓存扩展重试一次；本后端实例对该地址、模型和协议记住拒绝。鉴权失败、网络错误和已开始生成的响应不按缓存能力降级。该策略不宣称中转接受参数就一定实际使用了缓存，命中仍由真实 usage 确认。

`cache_mode=off` 表示不发送缓存扩展，不能关闭供应商自身的自动缓存。`stable_prefix_id` 是内部标识，只有支持 `prompt_cache_key` 的连接才发送映射后的键。不同协议的 TTL 不混用；未知中转不根据模型名字猜测能力。

## 配置

`configs/trina.yaml` 的 llm 段保存上下文预算及默认缓存策略：

```yaml
llm:
  cache_mode: auto
  cache_ttl: ""
  cache_features: []
  cache_namespace: trina
  context_window: 32768
  compaction_threshold: 0.8
  context_keep_messages: 8
  state_checkpoint_interval: 16
```

使用 `/model` 的供应商连接后，`cache_mode`、`cache_ttl` 和 `cache_features` 随该连接一起保存、切换和编辑，旧 v2 记录缺少字段时采用 auto、空 TTL 和空能力列表。高级缓存项可在仓库外的 providers.json 中配置；现有表单编辑会保留这些字段。

能力声明取值为 `openai_key`、`openai_retention`、`openai_options`、`anthropic_cache`。例如，已确认支持新版 OpenAI 缓存字段的中转连接可配置 `"cache_features":["openai_key","openai_options"]`；声明不适用于别的连接，不应因切换供应商而继承。

## Worker 与验证范围

同一 Core 与工具快照可生成一致的前缀标识；任务 ID、计时和当前事实不进入该标识。每个 Worker 的差量基线、压缩历史、用户约束、状态和诊断独立。能力仍按任务 JIT 绑定，工具集变化会进入请求条件诊断，不为提高命中率加载全部工具。

供应商可能先将工具定义加入模板，不同工具集能否复用基础 Core 的 KV 缓存不能由 Trina 保证。当前未实现供应商原生 `allowed_tools`、延迟工具搜索或跨组织 KV 迁移。后端实例复用也不作为缓存命中证据。

`prompt_layers` 继续默认 false。离线对照验证两条路径都保留固定 system 和初始合同，但未开展同题、同预算的真实模型质量与费用对照，因此本轮不依据结构测试修改默认值。

行为验收见 `tests/test_cache_runtime.py`：状态增量、周期完整状态、归档失败、不可容纳约束、原生工具配对、聊天与任务自动压缩、Worker 隔离、前缀变化分类、三种 API 参数、真实回环 HTTP 参数拒绝、鉴权失败及供应商配置切换。

实际运行输出和输入量对比见 [缓存运行时验证](../evidence/20261112-cache-runtime/README.md)。
