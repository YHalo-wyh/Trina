"""后台终端工具：启动、查询和取消均经 ToolRegistry 与受控执行入口。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .external import run_external
from .registry import ParamSpec, Tool, ToolResult, ToolSpec


class TerminalStart(Tool):
    spec = ToolSpec(
        name="terminal_start", summary="在任务目录启动后台终端程序，返回 job_id；启动成功不代表程序完成",
        category="utility", action_level="exploit",
        params={"argv": ParamSpec("list", "程序及参数数组，不经过 shell"),
                "timeout": ParamSpec("float", "后台最长秒数", required=False, default=120),
                "cwd": ParamSpec("str", "执行目录：run 为本次运行目录，project 为当前项目目录", required=False, default="run", enum=["run", "project"]),
                "target": ParamSpec("str", "目标；本地分析可为空", required=False, default="")},
    )

    def run(self, ctx, argv, timeout=120, cwd="run", target=""):
        if not argv or not all(isinstance(arg, str) and arg for arg in argv) or not 0 < timeout <= 3600:
            return ToolResult(ok=False, error="argv 必须为非空字符串数组，timeout 必须在 (0,3600] 内")
        directory = Path.cwd() if cwd == "project" else ctx.workspace
        if not directory.is_dir():
            return ToolResult(ok=False, error="后台执行目录不存在或不是目录")
        call = run_external(argv, ctx, tool=self.spec.name, target=target, required_level=ctx.effective_level,
                            timeout=timeout, cwd=str(directory), background=True)
        return ToolResult(ok=call.ok, summary=f"后台作业已启动：{call.job_id}（运行中）" if call.ok else call.error,
                          error=call.error, data={**call.to_dict(), "cwd": str(directory)}, command=call.command)


class TerminalStatus(Tool):
    spec = ToolSpec(
        name="terminal_status", summary="读取后台作业真实状态、退出码与输出尾部；可等待指定作业或当前会话全部作业，结束后归档证据",
        category="utility", action_level="detect",
        params={"job_id": ParamSpec("str", "留空列出近期作业", required=False, default=""),
                "wait_seconds": ParamSpec("float", "等待指定作业，最多 10 秒", required=False, default=0)},
    )

    def run(self, ctx, job_id="", wait_seconds=0):
        jobs = ctx.extra.get("background_jobs")
        if not 0 <= wait_seconds <= 10:
            return ToolResult(ok=False, error="wait_seconds 必须在 [0,10] 内")
        if wait_seconds:
            if job_id:
                jobs.wait(job_id, wait_seconds)
            else:
                jobs.wait_all(wait_seconds)
        # 指定作业保持轻量视图。等待当前会话全部作业时返回完整终态集合，
        # 避免第 9 个后台任务被“近期 8 项”截掉，导致证据无法一次性回收。
        rows = jobs.snapshot(job_id, tail=1600, include_all=not job_id and bool(wait_seconds))
        evidence = []
        data = {"jobs": rows, "evidence_ids": []}
        for row in rows:
            # monitor 先更新状态再发布 job.finished，短窗口内 status 已是终态
            # 而 done 事件尚未置位，终态状态仍然可以安全归档。
            if row["status"] == "running" and not row["finished"]:
                continue
            evidence_id = "bg-" + row["id"]
            data["evidence_ids"].append(evidence_id)
            if job_id:
                data["evidence_id"] = evidence_id
            if any(item.get("id") == evidence_id for item in ctx.state.evidence):
                continue
            # 仅由前台注册表提交状态；后台线程不向事实库写入。
            record = jobs.snapshot(row["id"], tail=16_000)[0]
            transcript = json.dumps({**record, "output_is_tail": True}, ensure_ascii=False)
            artifact = ctx.save_artifact(evidence_id + ".json", transcript)
            evidence.append({"id": evidence_id, "kind": "command", "path": str(artifact),
                             "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                             "job_id": row["id"], "exit_code": record["exit_code"],
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
