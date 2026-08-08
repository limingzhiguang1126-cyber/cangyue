# -*- coding: utf-8 -*-
"""v11_signal_watcher / v11_message_formatter / v11_signal_daemon 单元测试。

通过 mock 网络函数验证 v1.2 标准的判定逻辑（主信号②/辅助线③/通知线①/
一票否决/假启动排除）以及消息格式化、守护进程去重。
"""

# -*- coding: utf-8 -*-
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.screener import v11_signal_watcher as w
from src.screener import v11_signal_daemon as d
from src.notifier import v11_message_formatter as fm


# ---------------------------------------------------------------------------
# 工具：构造假数据
# ---------------------------------------------------------------------------
def _mk_4h_klines(chgs, vols_usd, base=100.0, start_ts=1785420000000):
    """构造 4h K 线序列：chgs 为每根相对前根的涨幅(%)，等长 vols_usd。"""
    out = []
    close = base
    for i, (chg, vol) in enumerate(zip(chgs, vols_usd)):
        open_ = close
        close = open_ * (1 + chg / 100.0)
        out.append({
            "ts": start_ts + i * 4 * 3600 * 1000,
            "open": round(open_, 6),
            "high": round(max(open_, close) * 1.01, 6),
            "low": round(min(open_, close) * 0.99, 6),
            "close": round(close, 6),
            "vol_usd": vol,
        })
    return out


def _mk_oi(values_usd):
    return [{"time": 1000 + i * 4 * 3600 * 1000, "oi_value": v} for i, v in enumerate(values_usd)]


def _mk_funding(rates):
    return [{"time": 1000 + i * 8 * 3600 * 1000, "rate": r} for i, r in enumerate(rates)]


def _mk_5m_klines(chgs, base=100.0, start_ts=1785420000000):
    out = []
    close = base
    for i, chg in enumerate(chgs):
        open_ = close
        close = open_ * (1 + chg / 100.0)
        out.append({
            "ts": start_ts + i * 5 * 60 * 1000,
            "open": round(open_, 6),
            "high": round(max(open_, close) * 1.01, 6),
            "low": round(min(open_, close) * 0.99, 6),
            "close": round(close, 6),
            "vol_usd": 1000.0,
        })
    return out


class _Patch:
    """手动 patch 模块级函数。"""

    def __init__(self, module, **kwargs):
        self.module = module
        self.orig = {k: getattr(module, k) for k in kwargs}
        for k, v in kwargs.items():
            setattr(module, k, v)

    def restore(self):
        for k, v in self.orig.items():
            setattr(self.module, k, v)


_FUTURES_MAP = {"BICO": "BICOUSDT", "HEI": "HEIUSDT", "TUT": "TUTUSDT", "KOMA": "KOMAUSDT"}


def _make_eval_env(kl, oi, funding, k5=None):
    """构造 evaluate_symbol 的网络依赖环境。

    按 interval 区分：4h 返回 kl，5m 返回 k5（未给则空列表）。
    """
    return _Patch(
        w,
        fetch_futures_symbols=lambda: dict(_FUTURES_MAP),
        resolve_futures_symbol=lambda base, fm: _FUTURES_MAP.get(base),
        _fetch_klines=lambda sym, interval="4h", limit=40: kl if interval == "4h" else (k5 or []),
        _fetch_oi_hist=lambda sym, period="4h", limit=40: oi,
        _fetch_funding=lambda sym, limit=30: funding,
    )


