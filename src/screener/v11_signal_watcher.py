# -*- coding: utf-8 -*-
"""v1.1 选币标准 · 实时信号监视器（Launch Signal Watcher）。

在「线 1 候选池」（Top100 小市值，`data/smallcap_top100_fdv.json`）内，
按 2026-08-07 讨论定稿的 **v1.1 标准** 对每个标的做实时判定，命中即输出
结构化信号（含命中原因 + 观察/建仓建议），供守护进程推送到 Telegram。

## v1.1 标准（讨论稿 + 回测修正）

| 信号线 | 触发条件 | 动作 |
|---|---|---|
| 🔔 通知线① | 5m 涨幅 ≥ +10%（收盘口径） | 立即通知 + 进观察；叠加确认(量≥3x+OI≥1.15x+费率正常)才可小仓 |
| 🔔 主信号② | 4h 涨幅 ≥ +30% 且距本波高点回撤 < 20% | 通知 + 重点分析；OI 同步放大(≥1.15x)可建仓，否则降级观察 |
| 📡 辅助线③ | 4h 涨幅 3%~10% 且 4h 量能 ≥ 5x | 提前埋伏观察 |
| ⛔ 一票否决 | 资金费率 > +0.3% 或 < -0.1%（v1.1 已放宽上限）/ OI 较峰值回落 > 30% | 命中信号也不碰 |
| 🧪 假启动排除 | 主信号② 触发时 OI 未同步放大（< 1.15x） | 降级为观察 |

数据源（币安官方，fapi 经 CORS 代理）：
- 4h K 线:      fapi.binance.com/fapi/v1/klines?interval=4h
- 5m K 线:      fapi.binance.com/fapi/v1/klines?interval=5m
- OI 历史:      fapi.binance.com/futures/data/openInterestHist?period=4h
- 资金费率:     fapi.binance.com/fapi/v1/fundingRate

用法：
    python -m src.screener.v11_signal_watcher                                  # 表格输出
    python -m src.screener.v11_signal_watcher --json                           # JSON 输出
    python -m src.screener.v11_signal_watcher --list data/smallcap_top100_fdv.json
    python -m src.screener.v11_signal_watcher --symbols BICOUSDT,TUTUSDT       # 指定标的
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
from typing import Any, Callable, Dict, List, Optional

import requests

from ..utils.logger import setup_logging
from .launch_signal_backtest import (
    _funding_at,
    _oi_at,
    _oi_peak_before,
    _mean,
    _ts,
)
from .momo_screener import fetch_futures_symbols, resolve_futures_symbol

setup_logging()
logger = logging.getLogger("fin-alert.screener.v11")

urllib3.disable_warnings()

# ---------------------------------------------------------------------------
# 带退避重试的 fapi 访问（429 限流友好）
# ---------------------------------------------------------------------------
_FAPI_BASE = "https://fapi.binance.com"
_PROXY_CORS_SH = "https://proxy.cors.sh/"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://cnb.cool"}


def _fapi_retry(path: str, timeout: int = 50, retries: int = 4) -> Optional[Any]:
    """经 CORS 代理访问 fapi.binance.com，带 429 退避重试。"""
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
                time.sleep(2.0 * (attempt + 1))  # 限流退避
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("fapi %s err: %s", path, exc)
        time.sleep(1.0 * (attempt + 1))
    return None


def _fetch_klines(symbol: str, interval: str, limit: int) -> List[Dict[str, Any]]:
    data = _fapi_retry(f"/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if not data:
        return []
    return [
        {"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
         "low": float(k[3]), "close": float(k[4]), "vol_usd": float(k[7])}
        for k in data
    ]


def _fetch_oi_hist(symbol: str, period: str = "4h", limit: int = 40) -> List[Dict[str, Any]]:
    """OI 历史（带 429 退避）。"""
    data = _fapi_retry(
        f"/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}"
    )
    if not data:
        return []
    return [{"time": int(d["timestamp"]), "oi_value": float(d["sumOpenInterestValue"])}
            for d in data]


def _fetch_funding(symbol: str, limit: int = 30) -> List[Dict[str, Any]]:
    """历史资金费率（带 429 退避）。"""
    data = _fapi_retry(f"/fapi/v1/fundingRate?symbol={symbol}&limit={limit}")
    if not data:
        return []
    return [{"time": int(d["fundingTime"]), "rate": float(d["fundingRate"])}
            for d in data]


# ---------------------------------------------------------------------------
# v1.1 标准阈值
# ---------------------------------------------------------------------------
SIGNAL_5M_MIN = 10.0          # 通知线①：5m 收盘涨幅 ≥ +10%
SIGNAL_4H_MIN = 30.0          # 主信号②：4h 涨幅 ≥ +30%
DRAWDOWN_MAX = 20.0           # 主信号②：距本波高点回撤 < 20%
AUX_4H_MIN = 3.0              # 辅助线③：4h 涨幅 ≥ +3%
AUX_4H_MAX = 10.0             # 辅助线③：4h 涨幅 ≤ +10%
AUX_VOL_X = 5.0               # 辅助线③：4h 量能 ≥ 前24h均量 5x
VETO_FUNDING_HIGH = 0.30      # v1.1：费率过热上限放宽到 +0.3%（回测修正，避免误杀 HFT 式主升）
VETO_FUNDING_LOW = -0.10      # v1.1：费率下限 -0.1%（负费率才是出货信号）
VETO_OI_DRAWDOWN = 30.0       # 一票否决：OI 较峰值回落 > 30%
FAKE_LAUNCH_OI_MIN = 1.15     # 假启动排除：主信号② 触发时 OI 放大 < 1.15x 降级观察
CONFIRM_VOL_X = 3.0           # 通知线① 上小仓确认：5m 量能 ≥ 3x

# 判定窗口（单位：根 4h bar）
LOOKBACK_4H = 24              # 前 24 根（96h）为均量基准
LAUNCH_HIGH_WINDOW = 7        # 「本波拉升高点」窗口：触发 bar 及前 6 根（24h）

# 动作优先级
ACTION_ORDER = {"build": 0, "watch": 1, "observe": 2, "none": 3}


def evaluate_symbol(base: str, futures_map: Dict[str, str]) -> Dict[str, Any]:
    """对单个标的按 v1.1 标准做实时判定。

    Args:
        base: 候选池里的 base symbol（如 "BICO"）。
        futures_map: base -> 合约 symbol 映射（来自 fetch_futures_symbols）。

    Returns:
        信号判定结果 dict：
        - signal: "signal1" / "signal2" / "aux3" / "veto" / "none"
        - action: "build"（可建仓）/ "watch"（观察）/ "observe"（埋伏观察）/ "none"
        - reasons: 命中原因列表（可直接展示）
        - metrics: 关键指标
    """
    res: Dict[str, Any] = {
        "symbol": base,
        "futures_symbol": "",
        "signal": "none",
        "action": "none",
        "vetoed": False,
        "reasons": [],
        "metrics": {},
        "ts": _ts(int(time.time() * 1000)),
    }

    fsym = resolve_futures_symbol(base, futures_map)
    if fsym is None:
        res["reasons"].append("无活跃 TRADING USDT 永续合约")
        return res
    res["futures_symbol"] = fsym

    kl = _fetch_klines(fsym, "4h", 40)
    oi = _fetch_oi_hist(fsym, "4h", 40)
    funding = _fetch_funding(fsym, 30)
    k5: List[Dict[str, Any]] = []  # 懒加载：仅 4h 层面平静时才拉 5m 检查通知线①

    if not kl or len(kl) < LOOKBACK_4H + 1:
        res["reasons"].append("4h K 线数据不足")
        return res

    i = len(kl) - 1
    bar = kl[i]
    prev = kl[i - 1]
    base4h = kl[i - LOOKBACK_4H:i]

    chg_4h = (bar["close"] - prev["close"]) / prev["close"] * 100 if prev["close"] else 0.0
    launch_window = kl[max(0, i - LAUNCH_HIGH_WINDOW + 1):i + 1]
    peak_high = max(k["high"] for k in launch_window)
    drawdown = (bar["close"] / peak_high - 1) * 100 if peak_high else 0.0
    avg_vol = _mean(k["vol_usd"] for k in base4h)
    vol_x = bar["vol_usd"] / avg_vol if avg_vol else 0.0

    fr = _funding_at(funding, bar["ts"])
    oi_now = _oi_at(oi, bar["ts"])
    oi_peak = _oi_peak_before(oi, bar["ts"], window=LOOKBACK_4H)
    oi_dd = None
    if oi_now is not None and oi_peak and oi_peak > 0:
        oi_dd = (oi_now / oi_peak - 1) * 100
    # OI 放大倍数（持仓进场信号）：近 10 条早期均值 vs 当前
    oi_x = 0.0
    if oi and len(oi) >= 12:
        early = _mean(o["oi_value"] for o in oi[:10])
        if early > 0 and oi_now is not None:
            oi_x = oi_now / early

    # ---- 信号线判定（先 4h 层面） ----
    is_signal2 = chg_4h >= SIGNAL_4H_MIN and drawdown > -DRAWDOWN_MAX
    is_aux3 = AUX_4H_MIN <= chg_4h <= AUX_4H_MAX and vol_x >= AUX_VOL_X

    # ---- 通知线①：仅当 4h 层面无信号时才拉 5m 检查（节省 API 请求） ----
    chg_5m = 0.0
    hit5 = False
    if not (is_signal2 or is_aux3):
        k5 = _fetch_klines(fsym, "5m", 60)
        if k5 and len(k5) >= 2:
            chg_5m = (k5[-1]["close"] / k5[-2]["close"] - 1) * 100
            hit5 = chg_5m >= SIGNAL_5M_MIN
    is_signal1 = hit5

    metrics = {
        "price": round(bar["close"], 8),
        "chg_4h": round(chg_4h, 2),
        "drawdown": round(drawdown, 2),
        "vol_x": round(vol_x, 2),
        "funding_pct": round(fr, 4) if fr is not None else None,
        "oi_dd_pct": round(oi_dd, 2) if oi_dd is not None else None,
        "oi_x": round(oi_x, 2),
        "chg_5m": round(chg_5m, 2),
    }
    res["metrics"] = metrics

    # ---- 一票否决 ----
    funding_veto = False
    if fr is not None and (fr > VETO_FUNDING_HIGH or fr < VETO_FUNDING_LOW):
        funding_veto = True
    oi_veto = False
    if oi_dd is not None and oi_dd < -VETO_OI_DRAWDOWN:
        oi_veto = True
    vetoed = funding_veto or oi_veto

    # ---- 信号线判定 ----
    is_signal1 = hit5

    reasons: List[str] = []
    signal = "none"
    action = "none"

    if is_signal2 or is_aux3 or is_signal1:
        if vetoed:
            res["vetoed"] = True
            signal = "veto"
            action = "watch"
            veto_reasons = []
            if funding_veto:
                veto_reasons.append(f"资金费率 {fr:+.4f}% 超出 [-0.1%, +0.3%]")
            if oi_veto:
                veto_reasons.append(f"OI 较峰值回落 {oi_dd:.1f}% (>30%)")
            reasons.append("⛔ 一票否决：" + "；".join(veto_reasons))
            if is_signal2:
                reasons.append(f"（同时命中主信号②：4h {chg_4h:+.1f}%，回撤 {drawdown:+.1f}%）")
            elif is_aux3:
                reasons.append(f"（同时命中辅助线③：4h {chg_4h:+.1f}%，量能 {vol_x:.1f}x）")
            else:
                reasons.append(f"（同时命中通知线①：5m {chg_5m:+.1f}%）")
        elif is_signal2:
            signal = "signal2"
            reasons.append(
                f"主信号②：4h 涨幅 {chg_4h:+.1f}% (≥30%)，"
                f"距本波高点回撤 {drawdown:+.1f}% (<20%)"
            )
            if oi_x >= FAKE_LAUNCH_OI_MIN:
                action = "build"
                reasons.append(f"OI 同步放大 {oi_x:.2f}x (≥1.15x)，持仓进场，可建仓")
            else:
                action = "watch"
                reasons.append(
                    f"OI 未同步放大 ({oi_x:.2f}x < 1.15x)，疑似假启动，降级观察"
                )
        elif is_aux3:
            signal = "aux3"
            action = "observe"
            reasons.append(
                f"辅助线③：4h 涨幅 {chg_4h:+.1f}% (3~10%) "
                f"且 4h 量能 {vol_x:.1f}x (≥5x)，提前埋伏观察"
            )
        else:  # is_signal1
            signal = "signal1"
            action = "watch"
            reasons.append(f"通知线①：5m 涨幅 {chg_5m:+.1f}% (≥10%)，立即关注")
            confirms = []
            if vol_x >= CONFIRM_VOL_X:
                confirms.append(f"量能 {vol_x:.1f}x")
            if oi_x >= FAKE_LAUNCH_OI_MIN:
                confirms.append(f"OI {oi_x:.2f}x")
            if fr is not None and VETO_FUNDING_LOW < fr < VETO_FUNDING_HIGH:
                confirms.append("费率正常")
            if len(confirms) >= 2:
                action = "build"
                reasons.append(
                    "叠加确认：" + "、".join(confirms) + " → 可小仓试错(1~2%)"
                )
            else:
                reasons.append("仅观察，待量能/OI/费率确认后再上仓")

    res["signal"] = signal
    res["action"] = action
    res["reasons"] = reasons
    return res


def scan_pool(
    pool: List[Dict[str, Any]],
    futures_map: Dict[str, str],
    workers: int = 8,
) -> List[Dict[str, Any]]:
    """并发扫描候选池，返回全部信号判定结果（含未命中）。"""
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool_ex:
        fut_map = {
            pool_ex.submit(evaluate_symbol, r["symbol"], futures_map): r["symbol"]
            for r in pool
        }
        for fut in as_completed(fut_map):
            sym = fut_map[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s evaluation failed: %s", sym, exc)
                results.append(
                    {"symbol": sym, "signal": "none", "action": "none",
                     "reasons": [f"评估异常: {exc}"], "metrics": {}}
                )
            time.sleep(0.05)

    results.sort(
        key=lambda r: (ACTION_ORDER.get(r.get("action", "none"), 9),
                       -float(r.get("metrics", {}).get("chg_4h", 0) or 0))
    )
    return results


def load_pool(path: str, top: int = 100) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data[:top]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="v1.1 选币标准实时信号监视器")
    ap.add_argument("--list", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "smallcap_top100_fdv.json"),
        help="线1候选池 JSON 路径")
    ap.add_argument("--top", type=int, default=100, help="取候选池前 N 个")
    ap.add_argument("--symbols", help="指定合约 symbol 列表（逗号分隔，跳过候选池）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--output", default="", help="结果写入文件")
    ap.add_argument("--workers", type=int, default=4, help="并发数（默认 4，避免 fapi 限流）")
    args = ap.parse_args(argv)

    if args.json:
        # JSON 输出时保证 stdout 纯净：日志重定向到 stderr
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
    hits = [r for r in results if r.get("signal") not in ("none",)]

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"scan_ts": results[0].get("ts", "") if results else "",
                       "pool_size": len(pool), "hits": hits, "all": results},
                      f, ensure_ascii=False, indent=2)
        print(f"\n结果已写入: {args.output}")

    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0

    print("\n== v1.1 实时信号扫描结果 ==")
    print(f"命中 {len(hits)} 个 / 扫描 {len(results)} 个")
    for r in hits:
        m = r.get("metrics", {})
        print("-" * 70)
        print(f"[{r.get('action','?').upper():>7}] {r['symbol']} ({r.get('futures_symbol','')})  "
              f"信号={r.get('signal')}  现价={m.get('price','?')}")
        print(f"  4h:{m.get('chg_4h','?')}% 回撤:{m.get('drawdown','?')}% "
              f"量能:{m.get('vol_x','?')}x OI放大:{m.get('oi_x','?')}x "
              f"费率:{m.get('funding_pct','?')}% OI回撤:{m.get('oi_dd_pct','?')}% 5m:{m.get('chg_5m','?')}%")
        for reason in r.get("reasons", []):
            print(f"  · {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
