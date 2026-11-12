"""统一 Capability 描述与按需发现。

Catalog 只回答“有哪些能力、哪些最匹配”，不执行命令、不授予权限。
现有 ToolRegistry 会被投影为同一种 Capability；外部 YAML/JSON 也走同一模型。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .. import __version__
from ..tools import ToolRegistry


_CAPABILITY_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_FIELDS = {"id", "version", "description", "provides", "interface", "guidance", "runtime", "policy", "requires"}


@dataclass(frozen=True)
class Capability:
    """能力的五个侧面：接口、指导、运行时、策略和依赖。"""

    id: str
    version: str
    description: str
    provides: tuple[str, ...]
    interface: dict[str, Any]
    guidance: str
    runtime: dict[str, Any]
    policy: dict[str, Any]
    requires: dict[str, Any]
    source: str = ""
    digest: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source: str = "") -> "Capability":
        if not isinstance(raw, dict):
            raise ValueError("Capability 必须是对象")
        unknown = sorted(set(raw) - _FIELDS)
        if unknown:
            raise ValueError(f"Capability 存在未知字段: {unknown}")
        capability_id = str(raw.get("id") or "").strip()
        version = str(raw.get("version") or "").strip()
        description = str(raw.get("description") or "").strip()
        if not _CAPABILITY_ID.fullmatch(capability_id):
            raise ValueError(f"非法 Capability ID: {capability_id!r}")
        if not version or not description:
            raise ValueError(f"Capability {capability_id} 缺少 version/description")
        provides = tuple(dict.fromkeys(str(item).strip() for item in raw.get("provides") or () if str(item).strip()))
        interface = _mapping(raw.get("interface"), "interface")
        runtime = _mapping(raw.get("runtime"), "runtime")
        policy = _mapping(raw.get("policy"), "policy")
        requires = _mapping(raw.get("requires"), "requires")
        guidance = str(raw.get("guidance") or "").strip()
        if str(runtime.get("type") or "").lower() not in {"tool", "cli", "mcp", "mcp_stdio"}:
            raise ValueError(f"Capability {capability_id} runtime.type 仅支持 tool/cli/mcp")
        canonical = {
            "id": capability_id,
            "version": version,
            "description": description,
            "provides": provides,
            "interface": interface,
            "guidance": guidance,
            "runtime": runtime,
            "policy": policy,
            "requires": requires,
        }
        digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return cls(
            id=capability_id,
            version=version,
            description=description,
            provides=provides,
            interface=interface,
            guidance=guidance,
            runtime=runtime,
            policy=policy,
            requires=requires,
            source=source,
            digest=digest,
        )

    @property
    def tool_name(self) -> str:
        value = self.interface.get("name") or self.runtime.get("tool")
        return str(value or self.id.replace(".", "_").replace("-", "_"))

    def snapshot(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version, "digest": self.digest, "source": self.source}

    def load_guidance(self, *, max_chars: int = 40_000) -> str:
        """只在租约建立时读取 Guidance，避免把整个能力库塞进上下文。"""
        if not self.guidance:
            return ""
        if "\n" in self.guidance or self.guidance.lstrip().startswith("#"):
            return self.guidance[:max_chars]
        if not self.source or self.source.startswith("<"):
            return self.guidance[:max_chars]
        base = Path(self.source).resolve().parent
        path = (base / self.guidance).resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"Capability {self.id} guidance 越出声明目录") from exc
        if not path.is_file():
            raise ValueError(f"Capability {self.id} guidance 不存在: {path}")
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


class CapabilityCatalog:
    """能力索引：注册、目录发现、匹配与精确快照恢复。"""

    def __init__(self, capabilities: Iterable[Capability] = ()) -> None:
        self._items: dict[str, Capability] = {}
        for capability in capabilities:
            self.register(capability)

    @classmethod
    def from_registry(cls, registry: ToolRegistry) -> "CapabilityCatalog":
        catalog = cls()
        for spec in registry.specs():
            params = {
                name: {
                    "type": value.type,
                    "description": value.description,
                    "required": value.required,
                    "default": value.default,
                    "enum": value.enum,
                }
                for name, value in spec.params.items()
            }
            provides = [f"tool.{spec.name}", f"category.{spec.category}"]
            provides.extend(f"fact.{item}" for item in spec.produces)
            catalog.register(Capability.from_dict({
                "id": f"tool.{spec.name}",
                "version": __version__,
                "description": spec.summary,
                "provides": provides,
                "interface": {"name": spec.name, "params": params},
                "guidance": "",
                "runtime": {"type": "tool", "tool": spec.name},
                "policy": {
                    "action_level": spec.action_level,
                    "category": spec.category,
                    "destructive": spec.destructive,
                },
                "requires": {},
            }, source="<ToolRegistry>"))
        return catalog

    def register(self, capability: Capability) -> None:
        if capability.id in self._items:
            previous = self._items[capability.id]
            raise ValueError(f"Capability ID 重复: {capability.id}（{previous.source} / {capability.source}）")
        self._items[capability.id] = capability

    def discover(self, directories: Iterable[str | Path]) -> int:
        loaded = 0
        visited: set[Path] = set()
        for directory in directories:
            root = Path(directory).expanduser().resolve()
            if not root.is_dir():
                continue
            files = sorted([
                *root.rglob("*.yaml"), *root.rglob("*.yml"), *root.rglob("*.json"),
            ])
            for path in files:
                path = path.resolve()
                if path in visited:
                    continue
                visited.add(path)
                data = json.loads(path.read_text(encoding="utf-8")) if path.suffix.lower() == ".json" else yaml.safe_load(path.read_text(encoding="utf-8"))
                rows = data.get("capabilities") if isinstance(data, dict) and "capabilities" in data else [data]
                if not isinstance(rows, list):
                    raise ValueError(f"{path} 的 capabilities 必须是列表")
                for row in rows:
                    self.register(Capability.from_dict(row, source=str(path.resolve())))
                    loaded += 1
        return loaded

    def get(self, capability_id: str) -> Capability | None:
        return self._items.get(capability_id)

    def all(self) -> list[Capability]:
        return [self._items[key] for key in sorted(self._items)]

    def exact(self, snapshot: Iterable[dict[str, str]]) -> list[Capability]:
        result: list[Capability] = []
        for pin in snapshot:
            capability = self.get(str(pin.get("id") or ""))
            if capability is None:
                raise ValueError(f"快照能力不存在: {pin.get('id')}")
            if capability.version != pin.get("version") or capability.digest != pin.get("digest"):
                raise ValueError(f"能力快照漂移: {capability.id}")
            result.append(capability)
        return result

    def resolve(self, intent: str, constraints: dict[str, Any] | None = None) -> list[Capability]:
        """返回最小候选集；策略、依赖和权限过滤由 Broker 完成。"""
        constraints = dict(constraints or {})
        selectors = [str(item).strip() for item in constraints.get("selectors") or constraints.get("ids") or () if str(item).strip()]
        maximum = max(1, int(constraints.get("max_count") or 6))
        items = self.all()
        if selectors:
            selected: list[Capability] = []
            missing: list[str] = []
            for selector in selectors:
                matches = items if selector == "full" else [
                    item for item in items
                    if item.id == selector or selector in item.provides or f"category.{selector}" in item.provides
                ]
                if not matches:
                    missing.append(selector)
                for item in matches:
                    if item not in selected:
                        selected.append(item)
            if missing:
                raise ValueError(f"未发现所需能力: {missing}")
            return selected

        terms = _terms(intent)
        scored: list[tuple[float, Capability]] = []
        for item in items:
            haystack = " ".join([item.id, item.description, *item.provides])
            item_terms = _terms(haystack)
            overlap = terms & item_terms
            score = len(overlap) / max(1, len(terms))
            lowered = intent.casefold()
            if any(value.casefold() in lowered for value in item.provides):
                score += 1.0
            if str(item.policy.get("action_level")) == "detect":
                score += 0.02
            if overlap:
                scored.append((score, item))
        if scored:
            return [item for _score, item in sorted(scored, key=lambda pair: (-pair[0], pair[1].id))[:maximum]]

        # 无法从自然语言得到重叠时，只给少量信息产出高、低风险的启动能力。
        bootstrap = sorted(
            items,
            key=lambda item: (
                str(item.policy.get("action_level")) != "detect",
                -sum(1 for value in item.provides if value.startswith("fact.")),
                item.id,
            ),
        )
        return bootstrap[:min(maximum, 3)]


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Capability {name} 必须是对象")
    return dict(value)


def _terms(value: str) -> set[str]:
    text = (value or "").casefold()
    terms = set(re.findall(r"[a-z0-9_.+-]{2,}", text))
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.update(segment[index:index + 2] for index in range(len(segment) - 1))
    return terms


__all__ = ["Capability", "CapabilityCatalog"]
