"""运行真实 ELF 纵向用例，保留工具协议、审计、实际输入与本地验证证据。"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "posix" or not all(shutil.which(name) for name in ("r2", "gdb", "gcc", "bwrap")):
        print("真实测试未运行：需要 Linux、radare2、GDB、GCC 与 bubblewrap")
        return 2
    from tests.test_binary import RealBinaryTests
    from trina.tools.external import run_external
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    case = RealBinaryTests("test_real_static_dynamic_candidate_and_authoritative_local_verification")
    calls = []
    started = time.monotonic()
    case.setUp()
    try:
        native_call = case.call
        def observed(name, **params):
            begin = time.monotonic()
            data = native_call(name, **params)
            calls.append({"tool": name, "params": params, "actual_data": data, "seconds": time.monotonic() - begin})
            return data
        case.call = observed
        case.test_real_static_dynamic_candidate_and_authoritative_local_verification()
        case.ctx.extra["binary_sessions"].close()
        versions = {name: run_external([name, "--version" if name != "r2" else "-v"], case.ctx).stdout.splitlines()[0]
                    for name in ("r2", "gdb", "gcc", "bwrap")}
        audit_ok, audit_detail = case.ctx.audit.verify()
        logs = list(case.ctx.audit.iter_records(case.ctx.audit.path))
        observations = [row for row in logs if row.get("event") == "binary.observation"]
        raw_dir = output / "raw"
        if raw_dir.exists():
            shutil.rmtree(raw_dir)
        raw_dir.mkdir()
        raw_index = {}
        for path in case.ctx.workspace.glob("binary-*.json"):
            shutil.copyfile(path, raw_dir / path.name)
            raw_index[str(path)] = "raw/" + path.name
        shutil.copyfile(case.ctx.workspace / "encoded.h", output / "encoded.h")
        shutil.copyfile(case.ctx.audit.path, output / "audit.jsonl")
        result = {"execution": "actual_native_elf", "platform": platform.platform(), "versions": versions,
                  "fixture_verified": True, "negative_control": "incorrect input was executed and rejected",
                  "audit_ok": audit_ok, "audit_detail": audit_detail, "tool_calls": calls,
                  "elapsed_seconds": time.monotonic() - started,
                  "native_observation_bytes": sum(row.get("raw_bytes", 0) for row in observations),
                  "raw_index": raw_index,
                  "closed_sessions": case.ctx.extra["binary_sessions"].snapshot(),
                  "model_usage": None, "ctfy_online_score": None,
                  "limits": "确定性工具链测试；未运行真实模型/CTFY 线上评分或四组模型质量对照"}
        (output / "native-e2e.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: result[key] for key in ("execution", "versions", "fixture_verified", "audit_ok", "elapsed_seconds", "native_observation_bytes")}, ensure_ascii=False))
        return 0
    finally:
        case.doCleanups()


if __name__ == "__main__":
    raise SystemExit(main())
