# -*- coding: utf-8 -*-
"""v1.2 信号 → Telegram 消息格式化（纯函数，便于测试）。

把 v11_signal_watcher.evaluate_symbol 的输出格式化为一条
适合 Telegram 推送的 HTML 消息，突出：命中原因 + 观察/建仓建议。
"""

from __future__ import annotations

from typing import Any, Dict


def _esc(text) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ACTION_ICON = {
    "build": "🟢 可建仓",
    "watch": "🟡 观察",
    "observe": "🔵 埋伏观察",
    "none": "⚪ 无信号",
}


def format_signal_message(result: Dict[str, Any], pool_rank: int = 0) -> str:
    """把单条 v1.2 信号格式化为 Telegram HTML 消息。

    Args:
        result: evaluate_symbol() 的输出。
        pool_rank: 该标的在候选池中的排名（1 起），0 表示未知。

    Returns:
        Telegram HTML 消息文本（可直接 send_html）。
    """
    symbol = result.get("symbol", "?")
    futures_symbol = result.get("futures_symbol", "")
    action = result.get("action", "none")
    signal = result.get("signal", "none")
    m = result.get("metrics", {})
    reasons = result.get("reasons", [])

    action_icon = _ACTION_ICON.get(action, _ACTION_ICON["none"])

    lines = [
        f"🚨 <b>{_esc(symbol)}</b> {action_icon}",
        "━━━━━━━━━━━━━━━━━",
    ]
    if pool_rank:
        lines.append(f"📊 候选池排名：#{pool_rank}")
    if futures_symbol:
        lines.append(f"📈 合约：{_esc(futures_symbol)}")

    # 关键指标一行
    metrics_bits = []
    if m.get("price") not in (None, ""):
        metrics_bits.append(f"现价 {m['price']}")
    if m.get("chg_4h") is not None:
        metrics_bits.append(f"4h {m['chg_4h']:+.1f}%")
    if m.get("chg_5m") is not None:
        metrics_bits.append(f"5m {m['chg_5m']:+.1f}%")
    if m.get("vol_x") is not None:
        metrics_bits.append(f"量能 {m['vol_x']:.1f}x")
    if m.get("oi_x") is not None:
        metrics_bits.append(f"OI {m['oi_x']:.2f}x")
    if m.get("funding_pct") is not None:
        metrics_bits.append(f"费率 {m['funding_pct']:+.3f}%")
    if m.get("oi_dd_pct") is not None:
        metrics_bits.append(f"OI回撤 {m['oi_dd_pct']:.1f}%")
    if metrics_bits:
        lines.append("📊 " + " | ".join(metrics_bits))

    lines.append("")
    lines.append("🎯 <b>命中原因：</b>")
    if reasons:
        for r in reasons:
            lines.append(f"· {r}")
    else:
        lines.append("· 无")

    # 建议
    advice = {
        "build": "可建仓：按计划分批进场，务必设好止损（跌破起爆点/入场价 -5% 无条件走）",
        "watch": "观察：先放入观察池，待量能/OI/费率确认后再决定是否参与",
        "observe": "埋伏观察：提前盯住，等 OI 跟进、量能持续再确认",
        "none": "无信号",
    }.get(action, "无信号")
    lines.append("")
    lines.append(f"💡 <b>建议：</b>{advice}")
    lines.append("")
    lines.append("⚠️ 数据量化分析仅供参考，不构成投资建议")

    return "\n".join(lines)
