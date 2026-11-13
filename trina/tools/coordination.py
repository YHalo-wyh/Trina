"""Worker 按需读取共享待办、事实索引以及其他 Worker 的执行经验。"""
from __future__ import annotations

from .registry import ParamSpec, Tool, ToolResult, ToolSpec


def _runtime(ctx):
    return ctx.extra.get("coordination_runtime")


class SharedContext(Tool):
    spec = ToolSpec(
        name="shared_context",
        summary="查看当前 Swarm 的共享 Todolist、Intent 状态和已验证事实计数；非 Swarm 会话不可用",
        category="coordination",
        action_level="detect",
        params={},
    )

    def run(self, ctx):
        runtime = _runtime(ctx)
        if runtime is None:
            return ToolResult(ok=False, error="当前不是 Swarm Worker，没有共享协作上下文")
        data = runtime.overview()
        return ToolResult(summary=f"Planner {data['planner_rounds']} 轮，事实 {sum(data['fact_counts'].values())} 条", data=data)


class SharedTraceSearch(Tool):
    spec = ToolSpec(
        name="shared_trace_search",
        summary="检索本次 Swarm 其他 Worker 的观察和失败摘要，再用 shared_trace_get 读取必要步骤",
        category="coordination",
        action_level="detect",
        params={
            "query": ParamSpec("str", "检索词，可包含工具、错误、资产或技术名称"),
            "limit": ParamSpec("int", "最大命中数", required=False, default=8),
        },
    )

    def run(self, ctx, query, limit=8):
        runtime = _runtime(ctx)
        if runtime is None:
            return ToolResult(ok=False, error="当前不是 Swarm Worker，没有共享执行留痕")
        rows = runtime.search_traces(query, exclude_intent=str(ctx.extra.get("intent_id") or ""), limit=limit)
        return ToolResult(summary=f"命中 {len(rows)} 条其他 Worker 记录", data={"matches": rows})


class SharedTraceGet(Tool):
    spec = ToolSpec(
        name="shared_trace_get",
        summary="按 Intent 和可选步骤号读取其他 Worker 的完整执行记录，用于复用有效观察或避开失败路径",
        category="coordination",
        action_level="detect",
        params={
            "intent_id": ParamSpec("str", "来源 Intent ID"),
            "step_ids": ParamSpec("list", "可选步骤号列表", required=False, default=[]),
            "limit": ParamSpec("int", "最大返回数", required=False, default=20),
        },
    )

    def run(self, ctx, intent_id, step_ids=None, limit=20):
        runtime = _runtime(ctx)
        if runtime is None:
            return ToolResult(ok=False, error="当前不是 Swarm Worker，没有共享执行留痕")
        if intent_id == str(ctx.extra.get("intent_id") or ""):
            return ToolResult(ok=False, error="请直接查看当前 Worker 历史；该工具只用于其他 Intent")
        rows = runtime.get_trace(intent_id, step_ids=step_ids or (), limit=limit)
        return ToolResult(summary=f"读取 Intent {intent_id} 的 {len(rows)} 条记录", data={"trace": rows})


TOOLS = [SharedContext(), SharedTraceSearch(), SharedTraceGet()]
