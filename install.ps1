# ============================================================
# install.ps1 — 安装 AIOS Framework（Windows PowerShell 版）
#
# 作者: hb <sendhb@21cn.com>
#
# 用法（Windows 10+ 自带 PowerShell，零安装）:
#   irm https://<host>/install.ps1 | iex
#
# 说明:
#   - 默认安装到 $HOME\.aibase
#   - 只检查 python/git，不自动安装（给出 winget 提示）
#   - 不污染系统全局状态
# ============================================================

param(
    [string]$Dir = "$HOME\.aibase",
    [string]$RepoUrl = "https://github.com/your-org/aibase.git"
)

Write-Host "=== AIOS Framework 安装 ===" -ForegroundColor Yellow
Write-Host "install dir: $Dir"

# ---------- 依赖检查（只检查，不自动安装）----------
$missing = $false

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "x 缺少 git" -ForegroundColor Red
    Write-Host "  安装: winget install Git.Git"
    $missing = $true
}
if (-not (Get-Command python3 -ErrorAction SilentlyContinue) -and
    -not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "x 缺少 Python 3" -ForegroundColor Red
    Write-Host "  安装: winget install Python.Python.3"
    $missing = $true
}
if ($missing) {
    Write-Host "请先安装缺失依赖后重试。" -ForegroundColor Red
    exit 1
}

# ---------- 下载 ----------
Write-Host "-> 克隆 aibase 到 $Dir ..."
if (Test-Path "$Dir\.git") {
    Write-Host "  已存在，git pull 更新..."
    git -C $Dir pull
} else {
    git clone $RepoUrl $Dir
}

# ---------- 完成 ----------
Write-Host "✓ 安装完成" -ForegroundColor Green
Write-Host ""
Write-Host "  使用:"
Write-Host "    cd C:\path\to\your-project"
Write-Host "    python $Dir\kit\cli\init --profile backend --install-deps"
Write-Host "    python $Dir\kit\cli\task list"
