"""配置模型：YAML + 环境变量覆盖 + 校验。

设计要点
--------
* 默认采用 ``full`` 信任模式；需要旧边界时显式切换到 ``restricted``。
* 任何字段都可用 `TRINA_<SECTION>_<KEY>` 环境变量覆盖（如 `TRINA_SCOPE_EXPLOIT_LEVEL=detect`）。
* 上下文管理器 `TrinaConfig.load()` 返回不可变语义的对象，调用方不应就地改字段
  （需要临时覆盖请用 `dataclasses.replace` 或 `with_overrides`）。
"""

from __future__ import annotations

import copy
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_CONFIG_PATH = PROJECT_ROOT / "configs" / "trina.yaml"
PACKAGED_CONFIG_PATH = Path(__file__).with_name("defaults.yaml")
DEFAULT_CONFIG_PATH = SOURCE_CONFIG_PATH if SOURCE_CONFIG_PATH.exists() else PACKAGED_CONFIG_PATH

#: 动作等级，从低到高。工具声明所需等级，护栏比较后放行/拒绝。
ACTION_LEVELS = ("detect", "verify", "exploit")
TRUST_MODES = ("full", "restricted")


class ConfigError(ValueError):
    """配置非法（宁可启动失败，也不带着错误边界跑）。"""


@dataclass
class ScopeConfig:
    """执行信任配置。默认完全信任，受限模式保留原有边界。"""

    trust_mode: str = "full"
    authorized_cidrs: list[str] = field(
        default_factory=lambda: [
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ]
    )
    authorized_hosts: list[str] = field(default_factory=list)
    #: 基准平台控制面主机白名单（只允许答题 API，不允许作为扫描目标）。
    #: 由 `trina bench` 在启动时从 BENCHMARK_BASE_URL 写入并记入审计。
    control_plane_hosts: list[str] = field(default_factory=list)
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
        if self.trust_mode not in TRUST_MODES:
            raise ConfigError(f"scope.trust_mode 必须是 {TRUST_MODES} 之一，得到 {self.trust_mode!r}")
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

    @property
    def full_trust(self) -> bool:
        return self.trust_mode == "full"


@dataclass
class LLMConfig:
    backend: str = "openai"  # openai | replay
    model: str = "deepseek-chat"
    timeout: int = 180
    temperature: float = 0.2
    max_tokens: int = 4096
    max_steps: int = 40
    max_seconds: int = 1800
    stall_limit: int = 3
    base_url: str = ""
    api_key_env: str = "TRINA_LLM_API_KEY"
    profile: str = ""
    api: str = "chat"  # chat | anthropic | responses
    reasoning_effort: str = "default"
    #: replay 后端读取的脚本路径（测试用）
    replay_script: str = ""
    #: 模型协议：auto(后端支持就用原生 Tool Calling) | json(只用 JSON Action) | native(强制原生)
    tool_protocol: str = "auto"
    #: 原生协议被供应商拒绝时是否自动降级到 JSON Action（只降级一次，不无限重试）
    tool_protocol_fallback: bool = True
    #: 提示词分层：true 使用 Core/Role/Contract/Dynamic 四层；false 保持旧拼装（对照实验用）
    prompt_layers: bool = False
    #: 可选附加角色描述；默认使用共用安全工程师 Core。
    role_profile: str = ""
    #: 缓存扩展只发给已知官方接口或显式声明能力的连接。
    cache_mode: str = "auto"  # auto | explicit | off
    cache_ttl: str = ""
    cache_features: list[str] = field(default_factory=list)
    cache_namespace: str = "trina"
    #: 保守的本地上下文预算，不代表供应商声明的模型容量。
    context_window: int = 0  # 0 自动：DeepSeek 500K，已确认百万模型 1M
    compaction_threshold: float = 0.8
    context_keep_messages: int = 8
    state_checkpoint_interval: int = 16

    @property
    def effective_context_window(self) -> int:
        from .model_context import default_context_window
        return self.context_window or default_context_window(self.model, self.base_url)

    def validate(self) -> None:
        if self.backend not in ("openai", "replay"):
            raise ConfigError(f"llm.backend 必须是 openai|replay，得到 {self.backend!r}")
        if self.api not in ("chat", "anthropic", "responses"):
            raise ConfigError("llm.api 必须是 chat|anthropic|responses")
        if self.tool_protocol not in ("auto", "json", "native"):
            raise ConfigError("llm.tool_protocol 必须是 auto|json|native")
        from .llm.reasoning import LEVEL_LABELS
        if self.reasoning_effort not in LEVEL_LABELS:
            raise ConfigError("llm.reasoning_effort 档位无效")
        if self.max_steps <= 0:
            raise ConfigError("llm.max_steps 必须为正整数")
        if self.cache_mode not in {"auto", "explicit", "off"}:
            raise ConfigError("llm.cache_mode 必须是 auto|explicit|off")
        if not isinstance(self.cache_features, list) or any(item not in {
            "openai_key", "openai_retention", "openai_options", "anthropic_cache"
        } for item in self.cache_features):
            raise ConfigError("llm.cache_features 含未知缓存能力")
        if self.cache_ttl not in {"", "5m", "1h", "30m", "12h", "in_memory", "24h"}:
            raise ConfigError("llm.cache_ttl 无效")
        if type(self.context_window) is not int or self.context_window < 0 or self.effective_context_window <= self.max_tokens + 1024 or not 0.3 <= self.compaction_threshold <= 0.95:
            raise ConfigError("上下文预算须大于输出预算加 1024，压缩阈值须在 [0.3,0.95] 内")
        if self.context_keep_messages < 1 or self.state_checkpoint_interval < 1:
            raise ConfigError("上下文保留消息数和状态检查点间隔必须为正整数")


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
    phases: list[str] = field(default_factory=lambda: ["recon", "enum", "vuln", "exploit", "post", "report"])

    def validate(self) -> None:
        if not self.phases or any(not isinstance(item, str) or not item for item in self.phases):
            raise ConfigError("run.phases 必须是非空字符串列表")


