"""E4 对照实验驱动：洞察层「开 / 关」的 A/B 跑分（同一任务、交替执行、逐次落数）。

两臂**唯一的差别**是 `run.hypothesis_ledger` / `run.loop_guard`（假设台账 + 循环守卫建议），
其余参数、靶机、模型完全一致；交替执行是为了摊平时间漂移（模型采样、靶机状态）。

用法::

    python3 scripts/experiments/e4_insight_ab.py --runs 3 --max-steps 8 \
        --target http://127.0.0.1:9099 --goal "拿到 flag"

输出：``reports/exp-e4-ab-<时间戳>.json``（逐次指标 + 两臂汇总）+ 终端表格。
只读 `runs/<id>/state.json`，不改动任何运行业务逻辑。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _run_id_set() -> set[str]:
    return {path.parent.name for path in (ROOT / "runs").glob("*/state.json")}


def _metrics(state: dict[str, Any]) -> dict[str, Any]:
    findings = state.get("findings") or []
    coverage = state.get("coverage") or {}
    counters = state.get("counters") or {}
    ledger = state.get("ledger") or {}
    loop = state.get("loop") or {}
    return {
        "status": state.get("status"),
        "verdict": state.get("verdict"),
        "findings": len(findings),
        "findings_verified": len([f for f in findings if f.get("status") in ("verified", "exploited")]),
        "flags": len(state.get("flags") or []),
        "executed_checks": len(coverage.get("executed") or []),
        "untested_checks": len(coverage.get("untested") or []),
        "duration": state.get("duration"),
        "steps": counters.get("steps"),
        "tool_calls": counters.get("tool_calls"),
        "llm_calls": counters.get("llm_calls"),
        "tokens_in": ledger.get("input_tokens"),
        "tokens_out": ledger.get("output_tokens"),
        "cost_usd": round(float(ledger.get("cost_usd") or 0.0), 6),
        "redundant_rate": loop.get("redundant_action_rate"),
        "failures": sum(int(v) for k, v in counters.items() if str(k).startswith("fail_")),
        "hypotheses": len(state.get("hypotheses") or []),
        "denied": counters.get("denied"),
    }


def _play(arm: str, args: argparse.Namespace) -> tuple[int, str, str]:
    cmd = [sys.executable, "-m", "trina", "play", "--max-steps", str(args.max_steps)]
    if args.target:
        cmd += ["--target", args.target]
    if args.goal:
        cmd += ["--goal", args.goal]
    if arm == "off":
        cmd += ["--set", "run.hypothesis_ledger=false", "--set", "run.loop_guard=false"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    # 注意：play 对"非成功结论"返回 1（如 INCONCLUSIVE），这不代表运行本身失败——
    # 判据是"是否产出了新的 run_id"，不是退出码。
    return proc.returncode, proc.stdout, proc.stderr


def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok_rows = [row for row in rows if "status" in row]

    def mean(key: str) -> float | None:
        values = [float(row[key]) for row in ok_rows if isinstance(row.get(key), (int, float))]
        return round(statistics.mean(values), 4) if values else None

    return {
        "runs": len(ok_rows),
        "errored": len(rows) - len(ok_rows),
        "success": len([row for row in ok_rows if row["status"] == "success"]),
        "flags": sum(int(row["flags"] or 0) for row in ok_rows),
        "findings_avg": mean("findings"),
        "findings_verified_avg": mean("findings_verified"),
        "executed_checks_avg": mean("executed_checks"),
        "duration_avg": mean("duration"),
        "steps_avg": mean("steps"),
        "llm_calls_avg": mean("llm_calls"),
        "cost_usd_total": round(sum(float(row["cost_usd"] or 0) for row in ok_rows), 6),
        "redundant_rate_avg": mean("redundant_rate"),
        "failures_total": sum(int(row["failures"] or 0) for row in ok_rows),
        "hypotheses_avg": mean("hypotheses"),
    }


def _row_from_state(arm: str, run_id: str, **extra: Any) -> dict[str, Any]:
    state_path = ROOT / "runs" / run_id / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return {"arm": arm, "run_id": run_id, **_metrics(state), **extra}


def _collect(spec: str, results: dict[str, list[dict[str, Any]]]) -> None:
    """从已有运行重建逐次数据（``on=id1,id2 off=id3`` 形式），避免重复烧钱。"""
    for chunk in spec.split():
        arm, _, ids = chunk.partition("=")
        arm = arm.strip()
        if arm not in results:
            raise SystemExit(f"--collect 的臂名只能是 on/off，收到 {arm!r}")
        for run_id in [item.strip() for item in ids.split(",") if item.strip()]:
            results[arm].append(_row_from_state(arm, run_id, reused=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="E4 洞察层 A/B 对照实验")
    parser.add_argument("--runs", type=int, default=3, help="每臂运行次数（默认 3）")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument(
        "--target", default="",
        help="目标；留空则让每次 play 自己拉起内置 lab 靶机（推荐：谁起来谁回收，避免靶机中途被清理）",
    )
    parser.add_argument("--goal", default="拿到 flag")
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--collect", default="",
        help="不跑新任务，从已有运行重建汇总：'on=id1,id2 off=id3'（配合 --out 复用已花费的样本）",
    )
    args = parser.parse_args()

    results: dict[str, list[dict[str, Any]]] = {"on": [], "off": []}
    if args.collect:
        _collect(args.collect, results)
        print(f"从已有运行重建：开 {len(results['on'])} 次 / 关 {len(results['off'])} 次")
    else:
        order = ["on", "off"] * args.runs
        for index, arm in enumerate(order, 1):
            before = _run_id_set()
            label = "洞察层=开" if arm == "on" else "洞察层=关"
            print(f"[{index}/{len(order)}] {label} 开始…", flush=True)
            started = time.time()
            code, stdout, stderr = _play(arm, args)
            new_ids = _run_id_set() - before
            elapsed = round(time.time() - started, 1)
            if not new_ids:
                tail = (stderr.strip() or stdout.strip())[-200:]
                print(f"    ! 未产出运行（exit={code}, {elapsed}s）: {tail}")
                row = {"arm": arm, "error": tail or "未产生新的 run_id", "duration": elapsed}
            else:
                run_id = sorted(new_ids)[-1]
                row = _row_from_state(arm, run_id, wall_seconds=elapsed, exit_code=code)
                print(
                    f"    {run_id}: 状态={row['status']} 结论={row['verdict']} "
                    f"漏洞={row['findings']}(已验证 {row['findings_verified']}) flag={row['flags']} "
                    f"检查={row['executed_checks']} 步数={row['steps']} 成本=${row['cost_usd']} "
                    f"冗余率={row['redundant_rate']} 失败={row['failures']} 假设={row['hypotheses']}（{elapsed}s）",
                    flush=True,
                )
            results[arm].append(row)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config": {
            "target": args.target or "每次 play 自拉内置 lab（127.0.0.1:9099）",
            "goal": args.goal,
            "max_steps": args.max_steps,
            "runs_per_arm": args.runs,
            "arm_off_overrides": ["run.hypothesis_ledger=false", "run.loop_guard=false"],
        },
        "runs": results,
        "summary": {arm: _summarise(rows) for arm, rows in results.items()},
    }
    out = Path(args.out) if args.out else ROOT / "reports" / f"exp-e4-ab-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n== 汇总 ==")
    keys = list(payload["summary"]["on"].keys())
    print(f"{'指标':<20}{'洞察层=开':>14}{'洞察层=关':>14}")
    for key in keys:
        print(f"{key:<20}{str(payload['summary']['on'][key]):>14}{str(payload['summary']['off'][key]):>14}")
    print(f"\n逐次数据: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
