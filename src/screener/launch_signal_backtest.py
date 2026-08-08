# -*- coding: utf-8 -*-
"""v1.2 选币标准历史回测器（Launch Signal Backtest）。

用途：验证 2026-08-08 定稿的 v1.2 选币标准在历史数据上的有效性。
回测对象 = 涨幅榜妖币（正样本）+ 追高失败币（负样本）。

## v1.2 标准（v1.1 + 用户确认的调整，2026-08-08）

| 线 | 触发条件 | 动作 |
|---|---|---|
| 🔔 通知线① | 5m 涨幅 ≥ +6%（收盘口径）且距 4h 本波高点回撤 > -3% | 通知 + 进观察池 |
| 🔔 主信号② | 4h 涨幅 ≥ +15% 且距本波高点回撤 < 20% | 通知 + 重点分析 |
| 📡 辅助线③ | 4h 涨幅 2%~8% 且 4h 量能 ≥ 3x、OI 放大≥1.15x | 提前埋伏观察 |
| ⛔ 一票否决 | 费率 > +0.3% 或 < -1.0% / OI 较峰值回落 > 30%（辅助线③暂缓） | 不碰 |

## 回测方法

1. 对每个样本币拉取币安 fapi 历史数据（4h K线 + OI 历史 + 资金费率历史 + 5m K线）。
2. 逐根 4h bar 扫描，模拟「实时判定」：每个 bar 只用当前及之前的信息，
   判定是否触发 通知线① / 主信号② / 辅助线③ / 一票否决。
3. 统计每个触发点的「后续收益」：+24h / +48h / 48h 峰值（相对触发时收盘价）。
4. 汇总各条线的：
   - 触发次数
   - 命中率（后续 48h 收益 > 0 的比例）
   - 平均收益 / 中位收益
   - 最大亏损（防止「高命中但单次爆亏」的陷阱）
5. 同时给出「基线对照」：同一批币在同一时段内所有 bar 的随机平均 48h 收益，
   用来衡量信号是否真的跑赢随机。

数据源：币安 fapi（经 proxy.cors.sh CORS 代理），全部实时抓取。

用法：
    python -m src.screener.launch_signal_backtest                # 默认回测
    python -m src.screener.launch_signal_backtest --json         # JSON 输出
    python -m src.screener.launch_signal_backtest --symbols HEIUSDT,BICOUSDT
    python -m src.screener.launch_signal_backtest --output data/signal_backtest.json
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
logger = logging.getLogger("fin-alert.screener.signal_backtest")

urllib3.disable_warnings()

FAPI_BASE = "https://fapi.binance.com"
PROXY_CORS_SH = "https://proxy.cors.sh/"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://cnb.cool"}

# ---------------------------------------------------------------------------
# v1.2 标准阈值（与 v11_signal_watcher 保持一致）
# ---------------------------------------------------------------------------
# 通知线①：5m 涨幅门槛 + 距 4h 高点回撤过滤
SIGNAL_5M_MIN = 6.0          # 5m 收盘涨幅 ≥ +6%
SIGNAL_5M_DD_MAX = -3.0      # 距 4h 本波高点回撤 > -3% 才有效

# 主信号②：4h 涨幅门槛 + 距高点回撤上限
SIGNAL_4H_MIN = 15.0         # 4h 涨幅 ≥ +15%
DRAWDOWN_MAX = 20.0          # 距本波高点回撤 < 20%（即回撤 < -20% 不触发）

# 辅助线③：4h 温和放量
AUX_4H_MIN = 2.0             # 4h 涨幅 ≥ +2%
AUX_4H_MAX = 8.0             # 4h 涨幅 ≤ +8%
AUX_VOL_X = 3.0              # 4h 量能 ≥ 前 24h 均量 3x
AUX_OI_X_MIN = 1.15          # 4h OI 放大 ≥ 1.15x（滤掉放量下跌噪音）

# 一票否决：资金费率 / OI 回撤
VETO_FUNDING_HIGH = 0.30     # 费率 > +0.3%
VETO_FUNDING_LOW = -1.00     # 费率 < -1.0%（负费率放宽到 -1%）
VETO_OI_DRAWDOWN = 30.0      # OI 较峰值回落 > 30%

# 判定窗口（单位：根 4h bar）
LOOKBACK_4H = 24              # 前 24 根（96h）为均量基准
HOLD_4H = 12                  # 持有 12 根（48h）
PEAK_4H = 12                  # 48h 峰值窗口
LAUNCH_HIGH_WINDOW = 7        # 「本波拉升高点」窗口：触发 bar 及前 6 根（24h）

# 样本集：symbol -> 备注
POSITIVE_COINS: Dict[str, str] = {
    "HEIUSDT": "妖币·主升龙头",
    "BICOUSDT": "妖币·健康启动",
    "TUTUSDT": "妖币·半山腰",
    "SKYAIUSDT": "妖币·纯合约慢牛",
    "ACEUSDT": "妖币·刚启动",
    "CTSIUSDT": "妖币·情绪过热",
    "COOKIEUSDT": "妖币·温和放量",
    "ZBTUSDT": "妖币·合约资金没进",
    "KOMAUSDT": "妖币·唯一完美命中",
    "HFTUSDT": "妖币·主升末端",
}
NEGATIVE_COINS: Dict[str, str] = {
    "DODOUSDT": "追高失败对照",
    "SYNUSDT": "追高失败对照",
}


def _fapi(path: str, timeout: int = 50) -> Optional[Any]:
    """经 CORS 代理访问币安 fapi。"""
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


def _median(xs) -> float:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def fetch_klines(symbol: str, interval: str = "4h", limit: int = 200) -> List[Dict[str, Any]]:
    data = _fapi(f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if not data:
        return []
    return [
        {"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
         "low": float(k[3]), "close": float(k[4]), "vol_usd": float(k[7])}
        for k in data
    ]


def fetch_klines_paged(symbol: str, interval: str = "5m", bars: int = 3000) -> List[Dict[str, Any]]:
    """分页拉取更长历史 K 线（币安单次 limit 上限 1500）。"""
    out: List[Dict[str, Any]] = []
    end_time = None
    while len(out) < bars:
        path = f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=1500"
        if end_time:
            path += f"&endTime={end_time}"
        data = _fapi(path)
        if not data:
            break
        rows = [
            {"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "vol_usd": float(k[7])}
            for k in data
        ]
        if not rows:
            break
        out = rows + out
        first_ts = rows[0]["ts"]
        if len(rows) < 1500 or first_ts == end_time:
            break
        end_time = first_ts
        time.sleep(0.3)
    return out[-bars:]


def fetch_oi_hist(symbol: str, period: str = "4h", limit: int = 200) -> List[Dict[str, Any]]:
    data = _fapi(f"/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}")
    if not data:
        return []
    return [{"time": int(d["timestamp"]), "oi_value": float(d["sumOpenInterestValue"])} for d in data]


def fetch_funding(symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
    """历史资金费率（每 8h 结算一条）。"""
    data = _fapi(f"/fapi/v1/fundingRate?symbol={symbol}&limit={limit}")
    if not data:
        return []
    return [{"time": int(d["fundingTime"]), "rate": float(d["fundingRate"])} for d in data]


def _funding_at(funding: List[Dict[str, Any]], ts: int) -> Optional[float]:
    """取 ts 时刻之前最近一条资金费率（%）。"""
    rate = None
    for f in funding:
        if f["time"] <= ts:
            rate = f["rate"] * 100.0
        else:
            break
    return rate


def _oi_at(oi: List[Dict[str, Any]], ts: int) -> Optional[float]:
    """取 ts 时刻之前最近一条 OI。"""
    v = None
    for o in oi:
        if o["time"] <= ts:
            v = o["oi_value"]
        else:
            break
    return v


def _oi_peak_before(oi: List[Dict[str, Any]], ts: int, window: int = 24) -> Optional[float]:
    """取 ts 之前 window 根 OI 序列的峰值。"""
    vals = [o["oi_value"] for o in oi if o["time"] <= ts]
    if not vals:
        return None
    return max(vals[-window:])


def _classify_bar(
    kl: List[Dict[str, Any]],
    oi: List[Dict[str, Any]],
    funding: List[Dict[str, Any]],
    i: int,
) -> Tuple[str, Dict[str, Any]]:
    """判定第 i 根 4h bar 是否触发 v1.2 标准的信号（不含 5m 通知线）。

    返回 (signal_key, detail)。signal_key ∈ {"signal2", "aux3", "veto", "none"}。
    若同时触发主信号②与一票否决，则返回 "veto"（一票否决优先）。
    """
    if i < LOOKBACK_4H + 1:
        return "none", {}
    bar = kl[i]
    prev = kl[i - 1]
    base = kl[i - LOOKBACK_4H:i]

    chg_4h = (bar["close"] - prev["close"]) / prev["close"] * 100 if prev["close"] else 0.0
    # 「本波拉升高点」：触发 bar 及前 6 根（24h）的最高 high，而非 96h 横盘高点
    launch_window = kl[max(0, i - LAUNCH_HIGH_WINDOW + 1):i + 1]
    peak_high = max(k["high"] for k in launch_window)
    drawdown = (bar["close"] / peak_high - 1) * 100 if peak_high else 0.0
    avg_vol = _mean(k["vol_usd"] for k in base)
    vol_x = bar["vol_usd"] / avg_vol if avg_vol else 0.0

    # 资金费率（一票否决依据）
    fr = _funding_at(funding, bar["ts"])
    # OI 较峰值回撤（一票否决依据）
    oi_before = [o["oi_value"] for o in oi if o["time"] <= bar["ts"]]
    oi_now = oi_before[-1] if oi_before else None
    oi_peak = _oi_peak_before(oi, bar["ts"], window=LOOKBACK_4H)
    oi_dd = None
    if oi_now is not None and oi_peak and oi_peak > 0:
        oi_dd = (oi_now / oi_peak - 1) * 100
    # OI 放大倍数（持仓进场信号）：当前 vs 早期 10 条均值
    oi_x = 0.0
    if len(oi_before) >= 12:
        early = _mean(oi_before[:10])
        if early > 0 and oi_now is not None:
            oi_x = oi_now / early

    detail = {
        "ts": _ts(bar["ts"]),
        "chg_4h": round(chg_4h, 2),
        "drawdown": round(drawdown, 2),
        "vol_x": round(vol_x, 2),
        "funding_pct": round(fr, 4) if fr is not None else None,
        "oi_dd_pct": round(oi_dd, 2) if oi_dd is not None else None,
        "oi_x": round(oi_x, 2),
    }

    # 一票否决条件（只对「本会触发」的信号生效）
    funding_veto = False
    if fr is not None and (fr > VETO_FUNDING_HIGH or fr < VETO_FUNDING_LOW):
        funding_veto = True
    oi_veto = False
    if oi_dd is not None and oi_dd < -VETO_OI_DRAWDOWN:
        oi_veto = True

    # 主信号②：4h 涨幅 ≥15% 且 距本波高点回撤 <20%
    is_signal2 = chg_4h >= SIGNAL_4H_MIN and drawdown > -DRAWDOWN_MAX
    # 辅助线③：4h 涨 2~8% 且 量能 ≥3x、OI 放大≥1.15x
    is_aux3 = (AUX_4H_MIN <= chg_4h <= AUX_4H_MAX
               and vol_x >= AUX_VOL_X
               and oi_x >= AUX_OI_X_MIN)

    # v1.2：OI 回落>30% 在「辅助线③」上暂缓（回测显示 OI 回落后仍常续涨）
    if (is_signal2 or is_aux3) and (funding_veto or (oi_veto and is_signal2)):
        detail["veto"] = "funding" if funding_veto else "oi"
        detail["veto_signal"] = "signal2" if is_signal2 else "aux3"
        return "veto", detail

    if is_signal2:
        return "signal2", detail

    if is_aux3:
        return "aux3", detail

    return "none", detail


def _drawdown_at(kl: List[Dict[str, Any]], ts: int) -> Optional[float]:
    """在 ts 时刻所在 4h bar 上，计算「本波拉升高点」窗口内的回撤(%)。"""
    idx = None
    for k in kl:
        if k["ts"] <= ts:
            idx = k
        else:
            break
    if idx is None:
        return None
    pos = kl.index(idx)
    start = max(0, pos - LAUNCH_HIGH_WINDOW + 1)
    window = kl[start:pos + 1]
    peak = max(k["high"] for k in window)
    cur = idx["close"]
    if not peak:
        return None
    return (cur / peak - 1) * 100


def _classify_5m(sym_5m: List[Dict[str, Any]], i: int, kl: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, Dict[str, Any]]:
    """判定 5m 通知线①：第 i 根 5m bar 收盘涨幅 ≥ +6%，且距 4h 本波高点回撤 > -3%。"""
    if i < 1:
        return False, {}
    bar = sym_5m[i]
    prev = sym_5m[i - 1]
    chg = (bar["close"] - prev["close"]) / prev["close"] * 100 if prev["close"] else 0.0
    if chg < SIGNAL_5M_MIN:
        return False, {}
    if kl is not None:
        dd = _drawdown_at(kl, bar["ts"])
        if dd is not None and dd <= SIGNAL_5M_DD_MAX:
            return False, {}
    return True, {
        "ts": _ts(bar["ts"]),
        "chg_5m": round(chg, 2),
    }


def backtest_symbol(symbol: str) -> Dict[str, Any]:
    """回测单个标的（4h 主信号② + 辅助线③ + 5m 通知线①）。"""
    res: Dict[str, Any] = {"symbol": symbol}
    kl = fetch_klines(symbol, interval="4h", limit=200)
    oi = fetch_oi_hist(symbol, period="4h", limit=200)
    funding = fetch_funding(symbol, limit=100)
    k5 = fetch_klines_paged(symbol, interval="5m", bars=6000)

    if not kl or len(kl) < LOOKBACK_4H + 12:
        res["error"] = f"insufficient 4h klines ({len(kl)})"
        return res

    res["range"] = {
        "start": _ts(kl[0]["ts"]),
        "end": _ts(kl[-1]["ts"]),
        "bars_4h": len(kl),
    }

    # --- 4h 信号扫描 ---
    sig2: List[Dict[str, Any]] = []
    aux3: List[Dict[str, Any]] = []
    veto: List[Dict[str, Any]] = []
    for i in range(LOOKBACK_4H + 1, len(kl)):
        key, detail = _classify_bar(kl, oi, funding, i)
        if key == "signal2":
            detail["fwd"] = _forward_returns(kl, i)
            sig2.append(detail)
        elif key == "aux3":
            detail["fwd"] = _forward_returns(kl, i)
            aux3.append(detail)
        elif key == "veto":
            detail["fwd"] = _forward_returns(kl, i)
            veto.append(detail)

    res["signal2"] = sig2
    res["aux3"] = aux3
    res["veto"] = veto

    # --- 基线：所有可持有 bar 的平均后续收益（随机买入对照）---
    baseline_fwd = [_forward_returns(kl, i) for i in range(LOOKBACK_4H + 1, len(kl) - PEAK_4H)]
    res["baseline_raw"] = baseline_fwd
    res["baseline"] = _summarize([{"fwd": f} for f in baseline_fwd])

    # --- 5m 通知线①扫描 ---
    sig1: List[Dict[str, Any]] = []
    if k5 and len(k5) >= 300:
        # 每根 5m bar 用收盘价判定涨幅≥6%（收盘口径），并直接基于 5m 序列算后续收益
        for i in range(1, len(k5) - 288):  # 预留 24h 后续数据
            hit, det = _classify_5m(k5, i, kl=kl)
            if hit:
                det["fwd"] = _forward_returns_5m(k5, i)
                sig1.append(det)
    res["signal1_5m"] = sig1

    return res


def _forward_returns_5m(k5: List[Dict[str, Any]], i: int) -> Dict[str, Any]:
    """5m 信号触发后 +6h/+24h 收益与 24h 峰值（基于 5m 收盘价）。"""
    f6 = f24 = peak = None
    # 6h = 72 根 5m
    if i + 72 < len(k5):
        f6 = (k5[i + 72]["close"] / k5[i]["close"] - 1) * 100
    # 24h = 288 根 5m
    if i + 288 < len(k5):
        f24 = (k5[i + 288]["close"] / k5[i]["close"] - 1) * 100
    seg = k5[i:i + 289]
    if seg:
        peak = (max(k["high"] for k in seg) / k5[i]["close"] - 1) * 100
    return {
        "f6h": round(f6, 2) if f6 is not None else None,
        "f24h": round(f24, 2) if f24 is not None else None,
        "peak24h": round(peak, 2) if peak is not None else None,
    }


def _forward_returns(kl: List[Dict[str, Any]], i: int) -> Dict[str, Any]:
    """第 i 根 bar 触发后 +24h/+48h 收益与 48h 峰值收益（基于收盘价）。"""
    f24 = f48 = peak48 = None
    if i + 6 < len(kl):
        f24 = (kl[i + 6]["close"] / kl[i]["close"] - 1) * 100
    if i + 12 < len(kl):
        f48 = (kl[i + 12]["close"] / kl[i]["close"] - 1) * 100
    seg = kl[i:i + PEAK_4H + 1]
    if seg:
        peak48 = (max(k["high"] for k in seg) / kl[i]["close"] - 1) * 100
    return {
        "f24h": round(f24, 2) if f24 is not None else None,
        "f48h": round(f48, 2) if f48 is not None else None,
        "peak48h": round(peak48, 2) if peak48 is not None else None,
    }


def _forward_returns_from_ts(kl: List[Dict[str, Any]], ts: int) -> Dict[str, Any]:
    """从任意 ts 时刻之后（找最近的 4h bar 起点）统计后续收益。"""
    idx = None
    for i, k in enumerate(kl):
        if k["ts"] >= ts:
            idx = i
            break
    if idx is None or idx + 1 >= len(kl):
        return {}
    return _forward_returns(kl, idx)


def _summarize(items: List[Dict[str, Any]], key_48: str = "f48h", key_24: str = "f24h", key_peak: str = "peak48h") -> Dict[str, Any]:
    """对一组触发点做收益统计。

    Args:
        items: 触发点列表。
        key_48: 主收益字段（4h 用 f48h，5m 用 f24h）。
        key_24: 短周期收益字段（4h 用 f24h，5m 用 f6h）。
        key_peak: 峰值字段（4h 用 peak48h，5m 用 peak24h）。
    """
    f48 = [x["fwd"][key_48] for x in items if x.get("fwd", {}).get(key_48) is not None]
    f24 = [x["fwd"][key_24] for x in items if x.get("fwd", {}).get(key_24) is not None]
    peak = [x["fwd"][key_peak] for x in items if x.get("fwd", {}).get(key_peak) is not None]
    n = len(items)
    if n == 0:
        return {"count": 0}
    return {
        "count": n,
        "f24h_mean": round(_mean(f24), 2) if f24 else None,
        "f24h_median": round(_median(f24), 2) if f24 else None,
        "f48h_mean": round(_mean(f48), 2) if f48 else None,
        "f48h_median": round(_median(f48), 2) if f48 else None,
        "hit_rate_48h": round(sum(1 for v in f48 if v > 0) / len(f48) * 100, 1) if f48 else None,
        "peak48h_mean": round(_mean(peak), 2) if peak else None,
        "max_loss_48h": round(min(f48), 2) if f48 else None,
        "best_48h": round(max(f48), 2) if f48 else None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v1 选币标准历史回测")
    ap.add_argument("--symbols", help="自定义合约 symbol 列表（逗号分隔）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--output", default="", help="结果写入文件")
    args = ap.parse_args(argv)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        coins: Dict[str, str] = {s: "" for s in symbols}
    else:
        coins = dict(POSITIVE_COINS)
        coins.update(NEGATIVE_COINS)

    results: List[Dict[str, Any]] = []
    for sym, note in coins.items():
        logger.info("backtest %s (%s)", sym, note)
        r = backtest_symbol(sym)
        r["note"] = note
        results.append(r)
        time.sleep(0.3)

    # 汇总
    all_sig2 = [d for r in results for d in r.get("signal2", [])]
    all_aux3 = [d for r in results for d in r.get("aux3", [])]
    all_sig1 = [d for r in results for d in r.get("signal1_5m", [])]
    all_veto = [d for r in results for d in r.get("veto", [])]
    all_baseline = [{"fwd": f} for r in results for f in r.get("baseline_raw", [])]
    summary = {
        "signal2_main": _summarize(all_sig2),
        "aux3": _summarize(all_aux3),
        "signal1_5m": _summarize(all_sig1, key_48="f24h", key_24="f6h", key_peak="peak24h"),
        "veto": _summarize(all_veto),
        "baseline": _summarize(all_baseline),
    }

    out = {"summary": summary, "results": results}
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n结果已写入: {args.output}")

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # 表格输出
    print("\n" + "=" * 76)
    print("v1 选币标准历史回测汇总")
    print("=" * 76)
    for name, key, k48, k24, kpeak in [
            ("🔔 通知线① (5m≥6%+距高点>-3%，后续24h)", "signal1_5m", "f24h", "f6h", "peak24h"),
            ("🔔 主信号② (4h≥15%+回撤<20%)", "signal2_main", "f48h", "f24h", "peak48h"),
            ("📡 辅助线③ (4h涨2-8%+量能3x+OI1.15x)", "aux3", "f48h", "f24h", "peak48h"),
            ("⛔ 一票否决(命中但被否决)", "veto", "f48h", "f24h", "peak48h"),
            ("⚪ 基线(全部bar随机买入)", "baseline", "f48h", "f24h", "peak48h")]:
        s = summary[key]
        if s.get("count", 0) == 0:
            print(f"{name}: 0 次触发")
            continue
        print(f"\n{name}: {s['count']} 次触发")
        print(f"  +{k24[1:]}  均值 {s.get('f24h_mean')}% / 中位 {s.get('f24h_median')}%")
        print(f"  +{k48[1:]} 均值 {s.get('f48h_mean')}% / 中位 {s.get('f48h_median')}%  | 命中率({k48} > 0) {s.get('hit_rate_48h')}%")
        print(f"  {kpeak}均值 {s.get('peak48h_mean')}% | 最佳 {s.get('best_48h')}% | 最差 {s.get('max_loss_48h')}%")

    print("\n\n== 逐币明细 ==")
    for r in results:
        print("=" * 76)
        print(f"{r['symbol']}  ({r.get('note','')})  窗口 {r.get('range',{}).get('start','-')} ~ {r.get('range',{}).get('end','-')}")
        if "error" in r:
            print("  ERROR:", r["error"])
            continue
        s2 = _summarize(r.get("signal2", []))
        a3 = _summarize(r.get("aux3", []))
        s1 = _summarize(r.get("signal1_5m", []), key_48="f24h", key_24="f6h", key_peak="peak24h")
        print(f"  主信号②: {s2['count']} 次 | 48h均值 {s2.get('f48h_mean')}% 命中率 {s2.get('hit_rate_48h')}%")
        print(f"  辅助线③: {a3['count']} 次 | 48h均值 {a3.get('f48h_mean')}% 命中率 {a3.get('hit_rate_48h')}%")
        print(f"  通知线①: {s1['count']} 次 | 24h均值 {s1.get('f48h_mean')}% 命中率 {s1.get('hit_rate_48h')}%")
        b = r.get("baseline", {})
        print(f"  基线对照: {b.get('count', 0)} bar | 48h均值 {b.get('f48h_mean')}% 命中率 {b.get('hit_rate_48h')}%")
        if r.get("signal2"):
            print("  主信号②触发点:")
            for d in r["signal2"][:6]:
                fw = d.get("fwd", {})
                print(f"    {d['ts']}  4h涨幅{d['chg_4h']:+.1f}% 回撤{d['drawdown']:+.1f}% "
                      f"量{d['vol_x']}x 费率{d.get('funding_pct')}% | 后48h {fw.get('f48h')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
