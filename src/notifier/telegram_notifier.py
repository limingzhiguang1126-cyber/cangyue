"""Telegram 消息推送服务。

对应 PRD 模块4：将分析结果格式化为 Telegram 消息并推送，
支持按重要性分级（importance 5/4 立即推送，3 每小时汇总）。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from ..models import EventMessage
from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.notifier")

_TG_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    """基于 Telegram Bot API 的推送器。"""

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    # ------------------------------------------------------------------
    # 基础发送
    # ------------------------------------------------------------------
    def send_text(self, text: str, parse_mode: str = "HTML", disable_web_page_preview: bool = True) -> bool:
        """发送纯文本/HTML 消息。"""
        if not self.bot_token or not self.chat_id:
            logger.warning("telegram not configured, skip send")
            return False
        url = _TG_API.format(token=self.bot_token, method="sendMessage")
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            ok = bool(data.get("ok"))
            if not ok:
                logger.warning("telegram send failed: %s", data)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram send error: %s", exc)
            return False

    def send_html(self, html: str) -> bool:
        return self.send_text(html, parse_mode="HTML")

    # ------------------------------------------------------------------
    # 业务级推送
    # ------------------------------------------------------------------
    def push_simple(self, msg: EventMessage) -> bool:
        """Phase 1 简单格式：标题 + 链接 + 来源 + 时间。"""
        lines = [
            f"📰 <b>{self._esc(msg.title)}</b>",
            "━━━━━━━━━━━━━━━━━",
            f"📡 来源：{self._esc(msg.source)}",
            f"🕐 时间：{self._esc(msg.timestamp)}",
        ]
        if msg.url:
            lines.append(f'🔗 <a href="{self._esc_attr(msg.url)}">原文链接</a>')
        return self.send_html("\n".join(lines))

    def push_analysis(self, analysis: dict) -> bool:
        """Phase 2+ 深度分析推送（结构化模板）。

        Args:
            analysis: 包含 event_summary / event_type / affected_instruments /
                      overall_market_impact / key_risks / links_to_watch 的 dict。
        """
        lines: list[str] = []
        importance = analysis.get("importance", "")
        summary = analysis.get("event_summary", "")
        lines.append(f"🚨 [{importance}级] {summary}")

        source = analysis.get("source", "")
        timestamp = analysis.get("timestamp", "")
        url = analysis.get("url", "")
        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append(f"📰 来源：{source} | {timestamp}")
        if url:
            lines.append(f'🔗 <a href="{self._esc_attr(url)}">原文链接</a>')

        instruments = analysis.get("affected_instruments", [])
        if instruments:
            lines.append("\n📊 影响标的：")
            for inst in instruments:
                direction = inst.get("direction", "neutral")
                emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(direction, "⚪")
                lines.append(
                    f"\n{emoji} {inst.get('instrument', '')} ({inst.get('instrument_type', '')})\n"
                    f"方向：{direction} (置信度: {inst.get('confidence', '')})\n"
                    f"逻辑：{inst.get('reasoning', '')}\n"
                    f"建议：{inst.get('suggested_action', '')}\n"
                    f"时间窗口：{inst.get('time_horizon', '')}"
                )

        impact = analysis.get("overall_market_impact", "")
        if impact:
            lines.append(f"\n⚡ 整体影响：{impact}")
        risks = analysis.get("key_risks", [])
        if risks:
            lines.append("⚠️ 风险点：" + "；".join(str(r) for r in risks))
        watch = analysis.get("links_to_watch", [])
        if watch:
            lines.append("👁️ 后续关注：" + "；".join(str(w) for w in watch))

        return self.send_html("\n".join(lines))

    def push_daily_report(self, report: str) -> bool:
        """Phase 3 次日复盘报告推送（纯文本）。"""
        return self.send_text(report, parse_mode="")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _esc(text) -> str:
        """HTML 转义。"""
        if text is None:
            return ""
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _esc_attr(text) -> str:
        return TelegramNotifier._esc(text).replace('"', "&quot;")
