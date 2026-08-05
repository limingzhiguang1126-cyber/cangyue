"""SQLite 事件存储。

对应 PRD 模块5：存储所有已推送事件，用于次日验证。
两张表：events 与 instrument_predictions。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,           -- 事件UUID
    source TEXT NOT NULL,          -- 数据来源
    title TEXT NOT NULL,           -- 事件标题
    content TEXT,                  -- 事件内容
    url TEXT,                      -- 原始链接
    timestamp TEXT NOT NULL,       -- 事件发生时间(UTC)
    importance INTEGER,            -- 重要性等级 1-5
    event_type TEXT,               -- 事件类型
    event_summary TEXT,            -- AI生成的事件摘要
    overall_market_impact TEXT,    -- 整体市场影响
    analysis_json TEXT,            -- 完整分析结果JSON
    pushed_at TEXT,                -- 推送时间
    verified INTEGER DEFAULT 0,    -- 是否已做次日验证
    verification_json TEXT,        -- 验证结果JSON
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instrument_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    instrument TEXT NOT NULL,      -- 标的名称
    instrument_type TEXT NOT NULL, -- us_stock | crypto | index | commodity
    direction TEXT NOT NULL,       -- bullish | bearish | neutral
    confidence REAL NOT NULL,      -- 置信度
    reasoning TEXT,                -- 影响逻辑
    suggested_action TEXT,         -- 操作建议
    time_horizon TEXT,             -- 时间窗口
    actual_change REAL,            -- 实际涨跌幅(%)
    prediction_correct TEXT,       -- correct | wrong | partial | pending
    verification_note TEXT,        -- 验证备注
    FOREIGN KEY (event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_events_verified ON events(verified);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_predictions_event ON instrument_predictions(event_id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventStore:
    """基于 SQLite 的事件存取。"""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # 事件写入 / 查询
    # ------------------------------------------------------------------
    def insert_event(self, event: Dict[str, Any]) -> None:
        """插入一个已推送事件（含分析结果）。"""
        analysis = event.get("analysis", {}) or {}
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO events
                   (id, source, title, content, url, timestamp, importance,
                    event_type, event_summary, overall_market_impact,
                    analysis_json, pushed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.get("id", ""),
                    event.get("source", ""),
                    event.get("title", ""),
                    event.get("content", ""),
                    event.get("url", ""),
                    event.get("timestamp", _utcnow()),
                    event.get("importance"),
                    analysis.get("event_type"),
                    analysis.get("event_summary"),
                    analysis.get("overall_market_impact"),
                    json.dumps(analysis, ensure_ascii=False),
                    event.get("pushed_at", _utcnow()),
                ),
            )

    def insert_predictions(self, event_id: str, instruments: List[Dict[str, Any]]) -> None:
        """插入事件的标的预测（用于次日验证）。"""
        with self._conn:
            for inst in instruments:
                self._conn.execute(
                    """INSERT INTO instrument_predictions
                       (event_id, instrument, instrument_type, direction,
                        confidence, reasoning, suggested_action, time_horizon,
                        prediction_correct)
                       VALUES (?,?,?,?,?,?,?,?, 'pending')""",
                    (
                        event_id,
                        inst.get("instrument", ""),
                        inst.get("instrument_type", ""),
                        inst.get("direction", "neutral"),
                        float(inst.get("confidence", 0.0)),
                        inst.get("reasoning", ""),
                        inst.get("suggested_action", ""),
                        inst.get("time_horizon", ""),
                    ),
                )

    def save_analysis(self, event: Dict[str, Any], analysis: Dict[str, Any]) -> None:
        """保存事件 + 分析结果 + 预测标的。"""
        event["analysis"] = analysis
        self.insert_event(event)
        self.insert_predictions(event.get("id", ""), analysis.get("affected_instruments", []))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def get_unverified_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        """查询所有待验证事件（verified=0）。"""
        rows = self._conn.execute(
            "SELECT * FROM events WHERE verified=0 ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_predictions(self, event_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM instrument_predictions WHERE event_id=?", (event_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_event_verified(self, event_id: str, verification: Dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE events SET verified=1, verification_json=? WHERE id=?",
                (json.dumps(verification, ensure_ascii=False), event_id),
            )

    def update_prediction_result(
        self, pred_id: int, actual_change: float, result: str, note: str
    ) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE instrument_predictions
                   SET actual_change=?, prediction_correct=?, verification_note=?
                   WHERE id=?""",
                (actual_change, result, note, pred_id),
            )

    def close(self) -> None:
        self._conn.close()
