"""终端内提醒与桌面通知协议格式化。"""
from __future__ import annotations

import os
import re
import sys
from typing import Callable, Mapping


_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _clean(value: str, limit: int = 240) -> str:
    return _UNSAFE.sub(" ", str(value)).strip()[:limit]


def notification_sequence(title: str, body: str = "", *, env: Mapping[str, str] | None = None) -> str:
    """按已知终端能力生成通知序列；未知终端回退为 BEL。"""
    values = env if env is not None else os.environ
    title, body = _clean(title, 80), _clean(body, 240)
    line = f"{title}: {body}" if title and body else title or body or "Trina"
    term = str(values.get("TERM", "")).lower()
    program = str(values.get("TERM_PROGRAM", values.get("LC_TERMINAL", ""))).lower()
    if "kitty" in term or values.get("KITTY_WINDOW_ID"):
        return f"\x1b]99;;{line}\x1b\\"
    if any(name in program for name in ("wezterm", "iterm", "ghostty", "warp")):
        return f"\x1b]9;{line}\x1b\\"
    return "\a"


def send_terminal_notification(title: str, body: str = "", *,
                               writer: Callable[[str], object] | None = None,
                               env: Mapping[str, str] | None = None) -> bool:
    """仅对真实 TTY 写通知控制序列；重定向输出保持纯净。"""
    stream = sys.stdout
    if writer is None and not bool(getattr(stream, "isatty", lambda: False)()):
        return False
    try:
        (writer or stream.write)(notification_sequence(title, body, env=env))
        if writer is None:
            stream.flush()
        return True
    except (OSError, ValueError):
        return False
