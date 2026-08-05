"""配置加载工具。

读取 config/config.yaml，并把其中的 ``${ENV_VAR}`` 占位符替换为
环境变量值（优先读取 .env 文件）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _load_dotenv(env_path: Path) -> None:
    """加载 .env 文件（不覆盖已存在的环境变量）。"""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _resolve(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


def load_config(config_path: str | Path = "config/config.yaml") -> Dict[str, Any]:
    """加载并解析配置（含 .env 环境变量替换）。"""
    path = Path(config_path)
    _load_dotenv(path.parent.parent / ".env")
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _resolve(raw)
