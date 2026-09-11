$ErrorActionPreference = 'Stop'

# 安装所需路径：项目根目录、固定 Conda 环境目录以及其 Python 可执行文件。
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EnvironmentPath = 'E:\anaconda3\envs\agent_dock'
$PythonPath = Join-Path $EnvironmentPath 'python.exe'

# 独立 Node 运行时不依赖启动本脚本的旧 PATH，避免 Windows Terminal/Codex 进程缓存过期路径。
$NodeRoot = "$env:USERPROFILE\Tools\nodejs\22.22.2"
$NodePath = Join-Path $NodeRoot 'node.exe'
$NpmPath = Join-Path $NodeRoot 'npm.cmd'
$RipgrepDirectory = Join-Path $ProjectRoot 'vendor\ripgrep\windows-x64'
$RipgrepPath = Join-Path $RipgrepDirectory 'rg.exe'
if (-not (Test-Path -LiteralPath $NodePath) -or -not (Test-Path -LiteralPath $NpmPath)) {
    throw "独立 Node.js/npm 未找到：$NodeRoot。请确认 Node.js 文件完整存在。"
}
$env:Path = "$NodeRoot;$env:Path"

# 安装和前端构建也使用项目自带 rg，使 setup 与实际服务保持同一工具解析顺序。
if (-not (Test-Path -LiteralPath $RipgrepPath -PathType Leaf)) {
    throw "PGAgent bundled ripgrep is missing: $RipgrepPath"
}
try {
    $null = & $RipgrepPath --version
    if ($LASTEXITCODE -ne 0) {
        throw "exit code $LASTEXITCODE"
    }
} catch {
    throw "PGAgent bundled ripgrep is not executable: $RipgrepPath. $($_.Exception.Message)"
}
$env:Path = "$RipgrepDirectory;$env:Path"

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
    & $NpmPath install
    if ($LASTEXITCODE -ne 0) {
        throw "Web dependency installation failed with exit code $LASTEXITCODE."
    }
    & $NpmPath run build
    if ($LASTEXITCODE -ne 0) {
        throw "Web interface build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

Write-Host 'PGAgent is ready. Run start_pgagent.bat.' -ForegroundColor Green
