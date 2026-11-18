"""TSecBench 答题 API 接入（平台托管 / 本地模式）。"""

from .client import BenchmarkClient, BenchmarkError
from .commander import (
    EXTEND,
    RELEASE,
    RETRY,
    BenchCommander,
    CommanderDecision,
    CommanderSettings,
)
from .runner import (
    BenchOptions,
    BenchSummary,
    BenchmarkRunner,
    BenchmarkTask,
    choose_target,
    run_benchmark,
    submission_candidates,
)

__all__ = [
    "BenchCommander",
    "BenchOptions",
    "BenchSummary",
    "BenchmarkClient",
    "BenchmarkError",
    "BenchmarkRunner",
    "BenchmarkTask",
    "CommanderDecision",
    "CommanderSettings",
    "EXTEND",
    "RELEASE",
    "RETRY",
    "choose_target",
    "run_benchmark",
    "submission_candidates",
]
