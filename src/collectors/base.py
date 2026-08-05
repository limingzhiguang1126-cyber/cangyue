"""Collector 基类与采集器注册表。

每个数据源对应一个独立的 Collector 子类，实现统一接口。
单个源异常不影响其他源（由调度层 catch 每个 collector 的 run）。
"""

from __future__ import annotations

import abc
import logging
import time
from typing import Dict, List, Optional, Type

from ..models import EventMessage
from ..utils.logger import setup_logging

logger = setup_logging()


class BaseCollector(abc.ABC):
    """采集器抽象基类。"""

    #: 数据源标识（对应 PRD 统一格式中的 source 字段）
    source: str = "base"
    #: 轮询间隔（秒），可由 config 覆盖
    poll_interval: int = 60

    def __init__(self, name: str = "", config: Optional[dict] = None):
        self.name = name or self.source
        self.config = config or {}
        self._last_run: float = 0.0
        self._log = logging.getLogger(f"fin-alert.collector.{self.name}")
        interval = self.config.get("poll_interval")
        if interval:
            self.poll_interval = int(interval)

    @property
    def log(self) -> logging.Logger:
        return self._log

    @abc.abstractmethod
    def fetch(self) -> List[EventMessage]:
        """抓取最新事件，返回标准化消息列表（可空）。"""

    def run(self) -> List[EventMessage]:
        """带节流保护的执行入口：距上次运行不足 poll_interval 时跳过。"""
        now = time.time()
        if now - self._last_run < self.poll_interval:
            return []
        self._last_run = now
        try:
            return self.fetch() or []
        except Exception as exc:  # noqa: BLE001 单个源失败不影响整体
            self.log.warning("collector %s failed: %s", self.name, exc)
            return []

    @staticmethod
    def _safe_text(value: Optional[str], default: str = "") -> str:
        """安全清理文本字段。"""
        if not value:
            return default
        return " ".join(str(value).split())


class CollectorRegistry:
    """采集器注册表：按名称/配置批量构建。"""

    def __init__(self) -> None:
        self._collectors: Dict[str, BaseCollector] = {}

    def register(self, collector: BaseCollector) -> None:
        self._collectors[collector.name] = collector

    def get(self, name: str) -> Optional[BaseCollector]:
        return self._collectors.get(name)

    def all(self) -> List[BaseCollector]:
        return list(self._collectors.values())

    @classmethod
    def build_from_config(cls, config: dict, collector_types: Dict[str, Type[BaseCollector]]) -> "CollectorRegistry":
        """依据 config.yaml 的 data_sources 构建已启用的采集器。

        Args:
            config: config.yaml 中的 ``data_sources`` 段
            collector_types: {配置键: Collector 类} 映射
        """
        registry = cls()
        for layer_key in ("layer1", "layer2", "layer3"):
            layer = config.get(layer_key, {})
            for key, item_cfg in layer.items():
                collector_cls = collector_types.get(key)
                if not collector_cls:
                    continue
                if not item_cfg.get("enabled", True):
                    continue
                merged = dict(item_cfg)
                merged["layer"] = layer_key
                registry.register(collector_cls(name=key, config=merged))
        return registry
