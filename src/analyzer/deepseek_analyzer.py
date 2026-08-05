"""DeepSeek 深度分析器（Phase 2）。

职责：对通过初筛的事件进行深度分析，输出结构化影响评估。
对应 PRD 模块3 的 Prompt 设计。

注意：初筛与深度分析统一使用同一个模型 DeepSeek-V4-Flash，
通过不同 prompt 切换角色。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from ..models import EventMessage
from ..screener.deepseek_screener import DeepSeekScreener
from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.analyzer")

_ANALYZER_PROMPT = """你是一个金融事件影响分析师。分析以下事件对金融市场的影响。

事件信息：
- 来源：{source}
- 标题：{title}
- 内容：{content}
- 时间：{timestamp}

请输出以下分析（严格JSON格式）：

{{
  "event_summary": "一句话总结事件核心",
  "event_type": "political | geopolitical | economic_data | corporate | regulatory | tech_breakthrough | other",
  "affected_instruments": [
    {{
      "instrument": "标的名称（如：NVDA, BTC, ETH, QQQ, 纳斯达克, 原油等）",
      "instrument_type": "us_stock | crypto | index | commodity | forex",
      "direction": "bullish | bearish | neutral",
      "confidence": 0.0-1.0,
      "reasoning": "为什么影响这个标的，影响逻辑",
      "suggested_action": "具体操作建议（如：考虑减仓/加仓/观望/做空/买入put等）",
      "time_horizon": "immediate | intraday | 1-3days | 1week | longer"
    }}
  ],
  "overall_market_impact": "low | medium | high | extreme",
  "key_risks": ["需要关注的风险点1", "风险点2"],
  "links_to_watch": ["需要持续关注的后续事件或数据"]
}}

注意：
1. 只列出有明确影响逻辑的标的，不要泛泛而谈
2. direction要给出明确判断，neutral仅在你真的无法判断时使用
3. confidence要诚实，不确定就给低分
4. suggested_action要具体可执行，不要说"注意风险"这种废话
5. 如果事件对加密和美股都有影响，都要列出
"""


class DeepSeekAnalyzer:
    """基于 DeepSeek-V4-Flash 的深度分析器。"""

    def __init__(self, screener: DeepSeekScreener | None = None):
        # 复用同一模型的客户端，只是 prompt 不同
        self._client = screener or DeepSeekScreener()

    def analyze(self, msg: EventMessage) -> Dict[str, Any]:
        """对事件做深度分析，返回结构化结果；失败时返回空分析。"""
        prompt = _ANALYZER_PROMPT.format(
            source=msg.source,
            title=msg.title,
            content=msg.content[:3000],
            timestamp=msg.timestamp,
        )
        try:
            raw = self._client._chat(prompt, max_tokens=2000, temperature=0.3)
            text = raw.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("analyzer call failed: %s", exc)
        return {}
