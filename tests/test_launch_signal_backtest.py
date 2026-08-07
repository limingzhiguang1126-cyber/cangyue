# -*- coding: utf-8 -*-
"""launch_signal_backtest v1 选币标准回测器单元测试。"""

from src.screener.launch_signal_backtest import (
    POSITIVE_COINS,
    NEGATIVE_COINS,
    SIGNAL_5M_MIN,
    SIGNAL_4H_MIN,
    DRAWDOWN_MAX,
    AUX_4H_MIN,
    AUX_4H_MAX,
    AUX_VOL_X,
    VETO_FUNDING_HIGH,
    VETO_FUNDING_LOW,
    VETO_OI_DRAWDOWN,
    _classify_bar,
    _classify_5m,
    _summarize,
    _mean,
)


def test_sample_coins_futures_usdt():
    # 样本全部带 USDT 后缀（合约口径）
    for sym in list(POSITIVE_COINS) + list(NEGATIVE_COINS):
        assert sym.endswith("USDT")


def test_thresholds_sane():
    # 主信号：30% 涨幅 + 回撤 20%
    assert SIGNAL_4H_MIN > AUX_4H_MAX > AUX_4H_MIN > 0
    assert 0 < DRAWDOWN_MAX < 30
    assert SIGNAL_5M_MIN > 0
    # 辅助线：量能 5x
    assert AUX_VOL_X > 1
    # 一票否决：费率区间合理、OI 回撤为正数
    assert VETO_FUNDING_LOW < 0 < VETO_FUNDING_HIGH
    assert VETO_OI_DRAWDOWN > 0


def test_classify_bar_signal2():
    # 构造 4h 涨幅≥30% 且回撤<20% 的 bar
    base = [{"ts": i * 4 * 3600 * 1000, "open": 100.0, "high": 100.0,
             "low": 99.0, "close": 99.0, "vol_usd": 1000.0} for i in range(25)]
    # 前 24 根高点 100，最新 bar 从 99 涨到 130（+31%），量能放大
    kl = list(base)
    kl.append({"ts": 25 * 4 * 3600 * 1000, "open": 99.0, "high": 135.0,
               "low": 98.0, "close": 130.0, "vol_usd": 20000.0})
    oi = [{"time": k["ts"], "oi_value": 1000.0} for k in kl]
    funding = [{"time": kl[0]["ts"], "rate": 0.0001}]
    key, detail = _classify_bar(kl, oi, funding, len(kl) - 1)
    assert key == "signal2", f"got {key}"
    assert detail["chg_4h"] >= SIGNAL_4H_MIN


def test_classify_bar_aux3():
    # 构造 4h 涨幅 5% 且量能 6x 的 bar
    base = [{"ts": i * 4 * 3600 * 1000, "open": 100.0, "high": 100.0,
             "low": 99.0, "close": 99.0, "vol_usd": 1000.0} for i in range(25)]
    kl = list(base)
    kl.append({"ts": 25 * 4 * 3600 * 1000, "open": 99.0, "high": 106.0,
               "low": 98.0, "close": 104.0, "vol_usd": 20000.0})  # +5%, 20x
    oi = [{"time": k["ts"], "oi_value": 1000.0} for k in kl]
    funding = [{"time": kl[0]["ts"], "rate": 0.0001}]
    key, detail = _classify_bar(kl, oi, funding, len(kl) - 1)
    assert key == "aux3", f"got {key}"


def test_classify_bar_veto_funding():
    # 费率过热 → 一票否决
    base = [{"ts": i * 4 * 3600 * 1000, "open": 100.0, "high": 100.0,
             "low": 99.0, "close": 99.0, "vol_usd": 1000.0} for i in range(25)]
    kl = list(base)
    kl.append({"ts": 25 * 4 * 3600 * 1000, "open": 99.0, "high": 135.0,
               "low": 98.0, "close": 130.0, "vol_usd": 20000.0})
    oi = [{"time": k["ts"], "oi_value": 1000.0} for k in kl]
    funding = [{"time": kl[0]["ts"], "rate": 0.0020}]  # +0.2% 过热
    key, detail = _classify_bar(kl, oi, funding, len(kl) - 1)
    assert key == "veto", f"got {key}"
    assert detail["veto"] == "funding"


def test_classify_bar_veto_oi():
    # OI 较峰值回撤 >30% → 一票否决
    base = [{"ts": i * 4 * 3600 * 1000, "open": 100.0, "high": 100.0,
             "low": 99.0, "close": 99.0, "vol_usd": 1000.0} for i in range(25)]
    kl = list(base)
    kl.append({"ts": 25 * 4 * 3600 * 1000, "open": 99.0, "high": 135.0,
               "low": 98.0, "close": 130.0, "vol_usd": 20000.0})
    oi = [{"time": k["ts"], "oi_value": 1000.0} for k in kl]
    # 把最近 OI 压到 500（-50%）
    oi[-1] = {"time": kl[-1]["ts"], "oi_value": 500.0}
    funding = [{"time": kl[0]["ts"], "rate": 0.0001}]
    key, detail = _classify_bar(kl, oi, funding, len(kl) - 1)
    assert key == "veto", f"got {key}"
    assert detail["veto"] == "oi"


