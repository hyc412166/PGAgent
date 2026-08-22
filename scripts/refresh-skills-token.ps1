$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$LocalEnvironmentFile = Join-Path $ProjectRoot '.env.local'
$VercelCommand = Get-Command vercel -ErrorAction SilentlyContinue
$RefreshTimeoutSeconds = 60

if ($env:SKILLS_SH_API_TOKEN -or $env:PGAGENT_SKILLS_SH_API_TOKEN) {
    Write-Host 'Using explicit Skill marketplace authentication.' -ForegroundColor DarkGray
    return
}

if (-not $VercelCommand) {
    throw 'Vercel CLI is unavailable. Install it and run vercel login first.'
}

$RefreshJob = Start-Job -ScriptBlock {
    param($VercelPath, $EnvironmentFile, $WorkingDirectory)
    Set-Location -LiteralPath $WorkingDirectory
    & $VercelPath env pull $EnvironmentFile --environment development --yes --no-color --non-interactive *> $null
    return $LASTEXITCODE
} -ArgumentList $VercelCommand.Source, $LocalEnvironmentFile, $ProjectRoot

try {
    $CompletedJob = Wait-Job -Job $RefreshJob -Timeout $RefreshTimeoutSeconds
    if (-not $CompletedJob) {
        Stop-Job -Job $RefreshJob -ErrorAction SilentlyContinue
        throw "Vercel CLI token refresh timed out after $RefreshTimeoutSeconds seconds."
    }
    $RefreshExitCode = Receive-Job -Job $RefreshJob
    if ($RefreshExitCode -ne 0) {
        throw "Vercel CLI could not refresh the local OIDC token (exit code $RefreshExitCode)."
    }
} finally {
    Remove-Job -Job $RefreshJob -Force -ErrorAction SilentlyContinue
}

Write-Host 'Refreshed local Skill marketplace authentication.' -ForegroundColor DarkGray
