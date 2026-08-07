# -*- coding: utf-8 -*-
"""accumulation_screener（线 2 新版：大涨后底部横盘 / 无量上涨）单元测试。

用 mock 日线数据验证 classify_accumulation 的判定逻辑（2026-08-07 用户新口径）：
- base_accumulated：历史一波大涨 ≥ 200%（几倍）+ 距高点回撤 ≥ 70%
  + 近一个月振幅 ≤ 30%，**不再考虑量能/缩量**
- low_volume_pump：近 3 天涨幅 ≥ 10% 且日均合约成交额 < $5M（无量上涨）
- other：不满足任一条件
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.screener import accumulation_screener as acc


def _mk_day_klines(price_seq, vol_seq, start_ts=1782000000000):
    """构造日 K 线：price_seq 收盘价、vol_seq 成交额（USD），等长。"""
    out = []
    prev = price_seq[0]
    for i, (c, v) in enumerate(zip(price_seq, vol_seq)):
        o = prev
        out.append({
            "ts": start_ts + i * 86_400_000,
            "open": o,
            "high": max(o, c) * 1.003,
            "low": min(o, c) * 0.997,
            "close": c,
            "vol_usd": v,
        })
        prev = c
    return out


def _mk_oi(values_usd):
    return [{"time": 1000 + i, "oi_value": v} for i, v in enumerate(values_usd)]


def _mk_taker(ratios):
    return [{"time": 1000 + i, "buy_sell": r} for i, r in enumerate(ratios)]


def _mk_funding(rates):
    return [{"time": 1000 + i, "rate": r} for i, r in enumerate(rates)]


# ----------------------------------------------------------------------
# 大涨后底部横盘（新口径：几倍大涨 + 回撤≥70% + 30d振幅≤30%，不看量能）
# ----------------------------------------------------------------------
def test_base_accumulated():
    """历史一波从 1.0 拉到 4.0（+300%，几倍），随后回撤到 1.1（-72.5%），
    近 30 天振幅 ≤ 30%，即使不放量也判底部横盘。"""
    prices = ([1.0] * 10) + ([4.0] * 5) + ([1.1] * 165)
    vols = ([1000.0] * 15) + ([1500.0] * 165)  # 近期放量也不影响判定
    kl = _mk_day_klines(prices, vols)
    assert len(kl) >= 180
    oi = _mk_oi([100.0] * 20 + [110.0] * 20)
    taker = _mk_taker([0.95] * 10)
    funding = _mk_funding([0.00005] * 10)

    res = acc.classify_accumulation(kl, oi, taker, funding)
    assert res["base_accumulated"] is True, res
    assert res["status"] == "base_accumulated", res
    m = res["metrics"]
    assert m["wave_ret_90d_pct"] >= 200.0
    assert -m["dd_from_peak_pct"] >= 70.0
    assert m["range_30d_pct"] <= 30.0


def test_base_accumulated_vol_ignored():
    """关键：新口径**不考虑量能**——近期放量 3x 也不影响底部横盘判定。"""
    prices = ([1.0] * 10) + ([5.0] * 5) + ([1.2] * 165)
    vols = ([100.0] * 15) + ([10000.0] * 165)  # 近期 100 倍放量
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, _mk_oi([100.0] * 30), [], [])
    assert res["base_accumulated"] is True, res


def test_base_accumulated_dd_not_enough():
    """距高点回撤不足 70%（如 -50%），不算底部横盘。"""
    prices = ([1.0] * 10) + ([4.0] * 5) + ([2.0] * 165)  # 回撤 -50%
    vols = ([1000.0] * 15) + ([300.0] * 165)
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, _mk_oi([100.0] * 30), [], [])
    assert res["base_accumulated"] is False, res


def test_base_accumulated_not_pumped_enough():
    """历史只涨 +50%（不足几倍），不算\"大涨过\"。"""
    prices = ([1.0] * 10) + ([1.5] * 5) + ([0.4] * 165)  # 低->高仅 +50%
    vols = ([1000.0] * 15) + ([300.0] * 165)
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, _mk_oi([100.0] * 30), [], [])
    assert res["base_accumulated"] is False, res


def test_base_accumulated_range_too_wide():
    """近 30 天振幅 > 30%，不算底部横盘。"""
    # 1.0 -> 4.0 -> 回撤到 1.2（-70%），但近 30 天在 1.0~1.6 之间大幅震荡（振幅 60%）
    prices = [1.0] * 10 + [4.0] * 5
    tail = []
    for i in range(165):
        tail.append(1.2 if i % 2 == 0 else 1.6)
    prices = prices + tail
    vols = [1000.0] * 180
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, _mk_oi([100.0] * 30), [], [])
    assert res["base_accumulated"] is False, res


