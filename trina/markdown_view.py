"""面向流式终端的轻量 Markdown 排版。"""
from __future__ import annotations

import re
from functools import lru_cache

from .text import clean_text


RESET = "\x1b[0m"
_STATEFUL_CODE = re.compile(r"'''|\"\"\"|/\*|\*/|<!--|-->|\\\s*$", re.MULTILINE)


@lru_cache(maxsize=1)
def _parser():
    from markdown_it import MarkdownIt
    return MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


@lru_cache(maxsize=64)
def _lexer(language: str):
    from pygments.lexers import TextLexer, get_lexer_by_name
    from pygments.util import ClassNotFound
    try:
        return get_lexer_by_name(language or "text", stripnl=False, ensurenl=False)
    except ClassNotFound:
        return TextLexer(stripnl=False, ensurenl=False)


@lru_cache(maxsize=512)
def _token_style(token_type) -> str:
    from pygments.styles import get_style_by_name
    values = get_style_by_name("monokai").style_for_token(token_type)
    parts = []
    if values.get("color"):
        parts.append("#" + values["color"])
    if values.get("bold"):
        parts.append("bold")
    if values.get("italic"):
        parts.append("italic")
    if values.get("underline"):
        parts.append("underline")
    return " ".join(parts)


def _merge(fragments: list[tuple[str, str]], style: str, text: str) -> None:
    if not text:
        return
    if fragments and fragments[-1][0] == style:
        previous_style, previous_text = fragments[-1]
        fragments[-1] = previous_style, previous_text + text
    else:
        fragments.append((style, text))


@lru_cache(maxsize=4096)
def _highlight_line_fragments(language: str, line: str) -> tuple[tuple[str, str], ...]:
    from pygments import lex
    fragments: list[tuple[str, str]] = []
    for token_type, value in lex(line, _lexer(language)):
        _merge(fragments, _token_style(token_type), value)
    return tuple(fragments)


def _highlight_stateful_fragments(language: str, code: str) -> tuple[tuple[tuple[str, str], ...], ...]:
    from pygments import lex
    rows: list[list[tuple[str, str]]] = [[]]
    for token_type, value in lex(code, _lexer(language)):
        style = _token_style(token_type)
        parts = value.split("\n")
        for index, part in enumerate(parts):
            _merge(rows[-1], style, part)
            if index < len(parts) - 1:
                rows.append([])
    return tuple(tuple(row) for row in rows)


def _inline_fragments(tokens, color: bool, base: str = "") -> list[tuple[str, str]]:
    """沿 CommonMark 的真实 token 栈合成样式，支持嵌套、转义与引用链接。"""
    result: list[tuple[str, str]] = []
    styles, links = [base], []
    for token in tokens or []:
        kind = token.type
        if token.nesting == 1:
            extra = {"strong_open": "bold", "em_open": "italic", "s_open": "strike", "link_open": "underline #c7a5e8"}.get(kind, "")
            styles.append(" ".join(part for part in (styles[-1], extra if color else "") if part))
            if kind == "link_open":
                links.append(token.attrGet("href") or "")
        elif token.nesting == -1:
            if len(styles) > 1:
                styles.pop()
            if kind == "link_close" and links:
                _merge(result, styles[-1], " (" + links.pop() + ")")
        elif kind == "code_inline":
            _merge(result, (styles[-1] + " #c7a5e8").strip() if color else "", token.content)
        elif kind in {"softbreak", "hardbreak"}:
            _merge(result, styles[-1], "\n")
        elif kind == "image":
            _merge(result, styles[-1], token.content + " (" + (token.attrGet("src") or "") + ")")
        else:
            _merge(result, styles[-1], token.content)
    return result


