"""评价 Agent（复核者）：只读审查一次运行的**声明与证据是否对得上**。

与产品 Agent 的权限分离（P3 要求）
--------------------------------
* 只读：读取 `state.json` / 审计 JSONL / 评测结果，**不接管扫描器、不新增工具、不放宽目标清单**；
* 只能：核对 Claim 与证据、指出覆盖缺口与边界事件、给出"建议补测的注册检查项"清单；
* 补测必须走注册检查项（由控制器批准），不能自行发明检查。

输出是一份可直接进 `review/` 的复核记录。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERDICT_ORDER = ("CONFIRMED", "NOT_CONFIRMED", "INCONCLUSIVE")


@dataclass
class ReviewNote:
    """复核结论：声明、证据、缺口、边界与建议补测。"""

    run_id: str = ""
    target: str = ""
    verdict: str = ""
    run_status: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    claims_without_evidence: list[str] = field(default_factory=list)
    verdict_conflicts: list[str] = field(default_factory=list)
    coverage_gaps: list[dict[str, Any]] = field(default_factory=list)
    boundary_events: list[dict[str, Any]] = field(default_factory=list)
    suggested_retests: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def requires_human_signoff(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "target": self.target,
            "verdict": self.verdict,
            "run_status": self.run_status,
            "findings": self.findings,
            "claims_without_evidence": self.claims_without_evidence,
            "verdict_conflicts": self.verdict_conflicts,
            "coverage_gaps": self.coverage_gaps,
            "boundary_events": self.boundary_events,
            "suggested_retests": self.suggested_retests,
            "audit": self.audit,
            "notes": self.notes,
        }

    def to_markdown(self) -> str:
        lines = [
            f"# 复核记录 · {self.run_id or '(未命名运行)'}",
            "",
            f"- 目标：`{self.target}`",
            f"- 产品 Agent 自评：状态 **{self.run_status}** · 结论 **{self.verdict or '-'}**",
            f"- 复核者：产品 Agent 之外的角色（人签署；可由 `trina review` 生成初稿）",
            "",
            "## 一、声明与证据核对",
            "",
            "| # | 漏洞类 | 位置 | 状态 | 证据 | 判定 |",
            "|---|---|---|---|---|---|",
        ]
        for index, finding in enumerate(self.findings, 1):
            mark = "✅ 有证据" if finding["has_evidence"] else "❌ 缺证据"
            lines.append(
                f"| {index} | {finding['vuln_class']} | `{finding['location'] or '-'}` | "
                f"{finding['status']} | {finding['evidence_count']} 条 | {mark} |"
            )
        if not self.findings:
            lines.append("| — | （无发现） | — | — | — | — |")
        lines += ["", "## 二、结论一致性", ""]
        if self.verdict_conflicts:
            lines += [f"- ❌ {item}" for item in self.verdict_conflicts]
        else:
            lines.append("- ✅ 结论状态与证据一致（无自相矛盾）")
        if self.claims_without_evidence:
            lines += ["", "**缺证据的声明（不能计入结论）**："]
            lines += [f"- {item}" for item in self.claims_without_evidence]
        lines += ["", "## 三、覆盖缺口（已测 / 未测 / 原因）", ""]
        if self.coverage_gaps:
            lines += ["| 未测检查项 | 工具 | 原因 |", "|---|---|---|"]
            lines += [f"| {item['test_id']} | {item.get('tool', '')} | {item.get('reason', '')} |" for item in self.coverage_gaps]
        else:
            lines.append("- 无未测项记录（或未启用注册表）")
        lines += ["", "## 四、边界事件（请求越界，执行层已阻断）", ""]
        if self.boundary_events:
            lines += ["| 工具 | 类别 | 结果 | 原因 |", "|---|---|---|---|"]
            lines += [
                f"| {item.get('tool', '')} | {item.get('kind', '')} | {item.get('result', '')} | {str(item.get('reason', ''))[:80]} |"
                for item in self.boundary_events
            ]
            lines.append("")
            lines.append("> 以上均为**请求越界**且被阻断；本系统不产生「实际越界」（执行链单点收口）。")
        else:
            lines.append("- 无越界请求")
        if self.audit:
            lines += ["", "## 五、审计与可追溯", ""]
            lines.append(f"- 审计文件：`{self.audit.get('path', '-')}`")
            lines.append(f"- 记录数：{self.audit.get('records', '-')} · 哈希链：{'完整 ✅' if self.audit.get('chain_ok') else '异常 ❌'}")
            if self.audit.get("chain_detail"):
                lines.append(f"- 链校验：{self.audit['chain_detail']}")
        lines += ["", "## 六、建议补测（走注册检查项，由控制器批准）", ""]
        if self.suggested_retests:
            lines += [f"- `{item['test_id']}`（{item.get('tool', '')}）— {item.get('reason', '')}" for item in self.suggested_retests]
        else:
            lines.append("- 无（覆盖已充分或未启用注册表）")
        lines += [
            "",
            "## 七、复核签署",
            "",
            "| 项 | 内容 |",
            "|---|---|",
            "| 复核结论 | 采信 / 部分采信 / 退回补测 |",
            "| 复核人 | （人填写：结对组/教师） |",
            "| 日期 |  |",
            "| 备注 | 如不采信，指出具体反例与最小复现 |",
            "",
            f"> {self.notes[-1] if self.notes else '复核只读证据，不接管扫描器、不新增工具、不放宽目标清单。'}",
            "",
        ]
        return "\n".join(lines)


def load_state(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def review_state(
    state: dict[str, Any],
    *,
    audit_path: str | Path | None = None,
    registry: Any | None = None,
) -> ReviewNote:
    """对一次运行做只读复核。"""
    note = ReviewNote(
        run_id=str(state.get("run_id", "")),
        target=str(state.get("target", "")),
        verdict=str(state.get("verdict", "")),
        run_status=str(state.get("status", "")),
    )
    verified = 0
    for finding in state.get("findings") or []:
        evidence = finding.get("evidence") or []
        entry = {
            "vuln_class": finding.get("vuln_class", ""),
            "location": finding.get("url") or finding.get("target") or "",
            "status": finding.get("status", ""),
            "evidence_count": len(evidence),
            "has_evidence": bool(evidence),
            "severity": finding.get("severity", ""),
        }
        note.findings.append(entry)
        if entry["status"] in ("verified", "exploited"):
            verified += 1
        if entry["status"] in ("verified", "exploited") and not entry["has_evidence"]:
            note.claims_without_evidence.append(
                f"{entry['vuln_class']} @ {entry['location']}（状态=verified 但没有任何证据条目）"
            )
    # 结论一致性：CONFIRMED 必须有已核实的发现；INCONCLUSIVE 不能同时宣称"全部安全"
    if note.verdict == "CONFIRMED" and verified == 0:
        note.verdict_conflicts.append("结论为 CONFIRMED，但没有任何 verified/exploited 发现——声明与证据不一致")
    if note.verdict == "NOT_CONFIRMED" and not (state.get("coverage") or {}).get("executed"):
        note.verdict_conflicts.append("结论为 NOT_CONFIRMED，但没有任何已执行的注册检查项——不能据此判断")

    coverage = state.get("coverage") or {}
    note.coverage_gaps = list(coverage.get("untested") or [])
    if coverage.get("stop_reason"):
        note.notes.append(f"停止原因：{coverage['stop_reason']}（未覆盖项必须写进报告）")
    note.boundary_events = list(state.get("boundary_events") or [])

    # 建议补测：未测的注册检查项（只建议，执行要过控制器）
    if registry is not None:
        for item in note.coverage_gaps:
            test = registry.tests.get(item.get("test_id", ""))
            note.suggested_retests.append(
                {
                    "test_id": item.get("test_id", ""),
                    "tool": item.get("tool") or (test.tool if test else ""),
                    "reason": item.get("reason", "") + (f"；期望发现 {test.expect_finding}" if test and test.expect_finding else ""),
                }
            )

    if audit_path:
        from .audit import AuditLog

        path = Path(audit_path)
        if path.exists():
            log = AuditLog(path, run_id=note.run_id or path.stem)
            records = log.read_all()
            log._records = len(records)
            ok, detail = log.verify()
            note.audit = {"path": str(path), "records": len(records), "chain_ok": ok, "chain_detail": detail}
        else:
            note.audit = {"path": str(path), "records": 0, "chain_ok": False, "chain_detail": "审计文件不存在"}
    return note


__all__ = ["ReviewNote", "load_state", "review_state"]
