#!/usr/bin/env bash
# 把仓库历史里的提交作者/提交者邮箱改成你的 Gitee 绑定邮箱（贡献墙按邮箱归属统计）。
#
# 用法：
#   scripts/fix-commit-email.sh --email 15640673@user.noreply.gitee.com            # 只改本地（安全，可先看结果）
#   scripts/fix-commit-email.sh --email <addr> --push                             # 改完强制推送到 origin
#   scripts/fix-commit-email.sh --email <addr> --name 你的昵称
#
# 安全保证：
#   1. 改前自动建备份分支 backup/pre-email-rewrite-<时间戳>；
#   2. 断言「内容树哈希」改前改后完全一致（只允许元数据变化），不一致立即回滚；
#   3. 推送使用 --force-with-lease（远端被别人改过会拒绝，不会误覆盖）；
#   4. 支持 --dry-run：只打印将要发生的事。
#
# 为什么必须重写历史：Gitee 贡献墙按「提交邮箱 ↔ 账号已绑定邮箱」匹配，
# 且邮箱绑定**之前**的历史提交不会被回填，因此旧提交必须改邮箱后重新推送。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EMAIL=""
NAME=""
PUSH=0
DRY=0
REMOTE="origin"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) EMAIL="${2:?--email 需要地址}"; shift 2 ;;
    --name) NAME="${2:?--name 需要值}"; shift 2 ;;
    --remote) REMOTE="${2:?--remote 需要值}"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "!! 未知参数 $1（-h 查看用法）" >&2; exit 2 ;;
  esac
done

if [[ -z "$EMAIL" ]]; then
  echo "!! 必须提供 --email（Gitee → 设置 → 邮箱管理 里显示的完整地址）" >&2
  exit 2
fi
if [[ ! "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then
  echo "!! 邮箱格式看起来不对：$EMAIL" >&2
  exit 2
fi

cd "$REPO_ROOT"
[[ -d .git ]] || { echo "!! 这里不是 git 仓库：$REPO_ROOT" >&2; exit 2; }
[[ -z "$(git status --porcelain)" ]] || { echo "!! 工作区不干净，请先提交或 stash" >&2; exit 2; }

BEFORE_TREE="$(git rev-parse HEAD^{tree})"
BEFORE_COUNT="$(git rev-list --count HEAD)"
BEFORE_EMAILS="$(git log --format='%ae' | sort -u | tr '\n' ' ')"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="backup/pre-email-rewrite-$STAMP"

echo "== 改前：提交 $BEFORE_COUNT 个 · 邮箱 [$BEFORE_EMAILS] · 内容树 ${BEFORE_TREE:0:12}"
echo "== 目标邮箱：$EMAIL${NAME:+（作者名：$NAME）}"
echo "== 备份分支：$BACKUP"

if [[ "$DRY" == "1" ]]; then
  echo "(dry-run) 将执行：备份分支 → filter-branch 重写作者邮箱 → 校验内容树 → "
  [[ "$PUSH" == "1" ]] && echo "(dry-run) 并推送到 $REMOTE（--force-with-lease）" || echo "(dry-run) 不推送（未加 --push）"
  exit 0
fi

git branch "$BACKUP" >/dev/null
echo "== 已建备份分支"

ENV_FILTER="export GIT_AUTHOR_EMAIL='$EMAIL'; export GIT_COMMITTER_EMAIL='$EMAIL';"
if [[ -n "$NAME" ]]; then
  ENV_FILTER+=" export GIT_AUTHOR_NAME='$NAME'; export GIT_COMMITTER_NAME='$NAME';"
fi

FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --env-filter "$ENV_FILTER" --tag-name-filter cat -- --all >/dev/null
rm -rf .git/refs/original .git/filter-branch 2>/dev/null || true

AFTER_TREE="$(git rev-parse HEAD^{tree})"
AFTER_COUNT="$(git rev-list --count HEAD)"
AFTER_EMAILS="$(git log --format='%ae' | sort -u | tr '\n' ' ')"

if [[ "$BEFORE_TREE" != "$AFTER_TREE" || "$BEFORE_COUNT" != "$AFTER_COUNT" ]]; then
  echo "!! 内容发生变化（tree 或提交数不一致）→ 回滚到 $BACKUP" >&2
  git reset --hard "$BACKUP" >/dev/null
  exit 3
fi

git config user.email "$EMAIL"
[[ -n "$NAME" ]] && git config user.name "$NAME"

echo "== 改后：提交 $AFTER_COUNT 个 · 邮箱 [$AFTER_EMAILS] · 内容树 ${AFTER_TREE:0:12}"
echo "== 内容树一致（只有元数据变了）✅"
echo "== 已把本仓库 git config user.email 设为 $EMAIL（后续提交自动归属）"

if [[ "$PUSH" == "1" ]]; then
  echo "== 推送到 $REMOTE（--force-with-lease）"
  git push --force-with-lease "$REMOTE" HEAD
  echo "== 推送完成。回到 Gitee 刷新个人主页贡献墙（新提交即时生效；历史提交改写后也会计入）"
else
  echo "== 未推送。确认无误后执行：git push --force-with-lease $REMOTE HEAD"
fi
