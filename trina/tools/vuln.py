"""漏洞层工具：内置只读 PoC 扫描与 nuclei 外部引擎封装（缺失时降级）。

原则
----
- **只读**：内置 PoC 全部是 GET，仅做信息泄露 / 未授权访问类匹配，不发送破坏性载荷。
- **外部优先、内置兜底**：`nuclei_scan` 检测到 `nuclei` 二进制则调用之，否则复用本模块
  内置的 PoC 表（`poc_scan` 的逻辑），并在 `data.engine` 中标注实际引擎与覆盖边界。
- **护栏优先**：所有请求经 `ctx.http`（外部命令经 `run_external`），自动过范围/等级校验。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..scope import ScopeViolation
from ..state import SEVERITIES
from .external import run_external, which
from .registry import ParamSpec, Tool, ToolResult, ToolSpec, denied_result, register

# ---------------------------------------------------------------------------
# PoC 定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoC:
    """一个只读 PoC：路径 + 匹配器（命中任一正向条件且状态码符合即判定）。"""

    id: str
    name: str
    vuln_class: str
    severity: str
    path: str
    cwe: str = ""
    status: tuple[int, ...] = ()
    body_any: tuple[str, ...] = ()
    regex: tuple[str, ...] = ()
    header_any: tuple[str, ...] = ()
    ctype_any: tuple[str, ...] = ()
    body_not: tuple[str, ...] = ()
    description: str = ""
    remediation: str = ""


POCS: tuple[PoC, ...] = (
    PoC(
        "git_leak", ".git 目录可公开访问", "info_disclosure", "high", ".git/HEAD",
        cwe="CWE-538", status=(200,), body_any=("ref: refs/", "ref:refs/"),
        description=".git/HEAD 可未授权访问，攻击者可据此还原完整源码与提交历史。",
        remediation="部署时移除版本控制元数据，或在 Web 入口屏蔽以 . 开头的路径。",
    ),
    PoC(
        "svn_leak", ".svn 目录可公开访问", "info_disclosure", "high", ".svn/entries",
        cwe="CWE-538", status=(200,), body_any=("svn", "dir", "<?xml"),
        body_not=("<html", "<!doctype", "<head"),
        description=".svn/entries 可访问，源码与元数据存在泄露风险。",
        remediation="移除 .svn 目录并禁止访问隐藏目录。",
    ),
    PoC(
        "env_leak", ".env 配置文件泄露", "info_disclosure", "high", ".env",
        cwe="CWE-538", status=(200,), regex=(r"(?m)^[A-Za-z_][A-Za-z0-9_]*\s*=",),
        body_not=("<html", "<!doctype", "<head"),
        description=".env 文件可访问，可能泄露数据库口令、API Key 等敏感配置。",
        remediation="将 .env 移出 Web 根目录并屏蔽其访问；轮换已泄露的密钥。",
    ),
    PoC(
        "backup_php_bak", "PHP 源码备份文件泄露", "backup_leak", "high", "index.php.bak",
        cwe="CWE-530", status=(200,), body_any=("<?php", "<?="), body_not=("<html",),
        description="index.php 的备份文件可访问，直接暴露服务端源码。",
        remediation="禁止 Web 根目录下的 .bak/.old/.swp 等备份文件访问。",
    ),
    PoC(
        "backup_config_bak", "配置文件备份泄露", "backup_leak", "high", "config.php.bak",
        cwe="CWE-530", status=(200,), body_any=("<?php", "password", "db_"), body_not=("<html",),
        description="config.php 备份文件可访问，可能泄露数据库连接凭据。",
        remediation="清理备份文件，部署流程加入敏感后缀黑名单。",
    ),
    PoC(
        "backup_www_zip", "站点打包文件泄露 (www.zip)", "backup_leak", "high", "www.zip",
        cwe="CWE-530", status=(200,), ctype_any=("zip",), body_any=("PK\x03\x04",),
        description="整站打包 www.zip 可下载，源码与配置全量泄露。",
        remediation="删除发布残留的压缩包；对静态资源目录做后缀黑名单。",
    ),
    PoC(
        "backup_web_zip", "站点打包文件泄露 (web.zip)", "backup_leak", "high", "web.zip",
        cwe="CWE-530", status=(200,), ctype_any=("zip",), body_any=("PK\x03\x04",),
        description="整站打包 web.zip 可下载，源码与配置全量泄露。",
        remediation="删除发布残留的压缩包；对静态资源目录做后缀黑名单。",
    ),
    PoC(
        "phpinfo", "phpinfo() 信息泄露", "info_disclosure", "low", "phpinfo.php",
        cwe="CWE-200", status=(200,), body_any=("phpinfo()", "PHP Version", "PHP Credits", "PHP License"),
        description="phpinfo 页面可访问，暴露 PHP 版本、模块、环境变量等敏感信息。",
        remediation="删除生产环境的 phpinfo 等调试页面。",
    ),
    PoC(
        "actuator_env", "Spring Actuator env 端点未授权", "info_disclosure", "high", "actuator/env",
        cwe="CWE-200", status=(200,), body_any=('"propertySources"', '"activeProfiles"', '"systemProperties"'),
        description="Actuator /env 未授权可读，可能泄露数据库口令与内部配置。",
        remediation="关闭或鉴权 Actuator 端点（management.endpoints.web.exposure.include）。",
    ),
    PoC(
        "actuator_heapdump", "Spring Actuator heapdump 未授权", "info_disclosure", "high", "actuator/heapdump",
        cwe="CWE-200", status=(200,), ctype_any=("octet-stream",), body_any=("JAVA PROFILE",),
        description="堆转储文件可下载，其中可能包含明文凭据与内存中的敏感数据。",
        remediation="关闭 /actuator/heapdump 或加鉴权与网络访问控制。",
    ),
    PoC(
        "swagger_ui", "Swagger UI 未授权访问", "info_disclosure", "medium", "swagger-ui.html",
        cwe="CWE-200", status=(200,), body_any=("swagger-ui", "SwaggerUIBundle", "Swagger UI"),
        description="Swagger UI 对外可访问，暴露完整 API 接口清单。",
        remediation="生产环境关闭 API 文档或加访问控制。",
    ),
    PoC(
        "swagger_api_docs", "Swagger api-docs 泄露", "info_disclosure", "medium", "v2/api-docs",
        cwe="CWE-200", status=(200,), body_any=('"swagger"', '"basePath"', '"openapi"'),
        description="api-docs JSON 可访问，泄露接口、参数与数据模型。",
        remediation="生产环境关闭 springfox/knife4j 文档端点。",
    ),
    PoC(
        "druid_unauth", "Druid 监控页未授权", "auth_bypass", "medium", "druid/index.html",
        cwe="CWE-306", status=(200,), body_any=("Druid Stat", "DruidDataSource", "druid"),
        description="Druid 监控台未授权可访问，泄露 SQL 与数据源信息。",
        remediation="为 Druid 监控台配置登录与访问控制。",
    ),
    PoC(
        "tomcat_manager", "Tomcat Manager 可访问", "misconfig", "medium", "manager/html",
        cwe="CWE-306", status=(200,), body_any=("Tomcat Manager", "/manager/html", "Tomcat Web Application Manager"),
        description="Tomcat Manager 页面可访问，存在弱口令爆破与部署 war 包风险。",
        remediation="限制 Manager 访问来源并设置强口令或直接下线。",
    ),
    PoC(
        "dir_listing_uploads", "目录列表 (AutoIndex) - uploads", "misconfig", "medium", "uploads/",
        cwe="CWE-548", status=(200,), body_any=("Index of /", "<title>Index of"),
        description="uploads 目录开启自动索引，可枚举上传文件与敏感附件。",
        remediation="关闭 Apache/Nginx 目录索引（Options -Indexes / autoindex off）。",
    ),
    PoC(
        "dir_listing_backup", "目录列表 (AutoIndex) - backup", "misconfig", "medium", "backup/",
        cwe="CWE-548", status=(200,), body_any=("Index of /", "<title>Index of"),
        description="backup 目录开启自动索引，可枚举备份文件。",
        remediation="关闭目录索引并移除线上备份目录。",
    ),
    PoC(
        "crossdomain_xml", "crossdomain.xml 配置宽松", "misconfig", "low", "crossdomain.xml",
        cwe="CWE-942", status=(200,), body_any=("<cross-domain-policy", "allow-access-from"),
        description="跨域策略文件存在，若域名通配将放大 Flash/跨域读取风险。",
        remediation="收紧 allow-access-from domain 白名单或移除该文件。",
    ),
    PoC(
        "apache_server_status", "Apache server-status 暴露", "info_disclosure", "low", "server-status",
        cwe="CWE-200", status=(200,), body_any=("Apache Server Status", "Server Version:", "Total Accesses"),
        description="server-status 未授权可读，暴露当前请求、客户端 IP 与版本信息。",
        remediation="限制 server-status 的 Require ip 白名单或直接关闭。",
    ),
    PoC(
        "thinkphp_debug", "ThinkPHP 调试模式信息泄露", "info_disclosure", "low", "index.php?s=index/index/index",
        cwe="CWE-200", status=(200,), body_any=("ThinkPHP", "think\\Exception", "Think\\Exception"),
        header_any=("ThinkPHP",),
        description="ThinkPHP 应用开启调试模式，异常页泄露路径、版本与调用栈。",
        remediation="生产环境关闭 app_debug 并统一异常处理页面。",
    ),
    PoC(
        "phpmyadmin", "phpMyAdmin 可访问", "misconfig", "info", "phpmyadmin/",
        cwe="CWE-200", status=(200,), body_any=("phpMyAdmin", "pma_", "phpmyadmin"),
        description="phpMyAdmin 面板对外可访问，存在弱口令与历史漏洞风险。",
        remediation="限制访问来源、启用强口令并保持版本更新。",
    ),
    PoC(
        "wp_login", "WordPress 登录入口暴露", "info", "info", "wp-login.php",
        cwe="CWE-200", status=(200,), body_any=("wp-login", "user_login", "WordPress"),
        description="WordPress 登录页可访问，暴露 CMS 类型便于针对性攻击。",
        remediation="限制后台访问来源、启用 2FA 与登录限速。",
    ),
)

POC_INDEX: dict[str, PoC] = {poc.id: poc for poc in POCS}
DEFAULT_NUCLEI_SEVERITY: tuple[str, ...] = ("low", "medium", "high", "critical")


# ---------------------------------------------------------------------------
# 匹配与执行
# ---------------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "http://" + url
    return url


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def _match(poc: PoC, res: Any) -> bool:
    """判定 PoC 是否命中：状态码符合且命中任一正向匹配器，且不触发排除条件。"""
    if res.error or not res.status:
        return False
    body = res.body or ""
    if poc.status and res.status not in poc.status:
        return False
    if poc.body_not and any(token in body for token in poc.body_not):
        return False
    headers = {k.lower(): str(v or "") for k, v in (res.headers or {}).items()}
    ctype = headers.get("content-type", "")
    header_joined = " ".join(headers.values())

    if poc.body_any and any(token in body for token in poc.body_any):
        return True
    if poc.regex and any(re.search(pattern, body, re.I | re.M) for pattern in poc.regex):
        return True
    if poc.ctype_any and any(token.lower() in ctype.lower() for token in poc.ctype_any):
        return True
    if poc.header_any and any(token.lower() in header_joined.lower() for token in poc.header_any):
        return True
    return False


def _run_poc_scan(ctx, target: str, poc_ids: Any = None, severities: Any = None) -> dict[str, Any]:
    """内置 PoC 扫描核心：返回 {selected, hits[(poc, res)], checked, unknown_ids}。"""
    ids = {str(i) for i in poc_ids} if poc_ids else None
    sevs = {str(s).lower() for s in severities} if severities else None
    unknown = (sorted(ids - set(POC_INDEX)) if ids else [])
    selected = [
        poc
        for poc in POCS
        if (ids is None or poc.id in ids) and (sevs is None or poc.severity in sevs)
    ]
    hits: list[tuple[PoC, Any]] = []
    checked = 0
    for poc in selected:
        url = urljoin(target.rstrip("/") + "/", poc.path)
        res = ctx.http.get(url, required_level="detect", payload_label=f"poc:{poc.id}")
        checked += 1
        if _match(poc, res):
            hits.append((poc, res))
    return {"selected": selected, "hits": hits, "checked": checked, "unknown_ids": unknown}


def _build_poc_result(
    target: str,
    selected: list[PoC],
    hits: list[tuple[PoC, Any]],
    checked: int,
    *,
    engine: str = "builtin",
    unknown_ids: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> ToolResult:
    findings: list[dict[str, Any]] = []
    facts_paths: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for poc, res in hits:
        reproduce = f"curl -sk -i '{res.url}'"
        findings.append(
            {
                "id": "",
                "title": poc.name,
                "vuln_class": poc.vuln_class,
                "severity": poc.severity,
                "status": "detected",
                "confidence": "high",
                "target": target,
                "url": res.url,
                "method": "GET",
                "tool": "poc_scan" if engine == "builtin" else "nuclei_scan",
                "cwe": poc.cwe,
                "description": poc.description,
                "remediation": poc.remediation,
                "evidence": [
                    res.as_evidence(note=f"PoC {poc.id} 命中", poc_id=poc.id, severity=poc.severity, cwe=poc.cwe, reproduce=reproduce)
                ],
            }
        )
        facts_paths.append({"host": _host_of(res.url), "url": res.url, "status": int(res.status)})
        evidence.append(
            res.as_evidence(note=f"PoC {poc.id}（{poc.severity}）命中", poc_id=poc.id, cwe=poc.cwe, reproduce=reproduce)
        )

    if hits:
        detail = ", ".join(f"{poc.id}({poc.severity})" for poc, _ in hits[:8])
        summary = f"命中 {len(hits)} 个 PoC: {detail}"
    else:
        summary = f"未命中 PoC（已检测 {checked} 项）"

    data: dict[str, Any] = {
        "engine": engine,
        "target": target,
        "checked": checked,
        "poc_total": len(selected),
        "hit_count": len(hits),
        "matched_ids": [poc.id for poc, _ in hits],
        "hits": [
            {
                "id": poc.id,
                "name": poc.name,
                "severity": poc.severity,
                "cwe": poc.cwe,
                "url": res.url,
                "status": int(res.status),
                "reproduce": f"curl -sk -i '{res.url}'",
            }
            for poc, res in hits
        ],
    }
    if engine == "builtin":
        data["coverage"] = f"内置 {len(POCS)} 个只读 GET PoC（信息泄露/备份泄露/未授权/目录列表等），不含 nuclei 模板引擎"
    if unknown_ids:
        data["unknown_poc_ids"] = unknown_ids
    if extra:
        data.update(extra)

    return ToolResult(
        ok=True,
        category="vuln",
        summary=summary,
        data=data,
        facts={"findings": findings, "paths": facts_paths},
        evidence=evidence[:10],
        next_hints=(
            ["对高危命中项人工确认可利用性，并规划后续利用链"]
            if hits
            else ["换用其他路径字典或对 web_surface 提取的接口做针对性验证"]
        ),
    )


# ---------------------------------------------------------------------------
# nuclei JSONL 解析
# ---------------------------------------------------------------------------


def _parse_nuclei_jsonl(text: str, target: str) -> tuple[list[dict[str, Any]], int]:
    """把 nuclei -jsonl 输出解析成 Finding 字典列表。返回 (findings, 解析行数)。"""
    findings: list[dict[str, Any]] = []
    parsed = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        parsed += 1
        info = obj.get("info") or {}
        template_id = str(obj.get("template-id") or obj.get("templateID") or obj.get("template") or "")
        name = str(info.get("name") or template_id or "nuclei-finding")
        severity = str(info.get("severity") or "info").lower()
        if severity not in SEVERITIES:
            severity = "info"
        matched = str(obj.get("matched-at") or obj.get("matched") or obj.get("host") or target)
        reference = info.get("reference")
        if isinstance(reference, list):
            refs = [str(r) for r in reference if r]
        elif reference:
            refs = [str(reference)]
        else:
            refs = []
        curl = str(obj.get("curl-command") or "")
        findings.append(
            {
                "id": "",
                "title": f"[nuclei] {name}",
                "vuln_class": template_id or "nuclei_finding",
                "severity": severity,
                "status": "detected",
                "confidence": "high",
                "target": target,
                "url": matched,
                "method": "GET",
                "tool": "nuclei_scan",
                "description": str(info.get("description") or ""),
                "remediation": str(info.get("remediation") or ""),
                "refs": refs,
                "evidence": [
                    {
                        "kind": "nuclei",
                        "target": matched,
                        "template": template_id,
                        "severity": severity,
                        "extracted": obj.get("extracted-results") or [],
                        "curl": curl,
                        "reproduce": curl or f"nuclei -u {target} -t {template_id}",
                    }
                ],
            }
        )
    return findings, parsed


# ---------------------------------------------------------------------------
# 工具 4：内置 PoC 扫描
# ---------------------------------------------------------------------------


class PocScanTool(Tool):
    """内置只读 PoC 合集（信息泄露 / 备份泄露 / 未授权 / 目录列表等）。"""

    spec = ToolSpec(
        name="poc_scan",
        summary="内置只读 PoC 扫描：覆盖 .git/.env/备份文件/Actuator/Swagger/Druid/phpinfo 等常见信息泄露与未授权",
        category="vuln",
        action_level="detect",
        params={
            "url": ParamSpec("str", "目标 base URL", required=True),
            "poc_ids": ParamSpec("list", "只跑指定 PoC id（默认全部）", required=False),
        },
        produces=["findings", "paths"],
        examples=["poc_scan url=http://10.0.0.5:8080", "poc_scan url=http://10.0.0.5 poc_ids=[git_leak,env_leak]"],
        notes=f"共 {len(POCS)} 个 PoC，全部为 GET 只读请求。",
    )

    def run(self, ctx, url: str, poc_ids: Any = None) -> ToolResult:
        target = _normalize_url(url)
        outcome = _run_poc_scan(ctx, target, poc_ids)
        return _build_poc_result(
            target,
            outcome["selected"],
            outcome["hits"],
            outcome["checked"],
            engine="builtin",
            unknown_ids=outcome["unknown_ids"],
        )


# ---------------------------------------------------------------------------
# 工具 5：nuclei 扫描（缺失降级内置）
# ---------------------------------------------------------------------------


class NucleiScanTool(Tool):
    """调用 nuclei 做模板化扫描；未安装时降级为内置 PoC 逻辑。"""

    spec = ToolSpec(
        name="nuclei_scan",
        summary="nuclei 模板扫描（外部二进制优先）；未安装时自动降级为内置只读 PoC 扫描",
        category="vuln",
        action_level="detect",
        external_bin="nuclei",
        params={
            "url": ParamSpec("str", "目标 URL", required=True),
            "severity": ParamSpec("list", "关注的严重级别", required=False, default=list(DEFAULT_NUCLEI_SEVERITY)),
            "templates": ParamSpec("list", "自定义模板路径/ID", required=False),
            "timeout": ParamSpec("int", "整体超时（秒）", required=False, default=180),
        },
        produces=["findings", "paths"],
        examples=["nuclei_scan url=http://10.0.0.5:8080 severity=[high,critical]"],
        notes="nuclei 缺失时复用内置 PoC 表，data.engine 会标注 'builtin' 与覆盖边界。",
    )

    def run(self, ctx, url: str, severity: Any = None, templates: Any = None, timeout: int = 180) -> ToolResult:
        target = _normalize_url(url)
        sevs = [str(s).lower() for s in (severity or DEFAULT_NUCLEI_SEVERITY)]
        binary = which("nuclei")

        if binary:
            argv = [binary, "-u", target, "-jsonl", "-silent", "-no-color", "-severity", ",".join(sevs)]
            for tpl in templates or []:
                argv += ["-t", str(tpl)]
            try:
                run = run_external(argv, ctx, tool="nuclei", target=target, required_level="detect", timeout=float(timeout))
            except ScopeViolation as exc:
                return denied_result(exc.decision, tool="nuclei_scan")

            if run.ok or (run.stdout or "").strip():
                findings, parsed = _parse_nuclei_jsonl(run.stdout, target)
                facts_paths = [{"host": _host_of(f["url"]), "url": f["url"]} for f in findings]
                summary = (
                    f"nuclei 命中 {len(findings)} 项" if findings
                    else f"nuclei 完成，无命中（{run.duration:.0f}s，严重级别 {','.join(sevs)}）"
                )
                return ToolResult(
                    ok=True,
                    category="vuln",
                    summary=summary,
                    data={
                        "engine": "nuclei",
                        "binary": binary,
                        "target": target,
                        "severity": sevs,
                        "templates": [str(t) for t in (templates or [])],
                        "finding_count": len(findings),
                        "parsed_lines": parsed,
                        "stderr": (run.stderr or "")[:400],
                        "duration": round(run.duration, 2),
                    },
                    facts={"findings": findings, "paths": facts_paths},
                    raw=(run.stdout or "")[:20000],
                    command=run.command,
                    evidence=[{"kind": "external", "tool": "nuclei", "command": run.command, "parsed": parsed, "findings": len(findings)}],
                    next_hints=["对命中项人工确认可利用性"] if findings else ["扩大模板集或降低严重级别阈值"],
                )

        # ---- 降级：复用内置 PoC 逻辑 ----
        outcome = _run_poc_scan(ctx, target, None, sevs)
        return _build_poc_result(
            target,
            outcome["selected"],
            outcome["hits"],
            outcome["checked"],
            engine="builtin",
            extra={
                "degraded_from": "nuclei",
                "degraded_reason": "未检测到 nuclei 二进制，已降级为内置 PoC 引擎",
                "severity": sevs,
            },
        )


TOOLS: list[Tool] = [PocScanTool(), NucleiScanTool()]

for _tool in TOOLS:  # 兼容模块级全局注册表
    register(_tool)

__all__ = [
    "TOOLS",
    "PoC",
    "POCS",
    "POC_INDEX",
    "PocScanTool",
    "NucleiScanTool",
    "_run_poc_scan",
    "_match",
    "_parse_nuclei_jsonl",
    "_build_poc_result",
]
