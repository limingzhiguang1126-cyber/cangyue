# -*- coding: utf-8 -*-
"""MA20 回踩信号 → Telegram 消息格式化（纯函数，便于测试）。

把 ma20_pullback.scan_pullback 的单条结果格式化为适合 Telegram
推送的 HTML 消息，突出：命中级别 + 现价/MA20 偏离 + 24h 成交额。
"""

from __future__ import annotations

from typing import Any, Dict


def _esc(text) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_ma20_message(result: Dict[str, Any], pool_rank: int = 0) -> str:
    """把单条 MA20 回踩结果格式化为 Telegram HTML 消息。

    Args:
        result: scan_pullback() 输出的单条结果。
        pool_rank: 在成交额 TopN 中的排名（1 起），0 表示未知。

    Returns:
        Telegram HTML 消息文本（可直接 send_html）。
    """
    symbol = result.get("symbol", "?")
    base = result.get("base", "")
    price = result.get("price", 0)
    vol = result.get("quote_volume", 0)
    chg24 = result.get("chg_24h_pct")
    levels = result.get("levels", []) or []

    # 排序：15m/1h/4h
    order = {"15m": 0, "1h": 1, "4h": 2}
    levels = sorted(levels, key=lambda x: order.get(x.get("interval", ""), 9))

    lines = [
        f"📉 <b>{_esc(symbol)}</b> 回踩 MA20",
        "━━━━━━━━━━━━━━━━━",
    ]
    if pool_rank:
        lines.append(f"🏆 合约成交额排名：#{pool_rank}")
    if base:
        lines.append(f"📈 合约：{_esc(symbol)}")

    # 关键指标一行
    metrics_bits = []
    if price:
        metrics_bits.append(f"现价 {price}")
    if chg24 is not None:
        metrics_bits.append(f"24h {chg24:+.2f}%")
    if vol:
        metrics_bits.append(f"成交额 {vol / 1e8:.2f}亿")
    if metrics_bits:
        lines.append("📊 " + " | ".join(metrics_bits))

    lines.append("")
    lines.append("🎯 <b>回踩级别（MA20 偏离）：</b>")
    if levels:
        for lv in levels:
            lines.append(
                f"· {_esc(lv.get('label', lv.get('interval', '')))}："
                f"MA20={lv.get('ma20', '-'):g} "
                f"偏离 <b>{lv.get('dev_pct', 0):+.2f}%</b>"
            )
    else:
        lines.append("· 无")

    lines.append("")
    lines.append(
        "💡 <b>策略解读：</b>价格从上方回调贴近 MA20 但仍位于均线上方，"
        "属趋势中的回踩买点。可结合 15m/1h/4h 多级别共振判断，"
        "回踩不破 MA20 且企稳转涨时关注，务必设好止损（跌破 MA20 视为失效）。"
    )
    lines.append("")
    lines.append("⚠️ 数据量化分析仅供参考，不构成投资建议")

    return "\n".join(lines)
