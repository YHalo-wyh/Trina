"""Trina 内置的故意脆弱靶机（仅监听 127.0.0.1，用于端到端自证与评测）。

* ``trina.lab.http_target`` —— stdlib ``http.server`` 实现的 Web 靶机（SQLi/LFI/RCE/上传/弱口令/软 404 …）
* ``trina.lab.ssh_target``  —— paramiko 实现的假 SSH 靶机（弱口令 trina/trina123 + 只读命令回显）
* ``trina.lab.run``         —— 命令行启动器（``python3 -m trina.lab.run --http``）

所有靶机都**不真正执行**危险操作（不读 /etc/shadow、不写盘、不 spawn 子进程）。
"""

from __future__ import annotations

from .http_target import (
    LabHTTPHandler,
    LabHTTPServer,
    LabState,
    create_server,
)

__all__ = [
    "LabHTTPHandler",
    "LabHTTPServer",
    "LabState",
    "create_server",
    "LabServerInterface",
    "SSHLabServer",
    "create_ssh_server",
]


def __getattr__(name: str):  # noqa: ANN401 - 懒加载 paramiko，避免无 paramiko 环境下 import 失败
    if name in ("SSHLabServer", "create_ssh_server", "LabServerInterface"):
        from . import ssh_target

        return getattr(ssh_target, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
