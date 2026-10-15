"""靶场适配层：local / docker-compose / http-range / tsecbench。

统一契约见 `trina.adapters.base`：

    from trina.adapters import load_adapter, list_adapters
    adapter = load_adapter(config, "dvwa")   # None → 环境变量 TRINA_RANGE → default.yaml → local.yaml
    print(adapter.status())                  # {"ready": bool, "detail": str, "targets": [...]}
    info = adapter.prepare()                 # RangeInfo（永不抛异常）

设计约束：`prepare()` 拿不到靶场时返回空 targets + notes，绝不抛异常；
所有 out-of-scope 的实际攻击流量仍由 `trina.scope.ScopeGuard` 把关。
"""

from __future__ import annotations

from .base import (
    DEFAULT_FLAG_PATTERNS,
    DEFAULT_RANGES_DIR,
    PROJECT_ROOT,
    RANGE_KINDS,
    GroundTruthItem,
    RangeAdapter,
    RangeError,
    RangeInfo,
    RangeTarget,
    adapter_class_for,
    adapter_classes,
    available_range_names,
    default_range_candidate,
    empty_local_adapter,
    list_adapters,
    load_adapter,
    parse_ground_truth,
    parse_targets,
    ranges_dir,
    read_range_spec,
    resolve_range_path,
    tcp_probe,
)
from .docker import DockerComposeRangeAdapter
from .http_range import HttpRangeAdapter
from .local import LocalRangeAdapter
from .tsecbench import (
    DEFAULT_ENDPOINTS,
    DIFFICULTY_WEIGHTS,
    DIMENSION_DESCRIPTIONS,
    DIMENSION_WEIGHTS,
    TSecBenchAdapter,
    difficulty_weight,
    score_tsecbench,
)

__all__ = [
    "DEFAULT_ENDPOINTS",
    "DEFAULT_FLAG_PATTERNS",
    "DEFAULT_RANGES_DIR",
    "DIFFICULTY_WEIGHTS",
    "DIMENSION_DESCRIPTIONS",
    "DIMENSION_WEIGHTS",
    "DockerComposeRangeAdapter",
    "GroundTruthItem",
    "HttpRangeAdapter",
    "LocalRangeAdapter",
    "PROJECT_ROOT",
    "RANGE_KINDS",
    "RangeAdapter",
    "RangeError",
    "RangeInfo",
    "RangeTarget",
    "TSecBenchAdapter",
    "adapter_class_for",
    "adapter_classes",
    "available_range_names",
    "default_range_candidate",
    "difficulty_weight",
    "empty_local_adapter",
    "list_adapters",
    "load_adapter",
    "parse_ground_truth",
    "parse_targets",
    "ranges_dir",
    "read_range_spec",
    "resolve_range_path",
    "score_tsecbench",
    "tcp_probe",
]
