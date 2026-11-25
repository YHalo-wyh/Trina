"""面向用户的子进程输出解码，保留管道分片之间的待解码字节。"""
from __future__ import annotations

import codecs
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def _windows_legacy_encoding() -> str | None:
    """读取 Windows 系统 ANSI 代码页，独立于 Python UTF-8 模式。"""
    if sys.platform != "win32":
        return None
    import ctypes

    code_page = ctypes.windll.kernel32.GetACP()
    if code_page in (0, 65001):
        return None
    try:
        return codecs.lookup(f"cp{code_page}").name
    except LookupError:
        return None


class ProcessOutputDecoder:
    """每个 stdout/stderr 流优先使用 UTF-8，遇到无效字节时尝试本地编码。"""

    def __init__(self) -> None:
        self._fallback = _windows_legacy_encoding()
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict" if self._fallback else "replace")

    def decode(self, chunk: bytes, *, final: bool = False) -> str:
        if self._fallback is None:
            return self._decoder.decode(chunk, final)
        pending = self._decoder.getstate()[0]
        try:
            return self._decoder.decode(chunk, final)
        except UnicodeDecodeError:
            self._decoder = codecs.getincrementaldecoder(self._fallback)("replace")
            self._fallback = None
            return self._decoder.decode(pending + chunk, final)