@dataclass
class BinaryConfig:
    """二进制工具的资源预算；操作系统隔离独立于目标信任模式。"""

    enabled: bool = True
    analysis_timeout: float = 30
    query_timeout: float = 10
    debug_timeout: float = 20
    idle_timeout: float = 600
    max_file_bytes: int = 64 * 1024 * 1024
    max_output_bytes: int = 8 * 1024 * 1024
    max_query_items: int = 100
    max_memory_bytes: int = 4096
    max_r2_sessions: int = 2
    max_gdb_sessions: int = 1
    sandbox: str = "bwrap"  # bwrap | none；none 是显式的无 OS 隔离选项

    def validate(self) -> None:
        if self.sandbox not in {"bwrap", "none"}:
            raise ConfigError("binary.sandbox 必须为 bwrap|none")
        import math
        for name, value in asdict(self).items():
            if name not in {"enabled", "sandbox"} and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ConfigError(f"binary.{name} 必须为正数")
            if name.startswith("max_") and type(value) is not int:
                raise ConfigError(f"binary.{name} 必须为正整数")


@dataclass
class ToolsConfig:
    prefer_external: bool = True
    wordlists: dict[str, str] = field(default_factory=dict)
    port_ranges: dict[str, str] = field(
        default_factory=lambda: {"quick": "22,80,443,3306,6379,8080", "full": "1-65535"}
    )

    def wordlist_path(self, kind: str) -> Path:
        raw = self.wordlists.get(kind, "")
        if not raw:
            raise ConfigError(f"未配置 {kind} 字典，请显式指定输入")
        path = Path(raw)
        return path if path.is_absolute() else Path.cwd() / path


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

    mode: str = "free"
    max_checks: int = 40
    logic_seconds: int = 900
    stop_on_no_new_evidence: int = 4
    registry_dirs: list[str] = field(default_factory=list)

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
    ranges_dir: str = ""
    output_dir: str = "reports/eval"
    repeat: int = 1
    tsecbench: TSecBenchConfig = field(default_factory=TSecBenchConfig)


