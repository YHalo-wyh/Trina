"""Web 搜索适配器与统一引用契约测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trina.tools.http import HttpResult
from trina.tools.web_search import WebSearch, _parse_bing_rss, _parse_html, _query_with_filters


class FakeHttp:
    def __init__(self, *, get_result: HttpResult | None = None, post_result: HttpResult | None = None):
        self.get_result = get_result
        self.post_result = post_result
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_result or HttpResult(url=url, status=200, body="")

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_result or HttpResult(url=url, status=200, body='{"results": []}')


def context(http: FakeHttp, root: Path):
    return SimpleNamespace(
        http=http,
        config=SimpleNamespace(llm=SimpleNamespace(profile="", api_key_env="DEEPSEEK_API_KEY")),
        workspace=root,
    )


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = WebSearch()

    def test_filters_are_normalized_and_free_bing_results_have_citations(self):
        rss = """<rss><channel><item><title>Python docs</title><link>https://docs.python.org/a</link>
        <description>subprocess timeout</description><pubDate>Wed, 01 Jan 2025</pubDate></item></channel></rss>"""
        http = FakeHttp(get_result=HttpResult(url="https://www.bing.com/search", status=200, body=rss, traffic_id="t-1"))
        result = self.tool.run(context(http, self.root), "subprocess timeout", provider="bing", count=3,
                               site="docs.python.org", after="2025-01-01", filetype="pdf")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["provider"], "bing")
        self.assertEqual(result.data["citations"][0]["citation_key"], "[1]")
        query = http.get_calls[0][1]["params"]["q"]
        self.assertIn("site:docs.python.org", query)
        self.assertIn("after:2025-01-01", query)
        self.assertIn("filetype:pdf", query)
        self.assertEqual(result.evidence[0]["traffic_id"], "t-1")

    def test_auto_falls_back_from_bing_to_google(self):
        google = '<a href="/url?q=https%3A%2F%2Fexample.test%2Fdoc"><h3>Example</h3><span>source text</span></a>'
        http = FakeHttp(get_result=HttpResult(url="https://www.google.com/search", status=200, body=google, traffic_id="g-1"))
        http.get_result = HttpResult(url="https://www.bing.com/search", status=503, error="upstream")
        def get(url, **kwargs):
            if "bing.com" in url:
                http.get_calls.append((url, kwargs))
                return HttpResult(url=url, status=503, error="upstream")
            http.get_calls.append((url, kwargs))
            return HttpResult(url=url, status=200, body=google, traffic_id="g-1")
        http.get = get
        result = self.tool.run(context(http, self.root), "example", provider="auto")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["provider"], "google")
        self.assertEqual(result.data["citations"][0]["url"], "https://example.test/doc")
        self.assertTrue(result.data["warnings"])

    def test_deepseek_adapter_normalizes_official_response(self):
        body = '{"results":[{"title":"DeepSeek docs","url":"https://api.deepseek.com/docs",' \
               '"snippet":"API","summary":"Search API","site_name":"DeepSeek",' \
               '"date":"2026-01-01T00:00:00+00:00","authority":4,"rerank_score":2.7}]}'
        http = FakeHttp(post_result=HttpResult(url="https://api.deepseek.com/v1/tools/search", status=200,
                                                body=body, traffic_id="d-1"))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False):
            result = self.tool.run(context(http, self.root), "DeepSeek", provider="deepseek", need_content=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["results"][0]["authority"], 4)
        self.assertEqual(result.data["results"][0]["citation_key"], "[1]")
        self.assertEqual(http.post_calls[0][1]["json_body"]["need_content"], True)
        self.assertTrue(http.post_calls[0][1]["headers"]["Authorization"].startswith("Bearer "))

    def test_deepseek_requires_key_and_filter_validation_is_deterministic(self):
        http = FakeHttp()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            result = self.tool.run(context(http, self.root), "test", provider="deepseek")
        self.assertFalse(result.ok)
        self.assertIn("需要", result.error)
        with self.assertRaises(ValueError):
            _query_with_filters("test", after="2025/01/01")
        self.assertEqual(_parse_bing_rss("<rss><channel></channel></rss>"), [])
        self.assertEqual(_parse_html("<html><body>none</body></html>"), [])


if __name__ == "__main__":
    unittest.main()

