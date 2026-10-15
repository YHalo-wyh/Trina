"""docker compose 靶场适配（Vulhub 等「一键起靶场」场景）。

行为
----
- 有 `docker`（compose 插件）或 `docker-compose` 时：`docker compose -f <file> up -d`
  起靶场，然后轮询 `wait_ports` 直到就绪（默认最长 120s）；`teardown()` 用 `down -v` 清理。
- 无 docker 时**不抛异常**：`prepare()` 返回空 targets + notes，`status()["detail"]`
  说明如何在宿主机 / Docker Desktop / WSL docker 集成中补齐。

外部命令全部经 `run_external()` 执行（shell=False + 护栏 `check_argv` + 审计哈希链），
适配器自己只提供最小 `ToolContext`（护栏 + 审计 + 限速）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

from ..tools.external import installed, run_external, which
from .base import PROJECT_ROOT, RangeAdapter, RangeInfo, RangeTarget, parse_targets, tcp_probe


class _ExecContext:
    """适配器内的最小 ToolContext：满足 `run_external()` 所需的护栏 / 审计 / 限速。"""

    def __init__(self, config, actor: str = "trina-range"):
        from ..audit import AuditLog
        from ..scope import ScopeGuard

        self.config = config
        self.guard = ScopeGuard(config.scope)
        self.audit = AuditLog.open(config.audit_dir, actor=actor)
        self.deadline: float | None = None

    def remaining(self) -> float | None:
        return None

    def throttle(self, host: str = "", tokens: float = 1.0) -> None:
        wait = self.guard.throttle(host, tokens)
        if wait > 0:
            time.sleep(min(wait, 5.0))


def _parse_wait_ports(raw: Any) -> list[tuple[str, int]]:
    """`wait_ports` 支持 `["127.0.0.1:8080", {host, port}]` 两种写法。"""
    out: list[tuple[str, int]] = []
    for item in raw or []:
        if isinstance(item, str):
            host, _, port_str = item.rpartition(":")
            if host and port_str.isdigit():
                out.append((host, int(port_str)))
        elif isinstance(item, Mapping):
            host = str(item.get("host") or "127.0.0.1")
            port = item.get("port")
            if port:
                out.append((host, int(port)))
    return out


class DockerComposeRangeAdapter(RangeAdapter):
    """基于 docker compose 的靶场（Vulhub / VulnStack 单机版等）。"""

    #: 依次尝试的 compose 调用方式。
    COMPOSE_CANDIDATES = (("docker", ("docker", "compose")), ("docker-compose", ("docker-compose",)))

    def __init__(self, config, spec: Mapping[str, Any] | None = None, path=None):
        super().__init__(config, spec, path)
        self.startup_timeout = float(self.spec.get("startup_timeout") or 120.0)
        self.poll_interval = float(self.spec.get("poll_interval") or 2.0)
        self.action_level = str(self.spec.get("action_level") or "verify")
        self.instances: list[dict[str, Any]] = self._parse_instances()
        self.wait_ports: list[tuple[str, int]] = self._collect_wait_ports()
        self._ctx: _ExecContext | None = None
        self._info: RangeInfo | None = None

    # ------------------------------------------------------------ 配置解析
    def _resolve_file(self, raw: str) -> str:
        """compose 文件路径：相对路径按项目根解析，绝对路径原样返回。"""
        text = str(raw or "").strip()
        if not text:
            return ""
        path = Path(text)
        return str(path if path.is_absolute() else PROJECT_ROOT / path)

    def _parse_instances(self) -> list[dict[str, Any]]:
        raw_instances = self.spec.get("instances") or []
        instances: list[dict[str, Any]] = []
        if raw_instances:
            for idx, item in enumerate(raw_instances):
                if not isinstance(item, Mapping):
                    continue
                compose_file = self._resolve_file(item.get("compose_file") or "")
                instances.append(
                    {
                        "name": str(item.get("name") or f"{self.name}-{idx + 1}"),
                        "compose_file": compose_file,
                        "description": str(item.get("description") or ""),
                        "targets": parse_targets(item.get("targets")),
                        "wait_ports": _parse_wait_ports(item.get("wait_ports")),
                    }
                )
        else:
            instances.append(
                {
                    "name": self.name,
                    "compose_file": self._resolve_file(self.spec.get("compose_file") or ""),
                    "description": self.description,
                    "targets": parse_targets(self.spec.get("targets")),
                    "wait_ports": [],
                }
            )
        return instances

    def _collect_wait_ports(self) -> list[tuple[str, int]]:
        ports: list[tuple[str, int]] = _parse_wait_ports(self.spec.get("wait_ports"))
        for inst in self.instances:
            ports.extend(inst["wait_ports"])
            for target in inst["targets"]:
                if target.port:
                    ports.append((target.host, target.port))
        seen: dict[tuple[str, int], None] = {}
        for host, port in ports:
            seen[(host, port)] = None
        return list(seen)

    def _targets(self) -> list[RangeTarget]:
        targets: list[RangeTarget] = []
        for inst in self.instances:
            targets.extend(inst["targets"])
        if not targets:
            targets = parse_targets(self.spec.get("targets"))
        return targets

    # ------------------------------------------------------------ 可用性
    def compose_argv(self) -> list[str] | None:
        """返回可用的 compose 命令前缀（如 `["docker", "compose"]`）。"""
        for binary, prefix in self.COMPOSE_CANDIDATES:
            if which(binary):
                return list(prefix)
        return None

    def _availability(self) -> tuple[bool, str]:
        if not self.instances or not any(inst["compose_file"] for inst in self.instances):
            return False, "yaml 未声明 compose_file（或 instances[].compose_file）"
        missing = [
            inst["compose_file"]
            for inst in self.instances
            if not inst["compose_file"] or not Path(inst["compose_file"]).exists()
        ]
        if missing:
            return False, f"compose 文件不存在: {', '.join(missing)}"
        prefix = self.compose_argv()
        if prefix is None:
            return False, "未找到 docker / docker-compose 命令"
        return True, f"可用 compose 命令: {' '.join(prefix)}"

    def _remediation(self) -> str:
        return (
            "补齐方式：① 在宿主机（或 Docker Desktop）里 cd 到 compose 文件目录执行 "
            "`docker compose up -d`，再把 targets 的 127.0.0.1 换成宿主可达地址；"
            "② WSL 里启用 docker 集成（Docker Desktop → Settings → Resources → WSL Integration），"
            "或在 WSL 内安装 docker 引擎后 `sudo service docker start`；"
            "③ 无 docker 时改用「已在运行的 Web 靶场」适配器（adapter: http_range + base_url）。"
        )

    def status(self) -> dict:
        targets = self._targets()
        ok, detail = self._availability()
        if ok:
            return self.status_dict(True, f"{detail}；靶场未启动，prepare() 会执行 up -d 并轮询端口", targets)
        if self.compose_argv() is None:
            return self.status_dict(False, f"{detail}；{self._remediation()}", targets)
        return self.status_dict(False, detail, targets)

    # ------------------------------------------------------------ 生命周期
    def _context(self) -> _ExecContext:
        if self._ctx is None:
            self._ctx = _ExecContext(self.config)
        return self._ctx

    def _compose(self, instance: Mapping[str, Any], args: list[str]) -> Any:
        """在指定实例的 compose 文件上执行子命令（护栏拒绝时返回 None 并记录原因）。"""
        prefix = self.compose_argv()
        if prefix is None:
            return None
        argv = [*prefix, "-f", str(instance["compose_file"]), *args]
        try:
            return run_external(
                argv,
                self._context(),
                tool="docker",
                required_level=self.action_level,
                timeout=self.startup_timeout + 60.0,
            )
        except Exception as exc:  # ScopeViolation 及其它异常都不应让 prepare() 崩掉
            return _FailedRun(str(exc), " ".join(argv))

    def prepare(self) -> RangeInfo:
        """起靶场并轮询端口就绪；任何失败都写成 notes，不抛异常。"""
        notes: list[str] = []
        targets = self._targets()
        ok, detail = self._availability()
        if not ok:
            notes.append(detail)
            notes.append(self._remediation())
        else:
            notes.append(detail)
            failed: list[str] = []
            for inst in self.instances:
                if not inst["compose_file"]:
                    failed.append(f"{inst['name']}: 缺少 compose_file")
                    continue
                run = self._compose(inst, ["up", "-d"])
                if run is None:
                    failed.append(f"{inst['name']}: 护栏拒绝或无法执行")
                elif not run.ok:
                    failed.append(f"{inst['name']}: {run.error or ('exit ' + str(run.code))}")
            if failed:
                notes.append("compose up 失败: " + "; ".join(failed))
            if not failed and self.wait_ports:
                notes.append(self._wait_ready())
            elif not failed:
                notes.append("已执行 up -d；yaml 未声明 wait_ports，跳过端口轮询")
        if self.notes:
            notes.append(self.notes)
        info = RangeInfo(
            name=self.name,
            kind=self.kind,
            targets=targets,
            ground_truth=self.ground_truth(),
            flag_patterns=self.flag_patterns(),
            notes="；".join(n for n in notes if n),
            source=self.source,
        )
        self._info = info
        self._set_detail(info.notes)
        return info

    def _wait_ready(self) -> str:
        """轮询 `wait_ports` 直到全部可连通或超时。"""
        pending = list(self.wait_ports)
        deadline = time.monotonic() + self.startup_timeout
        while pending and time.monotonic() < deadline:
            pending = [(host, port) for host, port in pending if not tcp_probe(host, port, timeout=2.0)]
            if pending:
                time.sleep(self.poll_interval)
        if pending:
            return "端口就绪轮询超时（%.0fs），仍不可达: %s" % (self.startup_timeout, ", ".join(f"{h}:{p}" for h, p in pending))
        return f"端口就绪轮询通过（{len(self.wait_ports)} 个端口可连通）"

    def teardown(self) -> None:
        """`docker compose down -v` 关闭并清理自建靶场（失败只记录，不抛异常）。"""
        for inst in self.instances:
            if not inst["compose_file"]:
                continue
            self._compose(inst, ["down", "-v"])

    def info(self) -> RangeInfo:
        return self._info or self._info_snapshot()

    def primary_target(self) -> str:
        targets = self._targets()
        if not targets:
            return ""
        first = targets[0]
        return first.url or first.authority

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data.update(
            {
                "compose": [
                    {"name": inst["name"], "compose_file": inst["compose_file"], "targets": [t.to_dict() for t in inst["targets"]]}
                    for inst in self.instances
                ],
                "wait_ports": [f"{h}:{p}" for h, p in self.wait_ports],
                "docker": installed("docker", "docker-compose"),
            }
        )
        return data


class _FailedRun:
    """`run_external` 不可用（护栏拒绝 / 执行异常）时的等价返回。"""

    def __init__(self, error: str, command: str = ""):
        self.ok = False
        self.code = -1
        self.stdout = ""
        self.stderr = ""
        self.error = error
        self.command = command
        self.duration = 0.0

    @property
    def output(self) -> str:
        return self.stderr or self.error


__all__ = ["DockerComposeRangeAdapter"]
