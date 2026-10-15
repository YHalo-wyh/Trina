"""目标约束：把「任务说明」变成可验证的完成条件。

问题背景：如果只看「拿到 flag 就收工」，Agent 会在靶机恰好暴露 `/flag` 时**走捷径**——
跳过任务要求的验证步骤（例如"确认 /sqli 的 SQL 注入"）。评测里这会表现为任务失败但状态 success。

做法：从目标文本里解析出**要求**（漏洞类关键词 + 路径），在事实库里查找是否已被覆盖；
未覆盖时不允许收敛，并把缺口回灌进提示词。注意：这里**不使用 ground truth**，
只依赖调用者给出的任务描述，模型看不到答案。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 目标文本关键词 → 规范漏洞类（与 trina/eval/metrics.py 的归一化保持一致的写法）
KEYWORD_CLASSES: dict[str, str] = {
    "sql注入": "sqli",
    "sql 注入": "sqli",
    "sqli": "sqli",
    "注入点": "sqli",
    "文件包含": "lfi",
    "任意文件读取": "lfi",
    "目录穿越": "lfi",
    "路径穿越": "lfi",
    "lfi": "lfi",
    "命令注入": "rce",
    "命令执行": "rce",
    "代码执行": "rce",
    "rce": "rce",
    "文件上传": "unrestricted_file_upload",
    "上传漏洞": "unrestricted_file_upload",
    "弱口令": "weak_password",
    "弱密码": "weak_password",
    "爆破": "weak_password",
    "xss": "xss_reflected",
    "跨站": "xss_reflected",
    "越权": "idor",
    "未授权访问": "idor",
    "信息泄露": "information_disclosure",
    "敏感信息": "information_disclosure",
    "反序列化": "deserialization",
    "ssrf": "ssrf",
    "开放端口": "open_port",
    "端口扫描": "open_port",
}

#: 只需要「发现」即算达成的类（不需要利用）
DETECTION_ONLY = {"information_disclosure", "open_port", "service_fingerprint", "security_misconfig_headers"}

_PATH_RE = re.compile(r"(?<![\w.])/(?:\.?[A-Za-z0-9_\-]+/)*\.?[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9]{1,5})?")
#: 端口候选：2–5 位独立数字（前后不能是词字符/点/斜杠/百分号）
_PORT_RE = re.compile(r"(?<![\w./%$])(\d{2,5})(?![\d.])")
#: 明显不是端口的数字：年份、以及后面跟着时间/数量单位的数字
_PORT_UNIT_RE = re.compile(r"\s*(?:年|月|日|秒|分|小时|次|步|个|条|台|元|%)")
#: 从任务描述里识别「明文凭据」的两种常见写法
_CRED_PAIR_RE = re.compile(r"(?<![\w/])([A-Za-z][\w.\-]{1,31})/([^\s/，,、）)]{2,64})")
_CRED_LABELED_RE = re.compile(
    r"(?:用户名|账号|账户|user(?:name)?)\s*[:：]?\s*(?P<user>[A-Za-z][\w.\-]{1,31})"
    r"[\s，,、和与]*(?:口令|密码|pass(?:word)?)\s*[:：]?\s*(?P<password>[^\s）)，,。;；、]{2,64})",
    re.I,
)
#: 先剥掉 URL 的 scheme://host[:port]，避免把 "//127.0" 误当成路径
_URL_PREFIX_RE = re.compile(r"[A-Za-z][\w+.\-]*://[^\s/]+")
_STOP_PATHS = {"/flag", "/flag.txt", "/etc", "/tmp", "/var", "/usr", "/bin", "/home"}


@dataclass
class GoalRequirements:
    """从目标任务描述里解析出的硬性要求。"""

    raw: str = ""
    classes: set[str] = field(default_factory=set)
    paths: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    credentials: list[tuple[str, str]] = field(default_factory=list)
    explicit_flag: bool = False

    @classmethod
    def parse(cls, goal: str = "") -> "GoalRequirements":
        text = (goal or "").strip()
        lower = text.lower()
        req = cls(raw=text)
        for keyword, vuln_class in KEYWORD_CLASSES.items():
            if keyword in lower:
                req.classes.add(vuln_class)
        host_stripped = _URL_PREFIX_RE.sub("", text)
        for match in _PATH_RE.finditer(host_stripped):
            path = match.group(0).rstrip(".").lower()
            if path in _STOP_PATHS or len(path) < 4 or path.startswith("/flag"):
                continue
            if path not in req.paths:
                req.paths.append(path)
        for match in _PORT_RE.finditer(host_stripped):
            literal = match.group(1)
            if literal.startswith("0"):  # 任务 ID/编号（web-0001）不是端口
                continue
            port = int(literal)
            if not (1 <= port <= 65535) or port in req.ports:
                continue
            tail = host_stripped[match.end() : match.end() + 3]
            if 1900 <= port <= 2100:  # 年份，不是端口
                continue
            if _PORT_UNIT_RE.match(tail):  # "3600 秒"、"3 次" 这类数量
                continue
            req.ports.append(port)
        for match in _CRED_PAIR_RE.finditer(text):
            user, password = match.group(1), match.group(2)
            if user and password and (user, password) not in req.credentials:
                req.credentials.append((user, password))
        for match in _CRED_LABELED_RE.finditer(text):
            user, password = match.group("user"), match.group("password")
            if user and password and (user, password) not in req.credentials:
                req.credentials.append((user, password))
        req.explicit_flag = "flag" in lower
        return req

    def credential_hints(self) -> list[str]:
        """目标里写明的凭据 → 建议的低等级验证方式（不是硬条件，只作提示）。"""
        return [
            f"目标给出了凭据 {user}/{password}：用 weak_password_brute 传 username={user}、password={password}"
            f"（单次验证属 verify 级），HTTP 表单场景也可用 http_form_login；不要用大字典爆破"
            for user, password in self.credentials[:2]
        ]

    @property
    def empty(self) -> bool:
        return not self.classes and not self.paths and not self.ports

    def unsatisfied(self, state: Any) -> list[str]:
        """返回尚未满足的**硬性**要求（用于阻止提前收敛）。

        口径：类要求是硬条件；路径要求只在「没有任何类要求」时才作为硬条件——
        否则像 /robots.txt 这类只会落到路径事实（而非 finding）的端点会把场景误判为未完成。
        """
        findings = list(getattr(state, "findings", None) or [])
        missing: list[str] = []
        for port in self.ports:
            if not _covers_port(state, port):
                missing.append(f"端口 {port} 尚未验证是否开放")
        for vuln_class in sorted(self.classes):
            if not any(_covers_class(f, vuln_class) for f in findings):
                missing.append(f"漏洞类 {vuln_class} 尚未验证")
        if not self.classes:
            for path in self.paths:
                if not any(_covers_path(f, path) for f in findings):
                    missing.append(f"路径 {path} 尚未给出结论")
        return missing

    def soft_hints(self, state: Any) -> list[str]:
        """不阻止收敛、但值得继续推进的提示（例如路径类要求）。"""
        findings = list(getattr(state, "findings", None) or [])
        hints: list[str] = []
        if self.classes:
            for path in self.paths:
                if not any(_covers_path(f, path) for f in findings):
                    hints.append(f"路径 {path} 也应有结论（可选）")
        return hints

    def satisfied(self, state: Any) -> bool:
        return not self.unsatisfied(state)

    def describe(self) -> str:
        bits: list[str] = []
        if self.classes:
            bits.append("需验证的漏洞类: " + ", ".join(sorted(self.classes)))
        if self.paths:
            bits.append("需给出结论的路径: " + ", ".join(self.paths))
        if self.ports:
            bits.append("需覆盖的端口: " + ", ".join(str(p) for p in self.ports))
        return "；".join(bits)


def _covers_port(state: Any, port: int) -> bool:
    """端口要求：任一主机上已记录该端口（开放或明确关闭）即视为已给出结论。"""
    for host_state in (getattr(state, "hosts", None) or {}).values():
        ports = getattr(host_state, "ports", None) or {}
        if any(int(existing) == int(port) for existing in ports):
            return True
    return False


def _finding_is_evidenced(finding: Any) -> bool:
    status = getattr(finding, "status", "") or ""
    if status in ("verified", "exploited"):
        return True
    return bool(getattr(finding, "evidence", None))


def _covers_class(finding: Any, vuln_class: str) -> bool:
    if not _finding_is_evidenced(finding):
        return False
    from ..eval.metrics import normalize_vuln_class

    return normalize_vuln_class(getattr(finding, "vuln_class", "")) == normalize_vuln_class(vuln_class)


def _covers_path(finding: Any, path: str) -> bool:
    url = str(getattr(finding, "url", "") or getattr(finding, "target", "") or "").lower()
    if path not in url:
        return False
    # 路径类要求允许「有结论即可」，但至少要带证据
    return bool(getattr(finding, "evidence", None)) or getattr(finding, "status", "") in ("verified", "exploited")


__all__ = ["GoalRequirements", "KEYWORD_CLASSES", "DETECTION_ONLY"]