# ---------------------------------------------------------------------------
# 主信号②：4h≥15% + 回撤<20%
# ---------------------------------------------------------------------------
def test_signal2_build_when_oi_follows():
    """主信号②触发 + OI 同步放大 → 可建仓。"""
    # 前 24 根横盘（100 附近），最新一根 +16%，回撤小
    chgs = [0.0] * 24 + [16.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [50000.0])
    oi = _mk_oi([100.0] * 12 + [160.0] * 4)  # 放大 1.6x
    funding = _mk_funding([0.0001] * 10)
    k5 = _mk_5m_klines([0.0, 1.0])

    env = _make_eval_env(kl, oi, funding, k5)
    try:
        res = w.evaluate_symbol("BICO", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "signal2"
    assert res["action"] == "build"
    assert res["metrics"]["chg_4h"] >= w.SIGNAL_4H_MIN
    assert any("可建仓" in r for r in res["reasons"])


def test_signal2_watch_when_oi_not_following():
    """主信号②触发但 OI 未同步放大（<1.15x）→ 假启动排除，降级观察。"""
    chgs = [0.0] * 24 + [16.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [50000.0])
    oi = _mk_oi([100.0] * 12 + [105.0] * 4)  # 放大仅 1.05x
    funding = _mk_funding([0.0001] * 10)

    env = _make_eval_env(kl, oi, funding)
    try:
        res = w.evaluate_symbol("HEI", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "signal2"
    assert res["action"] == "watch"
    assert any("降级观察" in r for r in res["reasons"])


def test_signal2_veto_funding_high():
    """主信号② + 费率过热(>+0.3%) → 一票否决。"""
    chgs = [0.0] * 24 + [16.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [50000.0])
    oi = _mk_oi([100.0] * 12 + [160.0] * 4)
    funding = _mk_funding([0.0040] * 10)  # +0.4% > +0.3%

    env = _make_eval_env(kl, oi, funding)
    try:
        res = w.evaluate_symbol("BICO", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "veto"
    assert res["vetoed"] is True
    assert any("一票否决" in r for r in res["reasons"])
    assert res["action"] == "watch"


def test_signal2_veto_funding_low():
    """主信号② + 费率深负(< -1%) → 一票否决（仅 -1% 以下才视为出货）。"""
    chgs = [0.0] * 24 + [16.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [50000.0])
    oi = _mk_oi([100.0] * 12 + [160.0] * 4)
    funding = _mk_funding([-0.0020] * 10)  # -0.2%：仍在 [-1%, +0.3%] 内，不否决

    env = _make_eval_env(kl, oi, funding)
    try:
        res = w.evaluate_symbol("BICO", _FUTURES_MAP)
    finally:
        env.restore()

    # v1.2：-0.2% 已放宽，不再一票否决
    assert res["signal"] == "signal2"
    assert res["vetoed"] is False

    # 深度负费率 -1.2% 才否决
    funding = _mk_funding([-0.0120] * 10)
    env = _make_eval_env(kl, oi, funding)
    try:
        res = w.evaluate_symbol("BICO", _FUTURES_MAP)
    finally:
        env.restore()
    assert res["signal"] == "veto"
    assert res["vetoed"] is True


def test_signal2_veto_oi_drawdown():
    """主信号② + OI 较峰值回落>30% → 一票否决。"""
    chgs = [0.0] * 24 + [16.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [50000.0])
    # OI 峰值 200，当前跌到 120（-40%）
    oi = [{"time": 1000 + i * 4 * 3600 * 1000, "oi_value": 200.0} for i in range(12)]
    oi += [{"time": 1000 + 12 * 4 * 3600 * 1000, "oi_value": 120.0}]
    funding = _mk_funding([0.0001] * 10)

    env = _make_eval_env(kl, oi, funding)
    try:
        res = w.evaluate_symbol("BICO", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "veto"
    assert any("OI 较峰值回落" in r for r in res["reasons"])


# ---------------------------------------------------------------------------
# 辅助线③：4h 涨 2~8% + 量能 3x + OI 放大 1.15x
# ---------------------------------------------------------------------------
def test_aux3_observe():
    """辅助线③触发（量能+OI 齐动）→ 埋伏观察。"""
    chgs = [0.0] * 24 + [5.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [8000.0])  # 8x 量
    oi = _mk_oi([100.0] * 12 + [130.0] * 4)  # OI 1.3x
    funding = _mk_funding([0.0001] * 10)

    env = _make_eval_env(kl, oi, funding)
    try:
        res = w.evaluate_symbol("TUT", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "aux3"
    assert res["action"] == "observe"
    assert any("埋伏观察" in r for r in res["reasons"])


def test_aux3_not_trigger_when_vol_low():
    """4h 涨 5% 但量能不足 3x → 无信号。"""
    chgs = [0.0] * 24 + [5.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [2000.0])  # 2x 量
    oi = _mk_oi([100.0] * 16)
    funding = _mk_funding([0.0001] * 10)

    env = _make_eval_env(kl, oi, funding)
    try:
        res = w.evaluate_symbol("TUT", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "none"
    assert res["action"] == "none"


def test_aux3_not_trigger_when_oi_not_following():
    """4h 涨 5% + 量能 8x 但 OI 未放大（<1.15x）→ 无信号（放量下跌噪音滤除）。"""
    chgs = [0.0] * 24 + [5.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [8000.0])
    oi = _mk_oi([100.0] * 16)  # OI 1.0x，未放大
    funding = _mk_funding([0.0001] * 10)

    env = _make_eval_env(kl, oi, funding)
    try:
        res = w.evaluate_symbol("TUT", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "none"
    assert res["action"] == "none"


def test_aux3_veto_oi_drawdown_suspended():
    """辅助线③ + OI 回落>30% → v1.2 暂缓 OI 否决，仍触发埋伏观察。"""
    chgs = [0.0] * 24 + [5.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [8000.0])
    # OI 峰值 200，当前 120（-40%），但早期均值低 → oi_x 仍可能 ≥1.15
    oi = [{"time": 1000 + i * 4 * 3600 * 1000, "oi_value": 200.0} for i in range(12)]
    oi += [{"time": 1000 + 12 * 4 * 3600 * 1000, "oi_value": 120.0}]
    funding = _mk_funding([0.0001] * 10)

    env = _make_eval_env(kl, oi, funding)
    try:
        res = w.evaluate_symbol("TUT", _FUTURES_MAP)
    finally:
        env.restore()

    # 辅助线③ 不因 OI 回落被否决
    assert res["vetoed"] is False
    assert res["signal"] in ("aux3", "none")


# ---------------------------------------------------------------------------
# 通知线①：5m ≥6% 且距 4h 高点回撤 > -3%
# ---------------------------------------------------------------------------
def test_signal1_watch_without_confirm():
    """通知线①触发但确认条件不足 → 仅观察。"""
    chgs = [0.0] * 24 + [2.0]  # 4h 涨幅 2%，不触发主信号
    kl = _mk_4h_klines(chgs, [1000.0] * 25)
    oi = _mk_oi([100.0] * 16)
    funding = _mk_funding([0.0001] * 10)
    k5 = _mk_5m_klines([0.0, 8.0])  # 5m +8% ≥ 6%

    env = _make_eval_env(kl, oi, funding, k5)
    try:
        res = w.evaluate_symbol("BICO", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "signal1"
    assert res["action"] == "watch"
    assert any("通知线①" in r for r in res["reasons"])


def test_signal1_build_with_confirm():
    """通知线①触发 + 量能/OI/费率确认 → 可小仓。"""
    chgs = [0.0] * 24 + [1.0]  # 4h +1%：低于辅助线③下限(2%)，仅剩 5m 通知线
    # 最新 bar 放量 5x，OI 放大 1.3x
    kl = _mk_4h_klines(chgs, [1000.0] * 24 + [5000.0])
    oi = _mk_oi([100.0] * 12 + [130.0] * 4)
    funding = _mk_funding([0.0001] * 10)
    k5 = _mk_5m_klines([0.0, 8.0])

    env = _make_eval_env(kl, oi, funding, k5)
    try:
        res = w.evaluate_symbol("BICO", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "signal1"
    assert res["action"] == "build"
    assert any("可小仓" in r for r in res["reasons"])


def test_signal1_not_trigger_when_5m_low():
    """5m 涨幅 <6% 且无其他信号 → none。"""
    chgs = [0.0] * 24 + [2.0]
    kl = _mk_4h_klines(chgs, [1000.0] * 25)
    oi = _mk_oi([100.0] * 16)
    funding = _mk_funding([0.0001] * 10)
    k5 = _mk_5m_klines([0.0, 3.0])  # 5m +3%

    env = _make_eval_env(kl, oi, funding, k5)
    try:
        res = w.evaluate_symbol("BICO", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "none"


def test_signal1_not_trigger_when_drawdown_deep():
    """5m 涨 8% 但距 4h 高点回撤超过 3% → 冲高回落，不触发通知线①。"""
    # 前一根 4h 拉高（高点 130），当前 4h 跌回 100（回撤 -23%），5m 从 100 拉 8%
    base = [{"ts": 1000 + i * 4 * 3600 * 1000, "open": 100.0, "high": 100.0,
             "low": 99.0, "close": 99.0, "vol_usd": 1000.0} for i in range(24)]
    kl = base + [
        {"ts": 1000 + 24 * 4 * 3600 * 1000, "open": 99.0, "high": 130.0,
         "low": 98.0, "close": 100.0, "vol_usd": 5000.0},
        {"ts": 1000 + 25 * 4 * 3600 * 1000, "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.0, "vol_usd": 3000.0},
    ]
    oi = _mk_oi([100.0] * 16)
    funding = _mk_funding([0.0001] * 10)
    # 5m 序列从 100 拉 +8%
    k5 = [{"ts": 2000 + i * 5 * 60 * 1000, "open": 100.0, "high": 100.0,
           "low": 99.0, "close": 100.0, "vol_usd": 1000.0} for i in range(30)]
    k5[-1] = {"ts": 2000 + 29 * 5 * 60 * 1000, "open": 100.0, "high": 109.0,
              "low": 99.0, "close": 108.0, "vol_usd": 2000.0}
    k5[-2] = {"ts": 2000 + 28 * 5 * 60 * 1000, "open": 100.0, "high": 100.0,
              "low": 99.0, "close": 100.0, "vol_usd": 1000.0}

    env = _make_eval_env(kl, oi, funding, k5)
    try:
        res = w.evaluate_symbol("BICO", _FUTURES_MAP)
    finally:
        env.restore()

    assert res["signal"] == "none"
    assert res["action"] == "none"


# ---------------------------------------------------------------------------
# 消息格式化
# ---------------------------------------------------------------------------
def test_format_signal_message():
    res = {
        "symbol": "BICO",
        "futures_symbol": "BICOUSDT",
        "signal": "signal2",
        "action": "build",
        "metrics": {"price": 0.036, "chg_4h": 35.0, "drawdown": -5.0,
                    "vol_x": 8.0, "oi_x": 1.6, "funding_pct": 0.08,
                    "oi_dd_pct": -2.0, "chg_5m": 2.0},
        "reasons": ["主信号②：4h 涨幅 +35.0%，距本波高点回撤 -5.0%",
                    "OI 同步放大 1.60x，持仓进场，可建仓"],
    }
    msg = fm.format_signal_message(res, pool_rank=7)
    assert "BICO" in msg
    assert "可建仓" in msg
    assert "候选池排名：#7" in msg
    assert "主信号②" in msg
    assert "<b>" in msg  # HTML 格式


def test_format_signal_message_escapes():
    res = {
        "symbol": "A<B",
        "futures_symbol": "A<B USDT",
        "signal": "signal1",
        "action": "watch",
        "metrics": {"price": 1.0, "chg_5m": 12.0},
        "reasons": ["通知线①：5m 涨幅 +12.0%"],
    }
    msg = fm.format_signal_message(res)
    assert "<" not in msg.split("<b>")[0] or "&lt;" in msg  # 转义


# ---------------------------------------------------------------------------
# 守护进程去重
# ---------------------------------------------------------------------------
def test_dedup_window(tmp_path):
    state = str(tmp_path / "state.json")
    dd = d.SignalDeduplicator(state_path=state, window_seconds=7200)

    r1 = {"symbol": "BICO", "signal": "signal2"}
    r2 = {"symbol": "BICO", "signal": "signal2"}  # 相同
    r3 = {"symbol": "BICO", "signal": "signal1"}  # 不同信号线
    r4 = {"symbol": "HEI", "signal": "signal2"}   # 不同标的

    assert dd.check_and_mark(r1) is True
    assert dd.check_and_mark(r2) is False   # 重复
    assert dd.check_and_mark(r3) is True    # 新信号线
    assert dd.check_and_mark(r4) is True    # 新标的

    # 状态持久化：重新加载后依然去重
    dd2 = d.SignalDeduplicator(state_path=state, window_seconds=7200)
    assert dd2.is_new(r1) is False


def test_dedup_expire(tmp_path):
    state = str(tmp_path / "state.json")
    dd = d.SignalDeduplicator(state_path=state, window_seconds=100)
    r = {"symbol": "BICO", "signal": "signal2"}
    now = time.time()
    assert dd.check_and_mark(r, now) is True
    # 窗口过期后再次可推
    assert dd.check_and_mark(r, now + 200) is True
