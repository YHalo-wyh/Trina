"""CTFY-0001 v3 的任务、提交与结果文件；真实判定与模型声明分开。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .tools.http import HttpClient


@dataclass
class CTFYTask:
    root: Path
    task: dict
    answers: dict = field(default_factory=dict)
    solved: list = field(default_factory=list)
    fully_solved: bool = False
    verdicts: list = field(default_factory=list)
    _http_session: Any = field(default=None, init=False, repr=False)
    _auto_attempted: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)

    @classmethod
    def load(cls, root: Path) -> CTFYTask:
        task = json.loads((root / "task.json").read_text(encoding="utf-8"))
        if not isinstance(task, dict) or task.get("schema_version") != 3 or task.get("run_mode") not in {"instance", "qa", "defence", "awd", "engagement"}:
            raise ValueError("需要有效的 CTFY-0001 v3 task.json")
        return cls(root.resolve(), task)

    def request(self, ctx, method: str, path: str, body: dict | None = None):
        base = os.environ.get("CTFY_PLATFORM_URL", "").rstrip("/")
        token = os.environ.get("CTFY_TOKEN", "")
        expected = str(self.task.get("platform_url", "")).rstrip("/")
        if not base or base != expected or not token:
            raise ValueError("CTFY 平台地址与任务合同不符或缺少运行令牌")
        # 所有方法/路径固定在代码内；绝不接受模型传入 URL 或跨题 ID。
        if self._http_session is None:
            import requests
            self._http_session = requests.Session()
        http = HttpClient(ctx.guard, retries=0, timeout=15, verify_tls=True,
                          headers={"Connection": "keep-alive"}, actor="ctfy-control",
                          session=self._http_session)
        reply = http.request(method, base + path, json_body=body,
            headers={"Authorization": "Bearer " + token}, control_plane=True,
            payload_label="ctfy:" + path, allow_redirects=False)
        if reply.error or not 200 <= reply.status < 300:
            raise ValueError(f"CTFY 请求失败 HTTP {reply.status}: {reply.error or reply.body[:300]}")
        data = json.loads(reply.body)
        if not isinstance(data, dict):
            raise ValueError("CTFY 响应不是对象")
        ctx.audit.append("ctfy.response", method=method, path=path, status=reply.status, response=data)
        return data, reply.as_evidence()

    def close(self) -> None:
        if self._http_session is not None:
            self._http_session.close()
            self._http_session = None

    def environment_contract(self) -> dict[str, Any]:
        """把托管容器的确定路径一次性交给模型，避免猜目录和全盘搜索。"""
        files: list[str] = []
        ignored = {"runs", "logs", ".git", "__pycache__"}
        for path in sorted(self.root.rglob("*")):
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                continue
            if not relative.parts or relative.parts[0] in ignored or not path.is_file():
                continue
            if path.name == "result.json":
                continue
            files.append(str(path.resolve()))
            if len(files) >= 80:
                break
        return {
            "working_directory": str(self.root),
            "task_file": str((self.root / "task.json").resolve()),
            "known_files": files,
            "terminal_cwd": {"project": str(self.root), "run": "Trina 本次运行工件目录"},
            "search_policy": "先用 known_files 和 task.json 中的路径；禁止 find /，只搜索具体目录",
            "tcp_policy": "一次性 nc 请求应发送 stdin，并使用 -q 1 -w 2；运行器会收紧更长等待",
        }

    def briefing(self) -> str:
        payload = {"ctfy_task": self.task, "environment": self.environment_contract()}
        return json.dumps(payload, ensure_ascii=False)

    def maybe_auto_submit(self, ctx, tool_name: str, result: Any) -> str:
        """唯一待答题且只出现一个非占位 Flag 时，经注册表立即提交一次。"""
        if tool_name == "ctfy_task":
            if getattr(result, "ok", False) and self.fully_solved:
                ctx.extra["_turn_completion"] = "平台已确认答案正确，任务完成。"
                return "平台权威判定已确认全部问题完成"
            return ""
        if not getattr(result, "ok", False) or ctx.tools is None:
            return ""
        pending = [
            str(row.get("id") or "")
            for row in self.task.get("questions", [])
            if isinstance(row, dict) and row.get("id") and row.get("id") not in self.solved
        ]
        if len(pending) != 1:
            return ""
        from .bench import submission_candidates

        candidates = submission_candidates(
            ctx.state,
            extra_texts=[result.to_observation(max_chars=20_000)],
            limit=3,
        )
        if len(candidates) != 1:
            return ""
        question_id, answer = pending[0], candidates[0]
        key = (question_id, answer)
        if key in self._auto_attempted:
            return ""
        self._auto_attempted.add(key)
        ctx.audit.append("ctfy.auto_submit", decision="attempt", tool=tool_name,
                         question_id=question_id, candidate=answer)
        submitted = ctx.tools.execute("ctfy_task", {
            "action": "submit", "question_id": question_id, "answer": answer,
        }, ctx)
        data = submitted.data if isinstance(submitted.data, dict) else {}
        correct = submitted.ok and data.get("correct") is True
        ctx.audit.append("ctfy.auto_submit.result", decision="allow" if correct else "error",
                         question_id=question_id, correct=correct,
                         error=(submitted.error or "")[:300])
        if correct and self.fully_solved:
            ctx.extra["_turn_completion"] = f"发现唯一候选 {answer}，平台已确认正确，任务完成。"
        if correct:
            return f"唯一候选 {answer} 已自动提交，平台判定正确"
        return f"唯一候选 {answer} 已自动提交，但平台未判定正确；不会原样重复提交"

    def refresh(self, ctx):
        challenge = str(self.task["challenge"]["id"])
        data, evidence = self.request(ctx, "GET", "/api/v1/challenges/" + quote(challenge, safe=""))
        if isinstance(data.get("questions"), list):
            self.task["questions"] = data["questions"]
        return data, evidence

    def submit(self, ctx, question_id: str, answer: str | list[str]):
        if question_id not in {row["id"] for row in self.task.get("questions", [])}:
            raise ValueError("问题不在本次已解锁题目中；先读取或刷新任务")
        if isinstance(answer, list):
            if not answer or len(answer) > 64 or any(not isinstance(item, str) or not item or len(item) > 8192 for item in answer):
                raise ValueError("多答案必须是 1—64 个非空有界字符串")
        elif not isinstance(answer, str) or not answer or len(answer) > 8192:
            raise ValueError("答案必须是非空有界字符串或字符串列表")
        mode = self.task["run_mode"]
        if mode == "qa":
            path = "/api/v1/qa/submissions"
            body = {"challenge_id": self.task["challenge"]["id"], "competition_id": self.task.get("competition_id", ""),
                    "question_id": question_id, "answer": answer}
        elif mode == "instance":
            path = "/api/v1/submissions"
            body = {"instance_id": self.task["target_instance"], "question_id": question_id, "answer": answer}
        else:
            raise ValueError("当前提交工具只支持 instance/qa，其他模式需专用权威适配器")
        data, evidence = self.request(ctx, "POST", path, body)
        if type(data.get("correct")) is not bool or data.get("question_id") != question_id:
            raise ValueError("提交响应缺少匹配问题的布尔 correct 判定；保持未验证")
        self.verdicts.append({"question_id": question_id, "submission_id": data.get("id"), "correct": data["correct"]})
        if data["correct"] and question_id not in self.solved:
            self.solved.append(question_id)
        self.fully_solved = data.get("challenge_fully_solved") is True
        if data["correct"] and self.task.get("gated_questions"):
            refreshed, extra = self.refresh(ctx)
            data["unlocked_questions"] = refreshed.get("questions", [])
        return data, evidence

    def write_result(self, *, stop_reason: str, turns: int = 0, tool_calls: int = 0, usage=None) -> Path:
        data = {"schema_version": 3, "answers": self.answers, "solved": self.solved,
                "fully_solved": self.fully_solved, "stop_reason": stop_reason, "turns": turns, "tool_calls_made": tool_calls}
        if usage is not None:
            if usage.input_reported:
                data["input_tokens"] = usage.input_tokens
            if usage.output_reported:
                data["output_tokens"] = usage.output_tokens
        # 不沿用 task.result.path 的任意位置；标准固定在工作目录。
        fd, name = tempfile.mkstemp(prefix=".ctfy-result-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            Path(name).replace(self.root / "result.json")
        finally:
            Path(name).unlink(missing_ok=True)
        return self.root / "result.json"


def run_ctfy(root: Path) -> int:
    """托管运行入口，instance/qa；不读个人供应商配置或使用个人 Key。"""
    from .brain.chat import ChatAgent
    from .brain.binary_prompt import CTFY_ROLE
    from .config import TrinaConfig
    from .tools import CTFY_TOOL_MODULES, build_registry
    task = CTFYTask.load(root)
    os.chdir(root)
    if os.environ.get("CTFY_EVAL_DRY_RUN") == "1" or task.task.get("dry_run"):
        task.write_result(stop_reason="dry_run")
        return 0
    if task.task["run_mode"] not in {"instance", "qa"}:
        task.write_result(stop_reason="unsupported_run_mode")
        raise ValueError("当前 CTFY 入口支持 instance/qa；AWD/防守/engagement 未完成")
    if not os.environ.get("LLM_API_KEY") or not os.environ.get("LLM_MODEL") or os.environ.get("LLM_FLAVOR") not in {"openai", "anthropic"}:
        task.write_result(stop_reason="missing_model_configuration")
        raise ValueError("CTFY 缺少 LLM_API_KEY、LLM_MODEL 或有效 LLM_FLAVOR，不回退个人连接")
    base = os.environ.get("CTFY_PLATFORM_URL", "")
    flavor = os.environ["LLM_FLAVOR"]
    cfg = TrinaConfig().with_overrides(run__workspace=str(root / "runs"), audit__dir=str(root / "logs"),
        llm__api_key_env="LLM_API_KEY", llm__model=os.environ["LLM_MODEL"],
        llm__api="anthropic" if flavor == "anthropic" else "chat",
        llm__base_url=os.environ.get("LLM_BASE_URL") or ("https://api.anthropic.com" if flavor == "anthropic" else "https://api.openai.com/v1"),
        llm__role_profile=CTFY_ROLE, scope__control_plane_hosts=[urlsplit(base).hostname or ""])
    requested_effort = os.environ.get("CTFY_REASONING_EFFORT", "low").strip().lower()
    from .llm.reasoning import reasoning_support
    if requested_effort in reasoning_support(cfg.llm).levels:
        cfg.llm.reasoning_effort = requested_effort
    maximum = int(os.environ.get("CTFY_EVAL_MAX_TURNS") or task.task.get("max_turns") or 40)
    if maximum <= 0:
        raise ValueError("CTFY 模型调用预算必须为正数")
    agent = ChatAgent(cfg, registry=build_registry(CTFY_TOOL_MODULES), logger=print)
    agent.ctx.extra["ctfy_task"] = task
    agent.ctx.extra["ctfy_mode"] = True
    agent.ctx.extra["ctfy_fast_nc"] = True
    agent.ctx.extra["disable_title_generation"] = True
    agent.ctx.extra["tool_result_hook"] = task.maybe_auto_submit
    reason = "turn_budget"
    try:
        while agent.usage.calls < maximum:
            before = agent.usage.calls
            agent.MAX_TOOL_STEPS = min(12, maximum - before)
            briefing = task.briefing() if not before else "继续检查当前合同剩余问题，先验证候选再结束。"
            reply = agent.ask(briefing)
            print(reply)
            task.write_result(stop_reason="running", turns=agent.usage.calls, tool_calls=agent.state.counters.get("tool_calls", 0), usage=agent.usage)
            if task.fully_solved:
                reason = "verified_complete"
                break
            if agent.usage.calls == before:
                reason = "no_model_progress"
                break
        return 0
    finally:
        agent.close()
        task.close()
        task.write_result(stop_reason=reason, turns=agent.usage.calls, tool_calls=agent.state.counters.get("tool_calls", 0), usage=agent.usage)
