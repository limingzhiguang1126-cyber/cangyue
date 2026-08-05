"""消息格式化与推送模板测试。"""

from src.models import EventMessage
from src.notifier.message_formatter import format_analysis, format_daily_report, format_simple
from src.notifier.telegram_notifier import TelegramNotifier


def test_format_simple():
    msg = EventMessage(source="ap", title="Headline", url="https://a.com/1", timestamp="2026-08-05T10:00:00Z")
    text = format_simple(msg)
    assert "Headline" in text
    assert "https://a.com/1" in text


def test_format_analysis():
    analysis = {
        "importance": 5,
        "event_summary": "FOMC 意外加息",
        "source": "ap",
        "timestamp": "2026-08-05T10:00:00Z",
        "url": "https://a.com/1",
        "affected_instruments": [
            {
                "instrument": "BTC",
                "instrument_type": "crypto",
                "direction": "bearish",
                "confidence": 0.8,
                "reasoning": "紧缩利空风险资产",
                "suggested_action": "观望",
                "time_horizon": "intraday",
            }
        ],
        "overall_market_impact": "high",
        "key_risks": ["流动性收紧"],
        "links_to_watch": ["下次 FOMC"],
    }
    text = format_analysis(analysis)
    assert "FOMC 意外加息" in text
    assert "BTC" in text
    assert "紧缩利空风险资产" in text


def test_format_daily_report():
    report = {
        "date": "2026-08-04",
        "correct": 2,
        "total": 3,
        "accuracy": 66.7,
        "weekly_accuracy": 60.0,
        "high_conf_accuracy": 75.0,
        "low_conf_accuracy": 50.0,
        "events": [
            {
                "event_summary": "CPI 超预期",
                "timestamp": "2026-08-04T08:30:00Z",
                "predictions": [
                    {
                        "instrument": "BTC",
                        "direction": "bearish",
                        "confidence": 0.8,
                        "actual_change": -2.1,
                        "prediction_correct": "correct",
                        "verification_note": "实际涨跌 -2.10%",
                    }
                ],
            }
        ],
    }
    text = format_daily_report(report)
    assert "总体准确率：2/3 = 66.7%" in text
    assert "BTC" in text


def test_telegram_escape():
    notifier = TelegramNotifier(bot_token="t", chat_id="c")
    assert notifier._esc("<b>&") == "&lt;b&gt;&amp;"
