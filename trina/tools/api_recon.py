"""有界静态与运行时 API 侦察；候选路由不等于后端可访问或权限绕过。"""
from __future__ import annotations

import json
import re
from collections import deque
from typing import Any
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from .browser import _browser_error, _call_result, _pool, _preview
from ..browser import BrowserError
from .casebook import CaseTool, insert_assets
from .registry import ParamSpec as P, Tool, ToolResult, ToolSpec

_QUOTED = re.compile(r'''["'`]((?:https?://|/|\./|\.\./)[^\s"'`<>\\]{1,300})["'`]''')
_ROUTES = re.compile(r'''\b(?:path|route)\s*:\s*["'`]([^"'`]{1,200})["'`]''')
_PERMISSIONS = re.compile(r'''\b(?:permission|permissions|authority|roles?)\s*:\s*(\[[^\]\n]{1,300}\]|["'][^"']{1,100}["'])''')
_STATIC = re.compile(r"\.(?:png|jpe?g|gif|svg|ico|woff2?|ttf|css|map)(?:\?|$)", re.I)


def route_tree(routes: list[str]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for route in sorted(set(routes)):
        node = tree
        for part in route.strip("/").split("/"):
            if part:
                node = node.setdefault(part, {})
    return tree


class ApiReconStatic(Tool):
    spec = ToolSpec(name="api_recon_static", category="web", action_level="detect", produces=["paths"],
        summary="从HTML/MPA、JS、manifest递归采集同源资源、API候选、前端路由和权限线索；有请求/深度预算",
        params={"url": P("str", "HTTP(S)入口"),
            "max_requests": P("int", "最多请求数，1..60", required=False, default=20),
            "max_depth": P("int", "资源/页面递归深度，0..4", required=False, default=2),
            "headers": P("dict", "可选会话请求头", required=False, default={})})

    def run(self, ctx, url, max_requests=20, max_depth=2, headers=None):
        if not 1 <= max_requests <= 60 or not 0 <= max_depth <= 4 or urlsplit(url).scheme not in {"http", "https"}:
            return ToolResult(ok=False, error="需要HTTP(S)入口、1..60请求预算和0..4深度")
        origin = urlsplit(url)[:2]
        queue = deque([(url, 0)])
        visited: set[str] = set()
        assets: dict[str, dict] = {}
        routes: set[str] = set()
        permissions: list[dict] = []
        sources = []
        evidence = []
        while queue and len(visited) < max_requests:
            current, depth = queue.popleft()
            current = urldefrag(current)[0]
            if current in visited or urlsplit(current)[:2] != origin:
                continue
            visited.add(current)
            # 自动递归不跟随跨源跳转；每个请求仍独立过 ScopeGuard。
            res = ctx.http.get(current, headers=headers, allow_redirects=False, required_level=ctx.effective_level)
            sources.append({"url": current, "status": res.status, "error": res.error, "traffic_id": res.traffic_id})
            evidence.append(res.as_evidence("api_recon.source"))
            if not res.ok or not 200 <= res.status < 300:
                continue
            text = res.body
            candidates: list[tuple[str, bool]] = []
            if "<html" in text.lower() or "text/html" in res.headers.get("Content-Type", "").lower():
                soup = BeautifulSoup(text, "html.parser")
                for tag in soup.find_all(["script", "a", "link", "form"]):
                    raw = tag.get("src") or tag.get("href") or tag.get("action")
                    if isinstance(raw, str):
                        # form action 只记录，绝不自动提交或把退出/删除链接当静态资源抓取。
                        follow = tag.name == "script" or (tag.name == "link" and raw.split("?")[0].endswith((".js", ".json")))
                        follow = follow or (tag.name == "a" and urlsplit(raw).path.endswith((".html", ".htm", "/")))
                        candidates.append((raw, follow))
            candidates += [(m, bool(re.search(r"\.(?:m?js|json)(?:\?|$)", m))) for m in _QUOTED.findall(text)]
            # Vite/webpack manifest 中的相对 chunk 文件。
            candidates += [(m, True) for m in re.findall(r'''["']([^\s"'<>]+\.(?:m?js|json))["']''', text)]
            for path in _ROUTES.findall(text):
                routes.add(path)
            for permission in _PERMISSIONS.findall(text)[:100]:
                permissions.append({"source": current, "candidate": permission, "traffic_id": res.traffic_id})
            for raw, follow in candidates[:1000]:
                absolute = urldefrag(urljoin(current, raw))[0]
                parts = urlsplit(absolute)
                if parts.scheme not in {"http", "https"} or parts[:2] != origin or _STATIC.search(absolute):
                    continue
                assets.setdefault(absolute, {"target": absolute, "kind": "static" if follow else "api_candidate", "method": "GET",
                    "parameters": parse_qs(parts.query), "evidence_refs": [res.traffic_id]})
                if follow and depth < max_depth and absolute not in visited:
                    queue.append((absolute, depth + 1))
        if assets:
            # insert_assets 的批次上限防止单个 bundle 造成无限状态膨胀。
            rows = list(assets.values())[:1000]
            for offset in range(0, len(rows), 100):
                insert_assets(ctx, rows[offset:offset + 100])
        ctx.state.casebook["api_recon"] = {"routes": sorted(routes), "permissions": permissions[:500]}
        artifact = ctx.save_artifact("api-recon-static.json", json.dumps({"sources": sources, "assets": list(assets.values()),
            "routes": sorted(routes), "route_tree": route_tree(list(routes)), "permission_candidates": permissions}, ensure_ascii=False, indent=2))
        success = any(s["status"] and 200 <= s["status"] < 300 and not s["error"] for s in sources)
        return ToolResult(ok=success, summary=f"请求 {len(visited)} 个资源，采集 {len(assets)} 条候选、{len(routes)} 条前端路由",
            data={"sources": sources, "assets_count": len(assets), "routes": sorted(routes)[:100], "route_tree": route_tree(list(routes)),
                  "permission_candidates": permissions[:30], "artifact": str(artifact), "budget_exhausted": bool(queue),
                  "limitations": "静态字符串为候选；不求值动态拼接，权限标签不能证明后端授权行为"},
            evidence=evidence, facts={"paths": [{"url": a} for a in list(assets)[:1000]]})


# 监听器挂在实际 Playwright Page 对象上，跨 CLI 命令持续收集；不替换登录/权限逻辑。
_START = r'''async page => {
  if (page.__trinaRecon) throw new Error('capture already active; stop it first');
  const state = {requests: [], websockets: [], active: true};
  const request = req => {
    if (state.requests.length >= 1000) return;
    const headers = Object.fromEntries(Object.entries(req.headers()).filter(([k]) => !/authorization|cookie/i.test(k)));
    state.requests.push({url:req.url(), method:req.method(), resource_type:req.resourceType(),
      body:(req.postData() || '').slice(0,8000), headers, query:Object.fromEntries(new URL(req.url()).searchParams)});
  };
  const response = res => {
    const item = [...state.requests].reverse().find(r=>r.url===res.url() && !r.status);
    if (item) { item.status=res.status(); item.content_type=res.headers()['content-type'] || ''; }
  };
  const websocket = ws => {
    if (state.websockets.length >= 100) return;
    const item={url:ws.url(), frames:[]}; state.websockets.push(item);
    const received = frame => { if (state.active && item.frames.length < 20) item.frames.push(String(frame.payload).slice(0,1000)); };
    ws.on('framereceived', received); state.sockets.push([ws,received]);
  };
  state.sockets=[]; state.handlers={request,response,websocket};
  page.on('request',request); page.on('response',response); page.on('websocket',websocket);
  page.__trinaRecon=state;
  return {active:true, note:'use normal navigation/interactions, then collect or stop'};
}'''
_COLLECT = r'''async page => {
  const state=page.__trinaRecon;
  if (!state) throw new Error('capture not active');
  return {requests:state.requests, websockets:state.websockets,
    routes:await page.evaluate(()=>({url:location.href, links:[...document.querySelectorAll('a[href]')].slice(0,300).map(a=>a.getAttribute('href'))})),
    active:state.active};
}'''
_STOP = _COLLECT.replace("return {requests:", "state.active=false; for(const [name,handler] of Object.entries(state.handlers)) page.off(name,handler); for(const [ws,handler] of state.sockets) ws.off('framereceived',handler); delete page.__trinaRecon; return {requests:")


class ApiReconRuntime(Tool):
    spec = ToolSpec(name="api_recon_runtime", category="browser", action_level="detect",
        summary="在浏览器会话采集真实请求/参数样本、WS帧和SSE类型；start后正常交互，再collect/stop",
        params={"session_id": P("str", "已有浏览器会话"), "action": P("str", "采集操作", enum=["start", "collect", "stop"])})

    def run(self, ctx, session_id, action):
        try:
            call = _pool(ctx).invoke(ctx, session_id, ["run-code", {"start": _START, "collect": _COLLECT, "stop": _STOP}[action]])
            result = _call_result(call, session_id=session_id, summary=f"运行时API采集 {action} 已执行", kind="api_recon.runtime",
                data={"output": _preview(call.payload, 10000), "limitations": "只覆盖监听开始后的当前页面活动；SSE记录请求类型，不解析事件流；完整样本见 evidence_ref"})
            if call.ok and action != "start":
                payload = call.payload
                if isinstance(payload, dict) and isinstance(payload.get("result"), str):
                    try:
                        payload = json.loads(payload["result"])
                    except json.JSONDecodeError:
                        payload = {}
                if isinstance(payload, dict) and isinstance(payload.get("requests"), list):
                    rows = payload["requests"]
                    result.data["requests"] = rows[:100]
                    result.data["websockets"] = payload.get("websockets", [])[:100]
                    result.data["request_count"] = len(rows)
                    result.data["synthetic"] = call.synthetic
                    ctx.state.casebook["api_runtime"] = {"requests": rows, "synthetic": call.synthetic,
                                                         "evidence_ref": call.evidence_ref}
                    # 当前浏览器证据将在外层注册表提交；先在工作副本登记，以供同一调用资产来源解析。
                    for ev in result.evidence:
                        ctx.state.add_evidence(ev)
                    assets = [{"target": row["url"], "method": row.get("method", "GET"), "kind": "runtime_api",
                               "parameters": {"query": row.get("query", {}), "body_sample": row.get("body", "")},
                               "evidence_refs": [call.evidence_ref]} for row in rows
                              if isinstance(row, dict) and str(row.get("url", "")).startswith(("http://", "https://"))]
                    if assets and not call.synthetic:
                        for offset in range(0, len(assets), 100):
                            insert_assets(ctx, assets[offset:offset + 100])
            return result
        except BrowserError as exc:
            return _browser_error(exc, session_id=session_id)


TOOLS = [ApiReconStatic(), ApiReconRuntime(), CaseTool("api_route_tree", "将前端路由候选按路径生成树；不作鉴权结论",
    lambda ctx, routes: ToolResult(summary="已生成路由树", data={"tree": route_tree(routes)}),
    {"routes": P("list", "静态或运行时收集的路由字符串")})]
