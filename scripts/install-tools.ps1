# Windows 使用本机浏览器、GDB/r2；完整网络工具栈安装到 Ubuntu WSL。
param([string]$Distribution = 'Ubuntu-24.04')
$ErrorActionPreference = 'Stop'
$trinaRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
& npm.cmd install -g '@playwright/cli@0.1.24'
if ($LASTEXITCODE -ne 0) { throw 'Playwright CLI 安装失败' }
& playwright-cli.cmd install-browser chromium
if ($LASTEXITCODE -ne 0) { throw 'Chromium 安装失败' }
$trinaLinuxRoot = (& wsl.exe -d $Distribution -- wslpath -a $trinaRoot).Trim()
if ($LASTEXITCODE -ne 0 -or -not $trinaLinuxRoot.StartsWith('/')) { throw '无法解析 WSL 仓库路径' }
& wsl.exe -d $Distribution -- bash "$trinaLinuxRoot/scripts/install-tools.sh"
if ($LASTEXITCODE -ne 0) { throw 'WSL 工具栈安装失败' }
Write-Output '安装完成：Windows 浏览器工具与 WSL 网络/二进制工具；WSL 内用 .venv-tools/bin/trina 启动。'
