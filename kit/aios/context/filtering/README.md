# filtering — Filter 策略（机械排除）

排除：`aios.config.yaml` 的 `generated_dirs`、二进制/隐藏文件、不可读文件；
排除结果在装配清单尾部汇总（filtered 行），机械生效不靠自觉。
实现：[kit/cli/lib/context_loader.py](../../../cli/lib/context_loader.py)（TASK-094）
