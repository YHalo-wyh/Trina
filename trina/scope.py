"""授权范围护栏 —— Trina 的**唯一放行判据**。

任何出网动作（工具调用、外部二进制、HTTP 请求）在真正执行前都必须经过本模块。
护栏判据只来自 `ScopeConfig`（配置文件），**不来自大模型**：模型只能申请，不能授权。

三层防线
--------
1. 目标校验 `check_target`：解析目标 → 全部 IP 必须落在授权网段且不在拒绝网段；
   公网默认拒绝（`allow_public=false`）；回环受 `allow_loopback` 控制。
2. 动作等级 `check_action_level`：detect(只读) < verify(可验证) < exploit(命令执行/批量爆破)，
   超过 `scope.exploit_level` 的动作直接拒绝。
3. 硬禁止清单 `check_argv`：DoS、系统破坏、数据导出、外带写文件等模式，
   **无论配置如何都无法开启**（`FORBIDDEN_ARGV_PATTERNS` / `FORBIDDEN_SHELL_PATTERNS`）。
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import unquote_plus, urlsplit

from .config import ACTION_LEVELS, ScopeConfig

# ---------------------------------------------------------------------------
# 硬禁止清单：任何配置都无法放行
# ---------------------------------------------------------------------------

#: 外部工具参数级禁止（正则作用于「工具名 + 参数」拼串后的文本）
FORBIDDEN_ARGV_PATTERNS: list[tuple[str, str]] = [
    (r"--os-shell", "拿到系统 shell 属越权破坏性操作，实训场景禁止"),
    (r"--os-pwn", "sqlmap 外带 OOB 利用禁止"),
    (r"--os-smbrelay", "sqlmap SMB 中继利用禁止"),
    (r"--os-bof", "sqlmap 溢出利用禁止"),
    (r"--file-write\b", "向目标写文件禁止（外带写 shell/webshell）"),
    (r"--file-dest\b", "向目标写文件禁止"),
    (r"--sql-shell\b", "交互式 SQL shell 禁止"),
    (r"--dump-all\b", "整库导出属数据泄露，禁止"),
    (r"--dump\b.*--exclude-sysdbs", "整库导出属数据泄露，禁止"),
    (r"--risk\s*=?\s*3", "sqlmap risk=3 含破坏性 payload，禁止"),
    (r"--eval\s*=", "sqlmap --eval 可执行任意 Python，禁止"),
    (r"--crawl\b", "爬虫会超范围抓取，禁止"),
    (r"\b(mkfs|mkfs\.\w+)\b", "格式化文件系统禁止"),
    (r"\bdd\s+.*of=/dev/", "裸设备写入禁止"),
    (r":\(\)\s*\{.*\};:", "fork 炸弹禁止"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "关停主机禁止"),
    (r"\b(iptables|nft)\s+(-F|--flush)", "清空防火墙规则禁止"),
    (r"\b(hping3?|slowloris|goldeneye|t50)\b", "洪泛/DoS 工具禁止"),
    (r"nmap.*--script[= ]\S*(dos|flood|broadcast|brute)", "DoS/NSE 广播脚本禁止"),
    (r"\b(curl|wget)\b[^|;]*\|\s*(ba|z|da)?sh", "执行远端下载的脚本禁止"),
    (r">\s*/etc/", "覆盖系统配置禁止"),
    (r"\bchmod\s+(-\w+\s+)?777\s+/\s*$", "放开根目录权限禁止"),
    (r"\brm\s+-rf?\s+(/\s*$|/\*|/etc|/var|/usr|/home|/root)", "删除系统目录禁止"),
    (r"\buserdel\b|\bpasswd\s+root\b", "破坏账户凭据禁止"),
]

#: 载荷级禁止：出现在注入/上传/命令参数中的危险载荷
FORBIDDEN_PAYLOAD_PATTERNS: list[tuple[str, str]] = [
    (r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)", "删库载荷禁止"),
    (r"\bDELETE\s+FROM\b", "批量删除数据载荷禁止"),
    (r"\bUPDATE\s+\w+\s+SET\b", "篡改数据载荷禁止"),
    (r"\b(INSERT|REPLACE)\s+INTO\s+.*\b(users|admin|mysql)\b", "篡改账户表载荷禁止"),
    (r"\bxp_cmdshell\b", "数据库命令执行禁止"),
    (r"\bINTO\s+(OUT|DUMP)FILE\b", "数据库写文件禁止"),
    (r"\bLOAD_FILE\s*\(", "读取服务器文件禁止"),
    # 注意：/etc/passwd 是 LFI 验证的标准无害取证（不含口令哈希），允许；
    # 真正涉及凭据的文件（shadow/sudoers/私钥）仍硬禁止。
    (r"/etc/shadow\b", "读取系统口令哈希禁止"),
    (r"/etc/sudoers\b", "读取提权配置禁止"),
    (r"(/root/\.ssh/|/\.ssh/id_[a-z]+)", "读取 SSH 私钥禁止"),
    (r"\brm\s+-rf\b", "删除文件载荷禁止"),
]

#: HTTP 方法策略：破坏性方法需要 exploit 级 + destructive 开关
DESTRUCTIVE_METHODS = {"DELETE", "PUT", "PATCH", "TRACE", "CONNECT"}

_FORBIDDEN_ARGV = [(re.compile(p, re.I), why) for p, why in FORBIDDEN_ARGV_PATTERNS]
_FORBIDDEN_PAYLOAD = [(re.compile(p, re.I), why) for p, why in FORBIDDEN_PAYLOAD_PATTERNS]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """护栏裁决结果。`hint` 会回灌给大模型，引导它改走合规路径。"""

    allowed: bool
    reason: str = ""
    category: str = "scope"
    target: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    hint: str = ""

    def __bool__(self) -> bool:  # 允许 `if guard.check_target(...)`
        return self.allowed

    @property
    def denied(self) -> bool:
        return not self.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "category": self.category,
            "target": self.target,
            "detail": self.detail,
            "hint": self.hint,
        }


class ScopeViolation(RuntimeError):
    """护栏拒绝动作时抛出；调用方必须捕获并作为观察结果回灌给模型。"""

    def __init__(self, decision: Decision):
        super().__init__(f"[{decision.category}] {decision.reason}")
        self.decision = decision


@dataclass
class TargetInfo:
    """规范化后的目标描述。"""

    raw: str
    host: str
    port: int | None = None
    scheme: str = ""
    path: str = ""
    ips: tuple[str, ...] = ()
    is_loopback: bool = False
    is_public: bool = False
    is_cidr: bool = False

    @property
    def hostport(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "host": self.host,
            "port": self.port,
            "scheme": self.scheme,
            "path": self.path,
            "ips": list(self.ips),
            "is_loopback": self.is_loopback,
            "is_public": self.is_public,
            "is_cidr": self.is_cidr,
        }


def parse_target(raw: str) -> TargetInfo:
    """把 URL / host:port / IP / CIDR 统一成 TargetInfo。"""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("目标为空")
    scheme = ""
    path = ""
    host = raw
    port: int | None = None

    if "://" in raw:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower() or "http"
        host = parts.hostname or ""
        path = parts.path or "/"
        port = parts.port or (443 if scheme == "https" else 80)
    elif "/" in raw and "-" not in raw.split("/")[0]:
        # CIDR
        return TargetInfo(raw=raw, host=raw, ips=(), is_cidr=True)
    else:
        # host[:port] 或 [v6]:port
        if raw.startswith("[") and "]" in raw:
            host, _, tail = raw.partition("]")
            host = host[1:]
            if tail.startswith(":") and tail[1:].isdigit():
                port = int(tail[1:])
        elif raw.count(":") == 1 and raw.rsplit(":", 1)[1].isdigit():
            host, port_str = raw.rsplit(":", 1)
            port = int(port_str)
    if not host:
        raise ValueError(f"无法解析目标: {raw!r}")
    return TargetInfo(raw=raw, host=host, port=port, scheme=scheme, path=path)


DEFAULT_RESOLVER = socket.getaddrinfo


def resolve_ips(host: str, resolver: Callable[..., Any] | None = None) -> tuple[str, ...]:
    """解析主机名到去重后的 IPv4/IPv6 字面量；IP 字面量直接返回。"""
    try:
        return (str(ipaddress.ip_address(host)),)
    except ValueError:
        pass
    resolver = resolver or DEFAULT_RESOLVER
    try:
        infos = resolver(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"DNS 解析失败 {host!r}: {exc}") from exc
    ips = sorted({str(info[4][0]) for info in infos})
    if not ips:
        raise ValueError(f"DNS 解析无结果: {host!r}")
    return tuple(ips)


# ---------------------------------------------------------------------------
# 限速
# ---------------------------------------------------------------------------


class RateLimiter:
    """令牌桶：全局 QPS + 单主机并发/速率，防止把靶机打出 DoS。"""

    def __init__(self, max_qps: float, per_host_qps: float | None = None, clock: Callable[[], float] = time.monotonic):
        self.max_qps = float(max_qps)
        self.per_host_qps = float(per_host_qps or max_qps)
        self.clock = clock
        self._tokens = float(max_qps)
        self._updated = clock()
        self._host: dict[str, tuple[float, float]] = {}
        self.waited = 0.0

    def _refill(self, bucket: float, cap: float, now: float, last: float) -> tuple[float, float]:
        elapsed = max(0.0, now - last)
        return min(cap, bucket + elapsed * cap), now

    def acquire(self, host: str = "", tokens: float = 1.0) -> float:
        """返回需要等待的秒数（并扣除令牌）。"""
        now = self.clock()
        self._tokens, self._updated = self._refill(self._tokens, self.max_qps, now, self._updated)
        bucket, last = self._host.get(host, (self.per_host_qps, now))
        bucket, _ = self._refill(bucket, self.per_host_qps, now, last)
        need = max(0.0, tokens - self._tokens, tokens - bucket)
        self._tokens = max(0.0, self._tokens - tokens)
        self._host[host] = (max(0.0, bucket - tokens), now)
        wait = need / self.max_qps if need > 0 else 0.0
        self.waited += wait
        return wait


# ---------------------------------------------------------------------------
# 护栏主体
# ---------------------------------------------------------------------------

_SAFE_CATEGORIES = ("scope", "action-level", "destructive", "forbidden", "format", "rate", "error")


class ScopeGuard:
    """授权范围守卫。所有工具的必经之路。"""

    def __init__(
        self,
        scope: ScopeConfig,
        audit: Any | None = None,
        resolver: Callable[..., Any] | None = None,
    ):
        self.scope = scope
        self.audit = audit
        self.resolver = resolver
        self.limiter = RateLimiter(scope.max_qps)
        self.stats: dict[str, int] = {"allowed": 0, "denied": 0}
        self.denials: list[Decision] = []
        self._cidrs = [ipaddress.ip_network(c, strict=False) for c in scope.authorized_cidrs]
        self._denied = [ipaddress.ip_network(c, strict=False) for c in scope.deny_cidrs]
        self._authorized_hosts = {h.lower() for h in scope.authorized_hosts}
        self._destructive_hosts = {h.lower() for h in scope.destructive_hosts}

    # ---------------------------------------------------------------- 内部
    def _record(self, decision: Decision, action: str) -> Decision:
        if decision.allowed:
            self.stats["allowed"] += 1
        else:
            self.stats["denied"] += 1
            self.denials.append(decision)
        if self.audit is not None:
            self.audit.append(
                "guard.allow" if decision.allowed else "guard.deny",
                decision="allow" if decision.allowed else "deny",
                category=decision.category,
                action=action,
                target=decision.target,
                reason=decision.reason,
                detail=decision.detail,
            )
        return decision

    @staticmethod
    def _deny(category: str, reason: str, target: str = "", hint: str = "", **detail: Any) -> Decision:
        if category not in _SAFE_CATEGORIES:
            category = "scope"
        return Decision(False, reason=reason, category=category, target=target, detail=detail, hint=hint)

    def _ip_allowed(self, ip: ipaddress._BaseAddress) -> tuple[bool, str]:
        """单个 IP 的判定；返回 (是否允许, 原因)。"""
        if ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_link_local:
            return False, f"{ip} 属特殊地址（组播/未指定/保留/链路本地）"
        if any(ip in net for net in self._denied):
            return False, f"{ip} 命中显式拒绝网段"
        if ip.is_loopback:
            if not self.scope.allow_loopback:
                return False, "回环地址被策略禁止"
            return True, "回环地址，允许（本地靶机）"
        if ip.is_private:
            return (any(ip in net for net in self._cidrs), f"{ip} 属私网地址")
        if not self.scope.allow_public:
            return False, f"{ip} 是公网地址且未开启 allow_public"
        return (any(ip in net for net in self._cidrs), f"{ip} 属公网地址")

    # ---------------------------------------------------------------- 目标
    def check_target(self, raw: str, *, action: str = "probe", port: int | None = None) -> Decision:
        """校验目标是否在授权范围内（含 DNS 解析后的每个 IP）。"""
        try:
            info = parse_target(raw)
        except ValueError as exc:
            return self._record(self._deny("format", str(exc), target=str(raw), hint="请提供 host:port 或 http(s):// 形式的合法目标"), action)

        if info.is_cidr:
            try:
                network = ipaddress.ip_network(info.host, strict=False)
            except ValueError as exc:
                return self._record(self._deny("format", f"非法网段 {info.host!r}: {exc}", target=raw), action)
            if not any(
                network.subnet_of(net) or network == net
                for net in self._cidrs
                if network.version == net.version
            ):
                return self._record(
                    self._deny(
                        "scope",
                        f"网段 {network} 未被整体授权",
                        target=raw,
                        hint="只能扫描授权网段的子集；请改用已授权的具体主机",
                    ),
                    action,
                )
            return self._record(Decision(True, category="scope", target=raw, detail={"cidr": str(network)}, reason="网段在授权范围内"), action)

        hostname_allowed = info.host.lower() in self._authorized_hosts
        if not self.scope.resolve_dns:
            try:
                ipaddress.ip_address(info.host)
            except ValueError:
                return self._record(
                    self._deny("format", f"{info.host} 不是 IP 字面量且 resolve_dns 已关闭", target=raw),
                    action,
                )
        try:
            ips = resolve_ips(info.host, self.resolver)
        except ValueError as exc:
            if hostname_allowed:
                ips = ()
            else:
                return self._record(
                    self._deny("scope", str(exc), target=raw, hint="请改用可达的 IP 或 host:port"), action
                )

        detail_ips: list[str] = []
        reasons: list[str] = []
        for ip_str in ips:
            ip = ipaddress.ip_address(ip_str)
            allowed, why = self._ip_allowed(ip)
            detail_ips.append(ip_str)
            reasons.append(f"{ip_str}: {why}")
            if not allowed and not hostname_allowed:
                return self._record(
                    self._deny(
                        "scope",
                        f"目标 {info.host} ({ip_str}) 不在授权范围：{why}",
                        target=raw,
                        hint="仅可测试配置文件中 authorized_cidrs / authorized_hosts 内的目标",
                        ips=detail_ips,
                    ),
                    action,
                )
            if not allowed and hostname_allowed:
                reasons[-1] += "（命中显式授权主机白名单，放行）"

        is_loopback = bool(ips) and all(ipaddress.ip_address(i).is_loopback for i in ips)
        is_public = bool(ips) and any(ipaddress.ip_address(i).is_global for i in ips)
        info.ips = ips
        info.is_loopback = is_loopback
        info.is_public = is_public
        return self._record(
            Decision(
                True,
                category="scope",
                target=raw,
                reason="目标在授权范围内",
                detail={"ips": detail_ips, "checks": reasons, "host": info.host, "port": info.port or port},
            ),
            action,
        )

    def require_target(self, raw: str, *, action: str = "probe", port: int | None = None) -> TargetInfo:
        """校验通过则返回 TargetInfo，否则抛 ScopeViolation。"""
        decision = self.check_target(raw, action=action, port=port)
        if decision.denied:
            raise ScopeViolation(decision)
        info = parse_target(raw)
        try:
            info.ips = resolve_ips(info.host, self.resolver)
        except ValueError:
            info.ips = ()
        info.is_loopback = bool(info.ips) and all(ipaddress.ip_address(i).is_loopback for i in info.ips)
        info.is_public = bool(info.ips) and any(ipaddress.ip_address(i).is_global for i in info.ips)
        return info

    # ------------------------------------------------------------ 动作等级
    def check_action_level(self, required: str, *, target: str = "", reason: str = "") -> Decision:
        """校验动作等级是否超过配置上限。"""
        if required not in ACTION_LEVELS:
            return self._record(self._deny("format", f"未知动作等级 {required!r}", target=target), "level")
        if ACTION_LEVELS.index(required) > self.scope.level_index:
            return self._record(
                self._deny(
                    "action-level",
                    f"动作等级 {required} 超过当前上限 {self.scope.exploit_level}"
                    + (f"（{reason}）" if reason else ""),
                    target=target,
                    hint=f"当前仅允许 {self.scope.exploit_level} 级动作；如需更高等级请由授权人修改配置",
                ),
                "level",
            )
        return self._record(Decision(True, category="action-level", target=target, reason=f"等级 {required} 允许"), "level")

    def check_destructive(self, *, target: str = "", reason: str = "") -> Decision:
        """破坏性动作总闸：需 destructive=true 且目标在 destructive_hosts。"""
        if not self.scope.destructive:
            return self._record(
                self._deny("destructive", "破坏性动作总开关为关闭状态", target=target, hint="改用非破坏性验证手法"),
                "destructive",
            )
        host = (parse_target(target).host.lower() if target else "")
        if not host or host not in self._destructive_hosts:
            return self._record(
                self._deny(
                    "destructive",
                    f"目标 {host or '(未指定)'} 未列入 destructive_hosts",
                    target=target,
                    hint="破坏性动作仅对显式列入 destructive_hosts 的主机开放",
                ),
                "destructive",
            )
        return self._record(Decision(True, category="destructive", target=target, reason="破坏性动作已授权"), "destructive")

    # ------------------------------------------------------------ 参数/命令
    def check_argv(self, argv: Iterable[str], *, tool: str = "", target: str = "", required_level: str = "detect") -> Decision:
        """校验外部二进制参数：硬禁止清单 + 动作等级 + 目标范围。"""
        argv = [str(a) for a in argv]
        text = " ".join(argv)
        for pattern, why in _FORBIDDEN_ARGV:
            if pattern.search(text):
                return self._record(
                    self._deny(
                        "forbidden",
                        f"参数命中硬禁止规则 {pattern.pattern!r}：{why}",
                        target=target,
                        hint="该操作在实训环境中永不允许，请改用非破坏性验证",
                        tool=tool,
                    ),
                    "argv",
                )
        level_decision = self.check_action_level(required_level, target=target, reason=f"工具 {tool or argv[:1]}")
        if level_decision.denied:
            return level_decision
        if target:
            target_decision = self.check_target(target, action="argv")
            if target_decision.denied:
                return target_decision
        return self._record(
            Decision(True, category="argv", target=target, reason="参数合规", detail={"tool": tool, "argv": argv[:8]}),
            "argv",
        )

    def check_payload(self, payload: str, *, target: str = "") -> Decision:
        """校验注入/上传/命令载荷中的危险模式。"""
        for pattern, why in _FORBIDDEN_PAYLOAD:
            if pattern.search(payload or ""):
                return self._record(
                    self._deny("forbidden", f"载荷命中硬禁止规则 {pattern.pattern!r}：{why}", target=target, hint="仅做无害验证，禁止破坏数据"),
                    "payload",
                )
        return self._record(Decision(True, category="forbidden", target=target, reason="载荷合规"), "payload")

    def check_http(self, url: str, *, method: str = "GET", body: str = "", required_level: str = "detect") -> Decision:
        """校验 HTTP 动作：目标范围 + 方法策略 + 载荷（URL 查询串与请求体都扫描）。"""
        method = (method or "GET").upper()
        try:
            scheme = urlsplit(url).scheme.lower()
        except ValueError as exc:
            return self._record(self._deny("format", f"非法 HTTP URL: {exc}", target=url), "http")
        if scheme not in ("http", "https"):
            return self._record(
                self._deny("format", "HTTP 出口只接受 http(s):// URL", target=url), "http"
            )
        target_decision = self.check_target(url, action=f"http.{method}")
        if target_decision.denied:
            return target_decision
        if method in DESTRUCTIVE_METHODS:
            if not self.scope.exploit_level == "exploit":
                return self._record(
                    self._deny("action-level", f"{method} 属破坏性方法，需要 exploit 等级", target=url), "http"
                )
            destructive = self.check_destructive(target=url, reason=f"{method} 请求")
            if destructive.denied:
                return destructive
        for label, blob in (
            ("query", unquote_plus(urlsplit(url).query)),
            ("body", body or ""), ("body", unquote_plus(body or "")),
        ):
            if not blob:
                continue
            payload_decision = self.check_payload(blob, target=url)
            if payload_decision.denied:
                payload_decision.detail["where"] = label
                return payload_decision
        level_decision = self.check_action_level(required_level, target=url, reason=f"HTTP {method}")
        if level_decision.denied:
            return level_decision
        return self._record(Decision(True, category="http", target=url, reason=f"{method} 合规"), "http")

    def check_http_redirect(self, source: str, destination: str) -> Decision:
        """自动跳转仅允许同源；目标本身授权不代表允许转发当前请求。"""
        try:
            before, after = urlsplit(source), urlsplit(destination)
            origins = [
                (parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme.lower() == "https" else 80))
                for parts in (before, after)
            ]
        except ValueError as exc:
            return self._record(self._deny("format", f"非法重定向 URL: {exc}", target=destination), "http.redirect")
        if origins[0] != origins[1] or after.username is not None or after.password is not None:
            return self._record(
                self._deny(
                    "redirect", "不自动跟随异源或含凭据的重定向", target=destination,
                    hint="将跳转目标作为独立检查重新提交授权控制器",
                ), "http.redirect",
            )
        return self._record(
            Decision(True, category="redirect", target=destination, reason="同源重定向，仍需逐跳校验请求"),
            "http.redirect",
        )

    # ---------------------------------------------------------------- 限速
    def throttle(self, host: str = "", tokens: float = 1.0) -> float:
        """消耗令牌并返回建议等待秒数（调用方决定是否 sleep）。"""
        return self.limiter.acquire(host, tokens)

    def check_brute_scale(self, attempts: int, *, target: str = "") -> Decision:
        """爆破规模上限校验（防止把靶机打成 DoS）。"""
        if attempts > self.scope.max_brute_attempts:
            return self._record(
                self._deny(
                    "rate",
                    f"爆破尝试次数 {attempts} 超过上限 {self.scope.max_brute_attempts}",
                    target=target,
                    hint="缩小字典或分批进行；单次爆破不超过 max_brute_attempts",
                ),
                "brute",
            )
        return self._record(Decision(True, category="rate", target=target, reason="爆破规模合规"), "brute")

    # ---------------------------------------------------------------- 视图
    def summary(self) -> dict[str, Any]:
        return {
            "exploit_level": self.scope.exploit_level,
            "destructive": self.scope.destructive,
            "allow_public": self.scope.allow_public,
            "authorized_cidrs": list(self.scope.authorized_cidrs),
            "authorized_hosts": list(self.scope.authorized_hosts),
            "deny_cidrs": list(self.scope.deny_cidrs),
            "stats": dict(self.stats),
            "denials": [d.to_dict() for d in self.denials[-20:]],
        }


def is_target_in_scope(scope: ScopeConfig, raw: str) -> bool:
    """免审计的一次性判定（给 CLI/评测做预检用）。"""
    return ScopeGuard(scope).check_target(raw).allowed
