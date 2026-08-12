# -*- coding: utf-8 -*-
"""多空比 + 主动买卖拐点监控器（Long/Short Ratio Sentinel）。

盯盘标的：线1 候选池（`data/smallcap_top100_fdv.json`，FDV 最小 Top100）。
数据源：币安 fapi 的 taker long/short ratio（**主动买卖比**）：
    - buySellRatio > 1  → 主动买入量 > 主动卖出量（多头主导）
    - buySellRatio < 1  → 主动卖出量 > 主动买入量（空头主导）

监控目标：
  ① 【空翻多】信号：近 N 根多空比均值 < 1（空头主导）后，最新值拐头向上**突破 1.0**，
     且 15m 价格同步转涨（>+0.5%）确认，说明空头回补 / 主动性买盘进场——见底反弹信号。
  ② 【暴跌风险】信号：多空比骤降（最新 < 0.55 且连续 ≥2 根下降）或 15m 跌幅较大（<-4%）
     且多空比快速转空（最新较均值降幅大），提示空头砸盘 / 资金出逃。

用法：
    python -m src.screener.long_short_sentinel --list data/smallcap_top100_fdv.json
    python -m src.screener.long_short_sentinel --list data/smallcap_top100_fdv.json --json

数据源（币安官方，fapi 经 CORS 代理）：
- 主动买卖比: fapi.binance.com/futures/data/takerlongshortRatio?period=15m
- 15m K线:    fapi.binance.com/fapi/v1/klines?interval=15m
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
from .momo_screener import fetch_futures_symbols, resolve_futures_symbol

setup_logging()
logger = logging.getLogger("fin-alert.screener.ls")

urllib3.disable_warnings()

_FAPI_BASE = "https://fapi.binance.com"
_PROXY_CORS_SH = "https://proxy.cors.sh/"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://cnb.cool"}

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
DEFAULT_POOL = os.path.join(DATA_DIR, "smallcap_top100_fdv.json")

# ---------------------------------------------------------------------------
# 多空比监控阈值
# ---------------------------------------------------------------------------
LS_LOOKBACK = 12          # 多空比均值窗口（根，15m）
CROSS_UP_MIN = 1.0        # 「空翻多」：多空比最新值须 ≥ 1.0（主动买入 ≥ 主动卖出）
EMPTY_MEAN_MAX = 0.92     # 空头主导判定：近 LS_LOOKBACK 根均值 < 0.92（<1 且留缓冲）
CROSS_UP_GAIN_MIN = 0.3   # 「空翻多」价格确认：15m 收盘涨幅 ≥ +0.3%
CRASH_RATIO_MAX = 0.55    # 「暴跌风险」：最新多空比 < 0.55（主动卖出严重主导）
CRASH_DROP_STEPS = 2      # 「暴跌风险」：连续下降根数
CRASH_PRICE_MIN = -4.0    # 「暴跌风险」：15m 价格跌幅触发阈值（%）
CRASH_RATIO_DROP_MIN = 0.25  # 「暴跌风险」：最新较均值降幅 ≥ 0.25

ACTION_ORDER = {"crash": 0, "cross_up": 1, "none": 2}


def _ts(ms: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%m-%d %H:%M")


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
    url = _FAPI_BASE + path
    for attempt in range(retries):
        try:
            resp = requests.get(_PROXY_CORS_SH + url, headers=_HEADERS,
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


def fetch_taker_ratio(symbol: str, period: str = "15m", limit: int = 30) -> List[Dict[str, Any]]:
    """拉取主动买卖比（多空比）序列。"""
    data = _fapi_retry(
        f"/futures/data/takerlongshortRatio?symbol={symbol}&period={period}&limit={limit}"
    )
    if not data:
        return []
    return [{"time": _f(d["timestamp"]), "ratio": _f(d["buySellRatio"])} for d in data]


def fetch_klines(symbol: str, interval: str = "15m", limit: int = 10) -> List[Dict[str, Any]]:
    """拉取 K 线（用于价格确认 / 跌幅判断）。"""
    data = _fapi_retry(f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if not data:
        return []
    return [
        {"ts": int(k[0]), "open": _f(k[1]), "high": _f(k[2]),
         "low": _f(k[3]), "close": _f(k[4]), "vol_usd": _f(k[7])}
        for k in data
    ]


def evaluate_ls(base: str, futures_map: Dict[str, str]) -> Dict[str, Any]:
    """对单个标的做多空比/主动买卖拐点判定。

    Returns:
        - signal: "cross_up"（空翻多）/ "crash"（暴跌风险）/ "none"
        - action: "alert" / "none"
        - reasons / metrics
    """
    res: Dict[str, Any] = {
        "symbol": base,
        "futures_symbol": "",
        "signal": "none",
        "action": "none",
        "reasons": [],
        "metrics": {},
        "ts": _ts(int(time.time() * 1000)),
    }

    fsym = resolve_futures_symbol(base, futures_map)
    if fsym is None:
        res["reasons"].append("无活跃 TRADING USDT 永续合约")
        return res
    res["futures_symbol"] = fsym

    ratios = fetch_taker_ratio(fsym, "15m", LS_LOOKBACK + 6)
    if len(ratios) < LS_LOOKBACK + 1:
        res["reasons"].append("多空比数据不足")
        return res

    kl = fetch_klines(fsym, "15m", 5)
    if len(kl) < 2:
        res["reasons"].append("15m K 线数据不足")
        return res

    # 最新多空比 + 历史均值
    latest = ratios[-1]["ratio"]
    recent = [r["ratio"] for r in ratios[:-1]]
    mean_hist = _mean(recent[-LS_LOOKBACK:])

    # 价格变化
    price_now = kl[-1]["close"]
    price_prev = kl[-2]["close"]
    chg_15m = (price_now / price_prev - 1) * 100 if price_prev else 0.0

    # 连续下降根数（含最新）
    drops = 0
    for i in range(len(ratios) - 1, 0, -1):
        if ratios[i]["ratio"] < ratios[i - 1]["ratio"]:
            drops += 1
        else:
            break

    metrics = {
        "ls_now": round(latest, 3),
        "ls_mean": round(mean_hist, 3),
        "ls_drops": drops,
        "price": round(price_now, 8),
        "chg_15m": round(chg_15m, 2),
    }
    res["metrics"] = metrics

    reasons: List[str] = []
    signal = "none"

    # ---- 信号判定 ----
    # ① 空翻多：历史空头主导(均值<1) → 最新突破 1.0，且价格转涨确认
    if (
        mean_hist < EMPTY_MEAN_MAX
        and latest >= CROSS_UP_MIN
        and chg_15m >= CROSS_UP_GAIN_MIN
    ):
        signal = "cross_up"
        reasons.append(
            f"空翻多：多空比从 {mean_hist:.2f}(空头主导) 拐头向上突破至 {latest:.2f}，"
            f"主动性买盘进场，15m 涨 {chg_15m:+.2f}% 确认"
        )

    # ② 暴跌风险：多空比骤降转空 或 价格大跌+多空比快速转空
    crash_price = chg_15m <= CRASH_PRICE_MIN
    crash_ratio = latest <= CRASH_RATIO_MAX and drops >= CRASH_DROP_STEPS
    ratio_plunge = (mean_hist - latest) >= CRASH_RATIO_DROP_MIN
    if crash_price or (crash_ratio and ratio_plunge):
        signal = "crash"
        if crash_price:
            reasons.append(
                f"暴跌风险：15m 大跌 {chg_15m:+.2f}%（≤{CRASH_PRICE_MIN}%），"
                f"多空比 {latest:.2f}（均值 {mean_hist:.2f}），资金快速出逃"
            )
        else:
            reasons.append(
                f"暴跌风险：多空比骤降至 {latest:.2f}（连续 {drops} 根下降，均值 {mean_hist:.2f}），"
                f"空头主动砸盘，谨防急跌"
            )

    res["signal"] = signal
    res["action"] = "alert" if signal != "none" else "none"
    res["reasons"] = reasons
    return res


def scan_pool(pool: List[Dict[str, Any]], futures_map: Dict[str, str],
              workers: int = 6) -> List[Dict[str, Any]]:
    """并发扫描候选池，返回全部判定结果。"""
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut_map = {
            ex.submit(evaluate_ls, r["symbol"], futures_map): r["symbol"]
            for r in pool
        }
        for fut in as_completed(fut_map):
            sym = fut_map[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s ls evaluation failed: %s", sym, exc)
                results.append(
                    {"symbol": sym, "signal": "none", "action": "none",
                     "reasons": [f"评估异常: {exc}"], "metrics": {}}
                )
            time.sleep(0.03)

    results.sort(key=lambda r: (ACTION_ORDER.get(r.get("action", "none"), 9),
                                -float(r.get("metrics", {}).get("ls_now", 0) or 0)))
    return results


def load_pool(path: str, top: int = 100) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data[:top]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="多空比 + 主动买卖拐点监控器")
    ap.add_argument("--list", default=DEFAULT_POOL, help="线1候选池 JSON 路径")
    ap.add_argument("--top", type=int, default=100, help="取候选池前 N 个")
    ap.add_argument("--symbols", help="指定合约 symbol 列表（逗号分隔，跳过候选池）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--workers", type=int, default=6, help="并发数")
    args = ap.parse_args(argv)

    if args.json:
        for _h in logging.getLogger().handlers:
            try:
                _h.setStream(sys.stderr)
            except Exception:  # noqa: BLE001
                pass

    if args.symbols:
        pool = [{"symbol": s.strip().upper().replace("USDT", "")}
                for s in args.symbols.split(",") if s.strip()]
    else:
        pool = load_pool(args.list, args.top)

    futures_map = fetch_futures_symbols()
    logger.info("futures map: %d symbols, pool: %d", len(futures_map), len(pool))

    results = scan_pool(pool, futures_map, workers=args.workers)
    hits = [r for r in results if r.get("signal") != "none"]

    if args.json:
        print(json.dumps({"scan_ts": results[0].get("ts", "") if results else "",
                          "pool_size": len(pool), "hits": hits, "all": results},
                         ensure_ascii=False, indent=2))
        return 0

    print("\n== 多空比 / 主动买卖拐点扫描结果 ==")
    print(f"命中 {len(hits)} 个 / 扫描 {len(results)} 个")
    for r in hits:
        m = r.get("metrics", {})
        print("-" * 70)
        print(f"[{r.get('signal','?').upper()}] {r['symbol']} ({r.get('futures_symbol','')})  "
              f"多空比={m.get('ls_now','?')} 均值={m.get('ls_mean','?')} "
              f"连续降={m.get('ls_drops','?')} 15m={m.get('chg_15m','?')}%")
        for reason in r.get("reasons", []):
            print(f"  · {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
