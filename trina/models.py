"""模型目录：把「可选模型」列出来，供 `/model` 之类的交互使用。

数据来源
--------
* **omp 后端**：读 omp 的 `models.db#model_cache`（omp 自己已经拉取并缓存了各 provider 的模型清单）；
  读不到就退回"常见模型"清单，绝不因为列不出模型就让交互失败。
* **OpenAI 兼容后端**：给内置预设（DeepSeek 官方等）。

只读、无网络：本模块只做本地读取与格式化，不发起任何请求。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

#: 内置预设：直连官方 API 时常用的 provider → (base_url, 模型 id)
OPENAI_PRESETS: dict[str, dict[str, object]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
        "api_key_env": "DEEPSEEK_API_KEY",
    },
}

#: 列不出 omp 模型时的兜底清单（避免命令空转）
FALLBACK_MODELS: list[tuple[str, str]] = [
    ("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash（本机 omp 默认）"),
    ("deepseek/deepseek-chat", "DeepSeek Chat"),
    ("openai/gpt-5.2", "GPT-5.2"),
    ("anthropic/claude-opus-4.8", "Claude Opus 4.8"),
    ("google/gemini-2.5-flash", "Gemini 2.5 Flash"),
]


@dataclass(frozen=True)
class ModelEntry:
    spec: str          # 形如 provider/model（omp 可直接用）
    name: str = ""
    provider: str = ""

    def render(self) -> str:
        label = f" — {self.name}" if self.name and self.name != self.spec else ""
        return f"{self.spec}{label}"


def omp_models_db() -> Path:
    """omp 的 models.db 位置（可用 TRINA_OMP_MODELS_DB 覆盖，便于测试）。"""
    override = os.environ.get("TRINA_OMP_MODELS_DB")
    if override:
        return Path(override)
    config_dir = os.environ.get("PI_CONFIG_DIR")
    root = Path(config_dir) if config_dir else Path.home() / ".omp"
    return root / "agent" / "models.db"


def list_models(*, limit: int = 60, provider_filter: str = "") -> list[ModelEntry]:
    """列出本机可用的模型（优先 omp 缓存，其次内置清单）。"""
    entries: list[ModelEntry] = []
    db = omp_models_db()
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = list(con.execute("select provider_id, models from model_cache"))
            finally:
                con.close()
            for provider_id, raw in rows:
                try:
                    models = json.loads(raw or "[]")
                except (json.JSONDecodeError, TypeError):
                    continue
                for item in models if isinstance(models, list) else []:
                    if not isinstance(item, dict):
                        continue
                    model_id = str(item.get("id") or "").strip()
                    provider = str(item.get("provider") or str(provider_id).split(":")[0]).strip()
                    if not model_id or not provider:
                        continue
                    # 跳过明显不是对话模型的条目
                    if any(tag in model_id.lower() for tag in ("embed", "whisper", "tts", "rerank", "moderation")):
                        continue
                    entries.append(
                        ModelEntry(spec=f"{provider}/{model_id}", name=str(item.get("name") or ""), provider=provider)
                    )
        except sqlite3.Error:
            entries = []
    if not entries:
        entries = [ModelEntry(spec=spec, name=name, provider=spec.split("/", 1)[0]) for spec, name in FALLBACK_MODELS]
    if provider_filter:
        entries = [e for e in entries if e.provider == provider_filter]
    seen: set[str] = set()
    unique: list[ModelEntry] = []
    for entry in sorted(entries, key=lambda e: (e.provider, e.spec)):
        if entry.spec in seen:
            continue
        seen.add(entry.spec)
        unique.append(entry)
    return unique[:limit]


def normalize_spec(spec: str, backend: str) -> str:
    """把用户输入的模型规格规整成当前后端能用的形式。

    * `omp`：保留 `provider/model` 原样（omp 自己会解析）。
    * `openai`：去掉 provider 前缀（绝大多数 OpenAI 兼容端点只认裸模型名）。
    """
    text = (spec or "").strip()
    if not text:
        return text
    if backend == "openai" and "/" in text:
        return text.split("/", 1)[1]
    return text


def preset_for(name: str) -> dict[str, object] | None:
    return OPENAI_PRESETS.get((name or "").strip().lower())


__all__ = [
    "FALLBACK_MODELS",
    "ModelEntry",
    "OPENAI_PRESETS",
    "list_models",
    "normalize_spec",
    "omp_models_db",
    "preset_for",
]
