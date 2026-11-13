"""本地上下文预算选择；不作为供应商容量探测或请求扩容参数。"""
from __future__ import annotations

import re
from decimal import Decimal


def parse_context_window(value: str | int) -> int:
    text = str(value).strip().lower()
    if text in {"", "auto", "自动", "0"}:
        return 0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", text)
    if not match:
        raise ValueError("上下文窗口请输入 auto、500K、1M 或 Token 整数")
    number = Decimal(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2]]
    if number != int(number) or number <= 0 or number > 16000000:
        raise ValueError("上下文窗口须为 1—16000000 之间的整数")
    return int(number)


def default_context_window(model: str, base_url: str = "") -> int:
    name = model.casefold().replace("_", "-")
    if "deepseek" in name or "deepseek.com" in base_url.casefold():
        return 500000
    # 已核对的百万窗口系列；未知中转别名可用显式配置声明，不能据品牌推测。
    if re.search(r"gpt-(?:4\.1|5\.[456](?!-mini|-nano)|6-(?:astra|sol|luna)|6\.1-(?:sol|terra))(?:\b|-)", name):
        return 1000000
    if re.search(r"gemini-(?:1\.5|2(?:\.\d+)?|3(?:\.\d+)?)(?:\b|-)", name):
        return 1000000
    if re.search(r"claude-(?:opus|sonnet)-(?:4[.-][6789]|5(?:[.-]\d+)?)(?:\b|-)", name):
        return 1000000
    return 32768