@dataclass
class UIConfig:
    """控制台显示配置；不影响模型权限或执行策略。"""

    status_line_preset: str = "default"
    status_line_separator: str = "dot"
    context_bar: bool = True
    motion: str = "full"

    def validate(self) -> None:
        if self.status_line_preset not in {"default", "compact", "full"}:
            raise ConfigError("ui.status_line_preset 必须为 default/compact/full")
        if self.status_line_separator not in {"dot", "pipe", "slash"}:
            raise ConfigError("ui.status_line_separator 必须为 dot/pipe/slash")
        if type(self.context_bar) is not bool or self.motion not in {"full", "reduced", "none"}:
            raise ConfigError("ui.context_bar 必须为布尔值，ui.motion 必须为 full/reduced/none")


@dataclass
class TrinaConfig:
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    run: RunConfig = field(default_factory=RunConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    binary: BinaryConfig = field(default_factory=BinaryConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    eval: EvalConfig = field(default_factory=EvalConfig)
    ui: UIConfig = field(default_factory=UIConfig)
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
        # 仅默认启动载入所选连接；显式配置文件仍控制自己的模型设置。
        from .providers import ProfileStore

        profile_explicit = bool(cfg.llm.profile) or "TRINA_LLM_PROFILE" in os.environ or "llm.profile" in (overrides or {})
        requested_profile = (overrides or {}).get("llm.profile", os.environ.get("TRINA_LLM_PROFILE", cfg.llm.profile))
        if profile_explicit or (path is None and cfg.llm.backend != "replay"):
            store = ProfileStore()
            selected = requested_profile if profile_explicit else store.active
            if selected:
                if selected not in store.profiles:
                    raise ConfigError(f"模型连接不存在: {selected}")
                store.apply(cfg.llm, selected)
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
            "binary": BinaryConfig,
            "report": ReportConfig,
            "audit": AuditConfig,
            "policy": PolicyConfig,
            "eval": EvalConfig,
            "ui": UIConfig,
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
        for section in ("scope", "llm", "run", "binary", "report", "audit", "policy", "ui"):
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
        self.binary.validate()
        self.policy.validate()
        self.ui.validate()
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
        return self.resolve_runtime_path(self.audit.dir)

    @property
    def report_dir(self) -> Path:
        return self.resolve_runtime_path(self.report.output_dir)

    @property
    def workspace_dir(self) -> Path:
        return self.resolve_runtime_path(self.run.workspace)

    @property
    def runtime_root(self) -> Path:
        """运行产物根目录：源码项目、显式项目配置与安装包彼此隔离。"""
        if self.path:
            config_path = self.path.expanduser().resolve()
            if config_path == PACKAGED_CONFIG_PATH.resolve():
                return _user_data_root()
            if config_path.parent.name.lower() == "configs":
                return config_path.parent.parent
            return config_path.parent
        return _default_runtime_root()

    def resolve_runtime_path(self, raw: str | Path) -> Path:
        """把运行产物路径相对当前配置所属项目解析。"""
        return _resolve(raw, base=self.runtime_root)

    def scope_prompt_block(self) -> str:
        """给运行协议看的信任模式摘要。"""
        s = self.scope
        if s.full_trust:
            return (
                "信任模式: full（默认完全信任）\n"
                "已注册工具可直接执行；目标、动作等级、破坏性方法、载荷模式与尝试规模不触发授权拒绝。\n"
                f"仍保留参数校验、执行超时、取消、审计与速率控制: {s.max_qps} req/s，单主机并发 {s.max_host_concurrency}。"
            )
        lines = [
            "信任模式: restricted（显式受限）",
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


def _user_data_root() -> Path:
    """安装版默认数据目录；可用 ``TRINA_HOME`` 显式覆盖。"""
    return Path(os.environ.get("TRINA_HOME") or Path.home() / ".trina").expanduser()


def _default_runtime_root() -> Path:
    """源码树写仓库根；Wheel 安装写用户数据目录。"""
    return PROJECT_ROOT if SOURCE_CONFIG_PATH.exists() else _user_data_root()


def _resolve(raw: str | Path, *, base: Path | None = None) -> Path:
    """解析运行路径，不把安装版产物写入 ``site-packages``。

    CLI 可以从任意目录启动（WSL 里常见的是 ``/mnt/c/Windows/system32``），
    源码树相对仓库根，Wheel 相对 ``~/.trina``。显式配置由
    :class:`TrinaConfig` 传入其项目根；绝对路径始终保持不变。
    """
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base or _default_runtime_root()) / path
