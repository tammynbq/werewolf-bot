"""LLM 中转站客户端封装。

走 OpenAI 兼容协议：把 base_url 指向中转站 / 代理即可，
不直连官方 API。所有参数从 config 读取。
"""
from __future__ import annotations

import asyncio
import logging

from openai import AsyncOpenAI

import config

log = logging.getLogger("werewolf.llm")

# 失败重试：中转站偶发抖动（限流/瞬时网络）不该立刻让 NPC 没词。
# 总尝试次数 = 1 + RETRIES，每次失败后做一点退避。
RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.2

# 懒加载单例客户端：避免在缺少 key 时一 import 就崩（缺 key 由 config.validate 兜底）。
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.OPENAI_API_KEY or "missing",
            base_url=config.OPENAI_BASE_URL,
            timeout=config.LLM_TIMEOUT_SECONDS,  # 超时即兜底，避免卡死整局
        )
    return _client


def explain_error(exc: Exception) -> str:
    """把中转站异常翻译成「人话 + 该改哪个配置」，方便看日志直接定位。"""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    name = type(exc).__name__
    if status == 401 or "Authentication" in name or "PermissionDenied" in name:
        return "鉴权失败(401)：OPENAI_API_KEY 不对，或中转站不认这个 key"
    if status == 404 or "NotFound" in name:
        return ("找不到(404)：MODEL_NAME 不被中转站支持，"
                "或 OPENAI_BASE_URL 路径不对（通常要以 /v1 结尾）")
    if status == 429 or "RateLimit" in name:
        return "限流/额度(429)：中转站额度耗尽或被限速"
    if "Timeout" in name:
        return f"超时(>{config.LLM_TIMEOUT_SECONDS:g}s)：中转站太慢或网络不通"
    if "Connection" in name or "Connect" in name:
        return "连接失败：OPENAI_BASE_URL 地址不可达（host/端口/协议是否正确？）"
    if status:
        return f"HTTP {status}：{exc}"
    return f"{name}: {exc}"


async def health_check() -> tuple[bool, str]:
    """开机自检：打一次最小调用，确认中转站可用。返回 (是否正常, 说明)。"""
    if not config.OPENAI_API_KEY:
        return False, "缺少 OPENAI_API_KEY（环境变量没配）"
    try:
        await _get_client().chat.completions.create(
            model=config.MODEL_NAME,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        return True, "ok"
    except Exception as exc:
        return False, explain_error(exc)


async def chat(
    system: str,
    user: str,
    *,
    temperature: float | None = None,
    max_tokens: int = 220,
) -> str:
    """调用中转站做一次对话补全，返回纯文本。

    失败会自动重试 RETRIES 次；全部失败仍不抛异常，返回空字符串，让调用方
    自行决定如何降级（保证一局游戏不会因为 LLM 抽风而崩）。
    """
    last_exc: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            resp = await _get_client().chat.completions.create(
                model=config.MODEL_NAME,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=config.NPC_TEMPERATURE if temperature is None else temperature,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content or ""
            content = content.strip()
            if content:
                return content
            # 空回复也当作一次失败，重试一次往往就有内容了
            log.warning("LLM 中转站返回空内容（第 %d/%d 次）", attempt + 1, RETRIES + 1)
        except Exception as exc:  # 网络 / 额度 / 模型名错误等
            last_exc = exc
            log.warning("LLM 中转站调用失败（第 %d/%d 次）: %s", attempt + 1, RETRIES + 1, exc)
        if attempt < RETRIES:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    # 重试用尽：用 error 级别 + 人话原因，让日志一眼能看出是中转站配置/额度问题。
    if last_exc is not None:
        log.error("❌ LLM 中转站重试 %d 次仍失败 → %s（NPC 本次将沉默）",
                  RETRIES + 1, explain_error(last_exc))
    else:
        log.error("❌ LLM 中转站连续 %d 次返回空内容，放弃本次调用（NPC 本次将沉默）",
                  RETRIES + 1)
    return ""
