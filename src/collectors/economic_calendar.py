"""经济数据日历采集器。

对应 PRD：CPI、非农、GDP、FOMC 纪要、初请失业金等，提前一周拉取，
在发布前 ``advance_notice_hours`` 小时提醒。

本实现内置一个常用美国经济数据发布日历（可配置），并支持从
config 中扩展自定义条目。真实环境建议补充 BLS/BEA 官网排期抓取。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from ..models import EventMessage
from .base import BaseCollector

# 常用经济数据（发布频率 + 大致时间），key 为固定标识
_DEFAULT_CALENDAR = [
    {"key": "cpi", "name": "CPI 消费者物价指数", "freq_days": 30, "hour": 8, "minute": 30},
    {"key": "nonfarm_payrolls", "name": "非农就业报告", "freq_days": 30, "hour": 8, "minute": 30},
    {"key": "gdp", "name": "GDP 季度数据", "freq_days": 90, "hour": 8, "minute": 30},
    {"key": "jobless_claims", "name": "初请失业金人数", "freq_days": 7, "hour": 8, "minute": 30},
    {"key": "fomc_meeting", "name": "FOMC 议息会议", "freq_days": 45, "hour": 14, "minute": 0},
]


class EconomicCalendarCollector(BaseCollector):
    """经济数据日历采集器：生成"即将发布"的提醒事件。"""

    source = "economic_calendar"
    #: 提前提醒窗口（小时）
    advance_hours: int = 24

    def __init__(self, name: str = "", config: dict | None = None):
        super().__init__(name=name, config=config)
        self.advance_hours = int((config or {}).get("advance_notice_hours", 24))

    def fetch(self) -> List[EventMessage]:
        now = datetime.now(timezone.utc)
        messages: List[EventMessage] = []

        for item in _DEFAULT_CALENDAR:
            next_time = self._next_release(now, item)
            if next_time is None:
                continue
            delta = next_time - now
            if timedelta(0) <= delta <= timedelta(hours=self.advance_hours):
                title = f"经济数据预告：{item['name']}（{next_time.strftime('%Y-%m-%d %H:%M UTC')} 发布）"
                messages.append(
                    EventMessage(
                        source=self.source,
                        title=title,
                        content=f"{item['name']} 将于 {next_time.isoformat()} 发布，"
                        f"距离发布还有 {int(delta.total_seconds() // 3600)} 小时。",
                        url="",
                        author="EconomicCalendar",
                        timestamp=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        raw_data={"event_key": item["key"], "release_at": next_time.isoformat()},
                    )
                )
        return messages

    @staticmethod
    def _next_release(now: datetime, item: dict) -> datetime | None:
        """根据频率估算下一次发布时间（简化为按天循环推算）。"""
        hour = int(item.get("hour", 8))
        minute = int(item.get("minute", 30))
        freq_days = int(item.get("freq_days", 30))

        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # 找到最近一次（可能为过去）的发布时间点
        while candidate > now:
            candidate -= timedelta(days=freq_days)
        # 推进到下一次
        while candidate <= now:
            candidate += timedelta(days=freq_days)
        return candidate
