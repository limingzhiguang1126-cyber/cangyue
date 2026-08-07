# -*- coding: utf-8 -*-
"""CNB 云端 Telegram 测试推送 + 连通性诊断。

在 CNB 云原生构建（定时任务 / Web 按钮 / api_trigger）中运行：
  1. 打印 Telegram 相关配置是否存在（不泄露 token）
  2. 探测 api.telegram.org 连通性（云端在国内，直连大概率超时）
  3. 若直连失败，提示 TELEGRAM_API_BASE 海外代理方案
  4. 若可连通则发一条测试消息到你的 chat_id

用法（在流水线中，密钥来自 imports 注入）：
    python scripts/tg_test_push_cloud.py

环境变量：
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  必填（密钥仓库 telegram.yml 注入）
    TELEGRAM_API_BASE                      可选，Telegram Bot API 代理地址（海外可达时）
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys

import requests

from src.notifier.telegram_notifier import TelegramNotifier  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402

setup_logging()
logger = logging.getLogger("fin-alert.tg-test")


def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


def _check_api_base(api_base: str, token: str) -> bool:
    """探测一个 Bot API 地址是否可达 + token 是否有效。"""
    try:
        resp = requests.get(f"{api_base}/bot{token}/getMe", timeout=12)
        data = resp.json()
        if data.get("ok"):
            bot = data.get("result", {})
            print(_green(f"  ✓ API 可达且 Token 有效: @{bot.get('username', '?')}"))
            return True
        print(_yellow(f"  ✗ API 可达但 Token 无效: {data.get('description', '')}"))
        return False
    except requests.exceptions.RequestException as exc:
        print(_red(f"  ✗ API 不可达: {exc}"))
        return False


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    api_base = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

    print("=" * 60)
    print(" CNB 云端 Telegram 测试推送")
    print("=" * 60)
    print(f"  token_set={bool(token)} token_len={len(token) if token else 0}")
    print(f"  chat_set={bool(chat)} chat_id={chat if chat else '(empty)'}")
    print(f"  api_base={api_base}")

    if not token or not chat:
        print(_red("✗ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未注入，无法测试。"))
        print("  请确认密钥仓库 telegram.yml 配置正确且 imports 指向它。")
        return 1

    # 1) 先测 DNS + TCP 443 直连官方地址
    print("\n[1/3] 探测官方 api.telegram.org 连通性")
    dns_ok = False
    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo("api.telegram.org", 443, proto=socket.IPPROTO_TCP)})
        print(f"  DNS 解析到: {ips}")
        dns_ok = True
    except Exception as exc:
        print(_red(f"  DNS 解析失败: {exc}"))

    tcp_ok = False
    if dns_ok:
        for ip in ips[:3]:
            try:
                s = socket.create_connection((ip, 443), timeout=5)
                s.close()
                tcp_ok = True
                print(f"  TCP 443 连通: {ip}")
                break
            except Exception:
                continue
    if not tcp_ok:
        print(_red("  TCP 443 连接 api.telegram.org 超时（云端网络无法直连 Telegram）。"))

    # 2) 尝试 getMe
    print("\n[2/3] 校验当前 api_base 是否可达")
    if not _check_api_base(api_base, token):
        print(_yellow(""))
        print(_yellow("  → 云端（CNB 构建机）位于国内网络，直连 api.telegram.org 通常被墙。"))
        print(_yellow("    解决办法（任选其一）："))
        print(_yellow("      ① 给流水线设置 TELEGRAM_API_BASE 指向一个海外可达的 Telegram Bot API 代理；"))
        print(_yellow("      ② 在本机能访问 Telegram 的电脑/服务器上跑 scripts/tg_test_push.py 常驻推送；"))
        print(_yellow("      ③ 自托管 Runner 接入海外构建机（需能访问 CNB 站点 + Telegram）。"))
        print(_yellow(""))
        print(_yellow("  → 若你只是想验证「电报能否收到消息」，请在本机（能访问 Telegram 的网络）运行："))
        print(_yellow("      python scripts/tg_test_push.py"))
        return 2

    # 3) 发测试消息
    print(f"\n[3/3] 发送测试消息 → chat_id={chat}")
    notifier = TelegramNotifier(bot_token=token, chat_id=chat, api_base=api_base)
    text = (
        "✅ [CNB 云端] 电报推送链路测试成功！\n"
        "如果你在 Telegram 看到这条消息，说明 Bot Token 与 chat_id 配置正确、"
        "云端推送链路已打通 🎉"
    )
    ok = notifier.send_text(text, parse_mode="")
    if ok:
        print(_green("  ✓ 测试消息发送成功！请到 Telegram 查看。"))
        return 0
    print(_red("  ✗ 发送失败，请查看上方日志。"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
