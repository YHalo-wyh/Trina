"""本地可复现的 Swarm 并发基准，不调用模型或网络。

它只测 Commander 的任务领取、Worker 并发、租约回收和结果契约，避免把
供应商延迟混进调度数据。运行：``python scripts/benchmarks/swarm_concurrency.py``。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from trina.config import TrinaConfig
from trina.state import RunState
from trina.swarm import SwarmRuntime, TaskSpec


def run_case(worker_count: int, task_count: int = 6, work_seconds: float = 0.12) -> dict:
    active = 0
    peak = 0
    lock = threading.Lock()
    with tempfile.TemporaryDirectory(prefix="trina-swarm-bench-") as root_name:
        root = Path(root_name)
        config = TrinaConfig.load().with_overrides(
            run__workspace=str(root / "runs"),
            audit__dir=str(root / "logs"),
            report__output_dir=str(root / "reports"),
        )
        tasks = [TaskSpec(f"task-{index}", "127.0.0.1", f"独立本地基准分支 {index}")
                 for index in range(task_count)]

        def execute(worker, task, run_id, hints, registry):
            nonlocal active, peak
            del hints, registry
            with lock:
                active += 1
                peak = max(peak, active)
            started = time.perf_counter()
            time.sleep(work_seconds)
            with lock:
                active -= 1
            state = RunState(run_id=run_id, target=task.target, goal=task.goal, status="success")
            state.host(task.target).technologies.append(worker.id)
            state.ended_at = time.time()
            return state

        started = time.perf_counter()
        summary = SwarmRuntime(
            config, tasks, max_workers=worker_count, executor=execute, write_report=False,
        ).run()
        wall = time.perf_counter() - started
        statuses = {item["status"] for item in summary["tasks"].values()}
        return {
            "workers": worker_count,
            "tasks": task_count,
            "work_seconds": work_seconds,
            "wall_seconds": round(wall, 3),
            "peak_active": peak,
            "status": sorted(statuses),
            "workers_created": summary["workers_created"],
            "worker_reuses": summary["worker_reuses"],
            "contract_versions": sorted({item["result"].get("contract_version")
                                          for item in summary["tasks"].values()}),
        }


def main() -> None:
    rows = [run_case(count) for count in (1, 2, 3)]
    serial = rows[0]["wall_seconds"]
    for row in rows:
        row["speedup_vs_1_worker"] = round(serial / row["wall_seconds"], 2) if row["wall_seconds"] else 0
    print(json.dumps({"benchmark": "local_swarm_scheduler", "rows": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
