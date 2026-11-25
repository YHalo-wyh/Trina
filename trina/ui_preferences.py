"""控制台个人偏好，每项独立保存。"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Preference:
    label: str
    field: str
    choices: tuple[tuple[str, str], ...]
    section: str = "ui"
    integer: bool = False

    def encode(self, value: str | bool | int) -> str:
        return ("on" if value else "off") if type(value) is bool else str(value)

    def decode(self, value: str) -> str | bool | int:
        if value not in dict(self.choices):
            raise ValueError(f"{self.label}可选值 {' / '.join(key for key, _ in self.choices)}")
        if self.choices == TOGGLE:
            return value == "on"
        return int(value) if self.integer else value


TOGGLE = (("on", "开启"), ("off", "关闭"))
PREFERENCES = {
    "theme": Preference("输出配色", "output_palette", (("trina", "粉紫主题"), ("contrast", "高对比"), ("mono", "单色"))),
    "footer": Preference("底栏密度", "status_line_preset", (("default", "均衡"), ("compact", "精简"), ("full", "完整"))),
    "separator": Preference("底栏分隔符", "status_line_separator", (("dot", "圆点"), ("pipe", "竖线"), ("slash", "斜杠"))),
    "context-bar": Preference("上下文指示条", "context_bar", TOGGLE),
    "notify": Preference("完成提醒", "completion_notify", TOGGLE),
    "error-notify": Preference("错误提醒", "error_notify", TOGGLE),
    "icons": Preference("命令菜单图标", "command_icons", TOGGLE),
    "retries": Preference("模型断连重试", "connection_retries",
                          (("0", "关闭"), *((str(n), f"{n} 次") for n in (1, 2, 3, 5, 10))),
                          section="llm", integer=True),
}


def read_preferences(root: Path) -> dict[str, str | bool | int]:
    path = root / "ui-preferences.sqlite3"
    if not path.is_file():
        return {}
    import sqlite3

    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            data = dict(db.execute("SELECT name, value FROM preferences"))
    except sqlite3.Error:
        return {}
    return {f"{spec.section}.{spec.field}": spec.decode(data[name]) for name, spec in PREFERENCES.items()
            if name in data and data[name] in dict(spec.choices)}


def save_preference(root: Path, name: str, value: str) -> None:
    import sqlite3

    PREFERENCES[name].decode(value)
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "ui-preferences.sqlite3")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS preferences (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO preferences VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))
