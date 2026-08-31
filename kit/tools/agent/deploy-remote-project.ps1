# deploy-remote-project.ps1 — TASK-070 远端项目 agent 一键部署（Windows 机执行）
#
# 拓扑（aimonitor 扁平 agents.json 授权模型）：每个 transport=agent 项目 = 独立 token +
# 独立 agent 进程。本脚本为 N 个项目各生成一个 agent.json、逐个 check + 首轮试推、
# 逐项目注册 Task Scheduler 常驻任务。
#
# 用法（hb 在 Windows 项目机执行）：
#   1) 从 aibase 机拷入三个文件（任选目录，如 $env:TEMP\aios-deploy）：
#      - aibase 的 kit 目录（含 tools/agent/）
#      - aimonitor/config/projects.json
#      - aimonitor/config/agents.json（token 源，⚠ 勿提交 Git）
#   2) powershell -ExecutionPolicy Bypass -File .\deploy-remote-project.ps1 `
#        -KitDir "C:\src\the5\aibase\kit" `
#        -ProjectsJson "C:\temp\projects.json" `
#        -AgentsJson "C:\temp\agents.json" `
#        -ServerUrl "http://47.109.205.200:3113/api/ingest"
#   3) 完成后：服务端验证 + aibase dispatcher reachability（见脚本末尾输出）
#
# 安全：agent.json（含 token）默认写入 C:\etc\aios-agent（本机盘，非网络共享），
# 并收紧 ACL 到 Administrators + 当前用户。脚本本身不含任何 token。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $KitDir,
    [Parameter(Mandatory = $true)][string] $ProjectsJson,
    [Parameter(Mandatory = $true)][string] $AgentsJson,
    [string] $ServerUrl = "http://47.109.205.200:3113/api/ingest",
    [string] $ConfigDir = "C:\etc\aios-agent",
    [switch] $SkipScheduler,
    [switch] $NoTestPush
)

$ErrorActionPreference = "Stop"

# --- 0. 前置检查 ---------------------------------------------------------------
$agentPy = Join-Path $KitDir "tools\agent\agent.py"
if (-not (Test-Path $agentPy)) { throw "找不到 agent.py: $agentPy（-KitDir 应指向 aibase 的 kit 目录）" }
if (-not (Test-Path $ProjectsJson)) { throw "找不到 -ProjectsJson: $ProjectsJson" }
if (-not (Test-Path $AgentsJson)) { throw "找不到 -AgentsJson: $AgentsJson" }
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "未在 PATH 找到 python（Windows 端需先安装 Python3）" }
Write-Host "python: $py" -ForegroundColor DarkGray

# --- 1. 解析 projects.json 中 transport=agent 的项目 ---------------------------
$projects = (Get-Content $ProjectsJson -Raw | ConvertFrom-Json).projects
$targets = @()
foreach ($p in $projects) {
    if ($p.transport -eq "agent") { $targets += $p }
}
if ($targets.Count -eq 0) { throw "projects.json 中没有 transport=agent 条目，无可部署项目" }
Write-Host ("agent 项目 x{0}: {1}" -f $targets.Count, (($targets | ForEach-Object { $_.id }) -join ", "))

# --- 2. 逐项目生成 agent.json（token 运行时从 agents.json 读出，不落库） --------
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
$agents = Get-Content $AgentsJson -Raw | ConvertFrom-Json
$summary = @()
foreach ($t in $targets) {
    $entry = $agents.$($t.id)
    if (-not $entry) {
        Write-Host ("[SKIP] {0}：agents.json 无此 id，跳过" -f $t.id) -ForegroundColor Yellow
        continue
    }
    $cfg = [ordered]@{
        server_url         = $ServerUrl
        token              = "$entry"
        projects           = @(@{ id = $t.id; path = ($t.path -replace '/', '\') })
        poll_interval_seconds = 30
    }
    $cfgPath = Join-Path $ConfigDir ("agent.{0}.json" -f $t.id)
    ($cfg | ConvertTo-Json -Depth 5) | Set-Content -Encoding UTF8 -Path $cfgPath
    # ACL 收紧（本机盘）
    icacls $cfgPath /inheritance:r /grant:r "Administrators:R" /grant:r "$env:USERNAME:R" | Out-Null
    Write-Host ("[CFG] {0} -> {1}" -f $t.id, $cfgPath) -ForegroundColor DarkGray

    # check-config
    & $py $agentPy --check-config --config $cfgPath
    if ($LASTEXITCODE -ne 0) { $summary += [pscustomobject]@{ id = $t.id; check = "FAIL"; push = "-" }; continue }
    if ($NoTestPush) { $summary += [pscustomobject]@{ id = $t.id; check = "OK"; push = "skip" }; continue }

    # 单轮试推
    & $py $agentPy --once --config $cfgPath
    $pushOk = ($LASTEXITCODE -eq 0)
    Write-Host ("[PUSH] {0}: exit {1}" -f $t.id, $LASTEXITCODE) -ForegroundColor $(if ($pushOk) { "Green" } else { "Red" })
    $summary += [pscustomobject]@{ id = $t.id; check = "OK"; push = $(if ($pushOk) { "OK" } else { "FAIL($LASTEXITCODE)" }) }

    # Task Scheduler 常驻（开机自启；任务名含项目 id，避免 4 任务同名冲突）
    if (-not $SkipScheduler) {
        $tn = "AIOS Agent - {0}" -f $t.id
        $tr = "`"$py`" `"$agentPy`" --config `"$cfgPath`""
        schtasks /Delete /TN $tn /F 2>$null | Out-Null
        $r = schtasks /Create /TN $tn /SC ONSTART /RU SYSTEM /TR $tr 2>&1
        if ($LASTEXITCODE -ne 0) { Write-Host ("[SCHED] {0} 创建失败: {1}" -f $tn, $r) -ForegroundColor Red }
        else { Write-Host ("[SCHED] 已启动: {0}" -f $tn) -ForegroundColor Green }
    }
}

# --- 3. 汇总 -------------------------------------------------------------------
Write-Host "`n=== 部署汇总 ===" -ForegroundColor Cyan
$summary | Format-Table | Out-String | Write-Host
Write-Host "===" -ForegroundColor Cyan
Write-Host @"

服务端验证（在监控端 Linux 执行）：
  1) curl -s http://localhost:3113/api/status | python3 -c "import json,sys; d=json.load(sys.stdin); [print(p['id'],'offline=',('离线' in (p.get('error') or ''))) for p in d['projects']]"
  2) dispatcher 可达性：
     python3 <aibase>/kit/tools/dispatcher/dispatcher.py list --config <aimonitor>/config/projects.json
     # 预期：4 个 agent 条目 REACHABLE=yes（刚 push 过，last_seen 应新鲜）

回滚：
  schtasks /Delete /TN "AIOS Agent - <project-id>" /F
  Remove-Item C:\etc\aios-agent\agent.<project-id>.json
"@ -ForegroundColor DarkGray
