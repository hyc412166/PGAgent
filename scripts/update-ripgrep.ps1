param(
    [string]$Version = '15.2.0'
)

$ErrorActionPreference = 'Stop'

# 本脚本通过 Cargo 获取指定正式版本，再更新 PGAgent 自己维护的 Windows 二进制。
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$BundledDirectory = Join-Path $ProjectRoot 'vendor\ripgrep\windows-x64'
$BundledRipgrep = Join-Path $BundledDirectory 'rg.exe'
$CargoRipgrep = Join-Path $env:USERPROFILE '.cargo\bin\rg.exe'
$RustToolchain = 'stable-x86_64-pc-windows-gnu'

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw 'Cargo is unavailable. Install or restore the Rust toolchain before updating ripgrep.'
}
if (-not (Get-Command rustup -ErrorAction SilentlyContinue)) {
    throw 'rustup is unavailable. Restore the Rust toolchain before updating ripgrep.'
}

Write-Host "Installing ripgrep $Version through Cargo..." -ForegroundColor Cyan
& rustup toolchain install $RustToolchain
if ($LASTEXITCODE -ne 0) {
    throw "rustup toolchain install $RustToolchain failed with exit code $LASTEXITCODE."
}
# GNU 工具链自带 Windows 链接依赖，不要求机器额外安装完整 Windows SDK。
$RustSysroot = (& rustc "+$RustToolchain" --print sysroot).Trim()
$RustLinkerDirectory = Join-Path $RustSysroot 'lib\rustlib\x86_64-pc-windows-gnu\bin\self-contained'
$RustLinker = Join-Path $RustLinkerDirectory 'x86_64-w64-mingw32-gcc.exe'
if (-not (Test-Path -LiteralPath $RustLinker -PathType Leaf)) {
    throw "Rust GNU linker is missing: $RustLinker"
}
$DllTool = (Get-Command dlltool.exe -ErrorAction SilentlyContinue).Source
if (-not $DllTool) {
    throw 'GNU dlltool.exe is unavailable. Install MinGW before updating ripgrep.'
}
# 当前机器可能已有旧 MinGW；显式优先使用 rustup 与本工具链一起分发的兼容 linker。
$env:Path = "$RustLinkerDirectory;$env:Path"
$env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = $RustLinker
$env:RUSTFLAGS = "-C dlltool=$DllTool"
& cargo "+$RustToolchain" install ripgrep --version $Version --locked --force --features pcre2
if ($LASTEXITCODE -ne 0) {
    throw "cargo install ripgrep failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $CargoRipgrep -PathType Leaf)) {
    throw "Cargo completed without producing rg.exe at $CargoRipgrep."
}

New-Item -ItemType Directory -Path $BundledDirectory -Force | Out-Null
Copy-Item -LiteralPath $CargoRipgrep -Destination $BundledRipgrep -Force

# 直接执行项目副本，验证升级结果而不是继承 PATH 中的其他 rg。
$installedVersion = & $BundledRipgrep --version
if ($LASTEXITCODE -ne 0) {
    throw "Bundled rg.exe validation failed with exit code $LASTEXITCODE."
}
Write-Host ($installedVersion | Select-Object -First 1) -ForegroundColor Green
