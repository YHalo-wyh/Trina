"""HTML 渲染：自包含单文件报告（内联 CSS + 内联 SVG 条形图，零外部资源、零脚本）。

与 `markdown.py` 共用 `builder.collect_data()` 的结构化数据，因此两者内容一致；
HTML 额外提供严重度分布图（内联 SVG，不使用任何图表库）与 mermaid 源码块。
"""

from __future__ import annotations

from html import escape
from typing import Any

from ..state import SEVERITIES

#: 严重度色板（SVG 填充 + 徽标）。
SEVERITY_COLORS = {
    "critical": "#b91c1c",
    "high": "#dc6803",
    "medium": "#b7791f",
    "low": "#2f7d32",
    "info": "#3c6e9f",
}

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 14px 56px; background: #f4f5f8; color: #1f2430;
  font-family: "Segoe UI", "Microsoft YaHei", -apple-system, "Helvetica Neue", Arial, sans-serif;
  font-size: 14px; line-height: 1.65; }
main.report { max-width: 1000px; margin: 0 auto; background: #ffffff; padding: 36px 44px 48px;
  border: 1px solid #e3e6ec; border-radius: 8px; }
h1 { margin: 0 0 6px; font-size: 25px; line-height: 1.3; }
h2 { margin: 34px 0 12px; padding-bottom: 6px; font-size: 19px; border-bottom: 2px solid #e3e6ec; }
h3 { margin: 26px 0 8px; font-size: 16px; }
h4 { margin: 16px 0 4px; font-size: 14.5px; }
p { margin: 8px 0; }
.meta { color: #5b6472; font-size: 12.5px; }
table { width: 100%; margin: 10px 0 14px; border-collapse: collapse; }
th, td { border: 1px solid #dfe3ea; padding: 6px 10px; text-align: left; vertical-align: top; word-break: break-word; }
th { background: #f0f3f8; font-weight: 600; }
td.num, th.num { text-align: right; }
code { background: #f3f4f7; padding: 1px 4px; border-radius: 3px; font-size: 12.5px;
  font-family: Consolas, "Courier New", monospace; word-break: break-all; }
pre { margin: 10px 0; padding: 10px 12px; background: #f8f9fb; border: 1px solid #e3e6ec;
  border-radius: 5px; overflow-x: auto; white-space: pre-wrap; word-break: break-all;
  font-family: Consolas, "Courier New", monospace; font-size: 12.5px; }
pre.mermaid-src { background: #fffdf4; border-color: #e5d9a8; }
.empty { color: #77808f; }
.warn { margin: 10px 0; padding: 8px 12px; background: #fff6e6; border-left: 4px solid #dc6803; }
.sev { display: inline-block; padding: 0 7px; border-radius: 9px; color: #ffffff; font-size: 12px; }
.sev-critical { background: #b91c1c; }
.sev-high { background: #dc6803; }
.sev-medium { background: #b7791f; }
.sev-low { background: #2f7d32; }
.sev-info { background: #3c6e9f; }
.chart-wrap { margin: 14px 0 6px; padding: 12px; border: 1px solid #e3e6ec; border-radius: 6px; background: #fbfcfe; }
.chart { width: 100%; max-width: 680px; height: auto; display: block; }
.chart-label { font-size: 12px; fill: #3b4252; }
.chart-value { font-size: 12px; fill: #1f2430; }
.chart-track { fill: #eceff4; }
.chart-bar.s-critical { fill: #b91c1c; }
.chart-bar.s-high { fill: #dc6803; }
.chart-bar.s-medium { fill: #b7791f; }
.chart-bar.s-low { fill: #2f7d32; }
.chart-bar.s-info { fill: #3c6e9f; }
figcaption { margin-top: 8px; color: #5b6472; font-size: 12.5px; }
ol.evidence { margin: 8px 0 12px; padding-left: 22px; }
ol.evidence > li { margin-bottom: 12px; }
.ev-head { margin-bottom: 4px; }
.chip { display: inline-block; padding: 0 6px; border-radius: 8px; background: #eef1f6;
  border: 1px solid #dfe3ea; font-size: 12px; }
.ev-target { font-size: 12.5px; color: #3b4252; }
.ev-clock { color: #77808f; font-size: 12px; }
.ev-line { margin: 2px 0; }
.ev-key { display: inline-block; min-width: 42px; margin-right: 6px; color: #5b6472; font-size: 12.5px; }
footer { margin-top: 28px; padding-top: 10px; border-top: 1px solid #e3e6ec; color: #77808f; font-size: 12px; }
"""


def esc(value: Any) -> str:
    """HTML 转义（所有动态内容必须经过这里）。"""
    return escape("" if value is None else str(value), quote=True)


def _sev_class(severity: Any) -> str:
    key = str(severity)
    return key if key in SEVERITIES else "info"


#: 数值列的对齐类名。
_NUM_CLASS = ' class="num"'


def _table(headers: list[str], rows: list[list[Any]], numeric: list[int] | None = None) -> str:
    numeric = list(numeric or [])
    head = "".join(
        f"<th{_NUM_CLASS if index in numeric else ''}>{esc(name)}</th>"
        for index, name in enumerate(headers)
    )
    body: list[str] = []
    for row in rows:
        cells = "".join(
            f"<td{_NUM_CLASS if index in numeric else ''}>{esc(cell)}</td>"
            for index, cell in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _kv(rows: list[list[Any]]) -> str:
    return _table(["项目", "内容"], rows)


def _severity_chart(rows: list[dict[str, Any]]) -> str:
    """内联 SVG 条形图（不依赖任何外部图表库）。"""
    bar_x, max_w, row_h, pad = 104, 430, 30, 16
    height = pad * 2 + row_h * len(rows)
    max_count = max([int(row["count"]) for row in rows] + [1])
    parts = [
        f'<svg class="chart" viewBox="0 0 640 {height}" width="640" height="{height}" '
        f'role="img" aria-label="漏洞严重度分布">',
        f'<text x="8" y="{pad - 2}" class="chart-label">严重度分布（共 {sum(int(r["count"]) for r in rows)} 条发现）</text>',
    ]
    for index, row in enumerate(rows):
        y = pad + index * row_h + 8
        count = int(row["count"])
        width = 0 if count <= 0 else max(3, round(max_w * count / max_count))
        parts.append(f'<text x="8" y="{y + 14}" class="chart-label">{esc(row["severity_cn"])}</text>')
        parts.append(f'<rect x="{bar_x}" y="{y + 2}" width="{max_w}" height="16" class="chart-track"/>')
        if width:
            parts.append(
                f'<rect x="{bar_x}" y="{y + 2}" width="{width}" height="16" '
                f'class="chart-bar s-{_sev_class(row["severity"])}"/>'
            )
        parts.append(f'<text x="{bar_x + max_w + 12}" y="{y + 15}" class="chart-value">{count}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render(data: dict[str, Any]) -> str:
    """渲染完整自包含 HTML 报告。"""
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(data['title'])}</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        '<main class="report">',
    ]
    _cover(parts, data)
    _summary(parts, data)
    _compliance(parts, data)
    _attack_path(parts, data)
    _assets(parts, data)
    _findings(parts, data)
    _evidence(parts, data)
    _secrets(parts, data)
    _appendix(parts, data)
    parts.append(
        f'<footer>本报告由 {esc(data["generator"])} 生成，内容与审计哈希链同源，可离线归档；'
        "页面不含任何外部资源引用。</footer>"
    )
    parts += ["</main>", "</body>", "</html>"]
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- 各章节
def _cover(parts: list[str], data: dict[str, Any]) -> None:
    parts.append(f"<h1>{esc(data['title'])}</h1>")
    parts.append(
        f'<p class="meta">最终状态 {esc(data["status_cn"])} · 目标 {esc(data["target"])} · '
        f'Run ID {esc(data["run_id"])} · 报告生成时间 {esc(data["generated_at"])}</p>'
    )
    parts.append(_kv(data["cover_rows"]))


def _summary(parts: list[str], data: dict[str, Any]) -> None:
    metrics = data["metrics"]
    parts.append(f"<h2>{esc(data['sections']['summary'])}</h2>")
    parts.append("<h3>风险统计</h3>")
    parts.append(_table(["严重度", "数量", "占比"], data["summary_rows"], numeric=[1, 2]))
    parts.append(
        '<figure class="chart-wrap">'
        + _severity_chart(data["severity_rows"])
        + "<figcaption>图 1 漏洞严重度分布（内联 SVG，无外部图表库）</figcaption></figure>"
    )
    parts.append("<h3>关键计数</h3>")
    parts.append(
        _kv(
            [
                ["主机数", metrics["hosts"]],
                ["开放端口数", metrics["ports"]],
                ["凭据数", metrics["credentials"]],
                ["Flag 数", metrics["flags"]],
                ["漏洞总数", metrics["findings"]],
                ["总步数", metrics["steps"]],
                ["LLM 调用次数", metrics["llm_calls"]],
                ["LLM 成本（美元）", f"{metrics['cost_usd']:.6f}"],
            ]
        )
    )


def _compliance(parts: list[str], data: dict[str, Any]) -> None:
    compliance = data["compliance"]
    parts.append(f"<h2>{esc(data['sections']['compliance'])}</h2>")
    parts.append(
        "<p>本次测试仅针对下列授权范围内的目标执行；所有主动动作均由护栏（ScopeGuard）判定，"
        "放行与拒绝结果写入审计哈希链，未授权目标不产生任何流量。</p>"
    )
    parts.append("<p>护栏判据（与运行时同源，取自 <code>cfg.scope</code>）：</p>")
    parts.append(f"<pre>{esc(compliance['statement'])}</pre>")
    parts.append(_kv(data["compliance_rows"]))
    if compliance["allow_public"]:
        parts.append(
            '<p class="warn">警告：<code>allow_public</code> 已开启，测试范围可越出私有网段，'
            "请确认授权文件覆盖公网目标。</p>"
        )
    if compliance["destructive"]:
        parts.append(
            '<p class="warn">警告：<code>destructive</code> 已开启，破坏性动作仅对 '
            "<code>destructive_hosts</code> 白名单内主机生效。</p>"
        )


def _attack_path(parts: list[str], data: dict[str, Any]) -> None:
    path = data["attack_path"]
    parts.append(f"<h2>{esc(data['sections']['attack_path'])}</h2>")
    parts.append('<pre class="mermaid-src">' + esc(path["mermaid"]) + "</pre>")
    parts.append(f'<p class="meta">{esc(path["note"])}</p>')
    parts.append(
        _table(
            ["#", "时刻", "类型", "事件"],
            [
                [index, node["clock"], node["kind_cn"], node["label"]]
                for index, node in enumerate(path["nodes"], 1)
            ],
            numeric=[0],
        )
    )


def _assets(parts: list[str], data: dict[str, Any]) -> None:
    parts.append(f"<h2>{esc(data['sections']['assets'])}</h2>")
    if not data["asset_rows"]:
        parts.append('<p class="empty">（未发现资产）</p>')
        return
    parts.append(_table(["主机", "存活", "端口/服务/版本", "技术栈", "已发现路径"], data["asset_rows"]))


def _findings(parts: list[str], data: dict[str, Any]) -> None:
    parts.append(f"<h2>{esc(data['sections']['findings'])}</h2>")
    findings = data["findings"]
    if not findings:
        parts.append('<p class="empty">（未发现漏洞）</p>')
        return
    rows: list[list[Any]] = []
    for finding in findings:
        rows.append(
            [
                finding["id"],
                finding["title"],
                finding["severity_cn"],
                finding["confidence_cn"],
                finding["status_cn"],
                finding["vuln_class"],
                finding["location"],
            ]
        )
    parts.append(_table(["ID", "标题", "严重度", "置信度", "状态", "类型", "位置"], rows))
    for finding in findings:
        _finding(parts, finding)


def _finding(parts: list[str], finding: dict[str, Any]) -> None:
    parts.append(
        f"<h3>[{esc(finding['id'])}] {esc(finding['title'])} "
        f'<span class="sev sev-{_sev_class(finding["severity"])}">{esc(finding["severity_cn"])}</span></h3>'
    )
    cvss_note = "（内置默认向量）" if finding["cvss_source"] == "default" else "（来自工具）"
    cwe_note = {"provided": "（来自工具）", "default": "（内置默认）"}.get(finding["cwe_source"], "")
    parts.append(
        _kv(
            [
                ["ID", finding["id"]],
                ["严重度", f"{finding['severity_cn']}（{finding['severity']}）"],
                ["置信度", f"{finding['confidence_cn']}（{finding['confidence']}）"],
                ["状态", f"{finding['status_cn']}（{finding['status']}）"],
                ["漏洞类型", f"{finding['vuln_class']}（归一: {finding['vuln_class_key']}）"],
                ["CVSS 向量", f"{finding['cvss_version']} {finding['cvss']}{cvss_note}"],
                ["CWE", f"{finding['cwe']}{cwe_note}"],
                ["目标", finding["target"] or "-"],
                ["URL", finding["url"] or "-"],
                ["参数 / 方法", f"{finding['parameter']} / {finding['method']}"],
                ["发现工具", finding["tool"]],
                ["首次发现", finding["first_seen_text"]],
                ["最近确认", finding["last_seen_text"]],
            ]
        )
    )
    parts.append(f"<h4>描述</h4><p>{esc(finding['description'])}</p>")
    parts.append(f"<h4>影响</h4><p>{esc(finding['impact'])}</p>")
    parts.append("<h4>利用过程与证据</h4>")
    if finding["evidence"]:
        parts.append('<ol class="evidence">')
        for ev in finding["evidence"]:
            parts.append("<li>")
            parts.append(
                f'<div class="ev-head"><span class="chip">{esc(ev["kind"])}</span> '
                f'<span class="ev-target">{esc(ev["target"] or "-")}</span> '
                f'<span class="ev-clock">{esc(ev["clock"])}</span></div>'
            )
            for key, label in (
                ("note", "说明"),
                ("payload", "载荷"),
                ("request", "请求"),
                ("response", "响应"),
                ("command", "命令"),
                ("output", "输出"),
            ):
                if ev.get(key):
                    parts.append(
                        f'<div class="ev-line"><span class="ev-key">{label}</span>'
                        f"<code>{esc(ev[key])}</code></div>"
                    )
            parts.append("</li>")
        parts.append("</ol>")
    else:
        parts.append('<p class="empty">（无证据记录）</p>')
    if finding["commands"]:
        parts.append("<h4>复现命令</h4>")
        parts.append("<pre>" + esc("\n".join(str(cmd) for cmd in finding["commands"])) + "</pre>")
    remediation_note = "（内置默认建议）" if finding["remediation_source"] == "default" else "（来自工具）"
    parts.append(f"<h4>修复建议{esc(remediation_note)}</h4><p>{esc(finding['remediation'])}</p>")
    if finding["refs"]:
        parts.append("<h4>参考</h4><ul>")
        for ref in finding["refs"]:
            parts.append(f"<li>{esc(ref)}</li>")
        parts.append("</ul>")


def _evidence(parts: list[str], data: dict[str, Any]) -> None:
    """证据链与假设台账（HTML 不依赖外部脚本，因此以表格代替 mermaid 图）。"""
    parts.append(f"<h2>{esc(data['sections']['evidence'])}</h2>")
    graph = data.get("evidence_graph") or {}
    counts = data.get("hypothesis_counts") or {}
    parts.append(
        f'<p class="meta">假设台账：共 {len(data.get("hypotheses") or [])} 条'
        f"（待验证 {counts.get('open', 0)}、验证中 {counts.get('verifying', 0)}、"
        f"已确认 {counts.get('confirmed', 0)}、已否定 {counts.get('refuted', 0)}、"
        f"被阻断 {counts.get('blocked', 0)}）</p>"
    )
    if data.get("hypothesis_rows"):
        parts.append(
            _table(
                ["ID", "状态", "置信度", "优先级", "假设", "下一步", "证据数"],
                data["hypothesis_rows"],
                numeric=[2, 3, 6],
            )
        )
    else:
        parts.append('<p class="empty">（本轮未记录假设）</p>')
    if graph.get("available"):
        parts.append(f"<h3>证据关系图</h3><p class=\"meta\">{esc(graph.get('summary') or '')}</p>")
        if graph.get("edge_rows"):
            parts.append(_table(["起点", "关系", "终点"], graph["edge_rows"]))
    else:
        parts.append('<p class="empty">（本轮没有可绘制的证据关系）</p>')


def _secrets(parts: list[str], data: dict[str, Any]) -> None:
    parts.append(f"<h2>{esc(data['sections']['secrets'])}</h2>")
    parts.append("<h3>有效凭据</h3>")
    if data["credentials"]:
        parts.append(_table(["目标", "服务", "用户名", "密码", "来源", "已验证"], data["credentials"]))
        parts.append(
            '<p class="meta">说明：口令按前 2 位 + 长度提示脱敏展示（如 <code>ad***(8)</code>），'
            "完整口令不入报告。</p>"
        )
    else:
        parts.append('<p class="empty">（未获取凭据）</p>')
    parts.append("<h3>Flag</h3>")
    if data["flags"]:
        parts.append(_table(["#", "Flag", "来源", "时间"], data["flags"], numeric=[0]))
    else:
        parts.append('<p class="empty">（未夺取 flag）</p>')


def _appendix(parts: list[str], data: dict[str, Any]) -> None:
    parts.append(f"<h2>{esc(data['sections']['appendix'])}</h2>")
    parts.append("<h3>工具调用时间线</h3>")
    if not data.get("include_timeline", True):
        parts.append(
            '<p class="meta">（已按 <code>report.include_timeline=false</code> 省略工具调用时间线；'
            "原始步骤仍在导出的 JSON 中）</p>"
        )
    elif data["step_rows"]:
        parts.append(_table(["时刻", "工具", "参数", "结果摘要", "耗时"], data["step_rows"], numeric=[4]))
        if data["steps_truncated"]:
            parts.append(
                f'<p class="meta">（共 {len(data["steps"])} 步，表格仅展示前 {len(data["step_rows"])} 步）</p>'
            )
    else:
        parts.append('<p class="empty">（无步骤记录）</p>')
    parts.append("<h3>指标</h3>")
    parts.append(_kv(data["metric_rows"]))
    parts.append("<h3>审计摘要</h3>")
    audit = data["audit"]
    if not audit:
        parts.append('<p class="empty">（未提供审计摘要）</p>')
        return
    parts.append(_kv(audit["kv_rows"]))
    if audit["event_rows"]:
        parts.append("<h4>事件计数（按次数降序）</h4>")
        parts.append(_table(["事件", "次数"], audit["event_rows"], numeric=[1]))
    if audit["tool_rows"]:
        parts.append("<h4>工具调用计数（按次数降序）</h4>")
        parts.append(_table(["工具", "次数"], audit["tool_rows"], numeric=[1]))
    if audit["raw_rows"]:
        parts.append(f"<h4>审计原始记录（前 {len(audit['raw_rows'])} 条）</h4>")
        parts.append(
            _table(
                ["seq", "时间", "事件", "工具", "目标", "判定", "说明"],
                audit["raw_rows"],
                numeric=[0],
            )
        )
        if audit["raw_truncated"]:
            parts.append(f'<p class="meta">（审计记录超过 {len(audit["raw_rows"])} 条，仅展示前 {len(audit["raw_rows"])} 条）</p>')
