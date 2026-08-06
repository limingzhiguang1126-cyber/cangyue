# -*- coding: utf-8 -*-
"""momo_screener 单元测试（mock 网络函数，验证评分与状态判定逻辑）。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.screener import momo_screener as ms


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _mk_klines(closes, vols_usd, interval_min=5, start_ts=1785420000000):
    """构造假 K 线列表：closes / vols_usd 等长。"""
    out = []
    for i, (c, v) in enumerate(zip(closes, vols_usd)):
        o = closes[i - 1] if i else c
        out.append({
            "ts": start_ts + i * interval_min * 60_000,
            "open": o,
            "high": max(o, c) * 1.002,
            "low": min(o, c) * 0.998,
            "close": c,
            "vol_usd": v,
        })
    return out


def _mk_oi(values_usd):
    return [{"time": 1000 + i, "oi": v / 10.0, "oi_value": v} for i, v in enumerate(values_usd)]


def _mk_taker(ratios):
    return [{"time": 1000 + i, "buy_sell": r} for i, r in enumerate(ratios)]


def _mk_ls(ratios):
    return [{"time": 1000 + i, "ratio": r} for i, r in enumerate(ratios)]


def _mk_funding(rates):
    return [{"time": 1000 + i, "rate": r} for i, r in enumerate(rates)]


class _Monkey:
    """手动 monkeypatch：设置模块级函数/属性并支持恢复。"""

    def __init__(self, module, **patches):
        self.module = module
        self.orig = {}
        for name, val in patches.items():
            self.orig[name] = getattr(module, name)
            setattr(module, name, val)

    def restore(self):
        for name, val in self.orig.items():
            setattr(self.module, name, val)


def _patch_basic(monkey, closes, vols, oi_vals, taker_ratios, ls_ratios, funding_rates):
    """替换 analyze_symbol 依赖的网络函数，返回恢复句柄。"""
    k5 = _mk_klines(closes, vols, interval_min=5)
    k15 = _mk_klines(closes[::3], vols[::3], interval_min=15)

    def fake_spot_klines(symbol, interval, limit):
        if interval == "5m":
            return k5
        return k15

    def fake_oi(symbol, period="5m", limit=200):
        return _mk_oi(oi_vals)

    def fake_taker(symbol, period="5m", limit=60):
        return _mk_taker(taker_ratios)

    def fake_ls(symbol, period="5m", limit=60):
        return _mk_ls(ls_ratios)

    def fake_funding(symbol, limit=120):
        return _mk_funding(funding_rates)

    def fake_fapi(path, timeout=50):
        # 不命中任何 fapi 调用（K 线走现货）
        return None

    return _Monkey(
        ms,
        fetch_spot_klines=fake_spot_klines,
        fetch_oi_hist=fake_oi,
        fetch_taker=fake_taker,
        fetch_ls_ratio=fake_ls,
        fetch_funding=fake_funding,
        _fapi=fake_fapi,
    )


# ----------------------------------------------------------------------
# 测试：合约 symbol 解析
# ----------------------------------------------------------------------
def test_resolve_futures_symbol():
    fmap = {
        "PEPE": "1000PEPEUSDT",
        "BOB": "BOBUSDT",
        "AAA": "AAAUSDT",
    }
    assert ms.resolve_futures_symbol("AAA", fmap) == "AAAUSDT"
    # 缩放合约反向解析：列表里是 PEPE，合约是 1000PEPE
    assert ms.resolve_futures_symbol("PEPE", fmap) == "1000PEPEUSDT"
    assert ms.resolve_futures_symbol("NOTEXIST", fmap) is None


# ----------------------------------------------------------------------
# 测试：launching 状态（量价齐升 + OI 放大 + 主动买占优）
# ----------------------------------------------------------------------
def test_launching_status():
    # 前 288 根低量横盘（1.0），后 3 根 5m 放量上涨至 1.06（+6% > 2.5%）
    closes = [1.0] * 290 + [1.02, 1.04, 1.06]
    vols = [100.0] * 290 + [500.0, 800.0, 1000.0]
    # OI：早期低，近期放大 2x+
    oi_vals = [100_000.0] * 100 + [200_000.0] * 100
    taker = [1.15] * 30
    ls = [1.3] * 30
    funding = [0.00005] * 20  # 0.5bp

    mon = _patch_basic(None, closes, vols, oi_vals, taker, ls, funding)
    fmap = {"TESTX": "TESTXUSDT"}
    try:
        res = ms.analyze_symbol("TESTX", fmap, {})
        assert res["status"] == "launching", res
        assert res["score"] >= 6.0
        assert res["futures"]["oi_x_early_to_now"] >= 1.5
        assert res["volume"]["vol_ratio_5m"] >= 1.8
    finally:
        mon.restore()


def test_hot_status():
    # 5m 单根爆拉 15%（超过 12% 过热，不追）
    closes = [1.0] * 290 + [1.15]
    vols = [100.0] * 290 + [5000.0]
    oi_vals = [100_000.0] * 200
    mon = _patch_basic(None, closes, vols, oi_vals, [1.2] * 30, [1.2] * 30, [0.0005] * 20)
    try:
        res = ms.analyze_symbol("TESTX", {"TESTX": "TESTXUSDT"}, {})
        assert res["status"] == "hot", res
    finally:
        mon.restore()


def test_15m_20pct_not_hot():
    """用户反馈：15 分钟涨幅 20% 左右仍应可关注，不视为过热。"""
    # k15 = closes[::3]，所以最后两根 15m 收盘 = closes[285] 与 closes[288]
    # 令 closes[285]=1.0、closes[288]=1.20 → 15m 涨幅 +20%；单根 5m 收盘平盘不触发 5m 过热
    closes = [1.0] * 285 + [1.0, 1.0, 1.0, 1.20, 1.20, 1.20]
    vols = [100.0] * 288 + [800.0, 1000.0, 1200.0]
    oi_vals = [100_000.0] * 100 + [300_000.0] * 100  # OI 放大 3x
    taker = [1.2] * 30
    ls = [1.3] * 30
    funding = [0.00005] * 20
    mon = _patch_basic(None, closes, vols, oi_vals, taker, ls, funding)
    fmap = {"TESTX": "TESTXUSDT"}
    try:
        res = ms.analyze_symbol("TESTX", fmap, {})
        # 15m 动量 +20% 属于可关注区间（< 25%），不应判为 hot
        assert res["momentum"]["m15_1"] >= 20.0, res
        assert res["status"] != "hot", res
        assert res["status"] == "launching", res
    finally:
        mon.restore()


def test_15m_over_25pct_hot():
    """15 分钟涨幅超过 25% 才视为过热。"""
    # closes[285]=1.0、closes[288]=1.30 → 15m 涨幅 +30% > 25% 过热
    closes = [1.0] * 285 + [1.0, 1.0, 1.0, 1.30, 1.30, 1.30]
    vols = [100.0] * 288 + [5000.0, 6000.0, 7000.0]
    oi_vals = [100_000.0] * 200
    mon = _patch_basic(None, closes, vols, oi_vals, [1.2] * 30, [1.2] * 30, [0.0005] * 20)
    try:
        res = ms.analyze_symbol("TESTX", {"TESTX": "TESTXUSDT"}, {})
        assert res["status"] == "hot", res
    finally:
        mon.restore()


def test_topped_status():
    # 从高点回撤超过 15%
    closes = [1.0] * 200 + [1.5] * 5 + [1.2] * 80
    vols = [100.0] * 285
    oi_vals = [100_000.0] * 200
    mon = _patch_basic(None, closes, vols, oi_vals, [1.0] * 30, [1.0] * 30, [0.0001] * 20)
    try:
        res = ms.analyze_symbol("TESTX", {"TESTX": "TESTXUSDT"}, {})
        assert res["status"] == "topped", res
    finally:
        mon.restore()


def test_quiet_status():
    # 全程低量横盘
    closes = [1.0] * 300
    vols = [100.0] * 300
    oi_vals = [100_000.0] * 200
    mon = _patch_basic(None, closes, vols, oi_vals, [1.0] * 30, [1.0] * 30, [0.0001] * 20)
    try:
        res = ms.analyze_symbol("TESTX", {"TESTX": "TESTXUSDT"}, {})
        assert res["status"] == "quiet", res
        assert res["score"] < 4.0
    finally:
        mon.restore()


def test_no_futures():
    res = ms.analyze_symbol("NOFUT", {}, {})
    assert res["status"] == "no_futures"


def test_load_smallcap_list(tmp_path):
    p = tmp_path / "list.json"
    p.write_text(json.dumps([{"symbol": "AAA"}, {"symbol": "BBB"}]), encoding="utf-8")
    lst = ms.load_smallcap_list(str(p))
    assert [r["symbol"] for r in lst] == ["AAA", "BBB"]
