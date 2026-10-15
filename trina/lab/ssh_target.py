"""故意脆弱的 SSH 靶机 —— 仅监听 127.0.0.1，供弱口令验证 / 凭据利用的端到端自证。

实现要点:
* 用 ``paramiko.ServerInterface`` + ``Transport`` 手搓一个假 SSH 服务；
* 只接受用户名 ``trina`` / 口令 ``trina123``，其余一律拒绝，并把失败次数暴露给评测
  （``server.auth_failures`` / ``server.auth_successes``）；
* 认证成功后提供只读命令回显（``id`` / ``whoami`` / ``ls`` / ``cat /flag`` 等），
  复用 http 靶机的「模拟 shell」——**不真正执行任何系统命令**；
* 只绑定回环地址。

CLI: ``python3 -m trina.lab.run --ssh --port 2222``
"""

from __future__ import annotations

import random
import socket
import threading
import time
from typing import Any

import paramiko

from .http_target import run_command

DEFAULT_USERNAME = "trina"
DEFAULT_PASSWORD = "trina123"


class _ExecState:
    """给 ``run_command`` 用的最小状态：只需要 ``flag`` 字段。"""

    def __init__(self, flag: str):
        self.flag = flag


def _make_host_key() -> paramiko.PKey:
    """生成一次性主机密钥（优先 RSA，退化到 ECDSA）。"""
    try:
        return paramiko.RSAKey.generate(2048)
    except Exception:  # noqa: BLE001 - 某些精简环境无 RSA 支持
        return paramiko.ECDSAKey.generate()


class LabServerInterface(paramiko.ServerInterface):
    """认证 + 通道请求策略；把认证计数记到 ``server`` 上。"""

    def __init__(self, server: "SSHLabServer"):
        self.server = server
        self.username = ""

    # -------------------------------------------------------------- 认证
    def get_banner(self) -> tuple[str, str]:  # noqa: D102 - paramiko 要求 (banner, language)
        return ("Trina lab SSH (仅授权实训使用)\r\n", "en")

    def check_auth_password(self, username: str, password: str) -> int:  # noqa: D102
        self.username = username
        if username == self.server.username and password == self.server.password:
            with self.server.lock:
                self.server.auth_successes += 1
            return paramiko.AUTH_SUCCESSFUL
        with self.server.lock:
            self.server.auth_failures += 1
            self.server.username_attempts[username] = self.server.username_attempts.get(username, 0) + 1
        return paramiko.AUTH_FAILED

    def check_auth_none(self, username: str) -> int:  # noqa: D102
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:  # noqa: D102
        return "password"

    # -------------------------------------------------------------- 通道
    def check_channel_request(self, kind: str, chanid: int) -> int:  # noqa: D102
        if kind == "session":
            with self.server.lock:
                self.server.channels_opened += 1
            return paramiko.OPEN_SUCCEEDED

        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_shell_request(self, channel: paramiko.Channel) -> bool:  # noqa: D102
        setattr(channel, "_lab_shell", True)
        return True

    def check_channel_pty_request(
        self, channel: paramiko.Channel, term: bytes, width: int, height: int, pixelwidth: int, pixelheight: int, modes: bytes
    ) -> bool:  # noqa: D102
        return True

    def check_channel_exec_request(self, channel: paramiko.Channel, command: bytes) -> bool:  # noqa: D102
        setattr(channel, "_lab_exec", command.decode("utf-8", "replace"))
        return True

    def check_channel_subsystem_request(self, channel: paramiko.Channel, name: str) -> bool:  # noqa: D102
        return name == "sftp"


