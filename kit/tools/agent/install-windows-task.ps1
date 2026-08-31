#Requires -Version 5.1
<#
.SYNOPSIS
  注册 AIOS 遥测 agent（kit/tools/agent/agent.py）为 Windows 计划任务，实现常驻持续监控。

.DESCRIPTION
  创建计划任务 "AIOS Agent"（可 -TaskName 覆盖）：
    - 触发器：开机启动（AtStartup），开机即跑，无需用户登录
    - 运行账户：SYSTEM（缺省；-RunAsUser 可覆盖）
    - 设置：失败自动重启（3 次 × 5 分钟）、运行时长不限（防 Task Scheduler
      默认 72h 限制杀死常驻进程）、多实例忽略新（防重复进程）
  启动后 agent 按 agent.json 的 poll_interval_seconds（缺省 30s）持续把被监控
  项目的 runtime/ 状态（TASK/焦点/心跳/事件/验证计数）推送到 aimonitor ingest。

  重复运行安全：先移除同名旧任务再重建（幂等）。

.PARAMETER Python
  python.exe 绝对路径。缺省自动探测 PATH；建议显式指定（SYSTEM 会话 PATH 可能不同）。

.PARAMETER TaskName
  计划任务名，缺省 "AIOS Agent"。

.PARAMETER Config
  agent.json 绝对路径。缺省 = 仓库根 agent.json（脚本在 kit/tools/agent/ 时上溯 3 级）。

.PARAMETER LogPath
  输出日志文件。缺省 = <仓库根>\runtime\logs\agent-aimonitor.log。

.PARAMETER RunAsUser
  运行账户。缺省 SYSTEM（开机即跑、无需登录）。如改用当前用户交互式运行，
  传当前用户名并以交互登录方式创建（任务将仅在该用户登录时运行）。

.EXAMPLE
  # 默认：SYSTEM + 仓库根 agent.json
  powershell -ExecutionPolicy Bypass -File install-windows-task.ps1

  # 指定解释器与任务名
  powershell -ExecutionPolicy Bypass -File install-windows-task.ps1 `
      -Python "C:\Users\me\AppData\Local\Programs\Python\Python312\python.exe"

.NOTES
  卸载：Unregister-ScheduledTask -TaskName "AIOS Agent" -Confirm:$false
  查看：Get-ScheduledTask -TaskName "AIOS Agent"; Get-ScheduledTaskInfo -TaskName "AIOS Agent"
  日志：Get-Content -Wait <LogPath>  （每 30s 一条 "推送成功/失败"）
#>
param(
    [string]$Python = "",
    [string]$TaskName = "AIOS Agent",
    [string]$Config = "",
    [string]$LogPath = "",
    [string]$RunAsUser = ""
)

$ErrorActionPreference = "Stop"

# 脚本所在目录 = kit/tools/agent；仓库根 = 上溯 3 级
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir "..\..\..")).Path

# 1. 定位 python（绝对路径，避免 SYSTEM 会话 PATH 差异）
if (-not $Python) {
    $py = Get-Command "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
    if (-not $py) { $py = Get-Command "python" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source }
    if (-not $py) { throw "未找到 python；请用 -Python 指定 python.exe 绝对路径" }
    $Python = $py
}
if (-not (Test-Path $Python)) { throw "python 不存在: $Python" }

# 2. 定位 agent.py / agent.json / 日志
$AgentPy = Join-Path $ScriptDir "agent.py"
if (-not (Test-Path $AgentPy)) { throw "agent.py 不存在: $AgentPy" }
if (-not $Config) { $Config = Join-Path $RepoRoot "agent.json" }
if (-not (Test-Path $Config)) { throw "agent.json 不存在: $Config" }
if (-not $LogPath) {
    $LogDir = Join-Path $RepoRoot "runtime\logs"
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $LogPath = Join-Path $LogDir "agent-aimonitor.log"
}

# 3. 清理旧任务（幂等；先停运行中实例，避免进程仍持有日志文件导致新实例写冲突）
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Start-Sleep -Seconds 2
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[x] 已停止并移除旧任务: $TaskName"
}

# 4. 构造 Action / Trigger / Principal / Settings
#    cmd /c 外层双引号包裹整条命令（经典模式）：
#    cmd 对含多引号+特殊字符(>> 等)的 /c 参数会剥掉首尾引号；
#    用 '/c "...整条命令..."' 让剥壳后恰好还原为正确命令行。
$cmdArgs = '/c ""' + $Python + '" -u "' + $AgentPy + '" --config "' + $Config + '" >> "' + $LogPath + '" 2>&1"'
$action    = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdArgs -WorkingDirectory $RepoRoot
$trigger   = New-ScheduledTaskTrigger -AtStartup

if ($RunAsUser) {
    # 指定账户：要求该账户可登录（交互式），登录后才运行
    $principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType Interactive -RunLevel Highest
} else {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
}

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "AIOS telemetry agent: continuously push runtime state of $RepoRoot to aimonitor" `
    -Force | Out-Null

# 5. 启动并确认
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$t    = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "[ok] 任务已注册并启动: $TaskName"
Write-Host "     状态: $($t.State)"
Write-Host "     上次运行: $($info.LastRunTime) | 上次结果: $($info.LastTaskResult)"
Write-Host "     运行账户: $($principal.UserId)"
Write-Host "     python : $Python"
Write-Host "     config : $Config"
Write-Host "     日志   : $LogPath"
Write-Host "     命令   : cmd.exe $cmdArgs"
Write-Host ""
Write-Host "     # 跟踪日志（每 30s 一条推送结果）:"
Write-Host "     Get-Content -Wait '$LogPath'"
