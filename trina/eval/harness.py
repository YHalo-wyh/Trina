"""评测编排：加载场景 → 逐个执行 → 转 ScenarioResult → 聚合指标 → 落盘报告。

大脑（``trina.brain``）**延迟导入**，因此评测框架可在没有真实 LLM / 大脑的环境下
用注入的 ``runner`` 跑通（单元测试与回归用）。
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from ..config import PROJECT_ROOT, TrinaConfig
from .metrics import (
    ScenarioResult,
    compute_metrics,
    format_metrics_markdown,
    gt_key,
    match_findings,
    metrics_to_json,
)


@dataclass
class ScenarioSpec:
    """一个评测场景的声明（来自 ``trina/eval/scenarios/*.yaml``）。"""

    id: str
    name: str = ""
    kind: str = "web"
    target: str = ""
    goal: str = ""
    adapter: str = "local"
    ground_truth: list[dict[str, Any]] = field(default_factory=list)
    flag_patterns: list[str] = field(default_factory=list)
    max_steps: int = 30
    max_seconds: int = 900
    ports_expected: int = 0
    services_expected: int = 0
    tags: list[str] = field(default_factory=list)
    adversarial: list[str] = field(default_factory=list)
    #: 判定成功的口径：auto(默认，看 agent 状态) | ports(端口覆盖度) | findings(至少命中一条 GT) | flags(拿到 flag)
    success_criteria: str = "auto"
    #: 该场景要求在场的外部夹具（`host:port`），评测器会为缺失的本机夹具自动拉起 lab
    fixtures: list[str] = field(default_factory=list)
    #: llm.backend=replay 时使用的回放脚本（离线评测）。
    replay_script: str = ""
    #: 归属套件（由文件名推断）。
    suite: str = ""
    #: 原始 YAML 条目（保留未知字段）。
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.id

    @classmethod
    def from_dict(cls, data: dict[str, Any], suite: str = "") -> "ScenarioSpec":
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in (data or {}).items() if k in known and k != "raw"}
        kwargs.setdefault("id", str(data.get("name") or "scenario"))
        kwargs.setdefault("suite", suite)
        spec = cls(**kwargs)  # type: ignore[arg-type]
        spec.raw = dict(data or {})
        return spec


def _local_endpoints(spec: ScenarioSpec) -> set[tuple[str, int]]:
    """从场景目标里抽出「本机端口」，用于决定需要拉起哪些 lab 夹具。"""
    import urllib.parse

    target = (spec.target or "").strip()
    if not target or target.startswith("bundle://"):
        return set()
    if "://" in target:
        parts = urllib.parse.urlsplit(target)
        host, port = parts.hostname or "", parts.port
        if host in ("127.0.0.1", "localhost") and port:
            return {(str(host), int(port))}
        return set()
    host, _, port_text = target.rpartition(":")
    host = host or "127.0.0.1"
    if host in ("127.0.0.1", "localhost") and port_text.isdigit():
        return {(host, int(port_text))}
    return set()


class EvalHarness:
    """场景执行编排器。"""

    def __init__(
        self,
        config: TrinaConfig,
        *,
        runner: Callable[[ScenarioSpec], Any] | None = None,
        logger: Callable[..., None] = print,
        autostart_lab: bool = True,
    ):
        self.config = config
        self.runner = runner
        self.logger = logger
        self.autostart_lab = autostart_lab
        self._lab_servers: list[tuple[str, Any]] = []

    # -------------------------------------------------------------- lab 夹具
    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
        import socket

        with socket.socket() as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0

    def start_lab_fixtures(self, specs: list[ScenarioSpec]) -> None:
        """评测依赖的本地靶机（127.0.0.1:9099 / :2222）若没起就自己拉起。

        评测要能一键跑完：外部手工起的靶机常在长跑中被环境回收（SIGTERM），
        与其等偶然失败，不如由评测器保证夹具在场，跑完再关。
        """
        if not self.autostart_lab:
            return
        wanted: set[tuple[str, int]] = set()
        for spec in specs:
            wanted |= _local_endpoints(spec)
            for raw in spec.fixtures or []:
                host, _, port_text = str(raw).rpartition(":")
                if port_text.isdigit():
                    wanted.add((host or "127.0.0.1", int(port_text)))
        for host, port in sorted(wanted):
            if self._port_open(host, port):
                continue
            try:
                if port == 2222:
                    from trina.lab.ssh_target import create_ssh_server

                    server, _thread = create_ssh_server(port=port, host=host)
                    self._lab_servers.append(("ssh", server))
                else:
                    from trina.lab.http_target import create_server

                    server, _thread = create_server(port=port, host=host)
                    self._lab_servers.append(("http", server))
                self.logger(f"[eval] 已自动拉起 lab 靶机 {host}:{port}")
            except OSError as exc:
                self.logger(f"[eval] lab 靶机 {host}:{port} 启动失败: {exc}")

    def stop_lab_fixtures(self) -> None:
        for kind, server in self._lab_servers:
            try:
                if kind == "http":
                    server.shutdown()
                    server.server_close()
                else:
                    server.stop()
            except Exception:  # noqa: BLE001 - 关闭失败不影响评测结论
                pass
        self._lab_servers.clear()

    # -------------------------------------------------------------- 路径
    def scenarios_dir(self) -> Path:
        raw = getattr(self.config.eval, "scenarios_dir", "trina/eval/scenarios")
        path = Path(raw)
        return path if path.is_absolute() else PROJECT_ROOT / path

    def output_dir(self) -> Path:
        raw = getattr(self.config.eval, "output_dir", "reports/eval")
        path = Path(raw)
        return path if path.is_absolute() else PROJECT_ROOT / path

    # -------------------------------------------------------------- 加载
    def load_scenarios(self, suite: str | None = None) -> list[ScenarioSpec]:
        """读取 ``scenarios_dir`` 下所有 ``*.yaml``；``suite`` 按文件名 / tags 过滤。"""
        directory = self.scenarios_dir()
        specs: list[ScenarioSpec] = []
        if not directory.exists():
            return specs
        files = sorted({*directory.glob("*.yaml"), *directory.glob("*.yml")})
        for file in files:
            try:
                data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            except Exception as exc:  # noqa: BLE001
                self.logger(f"[eval] 场景文件解析失败 {file.name}: {exc}")
                continue
            if isinstance(data, dict):
                file_suite = str(data.get("suite") or file.stem)
                items = data.get("scenarios") or []
            elif isinstance(data, list):
                file_suite = file.stem
                items = data
            else:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                specs.append(ScenarioSpec.from_dict(item, suite=file_suite))
        if suite:
            specs = [s for s in specs if self._matches_suite(s, suite)]
        return specs

    @staticmethod
    def _matches_suite(spec: ScenarioSpec, suite: str) -> bool:
        target = suite.strip().lower()
        return (
            spec.suite.lower() == target
            or target in [str(t).lower() for t in spec.tags]
            or spec.id.lower().startswith(target)
            or spec.kind.lower() == target
        )

    # -------------------------------------------------------------- 执行
    def run(self, suite: str | None = None, *, repeat: int | None = None) -> dict[str, Any]:
        """执行场景套件，返回聚合指标 dict（含逐场景明细与报告路径）。"""
        specs = self.load_scenarios(suite)
        rounds = int(repeat if repeat is not None else getattr(self.config.eval, "repeat", 1) or 1)
        rounds = max(rounds, 1)
        runner = self.runner or self.default_runner

        results: list[ScenarioResult] = []
        adversarial_runs: list[dict[str, Any]] = []
        self.start_lab_fixtures(specs)
        try:
            for round_index in range(rounds):
                for spec in specs:
                    result, adv_stats = self._run_one(spec, runner)
                    results.append(result)
                    if adv_stats is not None:
                        adversarial_runs.append({"scenario_id": spec.id, "round": round_index + 1, "stats": adv_stats})
        finally:
            self.stop_lab_fixtures()

        metrics = compute_metrics(results)
        metrics["suite"] = suite or "all"
        metrics["repeat"] = rounds
        metrics["timestamp"] = datetime.now().isoformat(timespec="seconds")
        metrics["results"] = [r.to_dict() for r in results]
        if adversarial_runs:
            metrics["adversarial"] = adversarial_runs
        reports = self._write_reports(metrics, suite)
        metrics["reports"] = [str(p) for p in reports]
        return metrics

    def _run_one(self, spec: ScenarioSpec, runner: Callable[[ScenarioSpec], Any]) -> tuple[ScenarioResult, dict | None]:
        start = time.time()
        proxy = None
        run_spec = spec
        stats: dict[str, Any] | None = None
        if spec.adversarial and spec.target.startswith(("http://", "https://")):
            from .adversarial import AdversarialProxy

            try:
                proxy = AdversarialProxy(upstream=spec.target, modes=spec.adversarial)
                proxy.start()
                run_spec = replace(spec, target=proxy.url)
            except Exception as exc:  # noqa: BLE001 - 对抗层故障不应中断评测
                self.logger(f"[eval] 场景 {spec.id} 对抗代理启动失败: {exc}")
                proxy = None
        try:
            state = runner(run_spec)
            result = self.to_result(spec, state, start)
        except Exception as exc:  # noqa: BLE001 - 单场景异常记为 error 继续
            self.logger(f"[eval] 场景 {spec.id} 执行失败: {type(exc).__name__}: {exc}")
            result = ScenarioResult(
                scenario_id=spec.id,
                kind=spec.kind,
                target=spec.target,
                status="error",
                duration=round(time.time() - start, 3),
                gt_total=len(spec.ground_truth),
                gt_miss=[gt_key(g, i) for i, g in enumerate(spec.ground_truth)],
                ports_expected=spec.ports_expected,
                services_expected=spec.services_expected,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if proxy is not None:
                stats = proxy.stats
                proxy.stop()
        return result, stats

    # -------------------------------------------------------------- 映射
    def to_result(self, spec: ScenarioSpec, state: Any, start: float, error: str = "") -> ScenarioResult:
        """把大脑返回的 ``RunState``（或任意兼容对象）映射成 ``ScenarioResult``。"""
        if isinstance(state, ScenarioResult):
            return state

        findings = [f.to_dict() if hasattr(f, "to_dict") else dict(f) for f in (getattr(state, "findings", None) or [])]
        findings.extend(self._infra_findings(spec, state, findings))
        match = match_findings(findings, spec.ground_truth)
        flags = [str(f.get("value", "")) for f in (getattr(state, "flags", None) or []) if isinstance(f, dict)]

        hosts = getattr(state, "hosts", None) or {}
        ports_found = 0
        services_correct = 0
        for host_state in hosts.values():
            ports = getattr(host_state, "ports", None) or {}
            ports_found += len(ports)
            for info in ports.values():
                if isinstance(info, dict) and (info.get("service") or info.get("version")):
                    services_correct += 1
        if spec.services_expected:
            services_correct = min(services_correct, spec.services_expected)

        counters = getattr(state, "counters", None) or {}
        duration = getattr(state, "duration", None)
        if duration is None:
            duration = time.time() - start

        status = self._judge_status(spec, str(getattr(state, "status", "failed") or "failed"), ports_found, match, flags)

        return ScenarioResult(
            scenario_id=spec.id,
            kind=spec.kind,
            target=spec.target,
            status=status,
            duration=round(float(duration), 3),
            findings=findings,
            gt_total=len(spec.ground_truth),
            gt_hit=match.gt_hit,
            gt_miss=match.gt_miss,
            fp_count=match.fp_count,
            out_of_scope_count=match.out_of_scope_count,
            flags=flags,
            ports_found=ports_found,
            ports_expected=spec.ports_expected,
            services_correct=services_correct,
            services_expected=spec.services_expected,
            steps=int(counters.get("steps", 0) or 0),
            denied=int(counters.get("denied", 0) or 0),
            error=error,
        )

    # -------------------------------------------------------------- 默认执行器
    @staticmethod
    def _infra_findings(spec: ScenarioSpec, state: Any, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把「端口/服务事实」转成评测口径的发现（open_port / service_fingerprint）。

        侦察类工具只记录事实（`RunState.hosts[…].ports`），不会为每个端口造一条漏洞；
        但主机场景的 ground truth 正是按端口/服务声明的，需要在这里做一次口径转换，
        否则端口覆盖度与服务识别会永远统计成遗漏。
        """
        from .metrics import normalize_vuln_class

        wanted = {normalize_vuln_class(gt.get("vuln_class", "")) for gt in (spec.ground_truth or [])}
        targets = wanted & {"open_port", "service_fingerprint"}
        if not targets:
            return []
        have = {(normalize_vuln_class(item.get("vuln_class", "")), str(item.get("url") or "")) for item in existing}
        synthesized: list[dict[str, Any]] = []
        for host, host_state in (getattr(state, "hosts", None) or {}).items():
            for port, info in sorted((getattr(host_state, "ports", None) or {}).items()):
                info = info if isinstance(info, dict) else {}
                address = f"{host}:{port}"
                evidence = [
                    {
                        "kind": "scan",
                        "target": address,
                        "note": "由扫描事实推导（端口开放/服务识别）",
                        "service": info.get("service"),
                        "version": info.get("version"),
                    }
                ]
                if "open_port" in targets and ("open_port", address) not in have:
                    synthesized.append(
                        {
                            "vuln_class": "open_port",
                            "title": f"开放端口 {address}",
                            "url": address,
                            "severity": "info",
                            "status": "verified",
                            "confidence": "high",
                            "evidence": evidence,
                        }
                    )
                service = info.get("service") or info.get("version")
                if "service_fingerprint" in targets and service and ("service_fingerprint", address) not in have:
                    synthesized.append(
                        {
                            "vuln_class": "service_fingerprint",
                            "title": f"服务识别 {address} -> {service}",
                            "url": address,
                            "severity": "info",
                            "status": "verified",
                            "confidence": "high" if info.get("version") else "medium",
                            "evidence": evidence,
                        }
                    )
        return synthesized

    @staticmethod
    def _judge_status(spec: ScenarioSpec, agent_status: str, ports_found: int, match: Any, flags: list[str]) -> str:
        """按场景声明的口径判定成功（默认沿用 agent 自评状态）。

        * ``ports``        —— 扫描类场景：GT 声明的端口全部发现才成功（无 GT 时退回端口数比较）
        * ``findings``     —— 至少命中一条 ground truth
        * ``findings_all`` —— ground truth 全部命中（服务识别这类要求完整的场景）
        * ``flags``        —— 拿到匹配 flag_patterns 的 flag
        """
        criteria = (spec.success_criteria or "auto").strip().lower()
        hits = len(getattr(match, "gt_hit", None) or [])
        if criteria == "ports":
            expected = len(spec.ground_truth or [])
            if expected:
                return "success" if hits >= expected else ("partial" if ports_found else "failed")
            if spec.ports_expected > 0 and ports_found >= spec.ports_expected:
                return "success"
            return "partial" if ports_found else "failed"
        if criteria == "findings_all":
            expected = len(spec.ground_truth or [])
            return "success" if hits >= max(1, expected) else ("partial" if agent_status in ("success", "partial") else agent_status)
        if criteria == "findings":
            if hits:
                return "success"
            return "partial" if agent_status in ("success", "partial") else agent_status
        if criteria == "flags":
            import re as _re

            patterns = [p for p in (spec.flag_patterns or []) if p]
            for flag in flags:
                if not patterns or any(_re.search(p, flag) for p in patterns):
                    return "success"
            return "partial" if flags else agent_status
        return agent_status

    def default_runner(self, spec: ScenarioSpec) -> Any:
        """用真实大脑跑一个场景（延迟导入 brain/tools/llm）。"""
        from trina.brain.agent import PentestAgent  # 延迟导入，避免无大脑环境 import 失败
        from trina.llm import make_backend
        from trina.tools import build_registry

        config = self.config
        if config.llm.backend == "replay" and spec.replay_script:
            config = replace(config, llm=replace(config.llm, replay_script=spec.replay_script))

        backend = make_backend(config.llm)
        tools = build_registry()
        agent = self._construct_agent(PentestAgent, config=config, backend=backend, tools=tools, logger=self.logger)
        return self._invoke_run(agent, spec)

    @staticmethod
    def _construct_agent(agent_cls: type, **kwargs: Any) -> Any:
        try:
            sig = inspect.signature(agent_cls)
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if not accepts_kwargs:
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (TypeError, ValueError):
            pass
        return agent_cls(**kwargs)

    def _resolve_target(self, spec: ScenarioSpec) -> str:
        """把 `bundle://<adapter>/<task_id>` 解析成真实目标 URL。

        TSecBench 的题目定义在 bundle manifest 里（含 target_url / compose_file）。
        解析失败时返回原样目标——大脑会在预检阶段报 blocked，这比静默换目标更诚实。
        """
        target = spec.target
        if not target.startswith("bundle://"):
            return target
        task_id = target.rstrip("/").split("/")[-1]
        from trina.adapters import load_adapter

        adapter = load_adapter(self.config, spec.adapter or "tsecbench")
        task = adapter.task_by_id(task_id) if hasattr(adapter, "task_by_id") else None
        if not task:
            self.logger(f"[eval] bundle 中找不到任务 {task_id}（{adapter.name}）")
            return target
        url = str(task.get("target_url") or "").strip()
        if not url and task.get("compose_file"):
            adapter.prepare()
            url = adapter.primary_target()
        if not url:
            self.logger(f"[eval] 任务 {task_id} 未声明 target_url/compose_file")
            return target
        self.logger(f"[eval] bundle 任务 {task_id} → {url}")
        return url

    def _invoke_run(self, agent: Any, spec: ScenarioSpec) -> Any:
        desire = {"goal": spec.goal, "max_steps": spec.max_steps, "max_seconds": spec.max_seconds}
        target = self._resolve_target(spec)
        try:
            sig = inspect.signature(agent.run)
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            call_kwargs = desire if accepts_kwargs else {k: v for k, v in desire.items() if k in sig.parameters}
            return agent.run(target, **call_kwargs)
        except TypeError:
            return agent.run(target)

    # -------------------------------------------------------------- 落盘
    def _write_reports(self, metrics: dict[str, Any], suite: str | None) -> list[Any]:
        out = self.output_dir()
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = f"{suite or 'all'}-{stamp}"
        md_path = out / f"{stem}.md"
        json_path = out / f"{stem}.json"
        title = f"Trina 评测结果 · {suite or '全部场景'}"
        md_path.write_text(format_metrics_markdown(metrics, title), encoding="utf-8")
        json_path.write_text(metrics_to_json(metrics), encoding="utf-8")
        return [md_path, json_path]


__all__ = ["EvalHarness", "ScenarioSpec"]
