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
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import TrinaConfig, PROJECT_ROOT, ConfigError, _default_runtime_root
from .model_names import display_model
from .scope import ScopeGuard
from .terminal import render_approval, render_message, render_panel, render_step, use_color

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
        console.print(message, style=style, markup=False, highlight=False)
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
        from rich.text import Text

        table = Table(title=title or None, show_lines=False)
        table.add_column("项", style="cyan", no_wrap=True)
        table.add_column("值")
        for key, value in rows:
            table.add_row(Text(str(key)), Text(str(value)))
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


def build_config(args: argparse.Namespace) -> TrinaConfig:
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
    config = TrinaConfig.load(getattr(args, "config", None), overrides)
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
        ("运行根目录", str(config.runtime_root)),
    ]
    kv_table(rows, "基础")

    binaries = installed("omp", "nmap", "nuclei", "sqlmap", "hydra", "ffuf", "gobuster", "nikto", "masscan", "docker")
    kv_table(
        [(name, path or "缺失（将使用内置引擎兜底）") for name, path in binaries.items()],
        "外部工具",
    )

    rule("执行信任模式")
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
    kv_table(
        [
            ("后端", config.llm.backend),
            ("模型", display_model(config.llm.model, config.llm.reasoning_effort)),
            ("模型 ID", config.llm.model),
            ("连通性", f"{_status_mark(ok)} {detail}"),
        ],
        "LLM",
    )

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
    from .tasks import TaskStore
    goal = args.goal if args.goal is not None else TaskStore().goal()
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

    print(render_panel("Trina / 任务", rows=[("目标", target), ("任务", goal or "按配置执行"),
                       ("模型", f"{display_model(config.llm.model, config.llm.reasoning_effort)} · {config.llm.backend}"),
                       ("信任模式", config.scope.trust_mode)], color=use_color()))

    def on_step(step) -> None:
        print(render_step(step, color=use_color()))

    agent = PentestAgent(config, registry=registry, backend=backend, logger=lambda msg: print(render_message(msg, label="  ·", color=use_color())), on_step=on_step)
    state = agent.run(
        target,
        goal=goal,
        max_steps=args.max_steps,
        max_seconds=args.max_seconds,
        extra_rules=(args.hint and "\n".join(args.hint)) or "",
        record_path=args.record,
    )

    from .board import render_board
    print(render_board(state, color=use_color()))
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


def cmd_swarm(args: argparse.Namespace) -> int:
    """运行 RASC 任务图或从任务级检查点继续。"""
    from .swarm import SwarmRuntime, default_tasks, load_manifest

    config = build_config(args)
    try:
        if args.resume:
            runtime = SwarmRuntime.resume(config, args.resume)
        else:
            if args.manifest:
                tasks = load_manifest(args.manifest)
            else:
                if not args.target:
                    out("请提供 --target、--manifest 或 --resume", "red")
                    return 2
                tasks = default_tasks(args.target, args.goal, max_steps=args.max_steps, max_seconds=args.max_seconds)
            runtime = SwarmRuntime(config, tasks, max_workers=args.workers)
        summary = runtime.run()
    except (ValueError, KeyError, OSError) as exc:
        out(f"Swarm 配置错误: {exc}", "red")
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        rule(f"Trina Swarm · {summary['run_id']}")
        for task_id, item in summary["tasks"].items():
            out(f"{task_id:18} {item['status']:10} {item['worker'] or '-'}  尝试 {item['attempt']}")
            if item["error"]:
                out(f"  {item['error']}", "yellow")
        out(f"检查点: {summary['checkpoint']}")
        out(f"Worker 创建 {summary['workers_created']}，复用 {summary['worker_reuses']} 次")
    return 0 if all(item["status"] == "completed" for item in summary["tasks"].values()) else 1