class SSHLabServer:
    """假 SSH 服务：accept 循环 + 每连接一个 Transport。

    ``create_ssh_server`` 返回本对象；它内部持有所有 paramiko ``Transport``。
    """

    def __init__(
        self,
        port: int = 2222,
        host: str = "127.0.0.1",
        username: str = DEFAULT_USERNAME,
        password: str = DEFAULT_PASSWORD,
        flag: str | None = None,
        verbose: bool = False,
    ):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.flag = flag or f"flag{{trina_lab_{random.randbytes(4).hex()}}}"
        self.state = _ExecState(self.flag)
        self.verbose = verbose

        self.lock = threading.Lock()
        self.auth_failures = 0
        self.auth_successes = 0
        self.username_attempts: dict[str, int] = {}
        self.channels_opened = 0
        self.commands: list[str] = []
        self.errors: list[str] = []

        self._sock: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._transports: list[paramiko.Transport] = []
        self._host_key = _make_host_key()

    # -------------------------------------------------------------- 生命周期
    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(16)
        sock.settimeout(0.5)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, name=f"trina-lab-ssh-{self.port}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        for transport in list(self._transports):
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    # -------------------------------------------------------------- accept
    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(conn, addr), daemon=True).start()

    def _record_error(self, message: str) -> None:
        with self.lock:
            self.errors.append(message)
            if len(self.errors) > 50:
                self.errors = self.errors[-25:]
        if self.verbose:
            print(f"[trina.lab.ssh] {message}")

    def _serve_conn(self, conn: socket.socket, addr: Any) -> None:
        transport: paramiko.Transport | None = None
        try:
            # accept 出来的 socket 可能继承了监听 socket 的超时，SSH 握手必须阻塞进行。
            conn.settimeout(None)
            transport = paramiko.Transport(conn)
            transport.add_server_key(self._host_key)
            transport.set_keepalive(30)
            with self.lock:
                self._transports.append(transport)
            interface = LabServerInterface(self)
            try:
                transport.start_server(server=interface)
            except paramiko.SSHException as exc:
                self._record_error(f"start_server 失败: {exc}")
                return
            # 一个连接上可开多个 session 通道（exec / shell 混用），逐个起线程处理。
            while self._running and transport.is_active():
                chan = transport.accept(1.0)
                if chan is None:
                    continue
                threading.Thread(
                    target=self._handle_channel, args=(chan,), daemon=True
                ).start()
        except Exception as exc:  # noqa: BLE001 - 单连接故障不应影响服务
            self._record_error(f"{type(exc).__name__}: {exc}")
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:  # noqa: BLE001
                    pass

    def _handle_channel(self, chan: paramiko.Channel) -> None:
        # 等 shell / exec 请求到达
        for _ in range(100):
            if getattr(chan, "_lab_exec", None) is not None or getattr(chan, "_lab_shell", False):
                break
            time.sleep(0.02)

        exec_cmd = getattr(chan, "_lab_exec", None)
        if exec_cmd is not None:
            with self.lock:
                self.commands.append(exec_cmd)
            output = run_command(self.state, exec_cmd)  # type: ignore[arg-type]
            chan.sendall(output.encode("utf-8", "replace"))
            chan.send_exit_status(0)
            chan.close()
            return

        # 交互式 shell：简单回显
        chan.sendall(b"Trina lab shell (read-only)\r\n$ ")
        buffer = b""
        while True:
            try:
                data = chan.recv(4096)
            except Exception:  # noqa: BLE001
                break
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                command = line.decode("utf-8", "replace").strip()
                if not command:
                    chan.sendall(b"$ ")
                    continue
                with self.lock:
                    self.commands.append(command)
                if command in ("exit", "logout", "quit"):
                    chan.sendall(b"bye\r\n")
                    chan.close()
                    return
                output = run_command(self.state, command)  # type: ignore[arg-type]
                chan.sendall(output.encode("utf-8", "replace") + b"$ ")


def create_ssh_server(
    port: int = 2222,
    host: str = "127.0.0.1",
    *,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    flag: str | None = None,
    verbose: bool = False,
) -> tuple[SSHLabServer, threading.Thread]:
    """启动假 SSH 服务，返回 ``(server, thread)``；``server.stop()`` 停止。

    第一个返回值 ``SSHLabServer`` 持有所有 paramiko ``Transport``（``server._transports``），
    等价于「transport 服务端」。
    """
    server = SSHLabServer(port=port, host=host, username=username, password=password, flag=flag, verbose=verbose)
    server.start()
    assert server.thread is not None
    return server, server.thread


__all__ = [
    "DEFAULT_PASSWORD",
    "DEFAULT_USERNAME",
    "LabServerInterface",
    "SSHLabServer",
    "create_ssh_server",
]
