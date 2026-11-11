"""校验 Trina 当前产品需求、机器规约、实现、测试与六类交付物的一致性。"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = Path("规约.json")
DELIVERABLE_IDS = {"D-REQ", "D-SPEC", "D-ADR", "D-HARNESS", "D-PLAN", "D-EVIDENCE"}
LEVELS = {"MUST", "SHOULD", "MAY", "PLANNED"}
STATUSES = {"implemented", "partial", "planned"}
ALLOWED_COMMANDS: dict[str, tuple[str, ...]] = {
    "V-CONTRACT": ("{python}", "-X", "utf8", "-m", "scripts.verify_contract"),
    "V-COURSE": ("{python}", "-X", "utf8", "-m", "scripts.verify_course_pack"),
    "V-TESTS": ("{python}", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests"),
    "V-DIFF": ("git", "diff", "--check"),
}


@dataclass
class ContractReport:
    """结构与可执行验证结果。"""

    errors: list[str] = field(default_factory=list)
    deliverables: int = 0
    requirements: int = 0
    rules: int = 0
    tests_referenced: int = 0
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and all(item.get("exit_code") == 0 for item in self.commands.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scope": "当前产品 TR 需求、规范规则、实现路径、测试声明、六类交付物与白名单验证命令",
            "deliverables": self.deliverables,
            "requirements": self.requirements,
            "rules": self.rules,
            "tests_referenced": self.tests_referenced,
            "errors": self.errors,
            "commands": self.commands,
        }


def load_contract(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """读取机器规约；只接受 UTF-8 JSON。"""
    return json.loads((root.resolve() / CONTRACT_PATH).read_text(encoding="utf-8"))


def _safe_path(root: Path, relative: str, report: ContractReport) -> Path | None:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        report.errors.append(f"路径越出仓库: {relative}")
        return None
    if not path.exists():
        report.errors.append(f"路径不存在: {relative}")
        return None
    return path


def _discover_tests(root: Path, report: ContractReport) -> set[str]:
    """静态发现 unittest 方法，不 import 测试代码。"""
    found: set[str] = set()
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        report.errors.append("tests 目录不存在")
        return found
    for path in sorted(tests_dir.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            report.errors.append(f"无法解析测试声明 {path.name}: {exc}")
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test_"):
                    found.add(f"tests.{path.stem}.{node.name}.{method.name}")
    return found


def _requirement_headings(source: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    pattern = re.compile(r"^### (TR-\d{2})\s+(.+?)｜(P[0-2])｜([^\r\n]+)$", re.MULTILINE)
    for identifier, title, priority, label in pattern.findall(source):
        if label.startswith("已实现"):
            status = "implemented"
        elif label.startswith("部分实现"):
            status = "partial"
        elif label.startswith("待实现"):
            status = "planned"
        else:
            status = "unknown"
        rows[identifier] = {"title": title.strip(), "priority": priority, "status": status}
    return rows


def check(root: Path = PROJECT_ROOT, *, data: dict[str, Any] | None = None) -> ContractReport:
    """执行只读一致性校验；不运行目标、模型或仓库中声明的任意命令。"""
    root = root.resolve()
    report = ContractReport()
    try:
        contract = copy.deepcopy(data) if data is not None else load_contract(root)
    except (OSError, ValueError, TypeError) as exc:
        report.errors.append(f"规约 JSON 无法读取: {exc}")
        return report

    if contract.get("schema_version") != 1:
        report.errors.append("schema_version 必须为 1")
    if contract.get("contract_id") != "trina-product-contract":
        report.errors.append("contract_id 不匹配")

    try:
        package_version = re.search(
            r'^__version__\s*=\s*"([^"]+)"',
            (root / "trina" / "__init__.py").read_text(encoding="utf-8"),
            re.MULTILINE,
        ).group(1)
    except (AttributeError, OSError):
        package_version = ""
        report.errors.append("无法读取 trina.__version__")
    if contract.get("product_version") != package_version:
        report.errors.append(f"规约版本与程序版本不一致: {contract.get('product_version')} != {package_version}")
    try:
        project_match = re.search(
            r'(?ms)^\[project\]\s*$.*?^version\s*=\s*"([^"]+)"',
            (root / "pyproject.toml").read_text(encoding="utf-8"),
        )
        project_version = project_match.group(1) if project_match else ""
    except OSError:
        project_version = ""
    if contract.get("product_version") != project_version:
        report.errors.append(f"规约版本与 pyproject 版本不一致: {contract.get('product_version')} != {project_version}")

    commands = contract.get("verification_commands")
    if not isinstance(commands, dict) or set(commands) != set(ALLOWED_COMMANDS):
        report.errors.append("verification_commands 必须完整匹配白名单命令 ID")
        commands = commands if isinstance(commands, dict) else {}
    for identifier, expected in ALLOWED_COMMANDS.items():
        item = commands.get(identifier, {})
        argv = item.get("argv") if isinstance(item, dict) else None
        if argv != list(expected):
            report.errors.append(f"{identifier} argv 与验证器白名单不一致")

    deliverables = contract.get("deliverables")
    if not isinstance(deliverables, list):
        report.errors.append("deliverables 必须为列表")
        deliverables = []
    report.deliverables = len(deliverables)
    deliverable_ids = [item.get("id") for item in deliverables if isinstance(item, dict)]
    if len(deliverable_ids) != len(set(deliverable_ids)) or set(deliverable_ids) != DELIVERABLE_IDS:
        report.errors.append("六类交付物 ID 缺失、重复或出现未知项")
    for item in deliverables:
        if not isinstance(item, dict):
            report.errors.append("交付物条目必须为对象")
            continue
        identifier = item.get("id", "<unknown>")
        if item.get("status") != "complete":
            report.errors.append(f"{identifier} 未标记 complete")
        for relative in item.get("required_paths", []):
            _safe_path(root, relative, report)
        for glob_rule in item.get("required_globs", []):
            pattern = glob_rule.get("pattern", "")
            minimum = glob_rule.get("minimum", 1)
            if not isinstance(pattern, str) or not isinstance(minimum, int) or minimum < 1:
                report.errors.append(f"{identifier} 的 glob 规则无效")
                continue
            count = sum(1 for path in root.glob(pattern) if path.is_file())
            if count < minimum:
                report.errors.append(f"{identifier} 的 {pattern} 仅 {count} 项，要求至少 {minimum} 项")

    requirements_source = (root / "需求.md").read_text(encoding="utf-8-sig")
    specification_source = (root / "规约.md").read_text(encoding="utf-8-sig")
    headings = _requirement_headings(requirements_source)
    requirements = contract.get("requirements")
    if not isinstance(requirements, list):
        report.errors.append("requirements 必须为列表")
        requirements = []
    report.requirements = len(requirements)
    identifiers = [item.get("id") for item in requirements if isinstance(item, dict)]
    if len(identifiers) != len(set(identifiers)):
        report.errors.append("机器规约中的 TR ID 重复")
    if set(identifiers) != set(headings):
        report.errors.append(f"需求与机器规约 TR 集合不一致: {sorted(set(identifiers) ^ set(headings))}")

    declared_tests = _discover_tests(root, report)
    all_rule_ids: list[str] = []
    referenced_tests: set[str] = set()
    for item in requirements:
        if not isinstance(item, dict):
            report.errors.append("需求条目必须为对象")
            continue
        identifier = str(item.get("id", "<unknown>"))
        heading = headings.get(identifier, {})
        for field_name in ("title", "priority", "status"):
            if item.get(field_name) != heading.get(field_name):
                report.errors.append(f"{identifier} 的 {field_name} 与需求文档不一致")
        status = item.get("status")
        if status not in STATUSES:
            report.errors.append(f"{identifier} 状态无效: {status}")
        for relative in item.get("implementation", []):
            _safe_path(root, relative, report)
        verification = item.get("verification", [])
        if not verification or any(command not in ALLOWED_COMMANDS for command in verification):
            report.errors.append(f"{identifier} 缺少有效验证命令")
        rules = item.get("rules")
        if not isinstance(rules, list) or not rules:
            report.errors.append(f"{identifier} 没有规范规则")
            continue
        if status == "partial" and not item.get("limitations"):
            report.errors.append(f"{identifier} 为 partial 但没有 limitations")
        if status == "planned" and not item.get("limitations"):
            report.errors.append(f"{identifier} 为 planned 但没有 limitations")
        if status in {"implemented", "partial"} and not any(rule.get("level") == "MUST" for rule in rules):
            report.errors.append(f"{identifier} 没有 MUST 规则")
        for rule in rules:
            if not isinstance(rule, dict):
                report.errors.append(f"{identifier} 规则必须为对象")
                continue
            rule_id = str(rule.get("id", ""))
            all_rule_ids.append(rule_id)
            if not re.fullmatch(rf"{re.escape(identifier)}-R\d{{2}}", rule_id):
                report.errors.append(f"{identifier} 规则 ID 非法: {rule_id}")
            level = rule.get("level")
            if level not in LEVELS:
                report.errors.append(f"{rule_id} 强度无效: {level}")
            if status == "planned" and level != "PLANNED":
                report.errors.append(f"{rule_id} 属于待实现需求，只能标记 PLANNED")
            if not str(rule.get("statement", "")).strip():
                report.errors.append(f"{rule_id} 缺少可判定陈述")
            tests = rule.get("tests", [])
            if level != "PLANNED" and not tests:
                report.errors.append(f"{rule_id} 缺少测试引用")
            for test in tests:
                referenced_tests.add(test)
                if test not in declared_tests:
                    report.errors.append(f"{rule_id} 引用不存在的测试: {test}")
            if specification_source.count(f"`{rule_id}`") != 1:
                report.errors.append(f"{rule_id} 必须在规约.md 的机器索引中恰好出现一次")

    report.rules = len(all_rule_ids)
    report.tests_referenced = len(referenced_tests)
    if len(all_rule_ids) != len(set(all_rule_ids)):
        report.errors.append("规则 ID 重复")
    return report


def execute_whitelisted(root: Path, report: ContractReport) -> None:
    """只执行代码内固定的验证命令，不执行 JSON 中的任意字符串。"""
    for identifier in ("V-COURSE", "V-TESTS", "V-DIFF"):
        argv = [sys.executable if part == "{python}" else part for part in ALLOWED_COMMANDS[identifier]]
        completed = subprocess.run(argv, cwd=root, text=True, capture_output=True, encoding="utf-8", errors="replace")
        output = (completed.stdout + completed.stderr).strip()
        report.commands[identifier] = {
            "argv": argv,
            "exit_code": completed.returncode,
            "output_tail": output[-4000:],
        }
        if completed.returncode != 0:
            report.errors.append(f"{identifier} 执行失败，退出码 {completed.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="待检查仓库")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    parser.add_argument("--execute", action="store_true", help="额外执行课程检查、全量测试和 diff 检查")
    args = parser.parse_args()
    report = check(args.root)
    if args.execute and not report.errors:
        execute_whitelisted(args.root.resolve(), report)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Trina 机器规约：{'通过' if report.ok else '失败'}")
        print(
            f"交付物 {report.deliverables}/6，需求 {report.requirements}，"
            f"规则 {report.rules}，测试引用 {report.tests_referenced}"
        )
        for error in report.errors:
            print(f"- {error}")
        for identifier, result in report.commands.items():
            print(f"- {identifier}: exit={result['exit_code']}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
