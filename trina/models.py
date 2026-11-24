"""供应商模型目录；只读本项目预设，不依赖外部 Agent 的本地数据库。"""

from __future__ import annotations

from dataclasses import dataclass

from .providers import PRESETS


OPENAI_PRESETS: dict[str, dict[str, object]] = {
    item.id: {
        "base_url": item.base_url,
        "models": list(item.models),
        "api_key_env": item.api_key_env,
        "api": item.api,
    }
    for item in PRESETS
}


@dataclass(frozen=True)
class CatalogModel:
    kind: str = "chat"
    context: int = 0
    output: int = 0
    vision: bool = False

    @classmethod
    def from_row(cls, row: dict) -> CatalogModel:
        model = row["id"].casefold()
        kind = row.get("type")
        if not isinstance(kind, str) or kind in ("", "model"):
            kind = next((kind for token, kind in (("embed", "embedding"), ("whisper", "stt"),
                                                  ("tts", "tts"), ("rerank", "rerank")) if token in model), "chat")
        limits = row.get("limit") if isinstance(row.get("limit"), dict) else {}
        modalities = row.get("modalities") if isinstance(row.get("modalities"), dict) else {}
        inputs = modalities.get("input")
        ctx = limits.get("context", row.get("context_length"))
        out = limits.get("output")
        return cls(kind if kind.isascii() and kind.isalnum() else "other",
                   ctx if type(ctx) is int and ctx > 0 else 0, out if type(out) is int and out > 0 else 0,
                   isinstance(inputs, list) and any(isinstance(v, str) and v.startswith("image") for v in inputs))


@dataclass(frozen=True)
class ModelEntry:
    spec: str
    name: str = ""
    provider: str = ""

    def render(self) -> str:
        label = f" — {self.name}" if self.name and self.name != self.spec else ""
        return f"{self.spec}{label}"


def list_models(*, limit: int = 60, provider_filter: str = "") -> list[ModelEntry]:
    """列出内置供应商的已知模型；远端动态目录由模型页显式刷新。"""
    entries = [
        ModelEntry(spec=f"{provider.id}/{model}", name=model, provider=provider.id)
        for provider in PRESETS
        if not provider_filter or provider.id == provider_filter
        for model in provider.models
    ]
    return entries[:limit]


def normalize_spec(spec: str, backend: str = "openai", *, provider: str = "") -> str:
    """当前供应商前缀可省略；第三方模型自身的命名空间保持原样。"""
    del backend
    text = (spec or "").strip()
    if provider and text.startswith(provider + "/"):
        suffix = text.split("/", 1)[1]
        if suffix in OPENAI_PRESETS.get(provider, {}).get("models", []):
            return suffix
    return text


def preset_for(name: str) -> dict[str, object] | None:
    return OPENAI_PRESETS.get((name or "").strip().lower())


__all__ = ["ModelEntry", "OPENAI_PRESETS", "list_models", "normalize_spec", "preset_for"]
