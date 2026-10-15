"""靶场适配层核心契约：RangeTarget / GroundTruthItem / RangeInfo / RangeAdapter + 加载器。

设计原则
--------
1. **适配器只负责「把靶场变成目标 + ground truth」**，不负责打靶；打靶是 brain + tools 的事。
2. **绝不抛异常给调用方**：靶场没起来、docker 缺失、远端 API 连不上，都通过
   `status()["ready"] is False` + `detail` 表达，`prepare()` 返回「空 targets + notes」而不是崩溃。
3. **ground truth 与 flag 校验同源**：评测器的 TP/FP/FN 口径完全来自 `configs/ranges/*.yaml`，
   避免人工口径漂移。
4. 适配器本身不绕过护栏：所有出网/执行外部命令的动作都走 `trina.tools`（HTTP 探测除外，
   本地接口探测属于靶场自带能力，见各子模块说明）。

加载器
------
    load_adapter(cfg, "dvwa")                       # configs/ranges/dvwa.yaml
    load_adapter(cfg, "configs/ranges/dvwa.yaml")   # 相对项目根
    load_adapter(cfg, "/abs/path/to/dvwa.yaml")
    load_adapter(cfg, None)                         # 环境变量 TRINA_RANGE → default.yaml → local.yaml
    list_adapters(cfg)                              # 扫描 configs/ranges/*.yaml
"""

from __future__ import annotations

import os
import re
import socket
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from ..config import TrinaConfig

#: 项目根目录（`trina/adapters/base.py` → parents[2]）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 靶场注册表默认目录（相对项目根）。
DEFAULT_RANGES_DIR = "configs/ranges"

#: 靶场配置文件的默认名（`load_adapter(cfg, None)` 时按序查找）。
DEFAULT_RANGE_FILENAMES = ("default.yaml", "default.yml", "local.yaml", "local.yml")

#: 读取默认靶场的环境变量（按序取第一个非空值）。
DEFAULT_RANGE_ENVS = ("TRINA_RANGE", "TRINA_DEFAULT_RANGE")

#: 未声明 `flag_patterns` 时的兜底形态。
DEFAULT_FLAG_PATTERNS: tuple[str, ...] = (
    r"flag\{[^}]{1,200}\}",
    r"Trina\{[^}]{1,200}\}",
    r"CTF\{[^}]{1,200}\}",
)

#: `RangeInfo.kind` 合法取值。
RANGE_KINDS = ("web", "host", "mixed")


