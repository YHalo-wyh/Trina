"""真实本地进程经过 Agent 执行链，再读取模型请求末尾的状态快照。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trina.brain.chat import ChatAgent
from trina.config import TrinaConfig, LLMConfig
from trina.llm import LLMResponse
from trina.llm.replay_backend import ReplayBackend
from trina.tools import build_registry


def probe():
    with tempfile.TemporaryDirectory() as folder:
        config = TrinaConfig().with_overrides(run__workspace=str(Path(folder) / "runs"),
                                              audit__dir=str(Path(folder) / "logs"))
        backend = ReplayBackend(LLMConfig(backend="replay"))
        observed = []
        command = [sys.executable, "-u", "-c",
                   "import time; print('work started',flush=True); time.sleep(.8); print('done')"]

        def complete(messages, **kwargs):
            observed.append(json.loads(messages[-1]["content"].splitlines()[-1]))
            if len(observed) == 1:
                return LLMResponse(text=json.dumps({"action": "terminal_start", "params": {"argv": command}}))
            return LLMResponse(text="已读取当前作业状态")

        backend.complete_messages = complete
        chat = ChatAgent(config, registry=build_registry(["terminal"]), backend=backend)
        try:
            chat.ask("启动隔离测试程序")
            job_id = observed[-1]["background_jobs"][0]["id"]
            result = chat.registry.execute("terminal_status", {"job_id": job_id, "wait_seconds": 2}, chat.ctx)
            chat.ask("核对真实退出状态")
            return {"execution": "actual_local_process_with_replay_backend", "command": command,
                    "model_request_snapshots": observed, "status_tool_result": result.data,
                    "audit_chain_valid": chat.audit.verify()[0]}
        finally:
            chat.close()


if __name__ == "__main__":
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
