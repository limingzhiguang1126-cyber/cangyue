# -*- coding: utf-8 -*-
"""accumulation_screener（线 2 新版：大涨后底部横盘 / 无量上涨）单元测试。

用 mock 日线数据验证 classify_accumulation 的判定逻辑：
- base_accumulated：历史大涨（>=40%）+ 距高点回撤 20~60% + 近 5 天低振幅 + 缩量
- low_volume_pump：近 3 天涨幅 >=10% 但日均合约成交额很小（无量上涨）
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
# 大涨后底部横盘
# ----------------------------------------------------------------------
def test_base_accumulated():
    """大涨后底部横盘：前期 1.0 拉到 2.0（+100%），随后回撤到 1.4（-30%），
    近 5 天低振幅、量能缩至前期的 0.3x。"""
    prices = ([1.0] * 10) + ([2.0] * 5) + ([1.4] * 45)
    # 前 15 天均量 1000，后 45 天均量 300 → 缩量 0.3x
    vols = ([1000.0] * 15) + ([300.0] * 45)
    kl = _mk_day_klines(prices, vols)
    assert len(kl) >= 60
    oi = _mk_oi([100.0] * 20 + [110.0] * 20)
    taker = _mk_taker([0.95] * 10)
    funding = _mk_funding([0.00005] * 10)

    res = acc.classify_accumulation(kl, oi, taker, funding)
    assert res["base_accumulated"] is True, res
    assert res["status"] == "base_accumulated", res
    m = res["metrics"]
    assert m["peak_ret_60d_pct"] >= 40.0
    assert 20.0 <= -m["dd_from_peak_pct"] <= 60.0
    assert m["vol_shrink_ratio"] <= 0.8


def test_base_accumulated_dd_too_shallow():
    """距高点回撤不足 20%（还在高位），不算底部横盘。"""
    # 1.0 -> 2.0，当前 1.8（回撤仅 -10%）
    prices = ([1.0] * 10) + ([2.0] * 5) + ([1.8] * 45)
    vols = ([1000.0] * 15) + ([300.0] * 45)
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, _mk_oi([100.0] * 30), [], [])
    assert res["base_accumulated"] is False, res


def test_base_accumulated_no_shrink():
    """回撤符合但没缩量（量能 1.5x），不算底部横盘。"""
    prices = ([1.0] * 10) + ([2.0] * 5) + ([1.4] * 45)
    vols = ([300.0] * 15) + ([1500.0] * 45)  # 近期放量而非缩量
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, _mk_oi([100.0] * 30), [], [])
    assert res["base_accumulated"] is False, res


# ----------------------------------------------------------------------
# 无量上涨
# ----------------------------------------------------------------------
def test_low_volume_pump():
    """近 3 天涨 12%，但日均合约成交额仅 $1M（无量上涨）。"""
    prices = [1.0] * 57 + [1.04, 1.08, 1.12]
    vols = [10_000_000.0] * 57 + [1_000_000.0] * 3  # 单日 $1M
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, _mk_oi([100.0] * 30), _mk_taker([1.05] * 5), _mk_funding([0.00005] * 5))
    assert res["low_volume_pump"] is True, res
    assert res["status"] == "low_volume_pump", res
    assert res["metrics"]["gain_3d_pct"] >= 10.0
    assert res["metrics"]["avg_daily_vol_usd_3d"] <= 2_000_000.0


def test_high_volume_pump_not_lowvol():
    """近 3 天虽涨但成交额大（>$2M），不算无量上涨。"""
    prices = [1.0] * 55 + [1.05, 1.10, 1.15]
    vols = [10_000_000.0] * 55 + [8_000_000.0] * 3
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, [], [], [])
    assert res["low_volume_pump"] is False, res


def test_quiet_other():
    """横盘无大涨、无上涨：other。"""
    prices = [1.0] * 60
    vols = [1000.0] * 60
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, [], [], [])
    assert res["base_accumulated"] is False
    assert res["low_volume_pump"] is False
    assert res["status"] == "other"


def test_insufficient_history():
    """历史不足 60 根：other + 数据不足。"""
    prices = [1.0] * 30
    vols = [1000.0] * 30
    kl = _mk_day_klines(prices, vols)
    res = acc.classify_accumulation(kl, [], [], [])
    assert res["status"] == "other"
    assert "不足" in res["reason"]


def test_combined_base_and_lowvol():
    """底部横盘 + 无量上涨可同时命中（status 保留 base_accumulated）。"""
    # 底部横盘：前期 1.0 拉到 2.0 后回撤到 1.4 缩量整理；最后 3 天从 1.4 涨到 1.6（+14%）
    # 量能：前期均量 $1M，近 3 天 $300K → 既缩量又无量上涨
    prices = ([1.0] * 10) + ([2.0] * 5) + ([1.4] * 42) + [1.4, 1.5, 1.6]
    vols = ([1_000_000.0] * 15) + ([300_000.0] * 42) + [300_000.0] * 3
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
