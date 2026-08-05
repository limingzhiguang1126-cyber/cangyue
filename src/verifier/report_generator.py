"""次日复盘报告生成（Phase 3）。

将验证结果汇总为报告，并推送 Telegram。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from .accuracy_checker import compute_accuracy


class ReportGenerator:
    """复盘报告生成器。"""

    def __init__(self, weekly_stats: Dict[str, Any] | None = None):
        # weekly_stats 可传入本周累计统计（Phase 3 完善）
        self.weekly_stats = weekly_stats or {}

    def build_report(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """构建报告 dict。"""
        all_results: List[Dict[str, Any]] = []
        for event in events:
            all_results.extend(event.get("results", []))

        stats = compute_accuracy(all_results)
        date_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        return {
            "date": date_str,
            "events": [
                {
                    "event_summary": e.get("event_summary") or e.get("title", ""),
                    "timestamp": e.get("timestamp", ""),
                    "predictions": e.get("results", []),
                }
                for e in events
            ],
            "correct": stats["correct"],
            "total": stats["total"],
            "accuracy": stats["accuracy"],
            "weekly_accuracy": self.weekly_stats.get("accuracy", 0),
            "high_conf_accuracy": stats["high_conf_accuracy"],
            "low_conf_accuracy": stats["low_conf_accuracy"],
        }

    @staticmethod
    def to_text(report: Dict[str, Any]) -> str:
        from ..notifier.message_formatter import format_daily_report

        return format_daily_report(report)
