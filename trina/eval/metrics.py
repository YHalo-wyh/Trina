"""评测指标：ground truth 匹配口径 + 聚合统计 + 中文 Markdown 呈现。

设计要点:
* **匹配口径唯一**：所有 TP/FP/FN 判定都走 :func:`match_findings`，避免人工漂移；
* ``gt_hit`` / ``gt_miss`` 的元素格式为 ``"<归一化漏洞类>@<gt_id>"``，
  这样 :func:`compute_metrics` 只需 ``list[ScenarioResult]`` 即可按漏洞类分组；
* 精确率/召回率/误报率/遗漏率等口径与 ``docs/PLAN.md §6`` 一致。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 漏洞类归一化
# ---------------------------------------------------------------------------

#: 各类别名 → 规范类名（比较前先做小写/分隔符归一）。
VULN_CLASS_ALIASES: dict[str, str] = {
    # SQL 注入
    "sql": "sqli",
    "sqli": "sqli",
    "sql_injection": "sqli",
    "sqlinjection": "sqli",
    "sql_inject": "sqli",
    "blind_sqli": "sqli",
    "error_based_sqli": "sqli",
    "boolean_based_sqli": "sqli",
    "time_based_sqli": "sqli",
    # 命令注入 / RCE
    "rce": "rce",
    "cmd_inject": "rce",
    "cmd_injection": "rce",
    "command_injection": "rce",
    "command_execution": "rce",
    "os_command_injection": "rce",
    "code_execution": "rce",
    # 文件包含 / 路径穿越
    "lfi": "lfi",
    "rfi": "lfi",
    "file_inclusion": "lfi",
    "local_file_inclusion": "lfi",
    "path_traversal": "lfi",
    "directory_traversal": "lfi",
    "file_traversal": "lfi",
    # 目录列举
    "dir_listing": "directory_listing",
    "directory_listing": "directory_listing",
    "dir_browsing": "directory_listing",
    "directory_browsing": "directory_listing",
    # 文件上传
    "upload": "unrestricted_file_upload",
    "file_upload": "unrestricted_file_upload",
    "arbitrary_file_upload": "unrestricted_file_upload",
    "unrestricted_upload": "unrestricted_file_upload",
    "unrestricted_file_upload": "unrestricted_file_upload",
    # 弱口令 / 凭据
    "weak_password": "weak_password",
    "weakpass": "weak_password",
    "weak_credential": "weak_password",
    "weak_credentials": "weak_password",
    "default_credential": "weak_password",
    "default_credentials": "weak_password",
    "brute_force": "weak_password",
    "bruteforce": "weak_password",
    "ssh_weak_password": "weak_password",
    # 信息泄露
    "info_leak": "information_disclosure",
    "info_disclosure": "information_disclosure",
    "information_disclosure": "information_disclosure",
    "information_leak": "information_disclosure",
    "sensitive_data_exposure": "information_disclosure",
    "source_leak": "information_disclosure",
    # 访问控制
    "idor": "idor",
    "access_control": "idor",
    "broken_access_control": "idor",
    "authorization_bypass": "idor",
    "unauthorized_access": "idor",
    # 其他
    "xss": "xss",
    "cross_site_scripting": "xss",
    "reflected_xss": "xss",
    "stored_xss": "xss",
    "ssrf": "ssrf",
    "csrf": "csrf",
    "xxe": "xxe",
    "ssti": "ssti",
    "open_redirect": "open_redirect",
    "open_port": "open_port",
    "port_scan": "open_port",
    "service": "service_fingerprint",
    "service_fingerprint": "service_fingerprint",
    "service_identification": "service_fingerprint",
    "banner": "service_fingerprint",
    "missing_security_headers": "missing_security_headers",
    "security_headers": "missing_security_headers",
}


def _norm_key(value: str) -> str:
    """小写 + 统一分隔符（空白/连字符/多点 → 单下划线）。"""
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s\-.:/]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def normalize_vuln_class(value: str) -> str:
    """把漏洞类名归一化到规范名（如 ``SQL Injection`` → ``sqli``）。"""
    key = _norm_key(value)
    return VULN_CLASS_ALIASES.get(key, key)


# ---------------------------------------------------------------------------
# ScenarioResult
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    """单个场景的评测结果。"""

    scenario_id: str = ""
    kind: str = ""
    target: str = ""
    status: str = "failed"  # success | partial | failed | error | blocked
    duration: float = 0.0
    findings: list[dict[str, Any]] = field(default_factory=list)
    gt_total: int = 0
    gt_hit: list[str] = field(default_factory=list)  # 元素: "<class>@<gt_id>"
    gt_miss: list[str] = field(default_factory=list)  # 元素: "<class>@<gt_id>"
    fp_count: int = 0
    #: 场景评分范围之外的真实发现（不计入误报，单独展示）
    out_of_scope_count: int = 0
    flags: list[str] = field(default_factory=list)
    ports_found: int = 0
    ports_expected: int = 0
    services_correct: int = 0
    services_expected: int = 0
    steps: int = 0
    denied: int = 0
    error: str = ""
    #: 成本与效率指标（文档建议的内部指标）
    cost_usd: float = 0.0
    first_evidence_seconds: float | None = None
    redundant_rate: float = 0.0
    failures: int = 0
    #: 假设驱动决策的验收口径：解决了多少条假设、调度被遵从了多少次
    hypotheses_total: int = 0
    hypotheses_resolved: int = 0
    scheduler_compliance: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            "duration": round(self.duration, 3),
            "findings": self.findings,
            "gt_total": self.gt_total,
            "gt_hit": self.gt_hit,
            "gt_miss": self.gt_miss,
            "fp_count": self.fp_count,
            "out_of_scope_count": self.out_of_scope_count,
            "flags": self.flags,
            "ports_found": self.ports_found,
            "ports_expected": self.ports_expected,
            "services_correct": self.services_correct,
            "services_expected": self.services_expected,
            "steps": self.steps,
            "denied": self.denied,
            "error": self.error,
            "cost_usd": round(self.cost_usd, 6),
            "first_evidence_seconds": self.first_evidence_seconds,
            "redundant_rate": self.redundant_rate,
            "failures": self.failures,
            "hypotheses_total": self.hypotheses_total,
            "hypotheses_resolved": self.hypotheses_resolved,
            "scheduler_compliance": self.scheduler_compliance,
        }


# ---------------------------------------------------------------------------
# ground truth 匹配
# ---------------------------------------------------------------------------


@dataclass
class GtMatch:
    """匹配结果。``gt_hit``/``gt_miss`` 元素为 ``"<class>@<gt_id>"``。

    ``fp`` 仅统计**评分范围内**未命中任何 ground truth 的发现；
    评分范围之外的发现进入 ``out_of_scope``，不计入误报。
    """

    gt_hit: list[str] = field(default_factory=list)
    gt_miss: list[str] = field(default_factory=list)
    fp_count: int = 0
    fp_indices: list[int] = field(default_factory=list)
    out_of_scope_indices: list[int] = field(default_factory=list)
    matched_indices: list[int] = field(default_factory=list)

    @property
    def tp(self) -> int:
        return len(self.gt_hit)

    @property
    def fn(self) -> int:
        return len(self.gt_miss)

    @property
    def out_of_scope_count(self) -> int:
        return len(self.out_of_scope_indices)


def gt_identifier(gt: dict[str, Any], index: int = 0) -> str:
    """取 ground truth 的稳定标识（缺省回退到序号）。"""
    return str(gt.get("id") or gt.get("name") or f"gt-{index + 1}")


def gt_key(gt: dict[str, Any], index: int = 0) -> str:
    """``"<归一化漏洞类>@<gt_id>"``。"""
    return f"{normalize_vuln_class(gt.get('vuln_class', ''))}@{gt_identifier(gt, index)}"


#: 基础设施类发现：由覆盖率/准确率指标衡量，命中不到 GT 不算误报
INFRA_CLASSES = {"open_port", "service_fingerprint"}


def _norm_url(value: Any) -> str:
    """去掉 scheme 并统一大小写/结尾斜杠，便于跨写法比较（`ssh://host:22` vs `host:22`）。"""
    text = str(value or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text)
    return text.rstrip("/")


def finding_matches_gt(finding: dict[str, Any], gt: dict[str, Any]) -> bool:
    """命中判据：漏洞类归一化后相同，且 GT 中**声明了**的约束必须全部满足。

    * 声明了 `url` → finding 的 url 必须等于它或以它结尾（路径写法 `/sqli` 也可以）；
    * 声明了 `parameter` → finding 的 parameter 必须一致；
    * 两者都没声明 → 同类即命中。

    早期实现把这几条写成 OR，导致「GT 只写了 url」时任意同类 finding 都会命中（漏报被掩盖）。
    """
    if normalize_vuln_class(finding.get("vuln_class", "")) != normalize_vuln_class(gt.get("vuln_class", "")):
        return False
    gt_url = _norm_url(gt.get("url"))
    if gt_url:
        f_url = _norm_url(finding.get("url") or finding.get("target"))
        if not (f_url == gt_url or f_url.endswith(gt_url)):
            return False
    gt_param = str(gt.get("parameter") or "").strip()
    if gt_param and str(finding.get("parameter") or "").strip() != gt_param:
        return False
    return True


def scope_classes(scoped_classes: list[str] | None, ground_truth: list[dict[str, Any]] | None) -> set[str]:
    """评分范围：显式 ``scoped_classes`` 优先，否则取 ground truth 的漏洞类集合。"""
    if scoped_classes:
        return {normalize_vuln_class(c) for c in scoped_classes if str(c).strip()}
    return {normalize_vuln_class(gt.get("vuln_class", "")) for gt in (ground_truth or [])}


def match_findings(
    findings: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    scoped_classes: list[str] | None = None,
) -> GtMatch:
    """把 findings 与 ground truth 做匹配，产出 TP/FN/FP/范围外。

    * 命中任一 gt 的 finding → 计入 TP（不判 FP）；
    * 未命中的 finding：漏洞类在**评分范围**内 → FP；范围外 → out_of_scope（不计误报）；
    * 场景**没有声明任何评分依据**（既无 GT 也无 scoped_classes，例如纯夺旗场景）时，
      所有 finding 一律视为范围外——没有依据就谈不上"误报"。
    """
    findings = list(findings or [])
    ground_truth = list(ground_truth or [])
    scope = scope_classes(scoped_classes, ground_truth) if ground_truth or scoped_classes else None

    result = GtMatch()
    matched: set[int] = set()
    for index, gt in enumerate(ground_truth):
        key = gt_key(gt, index)
        hit = False
        for fidx, finding in enumerate(findings):
            if finding_matches_gt(finding, gt):
                hit = True
                matched.add(fidx)
        if hit:
            result.gt_hit.append(key)
        else:
            result.gt_miss.append(key)

    result.matched_indices = sorted(matched)
    fp: list[int] = []
    out_of_scope: list[int] = []
    for fidx, finding in enumerate(findings):
        if fidx in matched:
            continue
        cls = normalize_vuln_class(finding.get("vuln_class", ""))
        if scope is None or (scope and cls not in scope):
            out_of_scope.append(fidx)
        elif cls in INFRA_CLASSES:
            # 扫描类发现（开放端口/服务指纹）超出 GT 不是误报——那是**额外覆盖**，
            # 由「端口覆盖率 / 服务识别准确率」两个指标衡量，而不是 precision。
            out_of_scope.append(fidx)
        else:
            fp.append(fidx)
    result.fp_indices = fp
    result.fp_count = len(fp)
    result.out_of_scope_indices = out_of_scope
    return result


def _class_of(key: str) -> str:
    return key.split("@", 1)[0] if "@" in key else normalize_vuln_class(key)


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    pos = (len(ordered) - 1) * p
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return round(ordered[int(pos)], 3)
    return round(ordered[low] * (high - pos) + ordered[high] * (pos - low), 3)


def _ratio(num: float, den: float) -> float:
    return round(num / den, 4) if den else 0.0


def compute_metrics(results: list[ScenarioResult]) -> dict[str, Any]:
    """把逐场景结果聚合成总览指标。"""
    results = list(results or [])
    total = len(results)
    passed = sum(1 for r in results if r.status == "success")
    durations = [float(r.duration or 0.0) for r in results]

    # ---- 按 kind 分组
    by_kind: dict[str, dict[str, Any]] = {}
    for r in results:
        bucket = by_kind.setdefault(r.kind or "unknown", {"total": 0, "passed": 0, "duration_sum": 0.0})
        bucket["total"] += 1
        bucket["passed"] += 1 if r.status == "success" else 0
        bucket["duration_sum"] += float(r.duration or 0.0)
    for bucket in by_kind.values():
        bucket["pass_rate"] = _ratio(bucket["passed"], bucket["total"])
        bucket["avg_duration"] = round(bucket.pop("duration_sum") / bucket["total"], 3) if bucket["total"] else 0.0

    # ---- 全局 TP / FN / FP
    tp = sum(len(r.gt_hit) for r in results)
    fn = sum(len(r.gt_miss) for r in results)
    fp = sum(int(r.fp_count) for r in results)

    ports_found = sum(int(r.ports_found) for r in results)
    ports_expected = sum(int(r.ports_expected) for r in results)
    services_correct = sum(int(r.services_correct) for r in results)
    services_expected = sum(int(r.services_expected) for r in results)
    flag_results = sum(1 for r in results if r.flags)

    # ---- 按漏洞类分组（fp 只统计评分范围内未命中的发现）
    by_vuln_class: dict[str, dict[str, Any]] = {}
    out_of_scope_by_class: dict[str, int] = {}
    for r in results:
        gt_classes = {_class_of(k) for k in list(r.gt_hit) + list(r.gt_miss)}
        hit_classes = {_class_of(k) for k in r.gt_hit}
        for key in r.gt_hit:
            by_vuln_class.setdefault(_class_of(key), {"tp": 0, "fn": 0, "fp": 0})["tp"] += 1
        for key in r.gt_miss:
            by_vuln_class.setdefault(_class_of(key), {"tp": 0, "fn": 0, "fp": 0})["fn"] += 1
        for finding in r.findings or []:
            cls = normalize_vuln_class(finding.get("vuln_class", ""))
            if not cls:
                continue
            if cls in gt_classes:
                if cls not in hit_classes:  # 范围内但没有命中任何 gt → 真误报
                    by_vuln_class.setdefault(cls, {"tp": 0, "fn": 0, "fp": 0})["fp"] += 1
            else:  # 范围外发现：不计误报，单独统计
                out_of_scope_by_class[cls] = out_of_scope_by_class.get(cls, 0) + 1
    for stats in by_vuln_class.values():
        stats["pass_rate"] = _ratio(stats["tp"], stats["tp"] + stats["fn"])

    metrics: dict[str, Any] = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": _ratio(passed, total),
        "by_kind": by_kind,
        "avg_duration": round(sum(durations) / total, 3) if total else 0.0,
        "p50_duration": _percentile(durations, 0.5),
        "p90_duration": _percentile(durations, 0.9),
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "out_of_scope_total": sum(int(r.out_of_scope_count) for r in results),
        "out_of_scope_by_class": out_of_scope_by_class,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "false_positive_rate": _ratio(fp, tp + fp),
        "miss_rate": _ratio(fn, tp + fn),
        "port_coverage": _ratio(ports_found, ports_expected),
        "service_accuracy": _ratio(services_correct, services_expected),
        "flag_rate": _ratio(flag_results, total),
        "denied_total": sum(int(r.denied) for r in results),
        # ---- 文档建议的内部指标（不只看总分）
        "cost_usd_total": round(sum(float(r.cost_usd or 0.0) for r in results), 6),
        "cost_per_success": round(
            (sum(float(r.cost_usd or 0.0) for r in results) / passed), 6
        ) if passed else 0.0,
        "time_to_evidence_avg": round(
            sum(float(r.first_evidence_seconds) for r in results if r.first_evidence_seconds is not None)
            / max(1, len([r for r in results if r.first_evidence_seconds is not None])),
            3,
        ) if any(r.first_evidence_seconds is not None for r in results) else None,
        "redundant_action_rate": round(
            sum(float(r.redundant_rate or 0.0) for r in results) / total, 4
        ) if total else 0.0,
        "recovery_rate": _ratio(
            len([r for r in results if r.failures and r.status == "success"]),
            len([r for r in results if r.failures]),
        ),
        "failures_total": sum(int(r.failures or 0) for r in results),
        # ---- 假设驱动决策的验收口径
        "hypothesis_resolution_rate": _ratio(
            sum(int(r.hypotheses_resolved or 0) for r in results),
            sum(int(r.hypotheses_total or 0) for r in results),
        ),
        "hypotheses_total": sum(int(r.hypotheses_total or 0) for r in results),
        "scheduler_compliance_rate": round(
            sum(float(r.scheduler_compliance) for r in results if r.scheduler_compliance is not None)
            / max(1, len([r for r in results if r.scheduler_compliance is not None])),
            4,
        ) if any(r.scheduler_compliance is not None for r in results) else None,
        "ports_found": ports_found,
        "ports_expected": ports_expected,
        "services_correct": services_correct,
        "services_expected": services_expected,
        "by_vuln_class": by_vuln_class,
    }
    return metrics


# ---------------------------------------------------------------------------
# 呈现
# ---------------------------------------------------------------------------

#: 工程参考线（非官方评级，见 PLAN §6.3）。
REFERENCE_BANDS = [
    (90.0, "领先", "≥90%"),
    (80.0, "进阶", "80–90%"),
    (70.0, "中等", "70–80%"),
    (50.0, "基础可用", "50–70%"),
    (0.0, "入门", "<50%"),
]


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def band_for(rate: float) -> str:
    """把通过率（0–1）映射到参考档位。"""
    score = rate * 100.0
    for threshold, label, _rng in REFERENCE_BANDS:
        if score >= threshold:
            return label
    return "入门"


def format_metrics_markdown(metrics: dict[str, Any], title: str = "评测结果") -> str:
    """把指标 dict 渲染成中文 Markdown（含与行业参考线对比行）。"""
    lines: list[str] = [f"## {title}", ""]

    band = band_for(metrics.get("pass_rate", 0.0))
    lines.append("### 总体指标")
    lines.append("")
    lines.append("| 指标 | 数值 | 行业参考线（工程口径） |")
    lines.append("|---|---|---|")
    lines.append(f"| 场景数 | {metrics.get('total', 0)} | — |")
    lines.append(f"| 通过 / 失败 | {metrics.get('passed', 0)} / {metrics.get('failed', 0)} | — |")
    lines.append(f"| 通过率 | {_pct(metrics.get('pass_rate', 0.0))} | 70–80% 中等；≥80% 进阶 |")
    lines.append(f"| 平均耗时 | {metrics.get('avg_duration', 0.0)}s | — |")
    lines.append(f"| P50 耗时 | {metrics.get('p50_duration', 0.0)}s | — |")
    lines.append(f"| P90 耗时 | {metrics.get('p90_duration', 0.0)}s | — |")
    lines.append(f"| 精确率 precision = TP/(TP+FP) | {_pct(metrics.get('precision', 0.0))} | 目标 ≥ 85% |")
    lines.append(f"| 召回率 recall = TP/(TP+FN) | {_pct(metrics.get('recall', 0.0))} | 目标 ≥ 70% |")
    lines.append(f"| 误报率 FPR = FP/(TP+FP) | {_pct(metrics.get('false_positive_rate', 0.0))} | 目标 ≤ 15% |")
    lines.append(f"| 遗漏率 = FN/(TP+FN) | {_pct(metrics.get('miss_rate', 0.0))} | 目标 ≤ 30% |")
    lines.append(f"| 范围外发现（不计误报） | {metrics.get('out_of_scope_total', 0)} | 场景评分范围外的真实发现 |")
    lines.append(f"| TP / FP / FN | {metrics.get('tp', 0)} / {metrics.get('fp', 0)} / {metrics.get('fn', 0)} | — |")
    lines.append(
        f"| 端口覆盖率 | {_pct(metrics.get('port_coverage', 0.0))} "
        f"({metrics.get('ports_found', 0)}/{metrics.get('ports_expected', 0)}) | 目标 ≥ 90% |"
    )
    lines.append(
        f"| 服务识别准确率 | {_pct(metrics.get('service_accuracy', 0.0))} "
        f"({metrics.get('services_correct', 0)}/{metrics.get('services_expected', 0)}) | 目标 ≥ 80% |"
    )
    lines.append(f"| flag 获取率 | {_pct(metrics.get('flag_rate', 0.0))} | 目标 ≥ 60% |")
    lines.append(f"| 被拒动作数 | {metrics.get('denied_total', 0)} | — |")
    lines.append("")
    lines.append(
        f"参考线口径（工程参考，非官方评级）：<50 入门；50–70 基础可用；70–80 中等；80+ 进阶；90+ 领先。"
        f"当前通过率 {_pct(metrics.get('pass_rate', 0.0))} → **{band}**。"
    )
    lines.append("")

    # ---- 按 kind
    by_kind = metrics.get("by_kind", {}) or {}
    if by_kind:
        lines.append("### 按场景类型")
        lines.append("")
        lines.append("| kind | 场景数 | 通过率 | 平均耗时 |")
        lines.append("|---|---|---|---|")
        for kind, stats in sorted(by_kind.items()):
            lines.append(
                f"| {kind} | {stats.get('total', 0)} | {_pct(stats.get('pass_rate', 0.0))} | {stats.get('avg_duration', 0.0)}s |"
            )
        lines.append("")

    # ---- 按漏洞类
    by_vuln = metrics.get("by_vuln_class", {}) or {}
    if by_vuln:
        lines.append("### 按漏洞类")
        lines.append("")
        lines.append("| 漏洞类 | TP | FN | FP | 检出率 TP/(TP+FN) |")
        lines.append("|---|---|---|---|---|")
        for cls, stats in sorted(by_vuln.items()):
            lines.append(
                f"| {cls} | {stats.get('tp', 0)} | {stats.get('fn', 0)} | {stats.get('fp', 0)} | "
                f"{_pct(stats.get('pass_rate', 0.0))} |"
            )
        lines.append("")

    # ---- 范围外发现（不计误报）
    oos = metrics.get("out_of_scope_by_class", {}) or {}
    if oos:
        lines.append("### 范围外发现（不计误报）")
        lines.append("")
        lines.append("| 漏洞类 | 条数 |")
        lines.append("|---|---|")
        for cls, count in sorted(oos.items()):
            lines.append(f"| {cls} | {count} |")
        lines.append(f"| **合计** | **{metrics.get('out_of_scope_total', 0)}** |")
        lines.append("")

    # ---- 明细
    details = metrics.get("results") or []
    if details:
        lines.append("### 逐场景明细")
        lines.append("")
        lines.append("| 场景 | kind | 状态 | 耗时 | GT命中 | GT遗漏 | FP | 范围外 | flags | 备注 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for item in details:
            note = str(item.get("error", "") or "").replace("|", "/")[:60]
            lines.append(
                f"| {item.get('scenario_id', '')} | {item.get('kind', '')} | {item.get('status', '')} | "
                f"{round(float(item.get('duration', 0.0)), 1)}s | {len(item.get('gt_hit', []))}/{item.get('gt_total', 0)} | "
                f"{len(item.get('gt_miss', []))} | {item.get('fp_count', 0)} | {item.get('out_of_scope_count', 0)} | "
                f"{len(item.get('flags', []))} | {note} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def metrics_to_json(metrics: dict[str, Any]) -> str:
    return json.dumps(metrics, ensure_ascii=False, indent=2)


__all__ = [
    "GtMatch",
    "REFERENCE_BANDS",
    "ScenarioResult",
    "VULN_CLASS_ALIASES",
    "band_for",
    "compute_metrics",
    "finding_matches_gt",
    "format_metrics_markdown",
    "gt_identifier",
    "gt_key",
    "match_findings",
    "metrics_to_json",
    "normalize_vuln_class",
]
