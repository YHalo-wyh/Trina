"""真实模型回归：独立 Lab、无限任务与未经点名的模型自主委派。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from trina.brain.agent import PentestAgent
from trina.config import TrinaConfig
from trina.eval.live_batch import RecordingBackend, judge, load_tasks
from trina.interactive import Console
from trina.lab.http_target import create_server
from trina.llm import make_backend

OUT = Path(__file__).resolve().parent
STAMP = time.strftime("%Y%m%d-%H%M%S")


class RecordingToolsBackend(RecordingBackend):
    def complete_messages(self, messages, **kwargs):
        response = super().complete_messages(messages, **kwargs)
        self.exchanges[-1]["available_tools"] = [item["function"]["name"] for item in kwargs.get("tools") or []]
        return response


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_task(base, source):
    server, thread = create_server(port=0)
    port = server.server_address[1]
    task = replace(source, goal=source.goal.replace("9099", str(port)),
                   target=source.target.replace("9099", str(port)), max_steps=0)
    backend = RecordingBackend(make_backend(base.llm))
    agent = PentestAgent(base, backend=backend)
    started = time.monotonic()
    try:
        state = agent.run(task.target, goal=task.goal, max_steps=0, max_seconds=0,
                          run_id=f"live-unlimited-{STAMP}-{source.id}")
        result = {"id": source.id, "goal": task.goal, "expected_tool": task.expect_tool,
                  **judge(task, state.steps, state.flags), "status": state.status,
                  "steps": state.steps, "counters": state.counters,
                  "duration": round(time.monotonic() - started, 2), "ledger": state.ledger,
                  "artifacts": state.artifacts, "audit": agent._ctx.audit.verify(),
                  "exchanges": backend.exchanges}
        save(f"task-{source.id}.json", result)
        return result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run_suite(config):
    tasks = load_tasks()
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = {pool.submit(run_task, config, task): task.id for task in tasks}
        for future in as_completed(pending):
            try:
                row = future.result()
            except Exception as exc:
                row = {"id": pending[future], "accepted": False, "error": repr(exc)}
            results.append(row)
            print(json.dumps({key: row[key] for key in ("id", "accepted", "status", "error") if key in row},
                             ensure_ascii=False), flush=True)
    summary = {"model": config.llm.model, "reasoning_effort": config.llm.reasoning_effort,
               "limits": {"max_steps": config.llm.max_steps, "max_seconds": config.llm.max_seconds},
               "tasks": len(tasks), "accepted": sum(bool(row.get("accepted")) for row in results),
               "audit_ok": all(row.get("audit", [False])[0] for row in results),
               "results": sorted(results, key=lambda row: row["id"])}
    save("live-suite.json", summary)
    return summary


def run_delegation(config, *, explicit=False):
    server, thread = create_server(port=0)
    target = f"http://127.0.0.1:{server.server_address[1]}"
    console = Console(config, output=lambda line: None, plain=True, autostart_lab=False)
    backend = RecordingToolsBackend(make_backend(config.llm))
    console._backend_obj = backend
    chat = console._ensure_chat()
    # 请求没有点名 worker_tasks、spawn 或子 Worker；委派完全由模型选择。
    prompt = (f"完成 {target} 的三项独立检查："
              "首页HTTP响应和技术特征；/sqli 的 id 参数SQL注入；/lfi 的 file 参数文件读取。"
              "仅检查这些指定入口，各项保留实际请求与响应证据，完成后统一汇总确认项和未确认项。")
    if explicit:
        prompt += "SQL注入和文件读取各委派给一个独立子Worker，主会话检查首页，最后取回两个任务的真实结果。"
    started = time.monotonic()
    try:
        reply = chat.ask(prompt)
        manager = console.subworkers
        while manager.running:
            manager.thread.join(timeout=1)
        rows = manager.rows()
        results = [manager.task_result(row["id"]) for row in rows]
        calls = [call for exchange in backend.exchanges for call in exchange["tool_calls"]]
        worker_calls = [call for call in calls if call["function"]["name"] == "worker_tasks"]
        actions = [json.loads(call["function"]["arguments"]).get("action") for call in worker_calls]
        result = {"prompt": prompt, "explicit_delegation_requested": explicit,
                  "model": config.llm.model, "reply": reply,
                  "duration": round(time.monotonic() - started, 2), "worker_actions": actions,
                  "spawned_autonomously": "spawn" in actions,
                  "retrieved_by_model": any(action in {"result", "wait"} for action in actions),
                  "workers": rows, "results": results, "chat_run_id": chat.run_id,
                  "audit": chat.audit.verify(), "exchanges": backend.exchanges}
        save("delegation-explicit.json" if explicit else "delegation-natural.json", result)
        print(json.dumps({key: result[key] for key in ("spawned_autonomously", "retrieved_by_model", "worker_actions", "audit")},
                         ensure_ascii=False), flush=True)
        return result
    finally:
        console._cleanup()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("suite", "delegation", "delegation-explicit"))
    args = parser.parse_args()
    config = TrinaConfig.load()
    assert config.llm.max_steps == config.llm.max_seconds == 0
    if args.mode == "suite":
        result = run_suite(config)
        return 0 if result["accepted"] == result["tasks"] and result["audit_ok"] else 1
    result = run_delegation(config, explicit=args.mode == "delegation-explicit")
    return 0 if result["spawned_autonomously"] and result["retrieved_by_model"] and result["audit"][0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
