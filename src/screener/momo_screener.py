# -*- coding: utf-8 -*-
"""小市值标的"启动初期"动量筛选器（Momentum Screener）。

用途：用户已拿到小市值 Top50 列表（data/smallcap_top50_fdv.json），
不想追已经涨完的币，而是**在启动初期就发现"量价齐升 + 持仓同步放大"的标的**，
小仓位试仓，回调后再加。

策略参考（基于已涨币 HEI/BICO/TUT/SKYAI/HFT 等启动前后特征归纳）：
- 健康启动：放量 5~7x 温和起步（单根 3~5%）、量比 2~5x、OI 同步放大、主动买盘占优
- 危险追高：单根爆拉 >25%（15m）/ >12%（5m）、量比 >20x（情绪过热）、OI 不跟（纯现货盘）、资金费率过热
- 阈值口径按用户反馈放宽：15 分钟涨幅在 20% 左右的标的仍可关注（仅超 25% 才视为过热不追）
- 本工具扫描小市值列表，按 5m / 15m 动量 + 24h 量能放大 + 合约 OI 变化 + 主动买卖比打分，
  输出"当前处于启动初期（第一段刚确认）"的候选，供用户从小市值池里筛选。

数据源（币安官方 API，fapi 经 CORS 代理）：
- 现货 5m/15m/1h K 线:  data-api.binance.vision/api/v3/klines
- 现货 24h 行情:        data-api.binance.vision/api/v3/ticker/24hr
- 合约 OI 历史:         fapi.binance.com/futures/data/openInterestHist
- 主动买卖比:           fapi.binance.com/futures/data/takerlongshortRatio
- 大户多空比:           fapi.binance.com/futures/data/topLongShortAccountRatio
- 资金费率:             fapi.binance.com/fapi/v1/fundingRate
- 当前资金费率:         fapi.binance.com/fapi/v1/premiumIndex

用法：
    python -m src.screener.momo_screener --list data/smallcap_top50_fdv.json
    python -m src.screener.momo_screener --list data/smallcap_top50_fdv.json --top 20
    python -m src.screener.momo_screener --list data/smallcap_top50_fdv.json --json --output data/momo_candidates.json
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
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.screener.momo")

urllib3.disable_warnings()

SPOT_BASE = "https://data-api.binance.vision"
FAPI_BASE = "https://fapi.binance.com"
PROXY_CORS_SH = "https://proxy.cors.sh/"

STABLE_SYMBOLS = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "EUR", "AEUR",
                  "USDE", "USDY", "RLUSD", "USD1", "XUSD", "FRAX", "SUSD"}

_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://cnb.cool"}

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")

# 启动初期信号阈值（基于已涨币 HEI/BICO/HFT/TUT 启动特征校准）
# 2026-08-06 按用户反馈放宽：15 分钟涨幅 20% 左右仍可关注，仅超 25% 才视为过热
MOMENTUM_5M_MIN = 1.5        # 5m 涨幅最低门槛（%）
MOMENTUM_5M_PREF = 5.0       # 5m 涨幅"优秀"线（%）
MOMENTUM_15M_MIN = 2.0       # 15m 涨幅最低门槛（%）
MOMENTUM_15M_PREF = 15.0     # 15m 涨幅"优秀"线（%）
HOT_5M = 12.0                # 5m 涨幅超过此值视为情绪过热（不追）
HOT_15M = 25.0               # 15m 涨幅超过此值才视为情绪过热（不追）
VOL_RATIO_5M_MIN = 1.8       # 5m 量能放大倍数门槛（相对 24h 均量）
VOL_RATIO_5M_PREF = 3.0      # 5m 量能放大"优秀"线
VOL_RATIO_24H_MIN = 1.5      # 24h 量比（vs 前期）最低门槛
OI_CHANGE_MIN = 1.15         # OI 相对早期放大倍数门槛（持仓进场）
OI_CHANGE_PREF = 1.5         # OI 放大"优秀"线
TAKER_MIN = 1.02             # 主动买卖比（买/卖）最低门槛
TAKER_PREF = 1.10            # 主动买卖比"优秀"线
LSR_MIN = 0.95               # 大户多空比最低门槛
FUNDING_LOW = -0.05          # 资金费率下限（%）
FUNDING_HIGH = 0.10          # 资金费率上限（%）
DRAWDOWN_LIMIT = 15.0        # 距近期高点回撤上限（%，超过视为已见顶）
PRELAUNCH_RANGE_MAX = 12.0   # 启动前横盘振幅上限（%，超过说明波动太大不是吸筹区）


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
        time.sleep(1.5)
    return None


def _get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Any:
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _fmt_ts(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%m-%d %H:%M")


def normalize_futures_symbol(base: str) -> str:
    """把小市值列表里的 base symbol 转成 USDT 永续合约 symbol。

    处理缩放合约：如列表里是 PEPE，但合约可能是 1000PEPEUSDT。
    通过查询 exchangeInfo 反向映射。这里用简化规则：先试 base+USDT，若不在
    合约列表则尝试加 1000/10000/100000/1000000 前缀。
    """
    return f"{base}USDT"


def fetch_spot_klines(symbol: str, interval: str, limit: int = 300) -> List[Dict[str, Any]]:
    """现货 K 线（data-api.binance.vision）。"""
    data = _get_json(
        f"{SPOT_BASE}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=25,
    )
    return [
        {
            "ts": int(k[0]),
            "open": _f(k[1]),
            "high": _f(k[2]),
            "low": _f(k[3]),
            "close": _f(k[4]),
            "vol_usd": _f(k[7]),
        }
        for k in data
    ]


def fetch_spot_ticker() -> Dict[str, Dict[str, Any]]:
    data = _get_json(f"{SPOT_BASE}/api/v3/ticker/24hr", timeout=30)
    out: Dict[str, Dict[str, Any]] = {}
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in STABLE_SYMBOLS:
            continue
        out[base] = {
            "price": _f(t.get("lastPrice")),
            "chg_pct": _f(t.get("priceChangePercent")),
            "quote_volume": _f(t.get("quoteVolume")),
        }
    return out


def fetch_futures_symbols() -> Dict[str, str]:
    """获取当前 TRADING 的 USDT 永续合约 base -> symbol 映射（用于缩放合约归一化）。"""
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
    """把小市值 base 解析为合约 symbol。

    优先精确匹配；否则尝试缩放前缀（1000X/10000X/100000X/1000000X）。
    返回 None 表示该币没有活跃永续合约。
    """
    if base in futures_map:
        return futures_map[base]
    for prefix in ("1000000", "100000", "10000", "1000", "100"):
        cand = f"{prefix}{base}"
        if cand in futures_map:
            return futures_map[cand]
    return None


def fetch_funding(symbol: str, limit: int = 120) -> List[Dict[str, Any]]:
    data = _fapi(f"/fapi/v1/fundingRate?symbol={symbol}&limit={limit}")
    if not data:
        return []
    return [
        {"time": d["fundingTime"], "rate": _f(d["fundingRate"])}
        for d in data
    ]


def fetch_oi_hist(symbol: str, period: str = "5m", limit: int = 200) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [
        {"time": _f(d["timestamp"]),
         "oi": _f(d["sumOpenInterest"]),
         "oi_value": _f(d["sumOpenInterestValue"])}
        for d in data
    ]


def fetch_taker(symbol: str, period: str = "5m", limit: int = 60) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/takerlongshortRatio?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [{"time": _f(d["timestamp"]), "buy_sell": _f(d["buySellRatio"])} for d in data]


def fetch_ls_ratio(symbol: str, period: str = "5m", limit: int = 60) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/topLongShortAccountRatio?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [{"time": _f(d["timestamp"]), "ratio": _f(d["longShortRatio"])} for d in data]


def fetch_premium_index() -> Dict[str, Dict[str, Any]]:
    data = _fapi("/fapi/v1/premiumIndex", timeout=40)
    out: Dict[str, Dict[str, Any]] = {}
    if not data:
        return out
    for d in data:
        sym = d.get("symbol", "")
        out[sym] = {
            "mark": _f(d.get("markPrice")),
            "funding_bps": _f(d.get("lastFundingRate")) * 10000,
        }
    return out


def _sma(xs: List[float], n: int) -> float:
    if len(xs) < n:
        return 0.0
    return sum(xs[-n:]) / n


def analyze_symbol(
    base: str,
    futures_map: Dict[str, str],
    premium: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """对单个小市值标的扫描启动信号。"""
    res: Dict[str, Any] = {"symbol": base}

    spot_symbol = f"{base}USDT"
    # 先解析合约 symbol（用于 K 线兑底 + OI 等）
    fsym = resolve_futures_symbol(base, futures_map)
    if fsym is None:
        res["status"] = "no_futures"
        res["reason"] = "该标的无活跃 TRADING USDT 永续合约"
        return res
    res["futures_symbol"] = fsym

    # 现货 K 线（5m / 15m）；若无现货，用合约 K 线兑底（Alpha-only 币）
    k5 = k15 = []
    source = "spot"
    try:
        k5 = fetch_spot_klines(spot_symbol, "5m", 300)
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s spot 5m klines failed: %s", base, exc)
    try:
        k15 = fetch_spot_klines(spot_symbol, "15m", 300)
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s spot 15m klines failed: %s", base, exc)

    if len(k5) < 60:
        # 现货无 K 线：尝试合约 5m/15m K 线（fapi）
        fk5 = _fapi(f"/fapi/v1/klines?symbol={fsym}&interval=5m&limit=300", timeout=40)
        fk15 = _fapi(f"/fapi/v1/klines?symbol={fsym}&interval=15m&limit=300", timeout=40)
        if fk5 and len(fk5) >= 60:
            k5 = [{"ts": int(k[0]), "open": _f(k[1]), "high": _f(k[2]), "low": _f(k[3]),
                   "close": _f(k[4]), "vol_usd": _f(k[7])} for k in fk5]
            source = "futures"
        if fk15 and len(fk15) >= 60:
            k15 = [{"ts": int(k[0]), "open": _f(k[1]), "high": _f(k[2]), "low": _f(k[3]),
                    "close": _f(k[4]), "vol_usd": _f(k[7])} for k in fk15]

    if len(k5) < 60:
        res["status"] = "no_klines"
        res["reason"] = "现货与合约均无 5m K 线（流动性极低）"
        return res
    res["data_source"] = source

    cur = k5[-1]["close"]
    # 5m 动量（近 1~3 根）
    mom5_1 = (k5[-1]["close"] / k5[-2]["close"] - 1) * 100 if len(k5) >= 2 else 0.0
    mom5_3 = (k5[-1]["close"] / k5[-4]["close"] - 1) * 100 if len(k5) >= 4 else 0.0
    mom15 = (k15[-1]["close"] / k15[-2]["close"] - 1) * 100 if len(k15) >= 2 else 0.0
    mom15_2 = (k15[-1]["close"] / k15[-3]["close"] - 1) * 100 if len(k15) >= 3 else 0.0

    # 量能：近 3 根 5m 均量 vs 24h 均量（288 根）
    avg_vol24 = _sma([k["vol_usd"] for k in k5], 288)
    vol_recent3 = _mean(k["vol_usd"] for k in k5[-3:]) if len(k5) >= 3 else 0.0
    vol_ratio_5m = vol_recent3 / avg_vol24 if avg_vol24 > 0 else 0.0
    last_bar_vol = k5[-1]["vol_usd"]
    vol_ratio_last = last_bar_vol / avg_vol24 if avg_vol24 > 0 else 0.0

    # 24h 量能 vs 前 7 天均量（用 5m K 线粗算）
    last288 = sum(k["vol_usd"] for k in k5[-288:])
    prev = k5[:-288]
    prev_avg = _mean(k["vol_usd"] for k in prev) if prev else 0.0
    vol_ratio_24h = (last288 / 288) / prev_avg if prev_avg > 0 else 0.0

    # 距近期高点回撤（近 24h 高点）
    hi24 = max(k["high"] for k in k5[-288:]) if len(k5) >= 288 else max(k["high"] for k in k5)
    drawdown = (cur / hi24 - 1) * 100 if hi24 > 0 else 0.0

    # 启动前横盘振幅（近 7 天，剔除最近 24h）
    base_k = k5[:-288] if len(k5) > 288 else k5[: max(len(k5) - 48, 10)]
    if base_k:
        lo = min(k["low"] for k in base_k)
        hi = max(k["high"] for k in base_k)
        range_pct = (hi / lo - 1) * 100 if lo > 0 else 0.0
    else:
        range_pct = 0.0

    res["price"] = cur
    res["momentum"] = {
        "m5_1": round(mom5_1, 2),
        "m5_3": round(mom5_3, 2),
        "m15_1": round(mom15, 2),
        "m15_2": round(mom15_2, 2),
        "drawdown_24h_pct": round(drawdown, 2),
        "prelaunch_range_pct": round(range_pct, 2),
    }
    res["volume"] = {
        "vol_ratio_5m": round(vol_ratio_5m, 2),
        "vol_ratio_last_bar": round(vol_ratio_last, 2),
        "vol_ratio_24h_vs_7d": round(vol_ratio_24h, 2),
    }

    # 合约数据（OI / taker / 大户多空比 / 资金费率）
    oi = fetch_oi_hist(fsym, "5m", 200)
    taker = fetch_taker(fsym, "5m", 60)
    ls = fetch_ls_ratio(fsym, "5m", 60)
    funding = fetch_funding(fsym, 120)

    if oi and len(oi) >= 30:
        early_avg = _mean(x["oi_value"] for x in oi[:30])
        recent_avg = _mean(x["oi_value"] for x in oi[-30:])
        latest_oi = oi[-1]["oi_value"]
        peak_oi = max(x["oi_value"] for x in oi)
        oi_x = recent_avg / early_avg if early_avg > 0 else 0.0
        res["futures"] = {
            "oi_x_early_to_now": round(oi_x, 2),
            "oi_now_usd": round(latest_oi, 0),
            "oi_peak_usd": round(peak_oi, 0),
            "oi_drawdown_from_peak_pct": round((latest_oi / peak_oi - 1) * 100, 2) if peak_oi > 0 else 0.0,
        }
    else:
        res["futures"] = {}

    if taker:
        taker_recent = _mean(x["buy_sell"] for x in taker[-12:])
        res["taker"] = {"buy_sell_avg_1h": round(taker_recent, 3),
                        "latest": round(taker[-1]["buy_sell"], 3)}
    else:
        res["taker"] = {}

    if ls:
        ls_recent = _mean(x["ratio"] for x in ls[-12:])
        res["long_short"] = {"ratio_avg_1h": round(ls_recent, 3),
                             "latest": round(ls[-1]["ratio"], 3)}
    else:
        res["long_short"] = {}

    if funding and len(funding) >= 8:
        res["funding"] = {
            "avg_bps": round(_mean(x["rate"] for x in funding) * 10000, 2),
            "latest_bps": round(funding[-1]["rate"] * 10000, 2),
        }
    else:
        res["funding"] = {}

    if fsym in premium:
        res["funding"]["premium_bps"] = premium[fsym]["funding_bps"]

    # ---- 综合评分 ----
    score = 0.0
    notes: List[str] = []

    # 1) 5m 动量：适中放大（1.5~12%），超过 12% 视为过热
    if HOT_5M >= mom5_1 >= MOMENTUM_5M_MIN:
        score += 2.0
        if mom5_1 >= MOMENTUM_5M_PREF:
            score += 1.0
            notes.append(f"5m +{mom5_1:.1f}% 动量健康")
    elif mom5_1 > HOT_5M:
        notes.append(f"5m +{mom5_1:.1f}% 过热，不追")
    else:
        score -= 1.0
        notes.append(f"5m +{mom5_1:.1f}% 未达启动线")

    # 2) 15m 动量：按用户反馈放宽，15m 涨幅 20% 左右仍可关注，仅超 25% 视为过热
    if HOT_15M >= mom15 >= MOMENTUM_15M_MIN:
        score += 1.5
        if mom15 >= MOMENTUM_15M_PREF:
            score += 0.5
            notes.append(f"15m +{mom15:.1f}% 强动量")
    elif mom15 > HOT_15M:
        notes.append(f"15m +{mom15:.1f}% 过热")
    else:
        score -= 0.5

    # 3) 5m 量能放大
    if vol_ratio_5m >= VOL_RATIO_5M_MIN:
        score += 2.0
        if vol_ratio_5m >= VOL_RATIO_5M_PREF:
            score += 1.0
        notes.append(f"5m 量能 {vol_ratio_5m:.1f}x 放大")
    else:
        notes.append(f"5m 量能 {vol_ratio_5m:.1f}x 不足")

    # 4) 24h 量能相对 7 天放大
    if vol_ratio_24h >= VOL_RATIO_24H_MIN:
        score += 1.5
        notes.append(f"24h 量比 {vol_ratio_24h:.1f}x")
    else:
        notes.append(f"24h 量比 {vol_ratio_24h:.1f}x 不足")

    # 5) OI 放大（持仓进场）
    fu = res.get("futures", {})
    oi_x = fu.get("oi_x_early_to_now", 0.0)
    if oi_x >= OI_CHANGE_MIN:
        score += 2.0
        if oi_x >= OI_CHANGE_PREF:
            score += 1.0
        notes.append(f"OI 放大 {oi_x:.1f}x")
    else:
        notes.append(f"OI {oi_x:.1f}x 未跟")

    # 6) 主动买卖比（买盘占优）
    tk = res.get("taker", {})
    taker_avg = tk.get("buy_sell_avg_1h", 0.0)
    if taker_avg >= TAKER_MIN:
        score += 1.5
        if taker_avg >= TAKER_PREF:
            score += 0.5
        notes.append(f"主动买 {taker_avg:.2f}")
    else:
        notes.append(f"主动买 {taker_avg:.2f} 弱")

    # 7) 大户多空比
    lsr = res.get("long_short", {}).get("ratio_avg_1h", 0.0)
    if lsr >= LSR_MIN:
        score += 1.0
    else:
        notes.append(f"大户多空比 {lsr:.2f} 偏空")

    # 8) 资金费率（不在过热/转负区间）
    fr = res.get("funding", {})
    fr_latest = fr.get("latest_bps", 0.0)
    if FUNDING_LOW * 100 <= fr_latest <= FUNDING_HIGH * 100:
        score += 1.0
    else:
        notes.append(f"费率 {fr_latest:.2f}bp 异常")

    # 9) 距高点回撤过大（已见顶信号）
    if drawdown < -DRAWDOWN_LIMIT:
        score -= 3.0
        notes.append(f"已从高点回撤 {drawdown:.1f}%")

    # 10) 启动前横盘振幅过大
    if range_pct > PRELAUNCH_RANGE_MAX:
        score -= 1.0
        notes.append(f"启动前振幅 {range_pct:.1f}% 偏大")

    res["score"] = round(score, 2)
    res["notes"] = notes

    # 状态判定：launching 必须同时满足 ①动量达标 ②量能放大 ③OI 跟进
    mom_ok = (MOMENTUM_5M_MIN <= mom5_1 <= HOT_5M) or (MOMENTUM_15M_MIN <= mom15 <= HOT_15M)
    if drawdown < -DRAWDOWN_LIMIT:
        res["status"] = "topped"
        res["reason"] = "已从高点大幅回撤，疑似见顶"
    elif mom5_1 > HOT_5M or mom15 > HOT_15M:
        res["status"] = "hot"
        res["reason"] = "短线涨幅过热，追高风险大"
    elif (
        mom_ok
        and score >= 6.0
        and oi_x >= OI_CHANGE_MIN
        and vol_ratio_5m >= VOL_RATIO_5M_MIN
    ):
        res["status"] = "launching"
        res["reason"] = "量价齐升 + 持仓放大，符合启动初期特征"
    elif score >= 4.0:
        res["status"] = "watch"
        res["reason"] = "有量能但需确认，放入观察"
    else:
        res["status"] = "quiet"
        res["reason"] = "未达启动信号"

    return res


def load_smallcap_list(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="小市值启动信号筛选器")
    ap.add_argument("--list", default=os.path.join(DATA_DIR, "smallcap_top50_fdv.json"),
                    help="小市值列表 JSON 路径")
    ap.add_argument("--top", type=int, default=50, help="取列表前 N 个（默认 50）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--output", default="", help="结果写入文件")
    ap.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    args = ap.parse_args(argv)

    lst = load_smallcap_list(args.list)[: args.top]
    futures_map = fetch_futures_symbols()
    premium = fetch_premium_index()
    logger.info("futures map: %d symbols, premium: %d", len(futures_map), len(premium))

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        fut_map = {
            pool.submit(analyze_symbol, r["symbol"], futures_map, premium): r["symbol"]
            for r in lst
        }
        for fut in as_completed(fut_map):
            sym = fut_map[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s analysis failed: %s", sym, exc)
                results.append({"symbol": sym, "status": "error", "reason": str(exc)})
            time.sleep(0.1)

    # 按状态分组：launching > watch > hot > quiet
    order = {"launching": 0, "watch": 1, "hot": 2, "quiet": 3, "topped": 4, "no_futures": 5, "no_spot": 6, "error": 7}
    results.sort(key=lambda r: (order.get(r.get("status", "error"), 9), -r.get("score", 0)))

    if args.output:
        if os.path.isabs(args.output):
            out_path = args.output
        elif os.path.dirname(args.output):
            out_path = args.output  # 用户给的相对路径按当前目录解析
        else:
            out_path = os.path.join(DATA_DIR, args.output)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n结果已写入: {out_path}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print("\n== 小市值启动信号扫描结果 ==")
    print(f"{'状态':<10}{'标的':<10}{'价格':>12}{'5m%':>7}{'15m%':>7}{'量比5m':>8}{'24h量比':>8}{'OI x':>6}{'主动买':>7}{'大户LS':>7}{'费率bp':>8}{'回撤%':>7}  信号")
    print("-" * 130)
    for r in results:
        m = r.get("momentum", {})
        v = r.get("volume", {})
        fu = r.get("futures", {})
        tk = r.get("taker", {})
        ls = r.get("long_short", {})
        fr = r.get("funding", {})
        notes = "; ".join(r.get("notes", []))
        print(
            f"{r.get('status','?'):<10}{r['symbol']:<10}{r.get('price',0):>12,.6g}"
            f"{m.get('m5_1',0):>7,.1f}{m.get('m15_1',0):>7,.1f}"
            f"{v.get('vol_ratio_5m',0):>8,.1f}{v.get('vol_ratio_24h_vs_7d',0):>8,.1f}"
            f"{fu.get('oi_x_early_to_now',0):>6,.1f}{tk.get('buy_sell_avg_1h',0):>7,.2f}"
            f"{ls.get('ratio_avg_1h',0):>7,.2f}{fr.get('latest_bps',0):>8,.1f}"
            f"{m.get('drawdown_24h_pct',0):>7,.1f}  {notes[:60]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
