# -*- coding: utf-8 -*-
"""GitHub Actions 工作流配置文件校验。

确保 .github/workflows/*.yml 是可解析的 YAML 且包含关键步骤，
避免用户推到 GitHub 后才发现语法错误。
"""

import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(REPO_ROOT, ".github", "workflows")


class _GHLoader(yaml.SafeLoader):
    """GitHub Actions 按 YAML 1.2 语义解析，`on` 应保持字符串而不是布尔值。"""


_GHLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _load_workflow(name):
    path = os.path.join(WORKFLOW_DIR, name)
    assert os.path.exists(path), f"缺少工作流文件: {path}"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.load(f, Loader=_GHLoader)


def test_signal_scan_workflow_valid():
    wf = _load_workflow("signal-scan.yml")
    assert wf["name"] == "v1.2 信号扫描 + Telegram 推送"
    # 定时调度：每 15 分钟
    schedules = wf["on"]["schedule"]
    assert any(s["cron"] == "*/15 * * * *" for s in schedules)
    # 手动触发
    assert "workflow_dispatch" in wf["on"]
    # 关键配置
    assert wf["permissions"]["contents"] == "write"
    assert "concurrency" in wf
    job = wf["jobs"]["scan"]
    assert job["runs-on"] == "ubuntu-latest"
    steps = job["steps"]
    assert any("actions/checkout@v4" in (s.get("uses") or "") for s in steps)
    assert any("actions/setup-python@v5" in (s.get("uses") or "") for s in steps)
    # 扫描命令必须带 --push-state（真实推送时回推去重状态）
    scan = [s for s in steps if "v11_cloud_run.py" in (s.get("run") or "")]
    assert scan, "缺少扫描步骤"
    assert "--push-state" in scan[0]["run"]
    # 使用 secrets
    assert "${{ secrets.TELEGRAM_BOT_TOKEN }}" in scan[0]["env"]["TELEGRAM_BOT_TOKEN"]
    assert "${{ secrets.TELEGRAM_CHAT_ID }}" in scan[0]["env"]["TELEGRAM_CHAT_ID"]


def test_refresh_pool_workflow_valid():
    wf = _load_workflow("refresh-pool.yml")
    assert wf["name"] == "刷新 FDV Top100 候选池"
    schedules = wf["on"]["schedule"]
    assert any(s["cron"] == "35 7 * * *" for s in schedules)
    assert wf["permissions"]["contents"] == "write"
    job = wf["jobs"]["refresh"]
    steps = job["steps"]
    refresh = [s for s in steps if "binance_smallcap" in (s.get("run") or "")]
    assert refresh, "缺少刷新候选池步骤"
    assert "smallcap_top100_fdv.json" in refresh[0]["run"]
