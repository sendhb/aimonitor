# tools/sandbox — 无网络容器沙箱

> 见 [tools/README.md](../README.md)

对应 `aios/governance/security-policy.md` 的 Rule of Two：跟具体 AI 工具无关的
执行层隔离——容器默认没有网络（`--network none`）、根文件系统只读，只有
挂载进去的项目目录可写。即使容器里的 agent 同时具备"处理不可信输入"和
"访问敏感数据"，缺了网络这条腿也没法外传。

## 用法

```bash
cli/sandbox-run -- <command...>            # 默认无网络
cli/sandbox-run --network -- <command...>  # 显式需要联网时才开（例如装依赖）
```

`tools/sandbox/Dockerfile` 是一个精简的基础镜像（git/python3/curl），故意不
预装任何具体 AI CLI——按需在 Dockerfile 里追加你要用的工具的安装步骤，保
持这层"沙箱"本身工具无关。
