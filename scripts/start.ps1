$ErrorActionPreference = 'Stop'

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

# Import only the OIDC value into the PGAgent process; never print it.
if (Test-Path -LiteralPath $LocalEnvironmentFile) {
    $oidcLine = Get-Content -LiteralPath $LocalEnvironmentFile | Where-Object {
        $_ -match '^\s*VERCEL_OIDC_TOKEN\s*='
    } | Select-Object -Last 1
    if ($oidcLine -match '^\s*VERCEL_OIDC_TOKEN\s*=\s*(.*?)\s*$') {
        $oidcToken = $matches[1]
        if (
            $oidcToken.Length -ge 2 -and
            (($oidcToken.StartsWith('"') -and $oidcToken.EndsWith('"')) -or
             ($oidcToken.StartsWith("'") -and $oidcToken.EndsWith("'")))
        ) {
            $oidcToken = $oidcToken.Substring(1, $oidcToken.Length - 2)
        }
        if ($oidcToken) {
            $env:VERCEL_OIDC_TOKEN = $oidcToken
            Write-Host 'Loaded local Skill marketplace authentication.' -ForegroundColor DarkGray
        }
    }
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
    & $PythonPath -m uvicorn src.main:app --app-dir backend --host 127.0.0.1 --port 8765
} finally {
    Stop-Job -Job $BrowserJob -ErrorAction SilentlyContinue
    Remove-Job -Job $BrowserJob -Force -ErrorAction SilentlyContinue
}
