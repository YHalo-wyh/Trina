$ErrorActionPreference = 'Stop'
$trinaBenchRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$trinaBenchDependencies = Join-Path $trinaBenchRoot 'tmp/omp-output-benchmark'
New-Item -ItemType Directory -Path $trinaBenchDependencies -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'package.json') -Destination (Join-Path $trinaBenchDependencies 'package.json')
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'bun.lock')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'bun.lock') -Destination (Join-Path $trinaBenchDependencies 'bun.lock')
}
Push-Location $trinaBenchDependencies
try {
    & bun install --ignore-scripts
    if ($LASTEXITCODE -ne 0) { throw 'benchmark dependency installation failed' }
} finally {
    Pop-Location
}
& (Join-Path $trinaBenchDependencies 'node_modules/@oven/bun-windows-x64/bin/bun.exe') --version
