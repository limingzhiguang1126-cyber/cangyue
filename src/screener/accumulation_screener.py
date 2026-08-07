# -*- coding: utf-8 -*-
"""小市值「大涨后底部横盘 / 无量上涨」蓄势筛选器（Accumulation Screener，线 2 新版）。

背景（用户 2026-08-07 反馈）：
- 线 1（小市值池）：改为按 FDV 从小到大取 Top 100。
- 线 2（启动信号）：**废弃原有 momo_screener 的"5m/15m 动量 + 量价齐升"标准**，
  改为寻找"**之前大涨过、最近底部横盘**，或 **无量上涨**"的标的——
  即已从高点大幅回落（≥70%）、在底部区域横盘整理，或低位启动但量能极小（如 20% 涨幅
  合约日成交额仅几百万刀）的币。这类标的最容易出现"低吸埋伏"机会。

本工具在 Top 100 小市值池内扫描，用日线判断：
- 历史大涨：近 180 天出现过 ≥ +200%（至少几倍）的上涨段（峰值 bar 前的低点起算）。
- 底部横盘：距近 180 天高点回撤 ≥ 70%（已充分回调），且
  近一个月（30 天）振幅 ≤ 30%（底部横盘），**不看量能**。
- 无量上涨：近 3 天累计涨幅 ≥ +10%，但日均合约成交额 < $5M（"涨但没量"）。
- 附合约佐证：OI 变化方向、资金费率、主动买卖比（fapi 实时）。

数据源（币安官方，fapi 经 CORS 代理）：
- 合约日 K 线: fapi.binance.com/fapi/v1/klines?interval=1d
- OI 历史:     fapi.binance.com/futures/data/openInterestHist?period=1d
- 主动买卖比:  fapi.binance.com/futures/data/takerlongshortRatio?period=1d
- 资金费率:     fapi.binance.com/fapi/v1/fundingRate

用法：
    python -m src.screener.accumulation_screener --list data/smallcap_top100_fdv.json
    python -m src.screener.accumulation_screener --list data/smallcap_top100_fdv.json --json
    python -m src.screener.accumulation_screener --list data/smallcap_top100_fdv.json --output data/accumulation_candidates.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
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
logger = logging.getLogger("fin-alert.screener.accum")

urllib3.disable_warnings()

FAPI_BASE = "https://fapi.binance.com"
PROXY_CORS_SH = "https://proxy.cors.sh/"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://cnb.cool"}

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")

# ---------------- 线 2 判定阈值（可按需微调） ----------------
# 用户 2026-08-07 反馈调整：
# ①"大涨过" = 历史一波涨过几倍甚至更多（低->高 ≥ +200%，即至少 3 倍）；
# ② 距高点回撤 ≥ 70%；近一个月（30 天）振幅 ≤ 30%；且**不再考虑量能/缩量**；
# ③"无量上涨"只看合约日交易量 < $5M（由 $2M 放宽）。
LOOKBACK_DAYS = 180            # 历史观察窗口（覆盖"历史一波大涨"，半年）
PUMP_MIN_PCT = 200.0           # 历史"大涨"：窗口内低点->高点涨幅下限（%，几倍以上）
BASE_DD_MIN = 70.0             # 底部横盘：距窗口高点回撤下限（%，≥70%）
BASE_DD_MAX = 99.0             # 底部横盘：距窗口高点回撤上限（%，排除近乎归零）
RANGE_30D_MAX = 30.0           # 底部横盘：近一个月（30 天）振幅上限（%）
LOWVOL_GAIN_MIN = 0.0            # 无量上涨：近 3 天涨幅下限（用户已放开，>0 即可，仅作佐证）
LOWVOL_MAX_DAILY_VOL_USD = 5_000_000.0  # 无量上涨：日均合约成交额上限（USD，< $5M，用户指定只看这条）

STABLE_SYMBOLS = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "EUR", "AEUR",
                  "USDE", "USDY", "RLUSD", "USD1", "XUSD", "FRAX", "SUSD"}


def _fapi(path: str, timeout: int = 50) -> Optional[Any]:
    """经 CORS 代理访问 fapi.binance.com。"""
    url = FAPI_BASE + path
    for _ in range(2):
        try:
            resp = requests.get(PROXY_CORS_SH + url, headers=_HEADERS,
                                timeout=timeout, verify=False)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("fapi %s HTTP %s", path, resp.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fapi %s err: %s", path, exc)
        time.sleep(1.2)
    return None


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def fetch_futures_symbols() -> Dict[str, str]:
    """当前 TRADING 的 USDT 永续合约 base -> symbol 映射。"""
    data = _fapi("/fapi/v1/exchangeInfo", timeout=60)
    out: Dict[str, str] = {}
    if not data:
        return out
    for s in data.get("symbols", []):
        if (
            s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
        ):
            base = s.get("baseAsset", "")
            if base not in out:
                out[base] = s["symbol"]
    return out


def resolve_futures_symbol(base: str, futures_map: Dict[str, str]) -> Optional[str]:
    if base in futures_map:
        return futures_map[base]
    for prefix in ("1000000", "100000", "10000", "1000", "100"):
        cand = f"{prefix}{base}"
        if cand in futures_map:
            return futures_map[cand]
    return None


def fetch_daily_klines(symbol: str, limit: int = 210) -> List[Dict[str, Any]]:
    data = _fapi(f"/fapi/v1/klines?symbol={symbol}&interval=1d&limit={limit}", timeout=40)
    if not data:
        return []
    return [
        {"ts": int(k[0]), "open": _f(k[1]), "high": _f(k[2]), "low": _f(k[3]),
         "close": _f(k[4]), "vol_usd": _f(k[7])}
        for k in data
    ]


def fetch_oi_hist(symbol: str, period: str = "1d", limit: int = 180) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [{"time": _f(d["timestamp"]), "oi_value": _f(d["sumOpenInterestValue"])} for d in data]


def fetch_taker(symbol: str, period: str = "1d", limit: int = 30) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/takerlongshortRatio?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [{"time": _f(d["timestamp"]), "buy_sell": _f(d["buySellRatio"])} for d in data]


def fetch_funding(symbol: str, limit: int = 30) -> List[Dict[str, Any]]:
    data = _fapi(f"/fapi/v1/fundingRate?symbol={symbol}&limit={limit}")
    if not data:
        return []
    return [{"time": _f(d["fundingTime"]), "rate": _f(d["fundingRate"])} for d in data]


def _fmt_ts(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%m-%d")


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def classify_accumulation(kl: List[Dict[str, Any]],
                         oi: List[Dict[str, Any]],
                         taker: List[Dict[str, Any]],
                         funding: List[Dict[str, Any]]) -> Dict[str, Any]:
    """根据日线数据判定「底部横盘 / 无量上涨 / 其他」，并计算配套指标。

    kl 需 >= LOOKBACK_DAYS 根日 K。返回 dict：
    - base_accumulated: 是否"大涨后底部横盘"
    - low_volume_pump: 是否"无量上涨"
    - status / reason / score / metrics
    """
    out: Dict[str, Any] = {
        "base_accumulated": False,
        "low_volume_pump": False,
        "status": "other",
        "reason": "",
        "score": 0.0,
        "metrics": {},
    }
    n = len(kl)
    if n < LOOKBACK_DAYS:
        out["reason"] = "历史数据不足"
        return out

    win = kl[-LOOKBACK_DAYS:]
    cur = kl[-1]["close"]

    # 历史"一波大涨"：找窗口内最高点 bar 及其**之前**的最低点，计算那一波从低点到高点的涨幅
    # （不能用"窗口内低->高"，否则会把回撤本身算进去，出现假阳性）
    hi_idx = max(range(len(win)), key=lambda i: win[i]["high"])
    hi = win[hi_idx]["high"]
    pre = win[:hi_idx]
    lo_pre = min(k["low"] for k in pre) if pre else hi
    wave_ret = _pct(hi, lo_pre)              # 历史那一波涨幅（几倍以上）
    dd_from_peak = _pct(cur, hi)             # 距窗口高点回撤（负值）

    lo_all = min(k["low"] for k in win)
    price_from_low = _pct(cur, lo_all)       # 距窗口最低点涨幅

    # 近一个月（30 天）振幅
    last30 = kl[-30:]
    lo30 = min(k["low"] for k in last30)
    hi30 = max(k["high"] for k in last30)
    range_30d = _pct(hi30, lo30)

    # 近 3 天累计涨幅
    gain_3d = _pct(cur, kl[-4]["close"])

    # 近 3 天日均合约成交额（仅"无量上涨"判定用）
    recent3 = kl[-3:]
    avg_vol_recent3 = _mean(k["vol_usd"] for k in recent3)

    m = {
        "wave_ret_90d_pct": round(wave_ret, 1),
        "dd_from_peak_pct": round(dd_from_peak, 1),
        "price_from_low_pct": round(price_from_low, 1),
        "range_30d_pct": round(range_30d, 1),
        "avg_daily_vol_usd_3d": round(avg_vol_recent3, 0),
        "gain_3d_pct": round(gain_3d, 1),
        "price": cur,
        "high_90d": hi,
        "low_90d": lo_all,
    }
    out["metrics"] = m

    # ---- 判定 1：大涨后底部横盘（用户新口径，不看量能） ----
    if (
        wave_ret >= PUMP_MIN_PCT
        and BASE_DD_MIN <= -dd_from_peak <= BASE_DD_MAX
        and range_30d <= RANGE_30D_MAX
    ):
        out["base_accumulated"] = True
        out["status"] = "base_accumulated"
        out["reason"] = (f"历史一波从低点最高涨 {wave_ret:.0f}%（几倍以上），现距高点回撤 "
                         f"{-dd_from_peak:.0f}%（≥70%），近一个月振幅 {range_30d:.1f}%（≤30%）")
        out["score"] = 3.0

    # ---- 判定 2：无量上涨（用户口径：只看合约日交易量 < $5M，涨幅>0 佐证） ----
    if (
        gain_3d > LOWVOL_GAIN_MIN
        and avg_vol_recent3 < LOWVOL_MAX_DAILY_VOL_USD
    ):
        out["low_volume_pump"] = True
        out["status"] = "low_volume_pump" if out["status"] == "other" else out["status"]
        reason2 = (f"合约日均成交额仅 ${avg_vol_recent3/1e6:.2f}M（< $5M，无量）"
                   f"，近3天涨 {gain_3d:.1f}%")
        if out["status"] == "base_accumulated":
            out["reason"] += "；" + reason2
        else:
            out["reason"] = reason2
        out["score"] += 2.0

    # ---- 合约佐证 ----
    oi_metrics: Dict[str, Any] = {}
    if oi and len(oi) >= 10:
        early = _mean(x["oi_value"] for x in oi[:10])
        recent = _mean(x["oi_value"] for x in oi[-5:])
        peak_oi = max(x["oi_value"] for x in oi)
        oi_metrics = {
            "oi_now_usd": round(oi[-1]["oi_value"], 0),
            "oi_x_early_to_now": round(recent / early, 2) if early else 0.0,
            "oi_dd_from_peak_pct": round((oi[-1]["oi_value"] / peak_oi - 1) * 100, 1) if peak_oi else 0.0,
        }
        out["metrics"].update(oi_metrics)

    if taker:
        out["metrics"]["taker_buy_sell"] = round(taker[-1]["buy_sell"], 3)

    if funding:
        out["metrics"]["funding_latest_bps"] = round(funding[-1]["rate"] * 10000, 2)

    return out


def analyze_symbol(base: str, futures_map: Dict[str, str]) -> Dict[str, Any]:
    """对单个小市值标的做「底部横盘 / 无量上涨」扫描。"""
    res: Dict[str, Any] = {"symbol": base}
    fsym = resolve_futures_symbol(base, futures_map)
    if fsym is None:
        res["status"] = "no_futures"
        res["reason"] = "无活跃 TRADING USDT 永续合约"
        return res
    res["futures_symbol"] = fsym

    kl = fetch_daily_klines(fsym, LOOKBACK_DAYS + 30)
    if not kl or len(kl) < LOOKBACK_DAYS:
        res["status"] = "no_klines"
        res["reason"] = "合约日 K 线不足"
        return res

    oi = fetch_oi_hist(fsym, "1d", 90)
    taker = fetch_taker(fsym, "1d", 30)
    funding = fetch_funding(fsym, 30)

    info = classify_accumulation(kl, oi, taker, funding)
    res.update(info)
    if res["status"] == "other":
        res["reason"] = "未达底部横盘/无量上涨条件"
    return res


def load_smallcap_list(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="小市值「大涨后底部横盘 / 无量上涨」蓄势筛选器")
    ap.add_argument("--list", default=os.path.join(DATA_DIR, "smallcap_top100_fdv.json"),
                    help="小市值列表 JSON 路径（默认线1 Top100）")
    ap.add_argument("--top", type=int, default=100, help="取列表前 N 个")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--output", default="", help="结果写入文件")
    ap.add_argument("--workers", type=int, default=4, help="并发数")
    args = ap.parse_args(argv)

    lst = load_smallcap_list(args.list)[: args.top]
    futures_map = fetch_futures_symbols()
    logger.info("futures map: %d symbols", len(futures_map))

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        fut_map = {pool.submit(analyze_symbol, r["symbol"], futures_map): r["symbol"] for r in lst}
        for fut in as_completed(fut_map):
            sym = fut_map[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s analysis failed: %s", sym, exc)
                results.append({"symbol": sym, "status": "error", "reason": str(exc)})
            time.sleep(0.1)

    order = {"base_accumulated": 0, "low_volume_pump": 1, "other": 2,
             "no_futures": 3, "no_klines": 4, "error": 5}
    results.sort(key=lambda r: (order.get(r.get("status", "error"), 9), -r.get("score", 0)))

    if args.output:
        if os.path.isabs(args.output):
            out_path = args.output
        elif os.path.dirname(args.output):
            out_path = args.output
        else:
            out_path = os.path.join(DATA_DIR, args.output)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n结果已写入: {out_path}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print("\n== 小市值「大涨后底部横盘 / 无量上涨」扫描结果 ==")
    print(f"{'状态':<18}{'标的':<10}{'现价':>10}{'一波涨%':>8}{'回撤%':>8}{'30d振幅':>8}{'3d涨%':>8}{'3d日均量':>12}{'OI x':>6}{'费率bp':>8}  信号")
    print("-" * 150)
    for r in results:
        mt = r.get("metrics", {})
        print(
            f"{r.get('status','?'):<18}{r['symbol']:<10}{mt.get('price',0):>10,.6g}"
            f"{mt.get('wave_ret_90d_pct',0):>8,.0f}{mt.get('dd_from_peak_pct',0):>8,.0f}"
            f"{mt.get('range_30d_pct',0):>8,.1f}"
            f"{mt.get('gain_3d_pct',0):>8,.1f}{mt.get('avg_daily_vol_usd_3d',0):>12,.0f}"
            f"{mt.get('oi_x_early_to_now',0):>6,.1f}{mt.get('funding_latest_bps',0):>8,.1f}"
            f"  {r.get('reason','')[:70]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
