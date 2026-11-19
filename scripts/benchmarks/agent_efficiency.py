"""固定回放基准：比较旧式完整 JSON 截断与 ToolResultView 的模型输入体积。"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from trina.brain.agent import PentestAgent
from trina.config import TrinaConfig
from trina.llm import LLMResponse
from trina.llm.replay_backend import ReplayBackend
from trina.tools import Tool, ToolRegistry, ToolResult, ToolSpec


class FixtureTool(Tool):
    spec = ToolSpec(name="fixture_large", summary="固定大型结果夹具", action_level="detect")

    def run(self, ctx):
        rows = [{"id": index, "status": "observed", "body": "fixture-value-" * 80} for index in range(80)]
        return ToolResult(ok=True, summary="固定夹具完成", data={"status": "ok", "items": rows},
                          raw="raw-fixture-output\n" * 5000,
                          facts={"flags": [{"value": "flag{fixture}"}]})


def legacy_observation(result: ToolResult, max_chars: int = 2600) -> str:
    parts = [f"[{'OK' if result.ok else '失败'}] {result.summary or result.error or '(无摘要)'}"]
    rendered = json.dumps(result.data or {}, ensure_ascii=False, indent=1)
    parts.append(rendered[:max_chars] + ("\n…(截断)" if len(rendered) > max_chars else ""))
    if result.error:
        parts.append(f"错误: {result.error}")
    return "\n".join(parts)


def run(output: Path | None = None) -> dict:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="trina-efficiency-") as directory:
        root = Path(directory)
        config = TrinaConfig().with_overrides(
            run__workspace=str(root / "runs"), audit__dir=str(root / "logs"),
            llm__backend="replay", llm__context_window=32768, llm__max_tokens=1024,
        )
        backend = ReplayBackend(config.llm)
        responses = [
            LLMResponse(text='{"action":"fixture_large","params":{}}'),
            LLMResponse(text='{"final":{"summary":"固定回放完成"}}'),
        ]
        backend.complete_messages = lambda messages, **kwargs: responses.pop(0)
        fixture = FixtureTool().run(None)
        agent = PentestAgent(config, registry=ToolRegistry([FixtureTool()]), backend=backend)
        state = agent.run("127.0.0.1", goal="获得 flag", max_steps=4)
        performance = dict(state.performance)
        performance["path"] = "<temporary-run>/performance.json"
        result = {
            "scenario": "replay-large-tool-result",
            "conditions": {"backend": "replay", "context_window": 32768, "max_tokens": 1024,
                           "tool_rows": 80, "raw_lines": 5000},
            "legacy_observation_chars": len(legacy_observation(fixture)),
            "optimized_observation_chars": len(fixture.to_observation()),
            "state": {"status": state.status, "counters": state.counters,
                      "context_stats": state.context_stats, "performance": performance},
            "wall_time_seconds": round(time.perf_counter() - started, 6),
        }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    run(parser.parse_args().output)
