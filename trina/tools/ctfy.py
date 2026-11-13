"""CTFY 当前题目的只读合同与权威提交，不暴露通用平台请求工具。"""
from __future__ import annotations

from .registry import ParamSpec, Tool, ToolResult, ToolSpec


class CTFYTaskTool(Tool):
    spec = ToolSpec(name="ctfy_task", category="benchmark", action_level="verify",
        summary="读取 CTFY 当前任务、刷新解锁问题，或提交答案并取得权威 correct 判定",
        params={"action": ParamSpec("str", enum=["read", "refresh", "submit"]),
                "question_id": ParamSpec("str", required=False, default=""),
                "answer": ParamSpec("str", required=False, default=""),
                "answers": ParamSpec("list", required=False, default=[])})

    def run(self, ctx, action: str, question_id: str = "", answer: str = "", answers: list | None = None) -> ToolResult:
        task = ctx.extra.get("ctfy_task")
        if task is None:
            return ToolResult(ok=False, summary="CTFY_NOT_BOUND", error="请通过 trina ctfy 启动平台任务")
        if action == "read":
            return ToolResult(summary="当前 CTFY v3 合同", data=task.task)
        try:
            if action == "refresh":
                data, evidence = task.refresh(ctx)
            else:
                if answer and answers:
                    raise ValueError("answer 与 answers 只能选择一个")
                data, evidence = task.submit(ctx, question_id, answers or answer)
            return ToolResult(summary="CTFY 权威响应", data=data, evidence=[evidence])
        except (ValueError, KeyError) as exc:
            return ToolResult(ok=False, summary="CTFY 请求或判定失败", error=str(exc))


TOOLS = [CTFYTaskTool()]
