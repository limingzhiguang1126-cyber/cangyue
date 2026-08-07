# -*- coding: utf-8 -*-
"""涨幅榜启动特征分析器（Launch Analyzer）

针对最近涨幅榜小币，从「启动前 / 启动前期」两个视角量化：
  - 现货/合约：价格、成交量（放量倍数、起爆点）
  - 合约持仓量（OI）变化：是否提前堆 OI（吸筹/埋伏）、起爆后 OI 增幅
  - 资金费率：启动前后变化、当前是否过热
  - 当前状态：距高点回撤、是否仍创新高（避免“追在山顶”）

数据源：
  - 币安现货 K 线：data-api.binance.vision（国内可达，仅返回 TRADING 对）
  - Gate 永续 contract_stats / funding_rate：api.gateio.ws（免费、无需 key）
  - Binance Alpha（可选）：复用 binance_smallcap 的 fetch_alpha_tokens()

用法：
  python -m src.screener.launch_analyzer --symbols HEIUSDT,BICOUSDT,TUTUSDT
  python -m src.screener.launch_analyzer --top 10
  python -m src.screener.launch_analyzer --top 10 --json --output data/launch_analysis.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("fin-alert.screener.launch")

SPOT_BASE = "https://data-api.binance.vision"
GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fin-alert/0.1)"}

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")


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


def _ts_hour(ts_ms: int) -> str:
    return _dt.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%m-%d %H:%M")


# ----------------------------------------------------------------------
# 1. 现货 K 线（币安 data-api）
# ----------------------------------------------------------------------
def fetch_spot_klines(symbol: str, interval: str = "1h", limit: int = 168) -> List[Dict[str, Any]]:
    """返回按时间升序的 K 线列表。symbol 需带报价币（如 HEIUSDT）。"""
    url = f"{SPOT_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = _get_json(url, params=params, timeout=25)
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


# ----------------------------------------------------------------------
# 2. Gate 永续 OI / 资金费率
# ----------------------------------------------------------------------
def fetch_gate_oi(symbol: str, interval: str = "1h", limit: int = 168) -> List[Dict[str, Any]]:
    """Gate 永续 contract_stats（含 open_interest / open_interest_usd / lsr_taker / 清算）。
    symbol 形如 HEI_USDT。"""
    url = f"{GATE_BASE}/contract_stats"
    params = {"contract": symbol, "interval": interval, "limit": limit}
    data = _get_json(url, params=params, timeout=25)
    out = []
    for x in data:
        out.append(
            {
                "ts": int(x.get("time", 0)),
                "oi": _f(x.get("open_interest")),
                "oi_usd": _f(x.get("open_interest_usd")),
                "lsr_taker": _f(x.get("lsr_taker")),
                "long_liq_usd": _f(x.get("long_liq_usd")) + _f(x.get("long_liq_usd_new")),
                "short_liq_usd": _f(x.get("short_liq_usd")) + _f(x.get("short_liq_usd_new")),
            }
        )
    out.sort(key=lambda r: r["ts"])
    return out


def fetch_gate_funding(symbol: str, limit: int = 48) -> List[Dict[str, Any]]:
    """Gate 永续历史资金费率（每 8h 结算）。"""
    url = f"{GATE_BASE}/funding_rate"
    params = {"contract": symbol, "limit": limit}
    data = _get_json(url, params=params, timeout=25)
    out = [{"ts": int(x.get("t", 0)), "rate": _f(x.get("r"))} for x in data]
    out.sort(key=lambda r: r["ts"])
    return out


# ----------------------------------------------------------------------
# 3. 特征计算
# ----------------------------------------------------------------------
def _find_launch_point(klines: List[Dict[str, Any]], vol_mult: float = 5.0, min_chg: float = 3.0) -> Optional[int]:
    """起爆点：成交量首次 > 前 72h 均量*vol_mult 且单根涨幅 > min_chg%。"""
    n = len(klines)
    if n < 73:
        return None
    for i in range(72, n):
        base = sum(k["vol_usd"] for k in klines[i - 72 : i]) / 72.0
        if base <= 0:
            continue
        chg = (klines[i]["close"] - klines[i]["open"]) / klines[i]["open"] * 100 if klines[i]["open"] else 0.0
        if klines[i]["vol_usd"] > base * vol_mult and chg > min_chg:
            return i
    return None


def analyze_symbol(symbol: str) -> Dict[str, Any]:
    """综合分析单个标的上币安现货（若无则降级到合约/Alpha 视角）。"""
    base = symbol[:-4] if symbol.upper().endswith("USDT") else symbol
    spot_symbol = f"{base}USDT"
    gate_symbol = f"{base}_USDT"

    res: Dict[str, Any] = {"symbol": base, "spot_symbol": spot_symbol, "gate_symbol": gate_symbol}

    # --- 现货 ---
    try:
        kl = fetch_spot_klines(spot_symbol)
        res["has_spot"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s spot fetch failed: %s", symbol, exc)
        res["has_spot"] = False
        kl = []

    if kl:
        closes = [k["close"] for k in kl]
        vols = [k["vol_usd"] for k in kl]
        peak = max(k["high"] for k in kl)
        peak_ts = max(kl, key=lambda k: k["high"])["ts"]
        cur = closes[-1]
        li = _find_launch_point(kl)
        last24 = kl[-24:]
        avg_prev = sum(vols[:-24]) / max(len(vols) - 24, 1)
        avg24 = sum(k["vol_usd"] for k in last24) / len(last24)

        res["spot"] = {
            "first_ts": kl[0]["ts"],
            "last_ts": kl[-1]["ts"],
            "price_7d_ago": closes[0],
            "price_now": cur,
            "peak": peak,
            "peak_ts": peak_ts,
            "drawdown_from_peak_pct": (cur / peak - 1) * 100 if peak else 0.0,
            "chg_7d_pct": (cur / closes[0] - 1) * 100 if closes[0] else 0.0,
            "vol_24h_usd": sum(k["vol_usd"] for k in last24),
            "vol_24h_avg_usd": avg24,
            "vol_prev_avg_usd": avg_prev,
            "vol_ratio_24h_vs_prev": avg24 / avg_prev if avg_prev else None,
        }
        if li is not None:
            base72 = sum(k["vol_usd"] for k in kl[max(0, li - 72) : li]) / 72.0
            pre24 = kl[max(0, li - 24) : li]
            lo = min(k["low"] for k in pre24)
            hi = max(k["high"] for k in pre24)
            res["spot"]["launch"] = {
                "ts": kl[li]["ts"],
                "launch_bar_vol_usd": kl[li]["vol_usd"],
                "launch_bar_chg_pct": (kl[li]["close"] - kl[li]["open"]) / kl[li]["open"] * 100 if kl[li]["open"] else 0.0,
                "avg_vol_72h_before_usd": base72,
                "launch_vol_mult": kl[li]["vol_usd"] / base72 if base72 else None,
                "pre24h_range_pct": (hi / lo - 1) * 100 if lo else 0.0,
            }

    # --- 合约 OI ---
    try:
        oi = fetch_gate_oi(gate_symbol)
        res["has_gate_futures"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s gate oi fetch failed: %s", symbol, exc)
        oi = []
        res["has_gate_futures"] = False

    if oi and len(oi) >= 24:
        n = len(oi)
        pre = oi[: n // 3]
        post = oi[n // 3 :]
        oi_pre_avg = sum(x["oi"] for x in pre) / len(pre)
        oi_post_avg = sum(x["oi"] for x in post) / len(post)
        oi_usd_pre_avg = sum(x["oi_usd"] for x in pre) / len(pre)
        oi_usd_post_avg = sum(x["oi_usd"] for x in post) / len(post)
        oi_peak_usd = max(x["oi_usd"] for x in oi)
        res["futures"] = {
            "oi_avg_before_usd": oi_usd_pre_avg,
            "oi_avg_now_usd": oi_usd_post_avg,
            "oi_change_x": oi_post_avg / oi_pre_avg if oi_pre_avg else None,
            "oi_usd_change_x": oi_usd_post_avg / oi_usd_pre_avg if oi_usd_pre_avg else None,
            "oi_now_usd": oi[-1]["oi_usd"],
            "oi_peak_usd": oi_peak_usd,
            "oi_drawdown_from_peak_pct": (oi[-1]["oi_usd"] / oi_peak_usd - 1) * 100 if oi_peak_usd else 0.0,
            "lsr_taker_now": oi[-1]["lsr_taker"],
            "lsr_taker_max": max(x["lsr_taker"] for x in oi),
            "max_liq_usd": max(x["long_liq_usd"] + x["short_liq_usd"] for x in oi),
        }

    # --- 资金费率 ---
    try:
        fr = fetch_gate_funding(gate_symbol)
        res["has_funding"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s gate funding fetch failed: %s", symbol, exc)
        fr = []
        res["has_funding"] = False

    if fr and len(fr) >= 10:
        n = len(fr)
        pre = fr[: n // 3]
        post = fr[n // 3 :]
        res["funding"] = {
            "avg_before_pct": sum(x["rate"] for x in pre) / len(pre) * 100,
            "avg_now_pct": sum(x["rate"] for x in post) / len(post) * 100,
            "now_pct": fr[-1]["rate"] * 100,
            "max_pct": max(x["rate"] for x in fr) * 100,
            "min_pct": min(x["rate"] for x in fr) * 100,
        }

    return res


# ----------------------------------------------------------------------
# 4. 今日涨幅榜
# ----------------------------------------------------------------------
def fetch_top_gainers(top: int = 12, min_vol_usd: float = 1_000_000) -> List[Dict[str, Any]]:
    """币安现货 24h 涨幅榜（USDT 对，过滤低流动性）。"""
    url = f"{SPOT_BASE}/api/v3/ticker/24hr"
    data = _get_json(url, timeout=40)
    rows = [
        {
            "symbol": x["symbol"],
            "pct": _f(x["priceChangePercent"]),
            "last": _f(x["lastPrice"]),
            "vol_usd": _f(x["quoteVolume"]),
        }
        for x in data
        if x["symbol"].endswith("USDT") and _f(x["quoteVolume"]) > min_vol_usd
    ]
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows[:top]


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="涨幅榜启动特征分析器")
    ap.add_argument("--symbols", type=str, default="", help="逗号分隔的标的列表（如 HEIUSDT,BICOUSDT,TUTUSDT）")
    ap.add_argument("--top", type=int, default=0, help="自动取今日涨幅榜前 N（需 --min-vol）")
    ap.add_argument("--min-vol", type=float, default=1_000_000, help="涨幅榜最低 24h 成交额 USD")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--output", type=str, default="", help="结果写入文件（同时打印表格）")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.top > 0:
        gainers = fetch_top_gainers(top=args.top, min_vol_usd=args.min_vol)
        symbols = [g["symbol"] for g in gainers]
        print(f"今日涨幅榜 Top{len(symbols)}: {', '.join(symbols)}")
    else:
        ap.error("需要 --symbols 或 --top")

    results: List[Dict[str, Any]] = []
    for sym in symbols:
        base = sym[:-4] if sym.endswith("USDT") else sym
        logger.info("analyzing %s ...", base)
        try:
            r = analyze_symbol(base)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s analysis failed: %s", base, exc)
            r = {"symbol": base, "error": str(exc)}
        results.append(r)
        time.sleep(0.3)

    if args.output:
        os.makedirs(DATA_DIR, exist_ok=True)
        out_path = args.output if os.path.isabs(args.output) else os.path.join(DATA_DIR, args.output)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n结果已写入: {out_path}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    # 表格
    print("\n%-8s %-10s %-8s %-8s %-8s %-9s %-9s %-8s %-9s" % (
        "标的", "7天涨幅", "现价", "峰值", "回撤", "OI增幅x", "当前OI$", "费率%", "24h量比"))
    print("-" * 100)
    for r in results:
        sp = r.get("spot", {})
        fu = r.get("futures", {})
        fd = r.get("funding", {})
        sym = r["symbol"]
        chg = sp.get("chg_7d_pct")
        now = sp.get("price_now")
        peak = sp.get("peak")
        dd = sp.get("drawdown_from_peak_pct")
        oix = fu.get("oi_change_x")
        oin = fu.get("oi_now_usd")
        fr_now = fd.get("now_pct")
        vratio = sp.get("vol_ratio_24h_vs_prev")
        print("%-8s %-10s %-8s %-8s %-8s %-9s %-9s %-8s %-9s" % (
            sym,
            f"{chg:+.1f}%" if chg is not None else "-",
            f"{now:.6g}" if now else "-",
            f"{peak:.6g}" if peak else "-",
            f"{dd:+.1f}%" if dd is not None else "-",
            f"{oix:.1f}x" if oix else "-",
            f"{oin/1e6:.2f}M" if oin else "-",
            f"{fr_now:+.4f}" if fr_now is not None else "-",
            f"{vratio:.1f}x" if vratio else "-",
        ))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
