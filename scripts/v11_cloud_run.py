# -*- coding: utf-8 -*-
"""v1.2 信号云端执行器（CNB 云原生构建 · 定时任务专用）。

在 CNB 云端流水线（crontab 定时触发）中运行：
  1. 跑一轮 v11_signal_watcher.scan_pool，命中信号推送 Telegram
  2. 把去重状态 data/v11_signal_state.json 推回仓库（持久化去重）

相比本地 `v11_signal_daemon`，本脚本不常驻：
每轮由 CNB 定时任务独立拉起，天然无需 7x24 服务器。

用法（在流水线中，token 来自 CNB_TOKEN 环境变量）：
    python scripts/v11_cloud_run.py --top 100 --push-state
    python scripts/v11_cloud_run.py --top 100 --dry-run   # 只打印不推送（调试用）

环境变量：
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID    Telegram 推送（必填）
    CNB_TOKEN / CNB_WEB_ENDPOINT             CNB 构建临时令牌（回推状态用）
    CNB_REPO_SLUG / CNB_BRANCH               仓库与分支
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

# 兼容本地 .env（云端密钥由流水线 imports 注入，不受影响）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    _env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".env",
    )
    if os.path.exists(_env_path):
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.notifier.telegram_notifier import TelegramNotifier  # noqa: E402
from src.notifier.v11_message_formatter import format_signal_message  # noqa: E402
from src.screener import v11_signal_daemon as daemon  # noqa: E402
from src.screener import v11_signal_watcher as watcher  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402

setup_logging()
logger = logging.getLogger("fin-alert.cloud")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_POOL = os.path.join(DATA_DIR, "smallcap_top100_fdv.json")
DEFAULT_STATE = os.path.join(DATA_DIR, "v11_signal_state.json")

REFRESH_CMD = (
    "python -m src.screener.binance_smallcap --top 100 "
    "--sort fdv --output data/smallcap_top100_fdv.json"
)


def refresh_pool() -> bool:
    """刷新 FDV Top100 候选池。失败不阻断扫描（沿用上次池子）。"""
    try:
        logger.info("refreshing candidate pool ...")
        subprocess.run(REFRESH_CMD, shell=True, check=True, timeout=600)
        logger.info("pool refreshed")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("pool refresh failed (use existing pool): %s", exc)
        return False


def push_state(state_path: str) -> bool:
    """把去重状态文件推回仓库，供下次定时任务复用（持久化去重）。

    仅在「本轮确实推送过信号」时才有意义；回推失败只影响去重，
    不影响本轮推送结果（优雅降级）。
    """
    token = os.getenv("CNB_TOKEN", "")
    endpoint = os.getenv("CNB_WEB_ENDPOINT", "https://cnb.cool")
    repo = os.getenv("CNB_REPO_SLUG", "")
    branch = os.getenv("CNB_BRANCH", "main")
    if not token or not repo:
        logger.info("CNB_TOKEN/CNB_REPO_SLUG 缺失，跳过状态回推")
        return False
    try:
        push_url = f"https://cnb:{token}@{endpoint.replace('https://', '')}/{repo}.git"
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        subprocess.run(["git", "remote", "set-url", "origin", push_url],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "add", state_path],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(
            ["git", "-c", "user.name=cnb", "-c", "user.email=cnb@cnb.cool",
             "commit", "-m", f"chore(v11): 更新信号去重状态 [{branch}]"],
            check=False, capture_output=True, timeout=30,
        )
        proc = subprocess.run(
            ["git", "push", "origin", f"HEAD:{branch}"],
            check=False, capture_output=True, timeout=120, env=env,
        )
        if proc.returncode == 0:
            logger.info("state pushed to %s/%s", repo, branch)
            return True
        logger.warning("state push failed: %s",
                       proc.stderr.decode(errors="ignore")[-500:])
    except Exception as exc:  # noqa: BLE001
        logger.warning("state push error: %s", exc)
    return False


def run_round(top: int, push_state_after: bool, workers: int,
              dry_run: bool = False) -> Dict[str, Any]:
    pool = watcher.load_pool(DEFAULT_POOL, top)
    futures_map = watcher.fetch_futures_symbols()
    logger.info("scanning pool=%d (workers=%d) dry_run=%s", len(pool), workers, dry_run)
    results = watcher.scan_pool(pool, futures_map, workers=workers)
    hits = [r for r in results if r.get("signal") not in ("none",)]
    rank_map = {r.get("symbol"): idx + 1 for idx, r in enumerate(pool)}

    # dry-run 模式不推送、不依赖 Telegram 密钥
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    notifier = None
    if not dry_run and tg_token and tg_chat:
        notifier = TelegramNotifier(bot_token=tg_token, chat_id=tg_chat)

    dedup = daemon.SignalDeduplicator(state_path=DEFAULT_STATE, window_seconds=7200)

    # 非 dry-run 且未配密钥时，日志前缀用 no-push 区分
    log_prefix = "dry-run" if dry_run else "no-push"

    pushed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for r in hits:
        symbol = r.get("symbol", "")
        rank = rank_map.get(symbol, 0)
        msg = format_signal_message(r, pool_rank=rank)
        if notifier is None:
            logger.info("[%s] [%s] %s", log_prefix, r.get("action"), msg.replace("\n", " | "))
            pushed.append(r)
            continue
        if not dedup.is_new(r):
            skipped.append(r)
            continue
        ok = notifier.send_html(msg)
        if ok:
            dedup.mark_pushed(r)
            pushed.append(r)
        else:
            logger.warning("push failed for %s", symbol)

    logger.info("round done: scanned=%d hits=%d pushed=%d skipped_dup=%d",
                len(results), len(hits), len(pushed), len(skipped))
    if push_state_after and not dry_run:
        push_state(DEFAULT_STATE)
    return {"scanned": len(results), "hits": hits, "pushed": pushed, "skipped_dup": skipped}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v1.2 信号云端执行器（CNB 定时任务）")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--push-state", action="store_true", help="扫描后把去重状态推回仓库")
    ap.add_argument("--dry-run", action="store_true", help="只打印命中信号，不推送 Telegram（调试用，无需密钥）")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    run_round(top=args.top, push_state_after=args.push_state, workers=args.workers,
              dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
