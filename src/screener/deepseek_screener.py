"""DeepSeek 初筛过滤器（Phase 2）。

职责：用便宜模型快速判断每条新闻是否与金融市场相关、是否重要。
对应 PRD 模块2 的 Prompt 设计。

依赖：
- 环境变量 DEEPSEEK_API_BASE / DEEPSEEK_API_KEY（见 .env.example）
- Phase 1 阶段不启用，本模块为骨架实现。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import requests

from ..models import EventMessage
from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.screener")

_SCREENER_PROMPT = """你是一个金融新闻初筛器。判断以下新闻是否：
1. 与金融市场相关（美股、加密货币、外汇、大宗商品）
2. 重要性等级（1-5，5最严重）

重要性评判标准：
5级：总统/美联储重大发言、战争/地缘冲突升级、重大并购/收购、黑天鹅事件
4级：重要经济数据、大型科技公司重大公告、监管执法行动
3级：行业政策变化、中型公司重大事件、重要分析师评级变动
2级：常规财报、行业会议发言、一般性监管新闻
1级：行业资讯、人事变动、常规公告

新闻：
- 来源：{source}
- 标题：{title}
- 内容：{content}

只返回JSON: {{"relevant": true/false, "importance": 1-5, "reason": "一句话原因"}}
"""


class DeepSeekScreener:
    """基于 DeepSeek-V4-Flash 的初筛过滤器。"""

    def __init__(self, api_key: str = "", api_base: str = "", model: str = "deepseek-v4-flash"):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.api_base = api_base or os.getenv("DEEPSEEK_API_BASE", "") or "https://api.deepseek.com"
        self.model = model

    def _chat(self, prompt: str, max_tokens: int = 200, temperature: float = 0.1) -> str:
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY 未配置")
        resp = requests.post(
            f"{self.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def screen(self, msg: EventMessage) -> Optional[Dict[str, Any]]:
        """返回 {relevant, importance, reason}；调用失败返回 None。"""
        prompt = _SCREENER_PROMPT.format(
            source=msg.source, title=msg.title, content=msg.content[:1500]
        )
        try:
            raw = self._chat(prompt)
            text = raw.strip()
            # 去掉可能的 ```json 围栏
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("screener call failed: %s", exc)
            return None

    @staticmethod
    def should_analyze(result: Dict[str, Any]) -> bool:
        """过滤规则：relevant=false 或 importance<=2 则丢弃。"""
        return bool(result.get("relevant")) and int(result.get("importance", 0)) >= 3
