# -*- coding: utf-8 -*-
"""scripts/ls_cloud_run.py 的单元测试。

覆盖：
- _gh_push_url 在非 GitHub Actions 环境返回 None
- _gh_push_url 在 GitHub Actions 环境生成带 GITHUB_TOKEN 的推送地址
- push_state 在 GitHub Actions 环境下优先走 GITHUB_TOKEN
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import ls_cloud_run as cloud  # noqa: E402


def test_gh_push_url_absent_outside_gh():
    """非 GitHub Actions 环境：_gh_push_url 返回 None。"""
    with mock.patch.dict(os.environ, {}, clear=True):
        assert cloud._gh_push_url() is None


def test_gh_push_url_none_when_missing_token():
    """GitHub Actions 环境但缺 GITHUB_TOKEN：返回 None。"""
    with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
        assert cloud._gh_push_url() is None


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
    """GitHub Actions 环境下 push_state 优先走 GITHUB_TOKEN 推送。"""
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
            assert cloud.push_state("/tmp/state.json") is True
        # 应调用 remote set-url 到 GitHub 地址
        set_url_call = m_run.call_args_list[0]
        assert "x-access-token:gh_xxx@github.com/myuser/cangyue.git" in str(
            set_url_call
        )


def test_push_state_without_token_returns_false():
    """无任何 token 环境：push_state 返回 False（优雅跳过）。"""
    with mock.patch.dict(os.environ, {}, clear=True):
        assert cloud.push_state("/tmp/nonexist-state.json") is False
