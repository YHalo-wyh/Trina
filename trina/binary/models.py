"""二进制地址、ELF 装载信息与稳定错误。"""
from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path


ELF_MACHINES = {3: "x86", 8: "mips", 20: "ppc", 21: "ppc64", 40: "arm", 62: "x86_64", 183: "aarch64", 243: "riscv"}


class BinaryError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def address(raw: str | int) -> int:
    text = str(raw)
    if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", text):
        raise BinaryError("BAD_ADDRESS", "地址必须为不带表达式的非负整数")
    number = int(text, 16 if text.lower().startswith("0x") else 10)
    if number >= 1 << 64:
        raise BinaryError("BAD_ADDRESS", "地址超出 64 位")
    return number


@dataclass(frozen=True)
class AddressRef:
    """显式区分文件偏移、静态 VA 与运行 VA，禁止调用方靠字段名猜地址空间。"""

    file_offset: int | None = None
    static_va: int | None = None
    runtime_va: int | None = None
    module: str = ""
    load_bias: int | None = None
    arch: str = ""
    pointer_bits: int = 0

    def to_dict(self) -> dict:
        data = {
            key: hex(value) if value is not None else None
            for key, value in {
                "file_offset": self.file_offset,
                "static_va": self.static_va,
                "runtime_va": self.runtime_va,
                "load_bias": self.load_bias,
            }.items()
        }
        data.update(module=self.module, arch=self.arch, pointer_bits=self.pointer_bits)
        return data


@dataclass(frozen=True)
class BinaryArtifact:
    path: Path
    sha256: str
    elf: dict

    @classmethod
    def read(cls, path: Path, max_bytes: int) -> BinaryArtifact:
        if not path.is_file():
            raise BinaryError("FILE_NOT_FOUND", str(path))
        if path.stat().st_size > max_bytes:
            raise BinaryError("FILE_LIMIT", "文件超过 binary.max_file_bytes")
        data = path.read_bytes()
        info = {}
        if data[:4] == b"\x7fELF":
            try:
                bits, endian = {1: 32, 2: 64}[data[4]], {1: "<", 2: ">"}[data[5]]
                kind, machine = struct.unpack_from(endian + "HH", data, 16)
                entry, phoff = struct.unpack_from(endian + ("QQ" if bits == 64 else "II"), data, 24)
                phsize, phnum = struct.unpack_from(endian + "HH", data, 54 if bits == 64 else 42)
                segments = []
                for index in range(phnum):
                    row = struct.unpack_from(endian + ("IIQQQQQQ" if bits == 64 else "IIIIIIII"), data, phoff + index * phsize)
                    if row[0] == 1:
                        offset, va, filesz, memsz = ((row[2], row[3], row[5], row[6]) if bits == 64
                                                       else (row[1], row[2], row[4], row[5]))
                        segments.append({"offset": offset, "vaddr": va, "filesz": filesz, "memsz": memsz})
                info = {"file_format": "ELF", "bits": bits, "endian": "little" if endian == "<" else "big",
                        "entry": hex(entry), "pie": kind == 3, "machine": machine, "segments": segments}
            except (KeyError, IndexError, struct.error) as exc:
                raise BinaryError("BAD_ELF", "ELF 头或段表损坏") from exc
        return cls(path, hashlib.sha256(data).hexdigest(), info)

    def address_ref(self, *, static_va: int | None = None, runtime_va: int | None = None,
                    load_bias: int | None = None, module: str = "") -> AddressRef:
        """根据已证实的装载偏移转换地址；未知偏移时不臆造运行地址。"""
        if load_bias is None and not self.elf.get("pie"):
            load_bias = 0
        if static_va is None and runtime_va is not None and load_bias is not None:
            static_va = runtime_va - load_bias
        if runtime_va is None and static_va is not None and load_bias is not None:
            runtime_va = static_va + load_bias
        file_offset = None
        if static_va is not None:
            for segment in self.elf.get("segments", []):
                delta = static_va - segment["vaddr"]
                if 0 <= delta < segment.get("filesz", 0):
                    file_offset = segment["offset"] + delta
                    break
        return AddressRef(file_offset=file_offset, static_va=static_va, runtime_va=runtime_va,
                          module=module or str(self.path), load_bias=load_bias,
                          arch=ELF_MACHINES.get(self.elf.get("machine"), str(self.elf.get("machine", ""))),
                          pointer_bits=int(self.elf.get("bits", 0)))


def page(items: list, query: str = "", limit: int = 20, cursor: str = "", maximum: int = 100) -> dict:
    import json
    if not 1 <= limit <= maximum or len(query) > 256:
        raise BinaryError("BAD_QUERY", "limit 或过滤条件超出限制")
    start = address(cursor or "0")
    rows = [row for row in items if query.casefold() in json.dumps(row, ensure_ascii=False).casefold()] if query else items
    if start > len(rows):
        raise BinaryError("BAD_CURSOR", "分页位置超出结果")
    end = min(start + limit, len(rows))
    return {"items": rows[start:end], "next_cursor": str(end) if end < len(rows) else None,
            "truncated": end < len(rows), "total": len(rows)}
