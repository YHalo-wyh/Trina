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
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Iterator

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
    "input_value",
    "storage_value",
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


def _read_lines(path: str | Path) -> Iterator[str]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line := line.strip():
                yield line


def _parse_record(line: str) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        record = None
    return record if isinstance(record, dict) else {"event": "corrupt", "raw": line[:200]}


@dataclass
class _ChainCheck:
    """保留首个校验错误和已读取的记录数。"""

    count: int = 0
    previous: str = GENESIS_HASH
    error: str = ""

    def accept(self, record: dict[str, Any]) -> None:
        self.count += 1
        if self.error:
            return
        if record.get("event") == "corrupt":
            self.error = f"第 {self.count} 行不是合法 JSON 对象"
        elif record.get("seq") != self.count:
            self.error = f"seq 断裂：期望 {self.count}，实际 {record.get('seq')}"
        elif record.get("prev_hash") != self.previous:
            self.error = f"第 {self.count} 条 prev_hash 不匹配（记录被插入或删除）"
        else:
            digest = sha256_text(_canonical({k: v for k, v in record.items() if k != "hash"}))
            if digest != record.get("hash"):
                self.error = f"第 {self.count} 条内容被篡改（hash 不匹配）"
            else:
                self.previous = digest

    def result(self) -> tuple[bool, str]:
        if self.error:
            return False, self.error
        if not self.count:
            return True, "审计文件为空"
        return True, f"哈希链完整：{self.count} 条记录，末端 {self.previous[:12]}…"


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
        for line in _read_lines(path):
            yield _parse_record(line)

    def read_all(self) -> list[dict[str, Any]]:
        return list(self.iter_records(self.path))

    def verify(self) -> tuple[bool, str]:
        """校验哈希链；返回 (是否完整, 说明)。"""
        chain = _ChainCheck()
        for record in self.iter_records(self.path):
            chain.accept(record)
            if chain.error:
                break
        return chain.result()

    def tail(self, limit: int) -> Iterator[dict[str, Any]]:
        """只解析需要输出的记录；负数表示跳过开头若干条。"""
        lines = _read_lines(self.path)
        if limit < 0:
            selected: Iterable[str] = islice(lines, min(-limit, sys.maxsize), None)
        elif limit:
            selected = deque(lines, maxlen=min(limit, sys.maxsize))
        else:
            return
        for line in selected:
            yield _parse_record(line)

    def inspect(self, limit: int = 0) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """单次遍历生成校验结果、摘要和有限的最近记录。"""
        chain = _ChainCheck()
        recent: deque[dict[str, Any]] = deque(maxlen=min(max(limit, 0), sys.maxsize))

        def observe() -> Iterator[dict[str, Any]]:
            for record in self.iter_records(self.path):
                chain.accept(record)
                recent.append(record)
                yield record

        summary = self._summarize(observe())
        summary["records"] = chain.count
        summary["chain_ok"] = not chain.error
        return summary, list(recent)

    # ------------------------------------------------------------------ 统计
    def summary(self) -> dict[str, Any]:
        return self._summarize(self.iter_records(self.path))

    def _summarize(self, records: Iterable[dict[str, Any]]) -> dict[str, Any]:
        events: dict[str, int] = {}
        tools: dict[str, int] = {}
        denied = 0
        first_ts = last_ts = None
        for record in records:
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
