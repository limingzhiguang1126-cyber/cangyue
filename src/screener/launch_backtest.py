# -*- coding: utf-8 -*-
"""涨幅榜妖币「启动早期特征」回测器（Launch Backtest）。

用途：回答「按当前选币标准（小市值+有合约+动量启动信号），能不能在早期抓到
涨幅榜妖币（HEI/BICO/TUT/SKYAI/ACE/HFT/CTSI/COOKIE/ZBT/KOMA 等）」。

回测思路（数据源：币安官方 fapi，经 CORS 代理）：
1. 对每个妖币，用 4h 合约 K 线定位「主升浪起点」：从用户关注的涨幅榜日期反推，
   取主升浪启动日对齐的第一根 4h bar。
2. 提取「启动前 72h」横盘/吸筹特征：振幅、均量、量能变化（vs 更早 72h）。
3. 提取「启动第一段放量 bar」特征：4h 涨幅、量能倍数（vs 前 72h 均量）、
   OI 变化（vs 更早 24 期均值）、主动买卖比、大户多空比。
4. 用当前 momo_screener 的阈值口径（4h 宽版）判断该 bar 是否满足「启动初期」信号，
   并统计「信号触发时距离主升浪还有多久、后续 24h/48h 涨幅」。
5. 汇总：按当前标准能抓到的比例、抓不到的共性（为什么）、以及改进建议。

用法：
    python -m src.screener.launch_backtest                # 默认回测
    python -m src.screener.launch_backtest --json         # JSON 输出
    python -m src.screener.launch_backtest --symbols HEIUSDT,BICOUSDT
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
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.screener.backtest")

urllib3.disable_warnings()

FAPI_BASE = "https://fapi.binance.com"
PROXY_CORS_SH = "https://proxy.cors.sh/"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://cnb.cool"}

# momo_screener 阈值口径（4h 宽版换算）：
#   4h 涨幅 >= 1.5%（温和启动）且 <= 25%（过热）
#   量能放大 >= 1.8x（vs 前 72h 均量）
#   OI 放大   >= 1.15x（vs 更早 24 期均值）
#   主动买卖比 >= 1.02、大户多空比 >= 0.95
MOM_MIN = 1.5
MOM_HOT = 25.0
VOL_MIN = 1.8
OI_MIN = 1.15
TAKER_MIN = 1.02
LS_MIN = 0.95

# 已涨妖币：symbol -> (主升浪起点 4h bar 时间, 主升浪起点前的收盘价)
# 依据涨幅榜日期（2026-08-04~08-06 主升，KOMA 07-30）手工标定。
HOT_COINS: Dict[str, Tuple[str, float]] = {
    "HEIUSDT":   ("08-04 00:00", 0.08688),
    "BICOUSDT":  ("08-02 00:00", 0.01172),
    "TUTUSDT":   ("08-03 00:00", 0.01757),
    "SKYAIUSDT": ("08-02 00:00", 0.02582),
    "ACEUSDT":   ("08-06 00:00", 0.07069),
    "HFTUSDT":   ("08-03 00:00", 0.00886),
    "CTSIUSDT":  ("08-06 00:00", 0.02147),
    "COOKIEUSDT":("08-06 00:00", 0.00839),
    "ZBTUSDT":   ("08-05 00:00", 0.10771),
    "KOMAUSDT":  ("07-30 00:00", 0.00789),
}


def _fapi(path: str, timeout: int = 50) -> Optional[Any]:
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


def _ts(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime("%m-%d %H:%M")


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def fetch_klines(symbol: str, interval: str = "4h", limit: int = 190) -> List[Dict[str, Any]]:
    data = _fapi(f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if not data:
        return []
    return [
        {"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
         "low": float(k[3]), "close": float(k[4]), "vol_usd": float(k[7])}
        for k in data
    ]


def fetch_oi_hist(symbol: str, period: str = "4h", limit: int = 190) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [{"time": int(d["timestamp"]), "oi_value": float(d["sumOpenInterestValue"])} for d in data]


def fetch_taker(symbol: str, period: str = "4h", limit: int = 190) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/takerlongshortRatio?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [{"time": int(d["timestamp"]), "buy_sell": float(d["buySellRatio"])} for d in data]


def fetch_ls(symbol: str, period: str = "4h", limit: int = 190) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/topLongShortAccountRatio?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [{"time": int(d["timestamp"]), "ratio": float(d["longShortRatio"])} for d in data]


def _bar_ts(kl, i: int) -> str:
    return _ts(kl[i]["ts"]) if 0 <= i < len(kl) else ""


def backtest_symbol(symbol: str, launch_ts: str, pre_price: float) -> Dict[str, Any]:
    """回测单个妖币的启动早期特征。"""
    res: Dict[str, Any] = {"symbol": symbol, "launch_ts": launch_ts, "pre_launch_price": pre_price}
    kl = fetch_klines(symbol)
    oi = fetch_oi_hist(symbol)
    tk = fetch_taker(symbol)
    ls = fetch_ls(symbol)
    if not kl:
        res["error"] = "no klines"
        return res

    idx = [i for i, k in enumerate(kl) if _ts(k["ts"]) >= launch_ts]
    if not idx:
        res["error"] = "launch_ts not in range"
        return res
    li = idx[0]

    # --- 启动前 72h 特征 ---
    pre = kl[max(0, li - 18):li]
    pre2 = kl[max(0, li - 36):max(0, li - 18)]
    if pre:
        lo = min(k["low"] for k in pre)
        hi = max(k["high"] for k in pre)
        avgv = _mean(k["vol_usd"] for k in pre)
        avgv2 = _mean(k["vol_usd"] for k in pre2) if pre2 else 0
        res["pre_72h"] = {
            "amp_pct": round((hi / lo - 1) * 100, 1) if lo else 0,
            "avg_vol_usd": round(avgv, 0),
            "vol_vs_earlier_x": round(avgv / avgv2, 2) if avgv2 else None,
        }

    # --- 主升浪起点 bar 特征 ---
    bar = kl[li]
    chg = (bar["close"] - bar["open"]) / bar["open"] * 100 if bar["open"] else 0
    avgv = _mean(k["vol_usd"] for k in pre) if pre else 0
    vol_mult = bar["vol_usd"] / avgv if avgv else 0

    oi_early = _mean(x["oi_value"] for x in oi[:24]) if oi and len(oi) >= 24 else 0
    oi_at = 0
    for x in oi:
        if x["time"] <= bar["ts"]:
            oi_at = x["oi_value"]
    oi_x = oi_at / oi_early if oi_early else 0

    tk_at = ls_at = None
    for x in tk:
        if x["time"] <= bar["ts"]:
            tk_at = x["buy_sell"]
    for x in ls:
        if x["time"] <= bar["ts"]:
            ls_at = x["ratio"]

    res["launch_bar"] = {
        "ts": _ts(bar["ts"]),
        "chg_pct": round(chg, 1),
        "vol_x": round(vol_mult, 1),
        "oi_x": round(oi_x, 2),
        "taker": round(tk_at, 3) if tk_at is not None else None,
        "ls": round(ls_at, 3) if ls_at is not None else None,
    }

    # --- momo 式信号判定（4h 宽版）---
    mom_ok = MOM_MIN <= chg <= MOM_HOT
    vol_ok = vol_mult >= VOL_MIN
    oi_ok = oi_x >= OI_MIN
    taker_ok = (tk_at is not None) and tk_at >= TAKER_MIN
    ls_ok = (ls_at is not None) and ls_at >= LS_MIN
    score = (mom_ok * 2 + vol_ok * 2 + oi_ok * 2 +
             taker_ok * 1.5 + ls_ok * 1.0)
    signal = "launching" if (mom_ok and vol_ok and oi_ok and score >= 6.0) else \
             ("watch" if score >= 4.0 else "quiet")
    res["signal"] = {
        "mom_ok": mom_ok, "vol_ok": vol_ok, "oi_ok": oi_ok,
        "taker_ok": taker_ok, "ls_ok": ls_ok,
        "score": round(score, 1), "status": signal,
    }

    # --- 触发点提前量：若起点bar未触发，往前找最早满足"量+OI齐升"的bar ---
    first = None
    for i in range(max(0, li - 12), li + 1):
        if i <= 0:
            continue
        b = kl[i]
        c = (b["close"] - b["open"]) / b["open"] * 100 if b["open"] else 0
        vm = b["vol_usd"] / avgv if avgv else 0
        oa = 0
        for x in oi:
            if x["time"] <= b["ts"]:
                oa = x["oi_value"]
        ox = oa / oi_early if oi_early else 0
        if vm >= 2.0 and ox >= 1.15 and c > 0:
            first = {"ts": _ts(b["ts"]), "chg_pct": round(c, 1),
                     "vol_x": round(vm, 1), "oi_x": round(ox, 2)}
            break
    res["first_vol_oi_signal"] = first

    # --- 后续收益 ---
    f24 = f48 = None
    if li + 6 < len(kl):
        f24 = (kl[li + 6]["close"] / bar["close"] - 1) * 100
    if li + 12 < len(kl):
        f48 = (kl[li + 12]["close"] / bar["close"] - 1) * 100
    # 48h 内最高点
    peak48 = None
    seg = kl[li:li + 13]
    if seg:
        peak48 = max(k["high"] for k in seg)
        peak48 = (peak48 / bar["close"] - 1) * 100
    res["forward"] = {
        "f24h_pct": round(f24, 1) if f24 is not None else None,
        "f48h_pct": round(f48, 1) if f48 is not None else None,
        "peak48h_pct": round(peak48, 1) if peak48 is not None else None,
    }
    return res


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="涨幅榜妖币启动早期特征回测")
    ap.add_argument("--symbols", help="自定义合约 symbol 列表（逗号分隔）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--output", default="", help="结果写入文件")
    args = ap.parse_args(argv)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        coins = {s: HOT_COINS.get(s, ("", 0.0)) for s in symbols}
    else:
        coins = HOT_COINS

    results: List[Dict[str, Any]] = []
    for sym, (lt, pp) in coins.items():
        logger.info("backtest %s launch@%s", sym, lt)
        results.append(backtest_symbol(sym, lt, pp))
        time.sleep(0.4)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n结果已写入: {args.output}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print("\n== 妖币启动早期特征回测 ==")
    for r in results:
        print("=" * 66)
        print(f"{r['symbol']}  主升起点 @ {r.get('launch_ts')}")
        pre = r.get("pre_72h", {})
        if pre:
            print(f"  启动前72h: 振幅{pre.get('amp_pct',0):.1f}%  量能vs更早 x{pre.get('vol_vs_earlier_x','--')}")
        lb = r.get("launch_bar", {})
        if lb:
            print(f"  起点bar: chg{lb.get('chg_pct',0):+.1f}% vol{lb.get('vol_x',0):.1f}x "
                  f"OI{lb.get('oi_x',0):.2f}x taker={lb.get('taker')} LS={lb.get('ls')}")
        sig = r.get("signal", {})
        if sig:
            print(f"  momo判定: score={sig.get('score')} status={sig.get('status')} "
                  f"(mom={sig.get('mom_ok')} vol={sig.get('vol_ok')} oi={sig.get('oi_ok')})")
        fw = r.get("forward", {})
        if fw:
            print(f"  后续: 24h {fw.get('f24h_pct')}%  48h {fw.get('f48h_pct')}%  48h峰值 {fw.get('peak48h_pct')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
