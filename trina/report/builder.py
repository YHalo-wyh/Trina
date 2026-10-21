"""报告构建：把 `RunState` + `TrinaConfig` 组装成可交付的报告。

数据流::

    state + cfg --collect_data--> data(dict, 可 JSON 序列化的事实集合)
    data        --trina.report.markdown--> Markdown 文本
    data        --trina.report.html-----> 自包含 HTML（内联 CSS + 内联 SVG，无外部资源/脚本）

设计约定
--------
* 所有「补默认值」的工作（CVSS / CWE / 修复建议 / 密码脱敏 / 表格行）都在这里完成，
  两个渲染器只做排版，因此 Markdown 与 HTML 的**内容**天然一致。
* 口令在进入 `data` 之前即脱敏（形如 ``ad***(8)``），JSON 导出同样不含明文。
* `data` 保证可被 `json.dumps` 序列化。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..audit import AuditLog
from ..config import TrinaConfig
from ..state import SEVERITIES, SEVERITY_CN, SEVERITY_ORDER, Finding, RunState
from .html import render as render_html
from .markdown import render as render_markdown

#: 攻击路径图最多节点数（含起始目标节点与省略节点）。
MAX_PATH_NODES = 12
#: 附录步骤表最多渲染的行数。
MAX_STEP_ROWS = 500
#: `report.include_raw_audit` 开启时，附录最多渲染的审计原始记录条数。
MAX_AUDIT_ROWS = 200

RUN_STATUS_CN = {
    "running": "进行中",
    "success": "成功",
    "partial": "部分成功",
    "failed": "失败",
    "blocked": "被阻断",
}
CONFIDENCE_CN = {"low": "低", "medium": "中", "high": "高"}
FINDING_STATUS_CN = {"detected": "已发现", "verified": "已验证", "exploited": "已利用"}
HYPOTHESIS_STATUS_CN = {"open": "待验证", "verifying": "验证中", "confirmed": "已确认", "refuted": "已否定", "blocked": "被阻断"}
NODE_KIND_CN = {
    "target": "目标",
    "phase": "阶段",
    "finding": "漏洞",
    "credential": "凭据",
    "flag": "夺旗",
    "denied": "护栏",
    "tool": "工具",
    "omitted": "省略",
}

SECTION_TITLES: dict[str, str] = {
    "summary": "一、执行摘要",
    "compliance": "二、授权与合规声明",
    "attack_path": "三、攻击路径",
    "assets": "四、资产清单",
    "findings": "五、漏洞详情",
    "evidence": "六、证据链与假设台账",
    "secrets": "七、凭据与 Flag",
    "appendix": "八、附录",
}

#: 漏洞类 → 默认 CVSS 3.1 向量（`Finding.cvss` 为空时使用）。
DEFAULT_CVSS: dict[str, str] = {
    "sqli": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
    "xss_reflected": "AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
    "xss_stored": "AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
    "rce": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    "lfi": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
    "file_upload": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    "weak_password": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    "git_leak": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
    "env_leak": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
    "phpinfo": "AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N",
    "actuator": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
    "directory_listing": "AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N",
    "security_misconfig_headers": "AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:L/A:N",
    "tls": "AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N",
    "unauthorized_access": "AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:L/A:N",
}
DEFAULT_CVSS_FALLBACK = "AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N"

#: 漏洞类 → 默认 CWE 编号。
DEFAULT_CWE: dict[str, str] = {
    "sqli": "CWE-89",
    "xss_reflected": "CWE-79",
    "xss_stored": "CWE-79",
    "rce": "CWE-78",
    "lfi": "CWE-22",
    "file_upload": "CWE-434",
    "weak_password": "CWE-521",
    "git_leak": "CWE-538",
    "env_leak": "CWE-538",
    "phpinfo": "CWE-200",
    "actuator": "CWE-200",
    "directory_listing": "CWE-548",
    "security_misconfig_headers": "CWE-693",
    "tls": "CWE-326",
    "unauthorized_access": "CWE-284",
}

#: 漏洞类 → 默认修复建议（`Finding.remediation` 为空时使用）。
DEFAULT_REMEDIATION: dict[str, str] = {
    "sqli": (
        "使用参数化查询/预编译语句，禁止把外部输入拼接进 SQL；对参数做类型与白名单校验；"
        "数据库账号按最小权限授权并关闭多语句执行；接入 WAF 并对异常查询告警。"
    ),
    "xss_reflected": (
        "按输出上下文（HTML/属性/JS/URL）做编码，优先使用框架的自动转义；对输入做白名单过滤；"
        "下发 Content-Security-Policy，敏感 Cookie 加 HttpOnly/Secure。"
    ),
    "xss_stored": (
        "存储前做白名单净化、输出时按上下文编码；富文本使用成熟的白名单净化库；"
        "配合 CSP 与 HttpOnly Cookie 降低利用面。"
    ),
    "rce": (
        "禁止把外部输入拼接进系统命令、模板或反序列化入口；使用白名单参数与安全 API（如 subprocess 列表参数）；"
        "服务以低权限账号运行并与主机关键资源隔离。"
    ),
    "lfi": (
        "禁止用用户输入直接拼接文件路径；改用文件名白名单或 ID→路径映射表；"
        "限制 open_basedir/容器挂载范围，过滤 '../' 与 php://、file:// 等协议包装器。"
    ),
    "file_upload": (
        "对扩展名、MIME、文件头做三重白名单校验；上传目录禁止脚本执行并置于 Web 根之外；"
        "重命名为随机名、限制大小与类型；必要时送病毒扫描。"
    ),
    "weak_password": (
        "实施强口令策略与登录失败锁定/验证码；清除默认口令；后台、SSH、数据库限制来源 IP 并启用多因素认证。"
    ),
    "git_leak": (
        "不要把 .git 目录部署到 Web 根；服务器显式拒绝 /.git/* 访问；定期扫描源码泄漏；"
        "一旦泄漏立即轮换其中出现的所有密钥与口令。"
    ),
    "env_leak": (
        "不要把 .env 等配置文件放进 Web 可访问目录；配置与代码分离并加密存储；"
        "拒绝访问点文件与备份文件；泄漏后轮换其中的全部凭据。"
    ),
    "phpinfo": (
        "生产环境移除 phpinfo/探针/测试页面；关闭 expose_php 与详细错误回显；无法删除时对调试页面加访问控制。"
    ),
    "actuator": (
        "生产环境收敛 Spring Boot Actuator 端点（仅保留 health/info）；用 Spring Security 保护管理端点；"
        "管理端口只对内网开放并启用认证。"
    ),
    "directory_listing": (
        "关闭 Web 服务器的目录浏览（nginx autoindex off / Apache Options -Indexes）；"
        "敏感目录放置占位 index 并加认证；确认静态资源目录不含备份与配置。"
    ),
    "security_misconfig_headers": (
        "补齐 X-Content-Type-Options、X-Frame-Options 或 CSP frame-ancestors、Strict-Transport-Security、"
        "Content-Security-Policy、Referrer-Policy；移除版本号等指纹响应头。"
    ),
    "tls": (
        "禁用 SSLv2/SSLv3/TLS1.0/TLS1.1 与弱套件，启用 TLS1.2+ 与 HSTS；"
        "部署完整有效证书链，关闭不安全重协商与压缩。"
    ),
    "unauthorized_access": (
        "在服务端强制做权限校验（禁止仅前端隐藏入口）；按对象归属做访问控制；遵循默认拒绝与最小授权；"
        "补充越权用例到回归测试。"
    ),
    "_default": (
        "按该漏洞类的最佳实践修复：输入校验、输出编码、最小权限与访问控制，并在修复后回归验证。"
    ),
}

#: 漏洞类别名归一化表（工具命名不统一，报告需归一到内置表）。
_CLASS_ALIASES: dict[str, str] = {
    "sql": "sqli",
    "sqlinjection": "sqli",
    "sql_injection": "sqli",
    "xss": "xss_reflected",
    "reflected_xss": "xss_reflected",
    "cross_site_scripting": "xss_reflected",
    "stored_xss": "xss_stored",
    "command_injection": "rce",
    "cmd_injection": "rce",
    "code_execution": "rce",
    "cmdi": "rce",
    "path_traversal": "lfi",
    "directory_traversal": "lfi",
    "local_file_inclusion": "lfi",
    "upload": "file_upload",
    "arbitrary_file_upload": "file_upload",
    "weak_credential": "weak_password",
    "weak_credentials": "weak_password",
    "weak_passwords": "weak_password",
    "default_credentials": "weak_password",
    "brute_force": "weak_password",
    "git_exposure": "git_leak",
    "git_repository_leak": "git_leak",
    "dotgit": "git_leak",
    ".git": "git_leak",
    "env_exposure": "env_leak",
    "dotenv_leak": "env_leak",
    "config_leak": "env_leak",
    ".env": "env_leak",
    "php_info": "phpinfo",
    "spring_actuator": "actuator",
    "actuator_exposure": "actuator",
    "dir_listing": "directory_listing",
    "directory_browsing": "directory_listing",
    "autoindex": "directory_listing",
    "security_headers": "security_misconfig_headers",
    "missing_security_headers": "security_misconfig_headers",
    "misconfig_headers": "security_misconfig_headers",
    "security_misconfiguration": "security_misconfig_headers",
    "ssl": "tls",
    "weak_tls": "tls",
    "ssl_tls": "tls",
    "tls_misconfig": "tls",
    "unauthorized": "unauthorized_access",
    "idor": "unauthorized_access",
    "broken_access_control": "unauthorized_access",
    "auth_bypass": "unauthorized_access",
    "access_control": "unauthorized_access",
}

_PATH_NOTE = (
    "上图为 mermaid 流程图源码；Markdown/HTML 预览器需支持 mermaid 才能渲染（报告不引入任何外部 JS）。"
)

_FORMAT_EXT: dict[str, str] = {"markdown": ".md", "md": ".md", "html": ".html", "json": ".json"}
_FORMAT_ALIAS: dict[str, str] = {"md": "markdown"}


# --------------------------------------------------------------------------- 小工具
def _short(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(1, limit - 1)] + "…"
    return text


def _time_text(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


def _clock_text(ts: Any) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _duration_text(seconds: Any) -> str:
    total = max(0.0, _num(seconds))
    if total >= 3600:
        hours = int(total // 3600)
        minutes = int((total % 3600) // 60)
        return f"{hours} 小时 {minutes} 分 {int(total % 60)} 秒"
    if total >= 60:
        return f"{int(total // 60)} 分 {int(total % 60)} 秒"
    return f"{total:.1f} 秒"


def _yesno(value: Any) -> str:
    return "是" if value else "否"


def _pct(count: int, total: int) -> str:
    if total <= 0:
        return "-"
    return f"{count * 100.0 / total:.1f}%"


def _host_only(value: Any) -> str:
    text = str(value or "")
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.split("/")[0]


def _limit(items: list[str], limit: int) -> list[str]:
    if len(items) <= limit:
        return list(items)
    return list(items[:limit]) + [f"…（另 {len(items) - limit} 条）"]


def _mask_secret(secret: Any) -> str:
    """口令脱敏：保留前 2 位 + 长度提示（``ad***(8)``）；过短或已脱敏则整体隐藏。"""
    text = str(secret or "")
    if not text or text in ("<redacted>", "-", "***"):
        return "<redacted>"
    if len(text) <= 3:
        return f"***({len(text)})"
    return f"{text[:2]}***({len(text)})"


def _class_key(vuln_class: Any) -> str:
    key = str(vuln_class or "").strip().lower()
    for ch in " -/\\.":
        key = key.replace(ch, "_")
    key = key.strip("_")
    return _CLASS_ALIASES.get(key, key)


def default_cvss(vuln_class: Any) -> str:
    """按漏洞类给出默认 CVSS 3.1 向量（未知类走兜底向量）。"""
    return DEFAULT_CVSS.get(_class_key(vuln_class), DEFAULT_CVSS_FALLBACK)


def default_cwe(vuln_class: Any) -> str:
    """按漏洞类给出默认 CWE 编号（未知类返回空串）。"""
    return DEFAULT_CWE.get(_class_key(vuln_class), "")


def default_remediation(vuln_class: Any) -> str:
    """按漏洞类给出默认修复建议（未知类走通用建议）。"""
    return DEFAULT_REMEDIATION.get(_class_key(vuln_class), DEFAULT_REMEDIATION["_default"])


def _usage(ledger: Any) -> dict[str, Any]:
    """归一 LLM 用量（接受 `LLMUsage` 对象或 dict；缺字段按 0 计）。"""
    empty = {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.0,
        "model": "",
    }
    if ledger is None:
        return dict(empty)
    if hasattr(ledger, "to_dict"):
        try:
            raw: dict[str, Any] = dict(ledger.to_dict())
        except Exception:  # noqa: BLE001 - 用量只是报告装饰，不能因此崩掉报告
            return dict(empty)
    elif isinstance(ledger, dict):
        raw = ledger
    else:
        return dict(empty)

    def pick_int(*names: str) -> int:
        for name in names:
            value = raw.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    cost = 0.0
    for name in ("cost_usd", "cost", "total_cost", "usd"):
        value = raw.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cost = float(value)
            break
    return {
        "calls": pick_int("calls", "llm_calls", "count"),
        "input_tokens": pick_int("input_tokens", "prompt_tokens"),
        "output_tokens": pick_int("output_tokens", "completion_tokens"),
        "cache_read_tokens": pick_int("cache_read_tokens", "cached_tokens"),
        "cost_usd": round(cost, 6),
        "model": str(raw.get("model") or ""),
    }


def _mermaid_safe(text: Any) -> str:
    """生成可直接放进 mermaid 节点标签的文本（剔除会破坏语法的字符）。"""
    bad = set('[](){}"|<>`\\;')
    cleaned = "".join(" " if ch in bad else ch for ch in str(text or ""))
    cleaned = cleaned.replace("-->", "→")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > 60:
        head = cleaned[:59]
        if " " in head[20:]:
            head = head[: head.rfind(" ")]
        cleaned = head.rstrip() + "…"
    return cleaned


def _clean(value: Any) -> Any:
    """把任意对象收敛成 JSON 可序列化的结构。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v) for v in value]
    return str(value)


