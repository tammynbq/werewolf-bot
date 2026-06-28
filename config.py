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


def _parse_profiles(raw: str) -> list[dict]:
    """解析多站登记表 LLM_PROFILES：每行一个站 `站名 | base_url | api_key | model`。
    `#` 开头或空行忽略；字段不全的行跳过。供 /werewolf api 一句话切站用。"""
    profiles: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4 and all(parts[:4]):
            profiles.append({"name": parts[0], "base_url": parts[1],
                             "api_key": parts[2], "model": parts[3]})
    return profiles


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for tok in (raw or "").replace(",", " ").split():
        try:
            ids.add(int(tok))
        except ValueError:
            pass
    return ids


# 多站登记表：一次性把几个站登记进来，之后 /werewolf api 一句话切换，不用重新部署。
# 没配则退化为只用上面的单组 OPENAI_BASE_URL/KEY/MODEL_NAME。
LLM_PROFILES: list[dict] = _parse_profiles(os.getenv("LLM_PROFILES", ""))
# 允许使用 /werewolf api 切站的 Discord 用户 ID 白名单（逗号/空格分隔）。
# 留空 = 不限制（任何人可切）；填了就只有名单里的人能切。
LLM_ADMIN_IDS: set[int] = _parse_ids(os.getenv("LLM_ADMIN_IDS", ""))


def _parse_lover_bindings(raw: str) -> dict[str, int]:
    """解析角色↔恋人绑定 LOVER_BINDINGS：`角色名:DiscordID, 角色名:DiscordID`。
    同一个 DiscordID 可被多个角色绑定（几个角色都把同一玩家当恋人）。"""
    m: dict[str, int] = {}
    for tok in (raw or "").replace("，", ",").replace("：", ":").split(","):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        name, _, idstr = tok.partition(":")
        name, idstr = name.strip(), idstr.strip()
        if name:
            try:
                m[name] = int(idstr)
            except ValueError:
                pass
    return m


# 角色 NPC ↔ 恋人 Discord 用户 ID 的绑定。被绑的角色一旦在局里看到这个真人，
# 就把他/她当恋人：投票不投、当狼不刀、发言暗中维护。比靠说话习惯认人可靠得多。
LOVER_BINDINGS: dict[str, int] = _parse_lover_bindings(os.getenv("LOVER_BINDINGS", ""))

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
    # 有多站登记表，或有单组 key，二者其一即可
    if not OPENAI_API_KEY and not LLM_PROFILES:
        missing.append("OPENAI_API_KEY 或 LLM_PROFILES")
    return missing
