$ErrorActionPreference = 'Stop'

# 安装所需路径：项目根目录、固定 Conda 环境目录以及其 Python 可执行文件。
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EnvironmentPath = 'E:\anaconda3\envs\agent_dock'
$PythonPath = Join-Path $EnvironmentPath 'python.exe'

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
    npm install
    npm run build
} finally {
    Pop-Location
}

Write-Host 'PGAgent is ready. Run start_pgagent.bat.' -ForegroundColor Green
