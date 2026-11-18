#!/usr/bin/env bash
# Ubuntu/Debian/WSL 的可重复工具安装；平台服务凭据不随工具安装。
set -euo pipefail
TRINA_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TRINA_SUDO=()
if [[ "$EUID" != 0 ]]; then TRINA_SUDO=(sudo); fi
"${TRINA_SUDO[@]}" apt-get update
"${TRINA_SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates git build-essential pkg-config python3-venv python3-pip \
  nodejs npm nmap dnsutils curl wget jq ripgrep netcat-openbsd telnet whois gdb bubblewrap
if ! node -e 'if (Number(process.versions.node.split(".")[0]) < 20) process.exit(1)'; then
  case "$(uname -m)" in x86_64) TRINA_NODE_ARCH=x64;; aarch64) TRINA_NODE_ARCH=arm64;; *) echo '不支持的Node架构' >&2; exit 1;; esac
  TRINA_NODE_VERSION=v22.21.1
  TRINA_NODE_BUILD="$(mktemp -d -t trina-node-XXXXXXXX)"
  TRINA_NODE_FILE="node-$TRINA_NODE_VERSION-linux-$TRINA_NODE_ARCH.tar.xz"
  (cd "$TRINA_NODE_BUILD"
   curl --fail --location --retry 2 -O "https://nodejs.org/dist/$TRINA_NODE_VERSION/$TRINA_NODE_FILE"
   curl --fail --location --retry 2 -O "https://nodejs.org/dist/$TRINA_NODE_VERSION/SHASUMS256.txt"
   awk -v file="$TRINA_NODE_FILE" '$2 == file' SHASUMS256.txt | sha256sum --check --strict
   "${TRINA_SUDO[@]}" tar -xJf "$TRINA_NODE_FILE" -C /usr/local --strip-components=1)
fi
"${TRINA_SUDO[@]}" npm install -g @playwright/cli@0.1.24
playwright-cli install-browser chromium --with-deps
if ! command -v r2 >/dev/null; then
  TRINA_R2_BUILD="$(mktemp -d -t trina-radare2-XXXXXXXX)"
  git clone --depth 1 --branch 5.9.8 https://github.com/radareorg/radare2.git "$TRINA_R2_BUILD/source"
  (cd "$TRINA_R2_BUILD/source"; ./configure --prefix=/usr/local; make -j2; "${TRINA_SUDO[@]}" make install)
  echo "radare2 构建源保留在 $TRINA_R2_BUILD"
fi
python3 -m venv "$TRINA_REPO/.venv-tools"
"$TRINA_REPO/.venv-tools/bin/python" -m pip install -e "$TRINA_REPO"
"$TRINA_REPO/.venv-tools/bin/python" -m trina tools --json >/dev/null
echo '工具栈安装完成；激活 .venv-tools 后可运行 trina doctor 检查模型配置。'
