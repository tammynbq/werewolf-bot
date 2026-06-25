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
NPC_TEMPERATURE: float = _float("WEREWOLF_NPC_TEMPERATURE", 0.9)


def validate() -> list[str]:
    """返回缺失的关键配置项列表（空列表表示 OK）。"""
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    return missing
