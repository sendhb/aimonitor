"""agent_runtime.py — 读取被监控项目 runtime/ 状态（kit/tools/agent/ 读取层）。

供 TASK-024 payload 构造使用：输入项目路径，输出该项目的 runtime 快照：

    {
      "tasks":              [{"name": "TASK-001-x.md", "content": "..."}, ...] | None,
      "focus":              "CURRENT_FOCUS.md 原文" | None,
      "heartbeats":         [{"file": "autoloop-coder.heartbeat", "mtime": 1786892280.0}, ...] | None,
      "events":             [{"name": "autoloop-coder-events.jsonl", "content": "..."}, ...] | None,
      "verification_count": 3 | None,
      "review_count":       1 | None,
    }

容错约定（任务要求"文件缺失容错为 null"）：
- 目录缺失（如项目还没有 runtime/）→ 对应字段为 None（无数据，区别于"0 个"）。
- 目录存在但没有匹配文件 → 空列表 / 0（有数据但为空）。
- 读取永不抛异常：内容用 errors="replace" 解码，OSError 跳过，mtime 取 st_mtime。
"""
import os
import json
import time


def _read_text(path):
    """读文件原文；不可读/非 UTF-8 时也不抛异常（errors=replace）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _listdir_sorted(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def _count_files(d, prefix, suffix):
    """统计目录下 prefix* + suffix 文件数；目录缺失返回 None（区别于 0）。"""
    if not os.path.isdir(d):
        return None
    return sum(1 for fn in _listdir_sorted(d)
               if fn.startswith(prefix) and fn.endswith(suffix))


def read_project_runtime(project_path):
    """读取项目 runtime/ 快照。

    参数:
        project_path: 被监控项目绝对路径（可以是还不存在的目录——全部字段按缺失容错）。
    返回:
        上面 docstring 形状的 dict；永不抛异常。
    """
    rt = os.path.join(project_path, "runtime")

    # tasks: runtime/tasks/TASK-*.md（文件名 + 原文，按文件名排序）
    tasks_dir = os.path.join(rt, "tasks")
    if os.path.isdir(tasks_dir):
        tasks = []
        for fn in _listdir_sorted(tasks_dir):
            if fn.startswith("TASK-") and fn.endswith(".md"):
                tasks.append({"name": fn, "content": _read_text(os.path.join(tasks_dir, fn))})
        snapshot_tasks = tasks
    else:
        snapshot_tasks = None

    # focus: runtime/states/CURRENT_FOCUS.md 原文
    focus_path = os.path.join(rt, "states", "CURRENT_FOCUS.md")
    snapshot_focus = _read_text(focus_path) if os.path.isfile(focus_path) else None

    # heartbeats / events: 都在 runtime/logs/ 下
    logs_dir = os.path.join(rt, "logs")
    if os.path.isdir(logs_dir):
        heartbeats = []
        events = []
        for fn in _listdir_sorted(logs_dir):
            path = os.path.join(logs_dir, fn)
            if fn.endswith(".heartbeat"):
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                heartbeats.append({"file": fn, "mtime": st.st_mtime})
            elif fn.endswith("-events.jsonl"):
                events.append({"name": fn, "content": _read_text(path)})
        snapshot_heartbeats = heartbeats
        snapshot_events = events
    else:
        snapshot_heartbeats = None
        snapshot_events = None

    # 计数: runtime/verification|reviews 下 VERIFY-*.md / REVIEW-*.md
    verification_count = _count_files(
        os.path.join(rt, "verification"), "VERIFY-", ".md"
    )
    review_count = _count_files(
        os.path.join(rt, "reviews"), "REVIEW-", ".md"
    )

    return {
        "tasks": snapshot_tasks,
        "focus": snapshot_focus,
        "heartbeats": snapshot_heartbeats,
        "events": snapshot_events,
        "verification_count": verification_count,
        "review_count": review_count,
    }


# ---------------------------------------------------------------------------
# TASK-066 事件流（outbox）读取层
# ---------------------------------------------------------------------------

TASK_EVENTS_FILE = "task-events.jsonl"
PUSH_CURSOR_FILE = ".push-cursor"


def _logs_dir(project_path):
    return os.path.join(project_path, "runtime", "logs")


def read_task_events(project_path):
    """读取 runtime/logs/task-events.jsonl → [parsed dict]（跳过损坏行）。

    与 read_project_runtime 容错语义一致：
    - runtime/logs 缺失或 task-events.jsonl 缺失 → None（无数据）；
    - 文件存在但为空/全损坏 → []（有数据但为空）。
    每行必须是含整数 seq 的 JSON 对象才被接受。永不抛异常。
    """
    path = os.path.join(_logs_dir(project_path), TASK_EVENTS_FILE)
    raw = _read_text(path)
    if raw is None:
        return None
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("seq"), int) and not isinstance(data.get("seq"), bool) and data.get("seq") >= 1:
            events.append(data)
    return events


def read_push_cursor(project_path):
    """读取 runtime/logs/.push-cursor → int（已确认推送的最大 seq）；缺失/非法 → None。"""
    path = os.path.join(_logs_dir(project_path), PUSH_CURSOR_FILE)
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    seq = data.get("seq") if isinstance(data, dict) else None
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


def write_push_cursor(project_path, seq):
    """原子写游标文件（tmp + os.replace）。seq 必须为非负整数。"""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError(f"push cursor seq 必须是非负整数（got {seq!r}）")
    logs = _logs_dir(project_path)
    os.makedirs(logs, exist_ok=True)
    target = os.path.join(logs, PUSH_CURSOR_FILE)
    tmp = os.path.join(logs, PUSH_CURSOR_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seq": seq, "updated": time.time()}, f, sort_keys=True)
    os.replace(tmp, target)
