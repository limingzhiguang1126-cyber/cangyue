"""币安公告采集器。

通过 Binance Announcement API 拉取上币/下架/维护/API 变更等公告。
官方接口示例：
  https://www.binance.com/bapi/composite/v1/public/content/community/notice/list
"""

from __future__ import annotations

import time
from typing import Any, List

import requests

from ..models import EventMessage
from .base import BaseCollector

_DEFAULT_API_URL = "https://www.binance.com/bapi/composite/v1/public/content/community/notice/list"


class BinanceAnnouncementCollector(BaseCollector):
    """币安公告采集器。"""

    source = "binance_announcement"

    def fetch(self) -> List[EventMessage]:
        api_url = self.config.get("api_url") or _DEFAULT_API_URL
        headers = {"User-Agent": "Mozilla/5.0 (compatible; fin-alert/0.1)"}
        try:
            resp = requests.get(api_url, headers=headers, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("binance api request failed: %s", exc)
            return []

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            self.log.warning("unexpected binance response shape: %s", str(payload)[:200])
            return []

        messages: List[EventMessage] = []
        for item in data[:50]:
            if not isinstance(item, dict):
                continue
            title = self._safe_text(item.get("title"))
            if not title:
                continue
            body = item.get("body") or item.get("description") or ""
            content = self._safe_text(body)[:2000]
            # 公告详情页
            code = item.get("code", "")
            url = f"https://www.binance.com/en/support/announcement/{code}" if code else ""
            ts = item.get("releaseDate") or item.get("publishTime") or ""
            if ts:
                try:
                    ts_iso = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(str(ts)) / 1000)
                    )
                except (ValueError, TypeError):
                    ts_iso = ""
            else:
                ts_iso = ""
            messages.append(
                EventMessage(
                    source=self.source,
                    title=title,
                    content=content,
                    url=url,
                    author="Binance",
                    timestamp=ts_iso,
                    raw_data={"item": {k: v for k, v in item.items() if k in ("code", "type", "releaseDate")}},
                )
            )
        return messages
