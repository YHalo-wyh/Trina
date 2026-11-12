"""受控回放对比：模型调用边界的可见输入量，不测供应商真实缓存命中。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from trina.brain.chat import ChatAgent
from trina.config import LLMConfig, TrinaConfig
from trina.conversation import encoded
from trina.hypotheses import Hypothesis
from trina.llm import LLMResponse
from trina.llm.replay_backend import ReplayBackend
from trina.tools.registry import ToolRegistry

BASELINE = "96207236936ca392b76c6233491269de73670ad1"


def visible_messages(messages):
    return [{key: value for key, value in row.items() if key != "cache_scope"} for row in messages]


def compare(agent_type, directory):
    config = TrinaConfig().with_overrides(run__workspace=str(directory / "runs"), audit__dir=str(directory / "logs"),
                                          llm__context_window=1000000, llm__max_tokens=1024)
    backend = ReplayBackend(LLMConfig(backend="replay"))
    requests, previous = [], b""
    def complete(messages, **kwargs):
        nonlocal previous
        visible = visible_messages(messages)
        current = b"".join(encoded(row) + b"\n" for row in visible)
        last = messages[-1]["content"]
        first_line = last.splitlines()[0]
        requests.append({"request": len(requests) + 1, "visible_input_bytes": len(current),
                         "runtime_tail_bytes": len(last.encode()), "runtime_heading": first_line,
                         "existing_prefix_preserved": current.startswith(previous) if previous else None})
        previous = current
        return LLMResponse(text="已读取测试状态")
    backend.complete_messages = complete
    chat = agent_type(config, registry=ToolRegistry([]), backend=backend, run_id="controlled")
    try:
        for index in range(12):
            host = chat.state.host(f"127.0.0.{index + 1}")
            host.add_port(80, service="http", version="test-1")
            host.technologies = ["test-http"]
        for index in range(20):
            item = Hypothesis(id=f"H{index:02}", statement=f"受控测试假设 {index}，结果需真实工具验证", target="127.0.0.1")
            chat.ledger.items[item.id] = item
        for index in range(24):
            if index % 3 == 0:
                chat.ledger.items["H00"].status = "verifying" if index % 2 else "open"
            chat.ask(f"核对当前测试状态 {index}")
        return {"requests": requests, "sum_runtime_tail_bytes": sum(row["runtime_tail_bytes"] for row in requests),
                "sum_visible_input_bytes": sum(row["visible_input_bytes"] for row in requests)}
    finally:
        chat.close()


def compaction(directory, context_window=32768, max_tokens=4096):
    config = TrinaConfig().with_overrides(run__workspace=str(directory / "runs"), audit__dir=str(directory / "logs"),
                                          llm__context_window=context_window, llm__max_tokens=max_tokens)
    backend = ReplayBackend(LLMConfig(backend="replay"))
    backend.complete_messages = lambda messages, **kwargs: LLMResponse(text="受控回复 " * 500)
    chat = ChatAgent(config, registry=ToolRegistry([]), backend=backend, run_id="compaction")
    try:
        for index in range(15):
            chat.ask(f"仅处理本地测试材料 {index}")
        archives = []
        for path in sorted(chat.workspace.rglob("epoch-*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            archives.append({"epoch": raw["epoch"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "original_messages": len(raw["messages"]), "retained_messages": len(raw["next_messages"]),
                             "original_visible_bytes": len(encoded(visible_messages(raw["messages"]))),
                             "retained_visible_bytes": len(encoded(visible_messages(raw["next_messages"]))),
                             "retained_user_constraints": len(raw["instructions"])})
        stats = chat.state.context_stats
        return {"context_window": context_window, "output_reserve": max_tokens,
                "compactions": stats["compactions"], "estimated_input_tokens": stats["estimated_input_tokens"],
                "input_budget": stats["input_budget"], "archives": archives,
                "audit_chain_valid": chat.audit.verify()[0], "preserved_instructions": chat.context.instructions}
    finally:
        chat.close()


if __name__ == "__main__":
    module = types.ModuleType("trina.brain._cache_before")
    module.__package__ = "trina.brain"
    sys.modules[module.__name__] = module
    source = subprocess.check_output(["git", "show", f"{BASELINE}:trina/brain/chat.py"], cwd=ROOT).decode("utf-8")
    exec(compile(source, f"{BASELINE}:trina/brain/chat.py", "exec"), module.__dict__)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        print(json.dumps({"execution": "actual_local_replay", "baseline_chat_class": BASELINE,
                          "shared_dependencies": "current prompt/protocol constants; identical controlled state and inputs",
                          "reported_cache_hit_rate": None, "before": compare(module.ChatAgent, root / "before"),
                          "after": compare(ChatAgent, root / "after"), "compaction": compaction(root / "compact"),
                          "tight_budget_compaction": compaction(root / "tight", 10000, 1024)},
                         ensure_ascii=False, indent=2))
