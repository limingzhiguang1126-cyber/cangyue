"""预判准确性判断（Phase 3）。

判断规则（对应 PRD 模块6）：
- bullish + 实际上涨 → correct
- bullish + 实际下跌 → wrong
- bearish + 实际下跌 → correct
- bearish + 实际上涨 → wrong
- 涨跌幅 < threshold（默认 0.5%）→ partial（影响不显著）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_THRESHOLD_DEFAULT = 0.5


def judge_prediction(direction: str, actual_change: float, threshold: float = _THRESHOLD_DEFAULT) -> str:
    """返回 correct | wrong | partial | neutral_skip。"""
    if abs(actual_change) < threshold:
        return "partial"
    if direction == "bullish":
        return "correct" if actual_change > 0 else "wrong"
    if direction == "bearish":
        return "correct" if actual_change < 0 else "wrong"
    return "neutral_skip"


def judge_all(
    predictions: List[Dict[str, Any]],
    actual_changes: Dict[str, float],
    threshold: float = _THRESHOLD_DEFAULT,
) -> List[Dict[str, Any]]:
    """批量判断，返回带结果的预测列表。

    Args:
        predictions: instrument_predictions 记录列表
        actual_changes: {instrument: 实际涨跌幅%}
    """
    results = []
    for pred in predictions:
        instrument = pred.get("instrument", "")
        change = actual_changes.get(instrument)
        if change is None:
            result = "pending"
            note = "未获取到价格数据"
        else:
            result = judge_prediction(pred.get("direction", "neutral"), change, threshold)
            note = f"实际涨跌 {change:+.2f}%"
        results.append({**pred, "actual_change": change, "prediction_correct": result, "verification_note": note})
    return results


def compute_accuracy(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """计算总体准确率统计。

    返回 {correct, total, accuracy, high_conf_accuracy, low_conf_accuracy}。
    """
    judged = [r for r in results if r.get("prediction_correct") in ("correct", "wrong", "partial")]
    total = len(judged)
    correct = sum(1 for r in judged if r.get("prediction_correct") == "correct")
    accuracy = round(correct / total * 100, 1) if total else 0.0

    high = [r for r in judged if float(r.get("confidence", 0)) >= 0.7]
    low = [r for r in judged if float(r.get("confidence", 0)) < 0.7]
    high_correct = sum(1 for r in high if r.get("prediction_correct") == "correct")
    low_correct = sum(1 for r in low if r.get("prediction_correct") == "correct")

    return {
        "correct": correct,
        "total": total,
        "accuracy": accuracy,
        "high_conf_accuracy": round(high_correct / len(high) * 100, 1) if high else 0.0,
        "low_conf_accuracy": round(low_correct / len(low) * 100, 1) if low else 0.0,
    }
