"""Telegram 消息格式化（纯函数，便于测试）。"""

from __future__ import annotations

from typing import Any, Dict, List

from ..models import EventMessage


def format_simple(msg: EventMessage) -> str:
    """Phase 1 简单推送模板。"""
    return (
        f"📰 {msg.title}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📡 来源：{msg.source} | {msg.timestamp}\n"
        f"🔗 {msg.url}"
    )


def format_analysis(analysis: Dict[str, Any]) -> str:
    """Phase 2 深度分析消息模板（纯文本版）。"""
    lines: List[str] = []
    importance = analysis.get("importance", "")
    summary = analysis.get("event_summary", "")
    lines.append(f"🚨 [{importance}级] {summary}")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append(f"📰 来源：{analysis.get('source', '')} | {analysis.get('timestamp', '')}")
    url = analysis.get("url", "")
    if url:
        lines.append(f"🔗 {url}")

    instruments = analysis.get("affected_instruments", [])
    if instruments:
        lines.append("")
        lines.append("📊 影响标的：")
        for inst in instruments:
            direction = inst.get("direction", "neutral")
            emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(direction, "⚪")
            lines.append("")
            lines.append(f"{emoji} {inst.get('instrument', '')} ({inst.get('instrument_type', '')})")
            lines.append(f"方向：{direction} (置信度: {inst.get('confidence', '')})")
            lines.append(f"逻辑：{inst.get('reasoning', '')}")
            lines.append(f"建议：{inst.get('suggested_action', '')}")
            lines.append(f"时间窗口：{inst.get('time_horizon', '')}")

    impact = analysis.get("overall_market_impact", "")
    if impact:
        lines.append("")
        lines.append(f"⚡ 整体影响：{impact}")
    risks = analysis.get("key_risks", [])
    if risks:
        lines.append("")
        lines.append("⚠️ 风险点：" + "；".join(str(r) for r in risks))
    watch = analysis.get("links_to_watch", [])
    if watch:
        lines.append("")
        lines.append("👁️ 后续关注：" + "；".join(str(w) for w in watch))
    return "\n".join(lines)


def format_daily_report(report: Dict[str, Any]) -> str:
    """Phase 3 次日复盘报告模板。"""
    lines: List[str] = []
    date = report.get("date", "")
    total = report.get("total", 0)
    correct = report.get("correct", 0)
    accuracy = report.get("accuracy", 0)
    lines.append(f"📋 昨日事件复盘报告 ({date})")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"📊 总体准确率：{correct}/{total} = {accuracy}%")

    for event in report.get("events", []):
        lines.append("")
        lines.append(f"📌 事件：{event.get('event_summary', '')}")
        lines.append(f"时间：{event.get('timestamp', '')}")
        for pred in event.get("predictions", []):
            lines.append("")
            lines.append(f"  {pred.get('instrument', '')} 预判：{pred.get('direction', '')} "
                         f"(置信度 {pred.get('confidence', '')})")
            lines.append(f"  实际：{pred.get('actual_change', '')}% → {pred.get('prediction_correct', '')}")
            note = pred.get("verification_note", "")
            if note:
                lines.append(f"  💡 {note}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append(f"📈 本周累计准确率：{report.get('weekly_accuracy', 0)}%")
    lines.append(f"🎯 置信度校准：高置信度 {report.get('high_conf_accuracy', 0)}% "
                 f"vs 低置信度 {report.get('low_conf_accuracy', 0)}%")
    return "\n".join(lines)