def render_markdown_fragments(message: str, *, color: bool = False) -> list[tuple[str, str]]:
    """直接生成 prompt_toolkit 样式片段，避免 ANSI 逐 token 反解析。"""
    source = clean_text(message.replace("\r\n", "\n").replace("\r", "\n"))
    rows: list[list[tuple[str, str]]] = []
    lists: list[dict] = []
    quote, heading, item_pending = 0, False, False
    table_cells: list[list[tuple[str, str]]] | None = None

    def add(parts: list[tuple[str, str]] | None = None) -> None:
        rows.append(parts or [])

    def prefix() -> list[tuple[str, str]]:
        nonlocal item_pending
        out = [("#9b7bc7" if color else "", "│ " * quote)] if quote else []
        if lists:
            if item_pending:
                current = lists[-1]
                marker = f"{current['index']}. " if current["ordered"] else "• "
                out.extend([("", "  " * (len(lists) - 1)), ("#9b7bc7" if color else "", marker)])
                item_pending = False
            else:
                out.append(("", "  " * len(lists)))
        return out

    def add_code(value: str, language: str) -> None:
        # parser 的终止换行不是额外空行，源码本身的其他空行必须保留。
        value = value.removesuffix("\n")
        if not value:
            add([("", "  ")])
            return
        if color and (language.lower() not in {"python", "py", "python3", "json", "text", "txt"} or _STATEFUL_CODE.search(value)):
            highlighted = _highlight_stateful_fragments(language, value)
        elif color:
            # 只缓存有界单行；长单行不长期占据会话缓存。
            highlighted = tuple((_highlight_line_fragments if len(line) <= 4096 else _highlight_line_fragments.__wrapped__)(language, line)
                                for line in value.split("\n"))
        else:
            highlighted = tuple((("", line),) for line in value.split("\n"))
        for highlighted_row in highlighted:
            add([*prefix(), ("", "  "), *highlighted_row])

    for token in _parser().parse(source):
        kind = token.type
        if kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append({"ordered": kind == "ordered_list_open", "index": int(token.attrGet("start") or 1)})
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
            if not lists:
                add()
        elif kind == "list_item_open":
            item_pending = True
        elif kind == "list_item_close":
            lists[-1]["index"] += 1
            item_pending = False
        elif kind == "blockquote_open":
            quote += 1
        elif kind == "blockquote_close":
            quote -= 1
        elif kind == "heading_open":
            heading = True
        elif kind == "heading_close":
            heading = False
            add()
        elif kind == "tr_open":
            table_cells = []
        elif kind == "tr_close":
            row = prefix()
            for cell in table_cells or []:
                row.extend([("#a595b3" if color else "", "│ "), *cell, ("", " ")])
            row.append(("#a595b3" if color else "", "│"))
            add(row)
            table_cells = None
        elif kind == "table_close":
            add()
        elif kind == "inline":
            fragments = _inline_fragments(token.children, color, "bold #edb2d2" if heading and color else "")
            if "&" in token.content:
                # 实体解码可能重新引入方向控制字符，解析后仍需净化。
                fragments = [(style, clean_text(text)) for style, text in fragments]
            if table_cells is not None:
                table_cells.append(fragments)
            else:
                add([*prefix(), *fragments])
        elif kind == "paragraph_close" and not token.hidden:
            add()
        elif kind in {"fence", "code_block"}:
            add_code(token.content, token.info.strip().split(None, 1)[0] if token.info.strip() else "text")
            add()
        elif kind == "hr":
            add([("#a595b3" if color else "", "─" * 24)])
        elif token.content:
            add([*prefix(), ("", token.content)])
    while rows and not rows[-1]:
        rows.pop()
    result: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        for style, text in row:
            _merge(result, style, text)
        if index < len(rows) - 1:
            _merge(result, "", "\n")
    return result


@lru_cache(maxsize=256)
def _style_ansi(style: str) -> str:
    if not style:
        return ""
    values = []
    for part in style.split():
        if part == "bold":
            values.append("1")
        elif part == "italic":
            values.append("3")
        elif part == "underline":
            values.append("4")
        elif part == "strike":
            values.append("9")
        elif part.startswith("#") and len(part) == 7:
            values.append("38;2;" + ";".join(str(int(part[index:index + 2], 16)) for index in (1, 3, 5)))
    return f"\x1b[{';'.join(values)}m" if values else ""


def fragments_to_ansi(fragments: list[tuple[str, str]]) -> str:
    """非全屏输出继续使用标准 ANSI，全屏界面直接消费样式片段。"""
    result, active = [], ""
    for style, text in fragments:
        if style != active:
            if active:
                result.append(RESET)
            if style:
                result.append(_style_ansi(style))
            active = style
        result.append(text)
    if active:
        result.append(RESET)
    return "".join(result)


def wrap_fragments(fragments: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    """非全屏输出按显示单元折行；样式片段不进入可见宽度计算。"""
    from rich.cells import cell_len, get_character_cell_size
    result: list[tuple[str, str]] = []
    used = 0
    width = max(1, width)
    for style, text in fragments:
        for index, line in enumerate(text.split("\n")):
            if index:
                _merge(result, "", "\n")
                used = 0
            cells = len(line) if line.isascii() else cell_len(line)
            if cells + used <= width:
                _merge(result, style, line)
                used += cells
                continue
            part = []
            for char in line:
                size = get_character_cell_size(char)
                if used + size > width:
                    _merge(result, style, "".join(part))
                    _merge(result, "", "\n")
                    part, used = [], 0
                part.append(char)
                used += size
            _merge(result, style, "".join(part))
    return result
