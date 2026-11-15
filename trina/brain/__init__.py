"""决策大脑：ReAct 循环 + PTES 阶段机 + 提示词 + 事实记忆。"""

from .agent import PentestAgent, StepRecord, run_pentest
from .prompts import build_step_prompt, build_system_prompt, parse_action

__all__ = [
    "PentestAgent",
    "StepRecord",
    "run_pentest",
    "Planner",
    "PHASES",
    "build_system_prompt",
    "build_step_prompt",
    "parse_action",
]


def __getattr__(name):
    """旧阶段机仅在显式导入兼容 API 时加载。"""
    if name in {"Planner", "PHASES"}:
        from . import planner
        return getattr(planner, name)
    raise AttributeError(name)
