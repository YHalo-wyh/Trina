#!/usr/bin/env bash
# Trina 本地启动器（给"我自己试一下"用）
#
# 用法：
#   scripts/dev.sh                     # 进交互控制台（后端=omp，复用 omp 鉴权）
#   scripts/dev.sh --api deepseek      # 进交互控制台（后端=DeepSeek 官方 API，读 $DEEPSEEK_API_KEY）
#   scripts/dev.sh play --api deepseek # 一条命令直接开打（自动起 lab → 渗透 → 报告）
#   scripts/dev.sh play --api deepseek --goal "拿到 flag 并验证 SQL 注入" --max-steps 16
#   scripts/dev.sh doctor --api deepseek   # 只做环境自检
#
# 密钥来源（优先级）：环境变量 → ~/.trina/env（不入仓库，建议 chmod 600）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${TRINA_ENV_FILE:-$HOME/.trina/env}"

# 载入本地密钥文件（如果存在）
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

API=""
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api)
      API="${2:-omp}"
      shift 2
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

# 默认后端优先级：命令行 --api > $TRINA_API（可由 ~/.trina/env 持久化）> omp
API="${API:-${TRINA_API:-omp}}"

# 默认子命令：console（交互控制台）；也允许 `dev.sh --api X` 这种把选项写在最前
if [[ ${#ARGS[@]} -eq 0 ]]; then
  ARGS=(console)
elif [[ "${ARGS[0]}" == --* ]]; then
  ARGS=(console "${ARGS[@]}")
fi
# 允许 `dev.sh play ...` / `dev.sh doctor` / `dev.sh console`
SUB="${ARGS[0]}"
REST=("${ARGS[@]:1}")

OVERRIDES=()
case "$API" in
  omp)
    echo "== 后端：omp（复用 omp 已配置的模型与鉴权，默认 ${TRINA_MODEL:-deepseek/deepseek-v4-flash}）"
    if [[ -n "${TRINA_MODEL:-}" ]]; then
      OVERRIDES+=(--set "llm.model=$TRINA_MODEL")
    fi
    ;;
  deepseek)
    if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
      echo "!! 未找到 DEEPSEEK_API_KEY：请 exports 或写入 $ENV_FILE（chmod 600）" >&2
      echo "   格式：export DEEPSEEK_API_KEY=sk-xxxxxxxx" >&2
      exit 2
    fi
    OVERRIDES=(
      --set llm.backend=openai
      --set "llm.model=${TRINA_MODEL:-deepseek-v4-flash}"
      --set llm.base_url=https://api.deepseek.com/v1
      --set llm.api_key_env=DEEPSEEK_API_KEY
    )
    echo "== 后端：DeepSeek 官方 API（${TRINA_MODEL:-deepseek-v4-flash}）"
    ;;
  *)
    echo "!! 未知 --api $API（可选：omp | deepseek）" >&2
    exit 2
    ;;
esac

cd "$REPO_ROOT"
case "$SUB" in
  console|play|doctor|run|eval|review|tools|tool|guard|ranges|audit|report|lab)
    exec python3 -m trina "$SUB" "${OVERRIDES[@]}" ${REST[@]+"${REST[@]}"}
    ;;
  *)
    echo "!! 未知子命令 $SUB（可用：console/play/doctor/run/eval/review/tools/guard/ranges/audit/report/lab）" >&2
    exit 2
    ;;
esac
