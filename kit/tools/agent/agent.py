#!/usr/bin/env python3
"""kit/tools/agent/ — AIOS 遥测推送 agent（入口）。

用法:
    python3 agent.py --check-config [--config agent.json]         # 只校验配置（exit 0/1）
    python3 agent.py [--config agent.json] [--once] \
                     [--interval N] [--quiet]                    # 启动推送（常驻或单轮）
    python3 agent.py --register [--config agent.json]            # 注册状态管理（构建申请/等待审批/已注册）
    python3 agent.py --register --status [--config agent.json]   # 查看注册状态
    python3 agent.py --register --reset [--config agent.json]    # 重置注册状态（pending → unregistered）

常驻模式按 agent.json 的 poll_interval_seconds（默认 30s）间隔轮询；
--once 推送一轮后退出（cron / systemd timer）；--interval N 覆盖轮询间隔；
--quiet 只输出错误不输出常规日志；SIGINT/SIGTERM 干净退出（exit 0）。

分层：配置加载/校验由 agent_config 负责（TASK-022），主循环由 agent_loop
负责（TASK-027），注册状态机由 agent_register 负责（TASK-042），均 stdlib-only。
"""
import argparse
import math
import os
import sys
import time

# Windows 管道/日志重定向下默认按 GBK 写中文会报 UnicodeEncodeError 或输出不一致；
# 统一 UTF-8（TASK-002/008：控制台走 PEP 528 WinAPI 不受影响，管道/文件确定性可预期）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_config as cfg_lib  # noqa: E402
import agent_loop as loop_lib  # noqa: E402
import agent_register as reg_lib  # noqa: E402


def _positive_interval(value):
    """argparse type：正有限秒数；非法抛 ArgumentTypeError（usage + exit 2）。"""
    try:
        fv = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--interval 必须是数字（秒）: {value!r}")
    if not math.isfinite(fv) or fv <= 0:
        raise argparse.ArgumentTypeError(f"--interval 必须大于 0（秒）: {value!r}")
    return fv


