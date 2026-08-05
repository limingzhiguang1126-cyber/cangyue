"""去重器与消息模型测试。"""

from src.models import EventMessage
from src.utils.dedup import Deduplicator, message_fingerprint


def test_message_fingerprint_by_url():
    a = EventMessage(source="ap", title="x", url="https://a.com/1")
    b = EventMessage(source="ap", title="x", url="https://a.com/1")
    assert message_fingerprint(a) == message_fingerprint(b)


def test_dedup_same_url():
    dedup = Deduplicator(window_seconds=3600)
    msg = EventMessage(source="ap", title="t", url="https://a.com/1")
    assert dedup.check_and_mark(msg) is True
    assert dedup.check_and_mark(msg) is False


def test_dedup_different_url():
    dedup = Deduplicator(window_seconds=3600)
    a = EventMessage(source="ap", title="t", url="https://a.com/1")
    b = EventMessage(source="ap", title="t", url="https://a.com/2")
    assert dedup.check_and_mark(a) is True
    assert dedup.check_and_mark(b) is True


def test_dedup_window_expire():
    dedup = Deduplicator(window_seconds=10)
    msg = EventMessage(source="ap", title="t", url="https://a.com/1")
    dedup.mark_seen(msg, now=100)
    # 超过窗口后应视为新消息
    assert dedup.is_duplicate(msg, now=200) is False


def test_message_roundtrip():
    msg = EventMessage(source="binance", title="上币", content="c", url="u", author="Binance")
    d = msg.to_dict()
    restored = EventMessage.from_dict(d)
    assert restored.title == msg.title
    assert restored.id == msg.id
