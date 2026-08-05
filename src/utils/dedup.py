"""基于 hash 的新闻去重。

PRD 模块1：去重机制 —— 基于 title+content 的 hash，24 小时内不重复处理。
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Optional

from ..models import EventMessage


def message_fingerprint(msg: EventMessage) -> str:
    """生成消息指纹。

    优先使用原始链接（url），无链接时退化为 title+content 的归一化 hash。
    """
    if msg.url:
        return hashlib.sha1(msg.url.strip().encode("utf-8")).hexdigest()
    raw = f"{msg.source}|{msg.title}|{msg.content}".strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class Deduplicator:
    """基于时间窗口的去重器。

    内部使用 LRU 语义的有序字典，窗口内的指纹视为已处理。
    """

    def __init__(self, window_seconds: int = 24 * 3600, max_size: int = 100_000):
        self.window_seconds = window_seconds
        self.max_size = max_size
        self._seen: "OrderedDict[str, float]" = OrderedDict()

    def _prune(self, now: float) -> None:
        expired = [fp for fp, ts in self._seen.items() if now - ts > self.window_seconds]
        for fp in expired:
            del self._seen[fp]
        while len(self._seen) > self.max_size:
            self._seen.popitem(last=False)

    def is_duplicate(self, msg: EventMessage, now: Optional[float] = None) -> bool:
        """判断消息是否已在窗口内出现过（不写入）。"""
        now = now or time.time()
        self._prune(now)
        return message_fingerprint(msg) in self._seen

    def mark_seen(self, msg: EventMessage, now: Optional[float] = None) -> None:
        """将消息标记为已见。"""
        now = now or time.time()
        self._prune(now)
        self._seen[message_fingerprint(msg)] = now

    def check_and_mark(self, msg: EventMessage, now: Optional[float] = None) -> bool:
        """原子操作：若未见过则标记并返回 True；否则返回 False。"""
        now = now or time.time()
        if self.is_duplicate(msg, now):
            return False
        self.mark_seen(msg, now)
        return True

    def __len__(self) -> int:
        return len(self._seen)
