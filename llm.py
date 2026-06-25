"""LLM 中转站客户端封装。

走 OpenAI 兼容协议：把 base_url 指向中转站 / 代理即可，
不直连官方 API。所有参数从 config 读取。
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

import config

log = logging.getLogger("werewolf.llm")

# 懒加载单例客户端：避免在缺少 key 时一 import 就崩（缺 key 由 config.validate 兜底）。
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.OPENAI_API_KEY or "missing",
            base_url=config.OPENAI_BASE_URL,
        )
    return _client


async def chat(
    system: str,
    user: str,
    *,
    temperature: float | None = None,
    max_tokens: int = 220,
) -> str:
    """调用中转站做一次对话补全，返回纯文本。

    出错时不抛异常，返回空字符串，让调用方走兜底逻辑（保证一局游戏不会因为
    LLM 抽风而崩）。
    """
    try:
        resp = await _get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=config.NPC_TEMPERATURE if temperature is None else temperature,
            max_tokens=max_tokens,
        )
        content = resp.choices[0].message.content or ""
        return content.strip()
    except Exception as exc:  # 网络 / 额度 / 模型名错误等
        log.warning("LLM 中转站调用失败: %s", exc)
        return ""
