"""任务隔离的 HTTP 留痕；正文保留于本地 SQLite，提示词只按需读取。"""
from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .scope import ensure_execution_active

_capture: contextvars.ContextVar[Any] = contextvars.ContextVar("trina_traffic", default=None)
BODY_LIMIT = 200_000


class TrafficStore:
    """每个 ToolContext 使用自己的数据库；按执行调用索引，禁止任意路径读取。"""

    def __init__(self, workspace: Path):
        self.path = Path(workspace) / "traffic.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS traffic (id TEXT PRIMARY KEY, ts REAL, call_id TEXT, url TEXT, method TEXT, status INTEGER, payload TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS traffic_call ON traffic(call_id)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def record(self, result: Any, call_id: str) -> str:
        ensure_execution_active()
        tid = "http-" + uuid.uuid4().hex
        body = result.body or ""
        request_body = result.request_body or ""
        payload = {
            "id": tid, "ts": time.time(), "call_id": call_id,
            "url": result.url, "method": result.method, "status": result.status,
            "error": result.error, "elapsed": result.elapsed,
            "request": {"headers": result.request_headers, "body": request_body[:BODY_LIMIT]},
            "response": {"headers": result.headers, "body": body[:BODY_LIMIT]},
            "truncated": bool(getattr(result, "body_truncated", False) or len(body) > BODY_LIMIT or len(request_body) > BODY_LIMIT),
            "sha256": hashlib.sha256(body[:BODY_LIMIT].encode("utf-8")).hexdigest(),
        }
        with self.connect() as db:
            db.execute("INSERT INTO traffic VALUES (?,?,?,?,?,?,?)", (
                tid, payload["ts"], call_id, result.url, result.method, result.status,
                json.dumps(payload, ensure_ascii=False),
            ))
            ensure_execution_active()
        return tid

    def get(self, tid: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM traffic WHERE id=?", (tid,)).fetchone()
        return json.loads(row[0]) if row else None

    def search(self, query: str = "", *, method: str = "", status: int = 0, limit: int = 20, offset: int = 0, call_id: str = "") -> list[dict[str, Any]]:
        # instr 是字面量检索，不把模型输入当正则或 SQL。
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM traffic WHERE instr(lower(payload),lower(?))>0 AND (?='' OR method=?) AND (?=0 OR status=?) AND (?='' OR call_id=?) ORDER BY ts DESC LIMIT ? OFFSET ?",
                (query, method, method, status, status, call_id, call_id, min(100, max(1, limit)), max(0, offset)),
            ).fetchall()
        return [self.summary(json.loads(row[0])) for row in rows]

    @staticmethod
    def summary(item: dict[str, Any]) -> dict[str, Any]:
        return {key: item[key] for key in ("id", "ts", "call_id", "url", "method", "status", "error", "truncated", "sha256")}


@contextmanager
def traffic_window(store: TrafficStore, call_id: str):
    token = _capture.set((store, call_id))
    try:
        yield
    finally:
        _capture.reset(token)


def capture_http(result: Any) -> Any:
    """只归档当前调用实际发出的交互，不修改出网路径或裁决。"""
    active = _capture.get()
    if active is not None:
        result.traffic_id = active[0].record(result, active[1])
    return result