# ----------------------------------------------------------------------
# 无量上涨（新口径：只看合约日均交易量 < $5M，涨幅>0 佐证）
# ----------------------------------------------------------------------
def test_low_volume_pump():
    """近 3 天涨 12%，日均合约成交额 $1M（< $5M），判无量上涨。"""
    prices = [1.0] * 177 + [1.04, 1.08, 1.12]
    vols = [10_000_000.0] * 177 + [1_000_000.0] * 3
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, _mk_oi([100.0] * 30), _mk_taker([1.05] * 5), _mk_funding([0.00005] * 5))
    assert res["low_volume_pump"] is True, res
    assert res["status"] == "low_volume_pump", res
    assert res["metrics"]["avg_daily_vol_usd_3d"] < 5_000_000.0


def test_low_volume_pump_under_5m():
    """新口径放宽到 $5M：日均 $4.5M（< $5M）仍判无量上涨，且不再要求涨幅≥10%。"""
    prices = [1.0] * 177 + [1.02, 1.03, 1.04]  # 3 天仅 +4%，仍算无量上涨
    vols = [10_000_000.0] * 177 + [4_500_000.0] * 3
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, [], [], [])
    assert res["low_volume_pump"] is True, res


def test_low_volume_pump_declining():
    """日均量 < $5M 但近 3 天下跌（涨幅≤0），不算无量上涨（无"涨"字面）。"""
    prices = [1.0] * 177 + [0.97, 0.95, 0.93]  # 3 天下跌 -7%
    vols = [10_000_000.0] * 177 + [1_000_000.0] * 3
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, [], [], [])
    assert res["low_volume_pump"] is False, res


def test_high_volume_pump_not_lowvol():
    """日均成交额 $8M（≥ $5M），即使上涨也不算无量上涨。"""
    prices = [1.0] * 177 + [1.05, 1.10, 1.15]
    vols = [10_000_000.0] * 177 + [8_000_000.0] * 3
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, [], [], [])
    assert res["low_volume_pump"] is False, res


def test_quiet_other():
    """横盘无大涨、无上涨：other。"""
    prices = [1.0] * 180
    vols = [1000.0] * 180
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, [], [], [])
    assert res["base_accumulated"] is False
    assert res["low_volume_pump"] is False
    assert res["status"] == "other"


def test_insufficient_history():
    """历史不足 90 根：other + 数据不足。"""
    prices = [1.0] * 120
    vols = [1000.0] * 120
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, [], [], [])
    assert res["status"] == "other"
    assert "不足" in res["reason"]


def test_combined_base_and_lowvol():
    """底部横盘 + 无量上涨可同时命中（status 保留 base_accumulated）。"""
    # 底部横盘：前期 1.0 拉到 4.0 后回撤到 1.0（-75%）；最后 3 天从 1.0 涨到 1.15（+15%）
    # 量能：近 3 天日均 $400K（< $5M）→ 同时命中无量上涨
    prices = ([1.0] * 10) + ([4.0] * 5) + ([1.0] * 162) + [1.0, 1.08, 1.15]
    vols = ([10_000_000.0] * 15) + ([400_000.0] * 165)
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, _mk_oi([100.0] * 30), [], [])
    assert res["base_accumulated"] is True, res
    assert res["low_volume_pump"] is True, res


# ----------------------------------------------------------------------
# 合约 symbol 解析
# ----------------------------------------------------------------------
def test_resolve_futures_symbol():
    fmap = {"AAA": "AAAUSDT", "PEPE": "1000PEPEUSDT", "BOB": "BOBUSDT"}
    assert acc.resolve_futures_symbol("AAA", fmap) == "AAAUSDT"
    assert acc.resolve_futures_symbol("PEPE", fmap) == "1000PEPEUSDT"
    assert acc.resolve_futures_symbol("NOPE", fmap) is None


def test_load_smallcap_list(tmp_path):
    p = tmp_path / "list.json"
    p.write_text(json.dumps([{"symbol": "AAA"}, {"symbol": "BBB"}]), encoding="utf-8")
    lst = acc.load_smallcap_list(str(p))
    assert [r["symbol"] for r in lst] == ["AAA", "BBB"]
