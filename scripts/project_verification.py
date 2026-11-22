"""管理本地项目验证工作单，绑定输入、记录交接并限时执行只读检查。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from trina.chat_store import ChatStore

from .verify_contract import PROJECT_ROOT, execute_check


CHECKS = ("V-CONTRACT", "V-COURSE")
WORK_DIR = Path("runs/project-verification")


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", value):
        raise ValueError("工作单和前提 ID 须使用 1–48 位小写字母、数字或连字符")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} 须为正整数秒数")
    return value


def _read_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("需要 JSON 对象")
    return data


def _save_json(path: Path, data: dict[str, Any]) -> None:
    """先写临时文件再替换，读者只能看到完整快照。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".verification-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _check_plan(plan: dict[str, Any]) -> tuple[str, int]:
    premise = _identifier(plan.get("premise"))
    budget = _positive_int(plan.get("budget_s"), "budget_s")
    for field in ("expected_signal", "failure_criterion", "alternative"):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError(f"验证计划缺少 {field}")
    checks = plan.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("验证计划须声明至少一项检查及其预计耗时")
    ids = set()
    estimated = 0
    for check in checks:
        if not isinstance(check, dict) or check.get("id") not in CHECKS:
            raise ValueError("工作单只接受 V-CONTRACT 和 V-COURSE")
        if check["id"] in ids:
            raise ValueError("一轮内每项检查只能出现一次")
        ids.add(check["id"])
        estimated += _positive_int(check.get("estimated_s"), "estimated_s")
    if estimated > budget:
        raise ValueError(f"预计耗时 {estimated}s 超过总预算 {budget}s")
    if "replaces" in plan:
        _identifier(plan["replaces"])
    return premise, budget


