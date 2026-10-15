"""Web 层工具：目录爆破、页面表面提取、参数反射检测。

设计要点
--------
- **护栏优先**：目录爆破在通过 `ScopeGuard.check_http(base)` 后，才用 `aiohttp`
  对**同源**路径发起高并发请求；每个请求前做一次轻量限速。
- **软 404 过滤**：先用随机路径建立 `(status, content-length)` 基线，状态码相同
  且长度落在 ±5% 容差内的结果一律丢弃，避免海量假阳性。
- **只读**：全部动作等级为 `detect`，不发送任何破坏性载荷。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup, Comment

from .http import DEFAULT_HEADERS
from .registry import ParamSpec, Tool, ToolResult, ToolSpec, denied_result, register

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_EXTENSIONS: tuple[str, ...] = ("", ".php", ".html", ".txt", ".bak", ".zip", ".json", ".js")
DEFAULT_MATCH_STATUS: tuple[int, ...] = (200, 201, 204, 301, 302, 401, 403, 405, 500)

#: 单次扫描最多构造的请求目标数（防御性上限，防止误用超大字典）
MAX_TARGETS = 2000
#: 单请求超时（秒）
REQUEST_TIMEOUT = 8.0
#: 软 404 长度容差
LENGTH_TOLERANCE = 0.05

#: 反射探针：纯文本 + 特殊字符
PROBE_PLAIN = "trina7x9z"
PROBE_SPECIAL = "trina7x9z'\"><"
PROBES: tuple[str, ...] = (PROBE_PLAIN, PROBE_SPECIAL)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _normalize_base(url: str) -> str:
    """补全 scheme，返回用于拼接的 base URL。"""
    url = (url or "").strip()
    if not url:
        return url
    if not url.split("://", 1)[0].count("/") and "://" not in url:
        url = "http://" + url
    return url


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


# ---------------------------------------------------------------------------
# 目录爆破核心（与护栏解耦，便于单测）
# ---------------------------------------------------------------------------


def _load_wordlist(path: str | Path) -> list[str]:
    """读取字典：跳过空行与 `#` 注释，去掉前导 `/`。"""
    words: list[str] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        word = line.strip()
        if not word or word.startswith("#"):
            continue
        words.append(word.lstrip("/"))
    return words


def _build_targets(base_url: str, words: list[str], extensions: Any) -> list[str]:
    """把字典词 × 扩展名展开成绝对 URL 列表（保持顺序、去重）。"""
    base = (base_url or "").rstrip("/")
    exts = ["" if e in (None, "") else str(e) for e in (extensions or [""])]
    seen: set[str] = set()
    targets: list[str] = []
    for word in words:
        for ext in exts:
            url = f"{base}/{word}{ext}"
            if url not in seen:
                seen.add(url)
                targets.append(url)
    return targets


def _baseline_hit(status: int, length: int, baseline: list[tuple[int, int]], tolerance: float = LENGTH_TOLERANCE) -> bool:
    """判断某响应是否命中软 404 基线（状态码相同、长度在容差内）。"""
    for b_status, b_len in baseline:
        if b_status != status:
            continue
        if b_len == 0:
            if length <= 1:
                return True
            continue
        if abs(length - b_len) <= tolerance * b_len:
            return True
    return False


async def _fetch(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    timeout: float = REQUEST_TIMEOUT,
    throttle: Callable[[str], float] | None = None,
    host: str = "",
) -> dict[str, Any]:
    """发起一次同源请求并归一化结果；任何网络异常都降级为 error 记录。"""
    if throttle is not None and host:
        try:
            wait = float(throttle(host))
        except Exception:  # noqa: BLE001 - 限速实现异常不应中断扫描
            wait = 0.0
        if wait > 0:
            await asyncio.sleep(min(wait, 5.0))
    try:
        async with session.request(
            method,
            url,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            body = await resp.read()
            return {"url": url, "method": method, "status": resp.status, "length": len(body), "error": ""}
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        return {"url": url, "method": method, "status": 0, "length": 0, "error": f"{type(exc).__name__}: {exc}"}


async def _build_baseline(
    session: aiohttp.ClientSession,
    base: str,
    *,
    timeout: float,
    throttle: Callable[[str], float] | None,
    host: str,
    probes: int = 3,
) -> list[tuple[int, int]]:
    """用随机路径建立软 404 基线：(status, content-length)。"""
    token = uuid.uuid4().hex
    candidates = [f"{base}/{token}.html", f"{base}/trina-notfound-{token}/", f"{base}/{token[:8]}.php"]
    baseline: list[tuple[int, int]] = []
    for url in candidates[:probes]:
        result = await _fetch(session, "GET", url, timeout=timeout, throttle=throttle, host=host)
        if not result["error"] and result["status"]:
            baseline.append((int(result["status"]), int(result["length"])))
    return baseline


async def _scan_core(
    base_url: str,
    targets: list[str],
    *,
    match_status: Any,
    concurrency: int,
    methods: Any,
    timeout: float = REQUEST_TIMEOUT,
    throttle: Callable[[str], float] | None = None,
) -> dict[str, Any]:
    """并发扫描 + 软 404 过滤。返回 {results, baseline, scanned, raw_count}。"""
    host = _host_of(base_url)
    statuses = {int(s) for s in (match_status or DEFAULT_MATCH_STATUS)}
    method_list = [str(m).upper() for m in (methods or ["GET"])]
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    connector = aiohttp.TCPConnector(limit=max(1, int(concurrency)), ssl=False)
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout_cfg, headers=DEFAULT_HEADERS) as session:
        baseline = await _build_baseline(session, base_url.rstrip("/"), timeout=timeout, throttle=throttle, host=host)

        async def _one(url: str, method: str) -> dict[str, Any]:
            async with semaphore:
                return await _fetch(session, method, url, timeout=timeout, throttle=throttle, host=host)

        tasks = [_one(url, method) for url in targets for method in method_list]
        raw = await asyncio.gather(*tasks) if tasks else []

    results: list[dict[str, Any]] = []
    for item in raw:
        if item["error"] or not item["status"]:
            continue
        if int(item["status"]) not in statuses:
            continue
        if _baseline_hit(int(item["status"]), int(item["length"]), baseline):
            continue
        results.append(item)
    return {"results": results, "baseline": baseline, "scanned": len(raw), "raw_count": len(raw)}


# ---------------------------------------------------------------------------
# 工具 1：目录爆破
# ---------------------------------------------------------------------------


class DirBruteforceTool(Tool):
    """基于字典的目录/文件探测，内置软 404 过滤。"""

    spec = ToolSpec(
        name="dir_bruteforce",
        summary="目录/文件爆破：用字典探测常见路径与备份文件，自动过滤软 404 假阳性",
        category="web",
        action_level="detect",
        params={
            "url": ParamSpec("str", "目标 base URL（可省略 scheme）", required=True),
            "wordlist": ParamSpec("str", "字典文件路径", required=False),
            "extensions": ParamSpec("list", "附加扩展名（含空串表示原样）", required=False, default=list(DEFAULT_EXTENSIONS)),
            "match_status": ParamSpec("list", "视为有效的状态码", required=False, default=list(DEFAULT_MATCH_STATUS)),
            "concurrency": ParamSpec("int", "并发数", required=False),
            "methods": ParamSpec("list", "HTTP 方法", required=False, default=["GET"]),
        },
        produces=["paths"],
        examples=["dir_bruteforce url=http://10.0.0.5:8080", "dir_bruteforce url=http://10.0.0.5 extensions=[.php,.bak] concurrency=10"],
        notes="先对 base 做一次护栏校验，随后对同源路径并发请求；随机路径建立基线以过滤软 404。",
    )

    def run(
        self,
        ctx,
        url: str,
        wordlist: str | None = None,
        extensions: Any = None,
        match_status: Any = None,
        concurrency: int | None = None,
        methods: Any = None,
    ) -> ToolResult:
        base = _normalize_base(url)
        decision = ctx.guard.check_http(base, required_level="detect")
        if decision.denied:
            return denied_result(decision, tool="dir_bruteforce")

        exts = list(extensions) if extensions else list(DEFAULT_EXTENSIONS)
        statuses = [int(s) for s in (match_status or DEFAULT_MATCH_STATUS)]
        method_list = [str(m).upper() for m in (methods or ["GET"])]
        conc = int(concurrency) if concurrency else min(20, int(ctx.config.scope.max_host_concurrency))

        wl_path = Path(wordlist) if wordlist else ctx.config.tools.wordlist_path("dirs")
        try:
            words = _load_wordlist(wl_path)
        except OSError as exc:
            return ToolResult(
                ok=False,
                category="web",
                summary=f"字典不可读: {wl_path}",
                error=f"{type(exc).__name__}: {exc}",
                data={"wordlist": str(wl_path)},
                next_hints=["确认字典路径，或留空使用默认字典"],
            )
        if not words:
            return ToolResult(ok=False, category="web", summary=f"字典为空: {wl_path}", error="字典为空")

        targets = _build_targets(base, words, exts)
        truncated = False
        if len(targets) > MAX_TARGETS:
            truncated = True
            targets = targets[:MAX_TARGETS]

        started = time.monotonic()
        try:
            outcome = asyncio.run(
                _scan_core(
                    base,
                    targets,
                    match_status=statuses,
                    concurrency=conc,
                    methods=method_list,
                    timeout=REQUEST_TIMEOUT,
                    throttle=ctx.guard.throttle,
                )
            )
        except RuntimeError as exc:  # 事件循环冲突（正常调用链不会触发）
            return ToolResult(ok=False, category="web", summary="扫描失败", error=str(exc))

        duration = time.monotonic() - started
        results = outcome["results"]
        host = _host_of(base)

        groups: dict[int, list[str]] = {}
        for item in results:
            groups.setdefault(int(item["status"]), []).append(item["url"])
        group_text = " ".join(f"{code}:{len(urls)}" for code, urls in sorted(groups.items()))
        summary = f"发现 {len(results)} 个有效路径" + (f"（{group_text}）" if results else "（均被软 404 基线过滤或未命中）")

        facts_paths = [
            {"host": host, "url": item["url"], "status": int(item["status"]), "length": int(item["length"])}
            for item in results[:60]
        ]
        evidence = [
            {
                "kind": "http",
                "target": item["url"],
                "method": item["method"],
                "status": int(item["status"]),
                "request": {"method": item["method"], "url": item["url"]},
                "response": {"status": int(item["status"]), "length": int(item["length"])},
                "note": "dir_bruteforce 命中（非软 404）",
            }
            for item in results[:10]
        ]

        return ToolResult(
            ok=True,
            category="web",
            summary=summary,
            data={
                "base": base,
                "wordlist": str(wl_path),
                "words": len(words),
                "extensions": [str(e) for e in exts],
                "match_status": statuses,
                "methods": method_list,
                "concurrency": conc,
                "targets": len(targets),
                "scanned": outcome["scanned"],
                "baseline": [{"status": s, "length": ln} for s, ln in outcome["baseline"]],
                "groups": {str(code): urls[:20] for code, urls in sorted(groups.items())},
                "results": [
                    {"url": item["url"], "status": int(item["status"]), "length": int(item["length"]), "method": item["method"]}
                    for item in results[:60]
                ],
                "truncated": truncated,
                "duration": round(duration, 2),
            },
            facts={"paths": facts_paths},
            evidence=evidence,
            command=f"aiohttp {','.join(method_list)} x{len(targets)} -> {base}",
            next_hints=(
                ["对命中路径逐个做 web_surface / poc_scan 深入分析"]
                if results
                else ["换用更大的字典或调整 extensions/match_status 后重试"]
            ),
        )


# ---------------------------------------------------------------------------
# 工具 2：页面表面提取
# ---------------------------------------------------------------------------

_COMMENT_HINTS = (
    "password", "passwd", "pwd", "secret", "token", "api", "key", "debug",
    "todo", "fixme", "user", "admin", "test", "backup", "内部", "密码", "凭据",
)


class WebSurfaceTool(Tool):
    """解析 HTML，提取表单、链接、参数名、注释与 JS 文件。"""

    spec = ToolSpec(
        name="web_surface",
        summary="页面表面提取：表单 / 链接 / 参数名 / HTML 注释（可能含凭据与调试信息）/ JS 文件",
        category="web",
        action_level="detect",
        params={"url": ParamSpec("str", "目标页面 URL", required=True)},
        produces=["paths"],
        examples=["web_surface url=http://10.0.0.5/login.php"],
    )

    def run(self, ctx, url: str) -> ToolResult:
        target = _normalize_base(url)
        res = ctx.http.get(target, required_level="detect", payload_label="web_surface")
        if res.error:
            return ToolResult(
                ok=False,
                category="web",
                summary=f"请求失败: {res.error}",
                error=res.error,
                evidence=[res.as_evidence(note="web_surface 拉取失败")],
            )

        soup = BeautifulSoup(res.body or "", "html.parser")

        forms: list[dict[str, Any]] = []
        for form in soup.find_all("form"):
            action = form.get("action") or ""
            method = str(form.get("method") or "GET").upper()
            inputs: list[dict[str, str]] = []
            for field in form.find_all(["input", "textarea", "select", "button"]):
                name = field.get("name")
                if not name:
                    continue
                inputs.append({"name": str(name), "type": str(field.get("type") or field.name or "text")})
            forms.append({"action": urljoin(target, action) if action else target, "method": method, "inputs": inputs})

        links: list[str] = []
        seen_links: set[str] = set()

        def _add_link(raw: str) -> None:
            if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
                return
            absolute = urljoin(target, raw)
            if absolute not in seen_links:
                seen_links.add(absolute)
                links.append(absolute)

        for anchor in soup.find_all("a", href=True):
            _add_link(anchor["href"])
        script_srcs = [s["src"] for s in soup.find_all("script", src=True)]
        for src in script_srcs:
            _add_link(src)
        for tag in soup.find_all("link", href=True):
            _add_link(tag["href"])

        js_files: list[str] = []
        for src in script_srcs:
            absolute = urljoin(target, src)
            if absolute not in js_files:
                js_files.append(absolute)

        params: list[str] = []
        for candidate in list(links) + [target]:
            for key, _value in parse_qsl(urlsplit(candidate).query, keep_blank_values=True):
                if key not in params:
                    params.append(key)

        comments: list[str] = []
        interesting: list[str] = []
        for node in soup.find_all(string=lambda text: isinstance(text, Comment)):
            text = str(node).strip()
            if not text:
                continue
            comments.append(text[:300])
            low = text.lower()
            if any(hint in low for hint in _COMMENT_HINTS):
                interesting.append(text[:300])

        host = _host_of(target)
        paths = [{"host": host, "url": link} for link in links[:60]]

        summary = f"提取到 {len(forms)} 个表单、{len(params)} 个参数、{len(comments)} 条注释"

        return ToolResult(
            ok=True,
            category="web",
            summary=summary,
            data={
                "url": target,
                "status": res.status,
                "title": res.title,
                "forms": forms,
                "params": params,
                "links": links[:80],
                "js_files": js_files[:40],
                "comments": comments[:40],
                "interesting_comments": interesting[:20],
            },
            facts={"paths": paths},
            evidence=[res.as_evidence(note=f"web_surface: {len(forms)} 表单 / {len(params)} 参数 / {len(comments)} 注释")],
            next_hints=(
                ["对表单字段用 param_reflect_scan / sql 注入类工具验证"]
                if forms
                else ["尝试 dir_bruteforce 扩大路径面"]
            ),
        )


# ---------------------------------------------------------------------------
# 工具 3：参数反射检测
# ---------------------------------------------------------------------------


class ParamReflectScanTool(Tool):
    """向参数注入无害探针，判断是否原样回显（潜在反射型 XSS）。"""

    spec = ToolSpec(
        name="param_reflect_scan",
        summary="参数反射检测：注入无害探针，判断输入是否原样回显（潜在反射型 XSS）",
        category="web",
        action_level="detect",
        params={
            "url": ParamSpec("str", "目标 URL", required=True),
            "params": ParamSpec("list", "待测参数名列表", required=True),
            "method": ParamSpec("str", "HTTP 方法", required=False, default="GET", enum=["GET", "POST"]),
            "data": ParamSpec("dict", "附加表单字段（POST 时的其他 kv）", required=False),
        },
        produces=["findings"],
        examples=["param_reflect_scan url=http://10.0.0.5/search.php params=[q]"],
    )

    def run(self, ctx, url: str, params: Any, method: str = "GET", data: dict | None = None) -> ToolResult:
        target = _normalize_base(url)
        method = str(method or "GET").upper()
        records: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        escaped_params: list[str] = []
        reflected_params: list[str] = []

        for name in params:
            states: dict[str, tuple[str, Any]] = {}
            for probe in PROBES:
                if method == "POST":
                    body = dict(data or {})
                    body[str(name)] = probe
                    res = ctx.http.post(target, data=body, required_level="detect", payload_label=probe)
                else:
                    query = dict(data or {})
                    query[str(name)] = probe
                    res = ctx.http.get(target, params=query, required_level="detect", payload_label=probe)

                if res.error:
                    states[probe] = ("error", res)
                    records.append({"param": name, "probe": probe, "status": res.status, "reflected": "error", "error": res.error})
                    continue

                body_text = res.body or ""
                if probe in body_text:
                    state = "raw"
                elif PROBE_PLAIN in body_text:
                    state = "escaped"
                else:
                    state = "none"
                states[probe] = (state, res)
                records.append({"param": name, "probe": probe, "status": res.status, "reflected": state, "url": res.url})

            special_state, special_res = states.get(PROBE_SPECIAL, ("none", None))
            plain_state, plain_res = states.get(PROBE_PLAIN, ("none", None))

            hit_res = None
            hit_payload = ""
            hit_confidence = ""
            if special_state == "raw":
                hit_res, hit_payload, hit_confidence = special_res, PROBE_SPECIAL, "medium"
            elif plain_state == "raw":
                hit_res, hit_payload, hit_confidence = plain_res, PROBE_PLAIN, "low"

            if hit_res is not None:
                reflected_params.append(str(name))
                finding = {
                    "title": f"参数 {name} 原样反射回显（潜在反射型 XSS）",
                    "vuln_class": "xss_reflected",
                    "severity": "medium",
                    "status": "detected",
                    "confidence": hit_confidence,
                    "target": target,
                    "url": target,
                    "parameter": str(name),
                    "method": method,
                    "tool": "param_reflect_scan",
                    "payload": hit_payload,
                    "cwe": "CWE-79",
                    "description": f"参数 {name} 的取值被原样回显到响应中，若回显落在可执行上下文则构成反射型 XSS。",
                    "impact": "攻击者可构造恶意链接诱导受害者点击，在其浏览器中执行脚本。",
                    "remediation": "对所有输出做上下文相关的 HTML/属性/URL 编码；启用 CSP 作为纵深防御。",
                    "evidence": [hit_res.as_evidence(note=f"参数 {name} 反射探针 {hit_payload}", parameter=str(name), probe=hit_payload)],
                }
                findings.append(finding)
                evidence.append(hit_res.as_evidence(note=f"参数 {name} 反射", parameter=str(name)))
            elif special_state == "escaped" or plain_state == "escaped":
                escaped_params.append(str(name))

        hit_ids = ", ".join(reflected_params) if reflected_params else "无"
        summary = (
            f"反射检测：{len(reflected_params)}/{len(list(params))} 个参数 potentially XSS"
            f"（HTML 转义后回显 {len(escaped_params)} 个）；命中参数: {hit_ids}"
        )

        return ToolResult(
            ok=True,
            category="web",
            summary=summary,
            data={
                "engine": "builtin",
                "url": target,
                "method": method,
                "params": [str(p) for p in params],
                "probes": list(PROBES),
                "records": records,
                "xss_count": len(findings),
                "reflected_params": reflected_params,
                "escaped_params": escaped_params,
            },
            facts={"findings": findings},
            evidence=evidence[:10],
            next_hints=(
                ["使用浏览器/PoC 页面确认可执行上下文，再升级为 verified"]
                if findings
                else ["尝试其他参数或改用 POST 方式重测"]
            ),
        )


TOOLS: list[Tool] = [DirBruteforceTool(), WebSurfaceTool(), ParamReflectScanTool()]

for _tool in TOOLS:  # 兼容模块级全局注册表
    register(_tool)

__all__ = [
    "TOOLS",
    "DirBruteforceTool",
    "WebSurfaceTool",
    "ParamReflectScanTool",
    "DEFAULT_EXTENSIONS",
    "DEFAULT_MATCH_STATUS",
    "_load_wordlist",
    "_build_targets",
    "_baseline_hit",
    "_scan_core",
    "_normalize_base",
]
