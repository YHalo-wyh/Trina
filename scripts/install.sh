#!/usr/bin/env bash
# 安装 Trina 全局命令：让你在任意目录直接敲 `trina`
#
#   bash scripts/install.sh                 # 装到 ~/.local/bin/trina（在 PATH 上，无需 sudo）
#   bash scripts/install.sh --prefix /usr/local/bin   # 需要写权限（通常要 sudo）
#   bash scripts/install.sh --api omp       # 默认后端改成 omp（默认 deepseek）
#   bash scripts/install.sh --uninstall     # 卸载
#
# 做三件事：
#   1. 生成一个包装脚本 <prefix>/trina → 调本仓库的 scripts/dev.sh（自动带上默认后端）
#   2. 若缺密钥文件，提示（不会替你写密钥）：~/.trina/env（chmod 600，仓库外）
#   3. 打印自检命令
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${HOME}/.local/bin"
DEFAULT_API="deepseek"
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix 需要目录}"; shift 2 ;;
    --api) DEFAULT_API="${2:?--api 需要取值}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "!! 未知参数 $1（-h 查看用法）" >&2; exit 2 ;;
  esac
done

TARGET="$PREFIX/trina"

if [[ "$UNINSTALL" == "1" ]]; then
  if [[ -f "$TARGET" ]] && grep -q "Trina 全局入口" "$TARGET" 2>/dev/null; then
    rm -f "$TARGET" && echo "已卸载：$TARGET"
  else
    echo "未发现由本脚本安装的 $TARGET（未做改动）"
  fi
  exit 0
fi

mkdir -p "$PREFIX"

cat > "$TARGET" <<EOF
#!/usr/bin/env bash
# Trina 全局入口（由 scripts/install.sh 生成于 $(date -Iseconds)）
#
#   trina                     → 对话式控制台（人工可介入）
#   trina play --goal "..."   → 一条命令直接开打
#   trina doctor              → 环境自检
#   trina review              → 评价 Agent 复核最近一次运行
#
# 后端/模型来自 ~/.trina/env（TRINA_API / TRINA_MODEL，可被命令行 --api 覆盖）；
# 也可用 TRINA_REPO / TRINA_ENV_FILE 覆盖路径。
set -euo pipefail
REPO="\${TRINA_REPO:-$REPO_ROOT}"
if [[ ! -f "\$REPO/scripts/dev.sh" ]]; then
  echo "trina: 找不到仓库入口 \$REPO/scripts/dev.sh（可用 TRINA_REPO 指定）" >&2
  exit 2
fi
export PYTHONPATH="\$REPO\${PYTHONPATH:+:\$PYTHONPATH}"
exec bash "\$REPO/scripts/dev.sh" "\$@"
EOF
chmod +x "$TARGET"
echo "已安装：$TARGET（默认后端来自 ~/.trina/env，缺省 ${DEFAULT_API}）"

case ":$PATH:" in
  *":$PREFIX:"*) echo "PATH 检查：OK（$PREFIX 已在 PATH 中）" ;;
  *) echo "!! $PREFIX 不在 PATH：请在 ~/.bashrc 追加 export PATH=\"$PREFIX:\$PATH\"" ;;
esac

ENV_FILE="${TRINA_ENV_FILE:-$HOME/.trina/env}"
if [[ ! -f "$ENV_FILE" ]]; then
  mkdir -p "$(dirname "$ENV_FILE")"
  {
    echo "# Trina 本地配置（仓库外，0600）。可用控制台里的 /save 更新后端与模型。"
    echo "export TRINA_API=${DEFAULT_API}"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  echo "已写入默认后端到 $ENV_FILE（TRINA_API=${DEFAULT_API}）"
elif ! grep -q '^export TRINA_API=' "$ENV_FILE"; then
  echo "export TRINA_API=${DEFAULT_API}" >> "$ENV_FILE"
  echo "已把默认后端追加到 $ENV_FILE（TRINA_API=${DEFAULT_API}）"
fi

if [[ "${DEFAULT_API}" == "deepseek" ]] && ! grep -q '^export DEEPSEEK_API_KEY=' "$ENV_FILE"; then
  echo
  echo "!! 还缺 DeepSeek 密钥：$ENV_FILE"
  echo "   编辑该文件追加：export DEEPSEEK_API_KEY=sk-xxxx（或 export 到环境变量）"
fi

echo
echo "自检："
echo "  trina doctor              # 看后端连通性与护栏边界"
echo "  trina                     # 进交互控制台"
echo "  trina play --goal \"拿到 flag 并验证 SQL 注入\""
