"""Trina 命令行入口。

    trina doctor                       环境自检（模型后端/外部工具/护栏边界）
    trina tools [--verbose] [--json]   查看已封装的工具清单
    trina guard check <target>         测试目标是否在授权范围内
    trina guard argv -- <命令>          测试一条外部命令是否会被拒
    trina run --target <host:port>     全自动渗透（核心命令）
    trina audit verify <file.jsonl>    校验审计哈希链
    trina audit show <file.jsonl>      审计摘要
    trina ranges [list|status]         靶场适配器状态
    trina lab [--http] [--ssh]         启动故意脆弱的本地靶机
    trina eval run [--suite web]       跑评测套件并输出指标
    trina report rebuild --state <f>   由历史状态重建报告
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import PAIConfig, PROJECT_ROOT, ConfigError
from .scope import ScopeGuard

# ---------------------------------------------------------------------------
# 输出helpers（rich 可选）
# ---------------------------------------------------------------------------


def _console():
    try:
        from rich.console import Console

        return Console()
    except Exception:  # noqa: BLE001 - 无 rich 时退化为纯文本
        return None


def out(message: str = "", style: str = "") -> None:
    console = _console()
    if console and style:
        console.print(message, style=style)
    else:
        print(message)


def rule(title: str = "") -> None:
    console = _console()
    if console:
        console.rule(title or None)
    else:
        print(("=" * 8) + f" {title} " + ("=" * 8 if title else ""))


def kv_table(rows: list[tuple[str, str]], title: str = "") -> None:
    console = _console()
    if console:
        from rich.table import Table

        table = Table(title=title or None, show_lines=False)
        table.add_column("项", style="cyan", no_wrap=True)
        table.add_column("值")
        for key, value in rows:
            table.add_row(key, str(value))
        console.print(table)
    else:
        if title:
            print(f"-- {title}")
        for key, value in rows:
            print(f"{key:28} {value}")


def _status_mark(ok: bool) -> str:
    return "OK" if ok else "!!"


# ---------------------------------------------------------------------------
# 配置装配
# ---------------------------------------------------------------------------


def build_config(args: argparse.Namespace) -> PAIConfig:
    overrides: dict[str, Any] = {}
    for item in getattr(args, "set", None) or []:
        if "=" not in item:
            raise ConfigError(f"--set 需要 section.field=value 形式，得到 {item!r}")
        key, value = item.split("=", 1)
        overrides[key] = _literal(value)
    if getattr(args, "model", None):
        overrides["llm.model"] = args.model
    if getattr(args, "backend", None):
        overrides["llm.backend"] = args.backend
    if getattr(args, "replay", None):
        overrides["llm.backend"] = "replay"
        overrides["llm.replay_script"] = str(Path(args.replay).resolve())
    if getattr(args, "allow_exploit", False):
        overrides["scope.exploit_level"] = "exploit"
    config = PAIConfig.load(getattr(args, "config", None), overrides)
    return config


def _literal(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("none", "null"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if "," in value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    from .llm import make_backend
    from .tools import LOAD_ERRORS, build_registry
    from .tools.external import installed

    config = build_config(args)
    rule("Trina 环境自检")

    rows: list[tuple[str, str]] = [
        ("版本", __version__),
        ("配置文件", str(config.path)),
        ("Python", sys.version.split()[0]),
        ("工作目录", str(PROJECT_ROOT)),
    ]
    kv_table(rows, "基础")

    binaries = installed("omp", "nmap", "nuclei", "sqlmap", "hydra", "ffuf", "gobuster", "nikto", "masscan", "docker")
    kv_table(
        [(name, path or "缺失（将使用内置引擎兜底）") for name, path in binaries.items()],
        "外部工具",
    )

    rule("护栏边界")
    guard = ScopeGuard(config.scope)
    print(config.scope_prompt_block())

    rule("工具清单")
    registry = build_registry()
    if LOAD_ERRORS:
        for module, error in LOAD_ERRORS.items():
            out(f"[警告] 工具模块 {module} 未加载: {error}", "yellow")
    kv_table([(category, f"{len(specs)} 个") for category, specs in registry.by_category().items()], "按类别")
    print("工具: " + ", ".join(registry.names()))

    rule("模型后端")
    backend = make_backend(config.llm)
    ok, detail = backend.health()
    kv_table([("后端", config.llm.backend), ("模型", config.llm.model), ("连通性", f"{_status_mark(ok)} {detail}")], "LLM")

    rule("写作权限")
    for path in (config.audit_dir, config.report_dir, config.workspace_dir):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".trina-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            rows.append((str(path), "可写"))
        except OSError as exc:
            rows.append((str(path), f"不可写: {exc}"))
    kv_table(rows)
    return 0 if ok else 3


def cmd_tools(args: argparse.Namespace) -> int:
    from .tools import LOAD_ERRORS, build_registry

    registry = build_registry()
    if args.json:
        print(json.dumps(registry.describe(), ensure_ascii=False, indent=2))
        return 0
    if LOAD_ERRORS:
        for module, error in LOAD_ERRORS.items():
            out(f"[警告] 工具模块 {module} 未加载: {error}", "yellow")
    rule(f"已封装工具（{len(registry.names())} 个）")
    print(registry.prompt_block(verbose=args.verbose))
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    config = build_config(args)
    guard = ScopeGuard(config.scope)
    if args.guard_action == "check":
        for target in args.targets:
            decision = guard.check_target(target)
            mark = "允许" if decision.allowed else "拒绝"
            out(f"[{mark}] {target} — {decision.reason}", "green" if decision.allowed else "red")
            if decision.detail.get("ips"):
                print(f"        解析 IP: {', '.join(decision.detail['ips'])}")
            if decision.hint:
                print(f"        提示: {decision.hint}")
        return 0 if all(guard.check_target(t).allowed for t in args.targets) else 2
    if args.guard_action == "argv":
        argv = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
        decision = guard.check_argv(argv, tool=argv[0] if argv else "", target=args.target or "")
        mark = "允许" if decision.allowed else "拒绝"
        out(f"[{mark}] {' '.join(argv)} — {decision.reason}", "green" if decision.allowed else "red")
        return 0 if decision.allowed else 2
    if args.guard_action == "summary":
        print(json.dumps(guard.summary(), ensure_ascii=False, indent=2))
        return 0
    return 1


def cmd_run(args: argparse.Namespace) -> int:
    from .brain import PentestAgent
    from .llm import make_backend
    from .tools import LOAD_ERRORS, build_registry

    config = build_config(args)
    target = args.target
    goal = args.goal or ""
    adapter = None

    if args.range:
        from .adapters import load_adapter

        adapter = load_adapter(config, args.range)
        info = adapter.prepare()
        target = target or adapter.primary_target()
        if not info.notes and getattr(adapter, "notes", ""):
            pass
        out(f"靶场 {adapter.name}（{adapter.kind}）就绪状态: {adapter.status().get('detail', '')}")
        if not target:
            out("靶场未提供可用目标，请显式指定 --target", "red")
            return 2
    if not target:
        out("必须指定 --target，或用 --range 让适配器给出目标", "red")
        return 2

    registry = build_registry()
    for module, error in LOAD_ERRORS.items():
        out(f"[警告] 工具模块 {module} 未加载: {error}", "yellow")
    backend = make_backend(config.llm)

    rule(f"Trina 自动渗透 · {target}")
    print(f"模型: {config.llm.model}（{config.llm.backend}） · 动作等级上限: {config.scope.exploit_level}"
          f" · 破坏性: {'开' if config.scope.destructive else '关'}")
    print(f"目标范围: {', '.join(config.scope.authorized_cidrs)}")

    def on_step(step) -> None:
        mark = "拒" if step.denied else ("OK" if step.ok else "×")
        out(f"  [{step.index:02d}] {mark} {step.action} — {step.summary[:120]}", "red" if step.denied else "")

    agent = PentestAgent(config, registry=registry, backend=backend, logger=lambda msg: print(f"  · {msg}"), on_step=on_step)
    state = agent.run(
        target,
        goal=goal,
        max_steps=args.max_steps,
        max_seconds=args.max_seconds,
        extra_rules=(args.hint and "\n".join(args.hint)) or "",
        record_path=args.record,
    )

    rule("结果")
    kv_table(
        [
            ("run_id", state.run_id),
            ("状态", state.status),
            ("耗时", f"{state.duration:.0f}s"),
            ("步数", str(state.counters.get("steps", 0))),
            ("漏洞", str(len(state.findings))),
            ("凭据", str(len(state.credentials))),
            ("flag", str(len(state.flags))),
            ("被护栏拒绝", str(state.counters.get("denied", 0))),
            ("LLM 调用/成本", f"{state.ledger.get('calls', 0)} 次 / ${state.ledger.get('cost_usd', 0)}"),
            ("审计文件", state.artifacts.get("audit_path", "")),
            ("工作区", state.artifacts.get("workspace", "")),
        ]
    )
    if state.findings:
        rule("漏洞清单")
        for finding in sorted(state.findings, key=lambda f: f.severity):
            print(f"  [{finding.severity:8}] {finding.id} {finding.vuln_class:22} {finding.url or finding.target} "
                  f"param={finding.parameter or '-'} status={finding.status}")
    if state.flags:
        rule("Flag")
        for flag in state.flags:
            print(f"  {flag['value']}  <- {flag.get('source', '')}")

    paths: list[Path] = []
    if not args.no_report:
        paths = _write_report(config, state, meta={"range": adapter.name if adapter else "", "goal": goal})
        for path in paths:
            print(f"报告: {path}")
    if adapter is not None and args.teardown:
        adapter.teardown()
    return 0 if state.status in ("success", "partial") else 1


def _write_report(config: PAIConfig, state, *, meta: dict[str, Any] | None = None) -> list[Path]:
    """生成报告（报告模块缺失时降级为提示，不影响主流程）。"""
    try:
        from .report import build_report
    except ImportError as exc:
        out(f"[警告] 报告模块不可用（{exc}），已跳过报告生成", "yellow")
        return []
    audit_summary = None
    audit_path = state.artifacts.get("audit_path")
    if audit_path and Path(audit_path).exists():
        from .audit import AuditLog

        log = AuditLog(Path(audit_path), state.run_id)
        log._records = len(log.read_all())
        summary = log.summary()
        ok, detail = log.verify()
        summary["chain_ok"] = ok
        summary["chain_detail"] = detail
        audit_summary = summary
    report = build_report(state, config, meta=meta, audit_summary=audit_summary)
    return report.save()


def cmd_audit(args: argparse.Namespace) -> int:
    from .audit import AuditLog

    path = Path(args.file)
    if not path.exists():
        candidates = sorted((PROJECT_ROOT / "logs").glob(f"*{args.file}*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            out(f"未找到审计文件: {args.file}", "red")
            return 2
        path = candidates[0]
    log = AuditLog(path, run_id=path.stem)
    records = log.read_all()
    log._records = len(records)
    if args.audit_action == "verify":
        ok, detail = log.verify()
        out(f"[{'通过' if ok else '失败'}] {path}", "green" if ok else "red")
        print(f"  {detail}")
        return 0 if ok else 2
    if args.audit_action == "show":
        summary = log.summary()
        summary["chain_ok"] = log.verify()[0]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if args.limit:
            rule(f"最近 {args.limit} 条")
            for record in records[-args.limit :]:
                print(f"  {record.get('time', '')} #{record.get('seq')} {record.get('event')} "
                      f"{record.get('tool', '')} {record.get('decision', '')} {str(record.get('reason', ''))[:80]}")
        return 0
    if args.audit_action == "tail":
        for record in records[-args.limit or -20 :]:
            print(json.dumps(record, ensure_ascii=False))
        return 0
    return 1


def cmd_ranges(args: argparse.Namespace) -> int:
    config = build_config(args)
    from .adapters import list_adapters, load_adapter

    if args.ranges_action == "list" or not args.ranges_action:
        rows = [
            (item["name"], f"{item['kind']:6} {item['source']} {'就绪' if item.get('ready') else '未就绪'}")
            for item in list_adapters(config)
        ]
        kv_table(rows, "可用靶场")
        return 0
    if args.ranges_action == "status":
        adapter = load_adapter(config, args.name)
        status = adapter.status()
        kv_table([(k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)) for k, v in status.items()])
        info = adapter.prepare()
        kv_table(
            [
                ("目标", ", ".join(f"{t.host}:{t.port} {t.service}".strip() for t in info.targets) or "(无)"),
                ("ground truth", f"{len(info.ground_truth)} 条"),
                ("flag 模式", ", ".join(info.flag_patterns) or "(默认)"),
                ("说明", info.notes),
            ],
            f"靶场 {adapter.name}",
        )
        return 0
    return 1


def cmd_lab(args: argparse.Namespace) -> int:
    try:
        from .lab.run import main as lab_main
    except ImportError as exc:
        out(f"实验室靶机模块不可用: {exc}", "red")
        return 2
    argv = []
    if args.http:
        argv += ["--http"]
    if args.ssh:
        argv += ["--ssh"]
    if args.port:
        argv += ["--port", str(args.port)]
    if args.ssh_port:
        argv += ["--ssh-port", str(args.ssh_port)]
    return lab_main(argv)


def cmd_eval(args: argparse.Namespace) -> int:
    config = build_config(args)
    try:
        from .eval.harness import EvalHarness
    except ImportError as exc:
        out(f"评测模块不可用: {exc}", "red")
        return 2
    if args.eval_action == "adversarial":
        from .eval.adversarial import AdversarialProxy

        modes = args.modes or ["waf", "ban", "ratelimit", "tamper", "honeypot"]
        upstream = args.upstream or "http://127.0.0.1:9099"
        with AdversarialProxy(upstream=upstream, modes=modes, port=args.port or 9100) as proxy:
            out(f"对抗代理已启动: http://127.0.0.1:{args.port or 9100} -> {upstream}，模式: {', '.join(modes)}")
            out("按 Ctrl+C 结束；结束后会打印统计。")
            try:
                import time

                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        print(json.dumps(proxy.stats, ensure_ascii=False, indent=2))
        return 0
    harness = EvalHarness(config, logger=lambda msg: print(f"  · {msg}"))
    metrics = harness.run(args.suite, repeat=args.repeat)
    out(json.dumps(metrics, ensure_ascii=False, indent=2) if args.json else "评测完成（详见报告）")
    return 0


def cmd_tool(args: argparse.Namespace) -> int:
    """执行单个工具（经护栏与审计），供 omp 扩展与手工运维使用。"""
    import time

    from .audit import AuditLog
    from .scope import ScopeGuard
    from .state import RunState
    from .tools import LOAD_ERRORS, build_registry
    from .tools.http import HttpClient
    from .tools.registry import ToolContext

    config = build_config(args)
    registry = build_registry()
    for module, error in LOAD_ERRORS.items():
        print(f"[警告] 工具模块 {module} 未加载: {error}", file=sys.stderr)
    if args.name not in registry.names():
        out(f"未知工具 {args.name}；可用: {', '.join(registry.names())}", "red")
        return 2

    params: dict[str, Any] = {}
    if args.params:
        try:
            params.update(json.loads(args.params))
        except json.JSONDecodeError as exc:
            out(f"--params 不是合法 JSON: {exc}", "red")
            return 2
    for item in args.param or []:
        key, _, value = item.partition("=")
        params[key] = _literal(value)
    if args.target:
        # 只填工具**声明过**的目标参数：有的工具用 url，有的用 target，
        # 不要给不接受 target 的工具硬塞一个（那会被参数校验直接拒掉）。
        tool_spec = registry.get(args.name).spec
        if "target" in tool_spec.params:
            params.setdefault("target", args.target)
        if "url" in tool_spec.params:
            params.setdefault("url", args.target)

    run_id = "tool-" + time.strftime("%Y%m%d-%H%M%S")
    audit = AuditLog.open(config.audit_dir, run_id=run_id, resume=False)
    guard = ScopeGuard(config.scope, audit=audit)
    state = RunState(run_id=run_id, target=str(params.get("target") or params.get("url") or ""))
    ctx = ToolContext(
        config=config,
        guard=guard,
        audit=audit,
        state=state,
        http=HttpClient(guard),
        workspace=config.workspace_dir / run_id,
        deadline=time.monotonic() + config.run.tool_timeout * 2,
    )
    result = registry.execute(args.name, params, ctx)
    payload = {
        "tool": args.name,
        "ok": result.ok,
        "denied": result.denied,
        "summary": result.summary,
        "error": result.error,
        "hints": result.next_hints,
        "facts": result.facts,
        "data": result.data,
        "duration": round(result.duration, 3),
        "audit": str(audit.path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    config = build_config(args)
    path = Path(args.state)
    if not path.exists():
        out(f"状态文件不存在: {path}", "red")
        return 2
    from .brain.agent import PentestAgent  # noqa: F401 - 保持导入以复用状态结构
    from .state import RunState
    from .report import build_report

    data = json.loads(path.read_text(encoding="utf-8"))
    state = _state_from_dict(data)
    report = build_report(state, config, meta={"rebuilt_from": str(path)})
    paths = report.save()
    for item in paths:
        print(f"报告: {item}")
    return 0


def _state_from_dict(data: dict[str, Any]):
    """从 state.json 还原 RunState（供报告重建）。"""
    from .state import Credential, Finding, HostState, RunState

    state = RunState(run_id=data.get("run_id", "rebuilt"), target=data.get("target", ""), goal=data.get("goal", ""))
    state.status = data.get("status", "unknown")
    state.phase = data.get("phase", "report")
    state.started_at = data.get("started_at", state.started_at)
    state.ended_at = data.get("ended_at")
    state.timeline = data.get("timeline", [])
    state.steps = data.get("steps", [])
    state.flags = data.get("flags", [])
    state.evidence = data.get("evidence", [])
    state.ledger = data.get("ledger", {})
    state.counters = data.get("counters", state.counters)
    state.artifacts = data.get("artifacts", {})
    for host, payload in (data.get("hosts") or {}).items():
        entry = HostState(host=host)
        entry.alive = payload.get("alive")
        entry.technologies = payload.get("technologies", [])
        entry.web_paths = payload.get("web_paths", [])
        entry.notes = payload.get("notes", [])
        entry.ports = {int(k): v for k, v in (payload.get("ports") or {}).items()}
        state.hosts[host] = entry
    for item in data.get("findings", []) or []:
        fields = {k: v for k, v in item.items() if k in Finding.__dataclass_fields__ and k != "severity_cn"}
        state.findings.append(Finding(**fields))
    for item in data.get("credentials", []) or []:
        fields = {k: v for k, v in item.items() if k in Credential.__dataclass_fields__}
        state.credentials.append(Credential(**fields))
    return state


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trina", description="Trina · 全自动渗透测试 Agent（omp 二开）")
    parser.add_argument("--version", action="version", version=f"trina {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--config", help="配置文件路径（默认 configs/trina.yaml）")
        target.add_argument("--set", action="append", metavar="section.field=value", help="临时覆盖配置，可重复")

    doctor = sub.add_parser("doctor", help="环境自检")
    add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    tools = sub.add_parser("tools", help="列出已封装工具")
    tools.add_argument("--verbose", "-v", action="store_true")
    tools.add_argument("--json", action="store_true")
    tools.set_defaults(func=cmd_tools)

    single = sub.add_parser("tool", help="执行单个工具（带护栏与审计）")
    add_common(single)
    single.add_argument("name", help="工具名，见 trina tools")
    single.add_argument("--params", help="JSON 参数对象")
    single.add_argument("--param", action="append", metavar="key=value", help="单个参数，可重复")
    single.add_argument("--target", help="便捷设置 target/url 参数")
    single.set_defaults(func=cmd_tool)

    guard = sub.add_parser("guard", help="护栏自测")
    add_common(guard)
    guard_sub = guard.add_subparsers(dest="guard_action", required=True)
    guard_check = guard_sub.add_parser("check", help="检查目标是否授权")
    guard_check.add_argument("targets", nargs="+")
    guard_check.set_defaults(func=cmd_guard)
    guard_argv = guard_sub.add_parser("argv", help="检查外部命令是否会被拒绝")
    guard_argv.add_argument("argv", nargs=argparse.REMAINDER)
    guard_argv.add_argument("--target", default="")
    guard_argv.set_defaults(func=cmd_guard)
    guard_summary = guard_sub.add_parser("summary", help="打印护栏边界")
    guard_summary.set_defaults(func=cmd_guard)

    run = sub.add_parser("run", help="对目标执行全自动渗透")
    add_common(run)
    run.add_argument("--target", "-t", help="目标（host:port 或 URL）")
    run.add_argument("--goal", "-g", default="", help="本次任务目标（自然语言）")
    run.add_argument("--range", "-r", help="使用靶场适配器（如 lab/dvwa/pikachu）")
    run.add_argument("--max-steps", type=int, default=None)
    run.add_argument("--max-seconds", type=int, default=None)
    run.add_argument("--model", help="覆盖模型（如 deepseek/deepseek-v4-flash）")
    run.add_argument("--backend", choices=["omp", "openai", "replay"])
    run.add_argument("--replay", help="离线回放脚本（等价于 --backend replay）")
    run.add_argument("--record", help="把本次 LLM 交互录制成回放脚本")
    run.add_argument("--hint", action="append", help="附加约束/提示，可重复")
    run.add_argument(
        "--allow-exploit",
        action="store_true",
        help="把动作等级上限提升到 exploit（允许命令执行与批量爆破；破坏性动作仍需配置单独开启）",
    )
    run.add_argument("--no-report", action="store_true", help="不生成报告")
    run.add_argument("--teardown", action="store_true", help="结束后关闭自建靶场")
    run.set_defaults(func=cmd_run)

    audit = sub.add_parser("audit", help="审计日志")
    audit_sub = audit.add_subparsers(dest="audit_action", required=True)
    for action, helptext in (("verify", "校验哈希链"), ("show", "摘要"), ("tail", "逐条输出")):
        node = audit_sub.add_parser(action, help=helptext)
        node.add_argument("file", help="审计文件路径或其片段")
        node.add_argument("--limit", type=int, default=20)
        node.set_defaults(func=cmd_audit)

    ranges = sub.add_parser("ranges", help="靶场适配器")
    add_common(ranges)
    ranges_sub = ranges.add_subparsers(dest="ranges_action")
    ranges_sub.add_parser("list")
    ranges_status = ranges_sub.add_parser("status")
    ranges_status.add_argument("name")
    ranges.set_defaults(func=cmd_ranges, name=None)

    lab = sub.add_parser("lab", help="启动本地实验靶机（故意脆弱）")
    lab.add_argument("--http", action="store_true")
    lab.add_argument("--ssh", action="store_true")
    lab.add_argument("--port", type=int)
    lab.add_argument("--ssh-port", type=int)
    lab.set_defaults(func=cmd_lab)

    evaluate = sub.add_parser("eval", help="评测")
    add_common(evaluate)
    eval_sub = evaluate.add_subparsers(dest="eval_action", required=True)
    eval_run = eval_sub.add_parser("run", help="跑评测套件")
    eval_run.add_argument("--suite", help="套件名（web/host/integrated）")
    eval_run.add_argument("--repeat", type=int)
    eval_run.add_argument("--json", action="store_true")
    eval_run.set_defaults(func=cmd_eval)
    eval_adv = eval_sub.add_parser("adversarial", help="启动蓝队对抗代理")
    eval_adv.add_argument("--upstream", default="")
    eval_adv.add_argument("--port", type=int, default=9100)
    eval_adv.add_argument("--modes", nargs="*")
    eval_adv.set_defaults(func=cmd_eval)

    report = sub.add_parser("report", help="报告")
    add_common(report)
    report_sub = report.add_subparsers(dest="report_action", required=True)
    report_rebuild = report_sub.add_parser("rebuild", help="由 state.json 重建报告")
    report_rebuild.add_argument("--state", required=True)
    report_rebuild.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        out(f"配置错误: {exc}", "red")
        return 2
    except KeyboardInterrupt:
        out("\n已中断", "yellow")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