# --------------------------------------------------------------------------- 证据
def _evidence_row(ev: dict[str, Any]) -> dict[str, Any]:
    """把工具写入的证据条目归一成「请求 / 响应 / 命令 / 输出」四段文本。"""
    request = ev.get("request")
    if isinstance(request, dict):
        method = str(request.get("method") or ev.get("method") or "GET")
        url = str(request.get("url") or ev.get("target") or ev.get("url") or "")
        request_text = f"{method} {url}".strip()
        headers = request.get("headers")
        if isinstance(headers, dict) and headers:
            head_text = ", ".join(f"{k}: {v}" for k, v in list(headers.items())[:4])
            request_text += f" | 请求头: {head_text}"
        body = str(request.get("body") or ev.get("request_body") or "")
        if body:
            request_text += f" | 请求体: {_short(body, 160)}"
    elif request:
        request_text = _short(request, 300)
    else:
        request_text = ""

    response = ev.get("response")
    if isinstance(response, dict):
        pieces: list[str] = []
        status = response.get("status")
        if isinstance(status, (int, float)) and not isinstance(status, bool):
            pieces.append(f"状态 {int(status)}")
        length = response.get("length")
        if isinstance(length, (int, float)) and not isinstance(length, bool):
            pieces.append(f"长度 {int(length)}")
        response_text = "，".join(pieces)
        body = str(response.get("body_excerpt") or response.get("body") or "")
        excerpt = _short(body, 200)
        if excerpt:
            response_text += ("｜" if response_text else "") + f"响应片段: {excerpt}"
    elif response:
        response_text = _short(response, 300)
    else:
        response_text = ""

    command = str(ev.get("command") or ev.get("cmd") or "")
    output = str(ev.get("output") or ev.get("stdout") or ev.get("raw") or ev.get("result") or "")
    return {
        "kind": str(ev.get("kind") or "evidence"),
        "target": str(ev.get("target") or ev.get("url") or ""),
        "note": _short(ev.get("note") or "", 200),
        "payload": _short(ev.get("payload") or "", 160),
        "request": request_text,
        "response": response_text,
        "command": _short(command, 400),
        "output": _short(output.replace("\n", " "), 300),
        "clock": str(ev.get("clock") or _clock_text(ev.get("ts"))),
    }


