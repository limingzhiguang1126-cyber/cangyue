"""小币种"启动前/启动初期"特征分析器（v1，币安官方 fapi 视角）。

> 与仓库内 launch_analyzer.py（基于 Gate 数据源）互补：
> 本模块全部走**币安官方 fapi**（经 CORS 代理），额外提供
> 大户多空比（topLongShortAccountRatio）、主动买卖比（takerlongshortRatio）、
> 币安资金费率、币安 OI 等 Gate 没有的维度；launch_analyzer 侧重现货 K 线
> 起爆点检测与 Gate 永续 OI。两者可交叉验证。

针对涨幅榜热门小币（如 HEI/BICO/TUT/SKYAI），自动拉取币安合约的
K 线、持仓量、资金费率、大户多空比、主动买卖比等数据，输出结构化的
"启动特征画像"，帮助识别"启动前吸筹"信号，辅助追涨/埋伏决策。

数据源（全部币安官方 API，fapi 经 CORS 代理转发）：
- 现货 24h 行情:  data-api.binance.vision/api/v3/ticker/24hr（直连）
- 合约 K 线:      fapi.binance.com/fapi/v1/klines
- 持仓量历史:     fapi.binance.com/futures/data/openInterestHist
- 资金费率:       fapi.binance.com/fapi/v1/fundingRate
- 大户多空比:     fapi.binance.com/futures/data/topLongShortAccountRatio
- 主动买卖比:     fapi.binance.com/futures/data/takerlongshortRatio

用法：
    python -m src.screener.launch_pattern --symbols HEIUSDT,BICOUSDT
    python -m src.screener.launch_pattern --top-gainers 10          # 自动取当天涨幅榜前10
    python -m src.screener.launch_pattern --symbols HEIUSDT --json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
import time
import urllib3
from typing import Any, Dict, List, Optional

import requests

from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.screener.launch")

# JSON 输出时需干净 stdout：把根 logger 的 handler 重定向到 stderr
for _h in logging.getLogger().handlers:
    try:
        _h.setStream(sys.stderr)
    except Exception:  # noqa: BLE001
        pass

urllib3.disable_warnings()

BINANCE_SPOT_TICKER = "https://data-api.binance.vision/api/v3/ticker/24hr"
FAPI_BASE = "https://fapi.binance.com"
PROXY_CORS_SH = "https://proxy.cors.sh/"

STABLE_SYMBOLS = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "EUR", "AEUR",
                  "USDE", "USDY", "RLUSD", "USD1", "XUSD", "FRAX", "SUSD", "EURI"}

_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://cnb.cool"}


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


def fetch_spot_ticker() -> Dict[str, Dict[str, Any]]:
    r = requests.get(BINANCE_SPOT_TICKER, headers={"User-Agent": "Mozilla/5.0"},
                     timeout=30)
    out: Dict[str, Dict[str, Any]] = {}
    for t in r.json():
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in STABLE_SYMBOLS:
            continue
        out[sym] = {
            "price": float(t["lastPrice"]),
            "chg_pct": float(t["priceChangePercent"]),
            "quote_volume": float(t["quoteVolume"]),
        }
    return out


def fetch_klines(symbol: str, interval: str = "4h", limit: int = 360) -> List[Dict[str, Any]]:
    data = _fapi(f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if not data:
        return []
    out = []
    for k in data:
        out.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "quote_volume": float(k[7]),
        })
    return out


def fetch_oi_hist(symbol: str, period: str = "4h", limit: int = 400) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [
        {"time": d["timestamp"],
         "oi": float(d["sumOpenInterest"]),
         "oi_value": float(d["sumOpenInterestValue"])}
        for d in data
    ]


def fetch_funding(symbol: str, limit: int = 400) -> List[Dict[str, Any]]:
    data = _fapi(f"/fapi/v1/fundingRate?symbol={symbol}&limit={limit}")
    if not data:
        return []
    return [
        {"time": d["fundingTime"], "rate": float(d["fundingRate"])}
        for d in data
    ]


def fetch_ls_ratio(symbol: str, period: str = "4h", limit: int = 100) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/topLongShortAccountRatio?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [
        {"time": d["timestamp"], "long": float(d["longAccount"]),
         "short": float(d["shortAccount"]), "ratio": float(d["longShortRatio"])}
        for d in data
    ]


def fetch_taker_ratio(symbol: str, period: str = "4h", limit: int = 100) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/takerlongshortRatio?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [
        {"time": d["timestamp"], "buy_sell": float(d["buySellRatio"])}
        for d in data
    ]


def ts(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%m-%d %H:%M")


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _min(xs) -> float:
    xs = list(xs)
    return min(xs) if xs else 0.0


def _max(xs) -> float:
    xs = list(xs)
    return max(xs) if xs else 0.0


def analyze_launch(symbol: str) -> Dict[str, Any]:
    """分析单个合约标的的启动特征。"""
    result: Dict[str, Any] = {"symbol": symbol}
    klines = fetch_klines(symbol, "1d", 30)
    k4h = fetch_klines(symbol, "4h", 180)
    oi = fetch_oi_hist(symbol, "4h", 180)
    funding = fetch_funding(symbol, 300)
    ls = fetch_ls_ratio(symbol, "4h", 60)
    taker = fetch_taker_ratio(symbol, "4h", 60)

    # --- 日线：横盘期 + 放量信号 ---
    if klines:
        closes = [k["close"] for k in klines]
        vols = [k["quote_volume"] for k in klines]
        first_30 = klines[:-3]
        base_high = _max(k["high"] for k in first_30) if len(first_30) >= 3 else 0.0
        base_low = _min(k["low"] for k in first_30) if len(first_30) >= 3 else 0.0
        avg_vol = _mean(vols[:-2]) if len(vols) > 2 else 0.0
        recent = klines[-3:]
        result["daily"] = {
            "range_high": round(base_high, 6),
            "range_low": round(base_low, 6),
            "range_pct": round((base_high - base_low) / base_low * 100, 1) if base_low else 0,
            "avg_quote_vol": round(avg_vol / 1e6, 1),
            "recent3d": [
                {"date": ts(k["open_time"]), "close": round(k["close"], 6),
                 "chg_pct": round((k["close"] - k["open"]) / k["open"] * 100, 1),
                 "vol_M": round(k["quote_volume"] / 1e6, 1)}
                for k in recent
            ],
        }

    # --- OI：启动前后变化 ---
    if oi and len(oi) >= 2:
        early_oi = _mean(o["oi"] for o in oi[:10])
        peak_oi = _max(o["oi"] for o in oi)
        latest_oi = oi[-1]["oi"]
        result["oi"] = {
            "early_avg": round(early_oi / 1e6, 2),
            "peak": round(peak_oi / 1e6, 2),
            "latest": round(latest_oi / 1e6, 2),
            "peak_vs_early": round(peak_oi / early_oi, 2) if early_oi else 0,
            "latest_vs_peak": round(latest_oi / peak_oi, 2) if peak_oi else 0,
        }

    # --- 资金费率 ---
    if funding and len(funding) >= 8:
        f_early = [f["rate"] for f in funding[:8]]
        f_recent = [f["rate"] for f in funding[-4:]]
        result["funding"] = {
            "early_avg_bps": round(_mean(f_early) * 10000, 2),
            "recent_avg_bps": round(_mean(f_recent) * 10000, 2),
            "latest_bps": round(funding[-1]["rate"] * 10000, 2),
        }

    # --- 大户多空比 ---
    if ls and len(ls) >= 8:
        ls_early = [x["ratio"] for x in ls[:8]]
        ls_recent = [x["ratio"] for x in ls[-4:]]
        result["long_short"] = {
            "early_avg": round(_mean(ls_early), 3),
            "recent_avg": round(_mean(ls_recent), 3),
            "latest": round(ls[-1]["ratio"], 3),
        }

    # --- 主动买卖比 ---
    if taker and len(taker) >= 8:
        tk_recent = [x["buy_sell"] for x in taker[-8:]]
        result["taker"] = {
            "recent_avg": round(_mean(tk_recent), 3),
            "latest": round(taker[-1]["buy_sell"], 3),
        }

    return result


def top_gainers(n: int = 10) -> List[str]:
    spot = fetch_spot_ticker()
    liq = [s for s, v in spot.items() if v["quote_volume"] > 500_000]
    ranked = sorted(liq, key=lambda s: spot[s]["chg_pct"], reverse=True)
    return ranked[:n]


def main() -> None:
    parser = argparse.ArgumentParser(description="小币启动特征分析")
    parser.add_argument("--symbols", help="合约 symbol，逗号分隔，如 HEIUSDT,BICOUSDT")
    parser.add_argument("--top-gainers", type=int, default=0,
                        help="自动取当天现货涨幅榜前 N 个（需有合约）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    symbols: List[str] = []
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.top_gainers:
        symbols = top_gainers(args.top_gainers)

    if not symbols:
        parser.error("请提供 --symbols 或 --top-gainers")

    if args.json:
        # 保证 JSON 输出时 stdout 纯净：日志重定向到 stderr（须在分析前）
        for _h in logging.getLogger().handlers:
            try:
                _h.setStream(sys.stderr)
            except Exception:  # noqa: BLE001
                pass

    out: List[Dict[str, Any]] = []
    for i, sym in enumerate(symbols):
        logger.info("analyzing %s (%d/%d)", sym, i + 1, len(symbols))
        out.append(analyze_launch(sym))
        time.sleep(0.6)

    if args.json:
        # 保证 JSON 输出时 stdout 纯净：日志重定向到 stderr（须在分析前）
        for _h in logging.getLogger().handlers:
            try:
                _h.setStream(sys.stderr)
            except Exception:  # noqa: BLE001
                pass
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for r in out:
            print("=" * 60)
            print(r["symbol"])
            d = r.get("daily", {})
            if d:
                print(f"  横盘区间: {d.get('range_low')}~{d.get('range_high')} "
                      f"(振幅 {d.get('range_pct')}%)  日均额 ${d.get('avg_quote_vol')}M")
                for k in d.get("recent3d", []):
                    print(f"    {k['date']}  close={k['close']}  chg={k['chg_pct']:+.1f}%  vol=${k['vol_M']}M")
            oi = r.get("oi", {})
            if oi:
                print(f"  OI: 早期均值 {oi['early_avg']}M → 峰值 {oi['peak']}M "
                      f"(x{oi['peak_vs_early']}) → 当前 {oi['latest']}M")
            fr = r.get("funding", {})
            if fr:
                print(f"  费率: 早期 {fr['early_avg_bps']}bp → 近期 {fr['recent_avg_bps']}bp → 最新 {fr['latest_bps']}bp")
            ls = r.get("long_short", {})
            if ls:
                print(f"  大户多空比: 早期 {ls['early_avg']} → 近期 {ls['recent_avg']} → 最新 {ls['latest']}")
            tk = r.get("taker", {})
            if tk:
                print(f"  主动买卖比(近8期均值): {tk['recent_avg']}  最新 {tk['latest']}")


if __name__ == "__main__":
    main()
