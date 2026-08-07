# -*- coding: utf-8 -*-
"""Hugging Face Space 入口：Web 健康检查 + v1.1 信号轮询守护线程。

在海外 Space 上运行，可直连 api.telegram.org，解决 CNB 国内构建机推送被墙问题。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import requests

from src.notifier.telegram_notifier import TelegramNotifier
from src.notifier.v11_message_formatter import format_signal_message
from src.screener import v11_signal_daemon as daemon
from src.screener import v11_signal_watcher as watcher
from src.utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.hf")

# ---- 路径 ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
POOL_PATH = os.path.join(DATA_DIR, "smallcap_top100_fdv.json")
STATE_PATH = os.path.join(DATA_DIR, "v11_signal_state.json")

# ---- 配置（来自 Space Secrets）----
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = int(os.getenv("HF_POLL_INTERVAL", "15"))  # 分钟
TOP = int(os.getenv("HF_TOP", "100"))
WORKERS = int(os.getenv("HF_WORKERS", "8"))
PORT = int(os.getenv("PORT", "7860"))

_latest: dict = {"status": "initializing"}
_lock = threading.Lock()

_notifier: TelegramNotifier | None = None
if TG_TOKEN and TG_CHAT:
    _notifier = TelegramNotifier(bot_token=TG_TOKEN, chat_id=TG_CHAT)
    logger.info("telegram notifier ready")
else:
    logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 未配置，仅打印命中（不推送）")


def _run_round() -> None:
    """跑一轮 v1.1 信号扫描并推送。"""
    global _latest
    try:
        if not os.path.exists(POOL_PATH):
            _latest = {"status": "error", "detail": f"候选池不存在: {POOL_PATH}"}
            return
        pool = watcher.load_pool(POOL_PATH, TOP)
        futures_map = watcher.fetch_futures_symbols()
        logger.info("scanning pool=%d (workers=%d)", len(pool), WORKERS)
        results = watcher.scan_pool(pool, futures_map, workers=WORKERS)
        hits = [r for r in results if r.get("signal") not in ("none",)]
        rank_map = {r.get("symbol"): idx + 1 for idx, r in enumerate(pool)}

        pushed, skipped = [], []
        dedup = daemon.SignalDeduplicator(state_path=STATE_PATH, window_seconds=7200)
        for r in hits:
            symbol = r.get("symbol", "")
            msg = format_signal_message(r, pool_rank=rank_map.get(symbol, 0))
            if _notifier is None:
                logger.info("[%s] %s", r.get("action"), msg.replace("\n", " | "))
                pushed.append(r)
                continue
            if not dedup.is_new(r):
                skipped.append(r)
                continue
            if _notifier.send_html(msg):
                dedup.mark_pushed(r)
                pushed.append(r)
            else:
                logger.warning("push failed for %s", symbol)

        summary = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "scanned": len(results),
            "hits": len(hits),
            "pushed": len(pushed),
            "skipped_dup": len(skipped),
            "hit_symbols": [h.get("symbol") for h in hits],
        }
        logger.info("round done: %s", summary)
        with _lock:
            _latest = {"status": "ok", **summary}
    except Exception as exc:  # noqa: BLE001
        logger.exception("round error")
        with _lock:
            _latest = {"status": "error", "detail": str(exc)}


def _loop() -> None:
    """常驻轮询：先跑一轮，再按间隔循环。"""
    _run_round()
    while True:
        time.sleep(POLL_INTERVAL * 60)
        _run_round()


def _health(app) -> None:
    """心跳端点：HF 访问即认为活跃，尽量防止 Space 休眠。"""
    try:
        with _lock:
            body = {"status": "ok", "latest": _latest}
        r = requests.get("https://api.telegram.org", timeout=8)
        body["tg_reachable"] = r.status_code < 500
    except Exception:
        body["tg_reachable"] = False
    app.send_response(200)
    app.send_header("Content-Type", "application/json; charset=utf-8")
    app.end_headers()
    app.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def main() -> None:
    import http.server

    handler = type("HFHandler", (http.server.BaseHTTPRequestHandler,), {
        "do_GET": lambda self: _health(self),
        "do_HEAD": lambda self: _health(self),
        "log_message": lambda *a, **k: None,
    })

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    logger.info("listening on %s, poll interval %d min, top %d", PORT, POLL_INTERVAL, TOP)
    server.serve_forever()


if __name__ == "__main__":
    main()