# --------------------------------------------------------------------------- 攻击路径
def _attack_path(state: RunState) -> dict[str, Any]:
    """从时间线/步骤生成「侦察 → 发现 → 利用 → 夺旗」链路（节点上限 12）。"""
    candidates: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    def push(ts: Any, kind: str, label: str) -> None:
        text = _mermaid_safe(label)
        if not text or text in seen_labels:
            return
        seen_labels.add(text)
        candidates.append({"ts": _num(ts), "kind": kind, "label": text, "clock": _clock_text(ts)})

    for ev in state.timeline or []:
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        text = str(ev.get("text") or "")
        if kind == "phase":
            push(ev.get("ts"), "phase", text or "阶段切换")
        elif kind in ("denied", "blocked"):
            push(ev.get("ts"), "denied", text or "护栏拦截")
    for finding in state.findings:
        verb = "利用成功" if finding.status == "exploited" else "发现漏洞"
        push(
            finding.first_seen,
            "finding",
            f"{verb} [{SEVERITY_CN.get(finding.severity, finding.severity)}] "
            f"{finding.title or finding.vuln_class} @ {_host_only(finding.url or finding.target)}",
        )
    for cred in state.credentials:
        push(cred.ts, "credential", f"获取凭据 {cred.username}@{cred.target}（{cred.service or '?'}）")
    for flag in state.flags or []:
        if isinstance(flag, dict):
            push(flag.get("ts"), "flag", f"夺取 flag {_short(str(flag.get('value', '')), 24)}")
    for step in state.steps or []:
        if not isinstance(step, dict) or str(step.get("kind") or "tool") != "tool":
            continue
        push(step.get("ts"), "tool", f"{step.get('tool') or '?'} {step.get('summary') or ''}")

    nodes: list[dict[str, Any]] = [
        {
            "ts": _num(state.started_at),
            "kind": "target",
            "label": _mermaid_safe(f"目标 {state.target}"),
            "clock": _clock_text(state.started_at),
        }
    ]

    # 按「阶段 → 漏洞 → 凭据 → 护栏拦截 → 工具」的优先级取样，保证关键节点不被时间序挤掉；
    # 夺旗节点单独留位并固定放在链路末端，形成 侦察→发现→利用→夺旗 的完整故事线。
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        buckets.setdefault(item["kind"], []).append(item)
    flag_nodes = sorted(buckets.get("flag", []), key=lambda item: item["ts"])[:2]
    budget = MAX_PATH_NODES - 1 - len(flag_nodes)
    others: list[dict[str, Any]] = []
    for kind, limit in (("phase", 4), ("finding", 4), ("credential", 1), ("denied", 1), ("tool", 99)):
        others.extend(sorted(buckets.get(kind, []), key=lambda item: item["ts"])[:limit])
    others.sort(key=lambda item: item["ts"])
    if len(others) > budget and budget >= 4:
        head = budget - 3
        tail = budget - 1 - head
        omitted = len(others) - (head + tail)
        others = (
            others[:head]
            + [{"ts": 0.0, "kind": "omitted", "label": f"…（省略 {omitted} 个中间步骤）", "clock": "-"}]
            + others[-tail:]
        )
    nodes.extend(others)
    nodes.extend(flag_nodes)
    for index, node in enumerate(nodes, 1):
        node["id"] = f"n{index}"
        node["kind_cn"] = NODE_KIND_CN.get(node["kind"], node["kind"])

    lines = ["flowchart LR"]
    for node in nodes:
        lines.append(f'    {node["id"]}["{node["label"]}"]')
    for left, right in zip(nodes, nodes[1:]):
        lines.append(f"    {left['id']} --> {right['id']}")
    return {"mermaid": "\n".join(lines), "nodes": nodes, "note": _PATH_NOTE}


