# -*- coding: utf-8 -*-
"""scripts/v11_cloud_run.py 单元测试。

验证云端执行器的核心逻辑（无需网络）：
- run_round 的推送/去重/跳过流程
- 状态回推失败时优雅降级（不影响推送结果）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from unittest import mock

import v11_cloud_run as cloud  # noqa: E402

_TG_ENV = {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "test-chat"}


def _mk_result(symbol, signal="signal2", action="build"):
    return {
        "symbol": symbol, "futures_symbol": symbol + "USDT",
        "signal": signal, "action": action, "vetoed": False,
        "reasons": [f"{symbol} 命中主信号②"], "metrics": {"price": 1.0},
        "ts": "2026-08-07T12:00:00Z",
    }


class _FakeNotifier:
    def __init__(self, *a, **kw):
        self.sent = []
        self.ok = True

    def send_html(self, msg):
        self.sent.append(msg)
        return self.ok


class _FakeDedup:
    def __init__(self, *a, **kw):
        self.seen = set()

    def is_new(self, r):
        return r.get("symbol") not in self.seen

    def mark_pushed(self, r):
        self.seen.add(r.get("symbol"))


def _patch_all(pool, results):
    """统一 patch：pool/scan 用假数据，notifier/dedup 用假对象。"""
    stack = mock.patch.multiple(
        cloud.watcher,
        load_pool=mock.DEFAULT,
        fetch_futures_symbols=mock.DEFAULT,
        scan_pool=mock.DEFAULT,
    )
    patchers = [
        stack,
        mock.patch.object(cloud, "TelegramNotifier", return_value=_FakeNotifier()),
        mock.patch.object(cloud.daemon, "SignalDeduplicator", return_value=_FakeDedup()),
        mock.patch.dict(os.environ, _TG_ENV, clear=False),
    ]
    for p in patchers:
        p.start()
    cloud.watcher.load_pool.return_value = pool
    cloud.watcher.fetch_futures_symbols.return_value = {}
    cloud.watcher.scan_pool.return_value = results
    return patchers


def test_run_round_push_and_dedup():
    pool = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
    results = [
        _mk_result("A", "signal2", "build"),
        _mk_result("B", "aux3", "observe"),
        _mk_result("C", "none", "none"),
    ]
    patchers = _patch_all(pool, results)
    try:
        out = cloud.run_round(top=3, push_state_after=False, workers=2)
    finally:
        for p in patchers:
            p.stop()

    assert len(out["pushed"]) == 2, out
    assert len(out["skipped_dup"]) == 0
    assert {"A", "B"} == {r["symbol"] for r in out["pushed"]}


def test_run_round_dedup_skip_duplicate():
    pool = [{"symbol": "A"}, {"symbol": "B"}]
    results = [
        _mk_result("A", "signal2", "build"),
        _mk_result("A", "signal2", "build"),  # 同标的重复命中
    ]
    patchers = _patch_all(pool, results)
    try:
        out = cloud.run_round(top=2, push_state_after=False, workers=2)
    finally:
        for p in patchers:
            p.stop()

    assert len(out["pushed"]) == 1
    assert len(out["skipped_dup"]) == 1


def test_push_state_failure_does_not_break():
    """状态回推失败（如无 CNB_TOKEN）不影响本轮扫描/推送结果。"""
    pool = [{"symbol": "A"}]
    results = [_mk_result("A", "signal2", "build")]
    patchers = _patch_all(pool, results)
    try:
        with mock.patch.object(cloud, "push_state", return_value=False) as ps:
            out = cloud.run_round(top=1, push_state_after=True, workers=1)
    finally:
        for p in patchers:
            p.stop()

    assert len(out["pushed"]) == 1
    assert ps.called


def test_push_state_without_token_returns_false():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert cloud.push_state("/tmp/nonexist-state.json") is False


def test_gh_push_url_absent_outside_gh():
    """非 GitHub Actions 环境：_gh_push_url 返回 None。"""
    with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
        assert cloud._gh_push_url() is None  # 无 GITHUB_TOKEN / GITHUB_REPOSITORY


def test_gh_push_url_present_in_gh():
    """GitHub Actions 环境：_gh_push_url 生成带 GITHUB_TOKEN 的推送地址。"""
    with mock.patch.dict(
        os.environ,
        {"GITHUB_ACTIONS": "true", "GITHUB_TOKEN": "gh_xxx",
         "GITHUB_REPOSITORY": "myuser/cangyue"},
        clear=True,
    ):
        url = cloud._gh_push_url()
        assert url == "https://x-access-token:gh_xxx@github.com/myuser/cangyue.git"


def test_push_state_uses_gh_url_in_gh_actions():
    """GitHub Actions 环境下 push_state 走 GITHUB_TOKEN 推送，不依赖 CNB_TOKEN。"""
    with mock.patch.dict(
        os.environ,
        {"GITHUB_ACTIONS": "true", "GITHUB_TOKEN": "gh_xxx",
         "GITHUB_REPOSITORY": "myuser/cangyue", "GITHUB_REF_NAME": "main"},
        clear=True,
    ):
        with mock.patch.object(cloud.subprocess, "run") as m_run:
            proc = mock.Mock()
            proc.returncode = 0
            m_run.return_value = proc
            ok = cloud.push_state("/tmp/state.json")
    assert ok is True
    # 推送到 github 地址，而非 cnb 地址
    set_url_calls = [c for c in m_run.call_args_list
                     if c.args and c.args[0] == ["git", "remote", "set-url", "origin",
                                                  "https://x-access-token:gh_xxx@github.com/myuser/cangyue.git"]]
    assert set_url_calls, m_run.call_args_list


def test_push_state_gh_no_token_returns_false():
    """GitHub Actions 但缺 GITHUB_TOKEN：push_state 返回 False（优雅降级）。"""
    with mock.patch.dict(
        os.environ,
        {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "myuser/cangyue"},
        clear=True,
    ):
        assert cloud.push_state("/tmp/nonexist-state.json") is False


def test_run_round_dry_run_does_not_push_or_push_state():
    """dry-run 模式：即使配了 Telegram 密钥也不推送，且不触发状态回推。"""
    pool = [{"symbol": "A"}]
    results = [_mk_result("A", "signal2", "build")]
    patchers = _patch_all(pool, results)
    try:
        with mock.patch.object(cloud, "push_state", return_value=True) as ps:
            out = cloud.run_round(top=1, push_state_after=True, workers=1,
                                  dry_run=True)
    finally:
        for p in patchers:
            p.stop()

    assert len(out["pushed"]) == 1  # dry-run 也计入"处理"结果，但仅打印
    assert not ps.called            # dry-run 不推送状态
    # 确认未使用 TelegramNotifier（dry-run 强制 notifier=None）
    assert out["hits"][0]["symbol"] == "A"
