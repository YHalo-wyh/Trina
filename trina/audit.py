"""审计日志：JSONL + SHA-256 哈希链，全程留痕且可校验完整性。

每条记录形如::

    {"seq": 12, "ts": 1750000000.123, "run_id": "…", "event": "tool.allow",
     "tool": "port_scan", "target": "127.0.0.1", "decision": "allow",
     "params_digest": "…", "output_digest": "…", "prev_hash": "…", "hash": "…"}

`hash` = sha256(规范化 JSON(记录去掉 hash 字段))，`prev_hash` 指向上一条的 hash，
首条 `prev_hash` 为 64 个 0。任何中间篡改都会让 `verify()` 从该条起失败。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64
DEFAULT_REDACT_KEYS = [
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "cookie",
    "authorization",
    "pass",
]


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def redact(data: Any, keys: list[str] | None = None, _depth: int = 0) -> Any:
    """递归脱敏：字段名命中敏感词 → 替换为 `<redacted:sha256前8位>`。"""
    keys = [k.lower() for k in (keys or DEFAULT_REDACT_KEYS)]
    if _depth > 6:
        return "<max-depth>"
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if isinstance(k, str) and k.lower() in keys:
                out[k] = f"<redacted:{sha256_text(str(v))[:8]}>"
            else:
                out[k] = redact(v, keys, _depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [redact(item, keys, _depth + 1) for item in data]
    return data


@dataclass
class AuditLog:
    """一条 run 对应一个审计文件：`<dir>/<run_id>.jsonl`。"""

    path: Path
    run_id: str
    actor: str = "trina"
    redact_keys: list[str] = field(default_factory=lambda: list(DEFAULT_REDACT_KEYS))
    record_output_digest: bool = True
    _prev_hash: str = GENESIS_HASH
    _seq: int = 0
    _records: int = 0

    @classmethod
    def open(
        cls,
        directory: str | Path,
        run_id: str | None = None,
        *,
        actor: str = "trina",
        redact_keys: list[str] | None = None,
        record_output_digest: bool = True,
        resume: bool = True,
    ) -> "AuditLog":
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        run_id = run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        path = directory / f"{run_id}.jsonl"
        log = cls(
            path=path,
            run_id=run_id,
            actor=actor,
            redact_keys=redact_keys or list(DEFAULT_REDACT_KEYS),
            record_output_digest=record_output_digest,
        )
        if resume and path.exists():
            ok, msg = log.verify()
            if not ok:
                raise RuntimeError(f"审计文件哈希链损坏，拒绝续写 {path}: {msg}")
            for record in cls.iter_records(path):
                log._seq = int(record.get("seq", log._seq))
                log._prev_hash = record.get("hash", log._prev_hash)
            log._records = log._seq
        return log

    # ------------------------------------------------------------------ 写入
    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        record: dict[str, Any] = {
            "seq": self._seq,
            "ts": round(time.time(), 3),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_id": self.run_id,
            "actor": self.actor,
            "event": event,
        }
        for key, value in redact(fields, self.redact_keys).items():
            record[key] = value
        record["prev_hash"] = self._prev_hash
        digest = sha256_text(_canonical({k: v for k, v in record.items() if k != "hash"}))
        record["hash"] = digest
        self._prev_hash = digest
        self._records += 1
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    # ------------------------------------------------------------------ 读取
    @staticmethod
    def iter_records(path: str | Path) -> Iterator[dict[str, Any]]:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"event": "corrupt", "raw": line[:200]}

    def read_all(self) -> list[dict[str, Any]]:
        return list(self.iter_records(self.path))

    def verify(self) -> tuple[bool, str]:
        """校验哈希链；返回 (是否完整, 说明)。"""
        prev = GENESIS_HASH
        expected_seq = 0
        count = 0
        for record in self.iter_records(self.path):
            if record.get("event") == "corrupt":
                return False, f"第 {count + 1} 行不是合法 JSON"
            count += 1
            expected_seq += 1
            if record.get("seq") != expected_seq:
                return False, f"seq 断裂：期望 {expected_seq}，实际 {record.get('seq')}"
            if record.get("prev_hash") != prev:
                return False, f"第 {expected_seq} 条 prev_hash 不匹配（记录被插入或删除）"
            digest = sha256_text(_canonical({k: v for k, v in record.items() if k != "hash"}))
            if digest != record.get("hash"):
                return False, f"第 {expected_seq} 条内容被篡改（hash 不匹配）"
            prev = digest
        if count == 0:
            return True, "审计文件为空"
        return True, f"哈希链完整：{count} 条记录，末端 {prev[:12]}…"

    # ------------------------------------------------------------------ 统计
    def summary(self) -> dict[str, Any]:
        events: dict[str, int] = {}
        tools: dict[str, int] = {}
        denied = 0
        first_ts = last_ts = None
        for record in self.iter_records(self.path):
            name = str(record.get("event", "?"))
            events[name] = events.get(name, 0) + 1
            if tool := record.get("tool"):
                tools[str(tool)] = tools.get(str(tool), 0) + 1
            if record.get("decision") in ("deny", "denied"):
                denied += 1
            ts = record.get("ts")
            if isinstance(ts, (int, float)):
                first_ts = ts if first_ts is None else first_ts
                last_ts = ts
        return {
            "run_id": self.run_id,
            "records": self._records or sum(events.values()),
            "events": events,
            "tools": tools,
            "denied": denied,
            "started_at": first_ts,
            "ended_at": last_ts,
            "path": str(self.path),
        }
