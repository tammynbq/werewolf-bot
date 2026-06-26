"""集中读取环境变量配置。"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ===== Discord =====
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")

# ===== LLM 中转站（OpenAI 兼容）=====
# 变量名与 bq-bot 对齐，方便在 Railway 等环境复用同一套配置。
OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
MODEL_NAME: str = os.getenv("MODEL_NAME", "claude-opus-4-8")

# ===== 游戏参数 =====
TOTAL_PLAYERS: int = _int("WEREWOLF_TOTAL_PLAYERS", 6)
TURN_SECONDS: int = _int("WEREWOLF_TURN_SECONDS", 60)
# 真人打字发言/遗言的时限：给慢手留足时间（NPC 不受影响、立即生成，不会卡住）。
SPEAK_SECONDS: int = _int("WEREWOLF_SPEAK_SECONDS", 300)
# 投票时限：太短的话视图会超时变"死"，点了就『交互失败』。给足思考时间。
VOTE_SECONDS: int = _int("WEREWOLF_VOTE_SECONDS", 180)
REVEAL_SECONDS: int = _int("WEREWOLF_REVEAL_SECONDS", 20)
NPC_TEMPERATURE: float = _float("WEREWOLF_NPC_TEMPERATURE", 0.9)
# LLM 单次调用超时（秒）：超时即走兜底，避免中转站卡死拖住整局。
LLM_TIMEOUT_SECONDS: float = _float("WEREWOLF_LLM_TIMEOUT", 30.0)
# LLM 全局最小调用间隔（秒）：所有 NPC 的调用串行排队、彼此至少隔这么久，
# 把瞬时爆发摊平成细水长流，避免免费中转站的「每分钟限流」(429)。免费站建议调大。
LLM_MIN_INTERVAL_SECONDS: float = _float("WEREWOLF_LLM_MIN_INTERVAL", 2.0)
# 单次输出 token 下限：有些「思考型」模型会先花一堆 token 思考，max_tokens 太小会导致
# 正文还没开始就没额度、返回空内容。给个下限保证正文有空间（思考型模型建议调大到 1024+）。
LLM_MIN_OUTPUT_TOKENS: int = _int("WEREWOLF_LLM_MIN_OUTPUT_TOKENS", 800)


def validate() -> list[str]:
    """返回缺失的关键配置项列表（空列表表示 OK）。"""
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    return missing
