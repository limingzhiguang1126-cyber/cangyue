# -*- coding: utf-8 -*-
"""Telegram 推送链路一键测试脚本。

用途：验证「电报能否收到推送」——在你自己的电脑（能正常访问 Telegram 的环境）上跑一下，
会先校验 Bot Token 是否有效（getMe），再往你的 chat_id 发一条测试消息（sendMessage）。

用法（任选其一）：
    1. 先配好 .env（复制 .env.example 填 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）：
        python scripts/tg_test_push.py
    2. 或直接用环境变量传参，不写文件：
        TELEGRAM_BOT_TOKEN=123456:ABC... TELEGRAM_CHAT_ID=654321 python scripts/tg_test_push.py
    3. 也支持 --token / --chat 参数直接指定：
        python scripts/tg_test_push.py --token 123456:ABC... --chat 654321

返回码：
    0 = 测试消息发送成功
    1 = 配置缺失 / Token 无效 / 发送失败

说明：
    - 本脚本直连官方 https://api.telegram.org（不走 CNB 云端，不受构建机网络限制）。
    - 如果本脚本在你的电脑上能发出消息、电报也收到了，说明 Token / chat_id / 电报链路全部正常，
      收不到推送的瓶颈在「云端网络无法访问 api.telegram.org」（CNB 构建机在国内，api.telegram.org 被墙）。
    - 若想给 CNB 云端也打通推送，可用 TELEGRAM_API_BASE 指向一个海外 Telegram Bot API 代理
      （见 README「电报推送网络问题」一节），或改用本地常驻守护进程。
"""

from __future__ import annotations

import argparse
import os
import sys

# 兼容 .env 本地文件（不强制依赖 python-dotenv 时手动兜底解析）
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:  # pragma: no cover
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(_env_path):
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

import requests  # noqa: E402

_DEFAULT_API = "https://api.telegram.org"


def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Telegram 推送链路一键测试")
    ap.add_argument("--token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""), help="Bot Token（@BotFather 获取）")
    ap.add_argument("--chat", default=os.getenv("TELEGRAM_CHAT_ID", ""), help="chat_id（@userinfobot 可查）")
    ap.add_argument("--api-base", default=os.getenv("TELEGRAM_API_BASE", _DEFAULT_API),
                    help="Telegram Bot API 地址（默认官方 api.telegram.org，可指向海外代理）")
    args = ap.parse_args(argv)

    token = (args.token or "").strip()
    chat = (args.chat or "").strip()
    api_base = (args.api_base or _DEFAULT_API).rstrip("/")

    print("=" * 60)
    print(" Telegram 推送链路一键测试")
    print("=" * 60)
    if not token:
        print(_red("✗ 缺少 Bot Token"))
        print("  请设置 TELEGRAM_BOT_TOKEN（@BotFather 创建 Bot 后获取），")
        print("  或在 .env 中填写，或用 --token 参数传入。")
        return 1
    if not chat:
        print(_red("✗ 缺少 chat_id"))
        print("  请设置 TELEGRAM_CHAT_ID（给 @userinfobot 发消息即可查询），")
        print("  或在 .env 中填写，或用 --chat 参数传入。")
        return 1

    print(f"\n[1/2] 校验 Bot Token（getMe）→ {api_base}")
    try:
        resp = requests.get(f"{api_base}/bot{token}/getMe", timeout=15)
        data = resp.json()
        if data.get("ok"):
            bot = data.get("result", {})
            print(_green(f"  ✓ Token 有效，Bot 用户名: @{bot.get('username', '?')}"))
        else:
            print(_red(f"  ✗ Token 无效: {data.get('description', resp.text[:200])}"))
            print("    请确认 Token 是否正确（@BotFather → /mybots 可查），或重新 /newbot 生成。")
            return 1
    except requests.exceptions.RequestException as exc:
        print(_red(f"  ✗ 无法访问 {api_base}: {exc}"))
        print(_yellow("  → 若访问 api.telegram.org 超时，说明当前网络无法直连 Telegram（被墙）。"))
        print(_yellow("    请改用海外代理 TELEGRAM_API_BASE，或在能访问 Telegram 的网络/代理下运行本脚本。"))
        return 1

    text = (
        "✅ 电报推送链路测试成功！\n"
        "如果你在 Telegram 看到这条消息，说明 Bot Token 与 chat_id 配置正确、"
        "Telegram 推送链路已打通 🎉\n\n"
        "—— 来自 cangyue 项目一键测试脚本"
    )
    print(f"\n[2/2] 发送测试消息（sendMessage）→ chat_id={chat}")
    try:
        resp = requests.post(
            f"{api_base}/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            print(_green("  ✓ 测试消息发送成功！请到 Telegram 查看。"))
            print(_green("  → 电报能收到，说明 Token / chat_id / 推送链路全部正常 ✅"))
            return 0
        desc = data.get("description", "")
        print(_red(f"  ✗ 发送失败: {desc}"))
        if "chat not found" in desc or "identifier is empty" in desc:
            print(_yellow("  → chat_id 不对。请给 @userinfobot 发一条消息，用返回的 Id 作为 chat_id。"))
        if "bot was blocked" in desc or "Forbidden" in desc:
            print(_yellow("  → 机器人被拉黑/未发起会话：请先在 Telegram 里给 @你的bot 发一条 /start。"))
        return 1
    except requests.exceptions.RequestException as exc:
        print(_red(f"  ✗ 请求失败: {exc}"))
        print(_yellow("  → 请检查网络，或改用 TELEGRAM_API_BASE 指向海外代理。"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
