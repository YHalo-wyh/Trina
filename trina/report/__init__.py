"""报告生成：Markdown + 自包含 HTML（另可导出 JSON 结构化数据）。

对外接口::

    from trina.report import build_report

    report = build_report(state, cfg, meta={"title": "..."}, audit_summary=audit.summary())
    report.save()                      # -> <cfg.report_dir>/<run_id>_report.md / .html
    report.save("/tmp/out", formats=["markdown", "html", "json"], stem="demo")
"""

from __future__ import annotations

from .builder import (
    MAX_AUDIT_ROWS,
    MAX_PATH_NODES,
    MAX_STEP_ROWS,
    Report,
    build_report,
    collect_data,
    default_cvss,
    default_cwe,
    default_remediation,
)

__all__ = [
    "Report",
    "build_report",
    "collect_data",
    "default_cvss",
    "default_cwe",
    "default_remediation",
    "MAX_PATH_NODES",
    "MAX_STEP_ROWS",
    "MAX_AUDIT_ROWS",
]
