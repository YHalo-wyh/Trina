"""检查课程工件、稳定追踪 ID、本地链接和真实测试声明，不执行目标动作。"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACK = Path("docs/course")


@dataclass
class Report:
    """结构检查结果，不能代替测试执行或人工课程验收。"""

    errors: list[str] = field(default_factory=list)
    documents: int = 0
    requirements: int = 0
    scenarios: dict[str, int] = field(default_factory=dict)
    tasks: int = 0
    test_references: int = 0
    distinct_test_references: int = 0
    local_links: int = 0
    checked_files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": not self.errors,
            "scope": "工件和引用一致性；未执行产品测试、付费模型或人工验收",
            "checked_files_hash_mode": "UTF-8 文本字节将 CRLF 规范为 LF 后计算 SHA-256；证据原件摘要另见 provenance.json",
            **self.__dict__,
        }


def _read(root: Path, relative: str, report: Report) -> str:
    """限定在选定仓库内读取，记录内容哈希。"""
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        report.errors.append(f"引用超出仓库: {relative}")
        return ""
    if not path.is_file():
        report.errors.append(f"文件不存在: {relative}")
        return ""
    raw = path.read_bytes()
    report.checked_files[relative] = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        report.errors.append(f"文本不是 UTF-8: {relative}")
        return ""


def _heading_ids(text: str, prefix: str) -> list[str]:
    return re.findall(rf"^##{{1,3}}\s+({prefix})\b", text, flags=re.MULTILINE)


def _test_declarations(root: Path, report: Report) -> set[str]:
    """静态解析测试声明，不 import 测试模块，避免读取检查产生运行副作用。"""
    result: set[str] = set()
    for path in sorted((root / "tests").glob("test_*.py")):
        relative = path.relative_to(root).as_posix()
        source = _read(root, relative, report)
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            report.errors.append(f"测试文件语法错误: {relative}:{exc.lineno}")
            continue
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef):
                for method in cls.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test_"):
                        result.add(f"{path.stem}.{cls.name}.{method.name}")
    return result


def _links(root: Path, relative: str, source: str, report: Report) -> None:
    """检查 Markdown 的本地文件引用；外部 URL 和标题锚点不发起联网。"""
    # 避免把代码示例中的 Markdown 片段当作实际链接。
    body = re.sub(r"^```.*?^```\s*$", "", source, flags=re.MULTILINE | re.DOTALL)
    for target in re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", body):
        target = target.strip().strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or not parsed.path:
            continue
        report.local_links += 1
        resolved = (root / relative).parent / unquote(parsed.path)
        resolved = resolved.resolve()
        if not resolved.is_relative_to(root):
            report.errors.append(f"链接超出仓库: {relative} -> {target}")
        elif not resolved.exists():
            report.errors.append(f"本地链接不存在: {relative} -> {target}")


def check(root: Path) -> Report:
    root = root.resolve()
    report = Report()
    raw = _read(root, (PACK / "traceability.json").as_posix(), report)
    try:
        manifest = json.loads(raw)
    except (ValueError, TypeError) as exc:
        report.errors.append(f"追踪清单无法解析: {exc}")
        return report
    if manifest.get("schema_version") != 1:
        report.errors.append("不支持的追踪清单 schema_version")
    documents = manifest.get("documents", [])
    if len(documents) != len(set(documents)):
        report.errors.append("documents 含重复项")
    required = {
        f"docs/course/{name}.md" for name in (
            "README", "intent", "problem-frame", "specification", "scenarios", "acceptance-matrix",
            "state-model", "impact-map", "plan", "tasks", "repository-map", "harness-manifest",
            "open-questions", "continuity", "ai-disclosure", "course-alignment",
        )
    }
    required.update({"README.md", "开题报告与第一次汇报.md"})
    if not required.issubset(documents):
        report.errors.append(f"清单遗漏课程工件: {sorted(required - set(documents))}")
    texts = {relative: _read(root, relative, report) for relative in documents}
    report.documents = len(texts)
    for relative, source in texts.items():
        _links(root, relative, source, report)

    spec = texts.get("docs/course/specification.md", "")
    scenes = texts.get("docs/course/scenarios.md", "")
    tasks = texts.get("docs/course/tasks.md", "")
    matrix = texts.get("docs/course/acceptance-matrix.md", "")
    questions = texts.get("docs/course/open-questions.md", "")
    harness = texts.get("docs/course/harness-manifest.md", "")
    continuity = texts.get("docs/course/continuity.md", "")
    req_ids = _heading_ids(spec, r"REQ-\d{2}")
    scene_ids = _heading_ids(scenes, r"SC-[PNA]\d{2}")
    task_ids = _heading_ids(tasks, r"TASK-\d{2}")
    for label, ids in (("REQ", req_ids), ("SC", scene_ids), ("TASK", task_ids)):
        if len(ids) != len(set(ids)):
            report.errors.append(f"{label} 正文主标题 ID 重复")
    if len(req_ids) < 6:
        report.errors.append("带 ID 的要求少于课程最小 6 条")
    for kind in ("P", "N", "A"):
        count = sum(item.startswith(f"SC-{kind}") for item in scene_ids)
        report.scenarios[kind] = count
        if count < 2:
            report.errors.append(f"{kind} 类场景不足 2 条")
    if not 5 <= len(task_ids) <= 8:
        report.errors.append("任务卡数量应为 5–8")
    report.requirements, report.tasks = len(req_ids), len(task_ids)
    if set(task_ids) != set(manifest.get("tasks", [])):
        report.errors.append("任务正文与清单不一致")
    for section in re.split(r"^## SC-[PNA]\d{2}[^\n]*", scenes, flags=re.MULTILINE)[1:]:
        if not all(keyword in section for keyword in ("Given", "When", "Then")):
            report.errors.append("存在缺少 Given/When/Then 的场景卡")
    matrix_ids = set(re.findall(r"^\| (REQ-\d{2})\b", matrix, flags=re.MULTILINE))
    if matrix_ids != set(req_ids):
        report.errors.append("验收矩阵未逐条覆盖规格要求")

    declarations = _test_declarations(root, report)
    used_tests: set[str] = set()
    linked_scenes: set[str] = set()
    linked_tasks: set[str] = set()
    rows = manifest.get("requirements", [])
    rows_ids = [row.get("id") for row in rows]
    if len(rows_ids) != len(set(rows_ids)) or set(rows_ids) != set(req_ids):
        report.errors.append("规格要求与追踪清单 ID 不一致或重复")
    commands = manifest.get("validation_commands", {})
    for row in rows:
        req = row.get("id", "<无 ID>")
        for key in ("title", "level", "scenarios", "task", "implementation", "commands", "evidence", "outcome"):
            if not row.get(key):
                report.errors.append(f"{req} 缺少追踪字段: {key}")
        if row.get("level") not in ("MUST", "SHOULD", "MAY"):
            report.errors.append(f"{req} 强度无效")
        for scene in row.get("scenarios", []):
            linked_scenes.add(scene)
            if scene not in scene_ids:
                report.errors.append(f"{req} 引用了不存在的场景: {scene}")
        task = row.get("task")
        linked_tasks.add(task)
        if task not in task_ids:
            report.errors.append(f"{req} 引用了不存在的任务: {task}")
        for command in row.get("commands", []):
            if command not in commands or f"`{commands[command]}`" not in harness:
                report.errors.append(f"{req} 验证命令未在 Harness 完整登记: {command}")
        for test in row.get("tests", []):
            report.test_references += 1
            used_tests.add(test)
            if test not in declarations:
                report.errors.append(f"{req} 测试声明不存在: {test}")
            if f"`{test}`" not in matrix:
                report.errors.append(f"{req} 测试未在矩阵中列明: {test}")
            test_file = test.split(".", 1)[0] + ".py"
            covered = False
            for command_id in row.get("commands", []):
                command_text = commands.get(command_id, "")
                if "-m unittest discover -s tests" not in command_text:
                    continue
                pattern = re.search(r"(?:^|\s)-p\s+(\S+)", command_text)
                if not pattern or fnmatch.fnmatchcase(test_file, pattern.group(1)):
                    covered = True
            if not covered:
                report.errors.append(f"{req} 登记的命令未覆盖测试模块: {test_file}")
        for issue in row.get("limitations", []):
            if issue not in questions:
                report.errors.append(f"{req} 未决问题不存在: {issue}")
        for relative in row.get("implementation", []):
            _read(root, relative, report)
        for relative in row.get("evidence", []):
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                report.errors.append(f"{req} 证据文件不存在或越界: {relative}")
    report.distinct_test_references = len(used_tests)
    if linked_scenes != set(scene_ids):
        report.errors.append(f"场景未被要求引用或无效: {sorted(set(scene_ids) ^ linked_scenes)}")
    if linked_tasks != set(task_ids):
        report.errors.append("存在未连接到要求的任务")
    for field_name in ("Current State", "Last Verified Commit", "Open Failures", "Decisions Made",
                       "Human Questions", "Next Safe Action", "Do Not Repeat"):
        if field_name not in continuity:
            report.errors.append(f"连续性交接缺字段: {field_name}")
    reviews = manifest.get("manual_reviews", [])
    if not reviews or any(item.get("status") not in ("pending", "recorded") for item in reviews):
        report.errors.append("人工审查状态缺失或无效")
    _read(root, "scripts/verify_course_pack.py", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="检查指定仓库副本，不执行其代码")
    parser.add_argument("--json", action="store_true", help="输出机器检查结果与已读取文件哈希")
    args = parser.parse_args()
    report = check(args.root)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"课程工件检查：{'通过' if not report.errors else '失败'}")
        print(f"文档 {report.documents}，要求 {report.requirements}，任务 {report.tasks}，场景 {report.scenarios}")
        print(f"测试引用 {report.test_references}（去重 {report.distinct_test_references}），本地链接 {report.local_links}")
        for error in report.errors:
            print(f"- {error}")
        print("检查范围为结构和引用；测试执行与人工验收需独立证据。")
    return 0 if not report.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
