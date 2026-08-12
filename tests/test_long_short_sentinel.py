# -*- coding: utf-8 -*-
"""long_short_sentinel + ls_cloud_run 单元测试。

无需网络，用假数据验证：
- 「空翻多」/「暴跌风险」/「无信号」三种判定逻辑
- 消息格式化器输出
- 云端执行器的推送/去重流程
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from unittest import mock

from src.screener import long_short_sentinel as ls  # noqa: E402
import ls_cloud_run as cloud  # noqa: E402
from src.notifier.long_short_formatter import format_ls_message  # noqa: E402


# ---------------- 信号判定逻辑 ----------------

def _ratio(seq):
    """把多空比值序列构造成 taker 数据。"""
    out = []
    for i, r in enumerate(seq):
        out.append({"time": 1000 + i * 900000, "ratio": r})
    return out


def _kl(chg_pct):
    """构造 15m K 线（两根，收盘涨幅 chg_pct%）。"""
    prev = 100.0
    cur = prev * (1 + chg_pct / 100.0)
    return [
        {"ts": 1, "open": prev, "high": prev, "low": prev, "close": prev, "vol_usd": 1},
        {"ts": 2, "open": prev, "high": cur, "low": cur, "close": cur, "vol_usd": 1},
    ]


def _futures_map(sym):
    return {sym: sym + "USDT"}


def _run_eval(base, ratios, kl, futures_map):
    with mock.patch.object(ls, "fetch_taker_ratio", return_value=_ratio(ratios)), \
         mock.patch.object(ls, "fetch_klines", return_value=_kl(kl)):
        return ls.evaluate_ls(base, futures_map)


def test_cross_up():
    """空翻多：历史空头主导 → 最新突破 1.0 + 价格转涨确认。"""
    res = _run_eval(
        "AAA", [0.80, 0.79, 0.82, 0.78, 0.83, 0.81, 0.84, 0.82, 0.80, 0.83, 0.85, 0.84, 1.32],
        1.2, _futures_map("AAA"),
    )
    assert res["signal"] == "cross_up"
    assert res["action"] == "alert"
    assert "空翻多" in res["reasons"][0]
    assert res["metrics"]["ls_now"] > 1.0


def test_cross_up_no_price_confirm():
    """空翻多但价格未确认上涨 → 不触发（避免假突破）。"""
    res = _run_eval(
        "BBB", [0.80, 0.79, 0.82, 0.78, 0.83, 0.81, 0.84, 0.82, 0.80, 0.83, 0.85, 0.84, 1.32],
        -0.5, _futures_map("BBB"),
    )
    assert res["signal"] == "none"


def test_cross_up_not_from_empty():
    """多空比已多头主导（均值>0.92）→ 不算空翻多。"""
    res = _run_eval(
        "CCC", [1.10, 1.12, 1.09, 1.11, 1.08, 1.13, 1.10, 1.12, 1.09, 1.11, 1.10, 1.12, 1.30],
        1.0, _futures_map("CCC"),
    )
    assert res["signal"] == "none"


def test_crash_price():
    """暴跌风险：15m 大跌 → 触发。"""
    res = _run_eval(
        "DDD", [0.95, 0.90, 0.88, 0.92, 0.87, 0.90, 0.86, 0.89, 0.85, 0.88, 0.84, 0.86, 0.80],
        -6.0, _futures_map("DDD"),
    )
    assert res["signal"] == "crash"
    assert res["action"] == "alert"
    assert "暴跌风险" in res["reasons"][0]


def test_crash_ratio_plunge():
    """暴跌风险：多空比骤降（<0.55 且连续下降 + 较均值降幅大）。"""
    res = _run_eval(
        "EEE", [1.0, 0.95, 0.90, 0.85, 0.88, 0.82, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.40],
        -1.0, _futures_map("EEE"),
    )
    assert res["signal"] == "crash"


def test_no_signal():
    """正常平稳 → 无信号。"""
    res = _run_eval(
        "FFF", [1.05, 1.06, 1.04, 1.05, 1.06, 1.05, 1.04, 1.06, 1.05, 1.04, 1.05, 1.06, 1.05],
        0.3, _futures_map("FFF"),
    )
    assert res["signal"] == "none"


def test_missing_contract():
    """无活跃永续合约 → 无信号并给出原因。"""
    res = _run_eval("ZZZ", [1.0] * 15, 0.1, {})
    assert res["signal"] == "none"
    assert "无活跃" in res["reasons"][0]


# ---------------- 消息格式化器 ----------------

def test_formatter_cross_up():
    r = {"symbol": "AAA", "futures_symbol": "AAAUSDT", "signal": "cross_up",
         "action": "alert",
         "metrics": {"price": 0.5, "ls_now": 1.32, "ls_mean": 0.84,
                     "ls_drops": 0, "chg_15m": 1.2},
         "reasons": ["空翻多：测试"]}
    msg = format_ls_message(r, pool_rank=5)
    assert "空翻多信号" in msg
    assert "多头主导" in msg
    assert "多空比 1.320" in msg


def test_formatter_crash():
    r = {"symbol": "BBB", "futures_symbol": "BBBUSDT", "signal": "crash",
         "action": "alert",
         "metrics": {"price": 0.2, "ls_now": 0.40, "ls_mean": 0.90,
                     "ls_drops": 3, "chg_15m": -6.0},
         "reasons": ["暴跌风险：测试"]}
    msg = format_ls_message(r)
    assert "暴跌风险" in msg
    assert "空头主导" in msg
    assert "止损/减仓" in msg


# ---------------- 云端执行器 ----------------

def _mk_hit(symbol, signal="cross_up"):
    return {"symbol": symbol, "futures_symbol": symbol + "USDT", "signal": signal,
            "action": "alert", "reasons": ["测试"], "metrics": {"ls_now": 1.3},
            "ts": "2026-08-07T12:00:00Z"}


class _FakeNotifier:
    def __init__(self, *a, **kw):
        self.sent = []

    def send_html(self, msg):
        self.sent.append(msg)
        return True


def _patch_cloud(pool, results):
    # 隔离去重状态：每次 patch 前清掉临时状态文件，避免跨用例污染
    state_path = "/tmp/ls_test_state_%s.json" % os.getpid()
    if os.path.exists(state_path):
        os.remove(state_path)
    patchers = [
        mock.patch.dict(os.environ,
                        {"TELEGRAM_BOT_TOKEN": "test-token",
                         "TELEGRAM_CHAT_ID": "test-chat"}),
        mock.patch.object(cloud, "DEFAULT_STATE", state_path),
        mock.patch.object(cloud.ls, "load_pool", return_value=pool),
        mock.patch.object(cloud.ls, "fetch_futures_symbols", return_value={}),
        mock.patch.object(cloud.ls, "scan_pool", return_value=results),
        mock.patch.object(cloud, "TelegramNotifier", _FakeNotifier),
        mock.patch.object(cloud, "push_state", return_value=True),
    ]
    for p in patchers:
        p.start()
    return patchers


def test_run_round_pushes_hits():
    pool = [{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "CCC"}]
    hits = [_mk_hit("AAA", "cross_up"), _mk_hit("BBB", "crash")]
    results = hits + [{"symbol": "CCC", "signal": "none", "action": "none",
                       "reasons": [], "metrics": {}}]
    patchers = _patch_cloud(pool, results)
    try:
        out = cloud.run_round(top=3, push_state_after=False, workers=1, dry_run=False)
    finally:
        for p in patchers:
            p.stop()
    assert len(out["hits"]) == 2
    assert len(out["pushed"]) == 2
    assert out["skipped_dup"] == []


def test_run_round_dedup_skips_duplicate():
    """去重窗口内同标的同信号只推一次。"""
    pool = [{"symbol": "AAA"}, {"symbol": "BBB"}]
    results = [_mk_hit("AAA", "cross_up"), _mk_hit("AAA", "cross_up"),
               {"symbol": "BBB", "signal": "none", "action": "none",
                "reasons": [], "metrics": {}}]
    patchers = _patch_cloud(pool, results)
    try:
        out = cloud.run_round(top=2, push_state_after=False, workers=1, dry_run=False)
    finally:
        for p in patchers:
            p.stop()
    assert len(out["pushed"]) == 1
    assert len(out["skipped_dup"]) == 1


def test_run_round_dry_run_no_push_requires():
    """dry-run 模式不依赖 Telegram 密钥，命中仅记录。"""
    pool = [{"symbol": "AAA"}]
    results = [_mk_hit("AAA", "cross_up")]
    patchers = _patch_cloud(pool, results)
    try:
        out = cloud.run_round(top=1, push_state_after=False, workers=1, dry_run=True)
    finally:
        for p in patchers:
            p.stop()
    assert len(out["pushed"]) == 1
