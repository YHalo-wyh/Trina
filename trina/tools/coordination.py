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


class WorkerTasks(Tool):
    """Commander 的异步委派工具；没有宿主会话时明确不可用。"""
    spec = ToolSpec(
        name="worker_tasks", category="delegation", action_level="exploit",
        summary="向独立 Worker 委派分支；查看结果、最多等待10秒、发送补充或取消指定任务。启动不等于完成",
        params={
            "action": ParamSpec("str", "操作", enum=["spawn", "list", "result", "wait", "message", "cancel"]),
            "task_id": ParamSpec("str", "唯一任务名称", required=False, default=""),
            "target": ParamSpec("str", "目标或附件路径", required=False, default=""),
            "goal": ParamSpec("str", "完整任务：文件范围、验收与停止条件", required=False, default=""),
            "depends_on": ParamSpec("list", "依赖的任务名称", required=False, default=[]),
            "model": ParamSpec("str", "空则继承子模型设置", required=False, default=""),
            "reasoning_effort": ParamSpec("str", "空则继承；实际支持档位由模型校验", required=False, default=""),
            "profile": ParamSpec("str", "已保存供应商名称", required=False, default=""),
            "text": ParamSpec("str", "给指定任务的补充", required=False, default=""),
            "wait_seconds": ParamSpec("float", "有界等待秒数，最多10", required=False, default=1),
        },
    )

    def required_level(self, params):
        return "detect" if params.get("action") in {"list", "result", "wait"} else "exploit"

    def run(self, ctx, action, task_id="", target="", goal="", depends_on=None, model="", reasoning_effort="", profile="", text="", wait_seconds=1):
        manager = ctx.extra.get("subworkers")
        if manager is None:
            return ToolResult(ok=False, error="此入口没有 Commander 委派宿主；不可递归创建子 Worker")
        try:
            if action == "spawn":
                from ..swarm import TaskSpec
                from ..llm import make_backend
                if not task_id or not target or not goal.strip() or len(goal) > 12000:
                    raise ValueError("spawn 必须提供 task_id、target 和不超过12000字的完整 goal")
                budget = ctx.config.subworker
                task = TaskSpec(task_id, target, goal, depends_on=tuple(depends_on or ()),
                                max_steps=budget.max_steps, max_seconds=budget.max_seconds,
                                model=model, reasoning_effort=reasoning_effort, profile=profile)
                accepted = manager.submit(ctx.config, task, backend_factory=ctx.extra.get("subworker_backend_factory") or make_backend)
                return ToolResult(summary="已接收委派，尚未完成" if accepted else "等价任务已存在，未重复执行",
                                  data={"accepted": accepted, "task_id": task_id if accepted else next(
                                      (t.id for t in manager.runtime.tasks if t.target == target and t.goal.strip().casefold() == goal.strip().casefold()), task_id),
                                        "run_id": manager.runtime.run_id})
            if action in {"message", "cancel"}:
                manager.task_control(task_id, action, text)
                return ToolResult(summary="补充已入队，下个决策点读取" if action == "message" else "已请求取消，等待真实终态")
            if action == "wait":
                if not 0 <= wait_seconds <= 10:
                    raise ValueError("wait_seconds 必须在0..10之间")
                import time
                from ..scope import ensure_execution_active
                deadline = time.monotonic() + wait_seconds
                while manager.running and time.monotonic() < deadline:
                    ensure_execution_active()
                    item = manager.task_result(task_id) if task_id else None
                    if item and item["status"] not in {"pending", "running"}:
                        break
                    manager.thread.join(timeout=min(.1, max(0, deadline - time.monotonic())))
            if action in {"result", "wait"} and task_id:
                item = manager.task_result(task_id)
                if item is None:
                    raise ValueError("未找到任务；先用 list 查看实际名称")
                return ToolResult(summary=f"{task_id} · {item['status']}", data=item)
            return ToolResult(summary=manager.badge or "暂无任务", data={"tasks": manager.rows(), "running": manager.running})
        except (ValueError, RuntimeError, TimeoutError) as exc:
            return ToolResult(ok=False, error=str(exc))


TOOLS = [SharedContext(), SharedTraceSearch(), SharedTraceGet(), WorkerTasks()]