# --------------------------------------------------------------------------- 审计
def _chain_result(audit: dict[str, Any]) -> tuple[bool | None, str]:
    """从审计摘要里取哈希链校验结果；没有再尝试就地校验 jsonl 文件。"""
    for key in ("chain_ok", "hash_chain_ok"):
        if isinstance(audit.get(key), bool):
            return bool(audit[key]), str(audit.get("chain_msg") or audit.get("verify_msg") or "")
    verify = audit.get("verify")
    if isinstance(verify, (list, tuple)) and len(verify) == 2:
        return bool(verify[0]), str(verify[1])
    if isinstance(verify, dict) and isinstance(verify.get("ok"), bool):
        return bool(verify["ok"]), str(verify.get("msg") or "")
    chain = audit.get("chain")
    if isinstance(chain, dict) and isinstance(chain.get("ok"), bool):
        return bool(chain["ok"]), str(chain.get("msg") or "")
    path = audit.get("path")
    if path and Path(str(path)).exists():
        try:
            log = AuditLog(path=Path(str(path)), run_id=str(audit.get("run_id") or ""))
            ok, msg = log.verify()
            return bool(ok), msg
        except Exception as exc:  # noqa: BLE001 - 校验失败不应让报告生成失败
            return None, f"校验失败: {exc}"
    return None, ""


def _audit_block(
    audit_summary: dict[str, Any] | None,
    state: RunState,
    include_raw: bool = False,
) -> dict[str, Any] | None:
    if not audit_summary:
        return None
    audit = dict(audit_summary)
    chain_ok, chain_msg = _chain_result(audit)
    path = str(audit.get("path") or "")
    raw_rows: list[list[Any]] = []
    raw_truncated = False
    if include_raw and path and Path(path).exists():
        raw_rows, raw_truncated = _audit_raw_rows(path)
    events = audit.get("events") if isinstance(audit.get("events"), dict) else {}
    tools = audit.get("tools") if isinstance(audit.get("tools"), dict) else {}
    event_rows = [
        [str(name), int(count)]
        for name, count in sorted(events.items(), key=lambda item: (-int(item[1]), str(item[0])))
    ]
    tool_rows = [
        [str(name), int(count)]
        for name, count in sorted(tools.items(), key=lambda item: (-int(item[1]), str(item[0])))
    ]
    kv_rows = [
        ["审计文件", str(audit.get("path") or "-")],
        ["Run ID", str(audit.get("run_id") or state.run_id)],
        ["记录条数", int(audit.get("records") or sum(int(c) for c in events.values()))],
        ["拒绝次数", audit.get("denied") if audit.get("denied") is not None else "-"],
        ["起始时间", _time_text(audit.get("started_at"))],
        ["结束时间", _time_text(audit.get("ended_at"))],
        ["哈希链校验", ("通过 - " if chain_ok else "失败 - " if chain_ok is False else "未提供 - ") + (chain_msg or "无说明")],
    ]
    return {
        "kv_rows": kv_rows,
        "event_rows": event_rows,
        "tool_rows": tool_rows,
        "chain_ok": chain_ok,
        "chain_msg": chain_msg,
        "raw_rows": raw_rows,
        "raw_truncated": raw_truncated,
    }


