$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonPath = 'E:\anaconda3\envs\agent_dock\python.exe'
$FrontendIndex = Join-Path $ProjectRoot 'frontend\dist\index.html'
$Url = 'http://127.0.0.1:8765'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'PGAgent Conda environment is missing. Run setup_pgagent.bat first.'
}
if (-not (Test-Path -LiteralPath $FrontendIndex)) {
    Write-Host 'Building the web interface...'
    Push-Location (Join-Path $ProjectRoot 'frontend')
    try { npm run build } finally { Pop-Location }
}

$BrowserJob = Start-Job -ScriptBlock {
    param($TargetUrl)
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
    & $PythonPath -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765
} finally {
    Stop-Job -Job $BrowserJob -ErrorAction SilentlyContinue
    Remove-Job -Job $BrowserJob -Force -ErrorAction SilentlyContinue
}

