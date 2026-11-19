# Web 搜索工具

`web_search` 是独立的只读资料检索工具，所有请求经过 `ToolContext.http`、ScopeGuard、限速和审计。工具不把搜索结果直接登记为漏洞事实，结果必须通过原文、实验或其他证据核对。

## 来源

- `provider=auto` 先尝试 Bing RSS，空结果或请求失败时再尝试 Google 页面。
- `provider=google` 使用公开 Google 搜索页面，不需要 API Key。
- `provider=bing` 使用公开 Bing RSS，不需要 API Key。
- `provider=deepseek` 调用 DeepSeek 官方 `POST /v1/tools/search`，读取当前 DeepSeek 供应商 Key 或 `DEEPSEEK_API_KEY`。

DeepSeek Search 返回标题、URL、短摘要、长摘要、页面内容、发布时间、权威等级和相关性分数。官方 Search API 按成功请求计费，免费 Google/Bing 路径不需要搜索服务商 Key。

## 参数

`query` 保留原始搜索词。`site`、`after`、`before`、`filetype` 和 `language` 会被转换成稳定的查询操作符，也可以直接写入 `query`。

```text
web_search {
  "query": "Python subprocess timeout",
  "provider": "auto",
  "site": "docs.python.org,github.com",
  "after": "2025-01-01",
  "before": "2026-12-31",
  "filetype": "pdf",
  "language": "en",
  "count": 8
}
```

生成的查询会保留 `site:`、`after:`、`before:`、`filetype:` 和 `lang:`。DeepSeek Search 官方明确支持 `site:`、`-site:`、引号短语、`lang:`、`after:` 和 `before:`，其他来源把 `filetype:` 作为搜索引擎查询条件传递。

## 统一结果

```json
{
  "version": 1,
  "provider": "deepseek",
  "query": "DeepSeek site:github.com after:2025",
  "results": [{
    "rank": 1,
    "citation_key": "[1]",
    "title": "页面标题",
    "url": "https://example.com/page",
    "snippet": "展示摘要",
    "summary": "供模型阅读的摘要",
    "content": "可选页面内容",
    "source": "example.com",
    "published_at": "2026-01-01T00:00:00+00:00",
    "score": 2.4,
    "authority": 3,
    "provider": "deepseek",
    "traffic_id": "..."
  }],
  "citations": [{"citation_key": "[1]", "title": "页面标题", "url": "https://example.com/page"}]
}
```

完整搜索响应进入 HTTP 交通证据。模型观察只接收结构化摘要、结果引用和 `traffic_id`，引用格式统一为 `[n]`，报告或回复应把引用放在对应结论附近。

