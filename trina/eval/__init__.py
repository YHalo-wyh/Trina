"""Trina 评测框架：场景库、指标聚合、蓝队对抗。

* ``trina.eval.metrics``     —— ground truth 匹配口径 + 指标聚合 + 中文 Markdown 呈现
* ``trina.eval.harness``     —— 场景加载 / 执行编排（runner 可注入，便于离线测试）
* ``trina.eval.adversarial`` —— 蓝队对抗代理（WAF / 封禁 / 限速 / 篡改 / 蜜罐）
* ``trina/eval/scenarios/``  —— 场景库（web / host / integrated）
"""

from __future__ import annotations

from .adversarial import AdversarialProxy
from .harness import EvalHarness, ScenarioSpec
from .metrics import (
    GtMatch,
    ScenarioResult,
    compute_metrics,
    finding_matches_gt,
    format_metrics_markdown,
    match_findings,
    metrics_to_json,
    normalize_vuln_class,
)

__all__ = [
    "AdversarialProxy",
    "EvalHarness",
    "GtMatch",
    "ScenarioResult",
    "ScenarioSpec",
    "compute_metrics",
    "finding_matches_gt",
    "format_metrics_markdown",
    "match_findings",
    "metrics_to_json",
    "normalize_vuln_class",
]
