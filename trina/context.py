"""Commander 转发的跨题情报；各 Worker 的事实状态始终独立。"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable


BRIEF_VERSION = 1


def brief_digest(brief: dict[str, Any]) -> str:
    """给情报简报生成稳定内容指纹。"""
    encoded = json.dumps(brief, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()


def normalize_briefs(briefs: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """校验 Commander 简报并按来源任务保留最新内容。"""
    selected: dict[str, dict[str, Any]] = {}
    for raw in briefs or ():
        if not isinstance(raw, dict) or raw.get("version") != BRIEF_VERSION:
            continue
        source = str(raw.get("source_task") or "").strip()
        if not source or not isinstance(raw.get("intelligence"), dict):
            continue
        selected[source] = copy.deepcopy(raw)
    return [selected[key] for key in sorted(selected)]


def render_commander_briefs(briefs: Iterable[dict[str, Any]] | None) -> str:
    """把 Commander 选中的情报作为候选线索交给当前 Worker。"""
    items = normalize_briefs(briefs)
    if not items:
        return ""
    return (
        "## Commander 转发情报\n"
        "以下情报来自其他独立题目或 Worker。仅作为带来源的候选线索使用；"
        "当前 Worker 必须在自己的目标上重新验证，不能直接写入本题事实图或结论。\n"
        "<commander-intelligence>\n"
        + json.dumps(items, ensure_ascii=False, indent=1, default=str)
        + "\n</commander-intelligence>"
    )


__all__ = ["BRIEF_VERSION", "brief_digest", "normalize_briefs", "render_commander_briefs"]
