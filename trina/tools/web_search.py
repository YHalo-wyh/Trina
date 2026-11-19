"""统一 Web 搜索工具。

免费路径使用 Google/Bing 的公开搜索页面或 RSS，不需要供应商 Key。DeepSeek
路径调用官方 Search API，保留其结构化摘要、发布时间和权威等级。三种来源
都归一为同一组结果与引用，原始 HTTP 交互仍由 ``ctx.http`` 记录。
"""
from __future__ import annotations

import html
import json
import os
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from xml.etree import ElementTree

from .http import HttpResult
from .registry import ParamSpec, Tool, ToolResult, ToolSpec


_DATE_RE = re.compile(r"^(?:\d{4}|\d{4}-\d{2}-\d{2})$")
_FILETYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$")
_SITE_RE = re.compile(r"^[A-Za-z0-9.-]+(?::\d{1,5})?$")


def _check_date(value: str, label: str) -> str:
    value = str(value or "").strip()
    if value and not _DATE_RE.fullmatch(value):
        raise ValueError(f"{label} 必须是 YYYY 或 YYYY-MM-DD")
    return value


def _check_site(value: str) -> list[str]:
    sites = [item.strip().lower() for item in re.split(r"[,\s]+", str(value or "")) if item.strip()]
    if any(not _SITE_RE.fullmatch(item) for item in sites):
        raise ValueError("site 必须是域名或 host:port，可用逗号分隔")
    return sites


