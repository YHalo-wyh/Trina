"""本地对话持久化与独占使用；不保存供应商配置或执行旧工具。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class ChatStore:
    def __init__(self, root: Path, project: Path | None = None):
        self.root = Path(root).resolve()
        self.project = os.path.normcase(str((project or Path.cwd()).resolve()))
        self.path = self.root / "chats.sqlite3"

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            os.chmod(self.path, 0o600)
            db.execute("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL, updated REAL NOT NULL, turns INTEGER NOT NULL, payload TEXT NOT NULL)")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def validate_id(run_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ValueError("对话 ID 无效")

    def save(self, data: dict) -> None:
        self.validate_id(data["run_id"])
        if data["project"] != self.project:
            raise ValueError("对话不属于当前项目")
        title = " ".join(data.get("title", "未命名对话").split())[:80]
        with self.connection() as db:
            existing = db.execute("SELECT project FROM chats WHERE id=?", (data["run_id"],)).fetchone()
            if existing and existing[0] != self.project:
                raise ValueError("对话 ID 已被其他项目使用")
            db.execute("INSERT INTO chats VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,updated=excluded.updated,turns=excluded.turns,payload=excluded.payload",
                       (data["run_id"], self.project, title, time.time(), data["turns"], json.dumps(data, ensure_ascii=False)))

    def list(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.connection() as db:
            return [dict(zip(("id", "title", "updated", "turns"), row)) for row in db.execute(
                "SELECT id,title,updated,turns FROM chats WHERE project=? ORDER BY updated DESC,id", (self.project,))]

    def load(self, run_id: str) -> dict:
        self.validate_id(run_id)
        if not self.path.exists():
            raise ValueError("没有找到历史对话")
        with self.connection() as db:
            row = db.execute("SELECT payload FROM chats WHERE id=? AND project=?", (run_id, self.project)).fetchone()
        if row is None:
            raise ValueError("对话不存在或不属于当前项目")
        data = json.loads(row[0])
        if data.get("version") != 1 or data.get("run_id") != run_id or data.get("project") != self.project:
            raise ValueError("对话记录格式无效")
        for field in ("messages", "history"):
            messages = data.get(field)
            if not isinstance(messages, list) or (field == "messages" and len(messages) < 2) or any(
                not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant", "tool"}
                or not isinstance(item.get("content"), str) for item in messages
            ):
                raise ValueError("历史消息格式无效")
        if not isinstance(data.get("title"), str) or type(data.get("turns")) is not int or data["turns"] < 0:
            raise ValueError("对话元数据格式无效")
        if any(not isinstance(data.get(field, {}), dict) for field in ("state", "context", "usage", "usage_gaps", "controller")):
            raise ValueError("对话状态格式无效")
        return data

    def acquire(self, run_id: str) -> BinaryIO:
        """进程结束后系统自动释放锁；残留锁文件不会形成假占用。"""
        self.validate_id(run_id)
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / ".chat.lock"
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes as w
            create = ctypes.windll.kernel32.CreateFileW
            create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
            create.restype = w.HANDLE
            # 共享删除避免临时测试目录被锁住；文件字节锁仍由进程生命周期管理。
            handle = create(str(lock_path), 0xC0000000, 7, None, 4, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise OSError("无法打开对话锁")
            stream = os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY), "r+b")
            stream.seek(0, os.SEEK_END)
        else:
            stream = lock_path.open("a+b")
        try:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except OSError as exc:
            stream.close()
            raise ValueError("该对话正在另一个 Trina 实例中使用") from exc
