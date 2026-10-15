"""侦察类工具：端口扫描 / 服务探测 / HTTP 指纹 / TLS 证书信息。

设计原则
--------
* 四个工具全部声明为 `detect` 级（纯只读），不改变目标状态。
* 外部二进制优先：存在 `nmap` 时走 `run_external(..., required_level="detect")`
  并解析 `-oX -` 的 XML；缺失或解析失败则回退内置 asyncio TCP connect 扫描。
* 出网只走 `ctx.http`；外部命令只走 `run_external`。所有批量连接按批
  `ctx.throttle(host, tokens=...)` 限速，避免把靶机打成 DoS。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
import ssl
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from typing import Any

from ..scope import parse_target
from .external import run_external, which
from .http import normalize_url
from .registry import ParamSpec, Tool, ToolContext, ToolResult, ToolSpec, register

# ---------------------------------------------------------------------------
# 常量：端口 → 服务名映射与默认端口集
# ---------------------------------------------------------------------------

#: 常见端口到服务名的内置映射（扫描结果的服务名兜底）
PORT_SERVICE: dict[int, str] = {
    20: "ftp-data",
    21: "ftp",
    22: "ssh",
    23: "telnet",
    25: "smtp",
    53: "dns",
    67: "dhcp",
    69: "tftp",
    80: "http",
    81: "http",
    88: "kerberos",
    110: "pop3",
    111: "rpcbind",
    123: "ntp",
    135: "msrpc",
    137: "netbios-ns",
    138: "netbios-dgm",
    139: "netbios-ssn",
    143: "imap",
    161: "snmp",
    162: "snmptrap",
    389: "ldap",
    443: "https",
    445: "smb",
    465: "smtps",
    500: "isakmp",
    512: "exec",
    513: "login",
    514: "shell",
    587: "smtp-submission",
    631: "ipp",
    636: "ldaps",
    873: "rsync",
    993: "imaps",
    995: "pop3s",
    1080: "socks",
    1433: "mssql",
    1521: "oracle",
    1723: "pptp",
    2049: "nfs",
    2181: "zookeeper",
    2375: "docker",
    2379: "etcd",
    3000: "http",
    3128: "http-proxy",
    3260: "iscsi",
    3306: "mysql",
    3389: "rdp",
    4369: "erlang-epmd",
    4443: "https",
    5000: "http",
    5432: "postgres",
    5601: "elasticsearch-kibana",
    5672: "amqp",
    5900: "vnc",
    5984: "couchdb",
    6379: "redis",
    6443: "kubernetes-api",
    7001: "weblogic",
    7002: "weblogic-ssl",
    8000: "http",
    8008: "http",
    8009: "ajp",
    8080: "http",  # 常见 phpMyAdmin/Web 控制台
    8081: "http",
    8086: "influxdb",
    8088: "http",
    8161: "activemq",
    8443: "https",
    8500: "consul",
    8888: "http",
    8983: "solr",
    9000: "http",
    9042: "cassandra",
    9090: "http",
    9092: "kafka",
    9200: "elasticsearch",
    9300: "elasticsearch-transport",
    9418: "git",
    10000: "webmin",
    10250: "kubelet",
    11211: "memcached",
    27017: "mongodb",
    27018: "mongodb",
    50000: "sap",
    50070: "hadoop-namenode",
}

#: 内置「快速」端口集（无外部工具时的默认扫描范围）
DEFAULT_QUICK_PORTS: list[int] = [
    21, 22, 23, 25, 53, 80, 81, 110, 111, 135, 139, 143, 443, 445, 465, 587,
    993, 995, 1080, 1433, 1521, 1723, 2049, 2181, 2375, 3000, 3306, 3389, 4443,
    5000, 5432, 5601, 5672, 5900, 5984, 6379, 7001, 8000, 8008, 8080, 8081,
    8443, 8888, 9000, 9090, 9200, 11211, 27017, 50000,
]

#: 关键字 → 端口文本（configs 里没配时兜底）
BUILTIN_PORT_SETS: dict[str, str] = {
    "quick": ",".join(str(p) for p in DEFAULT_QUICK_PORTS),
    "default": ",".join(str(p) for p in DEFAULT_QUICK_PORTS),
    "默认": ",".join(str(p) for p in DEFAULT_QUICK_PORTS),
    "top100": ",".join(str(p) for p in DEFAULT_QUICK_PORTS),
    "full": "1-65535",
    "all": "1-65535",
}


def parse_port_spec(spec: str, ranges: dict[str, str] | None = None) -> list[int]:
    """把 `quick` / `80,443` / `1-1000` / `full` 解析为去重升序的端口列表。"""
    raw = (spec or "quick").strip()
    table = {k.lower(): v for k, v in BUILTIN_PORT_SETS.items()}
    for key, value in (ranges or {}).items():
        table[str(key).lower()] = str(value)
    text = table.get(raw.lower(), raw)
    return sorted({p for p in _expand_port_text(text) if 1 <= p <= 65535})


def _expand_port_text(text: str) -> list[int]:
    """解析 `80,443` / `1-1000` 形式的端口文本。"""
    ports: set[int] = set()
    for chunk in str(text).replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            if start.strip().isdigit() and end.strip().isdigit():
                lo, hi = int(start), int(end)
                lo, hi = min(lo, hi), max(lo, hi)
                ports.update(range(lo, hi + 1))
        elif chunk.isdigit():
            ports.add(int(chunk))
    return sorted(ports)


def apply_max_ports(ports: Sequence[int], max_ports: int) -> tuple[list[int], bool]:
    """安全阀：超过上限时**优先保留高价值端口**再截断。返回 (端口, 是否截断)。"""
    ordered = sorted({int(p) for p in ports})
    if max_ports <= 0 or len(ordered) <= max_ports:
        return ordered, False
    present = set(ordered)
    priority = [p for p in DEFAULT_QUICK_PORTS if p in present]
    rest = [p for p in ordered if p not in set(priority)]
    return sorted(set((priority + rest)[:max_ports])), True


def compress_ports(ports: Sequence[int]) -> str:
    """把端口列表压缩成 `22,80,8000-8010` 形式（给 nmap 传参用）。"""
    ordered = sorted({int(p) for p in ports})
    chunks: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        chunks.append(str(ordered[i]) if j == i else f"{ordered[i]}-{ordered[j]}")
        i = j + 1
    return ",".join(chunks)


# ---------------------------------------------------------------------------
# 内置 TCP connect 扫描
# ---------------------------------------------------------------------------


async def scan_ports_async(
    host: str,
    ports: Sequence[int],
    *,
    timeout: float = 1.0,
    concurrency: int = 8,
    batch_size: int = 25,
    throttle: Callable[[str, float], None] | None = None,
    deadline: float | None = None,
    stats: dict[str, int] | None = None,
) -> list[int]:
    """异步 TCP connect 扫描；返回开放端口（升序去重）。

    `stats` 可选地回填 `open/closed/filtered/error` 计数，用于判定主机存活。
    """

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    opened: list[int] = []

    def bump(key: str) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    async def probe(port: int) -> None:
        async with sem:
            writer = None
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=timeout)
            except ConnectionRefusedError:
                bump("closed")  # 收到 RST → 主机存活、端口关闭
                return
            except socket.gaierror:
                bump("error")
                return
            except (asyncio.TimeoutError, TimeoutError):
                bump("filtered")  # 无应答 → 被过滤或主机不可达
                return
            except OSError:
                bump("closed")
                return
            except ValueError:
                bump("error")
                return
            opened.append(int(port))
            bump("open")
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    ordered = list(ports)
    for start in range(0, len(ordered), max(1, batch_size)):
        if deadline is not None and time.monotonic() >= deadline:
            break
        chunk = ordered[start : start + batch_size]
        await asyncio.gather(*(probe(p) for p in chunk), return_exceptions=True)
        if throttle is not None:
            throttle(host, float(len(chunk)))
    return sorted(set(opened))


def builtin_scan(
    host: str,
    ports: Sequence[int],
    *,
    timeout: float = 1.0,
    concurrency: int = 8,
    throttle: Callable[[str, float], None] | None = None,
    deadline: float | None = None,
    stats: dict[str, int] | None = None,
) -> list[int]:
    """同步包装：在独立事件循环里跑 TCP connect 扫描。"""
    return asyncio.run(
        scan_ports_async(
            host,
            ports,
            timeout=timeout,
            concurrency=concurrency,
            throttle=throttle,
            deadline=deadline,
            stats=stats,
        )
    )


# ---------------------------------------------------------------------------
# nmap XML 解析
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def parse_nmap_xml(xml_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """解析 `nmap -oX -` 输出，返回 (ports, hosts)。"""
    root = ET.fromstring(xml_text)
    ports: list[dict[str, Any]] = []
    hosts: list[dict[str, Any]] = []
    for host_el in root.iter():
        if _local(host_el.tag) != "host":
            continue
        addr = ""
        for child in host_el:
            if _local(child.tag) == "address" and child.get("addrtype") in ("ipv4", "ipv6"):
                addr = child.get("addr") or ""
                break
        status_el = next((c for c in host_el if _local(c.tag) == "status"), None)
        alive = status_el is not None and status_el.get("state") == "up"
        hosts.append({"host": addr, "alive": alive})
        ports_el = next((c for c in host_el if _local(c.tag) == "ports"), None)
        if ports_el is None:
            continue
        for port_el in ports_el:
            if _local(port_el.tag) != "port":
                continue
            state_el = next((c for c in port_el if _local(c.tag) == "state"), None)
            if state_el is None or state_el.get("state") != "open":
                continue
            try:
                port_num = int(port_el.get("portid", "0"))
            except ValueError:
                continue
            svc_el = next((c for c in port_el if _local(c.tag) == "service"), None)
            name = (svc_el.get("name") if svc_el is not None else "") or ""
            version = " ".join(
                filter(
                    None,
                    [
                        svc_el.get("product", "") if svc_el is not None else "",
                        svc_el.get("version", "") if svc_el is not None else "",
                        svc_el.get("extrainfo", "") if svc_el is not None else "",
                    ],
                )
            ).strip()
            ports.append(
                {
                    "host": addr,
                    "port": port_num,
                    "service": name or PORT_SERVICE.get(port_num, "unknown"),
                    "version": version,
                    "protocol": port_el.get("protocol", "tcp"),
                }
            )
    return ports, hosts


# ---------------------------------------------------------------------------
# 工具 1：port_scan
# ---------------------------------------------------------------------------


class PortScanTool(Tool):
    spec = ToolSpec(
        name="port_scan",
        summary="TCP 端口扫描：识别开放端口与服务（优先 nmap，缺失时内置 connect 扫描）",
        category="recon",
        action_level="detect",
        external_bin="nmap",
        produces=["ports", "hosts"],
        params={
            "target": ParamSpec("str", "目标主机（IP/域名/host:port）", required=True, example="127.0.0.1"),
            "ports": ParamSpec(
                "str",
                "端口范围：quick(内置常用集)/default、\"80,443\"、\"1-1000\"、full",
                required=False,
                default="quick",
                example="quick",
            ),
            "timeout": ParamSpec("float", "单端口连接超时（秒）", required=False, default=1.0),
            "max_ports": ParamSpec("int", "端口数量安全阀，超出则优先保留高价值端口后截断", required=False, default=4096),
        },
        examples=["port_scan(target='127.0.0.1', ports='quick')", "port_scan(target='10.0.0.5', ports='1-1000')"],
        notes="nmap 存在时执行 `nmap -Pn -sV --version-light -oX - <target> -p <ports>`；失败自动降级到内置扫描。",
    )

    def run(
        self,
        ctx: ToolContext,
        target: str,
        ports: str = "quick",
        timeout: float = 1.0,
        max_ports: int = 4096,
    ) -> ToolResult:
        started = time.monotonic()
        info = parse_target(target)
        host = info.host
        ranges = dict(getattr(ctx.config.tools, "port_ranges", {}) or {})
        port_list, truncated = apply_max_ports(parse_port_spec(ports, ranges), int(max_ports))
        if not port_list:
            return ToolResult(ok=False, error=f"端口范围无法解析: {ports!r}", summary=f"端口参数非法: {ports}")

        deadline = ctx.deadline if ctx.deadline is not None else None
        open_ports: list[dict[str, Any]] = []
        hosts: list[dict[str, Any]] = []
        engine = "builtin"
        raw = ""

        # 1) 外部 nmap 优先
        if which("nmap"):
            spec_arg = compress_ports(port_list)
            run = run_external(
                ["nmap", "-Pn", "-sV", "--version-light", "-oX", "-", host, "-p", spec_arg],
                ctx,
                tool="nmap",
                target=host,
                required_level="detect",
            )
            if run.ok and run.stdout.strip():
                try:
                    parsed_ports, parsed_hosts = parse_nmap_xml(run.stdout)
                except ET.ParseError:
                    parsed_ports, parsed_hosts = [], []
                if parsed_hosts:
                    open_ports, hosts, engine = parsed_ports, parsed_hosts, "nmap"
                    raw = run.output()[:20000]

        # 2) 内置扫描兜底
        stats: dict[str, int] = {}
        if engine == "builtin":
            scanned_ports = builtin_scan(
                host,
                port_list,
                timeout=float(timeout),
                concurrency=int(ctx.config.scope.max_host_concurrency),
                throttle=lambda h, n: ctx.throttle(h, tokens=n),
                deadline=deadline,
                stats=stats,
            )
            # 有开放端口或收到过 RST（closed）即判定主机存活
            alive = bool(scanned_ports) or stats.get("closed", 0) > 0
            hosts = [{"host": host, "alive": alive}]
            open_ports = [
                {
                    "host": host,
                    "port": port,
                    "service": PORT_SERVICE.get(port, "unknown"),
                    "version": "",
                    "protocol": "tcp",
                }
                for port in scanned_ports
            ]

        facts = {
            "ports": open_ports,
            "hosts": hosts,
        }
        note = f"共扫描 {len(port_list)} 个端口，开放 {len(open_ports)} 个（engine={engine}）"
        if truncated:
            note += f"；端口数超过 max_ports={max_ports}，已优先保留常用端口并截断"
        summary = f"端口扫描 {host}: {len(open_ports)} 个开放端口 / 扫描 {len(port_list)} 个 (engine={engine})"
        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "open_ports": open_ports[:300],
                "open_ports_truncated": len(open_ports) > 300,
                "scanned": len(port_list),
                "engine": engine,
                "ports_truncated": truncated,
                "probe_stats": stats,
                "note": note,
            },
            raw=raw,
            command=f"nmap -Pn -sV --version-light -oX - {host} -p {compress_ports(port_list)}" if engine == "nmap" else "",
            facts=facts,
            next_hints=["对开放端口运行 service_probe 抓取服务版本", "对 Web 端口运行 http_fingerprint"],
            category="recon",
            duration=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# 工具 2：service_probe
# ---------------------------------------------------------------------------

BANNER_TIMEOUT = 2.0
MAX_PROBE_PORTS = 512

#: 会先说 HTTP 的端口（需要主动发 HEAD 才会回应）
HTTP_LIKE_PORTS = {
    80, 81, 88, 443, 591, 3000, 5000, 5601, 7001, 7002, 8000, 8001, 8008, 8009,
    8080, 8081, 8088, 8161, 8443, 8888, 8983, 9000, 9090, 9200, 10000,
}

SSH_BANNER_RE = re.compile(r"SSH-([\d.]+)-(\S+)")
HTTP_STATUS_RE = re.compile(r"^HTTP/[\d.]+\s+(\d{3})")
HTTP_SERVER_RE = re.compile(r"^Server:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
FTP_BANNER_RE = re.compile(r"^220[- ](.*)$", re.MULTILINE)
SMTP_BANNER_RE = re.compile(r"^220[- ](.*?)\s*(ESMTP|SMTP)?\s*(.*)$", re.MULTILINE | re.IGNORECASE)
REDIS_VERSION_RE = re.compile(r"VERSION\s+(\S+)", re.IGNORECASE)
MEMCACHED_VERSION_RE = re.compile(r"^VERSION\s+(\S+)", re.IGNORECASE)


def _probe_payload(port: int, host: str) -> bytes | None:
    """按端口给出极简只读探测载荷。"""
    if port in HTTP_LIKE_PORTS:
        return (
            f"HEAD / HTTP/1.0\r\nHost: {host}\r\nUser-Agent: Trina/0.1 (+authorized-pentest)\r\n"
            "Accept: */*\r\nConnection: close\r\n\r\n"
        ).encode("latin-1", "replace")
    if port == 6379:
        return b"PING\r\n"
    if port == 11211:
        return b"version\r\n"
    return None


def _parse_mysql_handshake(data: bytes) -> str:
    """从 MySQL 服务端主动发送的握手包提取版本号。"""
    if len(data) > 5 and data[4] == 0x0A:
        tail = data[5:]
        end = tail.find(b"\x00")
        if end > 0:
            return tail[:end].decode("latin-1", "replace")
    return ""


def parse_banner(port: int, data: bytes) -> tuple[str, str, str]:
    """解析 banner，返回 (service, version, banner_text)。"""
    text = data.decode("latin-1", "replace")
    banner = text.strip()
    if not banner:
        return PORT_SERVICE.get(int(port), "unknown"), "", ""

    ssh = SSH_BANNER_RE.search(text)
    if ssh:
        return "ssh", ssh.group(2), banner

    mysql_version = _parse_mysql_handshake(data)
    if mysql_version:
        return "mysql", mysql_version, banner

    if text.startswith("+PONG") or text.startswith("-ERR") or text.startswith("-NOAUTH") or text.startswith("+OK"):
        m = REDIS_VERSION_RE.search(text)
        return "redis", m.group(1) if m else "redis", banner
    mem = MEMCACHED_VERSION_RE.search(banner)
    if mem and "VERSION" in banner.upper():
        return "memcached", mem.group(1), banner

    if HTTP_STATUS_RE.match(banner):
        server = HTTP_SERVER_RE.search(text)
        return ("https" if int(port) in (443, 8443) else "http"), (server.group(1) if server else ""), banner

    if banner.startswith("220") and ("ftp" in text.lower() or "vsftpd" in text.lower() or "proftpd" in text.lower()):
        m = FTP_BANNER_RE.search(text)
        return "ftp", (m.group(1).strip() if m else banner), banner
    if banner.startswith("220"):
        m = SMTP_BANNER_RE.search(text)
        version = (m.group(3).strip() if m and m.group(3) else (m.group(2) or "" if m else ""))
        return "smtp", version, banner

    low = text.lower()
    if "postfix" in low:
        return "smtp", "Postfix", banner
    if "exim" in low:
        return "smtp", "Exim", banner
    if "vsftpd" in low:
        return "ftp", "vsFTPd", banner
    if "telnet" in low or "login:" in low:
        return "telnet", "", banner
    if "smtp" in low or "esmtp" in low:
        return "smtp", "", banner
    if "http/" in low:
        return ("https" if int(port) in (443, 8443) else "http"), "", banner
    return PORT_SERVICE.get(int(port), "unknown"), "", banner


def probe_port(host: str, port: int, *, timeout: float = BANNER_TIMEOUT) -> dict[str, Any]:
    """对单个端口做 TCP banner 抓取（必要时发极简只读探测）。"""
    entry: dict[str, Any] = {"host": host, "port": int(port), "protocol": "tcp", "banner": "", "service": "", "version": ""}
    sock = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.settimeout(0.8)
        data = b""
        with contextlib.suppress(socket.timeout, OSError):
            data = sock.recv(4096)
        if not data:
            payload = _probe_payload(int(port), host)
            if payload:
                with contextlib.suppress(OSError):
                    sock.sendall(payload)
                with contextlib.suppress(socket.timeout, OSError):
                    data = sock.recv(8192)
        service, version, banner = parse_banner(int(port), data)
        entry.update(
            {
                "service": service or PORT_SERVICE.get(int(port), "unknown"),
                "version": version,
                "banner": banner[:400],
                "open": True,
            }
        )
    except (OSError, socket.timeout) as exc:
        entry.update({"service": PORT_SERVICE.get(int(port), "unknown"), "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()
    return entry


class ServiceProbeTool(Tool):
    spec = ToolSpec(
        name="service_probe",
        summary="服务探测：对指定端口抓取 banner 并解析服务名/版本（SSH/HTTP/MySQL/Redis/FTP/SMTP 等）",
        category="recon",
        action_level="detect",
        produces=["ports", "services"],
        params={
            "target": ParamSpec("str", "目标主机（IP/域名）", required=True, example="127.0.0.1"),
            "ports": ParamSpec("list", "待探测端口列表，如 [22, 80, 6379]", required=True, example=[22, 80, 3306, 6379]),
        },
        examples=["service_probe(target='127.0.0.1', ports=[22, 80, 6379])"],
        notes="纯只读 banner 抓取；HTTP 端口发一次 `HEAD / HTTP/1.0`，Redis 发 `PING`，MySQL 由服务端主动握手。",
    )

    def run(self, ctx: ToolContext, target: str, ports: list[Any]) -> ToolResult:
        started = time.monotonic()
        info = parse_target(target)
        host = info.host
        wanted: list[int] = []
        for item in ports or []:
            try:
                value = int(str(item).strip())
            except (TypeError, ValueError):
                continue
            if 1 <= value <= 65535:
                wanted.append(value)
        wanted = sorted(set(wanted))
        truncated = len(wanted) > MAX_PROBE_PORTS
        wanted = wanted[:MAX_PROBE_PORTS]
        if not wanted:
            return ToolResult(ok=False, error="ports 为空或非法", summary="service_probe: 无有效端口")

        entries: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        for port in wanted:
            deadline = ctx.deadline
            if deadline is not None and time.monotonic() >= deadline:
                break
            ctx.throttle(host)
            entry = probe_port(host, port)
            entries.append(entry)
            evidence.append(
                {
                    "kind": "tcp_banner",
                    "target": f"{host}:{port}",
                    "service": entry.get("service"),
                    "version": entry.get("version"),
                    "request": {"protocol": "tcp", "probe": "banner" if not _probe_payload(port, host) else "banner+probe"},
                    "response": {"banner": (entry.get("banner") or "")[:300], "error": entry.get("error", "")},
                }
            )

        services = [dict(e) for e in entries]
        found = [e for e in entries if e.get("open")]
        detail = ", ".join(f"{e['port']}/{e['service']}{('/' + e['version']) if e.get('version') else ''}" for e in found[:8])
        summary = f"服务探测 {host}: {len(found)}/{len(entries)} 个端口有响应"
        if detail:
            summary += f"（{detail}）"
        return ToolResult(
            ok=True,
            summary=summary,
            data={"services": services, "probed": len(entries), "truncated": truncated},
            facts={
                "ports": [
                    {
                        "host": host,
                        "port": e["port"],
                        "service": e.get("service", ""),
                        "version": e.get("version", ""),
                        "banner": e.get("banner", ""),
                        "protocol": "tcp",
                    }
                    for e in found
                ]
            },
            evidence=evidence,
            next_hints=["对识别出的 Web 服务运行 http_fingerprint", "对 ssh/mysql/redis 评估弱口令风险（需 verify 级工具）"],
            category="recon",
            duration=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# 工具 3：http_fingerprint
# ---------------------------------------------------------------------------

#: 技术栈指纹：正则 → 技术名（命中即记录）
TECH_SIGNATURES: list[tuple[str, str]] = [
    ("WordPress", r"wp-content|wp-includes|/wp-json|wordpress"),
    ("Discuz", r"discuz|forum\.php\?mod=|static/image/common"),
    ("ThinkPHP", r"thinkphp|/index\.php/[a-z]+/"),
    ("Laravel", r"laravel_session|xsrf-token|laravel"),
    ("Spring Boot", r"whitelabel error page|spring-boot|/actuator"),
    ("Tomcat", r"apache-coyote|coyote/|tomcat"),
    ("Nginx", r"\bnginx(?:/[\d.]+)?"),
    ("Apache", r"\bapache(?:/[\d.]+)?\b"),
    ("ASP.NET", r"asp\.net|x-aspnet-version|__viewstate"),
    ("jQuery", r"jquery[-.]?[\d.]*(?:\.min)?\.js|jquery\.min\.js"),
    ("Vue", r"vue(?:\.runtime)?(?:\.min)?\.js|__vue__|data-v-[0-9a-f]{8}"),
    ("React", r"react(?:-dom)?(?:\.production\.min)?\.js|__react"),
    ("宝塔面板", r"bt-waf|baota|宝塔"),
    ("PHPStudy", r"phpstudy"),
    ("Jenkins", r"x-jenkins|jenkins"),
    ("Druid", r"druid|druidstat"),
]

PHP_VERSION_RE = re.compile(r"PHP/([\d.]+)", re.IGNORECASE)

#: 安全响应头：缺失即记为 info 级 finding
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": "缺少 CSP，可能放大 XSS 与内容注入风险",
    "X-Frame-Options": "缺少 X-Frame-Options，页面可能被 iframe 嵌套（点击劫持）",
    "Strict-Transport-Security": "缺少 HSTS，HTTPS 可被降级到明文 HTTP",
    "X-Content-Type-Options": "缺少 X-Content-Type-Options，浏览器可能 MIME 嗅探导致 XSS",
}

WELL_KNOWN_PATHS = ["/robots.txt", "/sitemap.xml", "/.well-known/security.txt"]

#: robots.txt 中出现这些关键词说明它泄露了值得关注的位置
SENSITIVE_ROBOTS_KEYWORDS = (
    "admin", "login", "manager", "console", "backup", "bak", "config", "upload",
    "api", "phpmyadmin", "database", "db", "git", "svn", "internal", "private", "test",
)


def _sensitive_robots_entries(body: str, limit: int = 45) -> list[str]:
    """从 robots.txt 里挑出含敏感关键词的行（返回归一化的命中关键词，去重）。"""
    hits: list[str] = []
    for raw_line in (body or "").splitlines()[:limit]:
        line = raw_line.strip()
        if not line or line.lower().startswith("#"):
            continue
        lowered = line.lower()
        for keyword in SENSITIVE_ROBOTS_KEYWORDS:
            if keyword in lowered and keyword not in hits:
                hits.append(keyword)
    return hits


def detect_technologies(headers: dict[str, str], body: str) -> list[str]:
    """在响应头 + 响应体里做技术栈正则匹配。"""
    header_text = "\n".join(f"{k}: {v}" for k, v in (headers or {}).items())
    haystack = f"{header_text}\n{body or ''}".lower()
    found: list[str] = []
    for name, pattern in TECH_SIGNATURES:
        if re.search(pattern, haystack, re.IGNORECASE) and name not in found:
            found.append(name)
    php = PHP_VERSION_RE.search(header_text)
    if php:
        found.append(f"PHP/{php.group(1)}")
    elif "phpsessid" in haystack:
        found.append("PHP")
    server = (headers or {}).get("Server", "")
    for label, pattern in (("Nginx", r"nginx"), ("Apache", r"apache"), ("Tomcat", r"tomcat"), ("IIS", r"microsoft-iis")):
        if re.search(pattern, server, re.IGNORECASE) and label not in found:
            found.append(label)
    return found


class HttpFingerprintTool(Tool):
    spec = ToolSpec(
        name="http_fingerprint",
        summary="HTTP 指纹：状态码/标题/响应头/重定向链 + 技术栈识别 + 安全响应头缺失检查 + 常见路径探测",
        category="recon",
        action_level="detect",
        produces=["services", "paths", "findings"],
        params={
            "url": ParamSpec("str", "目标 URL（含 scheme）", required=True, example="http://127.0.0.1/"),
        },
        examples=["http_fingerprint(url='http://127.0.0.1:8080/')"],
        notes="只发 GET/HEAD 只读请求；缺失的安全响应头以 info 级 finding 记入 facts.findings。",
    )

    def run(self, ctx: ToolContext, url: str) -> ToolResult:
        started = time.monotonic()
        result = ctx.http.get(url, required_level="detect")
        if result.error:
            return ToolResult(
                ok=False,
                error=result.error,
                summary=f"http_fingerprint 请求失败: {result.error[:120]}",
                data=result.to_dict(include_body=False),
                duration=time.monotonic() - started,
                category="recon",
            )

        info = parse_target(result.url or url)
        host = info.host
        headers = {k: v for k, v in (result.headers or {}).items()}
        lower_headers = {k.lower(): v for k, v in headers.items()}
        technologies = detect_technologies(headers, result.body)

        missing = [name for name in SECURITY_HEADERS if name.lower() not in lower_headers]
        findings: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = [result.as_evidence(kind="http", note="http_fingerprint 首页探测")]
        for name in missing:
            findings.append(
                {
                    "title": f"缺少安全响应头 {name}",
                    "vuln_class": "security_misconfig_headers",
                    "target": result.url or url,
                    "url": result.url or url,
                    "parameter": name,
                    "method": "GET",
                    "severity": "info",
                    "confidence": "high",
                    "status": "detected",
                    "tool": "http_fingerprint",
                    "description": SECURITY_HEADERS[name],
                    "remediation": f"在 Web 服务器/反代上补充响应头 {name}",
                    "evidence": [result.as_evidence(kind="http", note=f"响应头缺失: {name}")],
                }
            )

        paths: list[dict[str, Any]] = []
        path_status: dict[str, int] = {}
        for path in WELL_KNOWN_PATHS:
            probe_url = normalize_url(result.url or url, path)
            probe = ctx.http.get(probe_url, required_level="detect")
            path_status[path] = probe.status
            if probe.status == 200:
                paths.append({"host": host, "url": probe_url, "path": path, "status": probe.status})
                evidence.append(probe.as_evidence(kind="http", note=f"发现路径 {path}"))
                # robots.txt 里点出管理/备份/源码路径＝信息泄露（只报一次，附命中关键词作证据）
                if path == "/robots.txt":
                    exposed = _sensitive_robots_entries(probe.body or "")
                    if exposed:
                        findings.append(
                            {
                                "title": "robots.txt 泄露敏感路径",
                                "vuln_class": "information_disclosure",
                                "url": probe_url,
                                "severity": "low",
                                "confidence": "medium",
                                "status": "detected",
                                "cwe": "CWE-200",
                                "description": "robots.txt 中列出了管理后台/备份/源码等敏感路径，可直接作为攻击入口。",
                                "impact": "攻击者据此外推后台与备份文件位置，降低入侵成本。",
                                "remediation": "robots.txt 不应列出敏感路径；重要目录改用鉴权与不可枚举命名，并移除线上备份文件。",
                                "evidence": [
                                    probe.as_evidence(kind="http", note="robots.txt 命中敏感路径关键词: " + ", ".join(exposed))
                                ],
                            }
                        )

        services = [{"host": host, "technology": tech} for tech in technologies]

        summary = (
            f"HTTP 指纹 {result.url or url}: {result.status} "
            f"title={result.title or '-'} tech={','.join(technologies) or '-'} 缺失安全头 {len(missing)} 个"
        )
        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "url": result.url or url,
                "status": result.status,
                "title": result.title,
                "server": lower_headers.get("server", ""),
                "powered_by": lower_headers.get("x-powered-by", ""),
                "set_cookie": lower_headers.get("set-cookie", ""),
                "redirects": result.history[:5],
                "technologies": technologies,
                "missing_security_headers": missing,
                "well_known": path_status,
                "content_length": result.length,
            },
            facts={"services": services, "paths": paths, "findings": findings},
            evidence=evidence,
            next_hints=["结合发现的路径/技术栈进入漏洞探测阶段", "对缺失的安全响应头做合规整改"],
            category="recon",
            duration=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# 工具 4：tls_info
# ---------------------------------------------------------------------------

WEAK_PROTOCOLS = {"SSLv2", "SSLv3", "TLSv1", "TLSv1.1"}
WEAK_CIPHER_MARKERS = ("RC4", "3DES", "DES", "NULL", "MD5", "EXPORT", "anon")


def _dn_to_dict(field: Any) -> dict[str, str]:
    """把 getpeercert 的 subject/issuer 结构拉平成 dict。"""
    out: dict[str, str] = {}
    if isinstance(field, dict):
        return {str(k): str(v) for k, v in field.items()}
    for rdn in field or ():
        if isinstance(rdn, (list, tuple)):
            for pair in rdn:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    out.setdefault(str(pair[0]), str(pair[1]))
    return out


def _dn_to_text(field: Any) -> str:
    return ", ".join(f"{k}={v}" for k, v in _dn_to_dict(field).items())


def _decode_der_cert(der: bytes) -> dict[str, Any]:
    """用标准库解码 DER 证书（自签名/不可信证书场景）。"""
    decoder = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if not der or decoder is None:
        return {}
    tmp_path = ""
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="ascii") as handle:
            handle.write(pem)
            tmp_path = handle.name
        return dict(decoder(tmp_path))
    except Exception:  # noqa: BLE001 - 证书解析失败不应中断工具
        return {}
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def collect_tls(host: str, port: int, *, timeout: float = 5.0) -> dict[str, Any]:
    """采集 TLS 会话与证书信息（先验证握手，失败再降级不校验）。"""
    info: dict[str, Any] = {
        "host": host,
        "port": int(port),
        "protocol": "",
        "cipher": "",
        "cipher_bits": 0,
        "verified": False,
        "cert": {},
        "error": "",
    }
    sni = host
    try:
        vctx = ssl.create_default_context()
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            with vctx.wrap_socket(raw, server_hostname=sni) as ssock:
                info["protocol"] = ssock.version() or ""
                cipher = ssock.cipher() or ("", "", 0)
                info["cipher"], info["cipher_bits"] = cipher[0], cipher[2] or 0
                info["cert"] = dict(ssock.getpeercert() or {})
                info["verified"] = True
        return info
    except ssl.SSLCertVerificationError as exc:
        info["error"] = f"证书校验失败: {getattr(exc, 'verify_message', '') or exc}"
    except (ssl.SSLError, OSError) as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"

    try:
        nctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        nctx.check_hostname = False
        nctx.verify_mode = ssl.CERT_NONE
        der = b""
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            with nctx.wrap_socket(raw, server_hostname=sni) as ssock:
                info["protocol"] = ssock.version() or ""
                cipher = ssock.cipher() or ("", "", 0)
                info["cipher"], info["cipher_bits"] = cipher[0], cipher[2] or 0
                der = ssock.getpeercert(binary_form=True) or b""
        if der and not info["cert"]:
            info["cert"] = _decode_der_cert(der)
    except (ssl.SSLError, OSError) as exc:
        info["error"] = info["error"] or f"{type(exc).__name__}: {exc}"
    return info


class TlsInfoTool(Tool):
    spec = ToolSpec(
        name="tls_info",
        summary="TLS 证书信息：subject/issuer/SAN/有效期/协议版本/弱套件；过期或自签名记 info 级 finding",
        category="recon",
        action_level="detect",
        produces=["findings"],
        params={
            "target": ParamSpec("str", "目标主机（IP/域名，可含 :port）", required=True, example="127.0.0.1"),
            "port": ParamSpec("int", "TLS 端口", required=False, default=443),
        },
        examples=["tls_info(target='127.0.0.1', port=443)"],
        notes="用标准库 ssl 完成握手；证书不可信时降级到不校验模式仍可读出证书细节。",
    )

    def run(self, ctx: ToolContext, target: str, port: int = 443) -> ToolResult:
        started = time.monotonic()
        info = parse_target(target)
        host = info.host
        use_port = int(info.port or port)
        ctx.throttle(host)
        tls = collect_tls(host, use_port)
        if not tls["protocol"]:
            return ToolResult(
                ok=False,
                error=tls.get("error") or "TLS 握手失败",
                summary=f"tls_info {host}:{use_port} 握手失败: {(tls.get('error') or '')[:120]}",
                data=tls,
                duration=time.monotonic() - started,
                category="recon",
            )

        cert = tls.get("cert") or {}
        subject = _dn_to_dict(cert.get("subject"))
        issuer = _dn_to_dict(cert.get("issuer"))
        san = [f"{kind}:{value}" for kind, value in (cert.get("subjectAltName") or [])]
        not_before = str(cert.get("notBefore", ""))
        not_after = str(cert.get("notAfter", ""))
        days_remaining: int | None = None
        expired = False
        if not_after:
            with contextlib.suppress(ValueError):
                seconds = ssl.cert_time_to_seconds(not_after)
                days_remaining = int((seconds - time.time()) // 86400)
                expired = seconds < time.time()

        self_signed = bool(subject) and subject == issuer
        if "self-signed" in (tls.get("error") or "").lower() or "self signed" in (tls.get("error") or "").lower():
            self_signed = True
        weak_protocol = tls["protocol"] in WEAK_PROTOCOLS
        weak_cipher = any(marker.lower() in (tls["cipher"] or "").lower() for marker in WEAK_CIPHER_MARKERS)

        findings: list[dict[str, Any]] = []
        common = {
            "target": f"{host}:{use_port}",
            "url": f"{host}:{use_port}",
            "tool": "tls_info",
            "severity": "info",
            "confidence": "high",
            "status": "detected",
        }
        evidence = [
            {
                "kind": "tls",
                "target": f"{host}:{use_port}",
                "request": {"protocol": "tcp+tls", "sni": host},
                "response": {
                    "protocol": tls["protocol"],
                    "cipher": tls["cipher"],
                    "subject": _dn_to_text(cert.get("subject")),
                    "issuer": _dn_to_text(cert.get("issuer")),
                    "notAfter": not_after,
                },
            }
        ]
        if expired:
            findings.append(
                {
                    **common,
                    "title": f"TLS 证书已过期（{not_after}）",
                    "vuln_class": "tls_expired_certificate",
                    "parameter": f"{host}:{use_port}",
                    "description": f"证书已于 {not_after} 过期，浏览器会告警，且可能被中间人替换。",
                    "remediation": "更新为有效期内的有效证书。",
                    "evidence": list(evidence),
                }
            )
        if self_signed:
            findings.append(
                {
                    **common,
                    "title": "TLS 证书为自签名",
                    "vuln_class": "tls_self_signed_certificate",
                    "parameter": f"{host}:{use_port}:self-signed",
                    "description": "证书 subject 与 issuer 相同或校验失败，未经受信 CA 签发，无法防中间人。",
                    "remediation": "使用受信任 CA 签发的证书，或在内网统一部署企业 CA。",
                    "evidence": list(evidence),
                }
            )
        if weak_protocol:
            findings.append(
                {
                    **common,
                    "title": f"TLS 使用了过时协议 {tls['protocol']}",
                    "vuln_class": "tls_weak_protocol",
                    "parameter": f"{host}:{use_port}:protocol",
                    "severity": "low",
                    "description": f"协商到 {tls['protocol']}，存在已知降级/破解风险。",
                    "remediation": "仅启用 TLSv1.2 及以上。",
                    "evidence": list(evidence),
                }
            )
        if weak_cipher:
            findings.append(
                {
                    **common,
                    "title": f"TLS 使用了弱加密套件 {tls['cipher']}",
                    "vuln_class": "tls_weak_cipher",
                    "parameter": f"{host}:{use_port}:cipher",
                    "severity": "low",
                    "description": f"协商套件 {tls['cipher']} 含弱算法标记（RC4/3DES/DES/NULL/MD5/EXPORT）。",
                    "remediation": "在服务端禁用弱套件，改用 AEAD 套件（如 AES-GCM/ChaCha20）。",
                    "evidence": list(evidence),
                }
            )

        summary = (
            f"TLS {host}:{use_port} {tls['protocol']} {tls['cipher']} "
            f"subject={subject.get('commonName', '-')} 剩余 {days_remaining if days_remaining is not None else '?'} 天"
        )
        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "host": host,
                "port": use_port,
                "protocol": tls["protocol"],
                "cipher": tls["cipher"],
                "cipher_bits": tls["cipher_bits"],
                "verified": tls["verified"],
                "subject": subject,
                "issuer": issuer,
                "san": san,
                "not_before": not_before,
                "not_after": not_after,
                "days_remaining": days_remaining,
                "expired": expired,
                "self_signed": self_signed,
                "weak_protocol": weak_protocol,
                "weak_cipher": weak_cipher,
                "serial": str(cert.get("serialNumber", "")),
                "error": tls.get("error", ""),
            },
            facts={"findings": findings},
            evidence=evidence,
            next_hints=["若证书自签名/过期，评估中间人风险", "确认是否开放了过时的 TLS 版本"],
            category="recon",
            duration=time.monotonic() - started,
        )


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    PortScanTool(),
    ServiceProbeTool(),
    HttpFingerprintTool(),
    TlsInfoTool(),
]

for _tool in TOOLS:
    register(_tool)

__all__ = [
    "TOOLS",
    "PortScanTool",
    "ServiceProbeTool",
    "HttpFingerprintTool",
    "TlsInfoTool",
    "PORT_SERVICE",
    "DEFAULT_QUICK_PORTS",
    "parse_port_spec",
    "apply_max_ports",
    "compress_ports",
    "scan_ports_async",
    "builtin_scan",
    "parse_nmap_xml",
    "parse_banner",
    "probe_port",
    "detect_technologies",
    "collect_tls",
]
