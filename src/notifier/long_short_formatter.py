# -*- coding: utf-8 -*-
"""多空比/主动买卖拐点信号 → Telegram 消息格式化（纯函数，便于测试）。

把 long_short_sentinel.evaluate_ls 的输出格式化为一条适合 Telegram
推送的 HTML 消息，突出：空翻多 / 暴跌风险 + 关键多空比数据 + 建议。
"""

from __future__ import annotations

from typing import Any, Dict


def _esc(text) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_ls_message(result: Dict[str, Any], pool_rank: int = 0) -> str:
    """把单条多空比信号格式化为 Telegram HTML 消息。

    Args:
        result: evaluate_ls() 的输出。
        pool_rank: 该标的在候选池中的排名（1 起），0 表示未知。

    Returns:
        Telegram HTML 消息文本（可直接 send_html）。
    """
    symbol = result.get("symbol", "?")
    futures_symbol = result.get("futures_symbol", "")
    signal = result.get("signal", "none")
    m = result.get("metrics", {})
    reasons = result.get("reasons", [])

    # 标题头：空翻多 vs 暴跌风险
    if signal == "crash":
        header = f"⚠️ <b>{_esc(symbol)}</b> 暴跌风险"
    elif signal == "cross_up":
        header = f"🟢 <b>{_esc(symbol)}</b> 空翻多信号"
    else:
        header = f"📊 <b>{_esc(symbol)}</b> 多空比监控"

    lines = [header, "━━━━━━━━━━━━━━━━━"]
    if pool_rank:
        lines.append(f"📊 候选池排名：#{pool_rank}")
    if futures_symbol:
        lines.append(f"📈 合约：{_esc(futures_symbol)}")

    # 多空比指标
    metrics_bits = []
    if m.get("price") not in (None, ""):
        metrics_bits.append(f"现价 {m['price']}")
    if m.get("ls_now") is not None:
        metrics_bits.append(f"多空比 {m['ls_now']:.3f}")
    if m.get("ls_mean") is not None:
        metrics_bits.append(f"均值 {m['ls_mean']:.3f}")
    if m.get("ls_drops") is not None:
        metrics_bits.append(f"连降 {m['ls_drops']}根")
    if m.get("chg_15m") is not None:
        metrics_bits.append(f"15m {m['chg_15m']:+.2f}%")
    if metrics_bits:
        lines.append("📊 " + " | ".join(metrics_bits))

    # 多空比解读
    ls_now = m.get("ls_now")
    if ls_now is not None:
        if ls_now >= 1.0:
            lines.append(f"🧭 主动性买卖：<b>多头主导</b>（买>卖 {ls_now:.2f}x）")
        else:
            lines.append(f"🧭 主动性买卖：<b>空头主导</b>（卖>买 {ls_now:.2f}x）")

    lines.append("")
    lines.append("🎯 <b>触发原因：</b>")
    if reasons:
        for r in reasons:
            lines.append(f"· {r}")
    else:
        lines.append("· 无")

    # 建议
    if signal == "crash":
        advice = ("⚠️ 风险提示：空头主动砸盘/资金出逃，谨防急跌。"
                  "持仓者注意止损/减仓，勿盲目抄底接飞刀；等多空比企稳回升再评估。")
    elif signal == "cross_up":
        advice = ("🟢 机会提示：空头回补 + 主动性买盘进场，出现见底反弹迹象。"
                  "可列入观察，结合量能/OI/费率确认后再考虑小仓试错，务必设好止损。")
    else:
        advice = "无信号"
    lines.append("")
    lines.append(f"💡 <b>建议：</b>{advice}")
    lines.append("")
    lines.append("⚠️ 数据量化分析仅供参考，不构成投资建议")

    return "\n".join(lines)