def test_classify_5m():
    k5 = [
        {"ts": 0, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol_usd": 100},
        {"ts": 1, "open": 1.0, "high": 1.2, "low": 0.99, "close": 1.15, "vol_usd": 500},  # +15%
        {"ts": 2, "open": 1.15, "high": 1.16, "low": 1.13, "close": 1.14, "vol_usd": 100},  # -0.9%
    ]
    hit, det = _classify_5m(k5, 1)
    assert hit is True
    assert det["chg_5m"] == 15.0
    hit2, _ = _classify_5m(k5, 2)
    assert hit2 is False


def test_summarize():
    items = [
        {"fwd": {"f24h": 5, "f48h": 10, "peak48h": 15}},
        {"fwd": {"f24h": -3, "f48h": -6, "peak48h": 2}},
        {"fwd": {"f24h": 2, "f48h": 3, "peak48h": 8}},
    ]
    s = _summarize(items)
    assert s["count"] == 3
    assert abs(s["f48h_mean"] - (10 - 6 + 3) / 3) < 0.01
    assert s["hit_rate_48h"] == 66.7  # 2/3
    assert s["max_loss_48h"] == -6.0


def test_summarize_empty():
    s = _summarize([])
    assert s["count"] == 0


def test_mean():
    assert _mean([1, 2, 3]) == 2.0
    assert _mean([]) == 0.0


def test_summarize_5m_keys():
    # 5m 线用 f24h 为主收益、f6h 为短收益
    items = [
        {"fwd": {"f6h": 1, "f24h": 8, "peak24h": 12}},
        {"fwd": {"f6h": -2, "f24h": -5, "peak24h": 1}},
    ]
    s = _summarize(items, key_48="f24h", key_24="f6h", key_peak="peak24h")
    assert s["count"] == 2
    assert s["f48h_mean"] == 1.5  # f24h 均值
    assert s["f24h_mean"] == -0.5  # f6h 均值
    assert s["peak48h_mean"] == 6.5
    assert s["hit_rate_48h"] == 50.0  # 1/2 为正


def test_classify_bar_drawdown_uses_launch_window():
    # 「本波拉升高点」窗口（触发 bar + 前6根）内的回撤判定
    base = [{"ts": i * 4 * 3600 * 1000, "open": 100.0, "high": 100.0,
             "low": 99.0, "close": 99.0, "vol_usd": 1000.0} for i in range(25)]
    kl = list(base)
    # 触发 bar 前一根从 99 拉到 110（高点 115），触发 bar 从 110 涨到 145（+31.8%）
    kl.append({"ts": 25 * 4 * 3600 * 1000, "open": 99.0, "high": 115.0,
               "low": 98.0, "close": 110.0, "vol_usd": 5000.0})
    kl.append({"ts": 26 * 4 * 3600 * 1000, "open": 110.0, "high": 148.0,
               "low": 109.0, "close": 145.0, "vol_usd": 20000.0})
    oi = [{"time": k["ts"], "oi_value": 1000.0} for k in kl]
    funding = [{"time": kl[0]["ts"], "rate": 0.0001}]
    # 高点 148，当前 145，回撤 -2.0% → 满足 <20%
    key, detail = _classify_bar(kl, oi, funding, len(kl) - 1)
    assert key == "signal2", f"got {key}"
    assert -DRAWDOWN_MAX < detail["drawdown"] < 0


def test_classify_bar_drawdown_too_deep():
    # 回撤超过 20% → 不触发主信号②
    base = [{"ts": i * 4 * 3600 * 1000, "open": 100.0, "high": 100.0,
             "low": 99.0, "close": 99.0, "vol_usd": 1000.0} for i in range(25)]
    kl = list(base)
    # 前一根拉到 130（高点 130），当前跌到 100（-23%）后再反弹 30% 到 100*1.3
    kl.append({"ts": 25 * 4 * 3600 * 1000, "open": 99.0, "high": 130.0,
               "low": 98.0, "close": 100.0, "vol_usd": 5000.0})
    kl.append({"ts": 26 * 4 * 3600 * 1000, "open": 100.0, "high": 132.0,
               "low": 99.0, "close": 130.0, "vol_usd": 20000.0})
    oi = [{"time": k["ts"], "oi_value": 1000.0} for k in kl]
    funding = [{"time": kl[0]["ts"], "rate": 0.0001}]
    # 高点 132，当前 130，回撤 -1.5%？—— 高点窗口包含前一根的 130 和本根 132
    # 高点 = max(前一根 high=130, 本根 high=132) = 132，回撤 = -1.5% < 20%，会触发
    # 因此该用例改为验证「回撤真的超 20%」：把前一根高点设为 170
    kl[25] = {"ts": 25 * 4 * 3600 * 1000, "open": 99.0, "high": 170.0,
              "low": 98.0, "close": 100.0, "vol_usd": 5000.0}
    key, detail = _classify_bar(kl, oi, funding, len(kl) - 1)
    assert key != "signal2", f"got {key}"
    assert detail["drawdown"] <= -DRAWDOWN_MAX