def _cmd_register(cfg, path, args, stop_fn=None):
    """处理 --register 子命令：查看状态 / 构建注册请求 / 重置恢复。

    参数:
        stop_fn: 停止判定 callable（main() 传入 StopFlag；信号请求停止时
                 轮询循环干净退出，见 FIND-001 返工）

    状态机语义（REVIEW-r3 返工，ISSUE-02）：
    - 契约（aimonitor TASK-046）就绪前，unregistered 只"构建"注册请求，
      不伪造 req_id、不写入 pending、不声称"已提交"。
    - pending 进入轮询循环（TASK-043 范围：run_registration_polling）。
    - --register --reset 恢复被污染/卡死的 pending（ISSUE-02b）。
    """
    reg = reg_lib.RegistrationState(cfg)
    status = reg.state

    # --register --reset：恢复被污染/卡死的 pending
    if args.register_reset:
        if status == reg_lib.STATE_ACTIVE:
            print("agent: 已注册（active），不允许 reset（需重新注册请手动编辑 agent.json）",
                  file=sys.stderr)
            return 1
        if status == reg_lib.STATE_UNREGISTERED:
            print("agent: 已是 unregistered，无需 reset")
            return 0
        reg.state = reg_lib.STATE_UNREGISTERED
        reg.req_id = None
        reg.request_key = None
        reg.save(path)
        print("agent: 已重置为 unregistered（清除 req_id/request_key）")
        return 0

    if args.register_status:
        print(f"agent: 注册状态: {status}")
        if status == reg_lib.STATE_PENDING and reg.req_id:
            print(f"agent: 请求 ID: {reg.req_id}")
        if status == reg_lib.STATE_ACTIVE:
            # ISSUE-01：不输出 token（含前缀），避免密钥泄露到终端/日志
            print("agent: 已配置 token")
        return 0

    # --register（无 --status / --reset）
    if status == reg_lib.STATE_UNREGISTERED:
        # TASK-014 接线：契约（aimonitor MONITOR-SPEC §3.2）已就绪 → 构建 payload →
        # submit_register() POST /api/register → 成功写 pending（req_id/request_key 入 agent.json）
        # → 进入轮询（TASK-043 路径）。失败时不修改 agent.json（不伪造 req_id/pending）。
        request_key = reg_lib.generate_request_key()
        payload = reg_lib.build_register_payload(cfg["projects"][0], request_key)
        print(f"agent: 提交注册申请至 aimonitor 服务端（项目: {payload['project_id']}）...",
              flush=True)
        try:
            resp = reg_lib.submit_register(cfg["server_url"], payload)
        except reg_lib.RegisterConflictError as e:
            print(f"agent: {e}", file=sys.stderr)
            return 1
        except reg_lib.PollRejectedError as e:
            print(f"agent: {e}", file=sys.stderr)
            return 1
        except reg_lib.PollRetryableError as e:
            print(f"agent: {e}（可稍后重试）", file=sys.stderr)
            return 1
        req_id = (resp or {}).get("req_id")
        if not req_id:
            print("agent: 服务端注册响应缺少 req_id，未修改 agent.json", file=sys.stderr)
            return 1
        # 写 pending 并保存（request_key 用于轮询绑定身份，机密不打印）
        reg.transition_to(reg_lib.STATE_PENDING, req_id=req_id, request_key=request_key)
        reg.save(path)
        print(f"agent: 注册申请已提交（req_id: {req_id}），等待管理员审批...", flush=True)
        # 重新加载 cfg：轮询器从 cfg 读 req_id/request_key（TASK-014 接线）
        cfg = cfg_lib.load_config(path)
        result = loop_lib.run_registration_polling(cfg, path, stop_fn=stop_fn)
        if result == "approved":
            return 0
        if result == "stopped":
            # 信号请求停止（SIGINT/SIGTERM）→ 与常驻模式契约一致，干净退出（exit 0）
            return 0
        print(f"agent: 注册未完成（{result}），请检查后重新注册", file=sys.stderr)
        return 1

    if status == reg_lib.STATE_PENDING:
        rid = reg.req_id or "unknown"
        # flush=True：确保"正在等待审批"先于轮询的 stderr 输出（ISSUE-10）
        print(f"agent: 正在等待审批（req_id: {rid}），进入轮询...", flush=True)
        # TASK-043 范围：轮询领 token 并切换正式推送
        # FIND-001 返工：传入 stop_fn，SIGINT/SIGTERM 下轮询循环干净退出
        # （不抛 KeyboardInterrupt traceback）；approved 后 run_forever 复用同一 flag
        result = loop_lib.run_registration_polling(cfg, path, stop_fn=stop_fn)
        if result == "approved":
            return 0
        if result == "stopped":
            # 信号请求停止（SIGINT/SIGTERM）→ 与常驻模式契约一致，干净退出（exit 0）
            return 0
        print(f"agent: 注册未完成（{result}），请检查后重新注册", file=sys.stderr)
        return 1

    if status == reg_lib.STATE_ACTIVE:
        print("agent: 已注册，无需重复注册")
        return 0

    print(f"agent: 未知状态: {status}", file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIOS 遥测推送 agent")
    parser.add_argument("--config", default=None,
                        help="agent.json 路径（默认当前目录 ./agent.json）")
    parser.add_argument("--check-config", action="store_true",
                        help="只加载并校验配置，不启动轮询")
    parser.add_argument("--once", action="store_true",
                        help="单轮：推送一轮后退出（cron / systemd timer 场景）")
    parser.add_argument("--interval", type=_positive_interval, default=None,
                        help="轮询间隔（秒），覆盖 agent.json 的 poll_interval_seconds")
    parser.add_argument("--quiet", action="store_true",
                        help="安静模式：只输出错误，不输出常规日志")
    parser.add_argument("--register", action="store_true",
                        help="注册状态管理：发起注册或查看状态")
    parser.add_argument("--status", dest="register_status", action="store_true",
                        help="与 --register 配合使用，显示当前注册状态")
    parser.add_argument("--reset", dest="register_reset", action="store_true",
                        help="与 --register 配合使用，重置注册状态（pending → unregistered）")
    args = parser.parse_args(argv)

    path = args.config or os.path.join(os.getcwd(), "agent.json")
    try:
        cfg = cfg_lib.load_config(path)
    except cfg_lib.AgentConfigError as e:
        print(f"agent: 配置错误: {e}", file=sys.stderr)
        return 1

    if args.check_config:
        print(f"agent: 配置 OK — {cfg['server_url']}，"
              f"{len(cfg['projects'])} 个项目，"
              f"poll_interval_seconds={cfg['poll_interval_seconds']}")
        return 0

    # FIND-001 返工：信号处理器在 --register 分支之前安装——注册轮询阶段
    # （pending 等待审批）与切换后的正式推送（run_forever）共享同一 StopFlag，
    # SIGINT/SIGTERM 均干净退出（exit 0），不抛 KeyboardInterrupt traceback。
    flag = loop_lib.StopFlag()
    loop_lib.install_signal_handlers(flag)

    if args.register:
        return _cmd_register(cfg, path, args, stop_fn=flag)

    interval = args.interval if args.interval is not None else cfg["poll_interval_seconds"]
    log = loop_lib.AgentLog(quiet=args.quiet)

    if args.once:
        pushed, skipped, failed = loop_lib.poll_once(
            cfg, states={}, log=log, clock=time.time
        )
        log.info(f"单轮结束：成功 {pushed}，跳过 {skipped}，失败 {failed}")
        if flag.stopped:
            log.info("收到信号，干净退出")
        return 0

    loop_lib.run_forever(cfg, interval, states={}, log=log, stop_fn=flag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
