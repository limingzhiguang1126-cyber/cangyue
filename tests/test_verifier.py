"""准确性判断与存储测试。"""

from src.storage.database import EventStore
from src.verifier.accuracy_checker import compute_accuracy, judge_prediction

import os
import tempfile


def test_judge_prediction():
    assert judge_prediction("bullish", 1.2) == "correct"
    assert judge_prediction("bullish", -1.2) == "wrong"
    assert judge_prediction("bearish", -1.2) == "correct"
    assert judge_prediction("bearish", 1.2) == "wrong"
    assert judge_prediction("bullish", 0.2) == "partial"
    assert judge_prediction("neutral", 1.2) == "neutral_skip"


def test_compute_accuracy():
    results = [
        {"direction": "bullish", "confidence": 0.8, "actual_change": 1.0, "prediction_correct": "correct"},
        {"direction": "bullish", "confidence": 0.6, "actual_change": -1.0, "prediction_correct": "wrong"},
        {"direction": "bearish", "confidence": 0.9, "actual_change": -2.0, "prediction_correct": "correct"},
    ]
    stats = compute_accuracy(results)
    assert stats["total"] == 3
    assert stats["correct"] == 2
    assert stats["accuracy"] == 66.7
    assert stats["high_conf_accuracy"] == 100.0  # 0.8 与 0.9 两条
    assert stats["low_conf_accuracy"] == 0.0


def test_event_store_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = EventStore(os.path.join(tmp, "test.db"))
        event = {
            "id": "evt-1",
            "source": "ap",
            "title": "Test Event",
            "timestamp": "2026-08-05T10:00:00Z",
            "importance": 4,
            "analysis": {
                "event_summary": "summary",
                "event_type": "political",
                "overall_market_impact": "high",
                "affected_instruments": [
                    {"instrument": "BTC", "instrument_type": "crypto", "direction": "bullish",
                     "confidence": 0.8, "reasoning": "r", "suggested_action": "s", "time_horizon": "intraday"}
                ],
            },
        }
        store.save_analysis(event, event["analysis"])
        got = store.get_event("evt-1")
        assert got is not None
        assert got["title"] == "Test Event"
        preds = store.get_predictions("evt-1")
        assert len(preds) == 1
        assert preds[0]["instrument"] == "BTC"
        store.close()
