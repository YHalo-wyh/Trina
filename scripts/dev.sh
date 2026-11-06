#!/usr/bin/env bash
# 开发入口只转发给统一 Python CLI，不维护模型配置或子命令副本。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "${TRINA_PYTHON:-python3}" -m trina "$@"