def _audit_raw_rows(path: str) -> tuple[list[list[Any]], bool]:
    """读取审计 jsonl（只读）并抽出关键列，供 `report.include_raw_audit` 开启时渲染。"""
    rows: list[list[Any]] = []
    truncated = False
    try:
        for record in AuditLog.iter_records(path):
            if len(rows) >= MAX_AUDIT_ROWS:
                truncated = True
                break
            if not isinstance(record, dict):
                continue
            rows.append(
                [
                    record.get("seq", len(rows) + 1),
                    str(record.get("time") or _clock_text(record.get("ts"))),
                    str(record.get("event") or "?"),
                    str(record.get("tool") or "-"),
                    str(record.get("target") or "-"),
                    str(record.get("decision") or "-"),
                    _short(record.get("reason") or record.get("summary") or record.get("error") or "", 120) or "-",
                ]
            )
    except OSError:
        return [], False
    return rows, truncated


# --------------------------------------------------------------------------- 明细
def _sorted_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 99), -_num(f.last_seen)))


def _finding_data(finding: Finding, cfg: TrinaConfig) -> dict[str, Any]:
    cls_key = _class_key(finding.vuln_class)
    cvss = str(finding.cvss or "").strip()
    cvss_source = "provided" if cvss else "default"
    if not cvss:
        cvss = default_cvss(finding.vuln_class)
    cwe = str(finding.cwe or "").strip()
    cwe_source = "provided" if cwe else "default"
    if not cwe:
        cwe = default_cwe(finding.vuln_class) or "-"
        cwe_source = "default" if cwe != "-" else "none"
    remediation = str(finding.remediation or "").strip()
    remediation_source = "provided" if remediation else "default"
    if not remediation:
        remediation = default_remediation(finding.vuln_class)

    evidence = [_evidence_row(ev) for ev in (finding.evidence or []) if isinstance(ev, dict)]
    commands: list[str] = []
    for row in evidence:
        if row["command"] and row["command"] not in commands:
            commands.append(row["command"])
    return {
        "id": finding.id,
        "title": finding.title or finding.vuln_class or "未命名漏洞",
        "vuln_class": finding.vuln_class or "unknown",
        "vuln_class_key": cls_key,
        "target": finding.target or "",
        "url": finding.url or "",
        "location": finding.url or finding.target or "-",
        "parameter": finding.parameter or "-",
        "method": finding.method or "-",
        "severity": finding.severity,
        "severity_cn": SEVERITY_CN.get(finding.severity, finding.severity),
        "confidence": finding.confidence,
        "confidence_cn": CONFIDENCE_CN.get(finding.confidence, finding.confidence),
        "status": finding.status,
        "status_cn": FINDING_STATUS_CN.get(finding.status, finding.status),
        "cvss": cvss,
        "cvss_source": cvss_source,
        "cvss_version": cfg.report.cvss_version,
        "cwe": cwe,
        "cwe_source": cwe_source,
        "description": finding.description or "（未提供描述，建议人工复核该发现）",
        "impact": finding.impact or "（未提供影响说明）",
        "remediation": remediation,
        "remediation_source": remediation_source,
        "tool": finding.tool or "-",
        "payload": finding.payload or "",
        "refs": [str(ref) for ref in (finding.refs or [])],
        "evidence": evidence,
        "commands": commands[:3],
        "first_seen_text": _time_text(finding.first_seen),
        "last_seen_text": _time_text(finding.last_seen),
    }


def _host_data(state: RunState) -> list[dict[str, Any]]:
    hosts: list[dict[str, Any]] = []
    for host, hs in sorted(state.hosts.items()):
        ports = [
            {
                "port": int(port),
                "protocol": str(info.get("protocol") or "tcp"),
                "service": str(info.get("service") or ""),
                "version": str(info.get("version") or ""),
                "banner": str(info.get("banner") or ""),
            }
            for port, info in sorted(hs.ports.items())
        ]
        hosts.append(
            {
                "host": host,
                "alive": hs.alive,
                "alive_cn": "是" if hs.alive else "否" if hs.alive is False else "未知",
                "ports": ports,
                "port_count": len(ports),
                "technologies": [str(t) for t in hs.technologies],
                "web_paths": [str(p) for p in hs.web_paths],
                "notes": [str(n) for n in hs.notes],
            }
        )
    return hosts


def _step_data(state: RunState) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for raw in state.steps or []:
        if not isinstance(raw, dict):
            continue
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        params_text = " ".join(f"{k}={_short(v, 60)}" for k, v in params.items()) or "-"
        if params_text == "-":
            for alt in ("args", "command", "argv"):
                if raw.get(alt):
                    params_text = _short(raw[alt], 160)
                    break
        steps.append(
            {
                "clock": str(raw.get("clock") or _clock_text(raw.get("ts"))),
                "kind": str(raw.get("kind") or "tool"),
                "tool": str(raw.get("tool") or raw.get("kind") or "?"),
                "params": params_text,
                "ok": None if raw.get("ok") is None else bool(raw.get("ok")),
                "summary": _short(raw.get("summary") or raw.get("observation") or raw.get("thought") or "", 200) or "-",
                "duration": f"{_num(raw.get('duration')):.2f}s",
            }
        )
    return steps


