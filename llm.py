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
# 撞到 429（限流）时退避得更狠：免费站多半是「每分钟」限流，等久点才有意义。
RATE_LIMIT_BACKOFF_SECONDS = 8.0

# 全局限速闸门：所有调用先过这道闸——串行 + 强制最小间隔，把瞬时爆发摊平成
# 细水长流，从源头避免触发中转站的每分钟限流。锁懒初始化（import 期没有事件循环）。
_gate: asyncio.Lock | None = None
_last_call_ts: float = 0.0


def _is_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    return status == 429 or "RateLimit" in type(exc).__name__


async def _throttle() -> None:
    """串行排队 + 强制最小调用间隔（config.LLM_MIN_INTERVAL_SECONDS）。"""
    global _gate, _last_call_ts
    if _gate is None:
        _gate = asyncio.Lock()
    async with _gate:
        now = asyncio.get_event_loop().time()
        wait = config.LLM_MIN_INTERVAL_SECONDS - (now - _last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_ts = asyncio.get_event_loop().time()


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


def _extract_content(resp) -> tuple[str, str]:
    """从补全结果里robustly抠出正文，返回 (正文, finish_reason)。

    兼容「思考型」模型：正文为空时，回退去读 reasoning_content / reasoning 字段
    （有的中转站把内容放那儿）。finish_reason 一并返回，方便日志判断为什么空：
    length=token不够、content_filter=被安全拦、stop但空=模型没给。
    """
    choice = resp.choices[0] if resp.choices else None
    if choice is None:
        return "", "no_choice"
    msg = choice.message
    finish = getattr(choice, "finish_reason", None) or "?"
    text = (getattr(msg, "content", None) or "").strip()
    if not text:
        extra = getattr(msg, "model_extra", None) or {}
        for key in ("reasoning_content", "reasoning"):
            alt = getattr(msg, key, None) or extra.get(key)
            if isinstance(alt, str) and alt.strip():
                text = alt.strip()
                break
    return text, finish


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
    # 给正文留足空间：思考型模型会先吃掉一堆 token，max_tokens 太小会返回空内容。
    effective_max = max(max_tokens, config.LLM_MIN_OUTPUT_TOKENS)
    last_exc: Exception | None = None
    for attempt in range(RETRIES + 1):
        await _throttle()  # 先过全局限速闸，避免和其他 NPC 调用挤在一起触发限流
        hit_rate_limit = False
        try:
            # 用 asyncio.wait_for 再套一层硬时限：思考型模型 + 慢中转站常常不认 SDK 的
            # timeout、能拖很久，这里强制每次调用不超过 LLM_TIMEOUT_SECONDS，超了当失败重试。
            resp = await asyncio.wait_for(
                _get_client().chat.completions.create(
                    model=config.MODEL_NAME,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=config.NPC_TEMPERATURE if temperature is None else temperature,
                    max_tokens=effective_max,
                ),
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
            content, finish = _extract_content(resp)
            if content:
                return content
            # 空回复也当作一次失败：带上 finish_reason，方便看出到底为什么空。
            log.warning("LLM 中转站返回空内容（第 %d/%d 次, finish_reason=%s）",
                        attempt + 1, RETRIES + 1, finish)
        except Exception as exc:  # 网络 / 额度 / 模型名错误等
            last_exc = exc
            hit_rate_limit = _is_rate_limit(exc)
            log.warning("LLM 中转站调用失败（第 %d/%d 次）: %s", attempt + 1, RETRIES + 1, exc)
        if attempt < RETRIES:
            # 限流退避更久；其他错误用渐增退避。
            backoff = (RATE_LIMIT_BACKOFF_SECONDS if hit_rate_limit
                       else RETRY_BACKOFF_SECONDS * (attempt + 1))
            await asyncio.sleep(backoff)
    # 重试用尽：用 error 级别 + 人话原因，让日志一眼能看出是中转站配置/额度问题。
    if last_exc is not None:
        log.error("❌ LLM 中转站重试 %d 次仍失败 → %s（NPC 本次将沉默）",
                  RETRIES + 1, explain_error(last_exc))
    else:
        log.error("❌ LLM 中转站连续 %d 次返回空内容，放弃本次调用（NPC 本次将沉默）",
                  RETRIES + 1)
    return ""
