#!/usr/bin/env bash
# 使用 Python 标准入口安装 Trina，Windows 可直接运行 python -m pip install .
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${TRINA_PYTHON:-python3}" -m pip install "$REPO_ROOT" "$@"
