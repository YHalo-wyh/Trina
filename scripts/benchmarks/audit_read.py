"""用固定 JSONL 日志测量离线审计命令的耗时和 Python 内存峰值。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import statistics
import tempfile
import time
import tracemalloc
from contextlib import redirect_stdout
from pathlib import Path

from trina.cli import main as trina_main


def write_fixture(path: Path, rows: int) -> None:
    previous = "0" * 64
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for seq in range(1, rows + 1):
            record = {
                "seq": seq,
                "ts": 1700000000 + seq,
                "time": "2023-11-14 22:13:20",
                "event": "fixture.observed",
                "decision": "deny" if seq % 13 == 0 else "allow",
                "tool": "fixture",
                "note": "固定审计记录" * 64,
                "prev_hash": previous,
            }
            canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            previous = hashlib.sha256(canonical.encode()).hexdigest()
            record["hash"] = previous
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def invoke(mode: str, path: Path) -> str:
    args = ["audit", mode, str(path)]
    if mode != "verify":
        args.extend(("--limit", "20"))
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        status = trina_main(args)
    if status:
        raise RuntimeError(f"audit {mode} 失败，退出码 {status}")
    escaped_path = json.dumps(str(path), ensure_ascii=False)[1:-1]
    return stdout.getvalue().replace(escaped_path, path.name).replace(str(path), path.name)


def measure(mode: str, path: Path, repeats: int) -> dict[str, float | str]:
    reference = invoke(mode, path)
    elapsed = []
    for _ in range(repeats):
        start = time.perf_counter()
        output = invoke(mode, path)
        elapsed.append((time.perf_counter() - start) * 1000)
        if output != reference:
            raise RuntimeError(f"audit {mode} 输出在重复测量间发生变化")
    tracemalloc.start()
    try:
        invoke(mode, path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        "p50_ms": round(statistics.median(elapsed), 3),
        "p95_ms": round(sorted(elapsed)[math.ceil(repeats * 0.95) - 1], 3),
        "peak_mib": round(peak / (1024 * 1024), 3),
        "output_sha256": hashlib.sha256(reference.encode()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, nargs="+", default=[100, 20000])
    parser.add_argument("--repeats", type=int, default=21)
    args = parser.parse_args()
    if args.repeats < 1 or min(args.records) < 1:
        parser.error("记录数和重复次数须为正整数")
    cases = {}
    with tempfile.TemporaryDirectory(prefix="trina-audit-bench-") as directory:
        path = Path(directory) / "fixture.jsonl"
        for rows in args.records:
            write_fixture(path, rows)
            cases[rows] = {
                "bytes": path.stat().st_size,
                "commands": {mode: measure(mode, path, args.repeats) for mode in ("verify", "show", "tail")},
            }
    print(json.dumps({"repeats": args.repeats, "cases": cases}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