class RangeError(ValueError):
    """靶场配置非法或无法定位（宁可启动失败，也不带着错误边界跑）。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class RangeTarget:
    """一个可攻击的目标端点（host / port / service / url）。"""

    host: str
    port: int | None = None
    service: str = ""
    url: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def authority(self) -> str:
        """`host[:port]` 形式的规范串（无端口时只返回 host）。"""
        return f"{self.host}:{self.port}" if self.port else self.host


@dataclass
class GroundTruthItem:
    """一条 ground truth：评测器据此判定 TP / FP / FN。"""

    vuln_class: str
    target: str = ""
    url: str = ""
    parameter: str = ""
    severity: str = "medium"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RangeInfo:
    """一次 `prepare()` 的结果：目标清单 + ground truth + flag 形态。"""

    name: str
    kind: str
    targets: list[RangeTarget] = field(default_factory=list)
    ground_truth: list[GroundTruthItem] = field(default_factory=list)
    flag_patterns: list[str] = field(default_factory=list)
    notes: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "targets": [t.to_dict() for t in self.targets],
            "ground_truth": [g.to_dict() for g in self.ground_truth],
            "flag_patterns": list(self.flag_patterns),
            "notes": self.notes,
            "source": self.source,
        }

    @property
    def ready(self) -> bool:
        """探测到目标才算就绪（`prepare()` 拿不到目标时保持 False）。"""
        return bool(self.targets)

    def summary(self) -> str:
        kinds = sorted({g.vuln_class for g in self.ground_truth})
        return (
            f"{self.name}({self.kind}) 目标 {len(self.targets)} 个，"
            f"ground truth {len(self.ground_truth)} 条"
            + (f"（{', '.join(kinds)}）" if kinds else "")
        )


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_targets(raw: Iterable[Any] | None) -> list[RangeTarget]:
    """把 yaml 里的 targets 列表解析成 `RangeTarget`（兼容简写字符串）。"""
    out: list[RangeTarget] = []
    for item in raw or []:
        if isinstance(item, str):
            host, _, port_str = item.rpartition(":")
            if host and port_str.isdigit():
                out.append(RangeTarget(host=host, port=int(port_str)))
            else:
                out.append(RangeTarget(host=item))
            continue
        if not isinstance(item, Mapping):
            continue
        host = str(item.get("host") or "").strip()
        if not host:
            continue
        out.append(
            RangeTarget(
                host=host,
                port=_as_int(item.get("port")),
                service=str(item.get("service") or ""),
                url=str(item.get("url") or ""),
                notes=str(item.get("notes") or item.get("note") or ""),
            )
        )
    return out


def parse_ground_truth(raw: Iterable[Any] | None) -> list[GroundTruthItem]:
    """把 yaml 里的 ground_truth 列表解析成 `GroundTruthItem`。

    键名宽松：`vuln_class` / `class` / `type` / `vuln` 均可；`note` / `notes` 均可。
    """
    out: list[GroundTruthItem] = []
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        vuln_class = str(
            item.get("vuln_class") or item.get("class") or item.get("type") or item.get("vuln") or ""
        ).strip()
        if not vuln_class:
            continue
        out.append(
            GroundTruthItem(
                vuln_class=vuln_class,
                target=str(item.get("target") or ""),
                url=str(item.get("url") or ""),
                parameter=str(item.get("parameter") or item.get("param") or ""),
                severity=str(item.get("severity") or "medium"),
                note=str(item.get("note") or item.get("notes") or ""),
            )
        )
    return out


def tcp_probe(host: str, port: int | None, timeout: float = 3.0) -> bool:
    """一次 TCP connect 连通性检查（不发送任何应用层数据）。"""
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def split_authority(url: str, default_port: int | None = None) -> tuple[str, int | None]:
    """从 URL / host:port 里拆出 `(host, port)`。"""
    text = (url or "").strip()
    if not text:
        return "", default_port
    if "://" in text:
        from urllib.parse import urlsplit

        parts = urlsplit(text)
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        return parts.hostname or "", port
    host, _, tail = text.rpartition(":")
    if host and tail.isdigit():
        return host, int(tail)
    return text, default_port


def merge_flag_patterns(*groups: Iterable[str] | None) -> list[str]:
    """去重合并多组 flag 正则（保持原顺序）。"""
    seen: dict[str, None] = {}
    for group in groups:
        for pattern in group or []:
            text = str(pattern).strip()
            if text and text not in seen:
                seen[text] = None
    return list(seen)


# ---------------------------------------------------------------------------
# 适配器基类
# ---------------------------------------------------------------------------


class RangeAdapter(ABC):
    """靶场适配器基类。

    子类必须实现：`status()` / `prepare()` / `primary_target()`。
    其余方法在基类给出通用实现（`teardown()` 默认 no-op，远程靶场无需清理）。
    """

    name: str
    kind: str

    def __init__(self, config: TrinaConfig, spec: Mapping[str, Any] | None = None, path: Path | None = None):
        self.config = config
        self.spec: dict[str, Any] = dict(spec or {})
        self.path: Path | None = Path(path) if path else None
        stem = self.path.stem if self.path else type(self).__name__
        self.name = str(self.spec.get("name") or stem)
        kind = str(self.spec.get("kind") or "mixed").strip().lower()
        self.kind = kind if kind in RANGE_KINDS else "mixed"
        self.description = str(self.spec.get("description") or "")
        self.notes = str(self.spec.get("notes") or "")
        self.source = str(self.spec.get("source") or (str(self.path) if self.path else ""))
        self._ground_truth = parse_ground_truth(self.spec.get("ground_truth"))
        self._flag_patterns = [str(p) for p in (self.spec.get("flag_patterns") or [])]
        self._detail = ""

    # ------------------------------------------------------------ 生命周期
    @abstractmethod
    def status(self) -> dict:
        """免副作用的就绪探测：`{"ready": bool, "detail": str, "targets": [...]}`。"""

    @abstractmethod
    def prepare(self) -> RangeInfo:
        """起靶场 / 做连通性检查，返回目标与 ground truth。**不得抛异常**。"""

    def teardown(self) -> None:
        """关闭自建靶场；远程/已运行的靶场为 no-op。"""

    def reset(self) -> None:
        """复位靶场：默认「拆掉再起」。"""
        self.teardown()
        self.prepare()

    def ground_truth(self) -> list[GroundTruthItem]:
        return list(self._ground_truth)

    def flag_patterns(self) -> list[str]:
        return list(self._flag_patterns)

    def check_flag(self, value: str) -> bool:
        """按 `flag_patterns` 判断字符串是否像 flag（正则非法时退化为子串匹配）。"""
        text = (value or "").strip()
        if not text:
            return False
        for pattern in self._flag_patterns or list(DEFAULT_FLAG_PATTERNS):
            try:
                if re.search(pattern, text):
                    return True
            except re.error:
                if pattern in text:
                    return True
        return False

    @abstractmethod
    def primary_target(self) -> str:
        """给 agent 的起始目标（如 `http://127.0.0.1:8080/pikachu`）。"""

    # ------------------------------------------------------------ 便捷方法
    def info(self) -> RangeInfo:
        """最近一次 `prepare()` 的结果；未准备过则返回配置快照。"""
        return self._info_snapshot()

    def _info_snapshot(self) -> RangeInfo:
        return RangeInfo(
            name=self.name,
            kind=self.kind,
            targets=[],
            ground_truth=self.ground_truth(),
            flag_patterns=self.flag_patterns(),
            notes=self.notes,
            source=self.source,
        )

    def _set_detail(self, detail: str) -> str:
        self._detail = detail
        return detail

    def status_dict(self, ready: bool, detail: str, targets: Iterable[RangeTarget] | None = None) -> dict:
        """统一 `status()` 返回结构。"""
        self._set_detail(detail)
        return {
            "ready": bool(ready),
            "detail": detail,
            "name": self.name,
            "kind": self.kind,
            "adapter": type(self).__name__,
            "source": self.source,
            "targets": [t.to_dict() for t in (targets or [])],
        }

    def describe(self) -> dict[str, Any]:
        """给 CLI / 报告用的静态描述。"""
        return {
            "name": self.name,
            "kind": self.kind,
            "adapter": type(self).__name__,
            "source": self.source,
            "path": str(self.path) if self.path else "",
            "description": self.description,
            "notes": self.notes,
            "ground_truth": [g.to_dict() for g in self.ground_truth()],
            "flag_patterns": self.flag_patterns(),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} name={self.name!r} kind={self.kind}>"


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------


def ranges_dir(config: TrinaConfig | None = None) -> Path:
    """靶场注册表目录（来自 `config.eval.ranges_dir`，相对项目根解析）。"""
    raw = ""
    if config is not None:
        raw = str(getattr(config.eval, "ranges_dir", "") or "")
    path = Path(raw or DEFAULT_RANGES_DIR)
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_range_spec(path: str | Path) -> dict[str, Any]:
    """读取并校验一个靶场 yaml。"""
    target = Path(path)
    if not target.exists():
        raise RangeError(f"靶场配置不存在: {target}")
    try:
        loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RangeError(f"靶场配置 YAML 解析失败 {target}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RangeError(f"{target} 顶层必须是映射")
    return loaded


def default_range_candidate(config: TrinaConfig) -> Path | None:
    """默认靶场：环境变量 `TRINA_RANGE`/`TRINA_DEFAULT_RANGE` → `default.yaml` → `local.yaml`；都没有则 None。"""
    for env_key in DEFAULT_RANGE_ENVS:
        value = os.environ.get(env_key, "").strip()
        if value:
            return resolve_range_path(config, value)
    directory = ranges_dir(config)
    for filename in DEFAULT_RANGE_FILENAMES:
        if (directory / filename).exists():
            return directory / filename
    return None


def resolve_range_path(config: TrinaConfig, name: str | Path | None = None) -> Path:
    """把简写 / 相对 / 绝对路径统一解析为靶场 yaml 路径。"""
    candidate = name
    directory = ranges_dir(config)
    if candidate is None:
        found = default_range_candidate(config)
        if found is not None:
            return found
        raise RangeError(
            f"未指定靶场，且 {directory} 下没有默认靶场（{', '.join(DEFAULT_RANGE_FILENAMES)}）；"
            f"可用: {available_range_names(config)}"
        )

    text = str(candidate).strip()
    looks_like_path = Path(text).is_absolute() or text.lower().endswith((".yaml", ".yml")) or "/" in text or "\\" in text
    if looks_like_path:
        for option in (Path(text), PROJECT_ROOT / text):
            if option.exists():
                return option
        raise RangeError(f"靶场配置不存在: {text}")

    for option in (directory / f"{text}.yaml", directory / f"{text}.yml", PROJECT_ROOT / f"{text}.yaml"):
        if option.exists():
            return option
    raise RangeError(f"未知靶场 {text!r}；可用: {available_range_names(config)}")


def available_range_names(config: TrinaConfig) -> list[str]:
    directory = ranges_dir(config)
    if not directory.is_dir():
        return []
    return sorted({p.stem for p in list(directory.glob("*.yaml")) + list(directory.glob("*.yml"))})


#: `adapter:` 字段的别名 → 实现类名。
_ADAPTER_ALIASES: dict[str, str] = {
    "http_range": "HttpRangeAdapter",
    "http-range": "HttpRangeAdapter",
    "http": "HttpRangeAdapter",
    "web": "HttpRangeAdapter",
    "docker": "DockerComposeRangeAdapter",
    "compose": "DockerComposeRangeAdapter",
    "docker_compose": "DockerComposeRangeAdapter",
    "docker-compose": "DockerComposeRangeAdapter",
    "local": "LocalRangeAdapter",
    "host": "LocalRangeAdapter",
    "tsecbench": "TSecBenchAdapter",
    "tsb": "TSecBenchAdapter",
}

_CLASSES: dict[str, type[RangeAdapter]] | None = None


def adapter_classes() -> dict[str, type[RangeAdapter]]:
    """实现类名 → 类（延迟导入，避免与子模块循环依赖）。"""
    global _CLASSES
    if _CLASSES is None:
        from .docker import DockerComposeRangeAdapter
        from .http_range import HttpRangeAdapter
        from .local import LocalRangeAdapter
        from .tsecbench import TSecBenchAdapter

        _CLASSES = {
            "HttpRangeAdapter": HttpRangeAdapter,
            "DockerComposeRangeAdapter": DockerComposeRangeAdapter,
            "LocalRangeAdapter": LocalRangeAdapter,
            "TSecBenchAdapter": TSecBenchAdapter,
        }
    return _CLASSES


def adapter_class_for(spec: Mapping[str, Any]) -> type[RangeAdapter]:
    """按 `adapter:` 字段选择实现类；缺失时按配置键推断。"""
    classes = adapter_classes()
    declared = str(spec.get("adapter") or spec.get("type") or "").strip().lower()
    if declared:
        class_name = _ADAPTER_ALIASES.get(declared) or _ADAPTER_ALIASES.get(declared.replace("_", "-"), "")
        if not class_name:
            raise RangeError(f"未知适配器类型 adapter={declared!r}，可选: {sorted(set(_ADAPTER_ALIASES))}")
        return classes[class_name]

    if spec.get("compose_file") or spec.get("instances"):
        return classes["DockerComposeRangeAdapter"]
    if spec.get("bundle_dir") or spec.get("endpoints") or str(spec.get("mode") or "").lower() in ("local", "remote") and spec.get("tasks_endpoint"):
        return classes["TSecBenchAdapter"]
    if spec.get("base_url") or spec.get("url"):
        return classes["HttpRangeAdapter"]
    if spec.get("targets"):
        return classes["LocalRangeAdapter"]
    return classes["LocalRangeAdapter"]


def empty_local_adapter(config: TrinaConfig) -> "RangeAdapter":
    """没有任何默认靶场配置时的兜底：空的 local 适配器（不抛异常，只提示怎么配）。"""
    spec = {
        "name": "local",
        "kind": "mixed",
        "adapter": "local",
        "description": "兜底空靶场：未配置默认靶场",
        "notes": (
            f"未指定靶场且 {ranges_dir(config)} 下没有 "
            f"{'/'.join(DEFAULT_RANGE_FILENAMES)}；请 export TRINA_RANGE=<name>、"
            "或创建 configs/ranges/local.yaml 声明 targets，或用 --range <name> 显式指定"
        ),
    }
    return adapter_classes()["LocalRangeAdapter"](config, spec, None)


def load_adapter(config: TrinaConfig, name: str | Path | None = None) -> RangeAdapter:
    """按简写名 / 路径 / 默认值装载靶场适配器。

    `name` 取值：
      - `"dvwa"` → `<ranges_dir>/dvwa.yaml`
      - `"configs/ranges/dvwa.yaml"` → 相对项目根
      - 绝对路径
      - `None` → 环境变量 `TRINA_RANGE` → `default.yaml` → `local.yaml` → 兜底 local（空靶场）
    """
    if name is None:
        found = default_range_candidate(config)
        if found is None:
            return empty_local_adapter(config)
        name = found
    path = resolve_range_path(config, name)
    spec = read_range_spec(path)
    adapter_class = adapter_class_for(spec)
    return adapter_class(config, spec, path)


def list_adapters(cfg: TrinaConfig) -> list[dict]:
    """扫描 `configs/ranges/*.yaml`，返回每个靶场的 `name/kind/source/ready`。"""
    directory = ranges_dir(cfg)
    entries: list[dict] = []
    if not directory.is_dir():
        return entries
    for path in sorted(list(directory.glob("*.yaml")) + list(directory.glob("*.yml"))):
        entry: dict[str, Any] = {
            "name": path.stem,
            "kind": "unknown",
            "source": str(path),
            "ready": False,
            "detail": "",
            "targets": [],
        }
        try:
            spec = read_range_spec(path)
            adapter = adapter_class_for(spec)(cfg, spec, path)
        except Exception as exc:  # 配置非法不影响其它靶场列出
            entry["detail"] = f"配置非法: {exc}"
            entries.append(entry)
            continue
        entry["kind"] = adapter.kind
        entry["source"] = adapter.source or str(path)
        entry["adapter"] = type(adapter).__name__
        if adapter.name != path.stem:
            entry["title"] = adapter.name
        entry["ground_truth"] = len(adapter.ground_truth())
        try:
            status = adapter.status()
        except Exception as exc:  # status 自身出错也要如实上报
            status = {"ready": False, "detail": f"status 探测失败: {exc}", "targets": []}
        entry["ready"] = bool(status.get("ready"))
        entry["detail"] = str(status.get("detail") or "")
        entry["targets"] = status.get("targets") or []
        entries.append(entry)
    return entries


__all__ = [
    "DEFAULT_FLAG_PATTERNS",
    "DEFAULT_RANGES_DIR",
    "GroundTruthItem",
    "PROJECT_ROOT",
    "RANGE_KINDS",
    "RangeAdapter",
    "RangeError",
    "RangeInfo",
    "RangeTarget",
    "adapter_class_for",
    "adapter_classes",
    "available_range_names",
    "default_range_candidate",
    "empty_local_adapter",
    "list_adapters",
    "load_adapter",
    "merge_flag_patterns",
    "parse_ground_truth",
    "parse_targets",
    "ranges_dir",
    "read_range_spec",
    "resolve_range_path",
    "split_authority",
    "tcp_probe",
]
