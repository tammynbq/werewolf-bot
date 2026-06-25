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
REVEAL_SECONDS: int = _int("WEREWOLF_REVEAL_SECONDS", 20)
NPC_TEMPERATURE: float = _float("WEREWOLF_NPC_TEMPERATURE", 0.9)
# LLM 单次调用超时（秒）：超时即走兜底，避免中转站卡死拖住整局。
LLM_TIMEOUT_SECONDS: float = _float("WEREWOLF_LLM_TIMEOUT", 30.0)


def validate() -> list[str]:
    """返回缺失的关键配置项列表（空列表表示 OK）。"""
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    return missing
