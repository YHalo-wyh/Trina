"""运行状态模型 —— 大脑、工具、报告三方的共享事实库。

一次渗透 = 一个 `RunState`：

* `hosts`      主机与端口/服务（recon 产出）
* `findings`   漏洞（去重合并，带证据与置信度）
* `credentials` 有效凭据
* `flags`      夺旗结果
* `evidence`   原始证据（请求/响应片段/命令回放）
* `timeline`   时间线（报告与审计交叉引用）
* `steps`      ReAct 每步的思考/动作/观察
* `ledger`     LLM 用量与成本

工具通过 `ToolResult.facts` 声明"我发现了什么"，由 `RunState.absorb()` 统一吸收，
避免每个工具各自维护全局状态。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_CN = {
    "critical": "严重",
    "high": "高危",
    "medium": "中危",
    "low": "低危",
    "info": "信息",
}
SEVERITY_ORDER = {name: idx for idx, name in enumerate(SEVERITIES)}


def _now() -> float:
    return round(time.time(), 3)


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


@dataclass
class Finding:
    """一条漏洞。`status`: detected(仅发现) → verified(可复现验证) → exploited(已利用)。"""

    id: str
    title: str
    vuln_class: str
    target: str = ""
    url: str = ""
    parameter: str = ""
    method: str = "GET"
    severity: str = "medium"
    confidence: str = "medium"  # low|medium|high
    status: str = "detected"
    tool: str = ""
    payload: str = ""
    description: str = ""
    impact: str = ""
    remediation: str = ""
    cvss: str = ""
    cwe: str = ""
    refs: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    first_seen: float = field(default_factory=_now)
    last_seen: float = field(default_factory=_now)

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        return (self.vuln_class, self.url or self.target, self.parameter)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["severity_cn"] = SEVERITY_CN.get(self.severity, self.severity)
        return data


@dataclass
class Credential:
    target: str
    service: str
    username: str
    password: str = "<redacted>"
    source: str = ""
    verified: bool = False
    ts: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class HostState:
    host: str
    ports: dict[int, dict[str, Any]] = field(default_factory=dict)
    technologies: list[str] = field(default_factory=list)
    web_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    alive: bool | None = None

    def add_port(self, port: int, **info: Any) -> None:
        entry = self.ports.setdefault(int(port), {})
        for key, value in info.items():
            if value not in (None, "", []):
                entry[key] = value

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "alive": self.alive,
            "ports": {str(p): v for p, v in sorted(self.ports.items())},
            "technologies": self.technologies,
            "web_paths": self.web_paths,
            "notes": self.notes,
        }


@dataclass
class RunState:
    run_id: str
    target: str
    goal: str = ""
    mode: str = "auto"  # auto|assist
    status: str = "running"  # running|success|partial|failed|blocked
    phase: str = "recon"
    phase_history: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=_now)
    ended_at: float | None = None
    hosts: dict[str, HostState] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    credentials: list[Credential] = field(default_factory=list)
    flags: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    ledger: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, int] = field(
        default_factory=lambda: {"steps": 0, "llm_calls": 0, "tool_calls": 0, "denied": 0, "errors": 0}
    )
    artifacts: dict[str, str] = field(default_factory=dict)
    max_evidence: int = 200

    # ------------------------------------------------------------------ 基础
    @property
    def duration(self) -> float:
        return round((self.ended_at or time.time()) - self.started_at, 1)

    def host(self, host: str) -> HostState:
        return self.hosts.setdefault(host, HostState(host=host))

    def set_phase(self, phase: str, note: str = "") -> None:
        if phase != self.phase:
            self.phase_history.append(self.phase)
            self.phase = phase
            self.note("phase", f"进入阶段 {phase}", note=note)

    def note(self, kind: str, text: str, **extra: Any) -> None:
        entry = {"ts": _now(), "clock": _clock(time.time()), "kind": kind, "text": text}
        entry.update(extra)
        self.timeline.append(entry)

    def log_step(self, step: dict[str, Any]) -> None:
        step.setdefault("ts", _now())
        self.steps.append(step)
        self.counters["steps"] = len(self.steps)

    # ------------------------------------------------------------------ 证据
    def add_evidence(self, item: dict[str, Any]) -> dict[str, Any]:
        item.setdefault("ts", _now())
        item.setdefault("clock", _clock(time.time()))
        self.evidence.append(item)
        if len(self.evidence) > self.max_evidence:
            self.evidence = self.evidence[-self.max_evidence :]
        return item

    # ------------------------------------------------------------------ 漏洞
    def add_finding(self, finding: Finding | dict[str, Any]) -> Finding:
        """按 (漏洞类, URL, 参数) 去重；重复则合并证据并提升置信度/状态。"""
        if isinstance(finding, dict):
            data = {k: v for k, v in finding.items() if k in Finding.__dataclass_fields__}
            data.setdefault("id", "")
            data.setdefault("title", data.get("vuln_class", "未命名漏洞"))
            data.setdefault("vuln_class", "unknown")
            finding = Finding(**data)
        if not finding.id:
            finding.id = f"TRINA-{len(self.findings) + 1:03d}"
        for existing in self.findings:
            if existing.dedup_key == finding.dedup_key:
                existing.last_seen = _now()
                existing.evidence.extend(finding.evidence)
                existing.status = _max_status(existing.status, finding.status)
                if SEVERITY_ORDER.get(finding.severity, 9) < SEVERITY_ORDER.get(existing.severity, 9):
                    existing.severity = finding.severity
                if finding.confidence == "high":
                    existing.confidence = "high"
                for attr in ("payload", "remediation", "impact", "description", "cvss", "cwe"):
                    if not getattr(existing, attr) and getattr(finding, attr):
                        setattr(existing, attr, getattr(finding, attr))
                return existing
        for ev in finding.evidence:
            self.add_evidence(dict(ev, finding_id=finding.id))
        self.findings.append(finding)
        self.note("finding", f"新增 {finding.severity} {finding.vuln_class} @ {finding.url or finding.target}")
        return finding

    # ------------------------------------------------------------------ 吸收
    def absorb(self, facts: dict[str, Any] | None) -> list[str]:
        """把工具声明的 facts 并入状态；返回人类可读的变更列表。"""
        if not facts:
            return []
        changed: list[str] = []
        for host_entry in facts.get("hosts", []) or []:
            host = str(host_entry.get("host") or "")
            if not host:
                continue
            state = self.host(host)
            if "alive" in host_entry:
                state.alive = bool(host_entry["alive"])
                changed.append(f"主机存活: {host}")
        for port_entry in facts.get("ports", []) or []:
            host = str(port_entry.get("host") or self.target)
            port = port_entry.get("port")
            if port is None:
                continue
            state = self.host(host)
            if int(port) not in state.ports:
                changed.append(f"发现端口 {host}:{port} ({port_entry.get('service', '?')})")
            state.add_port(
                int(port),
                service=port_entry.get("service"),
                version=port_entry.get("version"),
                banner=port_entry.get("banner"),
                protocol=port_entry.get("protocol", "tcp"),
            )
        for service in facts.get("services", []) or []:
            host = str(service.get("host") or self.target)
            state = self.host(host)
            tech = service.get("technology") or service.get("server")
            if tech and tech not in state.technologies:
                state.technologies.append(str(tech))
                changed.append(f"识别技术栈: {host} -> {tech}")
            state.notes.append(str(service.get("note", ""))) if service.get("note") else None
        for path in facts.get("paths", []) or []:
            host = str(path.get("host") or self.target)
            url = str(path.get("url") or path.get("path") or "")
            state = self.host(host)
            if url and url not in state.web_paths:
                state.web_paths.append(url)
                changed.append(f"发现路径: {url} [{path.get('status', '?')}]")
        for cred in facts.get("credentials", []) or []:
            entry = Credential(
                target=str(cred.get("target", "")),
                service=str(cred.get("service", "")),
                username=str(cred.get("username", "")),
                password=str(cred.get("password", "<redacted>")),
                source=str(cred.get("source", "")),
                verified=bool(cred.get("verified", False)),
            )
            if not any(
                c.target == entry.target and c.service == entry.service and c.username == entry.username
                for c in self.credentials
            ):
                self.credentials.append(entry)
                changed.append(f"获得凭据: {entry.service}@{entry.target} 用户 {entry.username}")
        for flag in facts.get("flags", []) or []:
            value = str(flag.get("value", "")).strip()
            if value and not any(f.get("value") == value for f in self.flags):
                self.flags.append({"value": value, "source": flag.get("source", ""), "ts": _now()})
                changed.append(f"获取 flag: {value[:40]}")
        for finding in facts.get("findings", []) or []:
            result = self.add_finding(finding)
            changed.append(f"记录漏洞: [{result.severity}] {result.vuln_class} @ {result.url or result.target}")
        for note in facts.get("notes", []) or []:
            self.host(self.target).notes.append(str(note))
        return changed

    # ------------------------------------------------------------------ 视图
    def summary_for_prompt(self, max_chars: int = 4000) -> str:
        """给大模型的状态摘要（严格控制体积）。"""
        lines: list[str] = [f"目标: {self.target}", f"阶段: {self.phase}", f"状态: {self.status}"]
        lines.append(f"已用步数: {self.counters['steps']}，耗时 {self.duration:.0f}s")
        for host, hs in self.hosts.items():
            ports = ", ".join(
                f"{p}/{info.get('service', '?')}"
                + (f"({info.get('version')})" if info.get("version") else "")
                for p, info in sorted(hs.ports.items())
            )
            lines.append(f"主机 {host}: 存活={hs.alive} 端口=[{ports or '无'}]")
            if hs.technologies:
                lines.append(f"  技术栈: {', '.join(hs.technologies[:8])}")
            if hs.web_paths:
                lines.append(f"  已发现路径: {', '.join(hs.web_paths[-12:])}")
        if self.credentials:
            lines.append("凭据: " + ", ".join(f"{c.service}@{c.target}:{c.username}" for c in self.credentials[:8]))
        if self.flags:
            lines.append("flags: " + ", ".join(f["value"][:40] for f in self.flags[:5]))
        if self.findings:
            lines.append("已知漏洞:")
            for finding in sorted(self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9)):
                lines.append(
                    f"  [{finding.id}] {finding.severity}/{finding.confidence} {finding.vuln_class} "
                    f"{finding.url or finding.target} param={finding.parameter or '-'} status={finding.status}"
                )
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(状态已截断)"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "target": self.target,
            "goal": self.goal,
            "status": self.status,
            "phase": self.phase,
            "phase_history": self.phase_history,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration": self.duration,
            "hosts": {h: v.to_dict() for h, v in self.hosts.items()},
            "findings": [f.to_dict() for f in self.findings],
            "credentials": [c.to_dict() for c in self.credentials],
            "flags": self.flags,
            "evidence": self.evidence,
            "timeline": self.timeline,
            "steps": self.steps,
            "ledger": self.ledger,
            "counters": self.counters,
            "artifacts": self.artifacts,
        }


def _max_status(a: str, b: str) -> str:
    order = {"detected": 0, "verified": 1, "exploited": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b