def _write_report(config: TrinaConfig, state, *, meta: dict[str, Any] | None = None) -> list[Path]:
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
        candidates = sorted((_default_runtime_root() / "logs").glob(f"*{args.file}*"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
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


def cmd_play(args: argparse.Namespace) -> int:
    """一条命令直接开打：准备靶场 → 全自动渗透 → 出报告。"""
    import sys as _sys
    import time

    from .play import build_plan, run_plan
    from .tasks import TaskStore
    from .session import Session, level_needs_approval

    config = build_config(args)
    approval_mode = getattr(args, "approve", "off") or "off"
    if approval_mode != "off" and not _sys.stdin.isatty():
        out(
            "非交互环境下无法进行人工审批：请改用 `--approve off`（全自动），"
            "或运行 `python3 -m trina console` 进入可介入的控制台。",
            "red",
        )
        return 2

    print(render_panel("Trina / 准备任务", rows=[("模型", display_model(config.llm.model, config.llm.reasoning_effort))], color=use_color()))
    plan = build_plan(
        config,
        target=getattr(args, "target", None),
        range_name=getattr(args, "range", None),
        goal=(getattr(args, "goal", None) if getattr(args, "goal", None) is not None else TaskStore().goal()),
        log=lambda message: print(render_message(message, label="准备", color=use_color())),
    )
    session = Session(approval_mode=approval_mode, emit=lambda message: print(f"[审批] {message}"))

    def _ask(request: dict) -> bool:
        """交互式审批：默认（enter）拒绝，避免误放行。"""
        print(render_approval(request, None, color=use_color()))
        question = "允许执行？[y/N] "
        try:
            answer = input(question).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        return answer in ("y", "yes")

    try:
        state = run_plan(
            config,
            plan,
            approval=_ask if approval_mode != "off" else None,
            steer_queue=session.steers,
            stop_event=session.stop_event,
            pause_event=session.pause_event,
            controller=None,
            max_steps=getattr(args, "max_steps", None),
            max_seconds=getattr(args, "max_seconds", None),
            log=lambda message: print(render_message(message, label="  ·", color=use_color())),
            on_step=lambda step: print(render_step(step, color=use_color())),
            write_report=not getattr(args, "no_report", False),
        )
    finally:
        if getattr(args, "keep_lab", False):
            out("（按 --keep-lab 保留本次拉起的靶机）")
        else:
            plan.teardown()

    from .board import render_board
    print(render_board(state, color=use_color()))
    return 0 if state.verdict in ("CONFIRMED", "NOT_CONFIRMED") else 1


def _bench_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _bench_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _prepare_bench_llm(config: TrinaConfig, *, hosted: bool, force_gateway: bool) -> None:
    """托管模式把大模型地址改写成平台网关（`.tsecbench.gw` + http）。

    仅在 ``hosted`` 或显式 ``--gateway`` 时执行，避免本地直连被误改。
    """
    from urllib.parse import urlsplit, urlunsplit

    if config.llm.backend == "omp" and hosted:
        config.llm.backend = "openai"
    base = (config.llm.base_url or os.environ.get("TRINA_LLM_BASE_URL") or "").strip()
    if not base and os.environ.get("DEEPSEEK_API_KEY"):
        base = "https://api.deepseek.com/v1"
        if config.llm.model.startswith("deepseek/"):
            config.llm.model = os.environ.get("TRINA_LLM_MODEL", "deepseek-chat")
    if base:
        parts = urlsplit(base)
        host = (parts.hostname or "").lower()
        if host and (hosted or force_gateway) and not host.endswith(".tsecbench.gw"):
            config.llm.base_url = urlunsplit(("http", f"{host}.tsecbench.gw", parts.path, parts.query, ""))
        else:
            config.llm.base_url = base.rstrip("/")
        if "api.deepseek.com" in host and config.llm.model.startswith("deepseek/"):
            config.llm.model = os.environ.get("TRINA_LLM_MODEL", "deepseek-chat")
    if not os.environ.get("TRINA_LLM_API_KEY") and os.environ.get("DEEPSEEK_API_KEY"):
        config.llm.api_key_env = "DEEPSEEK_API_KEY"


def cmd_bench(args: argparse.Namespace) -> int:
    """接入 TSecBench 答题 API：逐题启动容器 → 渗透 → 提交 flag → 关闭容器。"""
    from .bench import BenchOptions, BenchmarkError, run_benchmark

    config = build_config(args)
    base_url = (getattr(args, "base_url", None) or os.environ.get("BENCHMARK_BASE_URL") or "").strip()
    token = os.environ.get(getattr(args, "token_env", None) or "BENCHMARK_TOKEN", "")
    if not base_url:
        out("缺少 BENCHMARK_BASE_URL：请从 TSecBench 评测页获取后注入环境变量", "red")
        return 2
    if not token:
        out("缺少 BENCHMARK_TOKEN：请从 TSecBench 评测页获取后注入环境变量", "red")
        return 2
    hosted = _bench_bool("TRINA_BENCH_HOSTED", default=False)
    _prepare_bench_llm(config, hosted=hosted, force_gateway=bool(getattr(args, "gateway", False)))
    options = BenchOptions(
        max_steps=int(getattr(args, "max_steps", None) or _bench_int("TRINA_BENCH_MAX_STEPS", 36)),
        per_task_seconds=int(
            getattr(args, "per_task_seconds", None) or _bench_int("TRINA_BENCH_PER_TASK_SECONDS", 420)
        ),
        total_seconds=int(getattr(args, "total_seconds", None) or _bench_int("TRINA_BENCH_TOTAL_SECONDS", 6 * 3600)),
        attempts=int(getattr(args, "attempts", None) or _bench_int("TRINA_BENCH_ATTEMPTS", 1)),
        use_hints=bool(getattr(args, "use_hints", False) or _bench_bool("TRINA_BENCH_USE_HINTS", False)),
        limit=int(getattr(args, "limit", None) or _bench_int("TRINA_BENCH_LIMIT", 0)),
    )
    print(
        render_panel(
            "Trina / TSecBench",
            rows=[
                ("答题 API", base_url),
                ("模型后端", f"{config.llm.backend} / {display_model(config.llm.model, config.llm.reasoning_effort)}"),
                ("模型地址", config.llm.base_url or "(后端默认)"),
                ("单题预算", f"{options.per_task_seconds}s × {options.max_steps} 步"),
                ("总时限", f"{options.total_seconds}s"),
            ],
            color=use_color(),
        )
    )
    try:
        summary = run_benchmark(
            config,
            base_url=base_url,
            token=token,
            options=options,
            dry_run=bool(getattr(args, "dry_run", False)),
            log=lambda message: print(render_message(message, label="bench", color=use_color())),
        )
    except (BenchmarkError, ValueError) as exc:
        out(f"评测失败: {exc}", "red")
        return 1
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0 if not summary.error else 1


def cmd_console(args: argparse.Namespace) -> int:
    """进入交互式控制台（人工可介入）。"""
    from .interactive import main as console_main

    argv: list[str] = []
    if getattr(args, "approve", None):
        argv += ["--approve", args.approve]
    if getattr(args, "config", None):
        argv += ["--config", args.config]
    for item in getattr(args, "set", None) or []:
        argv += ["--set", item]
    if getattr(args, "plain", False):
        argv += ["--plain"]
    if getattr(args, "no_lab_autostart", False):
        argv += ["--no-lab-autostart"]
    return console_main(argv)


def cmd_review(args: argparse.Namespace) -> int:
    """评价 Agent（只读复核）：核对声明与证据、覆盖缺口、边界事件，生成复核记录。"""
    from .policy import PolicyRegistry
    from .review import load_state, review_state

    config = build_config(args)
    if args.state:
        state_path = Path(args.state)
        if not state_path.exists():
            out(f"状态文件不存在: {state_path}", "red")
            return 2
    else:
        candidates = sorted(
            config.workspace_dir.glob("*/state.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            out("未找到任何运行状态：请先跑一次（pai play / pai run），或用 --state 指定 state.json", "red")
            return 2
        state_path = candidates[0]
        out(f"（未指定 --state，复核最近一次运行：{state_path.parent.name}）")
    state = load_state(state_path)
    audit_path = args.audit
    if not audit_path:
        candidate = state.get("artifacts", {}).get("audit_path")
        if candidate and Path(candidate).exists():
            audit_path = candidate
    registry = None if args.no_registry else PolicyRegistry.load(config, extra_dirs=list(config.policy.registry_dirs))
    note = review_state(state, audit_path=audit_path, registry=registry)
    markdown = note.to_markdown()
    if args.json:
        print(json.dumps(note.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(markdown)
    if args.save:
        out_dir = Path(args.save)
        if not out_dir.is_absolute():
            out_dir = config.runtime_root / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{note.run_id or state_path.stem}-review.md"
        target.write_text(markdown, encoding="utf-8")
        print(f"复核记录: {target}")
    # 退出码：无矛盾且结论一致 → 0；存在矛盾/缺证据 → 2，便于 CI 与人工复核分流
    return 0 if not (note.verdict_conflicts or note.claims_without_evidence) else 2


def cmd_watch(args: argparse.Namespace) -> int:
    """实时监视一次运行（读取周期落盘的 state.json，不干预 Agent）。"""
    import sys as _sys
    import time as _time

    from .board import latest_state_file, load_state, render_board
    from .terminal import use_color

    config = build_config(args)
    workspace = config.workspace_dir
    if args.run_id:
        path = workspace / args.run_id / "state.json"
    else:
        path = latest_state_file(workspace)
    if path is None or not Path(path).exists():
        out("未找到运行状态：先跑一次（trina play / trina run），或指定 run_id", "red")
        return 2

    if args.once or not _sys.stdout.isatty():
        print(render_board(load_state(path), color=False))
        return 0

    out(f"监视 {path.parent.name}（每 {args.interval}s 刷新，Ctrl+C 退出）")
    try:
        while True:
            board = render_board(load_state(path), color=use_color())
            _sys.stdout.write("\033[2J\033[H" + board + "\n")
            _sys.stdout.flush()
            _time.sleep(max(0.5, float(args.interval)))
    except KeyboardInterrupt:
        out("\n已退出监视")
        return 0


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


def cmd_goal(args: argparse.Namespace) -> int:
    from .tasks import TaskStore
    store = TaskStore()
    if args.goal_action == "set":
        text = " ".join(args.text).strip()
        if not text:
            raise ConfigError("goal set 需要目标文本")
        store.set_goal(text)
    elif args.goal_action == "clear":
        store.set_goal("")
    print(render_panel("Goal", sections=[("当前目标", store.goal() or "未设置")]))
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from dataclasses import asdict
    from .tasks import TaskStore, TaskScheduler
    store = TaskStore()
    if args.schedule_action == "add":
        task = store.add(args.target, args.at, goal=args.goal, max_steps=args.max_steps,
                         approve=args.approve, config=build_config(args))
        print(render_panel("定时任务已创建", rows=[("ID", task.id), ("开始时间", task.run_at),
              ("目标", task.target), ("Goal", task.goal or "未设置")]))
    elif args.schedule_action == "cancel":
        store.cancel(args.task_id)
        out(f"已取消 {args.task_id}")
    elif args.schedule_action == "run":
        scheduler = TaskScheduler(store)
        try:
            if args.once:
                scheduler.tick()
            else:
                scheduler.serve()
        finally:
            scheduler.stop_event.set()
    else:
        rows = store.list()
        if getattr(args, "json", False):
            print(json.dumps([asdict(task) for task in rows], ensure_ascii=False, indent=2))
        else:
            print(render_panel("定时任务", sections=[("", "\n".join(
                f"{task.id} · {task.run_at} · {task.status} · {task.target}"
                + (f" · 结果 {task.outcome}" if task.outcome else "") for task in rows) or "暂无定时任务")]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trina", description="Trina · 全自动渗透测试 Agent")
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

    play = sub.add_parser("play", help="一条命令直接开打（自动准备靶场 → 渗透 → 报告）")
    add_common(play)
    play.add_argument("--target", "-t", help="目标（默认自动起本地 lab 靶机）")
    play.add_argument("--range", "-r", help="靶场适配器名（lab/dvwa/pikachu/...）")
    play.add_argument("--goal", "-g", default=None, help="任务目标（未提供时沿用当前项目 Goal）")
    play.add_argument("--max-steps", type=int, default=None)
    play.add_argument("--max-seconds", type=int, default=None)
    play.add_argument("--approve", choices=["off", "exploit", "verify", "all"], default="off",
                      help="人工审批闸门（默认 off 全自动；非 off 需交互终端，或改用 console）")
    play.add_argument("--no-report", action="store_true", help="不生成报告")
    play.add_argument("--keep-lab", action="store_true", help="结束后保留自动拉起的靶机")
    play.set_defaults(func=cmd_play)

    bench = sub.add_parser("bench", help="接入 TSecBench 答题 API（托管/本地评测）")
    add_common(bench)
    bench.add_argument("--base-url", help="答题 API 根地址（默认 BENCHMARK_BASE_URL）")
    bench.add_argument("--token-env", default="BENCHMARK_TOKEN", help="token 所在环境变量名")
    bench.add_argument("--max-steps", type=int, help="单轮决策步数上限（默认 36）")
    bench.add_argument("--per-task-seconds", type=int, help="单题时间预算（默认 420）")
    bench.add_argument("--total-seconds", type=int, help="整场时间预算（默认 21600）")
    bench.add_argument("--attempts", type=int, help="单题最多尝试轮次（默认 1）")
    bench.add_argument("--use-hints", action="store_true", help="补漏轮次前获取平台提示（会扣分）")
    bench.add_argument("--limit", type=int, help="只跑前 N 题（调试用）")
    bench.add_argument("--dry-run", action="store_true", help="只列出题目，不启动容器")
    bench.add_argument("--gateway", action="store_true", help="强制把模型地址改写成 .tsecbench.gw 网关")
    bench.add_argument("--model", help="覆盖模型名")
    bench.add_argument("--backend", choices=["omp", "openai", "replay"])
    bench.add_argument("--allow-exploit", action="store_true", help="restricted 模式兼容项；full 模式无需设置")
    bench.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    bench.set_defaults(func=cmd_bench)

    console = sub.add_parser("console", help="交互式控制台（人工可实时介入）")
    add_common(console)
    console.add_argument("--approve", choices=["off", "exploit", "verify", "all"], default="off")
    console.add_argument("--plain", action="store_true", help="关闭终端颜色")
    console.add_argument("--no-lab-autostart", action="store_true")
    console.set_defaults(func=cmd_console)

    run = sub.add_parser("run", help="对目标执行全自动渗透")
    add_common(run)
    run.add_argument("--target", "-t", help="目标（host:port 或 URL）")
    run.add_argument("--goal", "-g", default=None, help="本次任务目标（未提供时沿用当前项目 Goal）")
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
        help="restricted 模式兼容项：把动作等级上限提升到 exploit；full 模式无需设置",
    )
    run.add_argument("--no-report", action="store_true", help="不生成报告")
    run.add_argument("--teardown", action="store_true", help="结束后关闭自建靶场")
    run.set_defaults(func=cmd_run)

    swarm = sub.add_parser("swarm", help="可复用 Worker 并行执行任务 DAG")
    add_common(swarm)
    swarm.add_argument("--target", "-t", help="目标（使用默认任务图）")
    swarm.add_argument("--goal", "-g", default="", help="最终研究目标")
    swarm.add_argument("--manifest", help="YAML/JSON 任务图文件")
    swarm.add_argument("--resume", help="从 runs/<id>/swarm.json 继续")
    swarm.add_argument("--workers", type=int, default=2, help="最大并行 Worker 数（默认 2）")
    swarm.add_argument("--max-steps", type=int, default=12, help="默认图每任务步数上限")
    swarm.add_argument("--max-seconds", type=int, default=600, help="默认图每任务秒数上限")
    swarm.add_argument("--model", help="覆盖模型名")
    swarm.add_argument("--backend", choices=["omp", "openai", "replay"])
    swarm.add_argument("--replay", help="离线回放脚本（每个 Worker 独立加载）")
    swarm.add_argument("--allow-exploit", action="store_true", help="restricted 模式兼容项；full 模式无需设置")
    swarm.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    swarm.set_defaults(func=cmd_swarm)

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

    watch = sub.add_parser("watch", help="实时监视一次运行（工作台视图）")
    add_common(watch)
    watch.add_argument("run_id", nargs="?", help="运行 ID（省略则监视最近一次）")
    watch.add_argument("--interval", type=float, default=2.0, help="刷新间隔秒（默认 2）")
    watch.add_argument("--once", action="store_true", help="只打印一次")
    watch.set_defaults(func=cmd_watch)

    review = sub.add_parser("review", help="评价 Agent：只读复核一次运行的声明与证据")
    add_common(review)
    review.add_argument("--state", help="runs/<run_id>/state.json（省略则复核最近一次运行）")
    review.add_argument("--audit", help="审计 JSONL（默认取 state.json 里记录的路径）")
    review.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    review.add_argument("--save", metavar="DIR", help="保存复核记录到目录（默认 review/）")
    review.add_argument("--no-registry", action="store_true", help="不加载注册表（只做证据核对）")
    review.set_defaults(func=cmd_review)

    report = sub.add_parser("report", help="报告")
    add_common(report)
    report_sub = report.add_subparsers(dest="report_action", required=True)
    report_rebuild = report_sub.add_parser("rebuild", help="由 state.json 重建报告")
    report_rebuild.add_argument("--state", required=True)
    report_rebuild.set_defaults(func=cmd_report)

    goal = sub.add_parser("goal", help="保存、查看或清除当前项目目标")
    goal.add_argument("goal_action", choices=("show", "set", "clear"), nargs="?", default="show")
    goal.add_argument("text", nargs="*")
    goal.set_defaults(func=cmd_goal)

    schedule = sub.add_parser("schedule", aliases=["scheduled", "schedualed"], help="一次性定时任务")
    schedule_sub = schedule.add_subparsers(dest="schedule_action", required=True)
    listing = schedule_sub.add_parser("list")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_schedule)
    create = schedule_sub.add_parser("add")
    add_common(create)
    create.add_argument("--at", required=True, help="HH:MM、ISO 日期时间或 +10m")
    create.add_argument("--target", required=True)
    create.add_argument("--goal")
    create.add_argument("--max-steps", type=int, default=12)
    create.add_argument("--approve", choices=("off", "exploit", "verify", "all"), default="off")
    create.set_defaults(func=cmd_schedule)
    cancel = schedule_sub.add_parser("cancel")
    cancel.add_argument("task_id")
    cancel.set_defaults(func=cmd_schedule)
    serve = schedule_sub.add_parser("run")
    serve.add_argument("--once", action="store_true", help="仅领取一次已到点任务")
    serve.set_defaults(func=cmd_schedule)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # 裸命令 → 交互式控制台（人工可介入）
        return cmd_console(
            argparse.Namespace(approve="off", config=None, set=None, no_lab_autostart=False)
        )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ValueError) as exc:
        out(f"配置错误: {exc}", "red")
        return 2
    except KeyboardInterrupt:
        out("\n已中断", "yellow")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
