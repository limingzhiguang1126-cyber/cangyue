"""价格数据获取（Phase 3）。

- 美股：Yahoo Finance（yfinance）
- 加密：Binance API 历史 K 线

注意：yfinance 偶发失效，需异常重试 + 备用数据源（见 README）。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.verifier.price")


class PriceFetcher:
    """统一价格查询入口。"""

    def __init__(self, crypto_api_base: str = "https://api.binance.com"):
        self.crypto_api_base = crypto_api_base

    # ------------------------------------------------------------------
    def fetch_stock_price(self, symbol: str, start_ts: float, end_ts: float) -> Optional[float]:
        """拉取美股从 start 到 end 的涨跌幅（%）。

        Args:
            symbol: 如 NVDA / AAPL
            start_ts / end_ts: 秒级时间戳

        Returns:
            涨跌幅百分比，失败返回 None。
        """
        try:
            import yfinance as yf  # 延迟导入，避免 Phase 1 无此依赖

            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=start_ts, end=end_ts)
            if hist.empty:
                return None
            first = float(hist["Close"].iloc[0])
            last = float(hist["Close"].iloc[-1])
            if first == 0:
                return None
            return (last - first) / first * 100.0
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_stock_price(%s) failed: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    def fetch_crypto_price(self, symbol: str, start_ts: float, end_ts: float) -> Optional[float]:
        """拉取加密货币从 start 到 end 的涨跌幅（%）。

        Args:
            symbol: 如 BTC / ETH（Binance 需转成 BTCUSDT 形式）
        """
        base = self.crypto_api_base.rstrip("/")
        pair = f"{symbol.upper()}USDT"
        url = f"{base}/api/v3/klines"
        params = {
            "symbol": pair,
            "interval": "1h",
            "startTime": int(start_ts * 1000),
            "endTime": int(end_ts * 1000),
            "limit": 1000,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return None
            first = float(rows[0][1])  # open
            last = float(rows[-1][4])  # close
            if first == 0:
                return None
            return (last - first) / first * 100.0
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_crypto_price(%s) failed: %s", pair, exc)
            return None

    # ------------------------------------------------------------------
    def fetch_change(self, instrument: str, instrument_type: str, start_ts: float, end_ts: float) -> Optional[float]:
        """按标的类型分发的便捷入口。"""
        if instrument_type == "crypto":
            return self.fetch_crypto_price(instrument, start_ts, end_ts)
        return self.fetch_stock_price(instrument, start_ts, end_ts)
