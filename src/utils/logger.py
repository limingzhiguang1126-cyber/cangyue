"""通用日志配置。"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup_logging(level: str | None = None) -> logging.Logger:
    """初始化根日志器，返回名为 ``fin-alert`` 的应用 logger。

    Args:
        level: 日志级别字符串（DEBUG/INFO/WARNING/ERROR），
               默认读取环境变量 ``LOG_LEVEL``，兜底 INFO。
    """
    global _CONFIGURED

    log_level = (level or os.getenv("LOG_LEVEL", "info")).upper()
    if not _CONFIGURED:
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            stream=sys.stdout,
            force=True,
        )
        _CONFIGURED = True

    return logging.getLogger("fin-alert")
