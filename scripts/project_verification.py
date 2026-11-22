"""管理本地项目验证工作单，绑定输入、记录交接并限时执行只读检查。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from html import escape
from pathlib import Path
from typing import Any

from trina.chat_store import ChatStore

from .verify_contract import PROJECT_ROOT, execute_check


CHECKS = ("V-CONTRACT", "V-COURSE")
WORK_DIR = Path("runs/project-verification")
_REPORT_MARKER = "<!-- scripts.project_verification -->\n"
_STATUS_TEXT = {"ready": "待检查", "started": "结果未落盘", "passed": "通过", "failed": "检查失败",
                "timed_out": "超时", "interrupted": "已中断", "runner_error": "运行错误",
                "input_changed": "输入变更，需复验", "evidence_changed": "结果变更或缺失，需复验"}


class PreflightError(ValueError):
    """附可供本地检查入口展示的原因码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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


def _save_text(path: Path, text: str) -> None:
    """先写临时文件再替换，读者只能看到完整快照。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".verification-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_json(path: Path, data: dict[str, Any]) -> None:
    _save_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _save_report(path: Path, lines: list[str]) -> None:
    if path.exists() and not path.read_text(encoding="utf-8").startswith(_REPORT_MARKER):
        raise ValueError(f"同名文件需保留: {path.name}")
    _save_text(path, _REPORT_MARKER + "\n".join(lines) + "\n")


def _table_cell(value: str) -> str:
    # 结论和文件名都是字面数据，不能插入新行、表格列或链接。
    text = escape(" ".join(value.split()), quote=False)
    return re.sub(r"[|`\[\]\\*_~]", lambda match: f"&#{ord(match[0])};", text)


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

    def record(self, premise: str, outcome: str, evidence: str, *, conclusion: str = "", recheck: str = "") -> dict[str, Any]:
        _identifier(premise)
        if outcome not in {"verified", "refuted"}:
            raise ValueError("结论须为 verified 或 refuted")
        with self._lock():
            state = self.show(verify_inputs=True)
            if self._local_file(evidence) in {self.path, self.directory / "REJECTED.md", self.root / WORK_DIR / "STATUS.md"}:
                raise ValueError("工作单及其交接视图不能作为证据")
            verdict = {"outcome": outcome, "evidence": self._file_ref(evidence)}
            previous = state["verdicts"].get(premise, {})
            metadata = {key: value.strip() for key, value in (("conclusion", conclusion), ("recheck", recheck)) if value.strip()}
            if any(key in previous and previous[key] != value for key, value in (verdict | metadata).items()):
                raise ValueError("该前提已有带证据的结论，请为新前提使用新的 ID")
            verdict = previous | verdict | metadata
            if verdict != previous:
                state["verdicts"][premise] = verdict
                _save_json(self.path, state)
        return verdict

    def _ref_is_current(self, ref: dict[str, str]) -> bool:
        try:
            return self._file_ref(ref["path"]) == ref
        except (OSError, ValueError, KeyError):
            return False

    def write_reports(self, state: dict[str, Any]) -> tuple[str, str, int, str]:
        """生成证否明细，并返回索引所需的状态和轮次。"""
        try:
            self._verify_inputs(state["binding"])
            inputs_current = True
        except (OSError, ValueError):
            inputs_current = False

        lines = [f"# {self.work_id} · 已证否前提", "", "[工作单及原始记录](STATE.json)", "", "## 适用输入", ""]
        for ref in state["binding"]["inputs"]:
            lines.append(f"- {_table_cell(ref['path'])} · {_table_cell(ref['sha256'])}")
        lines.extend(["", "| 前提 | 结论 | 适用性 | 证据路径与 SHA-256 | 复验方式 |", "|---|---|---|---|---|"])
        refuted = 0
        for premise, verdict in state["verdicts"].items():
            if verdict["outcome"] != "refuted":
                continue
            refuted += 1
            evidence = verdict["evidence"]
            if not inputs_current:
                applicability = "输入变更，需复验"
            elif not self._ref_is_current(evidence):
                applicability = "证据变更或缺失，需复验"
            else:
                applicability = "适用上列输入"
            values = (premise, verdict.get("conclusion", "已证否"), applicability,
                      f"{evidence['path']} · {evidence['sha256']}", verdict.get("recheck", "重新核对绑定输入与检查输出"))
            lines.append("| " + " | ".join(map(_table_cell, values)) + " |")
        if not refuted:
            lines.extend(["", "本工作单暂无已证否前提。"])
        _save_report(self._managed(self.directory / "REJECTED.md"), lines)

        latest = next(reversed(state["rounds"]), "")
        entry = state["rounds"].get(latest, {})
        status = entry.get("status", "ready")
        if not isinstance(status, str):
            raise ValueError("轮次状态无效")
        if not inputs_current:
            status = "input_changed"
        elif status not in {"ready", "started"} and not self._ref_is_current(
                {"path": entry.get("evidence", ""), "sha256": entry.get("evidence_sha256", "")}):
            status = "evidence_changed"
        check_id = next((item["check"] for item in reversed(entry.get("events", []))
                         if isinstance(item, dict) and item.get("event") == "check.started" and item.get("check") in CHECKS), "")
        return status, latest, refuted, check_id

    def _prepare_round(self, plan_path: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
        try:
            state = self.show()
        except (OSError, ValueError) as exc:
            raise PreflightError("invalid_work_order", str(exc)) from exc
        try:
            self._verify_inputs(state["binding"])
        except (OSError, ValueError) as exc:
            raise PreflightError("input_changed", str(exc)) from exc
        try:
            plan = _read_object(self._local_file(plan_path))
            premise, _ = _check_plan(plan)
            evidence = self._managed(self.directory / "checks" / f"{premise}.json")
        except (OSError, ValueError) as exc:
            raise PreflightError("invalid_plan", str(exc)) from exc
        verdicts = state["verdicts"]
        if verdicts.get(premise, {}).get("outcome") == "refuted":
            raise PreflightError("premise_refuted", "该前提已被证否")
        if premise in state["rounds"]:
            raise PreflightError("already_registered", "该前提已登记过一轮验证，请更新前提")
        if evidence.exists() or (evidence.parent.exists() and not evidence.parent.is_dir()):
            raise PreflightError("result_path_conflict", "验证结果路径已被占用")
        if prior := plan.get("replaces"):
            previous = verdicts.get(prior, {})
            if previous.get("outcome") != "refuted":
                raise PreflightError("invalid_replacement", "replaces 须引用本工作单内带证据的已证否前提")
            if not self._ref_is_current(previous.get("evidence", {})):
                raise PreflightError("replacement_evidence_changed", "被替换前提的证据文件已变化或缺失")
        return state, plan, evidence

    def preflight(self, plan_path: str) -> dict[str, Any]:
        """只读校验，执行时仍需在工作单锁内复核。"""
        try:
            _, plan, evidence = self._prepare_round(plan_path)
        except PreflightError as exc:
            return {"work_id": self.work_id, "ready": False, "reason": exc.code, "detail": str(exc)}
        return {"work_id": self.work_id, "ready": True, "premise": plan["premise"],
                "checks": [item["id"] for item in plan["checks"]], "evidence": evidence.relative_to(self.root).as_posix()}

    def events(self, premise: str = "") -> dict[str, Any]:
        """读取一个轮次的已提交事件，旧版轮次仍可查状态和结果。"""
        rounds = self.show()["rounds"]
        selected = _identifier(premise) if premise else next(reversed(rounds), "")
        if selected and selected not in rounds:
            raise ValueError("当前工作单没有该验证轮次")
        round_data = rounds.get(selected, {})
        return {"work_id": self.work_id, "premise": selected, "status": round_data.get("status", "ready"),
                "evidence": round_data.get("evidence", ""), "events": round_data.get("events", [])}

    def run(self, plan_path: str) -> dict[str, Any]:
        with self._lock():
            state, plan, evidence = self._prepare_round(plan_path)
            premise, budget = plan["premise"], plan["budget_s"]
            results: list[dict[str, Any]] = []
            entry = {"plan": plan, "status": "started", "evidence": evidence.relative_to(self.root).as_posix(), "events": []}
            state["rounds"][premise] = entry

            def publish(event: str, **fields: Any) -> None:
                entry["events"].append({"event": event, "time": time.time(), "work_id": self.work_id,
                                        "premise": premise, **fields})
                _save_json(self.path, state)

            publish("round.started")

            started = time.monotonic()
            status = "passed"
            active_check = ""
            try:
                for check in plan["checks"]:
                    remaining = budget - (time.monotonic() - started)
                    if remaining <= 0:
                        status = "timed_out"
                        break
                    active_check = check["id"]
                    publish("check.started", check=active_check)
                    remaining = budget - (time.monotonic() - started)
                    if remaining <= 0:
                        status = "timed_out"
                        break
                    result = execute_check(self.root, check["id"], timeout=remaining)
                    results.append({"id": check["id"], **result})
                    if result["timed_out"] or time.monotonic() - started >= budget:
                        status = "timed_out"
                    elif result["exit_code"] != 0:
                        status = "failed"
                    publish("check.finished", check=active_check, status=status, exit_code=result["exit_code"])
                    active_check = ""
                    if status != "passed":
                        break
            except KeyboardInterrupt:
                status = "interrupted"
            except OSError as exc:
                status = "runner_error"
                results.append({"error": str(exc)})
            if active_check:
                publish("check.finished", check=active_check, status=status, exit_code=None)
            elapsed = round(time.monotonic() - started, 3)
            try:
                self._verify_inputs(state["binding"])
            except (OSError, ValueError) as exc:
                results.append({"binding_error": str(exc)})
                if status in {"passed", "failed"}:
                    status = "input_changed"
            _save_json(evidence, {"status": status, "elapsed_s": elapsed, "results": results})
            entry.update(status=status, elapsed_s=elapsed, evidence_sha256=self._file_ref(evidence)["sha256"])
            publish("round.finished", status=status, evidence=entry["evidence"], evidence_sha256=entry["evidence_sha256"])
            return {"premise": premise, **entry}


def write_workspace_reports(root: Path) -> dict[str, Any]:
    """从本工作区已有工作单生成交接索引。"""
    root = root.resolve()
    directory = root / WORK_DIR
    report = directory / "STATUS.md"
    if report.resolve() != report:
        raise ValueError("状态索引不能通过链接重定向")
    lines = ["# 离线验证工作单状态", "", "| 工作单 | 当前状态 | 最近轮次 | 已证否 | 最近检查 |", "|---|---|---|---|---|"]
    unreadable = []
    count = 0
    for path in sorted(directory.glob("*/STATE.json")):
        work_id = path.parent.name
        try:
            work = ProjectVerification(root, work_id)
            status, latest, refuted, check = work.write_reports(work.show())
        except (OSError, ValueError, KeyError, TypeError):
            unreadable.append(work_id)
            lines.append(f"| {_table_cell(work_id)} | 无法生成交接记录 | — | — | — |")
        else:
            lines.append(f"| [{work_id}]({work_id}/STATE.json) | {_STATUS_TEXT.get(status, '待核对')} | "
                         f"{_table_cell(latest or '—')} | [{refuted} 条]({work_id}/REJECTED.md) | {check or '—'} |")
        count += 1
    if not count:
        lines.extend(["", "当前没有验证工作单。"])
    _save_report(report, lines)
    return {"status_report": report.relative_to(root).as_posix(), "work_orders": count, "unreadable": unreadable}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("init", "show", "handoff", "record", "preflight", "run", "events"):
        commands.add_parser(name).add_argument("work_id")
    commands.add_parser("report", help="生成工作单状态索引和各工作单的证否明细")
    commands.choices["init"].add_argument("--input", action="append", required=True)
    commands.choices["record"].add_argument("premise")
    commands.choices["record"].add_argument("outcome", choices=("verified", "refuted"))
    commands.choices["record"].add_argument("--evidence", required=True)
    commands.choices["record"].add_argument("--conclusion", default="", help="简述证据确认或证否的内容")
    commands.choices["record"].add_argument("--recheck", default="", help="输入或证据变化后的复验方式")
    commands.choices["preflight"].add_argument("--plan", required=True)
    commands.choices["run"].add_argument("--plan", required=True)
    commands.choices["events"].add_argument("--premise", default="", help="指定轮次，省略时显示最近一轮")
    args = parser.parse_args()
    try:
        if args.action == "report":
            result = write_workspace_reports(PROJECT_ROOT)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return int(bool(result["unreadable"]))
        work = ProjectVerification(PROJECT_ROOT, args.work_id)
        if args.action == "init":
            work.initialize(args.input)
            result = work.handoff()
        elif args.action == "record":
            result = work.record(args.premise, args.outcome, args.evidence,
                                 conclusion=args.conclusion, recheck=args.recheck)
        elif args.action == "run":
            result = work.run(args.plan)
        elif args.action == "preflight":
            result = work.preflight(args.plan)
        elif args.action == "events":
            result = work.events(args.premise)
        else:
            result = work.show() if args.action == "show" else work.handoff()
    except PreflightError as exc:
        parser.exit(2, f"Error · {exc.code} · {exc}\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error · {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == "preflight":
        return 0 if result["ready"] else 2
    return int(args.action == "run" and result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
