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
# 板子预设：auto（按人数自适应，默认）/ simple / hunter / guard / classic。
# 见 game/roles.py 的 _PRESETS；12 人推荐 classic（预女猎守）。
BOARD: str = os.getenv("WEREWOLF_BOARD", "auto").strip().lower()
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
# 单次输出 token 下限：有些「思考型」模型会先花一段 token 思考，额度太小会正文没开始就被
# 截断(finish_reason=length)返回空；额度太大又会让它过度思考、半天不出话。1500 取个平衡：
# 够它想完还能答，又不至于钻牛角尖。配合硬时限兜底，慢/卡也不会拖死。可用环境变量调。
LLM_MIN_OUTPUT_TOKENS: int = _int("WEREWOLF_LLM_MIN_OUTPUT_TOKENS", 1500)
# NPC 发言（LLM）单轮硬时限（秒）：超过就当这名 NPC 本轮沉默、游戏继续，杜绝「发言中」卡死。
NPC_THINK_SECONDS: int = _int("WEREWOLF_NPC_THINK_SECONDS", 180)


def validate() -> list[str]:
    """返回缺失的关键配置项列表（空列表表示 OK）。"""
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    return missing
