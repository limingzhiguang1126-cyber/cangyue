"""RSSHub 采集器。

适用于依赖 RSSHub 中转的非标准源：
- Truth Social (特朗普)：``/truthsocial/realDonaldTrump``
- Elon Musk X (推特)：``/twitter/user/elonmusk``

注意：Truth Social / Twitter 路由可能因平台改版失效，
失效时请参考 README 中的备用抓取方案。
"""

from __future__ import annotations

import feedparser
from typing import List

from ..models import EventMessage
from .base import BaseCollector


class RSSHubCollector(BaseCollector):
    """通过自建 RSSHub 实例采集非标准源。"""

    source = "rsshub"

    def fetch(self) -> List[EventMessage]:
        rsshub_url = self.config.get("rsshub_url")
        if not rsshub_url:
            self.log.warning("rsshub_url not configured for %s", self.name)
            return []

        feed = feedparser.parse(rsshub_url)
        if feed.get("bozo") and not feed.entries:
            self.log.warning("rsshub parse error for %s: %s", rsshub_url, feed.get("bozo_exception"))
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
                    raw_data={"feed_name": self.name, "rsshub_route": rsshub_url},
                )
            )
        return messages
