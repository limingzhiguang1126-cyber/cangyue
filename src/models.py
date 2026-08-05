"""统一消息模型。

DataCollector 采集到的所有源数据均被标准化为 :class:`EventMessage`，
对应 PRD 模块1 的"统一输出格式"。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class EventMessage:
    """统一消息格式：{source, title, content, url, author, timestamp, raw_data}。"""

    source: str
    title: str
    content: str = ""
    url: str = ""
    author: str = ""
    timestamp: str = field(default_factory=_utcnow_iso)
    raw_data: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        """转为可序列化 dict（用于入库 / 队列 / JSON 输出）。"""
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "content": self.content,
            "url": self.url,
            "author": self.author,
            "timestamp": self.timestamp,
            "raw_data": self.raw_data,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventMessage":
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            source=data.get("source", ""),
            title=data.get("title", ""),
            content=data.get("content", ""),
            url=data.get("url", ""),
            author=data.get("author", ""),
            timestamp=data.get("timestamp", _utcnow_iso()),
            raw_data=data.get("raw_data", {}),
        )

    def __str__(self) -> str:
        return f"[{self.source}] {self.title}"
