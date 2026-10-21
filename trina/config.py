"""配置模型：YAML + 环境变量覆盖 + 校验。

设计要点
--------
* 配置是**唯一**的可变输入面：护栏边界（授权网段/动作等级）只来自这里。
* 任何字段都可用 `TRINA_<SECTION>_<KEY>` 环境变量覆盖（如 `TRINA_SCOPE_EXPLOIT_LEVEL=detect`）。
* 上下文管理器 `TrinaConfig.load()` 返回不可变语义的对象，调用方不应就地改字段
  （需要临时覆盖请用 `dataclasses.replace` 或 `with_overrides`）。
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "trina.yaml"

#: 动作等级，从低到高。工具声明所需等级，护栏比较后放行/拒绝。
ACTION_LEVELS = ("detect", "verify", "exploit")


class ConfigError(ValueError):
    """配置非法（宁可启动失败，也不带着错误边界跑）。"""


@dataclass
class ScopeConfig:
    """授权边界。护栏的判据全部来自这里，不来自 LLM。"""

    authorized_cidrs: list[str] = field(
        default_factory=lambda: [
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ]
    )
    authorized_hosts: list[str] = field(default_factory=list)
    deny_cidrs: list[str] = field(default_factory=list)
    allow_public: bool = False
    allow_loopback: bool = True
    exploit_level: str = "verify"
    destructive: bool = False
    destructive_hosts: list[str] = field(default_factory=list)
    max_qps: float = 20.0
    max_host_concurrency: int = 8
    max_brute_attempts: int = 200
    resolve_dns: bool = True

    def validate(self) -> None:
        if self.exploit_level not in ACTION_LEVELS:
            raise ConfigError(f"scope.exploit_level 必须是 {ACTION_LEVELS} 之一，得到 {self.exploit_level!r}")
        import ipaddress

        for name in ("authorized_cidrs", "deny_cidrs"):
            for cidr in getattr(self, name):
                try:
                    ipaddress.ip_network(cidr, strict=False)
                except ValueError as exc:
                    raise ConfigError(f"scope.{name} 中的 {cidr!r} 不是合法网段: {exc}") from exc
        if self.max_qps <= 0:
            raise ConfigError("scope.max_qps 必须为正数")
        if self.max_brute_attempts <= 0:
            raise ConfigError("scope.max_brute_attempts 必须为正数")

    @property
    def level_index(self) -> int:
        return ACTION_LEVELS.index(self.exploit_level)


@dataclass
class LLMConfig:
    backend: str = "omp"  # omp | openai | replay
    model: str = "deepseek/deepseek-v4-flash"
    timeout: int = 180
    temperature: float = 0.2
    max_tokens: int = 4096
    max_steps: int = 40
    max_seconds: int = 1800
    stall_limit: int = 3
    base_url: str = ""
    api_key_env: str = "TRINA_LLM_API_KEY"
    #: replay 后端读取的脚本路径（测试用）
    replay_script: str = ""

    def validate(self) -> None:
        if self.backend not in ("omp", "openai", "replay"):
            raise ConfigError(f"llm.backend 必须是 omp|openai|replay，得到 {self.backend!r}")
        if self.max_steps <= 0:
            raise ConfigError("llm.max_steps 必须为正整数")


@dataclass
class RunConfig:
    workspace: str = "runs"
    tool_timeout: int = 300
    max_evidence: int = 200
    #: 每 N 步把 state.json 落盘一次(供 trina watch / 外部工具实时读取;0 = 只在结束时落盘)
    flush_state_every: int = 1
    #: 假设排序里的风险厌恶系数 λ:优先级 = 置信度×价值×信息增量 / (成本 + λ·风险 + ε)
    hypothesis_risk_aversion: float = 1.0
    #: 洞察层开关(对照实验用):台账是否向模型展示并登记假设(关闭后仍可手工引用,但不进提示词/不记录)
    hypothesis_ledger: bool = True
    #: 循环守卫/失败分类的建议是否回灌给模型(关闭后仍照常统计 redundant_action_rate 与 fail_* 计数)
    loop_guard: bool = True
    #: 确定性假设调度:把台账优先级翻译成"这一步该验证谁"(绑定注册检查项);关闭后只作为提示词里的排序建议
    hypothesis_scheduler: bool = True
    #: 参与调度的最低优先级(低于它不占用一次调度)
    scheduler_min_priority: float = 0.15
    #: 专职 Agent 只作建议，默认关闭，待真实任务对照评测后再决定是否默认启用。
    specialists: bool = False
    specialist_max_calls: int = 3
    specialist_max_tokens: int = 1200
    specialist_context_chars: int = 6000
    phases: list[str] = field(default_factory=lambda: ["recon", "enum", "vuln", "exploit", "post", "report"])

    def validate(self) -> None:
        if type(self.specialists) is not bool:
            raise ConfigError("run.specialists 必须为布尔值")
        for name, lower, upper in (
            ("specialist_max_calls", 0, 20), ("specialist_max_tokens", 128, 4096),
            ("specialist_context_chars", 1000, 32000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ConfigError(f"run.{name} 必须是 {lower}–{upper} 范围内的整数")


@dataclass
class ToolsConfig:
    prefer_external: bool = True
    wordlists: dict[str, str] = field(
        default_factory=lambda: {
            "dirs": "configs/wordlists/dirs.txt",
            "users": "configs/wordlists/users.txt",
            "passwords": "configs/wordlists/passwords.txt",
        }
    )
    port_ranges: dict[str, str] = field(
        default_factory=lambda: {"quick": "22,80,443,3306,6379,8080", "full": "1-65535"}
    )

    def wordlist_path(self, kind: str) -> Path:
        raw = self.wordlists.get(kind, "")
        path = Path(raw)
        return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass
class ReportConfig:
    output_dir: str = "reports"
    formats: list[str] = field(default_factory=lambda: ["markdown", "html"])
    title: str = "Trina 自动渗透测试报告"
    include_timeline: bool = True
    include_raw_audit: bool = False
    cvss_version: str = "3.1"


@dataclass
class AuditConfig:
    dir: str = "logs"
    record_output_digest: bool = True
    redact_keys: list[str] = field(
        default_factory=lambda: [
            "password",
            "passwd",
            "pwd",
            "token",
            "secret",
            "api_key",
            "cookie",
            "authorization",
        ]
    )


@dataclass
class PolicyConfig:
    """授权策略（计划器提出、控制器批准）的配置。

    * `mode`: `free`（只看护栏）/ `guided`（注册表是提示，未注册目标拒绝）/ `strict`（只接受注册 target_id + test_id）
    * `max_checks` / `logic_seconds` / `stop_on_no_new_evidence`: 预算与停止合同
    * `registry_dirs`: 注册表来源目录（靶场 yaml 里的 targets/tests 段）
    """

    mode: str = "guided"
    max_checks: int = 40
    logic_seconds: int = 900
    stop_on_no_new_evidence: int = 4
    registry_dirs: list[str] = field(default_factory=lambda: ["configs/ranges"])

    def validate(self) -> None:
        from .policy import POLICY_MODES

        if self.mode not in POLICY_MODES:
            raise ConfigError(f"policy.mode 必须是 {POLICY_MODES} 之一，得到 {self.mode!r}")
        if self.max_checks <= 0:
            raise ConfigError("policy.max_checks 必须为正整数")
        if self.logic_seconds <= 0:
            raise ConfigError("policy.logic_seconds 必须为正整数")


@dataclass
class TSecBenchConfig:
    base_url: str = "https://tsecbench.zc.tencent.com"
    api_key_env: str = "TRINA_TSECBENCH_KEY"
    per_task_timeout: int = 3600
    mode: str = "local"  # local(bundle 回放) | remote(官方 API)


@dataclass
class EvalConfig:
    scenarios_dir: str = "trina/eval/scenarios"
    ranges_dir: str = "configs/ranges"
    output_dir: str = "reports/eval"
    repeat: int = 1
    tsecbench: TSecBenchConfig = field(default_factory=TSecBenchConfig)


@dataclass
class TrinaConfig:
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    run: RunConfig = field(default_factory=RunConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    eval: EvalConfig = field(default_factory=EvalConfig)
    path: Path | None = None

    # ------------------------------------------------------------------ 载入
    @classmethod
    def load(cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> "TrinaConfig":
        """载入配置：默认配置 → YAML → 环境变量 → 显式 overrides。"""
        raw: dict[str, Any] = {}
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if cfg_path.exists():
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ConfigError(f"{cfg_path} 顶层必须是映射")
            raw = loaded
        elif path:
            raise ConfigError(f"配置文件不存在: {cfg_path}")

        cfg = cls._from_mapping(raw)
        cfg = cls._apply_env(cfg)
        if overrides:
            cfg = cls._apply_overrides(cfg, overrides)
        cfg.path = cfg_path
        cfg.validate()
        return cfg

    @classmethod
    def _from_mapping(cls, raw: dict[str, Any]) -> "TrinaConfig":
        sections = {
            "scope": ScopeConfig,
            "llm": LLMConfig,
            "run": RunConfig,
            "tools": ToolsConfig,
            "report": ReportConfig,
            "audit": AuditConfig,
            "policy": PolicyConfig,
            "eval": EvalConfig,
        }
        kwargs: dict[str, Any] = {}
        for name, klass in sections.items():
            data = raw.get(name) or {}
            if not isinstance(data, dict):
                raise ConfigError(f"配置段 {name} 必须是映射")
            if name == "eval" and "tsecbench" in data:
                data = dict(data)
                data["tsecbench"] = TSecBenchConfig(**(data["tsecbench"] or {}))
            unknown = set(data) - {f for f in klass.__dataclass_fields__}
            if unknown:
                raise ConfigError(f"配置段 {name} 存在未知字段: {sorted(unknown)}")
            kwargs[name] = klass(**data)
        unknown_top = set(raw) - set(sections)
        if unknown_top:
            raise ConfigError(f"配置存在未知顶层段: {sorted(unknown_top)}")
        return cls(**kwargs)

    @classmethod
    def _apply_env(cls, cfg: "TrinaConfig") -> "TrinaConfig":
        """`TRINA_SCOPE_EXPLOIT_LEVEL=detect` 形式的覆盖（只处理标量字段）。"""
        for section in ("scope", "llm", "run", "report", "audit", "policy"):
            obj = getattr(cfg, section)
            for fname, fdef in obj.__dataclass_fields__.items():
                env_key = f"TRINA_{section.upper()}_{fname.upper()}"
                if env_key not in os.environ:
                    continue
                value = _coerce(os.environ[env_key], fdef.type)
                setattr(obj, fname, value)
        return cfg

    @classmethod
    def _apply_overrides(cls, cfg: "TrinaConfig", overrides: dict[str, Any]) -> "TrinaConfig":
        """点号路径覆盖：`{"scope.destructive": True, "llm.model": "x"}`。"""
        for dotted, value in overrides.items():
            if value is None:
                continue
            parts = dotted.split(".")
            if len(parts) != 2:
                raise ConfigError(f"覆盖键必须形如 section.field，得到 {dotted!r}")
            section, fname = parts
            if not hasattr(cfg, section):
                raise ConfigError(f"未知配置段: {section}")
            obj = getattr(cfg, section)
            if fname not in obj.__dataclass_fields__:
                raise ConfigError(f"未知配置字段: {dotted}")
            setattr(obj, fname, value)
        return cfg

    # ------------------------------------------------------------------ 校验
    def validate(self) -> None:
        self.scope.validate()
        self.llm.validate()
        self.run.validate()
        self.policy.validate()
        if self.audit.dir == "":
            raise ConfigError("audit.dir 不能为空")
        if not self.report.formats:
            raise ConfigError("report.formats 不能为空")

    # ------------------------------------------------------------------ 派生
    def with_overrides(self, **overrides: Any) -> "TrinaConfig":
        """返回副本并应用 `section__field=value` 形式的覆盖。"""
        cfg = copy.deepcopy(self)
        for key, value in overrides.items():
            if "__" not in key:
                raise ConfigError(f"覆盖键必须形如 section__field，得到 {key!r}")
            section, fname = key.split("__", 1)
            setattr(getattr(cfg, section), fname, value)
        cfg.validate()
        return cfg

    @property
    def audit_dir(self) -> Path:
        return _resolve(self.audit.dir)

    @property
    def report_dir(self) -> Path:
        return _resolve(self.report.output_dir)

    @property
    def workspace_dir(self) -> Path:
        return _resolve(self.run.workspace)

    def scope_prompt_block(self) -> str:
        """给 LLM 看的授权边界描述（与护栏真实判据同源，避免提示词漂移）。"""
        s = self.scope
        lines = [
            f"授权网段: {', '.join(s.authorized_cidrs) or '(无)'}",
            f"额外授权主机: {', '.join(s.authorized_hosts) or '(无)'}",
            f"显式拒绝网段: {', '.join(s.deny_cidrs) or '(无)'}",
            f"允许公网目标: {'是' if s.allow_public else '否'}",
            f"当前最大动作等级: {s.exploit_level}（detect 只读 / verify 可验证 / exploit 可执行命令）",
            f"破坏性动作: {'开启（仅限 ' + ', '.join(s.destructive_hosts) + '）' if s.destructive else '关闭'}",
            f"全局速率上限: {s.max_qps} req/s，单主机并发 {s.max_host_concurrency}，爆破尝试上限 {s.max_brute_attempts}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        data = asdict(self)
        data.pop("path", None)
        return data


def _coerce(value: str, type_hint: Any) -> Any:
    """把环境变量字符串转成目标字段类型。"""
    hint = str(type_hint)
    if "bool" in hint:
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    if "int" in hint:
        return int(value)
    if "float" in hint:
        return float(value)
    if "list" in hint:
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _resolve(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path
