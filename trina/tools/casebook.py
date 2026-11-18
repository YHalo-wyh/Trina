"""通用成果登记、资产覆盖、流量关联与有版本的复测；不把模型断言当验证器。"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable

from ..brain.goal import GoalRequirements
from ..state import SEVERITIES
from .registry import ParamSpec as P, Tool, ToolResult, ToolSpec


def _bucket(ctx: Any, key: str, factory: Callable = list) -> Any:
    return ctx.state.casebook.setdefault(key, factory())


def _finding(ctx: Any, finding_id: str):
    found = next((f for f in ctx.state.findings if f.id == finding_id), None)
    if found is None:
        raise ValueError("未找到 finding_id；先查询当前状态")
    return found


def _version(finding: Any) -> str:
    return hashlib.sha256(json.dumps(finding.to_dict(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def resolve_evidence(ctx: Any, refs: list, *, after: float = 0, allow_synthetic: bool = False) -> list[dict[str, Any]]:
    """只接受本任务已归档的证据；拒绝模型自造 ID 和失败的请求。"""
    if not refs or not all(isinstance(ref, str) for ref in refs):
        raise ValueError("需要非空 evidence_refs，引用本任务真实证据编号")
    found = []
    for ref in dict.fromkeys(refs):
        traffic = ctx.extra["traffic"].get(ref)
        if traffic:
            if traffic["error"] or not traffic["status"]:
                raise ValueError("失败流量不能作为成功证明")
            item = {**ctx.extra["traffic"].summary(traffic), "kind": "http", "traffic_id": ref,
                    "target": traffic["url"], "request": {"method": traffic["method"], "url": traffic["url"],
                        "body": traffic["request"]["body"][:600]},
                    "response": {"status": traffic["status"], "length": len(traffic["response"]["body"]),
                        "body_excerpt": traffic["response"]["body"][:600]}}
        else:
            item = next((e for e in ctx.state.evidence if ref in (e.get("id"), e.get("traffic_id"), e.get("evidence_ref"))), None)
            if item is None or item.get("successful") is False:
                raise ValueError(f"证据不存在或执行失败：{ref}")
            item = dict(item)
        if item.get("synthetic") and not allow_synthetic:
            raise ValueError("mock证据只能用于前端侦察，不能证明真实后端行为")
        if item.get("path") and item.get("sha256"):
            from pathlib import Path
            path = Path(item["path"])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("证据工件已缺失或被修改；请重新采集")
        if after and float(item.get("ts", 0)) <= after:
            raise ValueError("复测必须引用取得上下文后新采集的证据")
        found.append(item)
    return found


def _record(ctx: Any, text: str, evidence_refs: list, kind: str = "observation", **extra: Any) -> ToolResult:
    evidence = resolve_evidence(ctx, evidence_refs, allow_synthetic=kind == "inference")
    if not text.strip() or len(text) > 12000:
        raise ValueError("text 需要在1..12000字符之间")
    item = {"id": "note-" + uuid.uuid4().hex[:12], "text": text, "kind": kind,
            "evidence_refs": evidence_refs, "ts": time.time(), **extra}
    _bucket(ctx, "intelligence" if kind == "intelligence" else "facts").append(item)
    ctx.state.note("intelligence" if kind == "intelligence" else "observation", text[:200], record_id=item["id"])
    return ToolResult(summary="已登记有来源的观察；不自动升级为已验证漏洞", data=item,
                      facts={"notes": [text[:200]]})


def report_finding(ctx: Any, title: str, vuln_class: str, target: str, evidence_refs: list,
                   severity: str = "medium", parameter: str = "", description: str = "", payload: str = "",
                   impact: str = "", remediation: str = "", status: str = "detected",
                   verification_refs: list | None = None, verification: str = "") -> ToolResult:
    if not title.strip() or not vuln_class.strip():
        raise ValueError("title/vuln_class 不能为空")
    evidence = resolve_evidence(ctx, evidence_refs)
    if status == "verified":
        if not verification.strip() or not verification_refs or set(verification_refs) & set(evidence_refs):
            raise ValueError("verified 需要独立 verification_refs 和验证说明；重复引用同一证据不能独立确认")
        evidence += resolve_evidence(ctx, verification_refs)
    item = {"title": title, "vuln_class": vuln_class, "target": target,
            "url": target if target.startswith(("https://", "http://")) else "", "severity": severity,
            "parameter": parameter, "description": description, "payload": payload, "impact": impact,
            "remediation": remediation, "status": status, "tool": "report_finding", "evidence": evidence,
            "confidence": "high" if status == "verified" else "medium"}
    finding = ctx.state.add_finding(item)
    if verification:
        _bucket(ctx, "verification", dict)[finding.id] = {"explanation": verification, "refs": verification_refs}
    return ToolResult(summary=f"已登记 {finding.id}（{finding.status}）", data=finding.to_dict(),
                      facts={"notes": [f"登记发现 {finding.id}: {title}"]})


def insert_assets(ctx: Any, assets: list) -> ToolResult:
    if not 1 <= len(assets) <= 100 or not all(isinstance(a, dict) for a in assets):
        raise ValueError("assets 需要1..100个对象")
    prepared = []
    for raw in assets:
        target = str(raw.get("target", "")).strip()
        if not target:
            raise ValueError("资产需要 target")
        from ..scope import ScopeViolation
        decision = ctx.guard.check_target(target, action="insert_assets")
        if decision.denied:
            raise ScopeViolation(decision)
        refs = raw.get("evidence_refs", [])
        resolve_evidence(ctx, refs)
        prepared.append({"target": target, "kind": str(raw.get("kind", "url")),
                         "method": str(raw.get("method", "GET")).upper(),
                         "parameters": raw.get("parameters", {}), "evidence_refs": refs})
    book = _bucket(ctx, "assets", dict)
    for item in prepared:
        key = hashlib.sha256((item["method"] + " " + item["target"]).encode()).hexdigest()[:16]
        prior = book.get(key, {})
        book[key] = {**prior, **item, "id": key, "status": prior.get("status", "discovered")}
    return ToolResult(summary=f"已合并 {len(prepared)} 条资产", data={"assets": list(book.values())[-100:]},
                      facts={"paths": [{"url": a["target"]} for a in prepared if a["target"].startswith("http")]})


def list_assets(ctx: Any, query: str = "", limit: int = 50, untested: bool = False) -> ToolResult:
    items = [a for a in _bucket(ctx, "assets", dict).values()
             if query.lower() in a["target"].lower() and (not untested or a["status"] != "tested")]
    return ToolResult(summary=f"匹配 {len(items)} 条资产", data={"assets": items[:max(1, min(limit, 100))], "total": len(items)})


def mark_asset_tested(ctx: Any, asset_id: str, evidence_refs: list, checks: str) -> ToolResult:
    evidence = resolve_evidence(ctx, evidence_refs)
    item = _bucket(ctx, "assets", dict).get(asset_id)
    if item is None or not checks.strip():
        raise ValueError("需要已登记的 asset_id 和实际执行检查说明")
    item.update(status="tested", checks=checks, test_evidence=evidence_refs)
    return ToolResult(summary="已记录资产检查覆盖；不表示资产没有漏洞", data=item)


def traffic_search(ctx: Any, query: str = "", method: str = "", status: int = 0, limit: int = 20, offset: int = 0) -> ToolResult:
    rows = ctx.extra["traffic"].search(query, method=method.upper(), status=status, limit=limit, offset=offset)
    return ToolResult(summary=f"命中 {len(rows)} 条 HTTP 记录", data={"traffic": rows, "offset": offset})


def traffic_get(ctx: Any, traffic_id: str) -> ToolResult:
    item = ctx.extra["traffic"].get(traffic_id)
    if item is None:
        raise ValueError("本任务不存在该 traffic_id")
    # 默认不向模型暴露认证头；正文使用显式的分页接口。
    scrub = lambda headers: {k: v for k, v in headers.items() if k.lower() not in {"cookie", "set-cookie", "authorization", "proxy-authorization", "x-api-key", "mcp-session-id"}}
    return ToolResult(summary="已读取流量元数据；正文用 traffic_blob 分页", data={
        **ctx.extra["traffic"].summary(item), "request_headers": scrub(item["request"]["headers"]),
        "response_headers": scrub(item["response"]["headers"]),
        "request_length": len(item["request"]["body"]), "response_length": len(item["response"]["body"]),
    })


def traffic_blob(ctx: Any, traffic_id: str, side: str = "response", offset: int = 0, limit: int = 8000) -> ToolResult:
    item = ctx.extra["traffic"].get(traffic_id)
    if item is None or offset < 0 or not 1 <= limit <= 64000:
        raise ValueError("需要真实 traffic_id、非负 offset 和1..64000的 limit")
    body = item[side]["body"]
    text = body[offset:offset + limit]
    return ToolResult(summary="已读取归档正文片段", data={"text": text, "offset": offset,
        "next_offset": offset + len(text), "total": len(body), "has_more": offset + len(text) < len(body),
        "capture_truncated": item["truncated"]})


def bind_finding_traffic(ctx: Any, finding_id: str, traffic_ids: list) -> ToolResult:
    finding = _finding(ctx, finding_id)
    for tid in traffic_ids:
        if not isinstance(tid, str) or ctx.extra["traffic"].get(tid) is None:
            raise ValueError("只能关联本任务已存在的流量")
    bucket = _bucket(ctx, "bindings", dict)
    bucket[finding.id] = list(dict.fromkeys(bucket.get(finding.id, []) + traffic_ids))
    return get_finding_traffic(ctx, finding_id)


def get_finding_traffic(ctx: Any, finding_id: str) -> ToolResult:
    finding = _finding(ctx, finding_id)
    ids = list(dict.fromkeys(_bucket(ctx, "bindings", dict).get(finding.id, []) +
                            [e["traffic_id"] for e in finding.evidence if e.get("traffic_id")]))
    return ToolResult(summary=f"关联 {len(ids)} 条流量", data={"traffic_ids": ids, "finding_id": finding.id})


def _goals(ctx: Any) -> list[tuple[str, str, GoalRequirements]]:
    req = GoalRequirements.parse(ctx.state.goal)
    goals = []
    for kind, values in (("classes", sorted(req.classes)), ("paths", req.paths), ("ports", req.ports), ("credentials", req.credentials)):
        for index, value in enumerate(values):
            sub = GoalRequirements(raw=req.raw)
            setattr(sub, kind, {value} if kind == "classes" else [value])
            label = f"凭据验证 #{index + 1}" if kind == "credentials" else f"{kind}: {value}"
            goals.append((f"{kind}-{index + 1}", label, sub))
    if req.explicit_flag:
        goals.append(("flag", "恢复任务要求的答案候选", GoalRequirements(raw=req.raw, explicit_flag=True)))
    return goals


def _goal_met(ctx: Any, goal_id: str, req: GoalRequirements) -> bool:
    if goal_id == "flag":
        return bool(ctx.state.flags)
    if goal_id.startswith("credentials-"):
        return all(any(c.verified and c.username == username and c.password == password for c in ctx.state.credentials)
                   for username, password in req.credentials)
    return req.satisfied(ctx.state)


def list_goals(ctx: Any) -> ToolResult:
    goals = [{"id": gid, "description": label, "satisfied": _goal_met(ctx, gid, req)} for gid, label, req in _goals(ctx)]
    return ToolResult(summary="目标条件来自当前任务；无法解析的目标需原始验收器核对", data={"goal": ctx.state.goal, "goals": goals, "evaluable": bool(goals)})


def prove_goal(ctx: Any, goal_id: str, evidence_refs: list) -> ToolResult:
    evidence = resolve_evidence(ctx, evidence_refs)
    matches = [(gid, req) for gid, _, req in _goals(ctx) if gid == goal_id]
    if not matches or not _goal_met(ctx, *matches[0]):
        raise ValueError("目标不存在或实际状态尚未满足条件；不能用文字覆盖验证器")
    _bucket(ctx, "goal_proofs", dict)[goal_id] = {"evidence_refs": evidence_refs, "ts": time.time()}
    return ToolResult(summary="当前事实满足该目标条件；答案候选仍需平台权威回执", data={"goal_id": goal_id, "evidence": evidence})


def get_retest_context(ctx: Any, finding_id: str, environment: str) -> ToolResult:
    finding = _finding(ctx, finding_id)
    if not environment.strip():
        raise ValueError("environment 需要说明目标版本、账号角色与测试条件")
    token = uuid.uuid4().hex
    item = {"token": token, "version": _version(finding), "issued": time.time(), "environment": environment}
    _bucket(ctx, "retest_contexts", dict)[finding_id] = item
    return ToolResult(summary="已生成复测上下文；先读完整证据，再执行新请求和正常对照", data={
        **item, "finding": finding.to_dict(), "traffic_ids": get_finding_traffic(ctx, finding_id).data["traffic_ids"],
        "previous_retests": _bucket(ctx, "retests", dict).get(finding_id, [])})


def record_retest(ctx: Any, finding_id: str, context_token: str, result: str, evidence_refs: list,
                  control_refs: list, environment: str, explanation: str, comparable: bool = False) -> ToolResult:
    finding = _finding(ctx, finding_id)
    snapshot = _bucket(ctx, "retest_contexts", dict).get(finding_id)
    if not snapshot or context_token != snapshot["token"] or snapshot["version"] != _version(finding):
        raise ValueError("复测上下文缺失或证据版本已变化；重新读取上下文后再测")
    if not explanation.strip() or not environment.strip():
        raise ValueError("需要实际环境与复测解释")
    evidence = resolve_evidence(ctx, evidence_refs, after=snapshot["issued"])
    if result != "inconclusive":
        if not comparable or environment != snapshot["environment"] or not control_refs or set(control_refs) & set(evidence_refs):
            raise ValueError("明确复测结论需要可比环境和独立正常对照；否则记 inconclusive")
        resolve_evidence(ctx, control_refs, after=snapshot["issued"])
        if finding.status not in {"verified", "exploited"} and result == "fixed":
            raise ValueError("原问题尚未确认，不能宣布修复")
    item = {"result": result, "evidence_refs": evidence_refs, "control_refs": control_refs,
            "environment": environment, "comparable": comparable, "explanation": explanation,
            "version": snapshot["version"], "ts": time.time(), "source": "agent_assessment"}
    _bucket(ctx, "retests", dict).setdefault(finding_id, []).append(item)
    del ctx.state.casebook["retest_contexts"][finding_id]
    return ToolResult(summary=f"已登记复测 {result}；保留原始发现及历史证据", data=item,
                      facts={"notes": [f"复测 {finding_id}: {result}"]})


class CaseTool(Tool):
    """小型本地台账操作共用统一注册表，参数与错误仍可审计。"""
    def __init__(self, name: str, summary: str, fn: Callable, params: dict[str, P], level: str = "detect"):
        self.fn = fn
        self.spec = ToolSpec(name=name, summary=summary, category="evidence", action_level=level, params=params)

    def run(self, ctx: Any, **params: Any) -> ToolResult:
        try:
            return self.fn(ctx, **params)
        except (ValueError, TypeError, KeyError) as exc:
            return ToolResult(ok=False, error=str(exc), category="invalid")


REFS = P("list", "本任务真实 evidence_id / traffic_id / evidence_ref，不接受自造来源")
FID = P("str", "当前状态中的漏洞编号")
TOOLS = [
    CaseTool("record_fact", "登记有证据来源的观察或推断；不会自动确认漏洞", _record, {
        "text": P("str", "观察内容"), "evidence_refs": REFS,
        "kind": P("str", "证据性质", required=False, default="observation", enum=["observation", "inference"])}),
    CaseTool("record_intelligence", "登记后续路线需要的情报及来源", lambda ctx, **p: _record(ctx, kind="intelligence", **p), {
        "text": P("str", "情报内容"), "evidence_refs": REFS}),
    CaseTool("report_finding", "将脚本或人工验证结果写入统一漏洞与报告；verified 需要独立复核证据", report_finding, {
        "title": P("str", "漏洞标题"), "vuln_class": P("str", "漏洞分类"), "target": P("str", "实际目标"), "evidence_refs": REFS,
        "severity": P("str", "严重性", required=False, default="medium", enum=list(SEVERITIES)),
        **{k: P("str", k, required=False, default="") for k in ("parameter", "description", "payload", "impact", "remediation", "verification")},
        "status": P("str", "确认状态", required=False, default="detected", enum=["detected", "verified"]),
        "verification_refs": P("list", "通过独立命令或不同路径取得的复核证据", required=False, default=[])}, "verify"),
    CaseTool("insert_assets", "合并有来源的资产、API、方法和参数", insert_assets, {"assets": P("list", "对象含 target/kind/method/parameters/evidence_refs")}),
    CaseTool("list_assets", "查询资产台账", list_assets, {"query": P("str", "URL关键词", required=False, default=""), "limit": P("int", "最多100", required=False, default=50)}),
    CaseTool("list_untested_assets", "查询尚无明确检查覆盖的资产", lambda ctx, **p: list_assets(ctx, untested=True, **p), {
        "query": P("str", "URL关键词", required=False, default=""), "limit": P("int", "最多100", required=False, default=50)}),
    CaseTool("mark_asset_tested", "记录实际检查与证据；访问过页面不等于测试过", mark_asset_tested, {
        "asset_id": P("str", "资产编号"), "evidence_refs": REFS, "checks": P("str", "实际完成的检查")}),
    CaseTool("traffic_search", "检索本任务原生 HTTP 流量，支持方法、状态和分页", traffic_search, {
        "query": P("str", "字面量关键词", required=False, default=""), "method": P("str", "HTTP方法", required=False, default=""),
        "status": P("int", "状态码，0不过滤", required=False, default=0), "limit": P("int", "最多100", required=False, default=20),
        "offset": P("int", "分页偏移", required=False, default=0)}),
    CaseTool("traffic_get", "按编号读取流量元数据与脱敏请求/响应头", traffic_get, {"traffic_id": P("str", "流量编号")}),
    CaseTool("traffic_blob", "按需分页读取归档请求或响应正文；返回捕获截断标记", traffic_blob, {
        "traffic_id": P("str", "流量编号"), "side": P("str", "方向", required=False, default="response", enum=["request", "response"]),
        "offset": P("int", "字符偏移", required=False, default=0), "limit": P("int", "最多64000字符", required=False, default=8000)}),
    CaseTool("bind_finding_traffic", "给漏洞关联真实流量编号，不改变确认状态", bind_finding_traffic, {"finding_id": FID, "traffic_ids": P("list", "本任务流量编号")}),
    CaseTool("get_finding_traffic", "查询漏洞关联的流量", get_finding_traffic, {"finding_id": FID}),
    CaseTool("list_goals", "查看当前任务可解析的完成条件及实际满足状态", list_goals, {}),
    CaseTool("prove_goal", "按实际状态验证一个任务条件；不能凭文字宣告成功", prove_goal, {"goal_id": P("str", "list_goals 的目标编号"), "evidence_refs": REFS}),
    CaseTool("get_finding_retest_context", "读取完整发现、证据版本与历史复测；签发一次性上下文", get_retest_context, {
        "finding_id": FID, "environment": P("str", "待比较的版本、账号角色与测试环境")}),
    CaseTool("record_finding_retest_result", "登记 reproduced/fixed/inconclusive；要求新证据、可比环境和正常对照", record_retest, {
        "finding_id": FID, "context_token": P("str", "读取复测上下文返回的token"),
        "result": P("str", "复测结论", enum=["reproduced", "fixed", "inconclusive"]), "evidence_refs": REFS,
        "control_refs": P("list", "新采集的独立正常对照证据"), "environment": P("str", "实际测试环境"),
        "explanation": P("str", "差异、观察和判断依据"), "comparable": P("bool", "环境是否可比", required=False, default=False)}, "verify"),
]
