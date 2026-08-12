# -*- coding: utf-8 -*-
"""多空比/主动买卖拐点监控 · 云端执行器（CNB 云原生构建 · 定时任务专用）。

在 CNB 云端流水线（crontab 定时触发）中运行：
  1. 跑一轮 long_short_sentinel.scan_pool，命中「空翻多 / 暴跌风险」推 Telegram
  2. 把去重状态 data/ls_signal_state.json 推回仓库（持久化去重）

相比本地守护进程，本脚本不常驻：每轮由 CNB 定时任务独立拉起。

用法（在流水线中，token 来自 CNB_TOKEN 环境变量）：
    python scripts/ls_cloud_run.py --top 100 --push-state
    python scripts/ls_cloud_run.py --top 100 --dry-run   # 只打印不推送（调试用）

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
import time
from typing import Any, Dict, List, Optional

# 兼容本地 .env
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

from src.notifier.long_short_formatter import format_ls_message  # noqa: E402
from src.notifier.telegram_notifier import TelegramNotifier  # noqa: E402
from src.screener import long_short_sentinel as ls  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402

setup_logging()
logger = logging.getLogger("fin-alert.cloud.ls")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_POOL = os.path.join(DATA_DIR, "smallcap_top100_fdv.json")
DEFAULT_STATE = os.path.join(DATA_DIR, "ls_signal_state.json")


class LsDeduplicator:
    """基于「标的 + 信号线」的去重器（窗口内只推一次）。"""

    def __init__(self, state_path: str = DEFAULT_STATE, window_seconds: int = 1800):
        self.state_path = state_path
        self.window_seconds = window_seconds
        self._seen: Dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._seen = {k: float(v) for k, v in data.get("seen", {}).items()}
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            self._seen = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"seen": self._seen}, f, ensure_ascii=False)
        except OSError as exc:  # noqa: BLE001
            logger.warning("state save failed: %s", exc)

    @staticmethod
    def _key(result: Dict[str, Any]) -> str:
        return f"{result.get('symbol','')}|{result.get('signal','')}"

    def _prune(self, now: float) -> None:
        expired = [k for k, ts in self._seen.items() if now - ts > self.window_seconds]
        for k in expired:
            del self._seen[k]

    def is_new(self, result: Dict[str, Any], now: Optional[float] = None) -> bool:
        if result.get("signal") in ("none",):
            return False
        now = now or time.time()
        self._prune(now)
        return self._key(result) not in self._seen

    def mark_pushed(self, result: Dict[str, Any], now: Optional[float] = None) -> None:
        now = now or time.time()
        self._prune(now)
        key = self._key(result)
        if key:
            self._seen[key] = now
            self._save()


def push_state(state_path: str) -> bool:
    """把去重状态文件推回仓库，供下次定时任务复用（持久化去重）。"""
    token = os.getenv("CNB_TOKEN", "")
    endpoint = os.getenv("CNB_WEB_ENDPOINT", "https://cnb.cool")
    repo = os.getenv("CNB_REPO_SLUG", "")
    branch = os.getenv("CNB_BRANCH", "main")
    if not token or not repo:
        logger.info("CNB_TOKEN/CNB_REPO_SLUG 缺失，跳过状态回推")
        return False
    push_url = f"https://cnb:{token}@{endpoint.replace('https://', '')}/{repo}.git"
    try:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        subprocess.run(["git", "remote", "set-url", "origin", push_url],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "add", state_path],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(
            ["git", "-c", "user.name=cnb", "-c", "user.email=cnb@cnb.cool",
             "commit", "-m", f"chore(ls): 更新多空比去重状态 [{branch}]"],
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
    pool = ls.load_pool(DEFAULT_POOL, top)
    futures_map = ls.fetch_futures_symbols()
    logger.info("scanning pool=%d (workers=%d) dry_run=%s", len(pool), workers, dry_run)
    results = ls.scan_pool(pool, futures_map, workers=workers)
    hits = [r for r in results if r.get("signal") not in ("none",)]
    rank_map = {r.get("symbol"): idx + 1 for idx, r in enumerate(pool)}

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    notifier = None
    if not dry_run and tg_token and tg_chat:
        notifier = TelegramNotifier(bot_token=tg_token, chat_id=tg_chat)

    dedup = LsDeduplicator(state_path=DEFAULT_STATE, window_seconds=1800)
    log_prefix = "dry-run" if dry_run else "no-push"

    pushed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for r in hits:
        symbol = r.get("symbol", "")
        rank = rank_map.get(symbol, 0)
        msg = format_ls_message(r, pool_rank=rank)
        if notifier is None:
            logger.info("[%s] [%s] %s", log_prefix, r.get("signal"),
                        msg.replace("\n", " | "))
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
    ap = argparse.ArgumentParser(description="多空比监控云端执行器（CNB 定时任务）")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--push-state", action="store_true", help="扫描后把去重状态推回仓库")
    ap.add_argument("--dry-run", action="store_true", help="只打印命中信号，不推送")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)
    run_round(top=args.top, push_state_after=args.push_state, workers=args.workers,
              dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
