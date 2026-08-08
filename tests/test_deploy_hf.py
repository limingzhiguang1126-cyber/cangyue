# -*- coding: utf-8 -*-
"""scripts/deploy_hf.py 单元测试（无需真实 HF 网络）。

验证：
- 打包文件收集逻辑（含忽略规则、HF 部署文件）
- 用户名自动探测（whoami）
- secret 写入逻辑（跳过已存在、只写非空）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from unittest import mock  # noqa: E402

import deploy_hf  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_collect_local_files_contains_core_items():
    files = deploy_hf.collect_local_files(ROOT)
    rels = set(files.values())
    # HF Space 部署文件
    assert "Dockerfile" in rels
    assert "app.py" in rels
    assert "README.md" in rels
    # 核心目录/文件
    assert "requirements.txt" in rels
    assert ".env.example" in rels
    assert any(r.startswith("src/") for r in rels)
    assert any(r.startswith("scripts/") for r in rels)
    assert any(r.startswith("data/") for r in rels)
    assert any(r.startswith("config/") for r in rels)


def test_collect_local_files_ignores_sensitive():
    files = deploy_hf.collect_local_files(ROOT)
    rels = set(files.values())
    # 不允许打包 .git / .env / __pycache__ / .venv（.env.example 是模板，允许）
    assert ".git" not in rels and not any("/.git/" in r for r in rels)
    assert ".env" not in rels
    assert not any("/__pycache__/" in r for r in rels)
    assert ".venv" not in rels and not any("/.venv/" in r for r in rels)
    # .env.example 模板应当保留
    assert ".env.example" in rels


def test_resolve_user_uses_whoami_when_empty():
    api = mock.Mock()
    api.whoami.return_value = {"name": "changan"}
    assert deploy_hf.resolve_user(api, "") == "changan"


def test_resolve_user_uses_given_user():
    api = mock.Mock()
    assert deploy_hf.resolve_user(api, "myuser") == "myuser"
    api.whoami.assert_not_called()


def test_write_secrets_skips_existing_and_empty():
    api = mock.Mock()
    existing = mock.Mock()
    existing.secrets = [{"key": "TELEGRAM_BOT_TOKEN"}]
    api.repo_info.return_value = existing

    deploy_hf.write_secrets(api, "u/s", {
        "TELEGRAM_BOT_TOKEN": "existing-token",   # 已存在 → 跳过
        "TELEGRAM_CHAT_ID": "new-chat",           # 不存在 → 写入
        "HF_TOKEN": "",                            # 空值 → 跳过
    })
    api.add_space_secret.assert_called_once_with(
        repo_id="u/s", key="TELEGRAM_CHAT_ID", value="new-chat")