class ProjectVerification:
    """工作区内的工程验证记录，每个工作单独立写入。"""

    def __init__(self, root: Path, work_id: str):
        self.root = root.resolve()
        self.work_id = _identifier(work_id)
        self.directory = self.root / WORK_DIR / work_id
        self.path = self.directory / "STATE.json"
        self._locks = ChatStore(self.root / WORK_DIR)

    def _local_file(self, path: str | Path) -> Path:
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"路径超出工作区: {path}")
        return resolved

    def _managed(self, path: Path) -> Path:
        if self._local_file(path) != path:
            raise ValueError(f"工作单产物不能通过链接重定向: {path}")
        return path

    def _lock(self):
        self._managed(self.directory / ".chat.lock")
        try:
            return self._locks.acquire(self.work_id)
        except ValueError as exc:
            raise ValueError("该工作单正在由另一个进程更新") from exc

    def _file_ref(self, path: str | Path) -> dict[str, str]:
        resolved = self._local_file(path)
        if not resolved.is_file():
            raise ValueError(f"文件不存在或不是普通文件: {path}")
        digest = hashlib.sha256()
        with resolved.open("rb") as source:
            for block in iter(lambda: source.read(65536), b""):
                digest.update(block)
        return {"path": resolved.relative_to(self.root).as_posix(), "sha256": digest.hexdigest()}

    def initialize(self, inputs: list[str]) -> dict[str, Any]:
        if not inputs:
            raise ValueError("至少绑定一个本地配置或样例文件")
        with self._lock():
            path = self._managed(self.path)
            if path.exists():
                raise ValueError("工作单已存在")
            refs = {ref["path"]: ref for ref in map(self._file_ref, inputs)}
            binding = {"workspace": os.path.normcase(str(self.root)), "work_id": self.work_id,
                       "inputs": [refs[name] for name in sorted(refs)]}
            state = {"version": 1, "binding": binding, "verdicts": {}, "rounds": {}}
            _save_json(path, state)
        return state

    def show(self, *, verify_inputs: bool = False) -> dict[str, Any]:
        state = _read_object(self._managed(self.path))
        binding = state.get("binding")
        if (state.get("version") != 1 or not isinstance(binding, dict) or binding.get("work_id") != self.work_id
                or binding.get("workspace") != os.path.normcase(str(self.root))):
            raise ValueError("工作单与当前工作区或 ID 不匹配")
        if not isinstance(state.get("verdicts"), dict) or not isinstance(state.get("rounds"), dict):
            raise ValueError("工作单记录格式无效")
        if verify_inputs:
            self._verify_inputs(binding)
        return state

    def _verify_inputs(self, binding: dict[str, Any]) -> None:
        refs = binding.get("inputs")
        if not isinstance(refs, list) or not refs:
            raise ValueError("工作单缺少绑定文件")
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                raise ValueError("绑定文件格式无效")
            if self._file_ref(ref["path"]) != ref:
                raise ValueError(f"绑定文件已变化: {ref['path']}")

    def handoff(self) -> dict[str, Any]:
        state = self.show()
        refuted = [premise for premise, verdict in state["verdicts"].items()
                   if verdict["outcome"] == "refuted"]
        return {"state": self.path.relative_to(self.root).as_posix(), "refuted": refuted[:8],
                "refuted_count": len(refuted)}

    def record(self, premise: str, outcome: str, evidence: str) -> dict[str, Any]:
        _identifier(premise)
        if outcome not in {"verified", "refuted"}:
            raise ValueError("结论须为 verified 或 refuted")
        with self._lock():
            state = self.show(verify_inputs=True)
            if self._local_file(evidence) == self.path:
                raise ValueError("工作单自身不能作为证据")
            verdict = {"outcome": outcome, "evidence": self._file_ref(evidence)}
            previous = state["verdicts"].get(premise)
            if previous is not None and previous != verdict:
                raise ValueError("该前提已有带证据的结论，请为新前提使用新的 ID")
            if previous is None:
                state["verdicts"][premise] = verdict
                _save_json(self.path, state)
        return verdict

    def run(self, plan_path: str) -> dict[str, Any]:
        with self._lock():
            state = self.show(verify_inputs=True)
            plan = _read_object(self._local_file(plan_path))
            premise, budget = _check_plan(plan)
            verdicts = state["verdicts"]
            if verdicts.get(premise, {}).get("outcome") == "refuted":
                raise ValueError("该前提已被证否")
            if premise in state["rounds"]:
                raise ValueError("该前提已登记过一轮验证，请更新前提")
            if prior := plan.get("replaces"):
                previous = verdicts.get(prior, {})
                if previous.get("outcome") != "refuted":
                    raise ValueError("replaces 须引用本工作单内带证据的已证否前提")
                if self._file_ref(previous["evidence"]["path"]) != previous["evidence"]:
                    raise ValueError("被替换前提的证据文件已变化")

            evidence = self._managed(self.directory / "checks" / f"{premise}.json")
            results: list[dict[str, Any]] = []
            entry = {"plan": plan, "status": "started", "evidence": evidence.relative_to(self.root).as_posix()}
            state["rounds"][premise] = entry
            _save_json(self.path, state)

            started = time.monotonic()
            status = "passed"
            try:
                for check in plan["checks"]:
                    remaining = budget - (time.monotonic() - started)
                    if remaining <= 0:
                        status = "timed_out"
                        break
                    result = execute_check(self.root, check["id"], timeout=remaining)
                    results.append({"id": check["id"], **result})
                    if result["timed_out"] or time.monotonic() - started >= budget:
                        status = "timed_out"
                        break
                    if result["exit_code"] != 0:
                        status = "failed"
                        break
            except KeyboardInterrupt:
                status = "interrupted"
            except OSError as exc:
                status = "runner_error"
                results.append({"error": str(exc)})
            elapsed = round(time.monotonic() - started, 3)
            try:
                self._verify_inputs(state["binding"])
            except (OSError, ValueError) as exc:
                results.append({"binding_error": str(exc)})
                if status in {"passed", "failed"}:
                    status = "input_changed"
            _save_json(evidence, {"status": status, "elapsed_s": elapsed, "results": results})
            entry.update(status=status, elapsed_s=elapsed, evidence_sha256=self._file_ref(evidence)["sha256"])
            _save_json(self.path, state)
            return {"premise": premise, **entry}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("init", "show", "handoff", "record", "run"):
        commands.add_parser(name).add_argument("work_id")
    commands.choices["init"].add_argument("--input", action="append", required=True)
    commands.choices["record"].add_argument("premise")
    commands.choices["record"].add_argument("outcome", choices=("verified", "refuted"))
    commands.choices["record"].add_argument("--evidence", required=True)
    commands.choices["run"].add_argument("--plan", required=True)
    args = parser.parse_args()
    try:
        work = ProjectVerification(PROJECT_ROOT, args.work_id)
        if args.action == "init":
            work.initialize(args.input)
            result = work.handoff()
        elif args.action == "record":
            result = work.record(args.premise, args.outcome, args.evidence)
        elif args.action == "run":
            result = work.run(args.plan)
        else:
            result = work.show() if args.action == "show" else work.handoff()
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error · {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(args.action == "run" and result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
