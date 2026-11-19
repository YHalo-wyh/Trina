"""后台终端工具：启动、查询和取消均经 ToolRegistry 与受控执行入口。"""
from __future__ import annotations

import hashlib
import json

from .external import run_external
from .registry import ParamSpec, Tool, ToolResult, ToolSpec


class TerminalStart(Tool):
    spec = ToolSpec(
        name="terminal_start", summary="在当前任务工作目录启动后台终端程序，返回 job_id；启动成功不代表程序完成",
        category="utility", action_level="exploit",
        params={"argv": ParamSpec("list", "程序及参数数组，不经过 shell"),
                "timeout": ParamSpec("float", "后台最长秒数", required=False, default=120),
                "target": ParamSpec("str", "目标；本地分析可为空", required=False, default="")},
    )

    def run(self, ctx, argv, timeout=120, target=""):
        if not argv or not all(isinstance(arg, str) and arg for arg in argv) or not 0 < timeout <= 3600:
            return ToolResult(ok=False, error="argv 必须为非空字符串数组，timeout 必须在 (0,3600] 内")
        call = run_external(argv, ctx, tool=self.spec.name, target=target, required_level=ctx.effective_level,
                            timeout=timeout, background=True)
        return ToolResult(ok=call.ok, summary=f"后台作业已启动：{call.job_id}（运行中）" if call.ok else call.error,
                          error=call.error, data=call.to_dict(), command=call.command)


class TerminalStatus(Tool):
    spec = ToolSpec(
        name="terminal_status", summary="读取后台作业真实状态、退出码与输出尾部；指定已结束作业时归档 evidence_id，可短暂等待",
        category="utility", action_level="detect",
        params={"job_id": ParamSpec("str", "留空列出近期作业", required=False, default=""),
                "wait_seconds": ParamSpec("float", "等待指定作业，最多 10 秒", required=False, default=0)},
    )

    def run(self, ctx, job_id="", wait_seconds=0):
        jobs = ctx.extra.get("background_jobs")
        if not 0 <= wait_seconds <= 10:
            return ToolResult(ok=False, error="wait_seconds 必须在 [0,10] 内")
        if job_id and wait_seconds:
            jobs.wait(job_id, wait_seconds)
        rows = jobs.snapshot(job_id, tail=1600)
        evidence = []
        data = {"jobs": rows}
        if job_id and rows and rows[0]["finished"]:
            evidence_id = "bg-" + job_id
            data["evidence_id"] = evidence_id
            if not any(item.get("id") == evidence_id for item in ctx.state.evidence):
                # 仅由前台注册表提交状态；后台线程不向事实库写入。
                record = jobs.snapshot(job_id, tail=16_000)[0]
                transcript = json.dumps({**record, "output_is_tail": True}, ensure_ascii=False)
                artifact = ctx.save_artifact(evidence_id + ".json", transcript)
                evidence.append({"id": evidence_id, "kind": "command", "path": str(artifact),
                                 "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                                 "job_id": job_id, "exit_code": record["exit_code"],
                                 "successful": record["status"] == "completed" and record["exit_code"] == 0,
                                 "output_is_tail": True})
        failed = any(row["status"] in {"failed", "timed_out", "cancelled"} for row in rows)
        ctx.audit.append("external.background.status", jobs=[{k: v for k, v in row.items() if not k.endswith("_tail")} for row in rows])
        return ToolResult(ok=not failed, summary="；".join(f"{row['id']} {row['status']} code={row['exit_code']}" for row in rows) or "没有后台作业",
                          error="后台作业失败、超时或取消" if failed else "", data=data, evidence=evidence)


class TerminalStop(Tool):
    spec = ToolSpec(name="terminal_stop", summary="取消当前会话的指定后台作业", category="utility", action_level="detect",
                    params={"job_id": ParamSpec("str", "当前会话 job_id")})

    def run(self, ctx, job_id):
        jobs = ctx.extra.get("background_jobs")
        jobs.stop(job_id)
        ctx.audit.append("external.background.stop", job_id=job_id)
        return ToolResult(summary="后台作业已停止", data={"jobs": jobs.snapshot(job_id)})


TOOLS = [TerminalStart(), TerminalStatus(), TerminalStop()]
