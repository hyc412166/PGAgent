$ErrorActionPreference = 'Stop'

# 安装所需路径：项目根目录、固定 Conda 环境目录以及其 Python 可执行文件。
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EnvironmentPath = 'E:\anaconda3\envs\agent_dock'
$PythonPath = Join-Path $EnvironmentPath 'python.exe'

# 优先沿用原有 npm；仅在 npm 不可用时使用项目已支持的 pnpm。
$WebPackageManager = Get-Command 'npm.cmd' -ErrorAction SilentlyContinue
if (-not $WebPackageManager) {
    $WebPackageManager = Get-Command 'npm' -ErrorAction SilentlyContinue
}
if (-not $WebPackageManager) {
    $WebPackageManager = Get-Command 'pnpm.cmd' -ErrorAction SilentlyContinue
}
if (-not $WebPackageManager) {
    $WebPackageManager = Get-Command 'pnpm' -ErrorAction SilentlyContinue
}
if (-not $WebPackageManager) {
    throw 'Node.js package manager is missing. Install Node.js LTS (npm) or pnpm, then run setup_pgagent.bat again.'
}
if (-not (Get-Command 'node.exe' -ErrorAction SilentlyContinue) -and -not (Get-Command 'node' -ErrorAction SilentlyContinue)) {
    throw 'Node.js runtime is missing from PATH. Install Node.js LTS and reopen this terminal, then run setup_pgagent.bat again.'
}

Write-Host 'PGAgent setup' -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $PythonPath)) {
    Write-Host "Creating Conda environment at $EnvironmentPath"
    conda create -p $EnvironmentPath python=3.12 pip -y --override-channels -c https://repo.anaconda.com/pkgs/main
}

Write-Host 'Installing Python dependencies...'
& $PythonPath -m pip install -r (Join-Path $ProjectRoot 'backend\requirements.txt')
& $PythonPath -m pip check

Write-Host 'Installing and building the web interface...'
Push-Location (Join-Path $ProjectRoot 'frontend')
try {
    & $WebPackageManager.Source install
    if ($LASTEXITCODE -ne 0) {
        throw "Web dependency installation failed with exit code $LASTEXITCODE."
    }
    & $WebPackageManager.Source run build
    if ($LASTEXITCODE -ne 0) {
        throw "Web interface build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

Write-Host 'PGAgent is ready. Run start_pgagent.bat.' -ForegroundColor Green