def _query_with_filters(query: str, *, site: str = "", after: str = "", before: str = "",
                        filetype: str = "", language: str = "") -> tuple[str, dict[str, str]]:
    query = str(query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    after, before = _check_date(after, "after"), _check_date(before, "before")
    filetype = str(filetype or "").strip().lower()
    if filetype and not _FILETYPE_RE.fullmatch(filetype):
        raise ValueError("filetype 只能包含字母、数字、点、加号、下划线或短横线")
    language = str(language or "").strip().lower()
    if language and not re.fullmatch(r"[a-z]{2}(?:,[a-z]{2})*", language):
        raise ValueError("language 必须是语言代码，例如 zh 或 zh,en")
    sites = _check_site(site)
    operators = {
        "site": " ".join(f"site:{item}" for item in sites),
        "after": f"after:{after}" if after else "",
        "before": f"before:{before}" if before else "",
        "filetype": f"filetype:{filetype}" if filetype else "",
        "lang": f"lang:{language}" if language else "",
    }
    additions = [item for item in operators.values() if item]
    normalized = " ".join([query, *additions]).strip()
    return normalized, operators


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


class _SearchHTMLParser(HTMLParser):
    """从 Google/Bing HTML 中提取标题、链接及其同一链接内的摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._anchor = ""
        self._anchor_text: list[str] = []
        self._heading = False
        self._heading_text: list[str] = []
        self.rows: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a":
            self._anchor = values.get("href") or ""
            self._anchor_text = []
        elif tag in {"h2", "h3"} and self._anchor:
            self._heading = True
            self._heading_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor:
            self._anchor_text.append(data)
        if self._heading:
            self._heading_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h2", "h3"} and self._heading:
            self._heading = False
        if tag != "a" or not self._anchor:
            return
        title = _text(" ".join(self._heading_text))
        href = self._anchor
        if title and href:
            self.rows.append({"title": title, "url": href,
                              "snippet": _text(" ".join(self._anchor_text))})
        self._anchor = ""
        self._anchor_text = []
        self._heading_text = []


def _unwrap_url(value: str) -> str:
    value = html.unescape(value or "")
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        query = parse_qs(urlsplit(value).query)
        value = query.get("q", query.get("url", [value]))[0]
    return unquote(value)


def _parse_html(body: str) -> list[dict[str, Any]]:
    parser = _SearchHTMLParser()
    parser.feed(body or "")
    rows: list[dict[str, Any]] = []
    for row in parser.rows:
        url = _unwrap_url(row["url"])
        if not url.startswith(("http://", "https://")):
            continue
        host = (urlsplit(url).hostname or "").lower()
        if host.endswith(("google.com", "googleusercontent.com", "bing.com")):
            continue
        snippet = row["snippet"]
        if snippet.startswith(row["title"]):
            snippet = snippet[len(row["title"]):].strip()
        rows.append({"title": row["title"], "url": url, "snippet": snippet[:500]})
    return rows


def _parse_bing_rss(body: str) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(body or "")
    except ElementTree.ParseError:
        return []
    rows: list[dict[str, Any]] = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "item":
            continue
        values = {child.tag.rsplit("}", 1)[-1]: (child.text or "") for child in item}
        url = _unwrap_url(values.get("link", ""))
        if not url.startswith(("http://", "https://")):
            continue
        rows.append({"title": _text(values.get("title", "")), "url": url,
                     "snippet": _text(values.get("description", "")),
                     "published_at": _text(values.get("pubDate", ""))})
    return rows


def _deepseek_key(ctx: Any) -> str:
    from ..providers import ProfileStore

    config = ctx.config.llm
    try:
        key = ProfileStore().key(getattr(config, "profile", ""), getattr(config, "api_key_env", ""))
    except (OSError, ValueError):
        key = ""
    return key or os.environ.get("DEEPSEEK_API_KEY", "").strip()


def _normalize_rows(rows: list[dict[str, Any]], provider: str, traffic_id: str, count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("url") or "").strip()
        title = _text(str(row.get("title") or ""))
        if not url or not title or url in seen:
            continue
        seen.add(url)
        index = len(normalized) + 1
        citation_key = f"[{index}]"
        item = {
            "rank": index,
            "citation_key": citation_key,
            "title": title[:300],
            "url": url,
            "snippet": _text(str(row.get("snippet") or ""))[:800],
            "summary": _text(str(row.get("summary") or ""))[:2400],
            "content": str(row.get("content") or "")[:12000],
            "source": _text(str(row.get("site_name") or urlsplit(url).hostname or ""))[:200],
            "published_at": str(row.get("date") or row.get("published_at") or ""),
            "score": row.get("rerank_score", row.get("score")),
            "authority": row.get("authority"),
            "provider": provider,
            "traffic_id": traffic_id,
        }
        normalized.append(item)
        citations.append({key: item[key] for key in
                          ("citation_key", "title", "url", "source", "published_at", "provider", "traffic_id")})
        if len(normalized) >= count:
            break
    return normalized, citations


def _result(ctx: Any, http_result: HttpResult, provider: str, query: str, filters: dict[str, str],
            rows: list[dict[str, Any]], count: int, *, warning: str = "") -> ToolResult:
    results, citations = _normalize_rows(rows, provider, http_result.traffic_id, count)
    data = {
        "version": 1,
        "provider": provider,
        "query": query,
        "filters": filters,
        "total": len(results),
        "results": results,
        "citations": citations,
        "warnings": [warning] if warning else [],
    }
    evidence = [http_result.as_evidence(kind="web_search", note=f"{provider} 搜索 {query}",
                                        result_count=len(results), citations=citations)]
    findings = [f"{item['citation_key']} {item['title']} — {item['url']}" for item in results[:8]]
    summary = f"{provider} 搜索返回 {len(results)} 条结果"
    if warning:
        summary += f"（{warning}）"
    return ToolResult(ok=True, summary=summary, data=data, evidence=evidence,
                      key_findings=findings, artifact_ref=f"traffic:{http_result.traffic_id}" if http_result.traffic_id else "",
                      metadata={"provider": provider, "query": query, "citations": citations})


def _free_search(ctx: Any, provider: str, query: str, count: int) -> tuple[str, HttpResult, list[dict[str, Any]]]:
    if provider == "bing":
        response = ctx.http.get("https://www.bing.com/search",
                                params={"q": query, "format": "rss", "count": count},
                                headers={"Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"},
                                required_level="detect", payload_label="web_search:bing")
        return provider, response, _parse_bing_rss(response.body)
    response = ctx.http.get("https://www.google.com/search",
                            params={"q": query, "num": count, "gbv": "1", "filter": "0"},
                            headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
                            required_level="detect", payload_label="web_search:google")
    return "google", response, _parse_html(response.body)


class WebSearch(Tool):
    spec = ToolSpec(
        name="web_search",
        summary="检索公开 Web 资料并返回统一结构化结果与 [n] 引用，可用 Google、Bing 或 DeepSeek Search",
        category="web",
        action_level="detect",
        params={
            "query": ParamSpec("str", "搜索词；可直接包含 site:、after:、before:、filetype: 等操作符"),
            "provider": ParamSpec("str", "搜索来源", required=False, default="auto", enum=["auto", "google", "bing", "deepseek"]),
            "count": ParamSpec("int", "结果数（1–20）", required=False, default=8),
            "site": ParamSpec("str", "限定域名，可用逗号分隔", required=False, default=""),
            "after": ParamSpec("str", "发布日期下限 YYYY 或 YYYY-MM-DD", required=False, default=""),
            "before": ParamSpec("str", "发布日期上限 YYYY 或 YYYY-MM-DD", required=False, default=""),
            "filetype": ParamSpec("str", "文件类型，例如 pdf、md", required=False, default=""),
            "language": ParamSpec("str", "语言代码，例如 zh 或 zh,en", required=False, default=""),
            "need_content": ParamSpec("bool", "DeepSeek 是否返回页面内容", required=False, default=False),
        },
        produces=["web_results", "citations"],
    )

    def run(self, ctx, query, provider="auto", count=8, site="", after="", before="", filetype="",
            language="", need_content=False):
        try:
            if provider not in {"auto", "google", "bing", "deepseek"}:
                return ToolResult(ok=False, category="invalid", error="provider 必须是 auto/google/bing/deepseek")
            count = int(count)
            if not 1 <= count <= 20:
                return ToolResult(ok=False, category="invalid", error="count 必须在 [1,20]")
            normalized, filters = _query_with_filters(query, site=site, after=after, before=before,
                                                      filetype=filetype, language=language)
            if provider == "deepseek":
                key = _deepseek_key(ctx)
                if not key:
                    return ToolResult(ok=False, category="configuration", error="DeepSeek Search 需要 DEEPSEEK_API_KEY 或当前供应商 Key")
                if len(normalized) > 128:
                    return ToolResult(ok=False, category="invalid", error="DeepSeek Search 查询最多 128 个字符，请缩短搜索条件")
                response = ctx.http.post(
                    "https://api.deepseek.com/v1/tools/search",
                    json_body={"query": normalized, "count": count, "need_content": bool(need_content)},
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    required_level="detect", payload_label="web_search:deepseek",
                )
                if response.error or not 200 <= response.status < 300:
                    return ToolResult(ok=False, category="network", error=response.error or f"HTTP {response.status}",
                                      data={"provider": "deepseek", "query": normalized}, evidence=[response.as_evidence(kind="web_search")])
                try:
                    payload = json.loads(response.body or "{}")
                    rows = payload.get("results", []) if isinstance(payload, dict) else []
                except json.JSONDecodeError as exc:
                    return ToolResult(ok=False, category="protocol", error=f"DeepSeek Search 返回无效 JSON: {exc}")
                return _result(ctx, response, "deepseek", normalized, filters, rows if isinstance(rows, list) else [], count)

            providers = [provider] if provider in {"google", "bing"} else ["bing", "google"]
            warnings: list[str] = []
            for selected in providers:
                selected, response, rows = _free_search(ctx, selected, normalized, count)
                if response.error or not 200 <= response.status < 300:
                    warnings.append(f"{selected}: {response.error or 'HTTP ' + str(response.status)}")
                    continue
                if not rows and selected == "bing" and provider == "auto":
                    warnings.append("bing: RSS 无结果，已尝试 Google")
                    continue
                return _result(ctx, response, selected, normalized, filters, rows, count,
                               warning="；".join(warnings))
            return ToolResult(ok=False, category="network", error="；".join(warnings) or "搜索没有返回结果")
        except (TypeError, ValueError, OSError) as exc:
            return ToolResult(ok=False, category="invalid", error=str(exc))


TOOLS = [WebSearch()]

