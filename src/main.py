"""应用入口。

启动流程：
1. 加载配置（config/config.yaml + .env）
2. 构建采集器注册表
3. 构建 Telegram 推送器与去重器
4. 注册 APScheduler 任务，按各源 poll_interval 轮询
5. （Phase 2+）接入 screener/analyzer/event store/verifier

用法：
    python -m src.main
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import List

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .collectors.base import BaseCollector, CollectorRegistry
from .collectors.binance import BinanceAnnouncementCollector
from .collectors.economic_calendar import EconomicCalendarCollector
from .collectors.rss import RSSCollector
from .collectors.rsshub import RSSHubCollector
from .models import EventMessage
from .notifier.telegram_notifier import TelegramNotifier
from .utils.config import load_config
from .utils.dedup import Deduplicator
from .utils.logger import setup_logging

# 配置键 -> Collector 类映射
COLLECTOR_TYPES = {
    "truth_social": RSSHubCollector,
    "sec_edgar": RSSCollector,
    "binance_announcement": BinanceAnnouncementCollector,
    "ap_news": RSSCollector,
    "reuters": RSSCollector,
    "fed_speeches": RSSCollector,
    "sec_enforcement": RSSCollector,
    "elon_musk": RSSHubCollector,
    "cnbc": RSSCollector,
    "coindesk": RSSCollector,
    "earnings_calendar": RSSCollector,
    "economic_calendar": EconomicCalendarCollector,
}

logger = setup_logging()


class FinAlertApp:
    """主应用：持有采集器、推送器、去重器，并通过调度器驱动。"""

    def __init__(self, config: dict):
        self.config = config
        self.dedup = Deduplicator()

        tg = config.get("telegram", {})
        self.notifier = TelegramNotifier(
            bot_token=tg.get("bot_token", ""),
            chat_id=tg.get("chat_id", ""),
        )

        data_sources = config.get("data_sources", {})
        # 经济日历挂到 data_sources 平级，单独注册
        ec_cfg = config.get("economic_calendar", {})
        if ec_cfg.get("enabled", True):
            data_sources = {**data_sources, "economic_calendar": ec_cfg}

        self.registry = CollectorRegistry.build_from_config(data_sources, COLLECTOR_TYPES)
        self.scheduler = BlockingScheduler(timezone="UTC")

        # Phase 2+ 占位：screener / analyzer / store / verifier
        self.screener = None
        self.analyzer = None
        self.store = None

    # ------------------------------------------------------------------
    def _handle_messages(self, messages: List[EventMessage]) -> None:
        """处理一批采集结果：去重 -> 推送。"""
        for msg in messages:
            if not self.dedup.check_and_mark(msg):
                continue
            self.notifier.push_simple(msg)

    def _collect_and_push(self, collector: BaseCollector) -> None:
        messages = collector.run()
        if messages:
            logger.info("[%s] collected %d messages", collector.name, len(messages))
            self._handle_messages(messages)

    # ------------------------------------------------------------------
    def start(self) -> None:
        """注册定时任务并启动调度器。"""
        if not self.registry.all():
            logger.warning("no collectors enabled, nothing to run")

        for collector in self.registry.all():
            self.scheduler.add_job(
                self._collect_and_push,
                trigger=IntervalTrigger(seconds=collector.poll_interval),
                args=[collector],
                id=f"collector-{collector.name}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=30,
            )
            logger.info(
                "registered collector %s (interval=%ss, layer=%s)",
                collector.name,
                collector.poll_interval,
                collector.config.get("layer", "?"),
            )

        # Phase 3: 次日验证任务（每日 08:00 Asia/Shanghai）
        verification = self.config.get("verification", {})
        if verification.get("enabled", True):
            self.scheduler.add_job(
                self._run_verification,
                trigger="cron",
                hour=8,
                minute=0,
                timezone="Asia/Shanghai",
                id="daily-verification",
                misfire_grace_time=3600,
            )
            logger.info("registered daily verification job (08:00 Asia/Shanghai)")

        self.scheduler.start()

    def _run_verification(self) -> None:
        """Phase 3 占位：次日复盘（待 verifier 模块接入）。"""
        logger.info("daily verification job triggered (verifier not yet implemented)")

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="金融事件实时预警系统")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="单次运行后退出（便于调试/测试）")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    app = FinAlertApp(config)

    if args.once:
        for collector in app.registry.all():
            app._collect_and_push(collector)
        return 0

    def _stop(_sig, _frame):  # noqa: ANN001
        logger.info("shutting down ...")
        app.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        app.start()
    except (KeyboardInterrupt, SystemExit):
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
