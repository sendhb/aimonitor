"""downlink.py — 本地 subprocess 下行执行适配器（kit/tools/dispatcher/ 执行层）。

TASK-073：Phase 3 调度器下行执行链。

职责（保持适配器形态，未来 SSH/A2A 换实现不换接口）：
- 校验项目路径属于注册表（拒绝伪造 path，不执行）；
- 在目标项目目录内执行命令；
- 收集 exit code + stdout/stderr；失败以 CommandResult 回报，不静默吞错。

硬约束：本模块只触发既有工具链（task CLI / autoloop-*），不直接写任何
任务文件——任务文件由目标项目本地 CLI 落盘（中央不直写远端 FS）。

零外部依赖（仅 stdlib）。
"""
import os
import shlex
import subprocess
from dataclasses import dataclass

from registry import RegistryEntry, AGENT_TRANSPORT  # noqa: E402


class DownlinkError(Exception):
    """路径校验/目录缺失/进程无法启动时抛出；CLI 层打印后 exit 非 0。"""


@dataclass(frozen=True)
class CommandResult:
    """单条下行命令的执行结果（exit code + 输出 + 是否超时）。"""

    entry_id: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def find_entry(entries, path):
    """在注册表中精确匹配项目路径；找不到或为 agent 条目 → DownlinkError。

    路径规范化（abspath + normpath）后比较，杜绝 `../` 之类伪造路径绕过。
    """
    if not path:
        raise DownlinkError("路径为空（需要 --path <项目路径>）")
    norm = os.path.normpath(os.path.abspath(path))
    for entry in entries:
        if os.path.normpath(os.path.abspath(entry.path)) == norm:
            if entry.transport == AGENT_TRANSPORT:
                raise DownlinkError(
                    f"agent 传输条目不支持本地执行（拒绝）: {entry.id} ({entry.path})"
                )
            return entry
    raise DownlinkError(f"路径不在注册表中（拒绝执行）: {path}")


def validate(entry):
    """执行前校验：项目目录必须存在。不存在 → DownlinkError。"""
    if not os.path.isdir(entry.path):
        raise DownlinkError(f"项目目录不存在: {entry.path}")


def run(entry, command, args=None, timeout=1800):
    """在 entry.path 目录内执行 command；返回 CommandResult。

    参数:
        entry: registry.RegistryEntry（必须来自注册表；find_entry 兜底校验）。
        command: 可执行命令字（如 sys.executable / "bash"）。
        args: 命令参数列表（如 ["kit/cli/task", "start", "TASK-001"]）。
        timeout: 超时秒数；超时返回 timed_out=True、exit_code=-1。
    返回:
        CommandResult；进程非 0 退出不抛异常（由调用方决定失败语义）。
    """
    validate(entry)
    argv = [command] + list(args or [])
    cmd_str = " ".join(shlex.quote(a) for a in argv)
    try:
        proc = subprocess.run(
            argv,
            cwd=entry.path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(
            entry_id=entry.id,
            command=cmd_str,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    except subprocess.TimeoutExpired as e:
        return CommandResult(
            entry_id=entry.id,
            command=cmd_str,
            exit_code=-1,
            stdout=(e.stdout or "").decode("utf-8", "replace")
            if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=(e.stderr or "").decode("utf-8", "replace")
            if isinstance(e.stderr, bytes) else (e.stderr or ""),
            timed_out=True,
        )
    except OSError as e:
        raise DownlinkError(f"命令启动失败（{entry.id}）: {e}") from e
