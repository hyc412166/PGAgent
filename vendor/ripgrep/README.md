# PGAgent bundled ripgrep

PGAgent 在 Windows 上携带自己的 `rg.exe`，默认编码 Agent 的 PowerShell 会优先从 `vendor/ripgrep/windows-x64` 解析 `rg`。这避免运行时依赖 Codex 的内部目录或用户机器上版本不确定的全局安装。

当前版本：ripgrep 15.2.0。

升级时在项目根目录运行：

```powershell
.\scripts\update-ripgrep.ps1 -Version 15.2.0
```

脚本会通过 rustup 的 Windows GNU 工具链让 Cargo 编译锁定依赖和 PCRE2 支持，不要求额外安装 Windows SDK；构建时需要 PATH 中已有 MinGW 的 `dlltool.exe`。随后脚本复制生成的 `rg.exe`，并直接执行项目副本的 `--version` 做验证。发布或复制 `rg.exe` 时必须同时保留本目录下的 `LICENSE-MIT` 与 `UNLICENSE`。