def _timeline_data(state: RunState) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for raw in state.timeline or []:
        if not isinstance(raw, dict):
            continue
        timeline.append(
            {
                "clock": str(raw.get("clock") or _clock_text(raw.get("ts"))),
                "kind": str(raw.get("kind") or "event"),
                "text": _short(raw.get("text") or "", 200),
            }
        )
    return timeline


# --------------------------------------------------------------------------- 汇总
def _evidence_graph(state: RunState, hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
    """证据关系图（节点/边 + mermaid 源码）；构建失败不影响报告生成。"""
    empty = {"available": False, "mermaid": "", "summary": "", "node_rows": [], "edge_rows": []}
    try:
        from ..evidence_graph import build as build_graph

        graph = build_graph(state, hypotheses=hypotheses)
    except Exception:  # noqa: BLE001 - 报告宁可少一节也不能失败
        return empty
    try:
        payload = graph.to_dict()
        kind_cn = {
            "host": "主机", "port": "端口", "service": "服务", "path": "路径", "web_path": "路径",
            "finding": "漏洞", "evidence": "证据", "flag": "夺旗", "credential": "凭据",
            "hypothesis": "假设", "check": "检查项", "target": "目标",
        }
        return {
            "available": True,
            "mermaid": graph.to_mermaid(),
            "summary": graph.summary(),
            "node_rows": [
                [node["id"], kind_cn.get(node.get("kind", ""), node.get("kind", "")), node.get("label", "")]
                for node in payload["nodes"]
            ],
            "edge_rows": [
                [edge["src"], edge.get("label", ""), edge["dst"]] for edge in payload["edges"]
            ],
        }
    except Exception:  # noqa: BLE001
        return empty


def collect_data(
    state: RunState,
    cfg: TrinaConfig,
    *,
    meta: dict[str, Any] | None = None,
    audit_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把运行状态与配置汇总成结构化报告数据（两个渲染器共用）。"""
    meta = dict(meta or {})
    counters = dict(state.counters or {})
    usage = _usage(state.ledger)
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    title = str(meta.get("title") or cfg.report.title or "Trina 自动渗透测试报告")
    status = str(state.status or "running")
    duration = _num(state.duration)

    hosts = _host_data(state)
    steps = _step_data(state)
    timeline = _timeline_data(state)
    findings = [_finding_data(f, cfg) for f in _sorted_findings(state.findings)]

    severity_counts: dict[str, int] = {}
    for finding in state.findings:
        key = str(finding.severity or "info")
        severity_counts[key] = severity_counts.get(key, 0) + 1
    severity_rows = [
        {
            "severity": sev,
            "severity_cn": SEVERITY_CN.get(sev, sev),
            "count": severity_counts.get(sev, 0),
            "pct": _pct(severity_counts.get(sev, 0), len(state.findings)),
        }
        for sev in SEVERITIES
    ]
    for extra in sorted(set(severity_counts) - set(SEVERITIES)):
        severity_rows.append(
            {
                "severity": extra,
                "severity_cn": SEVERITY_CN.get(extra, extra),
                "count": severity_counts[extra],
                "pct": _pct(severity_counts[extra], len(state.findings)),
            }
        )

    http_requests: int | None = None
    http_source = ""
    for key in ("http_requests", "http", "http_calls", "requests"):
        value = counters.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            http_requests, http_source = value, "counters"
            break
    if http_requests is None and isinstance(state.ledger, dict):
        value = state.ledger.get("http_requests")
        if isinstance(value, int) and not isinstance(value, bool):
            http_requests, http_source = value, "ledger"
    if http_requests is None:
        http_evidence = [ev for ev in (state.evidence or []) if isinstance(ev, dict) and str(ev.get("kind")) == "http"]
        if http_evidence:
            http_requests, http_source = len(http_evidence), "evidence（受证据上限约束）"

    counter_steps = counters.get("steps")
    if not isinstance(counter_steps, int) or isinstance(counter_steps, bool) or counter_steps <= 0:
        counter_steps = len(steps)
    counter_tools = counters.get("tool_calls")
    if not isinstance(counter_tools, int) or isinstance(counter_tools, bool) or counter_tools <= 0:
        counter_tools = len([s for s in steps if s["kind"] == "tool"])
    counter_llm = counters.get("llm_calls")
    if not isinstance(counter_llm, int) or isinstance(counter_llm, bool) or counter_llm <= 0:
        counter_llm = usage["calls"]
    denied = counters.get("denied") if isinstance(counters.get("denied"), int) else 0
    errors = counters.get("errors") if isinstance(counters.get("errors"), int) else 0

    port_total = sum(host["port_count"] for host in hosts)
    metrics: dict[str, Any] = {
        "duration_s": duration,
        "duration_text": _duration_text(duration),
        "steps": counter_steps,
        "tool_calls": counter_tools,
        "llm_calls": counter_llm,
        "cost_usd": usage["cost_usd"],
        "denied": int(denied or 0),
        "errors": int(errors or 0),
        "hosts": len(hosts),
        "ports": port_total,
        "credentials": len(state.credentials),
        "flags": len(state.flags or []),
        "findings": len(state.findings),
        "http_requests": http_requests,
        "http_requests_source": http_source,
    }

    metric_rows: list[list[Any]] = [
        ["运行耗时", metrics["duration_text"]],
        ["总步数", metrics["steps"]],
        ["工具调用次数", metrics["tool_calls"]],
        ["LLM 调用次数", metrics["llm_calls"]],
        ["LLM 成本（美元）", f"{metrics['cost_usd']:.6f}"],
        ["护栏拒绝次数", metrics["denied"]],
        ["错误计数", metrics["errors"]],
        ["主机数", metrics["hosts"]],
        ["开放端口数", metrics["ports"]],
        ["凭据数", metrics["credentials"]],
        ["Flag 数", metrics["flags"]],
        ["漏洞数", metrics["findings"]],
    ]
    if metrics["http_requests"] is not None:
        metric_rows.append(["HTTP 请求数", f"{metrics['http_requests']}（来源: {metrics['http_requests_source']}）"])

    scope = cfg.scope
    audit = _audit_block(audit_summary, state, include_raw=bool(getattr(cfg.report, "include_raw_audit", False)))
    audit_denied = audit_summary.get("denied") if isinstance(audit_summary, dict) else None
    compliance = {
        "statement": cfg.scope_prompt_block(),
        "authorized_cidrs": list(scope.authorized_cidrs),
        "authorized_hosts": list(scope.authorized_hosts),
        "deny_cidrs": list(scope.deny_cidrs),
        "allow_public": bool(scope.allow_public),
        "allow_loopback": bool(scope.allow_loopback),
        "exploit_level": scope.exploit_level,
        "destructive": bool(scope.destructive),
        "destructive_hosts": list(scope.destructive_hosts),
        "max_qps": scope.max_qps,
        "max_host_concurrency": scope.max_host_concurrency,
        "max_brute_attempts": scope.max_brute_attempts,
        "denied_state": int(denied or 0),
        "denied_audit": audit_denied,
    }
    compliance_rows: list[list[Any]] = [
        ["授权网段", ", ".join(compliance["authorized_cidrs"]) or "（无）"],
        ["额外授权主机", ", ".join(compliance["authorized_hosts"]) or "（无）"],
        ["显式拒绝网段", ", ".join(compliance["deny_cidrs"]) or "（无）"],
        ["允许公网目标", _yesno(compliance["allow_public"])],
        ["允许回环地址", _yesno(compliance["allow_loopback"])],
        [
            "最大动作等级",
            f"{compliance['exploit_level']}（detect 只读 / verify 可验证 / exploit 可执行命令）",
        ],
        [
            "破坏性动作",
            _yesno(compliance["destructive"])
            + (
                f"，白名单: {', '.join(compliance['destructive_hosts'])}"
                if compliance["destructive"] and compliance["destructive_hosts"]
                else ""
            ),
        ],
        [
            "速率与规模上限",
            f"{compliance['max_qps']} req/s，单主机并发 {compliance['max_host_concurrency']}，"
            f"爆破尝试上限 {compliance['max_brute_attempts']} 次",
        ],
        ["护栏拒绝次数（运行态）", compliance["denied_state"]],
        ["护栏拒绝次数（审计）", compliance["denied_audit"] if compliance["denied_audit"] is not None else "（未提供审计摘要）"],
    ]

    asset_rows: list[list[Any]] = []
    for host in hosts:
        ports = (
            "; ".join(
                f"{p['port']}/{p['protocol']} {p['service'] or '?'}" + (f" {p['version']}" if p["version"] else "")
                for p in host["ports"]
            )
            or "-"
        )
        asset_rows.append(
            [
                host["host"],
                host["alive_cn"],
                ports,
                ", ".join(host["technologies"]) or "-",
                "; ".join(_limit(host["web_paths"], 12)) or "-",
            ]
        )

    finding_rows = [
        [f["id"], f["title"], f["severity_cn"], f["confidence_cn"], f["status_cn"], f["vuln_class"], f["location"]]
        for f in findings
    ]
    credential_rows = [
        [
            cred.target,
            cred.service or "-",
            cred.username or "-",
            _mask_secret(cred.password),
            cred.source or "-",
            "是" if cred.verified else "否",
        ]
        for cred in state.credentials
    ]
    flag_rows = [
        [
            index,
            str(flag.get("value", "")) if isinstance(flag, dict) else str(flag),
            str(flag.get("source", "")) if isinstance(flag, dict) else "-",
            _clock_text(flag.get("ts")) if isinstance(flag, dict) else "-",
        ]
        for index, flag in enumerate(state.flags or [], 1)
    ]
    step_rows = [[s["clock"], s["tool"], s["params"], s["summary"], s["duration"]] for s in steps[:MAX_STEP_ROWS]]

    cover_rows: list[list[Any]] = [
        ["报告标题", title],
        ["目标", state.target],
        ["目标说明", state.goal or "-"],
        ["Run ID", state.run_id],
        ["运行模式", state.mode or "-"],
        ["开始时间", _time_text(state.started_at)],
        ["结束时间", _time_text(state.ended_at) if state.ended_at else "-（运行中）"],
        ["耗时", _duration_text(duration)],
        ["最终状态", RUN_STATUS_CN.get(status, status)],
        ["Agent 后端 / 模型", f"{cfg.llm.backend} / {usage['model'] or cfg.llm.model}"],
        ["LLM 调用次数", usage["calls"]],
        [
            "Token 用量",
            f"输入 {usage['input_tokens']} / 输出 {usage['output_tokens']} / 缓存读 {usage['cache_read_tokens']}",
        ],
        ["LLM 成本（美元）", f"{usage['cost_usd']:.6f}"],
        ["报告生成时间", generated_at],
        ["生成器", f"Trina {__version__}"],
    ]
    for key, value in meta.items():
        if str(key) == "title":
            continue
        cover_rows.append([f"元信息 · {key}", _short(value, 200)])

    summary_rows: list[list[Any]] = [[row["severity_cn"], row["count"], row["pct"]] for row in severity_rows]
    summary_rows.append(["合计", len(state.findings), "100.0%" if state.findings else "-"])

    hypotheses = list(getattr(state, "hypotheses", None) or [])
    hypothesis_rows: list[list[Any]] = [
        [
            item.get("id", ""),
            HYPOTHESIS_STATUS_CN.get(str(item.get("status", "")), str(item.get("status", ""))),
            f"{float(item.get('confidence', 0) or 0):.2f}",
            f"{float(item.get('priority', 0) or 0):.2f}",
            _short(item.get("statement", ""), 140),
            _short(item.get("next_step", "") or "-", 80),
            len(item.get("evidence") or []),
        ]
        for item in hypotheses
    ]
    hypothesis_counts = {
        key: len([h for h in hypotheses if h.get("status") == key])
        for key in ("open", "verifying", "confirmed", "refuted", "blocked")
    }

    data: dict[str, Any] = {
        "title": title,
        "generated_at": generated_at,
        "generator": f"Trina {__version__}",
        "run_id": state.run_id,
        "target": state.target,
        "goal": state.goal or "",
        "mode": state.mode or "",
        "status": status,
        "status_cn": RUN_STATUS_CN.get(status, status),
        "phase": state.phase,
        "started_at": _num(state.started_at),
        "ended_at": _num(state.ended_at) if state.ended_at else None,
        "started_text": _time_text(state.started_at),
        "ended_text": _time_text(state.ended_at) if state.ended_at else "-",
        "duration": duration,
        "duration_text": _duration_text(duration),
        "sections": dict(SECTION_TITLES),
        "meta": {str(k): _short(v, 200) for k, v in meta.items() if str(k) != "title"},
        "agent": {"backend": cfg.llm.backend, "model": usage["model"] or cfg.llm.model, "usage": usage},
        "cover_rows": cover_rows,
        "severity_counts": severity_counts,
        "severity_rows": severity_rows,
        "summary_rows": summary_rows,
        "metrics": metrics,
        "metric_rows": metric_rows,
        "compliance": compliance,
        "compliance_rows": compliance_rows,
        "attack_path": _attack_path(state),
        "hosts": hosts,
        "asset_rows": asset_rows,
        "findings": findings,
        "finding_rows": finding_rows,
        "hypotheses": hypotheses,
        "hypothesis_rows": hypothesis_rows,
        "hypothesis_counts": hypothesis_counts,
        "scheduler": dict(getattr(state, "scheduler", None) or {}),
        "evidence_graph": _evidence_graph(state, hypotheses),
        "credentials": credential_rows,
        "flags": flag_rows,
        "steps": steps,
        "step_rows": step_rows,
        "steps_truncated": len(steps) > MAX_STEP_ROWS,
        "timeline": timeline,
        "audit": audit,
        "counters": counters,
        "include_raw_audit": bool(getattr(cfg.report, "include_raw_audit", False)),
        "include_timeline": bool(getattr(cfg.report, "include_timeline", True)),
    }
    return _clean(data)


# --------------------------------------------------------------------------- 交付物
@dataclass
class Report:
    """一次运行的报告成品：Markdown / HTML / 结构化数据，可落盘为 .md/.html/.json。"""

    title: str
    generated_at: str
    markdown: str
    html: str
    data: dict[str, Any]
    #: `save()` 未显式指定目录时的默认输出目录（由 build_report 从 cfg.report_dir 写入）。
    default_dir: str = ""
    #: `save()` 未显式指定格式时的默认格式（由 build_report 从 cfg.report.formats 写入）。
    default_formats: list[str] = field(default_factory=lambda: ["markdown", "html"])

    def save(
        self,
        out_dir: str | Path | None = None,
        formats: list[str] | None = None,
        stem: str | None = None,
    ) -> list[Path]:
        """写盘：默认 ``<cfg.report_dir>/<run_id>_report.{md,html,json}``，返回写入的文件路径列表。"""
        directory = Path(out_dir) if out_dir else Path(self.default_dir or "reports")
        directory.mkdir(parents=True, exist_ok=True)
        stem = stem or f"{self.data.get('run_id') or 'run'}_report"
        wanted = list(formats) if formats else list(self.default_formats or ["markdown", "html"])
        written: list[Path] = []
        for raw in wanted:
            key = _FORMAT_ALIAS.get(str(raw).strip().lower(), str(raw).strip().lower())
            ext = _FORMAT_EXT.get(key)
            if ext is None:
                raise ValueError(f"未支持的报告格式 {raw!r}，可选: markdown/md, html, json")
            path = directory / f"{stem}{ext}"
            if key == "markdown":
                path.write_text(self.markdown, encoding="utf-8")
            elif key == "html":
                path.write_text(self.html, encoding="utf-8")
            else:
                path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            written.append(path)
        return written


def build_report(
    state: RunState,
    cfg: TrinaConfig,
    *,
    meta: dict[str, Any] | None = None,
    audit_summary: dict[str, Any] | None = None,
) -> Report:
    """构建报告（`meta["title"]` 可覆盖标题；`audit_summary` 来自 `AuditLog.summary()`）。"""
    data = collect_data(state, cfg, meta=meta, audit_summary=audit_summary)
    return Report(
        title=str(data["title"]),
        generated_at=str(data["generated_at"]),
        markdown=render_markdown(data),
        html=render_html(data),
        data=data,
        default_dir=str(cfg.report_dir),
        default_formats=[str(fmt) for fmt in (cfg.report.formats or ["markdown", "html"])],
    )
