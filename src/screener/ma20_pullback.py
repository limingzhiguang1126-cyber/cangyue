# -*- coding: utf-8 -*-
"""币安永续合约「成交额 Top50 · 回踩 MA20」扫描器。

盯盘标的：币安 USDT 永续合约 24h 成交额（quoteVolume）排名 TopN（默认 50）。
策略（回踩均线买点，经典趋势回调形态）：
  对每个币种分别检查 15m / 1h / 4h 三个时间级别，
  若同时满足：
    ① 价格位于 MA20 上方（上升趋势中回调，未跌破均线）
    ② 最新价已回调到贴近 MA20（回踩）——相对 MA20 偏离幅度落在阈值区间
  则判定该币种在该级别「回踩 MA20」，列入推送候选。

数据源（币安官方 API，fapi 经 CORS 代理，与项目其它扫描器一致）：
- 合约 24h ticker:   fapi.binance.com/fapi/v1/ticker/24hr
- 合约 K 线:         fapi.binance.com/fapi/v1/klines?interval=15m/1h/4h

用法：
    python -m src.screener.ma20_pullback --top 50
    python -m src.screener.ma20_pullback --top 50 --json --output data/ma20_pullback.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests

from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.screener.ma20")

urllib3.disable_warnings()

FAPI_BASE = "https://fapi.binance.com"
PROXY_CORS_SH = "https://proxy.cors.sh/"

STABLE_SYMBOLS = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "EUR", "AEUR",
                  "USDE", "USDY", "RLUSD", "USD1", "XUSD", "FRAX", "SUSD"}

_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://cnb.cool"}

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)

# 回踩 MA20 判定阈值（相对偏离，%）
#   PULLBACK_MAX_PCT: 现价离 MA20 的最近偏离上限（越小说明越贴近均线）
#   PULLBACK_MIN_PCT: 允许的最小偏离（避免现价完全跌穿到均线下方又回拉的情况，
#                     也用于过滤已经明显跌破均线的标的）
# 默认认为「回踩」= 现价位于 MA20 上方 0.5% ~ 3% 区间内（贴近但未有效跌破）。
PULLBACK_MIN_PCT = 0.3       # 现价距 MA20 至少 +0.3%（仍在均线上方）
PULLBACK_MAX_PCT = 4.0       # 现价距 MA20 最多 +4.0%（不能偏离太远，须贴近回踩）

# 时间级别配置：interval -> 用于判定当前价格与 MA20 关系的 K 线
TIMELEVELS = [
    {"key": "15m", "interval": "15m", "label": "15分钟"},
    {"key": "1h", "interval": "1h", "label": "1小时"},
    {"key": "4h", "interval": "4h", "label": "4小时"},
]

# MA20 周期
MA_WINDOW = 20


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _fapi_retry(path: str, timeout: int = 40, retries: int = 3) -> Optional[Any]:
    """经 CORS 代理访问 fapi，带退避重试。"""
    url = FAPI_BASE + path
    for attempt in range(retries):
        try:
            resp = requests.get(PROXY_CORS_SH + url, headers=_HEADERS,
                                timeout=timeout, verify=False)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("fapi %s HTTP %s (attempt %d/%d)", path, resp.status_code,
                           attempt + 1, retries)
            if resp.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("fapi %s err: %s", path, exc)
        time.sleep(1.0 * (attempt + 1))
    return None


def fetch_futures_top_volume(top: int = 50) -> List[Dict[str, Any]]:
    """获取币安 USDT 永续合约 24h 成交额 TopN。

    过滤掉稳定币等，返回按 quoteVolume 降序排列的合约列表。
    """
    data = _fapi_retry("/fapi/v1/ticker/24hr", timeout=60)
    if not data:
        return []
    rows: List[Dict[str, Any]] = []
    for t in data:
        symbol = t.get("symbol", "")
        # ticker/24hr 不返回 quoteAsset，直接从 symbol 判断 USDT 永续
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if base in STABLE_SYMBOLS:
            continue
        rows.append({
            "symbol": symbol,
            "base": base,
            "price": _f(t.get("lastPrice")),
            "quote_volume": _f(t.get("quoteVolume")),
            "chg_pct": _f(t.get("priceChangePercent")),
        })
    rows.sort(key=lambda r: r["quote_volume"], reverse=True)
    return rows[:top]


def fetch_klines(symbol: str, interval: str, limit: int = 60) -> List[Dict[str, Any]]:
    """拉取合约 K 线。返回含 close 的列表。"""
    data = _fapi_retry(
        f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}",
        timeout=40,
    )
    if not data:
        return []
    out = []
    for k in data:
        out.append({
            "ts": int(k[0]),
            "open": _f(k[1]),
            "high": _f(k[2]),
            "low": _f(k[3]),
            "close": _f(k[4]),
            "volume": _f(k[5]),
            "close_time": int(k[6]),
            "quote_volume": _f(k[7]),
        })
    return out


def ma(closes: List[float], n: int = MA_WINDOW) -> Optional[float]:
    """计算最近 n 根收盘价的简单移动平均（MA）。不足 n 根返回 None。"""
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def evaluate_level(symbol: str, interval: str, label: str) -> Optional[Dict[str, Any]]:
    """判断单个币种单个级别是否「回踩 MA20」。

    返回 dict（含 ma、偏离度等）若命中；否则返回 None。
    """
    klines = fetch_klines(symbol, interval, limit=MA_WINDOW + 5)
    if len(klines) < MA_WINDOW:
        return None
    closes = [k["close"] for k in klines]
    ma20 = ma(closes, MA_WINDOW)
    if not ma20 or ma20 <= 0:
        return None
    price = closes[-1]
    # 相对偏离（%）：(price - ma20) / ma20 * 100
    dev_pct = (price - ma20) / ma20 * 100.0
    if not (PULLBACK_MIN_PCT <= dev_pct <= PULLBACK_MAX_PCT):
        return None
    # 计算当前价相对前一根收盘的涨跌（用于展示）
    prev_close = closes[-2] if len(closes) >= 2 else price
    chg_pct = (price - prev_close) / prev_close * 100.0 if prev_close else 0.0
    return {
        "symbol": symbol,
        "interval": interval,
        "label": label,
        "price": price,
        "ma20": ma20,
        "dev_pct": dev_pct,
        "chg_pct": chg_pct,
    }


def scan_pullback(top: int = 50, workers: int = 8) -> List[Dict[str, Any]]:
    """扫描成交额 TopN 合约，返回命中「回踩 MA20」的标的列表。

    返回列表的每个元素含：合约 symbol、base、24h 成交额、命中级别明细。
    只要任一级别（15m/1h/4h）命中即算入选；级别全部命中则该币优先级更高。
    """
    top_symbols = fetch_futures_top_volume(top)
    if not top_symbols:
        logger.warning("未能获取币安合约成交额排行")
        return []
    logger.info("fetched top-%d futures by 24h quote volume", len(top_symbols))

    results: List[Dict[str, Any]] = []
    for row in top_symbols:
        symbol = row["symbol"]
        hits: List[Dict[str, Any]] = []
        for lv in TIMELEVELS:
            r = evaluate_level(symbol, lv["interval"], lv["label"])
            if r:
                hits.append(r)
        if not hits:
            continue
        results.append({
            "symbol": symbol,
            "base": row["base"],
            "price": row["price"],
            "quote_volume": row["quote_volume"],
            "chg_24h_pct": row["chg_pct"],
            "levels": hits,
        })
    # 命中级别越多越靠前（回踩越充分/共振越强）
    results.sort(key=lambda r: (-len(r["levels"]), -r["quote_volume"]))
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="币安合约成交额TopN 回踩MA20 扫描器")
    ap.add_argument("--top", type=int, default=50, help="扫描成交额 TopN 合约（默认 50）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--output", type=str, default="", help="结果写入的 JSON 文件路径")
    args = ap.parse_args(argv)

    results = scan_pullback(top=args.top)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logger.info("written %d hits to %s", len(results), args.output)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            lv_str = ",".join(f"{h['label']}@{h['dev_pct']:+.2f}%" for h in r["levels"])
            print(f"{r['symbol']:<20} vol24h={r['quote_volume']:,.0f} 回踩: {lv_str}")
        print(f"\n共命中 {len(results)} 个币种")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
