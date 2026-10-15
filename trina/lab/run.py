"""本地脆弱靶机启动器。

用法::

    python3 -m trina.lab.run --http --port 9099          # 只起 HTTP 靶机
    python3 -m trina.lab.run --ssh --port 2222           # 只起 SSH 靶机（--port 作为 SSH 端口）
    python3 -m trina.lab.run --http --ssh                # 两个都起（HTTP 9099 / SSH 2222）

不带 ``--http``/``--ssh`` 时默认只起 HTTP 靶机。Ctrl+C 优雅退出。
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

DEFAULT_HTTP_PORT = 9099
DEFAULT_SSH_PORT = 2222


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trina.lab.run",
        description="启动 Trina 内置的脆弱靶机（仅 127.0.0.1）",
    )
    parser.add_argument("--http", action="store_true", help="启动 HTTP 靶机")
    parser.add_argument("--ssh", action="store_true", help="启动 SSH 靶机")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"HTTP 端口（默认 {DEFAULT_HTTP_PORT}）；只起 --ssh 时作为 SSH 端口",
    )
    parser.add_argument("--ssh-port", type=int, default=None, help=f"SSH 端口（默认 {DEFAULT_SSH_PORT}）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1，勿改）")
    parser.add_argument("--flag", default=None, help="固定 flag（默认随机）")
    return parser


def _resolve_ports(args: argparse.Namespace) -> tuple[int, int]:
    http_port = args.port if args.port is not None else DEFAULT_HTTP_PORT
    if args.ssh_port is not None:
        ssh_port = args.ssh_port
    elif args.port is not None and not args.http:
        ssh_port = args.port  # 只起 SSH 时 --port 指 SSH
    else:
        ssh_port = DEFAULT_SSH_PORT
    return http_port, ssh_port


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    start_http = args.http or not args.ssh
    start_ssh = args.ssh
    http_port, ssh_port = _resolve_ports(args)

    servers: list = []
    if start_http:
        from .http_target import create_server

        server, _thread = create_server(port=http_port, host=args.host, flag=args.flag)
        servers.append(("http", server))
        print(f"[Trina lab] HTTP 靶机已启动: http://{args.host}:{http_port}/")
        print("  脆弱点: /sqli?id=1  /lfi?file=../../../../etc/passwd  /rce?cmd=id  POST /upload  POST /login  /flag  /admin")

    if start_ssh:
        from .ssh_target import create_ssh_server

        ssh_server, _sthread = create_ssh_server(port=ssh_port, host=args.host, flag=args.flag)
        servers.append(("ssh", ssh_server))
        print(f"[Trina lab] SSH 靶机已启动: ssh trina@127.0.0.1 -p {ssh_port}  (口令 trina123)")
        print(f"  假 flag: {ssh_server.flag}")

    if not servers:
        print("[Trina lab] 未启动任何服务", file=sys.stderr)
        return 2

    print("[Trina lab] 按 Ctrl+C 退出。")

    stopping = {"now": False}

    def _handle_signal(_signum: int, _frame: object) -> None:
        stopping["now"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass  # 非主线程 / 平台限制

    try:
        while not stopping["now"]:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[Trina lab] 正在关闭…")
        for kind, server in servers:
            try:
                if kind == "http":
                    server.shutdown()
                    server.server_close()
                else:
                    server.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[Trina lab] 关闭 {kind} 失败: {exc}", file=sys.stderr)
        print("[Trina lab] 已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
