"""launch_pattern 启动特征分析器测试（mock 数据，不访问真实网络）。"""

from unittest.mock import patch

from src.screener.launch_pattern import analyze_launch, _mean, _min, _max


def _mock_klines_1d():
    """模拟 30 根 1d K 线：前 27 根横盘，后 3 根放量上涨（已解析 dict）。"""
    rows = []
    base = 0.10
    for i in range(30):
        o = base * (1 + i * 0.001)
        c = base * (1 + i * 0.001)
        h = c * 1.02
        l = c * 0.98
        v = 5_000_000 if i < 27 else 80_000_000  # 后 3 根放量
        rows.append({
            "open_time": 1786000000000 + i * 86400000,
            "open": o, "high": h, "low": l, "close": c,
            "volume": v, "quote_volume": v * 0.10,
        })
    return rows


def _mock_oi_hist():
    return [
        {"time": 1786000000000 + i * 14400000, "oi": 100_000_000 + i * 2_000_000,
         "oi_value": 10_000_000.0}
        for i in range(40)
    ]


def _mock_funding():
    return [
        {"time": 1786000000000 + i * 28800000, "rate": 0.00001}
        for i in range(40)
    ]


def _mock_ls():
    return [
        {"time": 1786000000000 + i * 14400000, "long": 0.5,
         "short": 0.5, "ratio": 1.0}
        for i in range(20)
    ]


def _mock_taker():
    return [
        {"time": 1786000000000 + i * 14400000, "buy_sell": 1.05}
        for i in range(20)
    ]


@patch("src.screener.launch_pattern.fetch_taker_ratio", return_value=_mock_taker())
@patch("src.screener.launch_pattern.fetch_ls_ratio", return_value=_mock_ls())
@patch("src.screener.launch_pattern.fetch_funding", return_value=_mock_funding())
@patch("src.screener.launch_pattern.fetch_oi_hist", return_value=_mock_oi_hist())
@patch("src.screener.launch_pattern.fetch_klines", return_value=_mock_klines_1d())
def test_analyze_launch_structure(mock_kl, mock_oi, mock_fr, mock_ls, mock_tk):
    r = analyze_launch("AAAUSDT")
    assert r["symbol"] == "AAAUSDT"
    assert "daily" in r and "oi" in r and "funding" in r
    assert "long_short" in r and "taker" in r
    # 后 3 根放量上涨
    assert r["daily"]["recent3d"][-1]["chg_pct"] >= 0
    # OI 峰值 ≥ 早期均值
    assert r["oi"]["peak"] >= r["oi"]["early_avg"]


@patch("src.screener.launch_pattern.fetch_taker_ratio", return_value=None)
@patch("src.screener.launch_pattern.fetch_ls_ratio", return_value=None)
@patch("src.screener.launch_pattern.fetch_funding", return_value=None)
@patch("src.screener.launch_pattern.fetch_oi_hist", return_value=None)
@patch("src.screener.launch_pattern.fetch_klines", return_value=None)
def test_analyze_launch_no_data(mock_kl, mock_oi, mock_fr, mock_ls, mock_tk):
    r = analyze_launch("AAAUSDT")
    # 无数据时不应抛异常，各小节缺失
    assert r["symbol"] == "AAAUSDT"
    assert "daily" not in r or r["daily"].get("range_low") is not None or "daily" not in r


def test_mean_min_max():
    assert _mean([1, 2, 3, 4]) == 2.5
    assert _mean([]) == 0.0
    assert _mean((x for x in [1, 2])) == 1.5  # generator 输入
    assert _min([3, 1, 2]) == 1
    assert _max([3, 1, 2]) == 3
    assert _min([]) == 0.0 and _max([]) == 0.0
