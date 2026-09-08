$ErrorActionPreference = 'Stop'

# 启动脚本变量：项目根目录、后端解释器、前端构建产物、本地环境文件和服务地址。
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonPath = 'E:\anaconda3\envs\agent_dock\python.exe'
$FrontendIndex = Join-Path $ProjectRoot 'frontend\dist\index.html'
$LocalEnvironmentFile = Join-Path $ProjectRoot '.env.local'
$Url = 'http://127.0.0.1:8765'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'PGAgent Conda environment is missing. Run setup_pgagent.bat first.'
}
if (-not (Test-Path -LiteralPath $FrontendIndex)) {
    Write-Host 'Building the web interface...'
    Push-Location (Join-Path $ProjectRoot 'frontend')
    try { npm run build } finally { Pop-Location }
}

# Import only the supported marketplace values into the PGAgent process; never print secrets.
if (Test-Path -LiteralPath $LocalEnvironmentFile) {
    # localEnvironmentLines 是文件原文；marketEnvironmentLoaded 仅记录是否成功导入至少一个值。
    $localEnvironmentLines = Get-Content -LiteralPath $LocalEnvironmentFile
    $marketEnvironmentLoaded = $false
    foreach ($environmentKey in @(
        'VERCEL_OIDC_TOKEN',
        'PGAGENT_SKILL_MARKET_URL',
        'PGAGENT_SKILL_MARKET_CLIENT_TOKEN'
    )) {
        # escapedKey 用于安全拼接正则；environmentLine 取同名配置的最后一次声明。
        $escapedKey = [regex]::Escape($environmentKey)
        $environmentLine = $localEnvironmentLines | Where-Object {
            $_ -match "^\s*$escapedKey\s*="
        } | Select-Object -Last 1
        if ($environmentLine -match "^\s*$escapedKey\s*=\s*(.*?)\s*$") {
            # environmentValue 是等号右侧内容，成对引号会被移除后再写入当前进程环境。
            $environmentValue = $matches[1]
            if (
                $environmentValue.Length -ge 2 -and
                (($environmentValue.StartsWith('"') -and $environmentValue.EndsWith('"')) -or
                 ($environmentValue.StartsWith("'") -and $environmentValue.EndsWith("'")))
            ) {
                $environmentValue = $environmentValue.Substring(1, $environmentValue.Length - 2)
            }
            if ($environmentValue) {
                Set-Item -LiteralPath "Env:$environmentKey" -Value $environmentValue
                $marketEnvironmentLoaded = $true
            }
        }
    }
    if ($marketEnvironmentLoaded) {
        Write-Host 'Loaded local Skill marketplace configuration.' -ForegroundColor DarkGray
    }
}

# 后台探测任务：服务健康检查通过后自动打开浏览器，主进程仍由 uvicorn 负责。
$BrowserJob = Start-Job -ScriptBlock {
    param($TargetUrl)
    # attempt 限制轮询次数；response 仅用于判断健康接口是否返回 HTTP 200。
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "$TargetUrl/api/health" -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                Start-Process $TargetUrl
                return
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
} -ArgumentList $Url

Set-Location $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot 'backend'
Write-Host "PGAgent is starting at $Url" -ForegroundColor Cyan
Write-Host 'Press Ctrl+C to stop the local service.'
try {
    & $PythonPath -m uvicorn src.main:app --app-dir backend --host 127.0.0.1 --port 8765
} finally {
    Stop-Job -Job $BrowserJob -ErrorAction SilentlyContinue
    Remove-Job -Job $BrowserJob -Force -ErrorAction SilentlyContinue
}
