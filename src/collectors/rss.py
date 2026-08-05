"""原生 RSS 采集器。

适用于：SEC EDGAR 8-K、AP News、Reuters、Fed speeches、
SEC enforcement、CNBC、CoinDesk 等标准 RSS Feed。
"""

from __future__ import annotations

import feedparser
from typing import List

from ..models import EventMessage
from .base import BaseCollector


class RSSCollector(BaseCollector):
    """标准 RSS/Atom 源采集器。"""

    source = "rss"

    def fetch(self) -> List[EventMessage]:
        rss_url = self.config.get("rss_url")
        if not rss_url:
            self.log.warning("rss_url not configured for %s", self.name)
            return []

        feed = feedparser.parse(rss_url)
        if feed.get("bozo") and not feed.entries:
            self.log.warning("parse error for %s: %s", rss_url, feed.get("bozo_exception"))
            return []

        messages: List[EventMessage] = []
        for entry in feed.entries[:50]:
            title = self._safe_text(entry.get("title"))
            if not title:
                continue
            content = self._safe_text(
                entry.get("summary")
                or (entry.get("description") if hasattr(entry, "description") else "")
                or ""
            )
            link = entry.get("link", "")
            published = entry.get("published") or entry.get("updated") or ""
            author = self._safe_text(entry.get("author"))
            messages.append(
                EventMessage(
                    source=self.source,
                    title=title,
                    content=content,
                    url=link,
                    author=author,
                    timestamp=published,
                    raw_data={"feed_name": self.name, "tags": [t.get("term", "") for t in entry.get("tags", [])]},
                )
            )
        return messages
