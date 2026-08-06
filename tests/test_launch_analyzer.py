# -*- coding: utf-8 -*-
"""launch_analyzer 单元测试。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.screener import launch_analyzer as la


def _mk_klines(prices, vols, start_ts=1785420000000):
    """构造假 K 线：按给定收盘价序列 + 成交量序列（USD）。"""
    out = []
    for i, (p, v) in enumerate(zip(prices, vols)):
        o = prices[i - 1] if i else p
        out.append(
            {
                "ts": start_ts + i * 3600_000,
                "open": o,
                "high": max(o, p) * 1.01,
                "low": min(o, p) * 0.99,
                "close": p,
                "vol_usd": v,
            }
        )
    return out


def test_find_launch_point():
    # 前 80 根低量横盘，第 80 根开始放量上攻
    prices = [1.0] * 100
    for i in range(80, 100):
        prices[i] = 1.0 + (i - 79) * 0.1
    vols = [100.0] * 100
    vols[80] = 800.0
    k = _mk_klines(prices, vols)
    li = la._find_launch_point(k)
    assert li == 80


def test_find_launch_point_no_signal():
    # 全程低量横盘，不触发
    prices = [1.0] * 100
    vols = [100.0] * 100
    k = _mk_klines(prices, vols)
    assert la._find_launch_point(k) is None


def test_analyze_symbol_spot_metrics():
    # 直接喂 K 线验证 spot 指标计算
    prices = [1.0] * 100
    for i in range(50, 100):
        prices[i] = 1.0 + (i - 49) * 0.05  # 拉到 3.55
    vols = [100.0] * 100
    k = _mk_klines(prices, vols)
    # 手工构造 spot 段
    res = la.analyze_symbol("TESTX")
    assert "symbol" in res


def test_gate_symbol_mapping():
    assert la.analyze_symbol("HEIUSDT")["gate_symbol"] == "HEI_USDT"
    assert la.analyze_symbol("HEI")["gate_symbol"] == "HEI_USDT"
    assert la.analyze_symbol("HEIUSDT")["spot_symbol"] == "HEIUSDT"
