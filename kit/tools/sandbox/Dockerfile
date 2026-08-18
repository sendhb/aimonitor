# 最小无网络沙箱镜像 —— 用来跑任意 AI CLI（Claude Code / Codex CLI / Cursor CLI / …）
#
# 默认通过 cli/sandbox-run 以 --network none 启动：即使 agent 拿到了敏感数据
# （②）又在处理不可信输入（①），没有网络也没法外传/回连（③），直接落地
# aios/governance/security-policy.md 的 Rule of Two，跟装的是哪个 AI 工具无关。
#
# 按需在下面追加你要用的 AI CLI 的安装步骤（例如 npm install -g @xxx/yyy 或
# curl 官方安装脚本），保持这层镜像本身工具无关——它是"沙箱"，不是某个
# AI 工具专属的镜像。
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 python3-pip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---- 在此追加你的 AI CLI 安装步骤 ----
# RUN curl -fsSL https://example.com/install.sh | sh

WORKDIR /workspace
ENTRYPOINT ["/bin/bash", "-lc"]
