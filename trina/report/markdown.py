"""Markdown 渲染：把 `builder.collect_data()` 产出的结构化数据排成中文报告。

渲染器不访问 `RunState`/`TrinaConfig`，所有默认值填充已在 builder 完成，
因此 Markdown 与 HTML 的内容严格一致（HTML 仅多了内联 SVG 图表）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def render(data: dict[str, Any]) -> str:
    """渲染完整 Markdown 报告。"""
    lines: list[str] = []
    _cover(lines, data)
    _summary(lines, data)
    _compliance(lines, data)
    _attack_path(lines, data)
    _assets(lines, data)
    _findings(lines, data)
    _secrets(lines, data)
    _appendix(lines, data)
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- 基础排版
def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    align: Sequence[str] | None = None,
) -> list[str]:
    align = list(align or ["l"] * len(headers))
    marks = {"l": "---", "r": "---:", "c": ":---:"}
    out = ["| " + " | ".join(_cell(h) for h in headers) + " |"]
    out.append("| " + " | ".join(marks.get(a, "---") for a in align) + " |")
    for row in rows:
        out.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return out


def _code(text: Any) -> str:
    return "`" + str(text).replace("`", "'") + "`"


def _kv(pairs: Iterable[Sequence[Any]], align: Sequence[str] | None = None) -> list[str]:
    return _table(["项目", "内容"], pairs, align)


def _section(data: dict[str, Any], key: str) -> list[str]:
    return ["", f"## {data['sections'][key]}", ""]


# --------------------------------------------------------------------------- 各章节
def _cover(lines: list[str], data: dict[str, Any]) -> None:
    lines.append(f"# {data['title']}")
    lines.append("")
    lines += _kv(data["cover_rows"])
    lines.append("")


def _summary(lines: list[str], data: dict[str, Any]) -> None:
    lines += _section(data, "summary")
    lines.append("风险统计：")
    lines.append("")
    lines += _table(["严重度", "数量", "占比"], data["summary_rows"], align=["l", "r", "r"])
    lines.append("")
    metrics = data["metrics"]
    lines += _kv(
        [
            ["主机数", metrics["hosts"]],
            ["开放端口数", metrics["ports"]],
            ["凭据数", metrics["credentials"]],
            ["Flag 数", metrics["flags"]],
            ["漏洞总数", metrics["findings"]],
            ["总步数", metrics["steps"]],
            ["LLM 调用次数", metrics["llm_calls"]],
            ["LLM 成本（美元）", f"{metrics['cost_usd']:.6f}"],
        ],
        align=["l", "r"],
    )


def _compliance(lines: list[str], data: dict[str, Any]) -> None:
    compliance = data["compliance"]
    lines += _section(data, "compliance")
    lines.append("本次测试仅针对下列授权范围内的目标执行；所有主动动作均由护栏（ScopeGuard）判定，")
    lines.append("放行/拒绝结果写入审计哈希链，未授权目标不产生任何流量。")
    lines.append("")
    lines.append("护栏判据（与运行时同源，取自 `cfg.scope`）：")
    lines.append("")
    lines.append("```text")
    lines.append(str(compliance["statement"]))
    lines.append("```")
    lines.append("")
    lines += _kv(data["compliance_rows"], align=["l", "l"])
    if compliance["allow_public"]:
        lines.append("")
        lines.append("> 警告：`allow_public` 已开启，测试范围可越出私有网段，请确认授权文件覆盖公网目标。")
    if compliance["destructive"]:
        lines.append("")
        lines.append("> 警告：`destructive` 已开启，破坏性动作仅对 `destructive_hosts` 白名单内主机生效。")


def _attack_path(lines: list[str], data: dict[str, Any]) -> None:
    path = data["attack_path"]
    lines += _section(data, "attack_path")
    lines.append("```mermaid")
    lines.append(str(path["mermaid"]))
    lines.append("```")
    lines.append("")
    lines.append(str(path["note"]))
    lines.append("")
    lines += _table(
        ["#", "时刻", "类型", "事件"],
        [
            [index, node["clock"], node["kind_cn"], node["label"]]
            for index, node in enumerate(path["nodes"], 1)
        ],
        align=["r", "l", "l", "l"],
    )


def _assets(lines: list[str], data: dict[str, Any]) -> None:
    lines += _section(data, "assets")
    if not data["asset_rows"]:
        lines.append("（未发现资产）")
        return
    lines += _table(["主机", "存活", "端口/服务/版本", "技术栈", "已发现路径"], data["asset_rows"])


def _findings(lines: list[str], data: dict[str, Any]) -> None:
    lines += _section(data, "findings")
    findings = data["findings"]
    if not findings:
        lines.append("（未发现漏洞）")
        return
    lines += _table(
        ["ID", "标题", "严重度", "置信度", "状态", "类型", "位置"],
        data["finding_rows"],
    )
    for finding in findings:
        _finding(lines, finding)


def _finding(lines: list[str], finding: dict[str, Any]) -> None:
    lines.append("")
    lines.append(f"### [{finding['id']}] {finding['title']}")
    lines.append("")
    cvss_note = "（内置默认向量）" if finding["cvss_source"] == "default" else "（来自工具）"
    cwe_note = {"provided": "（来自工具）", "default": "（内置默认）"}.get(finding["cwe_source"], "")
    lines += _kv(
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
    lines.append("")
    lines.append("**描述**")
    lines.append("")
    lines.append(str(finding["description"]))
    lines.append("")
    lines.append("**影响**")
    lines.append("")
    lines.append(str(finding["impact"]))
    lines.append("")
    lines.append("**利用过程与证据**")
    lines.append("")
    if finding["evidence"]:
        for index, ev in enumerate(finding["evidence"], 1):
            lines.append(f"{index}. [{ev['kind']}] {ev['target'] or '-'}（{ev['clock']}）")
            if ev["note"]:
                lines.append(f"   - 说明: {ev['note']}")
            if ev["payload"]:
                lines.append(f"   - 载荷: {_code(ev['payload'])}")
            if ev["request"]:
                lines.append(f"   - 请求: {ev['request']}")
            if ev["response"]:
                lines.append(f"   - 响应: {ev['response']}")
            if ev["command"]:
                lines.append(f"   - 命令: {_code(ev['command'])}")
            if ev["output"]:
                lines.append(f"   - 输出: {ev['output']}")
    else:
        lines.append("（无证据记录）")
    lines.append("")
    if finding["commands"]:
        lines.append("**复现命令**")
        lines.append("")
        lines.append("```bash")
        lines += [str(cmd) for cmd in finding["commands"]]
        lines.append("```")
        lines.append("")
    remediation_note = "（内置默认建议）" if finding["remediation_source"] == "default" else "（来自工具）"
    lines.append(f"**修复建议**{remediation_note}")
    lines.append("")
    lines.append(str(finding["remediation"]))
    if finding["refs"]:
        lines.append("")
        lines.append("**参考**")
        lines.append("")
        lines += [f"- {ref}" for ref in finding["refs"]]
    lines.append("")


def _secrets(lines: list[str], data: dict[str, Any]) -> None:
    lines += _section(data, "secrets")
    lines.append("### 有效凭据")
    lines.append("")
    if data["credentials"]:
        lines += _table(["目标", "服务", "用户名", "密码", "来源", "已验证"], data["credentials"])
        lines.append("")
        lines.append("说明：口令按前 2 位 + 长度提示脱敏展示（如 `ad***(8)`），完整口令不入报告。")
    else:
        lines.append("（未获取凭据）")
    lines.append("")
    lines.append("### Flag")
    lines.append("")
    if data["flags"]:
        lines += _table(["#", "Flag", "来源", "时间"], data["flags"], align=["r", "l", "l", "l"])
    else:
        lines.append("（未夺取 flag）")


def _appendix(lines: list[str], data: dict[str, Any]) -> None:
    lines += _section(data, "appendix")
    lines.append("### 工具调用时间线")
    lines.append("")
    if not data.get("include_timeline", True):
        lines.append("（已按 `report.include_timeline=false` 省略工具调用时间线；原始步骤仍在导出的 JSON 中）")
    elif data["step_rows"]:
        lines += _table(["时刻", "工具", "参数", "结果摘要", "耗时"], data["step_rows"])
        if data["steps_truncated"]:
            lines.append("")
            lines.append(f"（共 {len(data['steps'])} 步，表格仅展示前 {len(data['step_rows'])} 步）")
    else:
        lines.append("（无步骤记录）")
    lines.append("")
    lines.append("### 指标")
    lines.append("")
    lines += _kv(data["metric_rows"], align=["l", "r"])
    lines.append("")
    lines.append("### 审计摘要")
    lines.append("")
    audit = data["audit"]
    if not audit:
        lines.append("（未提供审计摘要）")
        return
    lines += _kv(audit["kv_rows"])
    if audit["event_rows"]:
        lines.append("")
        lines.append("事件计数（按次数降序）：")
        lines.append("")
        lines += _table(["事件", "次数"], audit["event_rows"], align=["l", "r"])
    if audit["tool_rows"]:
        lines.append("")
        lines.append("工具调用计数（按次数降序）：")
        lines.append("")
        lines += _table(["工具", "次数"], audit["tool_rows"], align=["l", "r"])
    if audit["raw_rows"]:
        lines.append("")
        lines.append(f"审计原始记录（前 {len(audit['raw_rows'])} 条）：")
        lines.append("")
        lines += _table(
            ["seq", "时间", "事件", "工具", "目标", "判定", "说明"],
            audit["raw_rows"],
            align=["r", "l", "l", "l", "l", "l", "l"],
        )
        if audit["raw_truncated"]:
            lines.append("")
            lines.append(f"（审计记录超过 {len(audit['raw_rows'])} 条，仅展示前 {len(audit['raw_rows'])} 条）")
