#!/usr/bin/env bash
# ============================================================
# llm.sh — LLM 调用抽象（供 cli/autoloop-* 使用，TASK-009）
#
# 让无人值守循环可配置 LLM provider：
#   - claude   : 调用 claude CLI（Anthropic 原生，需 --dangerously-skip-permissions）
#   - deepseek : 调用 OpenAI 兼容 API（baseUrl/key/model 读自 ~/.pi/agent/models.json，
#               即 pi coding agent 的现有配置，key 只在本进程内使用，绝不打印）
#
# 用法:
#   cli/lib/llm.sh <provider> <prompt>
#
# 安全：
#   - 密钥不写代码/不打印；deepseek key 从 ~/.pi/agent/models.json 读取
#   - 沙箱内使用时需挂载该配置（见 cli/sandbox-review）
# ============================================================
set -euo pipefail

PROVIDER="${1:-}"
PROMPT="${2:-}"
if [ -z "$PROVIDER" ] || [ -z "$PROMPT" ]; then
  echo "用法: cli/lib/llm.sh <provider> <prompt>" >&2
  exit 1
fi

case "$PROVIDER" in
  claude)
    exec claude -p "$PROMPT" --dangerously-skip-permissions
    ;;
  deepseek)
    # codewhale agent CLI（原 deepseek-tui，TASK-009）：等效于 claude -p --dangerously-skip-permissions
    # key 优先用环境变量 DEEPSEEK_API_KEY，否则从 pi 配置 models.json 读取
    # （解释器分派 TASK-002：Windows 商店 stub / 缺失时回退 python）
    if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f "$HOME/.pi/agent/models.json" ]; then
      # shellcheck source=cli/lib/pick_python.sh
      source "$(dirname "${BASH_SOURCE[0]}")/pick_python.sh"
      # SMELL-001（TASK-012）：set -e 下无 || 保护会静默退出；降级为警告并继续
      py="$(pick_python)" || { echo "⚠ llm.sh: 缺少 python3/python，无法从 pi 配置读取 deepseek key" >&2; py=""; }
      if [ -n "$py" ]; then
        DEEPSEEK_API_KEY=$("$py" -c "import json; d=json.load(open('$HOME/.pi/agent/models.json')); print(d['providers']['deepseek']['apiKey'])" 2>/dev/null || true)
        export DEEPSEEK_API_KEY
      fi
    fi
    exec codewhale exec --auto "$PROMPT"
    ;;
  pi)
    # pi coding agent（TASK-009）: AI coding assistant with read/bash/edit/write tools
    # 配置优先用环境变量或 ~/.pi/agent/settings.json（已通过 sandbox-review :ro 挂载）
    exec pi -p "$PROMPT" --no-session
    ;;
  *)
    echo "✗ 未知 provider: $PROVIDER（支持 claude | deepseek | pi）" >&2
    exit 1
    ;;
esac
