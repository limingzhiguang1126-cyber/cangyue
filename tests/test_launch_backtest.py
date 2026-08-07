# -*- coding: utf-8 -*-
"""launch_backtest 回测器单元测试。"""

from src.screener.launch_backtest import (
    HOT_COINS,
    MOM_MIN,
    MOM_HOT,
    VOL_MIN,
    OI_MIN,
    _mean,
)


def test_hot_coins_have_launch_ts():
    # 所有内置妖币都标定了主升浪起点
    for sym, (lt, price) in HOT_COINS.items():
        assert lt, f"{sym} 缺主升浪起点"
        assert price > 0, f"{sym} 缺启动前价格"


def test_hot_coins_are_futures_usdt():
    # 妖币全部带 USDT 后缀（合约口径）
    for sym in HOT_COINS:
        assert sym.endswith("USDT")


def test_thresholds_sane():
    # 阈值自洽：起步线 < 过热线，量/OI 门槛 > 1
    assert 0 < MOM_MIN < MOM_HOT
    assert VOL_MIN > 1.0
    assert OI_MIN > 1.0


def test_mean():
    assert _mean([1, 2, 3]) == 2.0
    assert _mean([]) == 0.0
