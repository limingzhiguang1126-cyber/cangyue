# -*- coding: utf-8 -*-
"""v1.1 信号 · 每 15 分钟轮询 + Telegram 实时推送守护进程。

在「线 1 候选池」（Top100 小市值）上每 15 分钟跑一次
`v11_signal_watcher.scan_pool`，命中信号则格式化后推送到 Telegram。
同标的 + 同信号线在去重窗口内只推送一次，避免重复轰炸。

用法：
    python -m src.screener.v11_signal_daemon                  # 常驻轮询（默认 15 分钟）
    python -m src.screener.v11_signal_daemon --once           # 单次扫描并推送（调试）
    python -m src.screener.v11_signal_daemon --interval 15    # 自定义轮询间隔（分钟）
    python -m src.screener.v11_signal_daemon --dry-run        # 只打印不推送

依赖环境变量（见 .env.example）：
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

# 自动加载仓库根目录 .env（本地部署时填入 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 即可生效）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    _env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".env",
    )
    if os.path.exists(_env_path):
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

from ..notifier.telegram_notifier import TelegramNotifier
from ..notifier.v11_message_formatter import format_signal_message
from ..utils.logger import setup_logging
from . import v11_signal_watcher as watcher

setup_logging()
logger = logging.getLogger("fin-alert.daemon.v11")

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
DEFAULT_POOL = os.path.join(DATA_DIR, "smallcap_top100_fdv.json")
DEFAULT_STATE = os.path.join(DATA_DIR, "v11_signal_state.json")


class SignalDeduplicator:
    """基于「标的 + 信号线」的去重器（窗口内只推一次）。

    状态持久化到 JSON 文件，守护进程重启后仍能跨会话去重。
    """

    def __init__(self, state_path: str = DEFAULT_STATE, window_seconds: int = 7200):
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
        """若该「标的+信号线」在窗口内已推送过则返回 False。"""
        if result.get("signal") in ("none",):
            return False
        now = now or time.time()
        self._prune(now)
        key = self._key(result)
        return key not in self._seen

    def mark_pushed(self, result: Dict[str, Any], now: Optional[float] = None) -> None:
        now = now or time.time()
        self._prune(now)
        key = self._key(result)
        if key:
            self._seen[key] = now
            self._save()

    def check_and_mark(self, result: Dict[str, Any], now: Optional[float] = None) -> bool:
        if self.is_new(result, now):
            self.mark_pushed(result, now)
            return True
        return False


def run_once(
    pool_path: str = DEFAULT_POOL,
    top: int = 100,
    notifier: Optional[TelegramNotifier] = None,
    dedup: Optional[SignalDeduplicator] = None,
    dry_run: bool = False,
    workers: int = 8,
) -> Dict[str, Any]:
    """单轮扫描 + 去重 + 推送。

    Returns:
        {"scanned": n, "hits": [...], "pushed": [...], "skipped_dup": [...]}
    """
    pool = watcher.load_pool(pool_path, top)
    futures_map = watcher.fetch_futures_symbols()
    logger.info("scanning pool=%d symbols (workers=%d)", len(pool), workers)

    results = watcher.scan_pool(pool, futures_map, workers=workers)
    hits = [r for r in results if r.get("signal") not in ("none",)]

    # 候选池排名（用于消息展示）
    rank_map = {r.get("symbol"): idx + 1 for idx, r in enumerate(pool)}

    pushed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for r in hits:
        symbol = r.get("symbol", "")
        rank = rank_map.get(symbol, 0)
        msg = format_signal_message(r, pool_rank=rank)

        if notifier is None:
            logger.info("[%s] %s", r.get("action"), msg.replace("\n", " | "))
            pushed.append(r)
            continue

        if dedup is not None and not dedup.is_new(r):
            logger.info("skip duplicate: %s signal=%s", symbol, r.get("signal"))
            skipped.append(r)
            continue

        if dry_run:
            logger.info("[dry-run] would push %s: %s", symbol, r.get("signal"))
        else:
            ok = notifier.send_html(msg)
            if not ok:
                logger.warning("push failed for %s", symbol)
                continue
        if dedup is not None:
            dedup.mark_pushed(r)
        pushed.append(r)

    logger.info("round done: scanned=%d hits=%d pushed=%d skipped=%d",
                len(results), len(hits), len(pushed), len(skipped))
    return {"scanned": len(results), "hits": hits,
            "pushed": pushed, "skipped_dup": skipped}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v1.1 信号每 15 分钟轮询 + Telegram 推送")
    ap.add_argument("--pool", default=DEFAULT_POOL, help="线1候选池 JSON 路径")
    ap.add_argument("--top", type=int, default=100, help="取候选池前 N 个")
    ap.add_argument("--interval", type=int, default=15, help="轮询间隔（分钟），默认 15")
    ap.add_argument("--once", action="store_true", help="单次扫描后退出")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    ap.add_argument("--state", default=DEFAULT_STATE, help="去重状态文件路径")
    ap.add_argument("--dedup-window", type=int, default=7200, help="去重窗口（秒），默认 7200=2h")
    ap.add_argument("--workers", type=int, default=4, help="扫描并发数（默认 4，避免 fapi 限流）")
    args = ap.parse_args(argv)

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")

    notifier = None
    if args.dry_run:
        logger.info("dry-run 模式（你加了 --dry-run）：命中信号只会打印，不会真的发到 Telegram；去掉 --dry-run 才会真实推送")
    elif tg_token and tg_chat:
        notifier = TelegramNotifier(bot_token=tg_token, chat_id=tg_chat)
        logger.info("telegram notifier ready（已读到 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID，命中即推送到你的 Telegram）")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 未配置，将仅打印命中（不推送）")
        notifier = None

    dedup = SignalDeduplicator(state_path=args.state,
                               window_seconds=args.dedup_window)

    def _round() -> None:
        run_once(pool_path=args.pool, top=args.top, notifier=notifier,
                 dedup=dedup, dry_run=args.dry_run, workers=args.workers)

    if args.once:
        _round()
        return 0

    # 常驻：立即跑一轮，然后按 interval 分钟轮询
    _round()
    logger.info("进入轮询模式，每 %d 分钟扫描一次（Ctrl-C 退出）", args.interval)
    try:
        while True:
            time.sleep(args.interval * 60)
            _round()
    except KeyboardInterrupt:
        logger.info("stopped by user")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
