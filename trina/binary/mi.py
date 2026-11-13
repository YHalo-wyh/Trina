"""GDB/MI 值语法解析；保留指针和大整数的原始字符串。"""
from __future__ import annotations

import ast
import re

from .models import BinaryError


def c_string(text: str) -> str:
    value = ast.literal_eval(text)
    if re.search(r"\\[0-7]{3}", text):
        try:
            value = value.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return value


def parse_record(line: str) -> dict:
    match = re.match(r"^(\d*)([\^*+=~@&])(.*)$", line.strip())
    if not match:
        return {"kind": "output", "text": line}
    token, kind, tail = match.groups()
    if kind in "~@&":
        try:
            text = c_string(tail)
        except (SyntaxError, ValueError):
            text = tail
        return {"kind": kind, "text": text}
    name, _, rest = tail.partition(",")
    parser = _Values(rest)
    return {"kind": kind, "token": token, "class": name, "data": parser.results() if rest else {}}


class _Values:
    def __init__(self, text: str):
        self.text, self.i = text, 0

    def results(self, end: str = "") -> dict:
        result = {}
        while self.i < len(self.text) and (not end or self.text[self.i] != end):
            match = re.match(r"([\w-]+)=", self.text[self.i:])
            if not match:
                raise BinaryError("MI_PARSE", self.text[self.i:self.i + 80])
            self.i += len(match[0])
            key, value = match[1], self.value()
            if key in result:
                result[key] = [result[key], value]
            else:
                result[key] = value
            if self.i < len(self.text) and self.text[self.i] == ",":
                self.i += 1
            else:
                break
        return result

    def value(self):
        if self.i >= len(self.text):
            raise BinaryError("MI_PARSE", "值不完整")
        ch = self.text[self.i]
        if ch == '"':
            start = self.i
            self.i += 1
            while self.i < len(self.text):
                if self.text[self.i] == "\\":
                    self.i += 2
                elif self.text[self.i] == '"':
                    self.i += 1
                    return c_string(self.text[start:self.i])
                else:
                    self.i += 1
            raise BinaryError("MI_PARSE", "字符串未闭合")
        if ch == "{":
            self.i += 1
            result = self.results("}")
            self.end("}")
            return result
        if ch == "[":
            self.i += 1
            rows = []
            while self.i < len(self.text) and self.text[self.i] != "]":
                match = re.match(r"([\w-]+)=", self.text[self.i:])
                if match:
                    self.i += len(match[0])
                    rows.append({match[1]: self.value()})
                else:
                    rows.append(self.value())
                if self.i < len(self.text) and self.text[self.i] == ",":
                    self.i += 1
                else:
                    break
            self.end("]")
            return rows
        raise BinaryError("MI_PARSE", "非标准 MI 值")

    def end(self, ch: str) -> None:
        if self.i >= len(self.text) or self.text[self.i] != ch:
            raise BinaryError("MI_PARSE", "结构未闭合")
        self.i += 1
