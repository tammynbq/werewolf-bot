"""AI NPC：补位凑人数、规则化夜晚/投票决策、LLM 生成白天发言。

设计：
- 决策（狼刀、查验、投票）走简单规则，省 token、行为可预测。
- 发言（白天讨论）走 LLM 中转站生成，让 NPC 像真人。
"""
from __future__ import annotations

import random

import llm
from game.player import Player
from game.roles import Role
from game.state import GameState

# NPC 名字池（补位时随机取）
_NPC_NAMES = [
    "阿强", "小美", "老王", "丸子", "阿杰", "果冻", "大锤", "青柠",
    "卷卷", "土豆", "可乐", "栗子", "阿楠", "团团", "西瓜", "咸鱼",
]

# NPC 性格池（喂给 LLM 增加发言差异）
_PERSONAS = [
    "说话谨慎、爱分析逻辑，喜欢复盘前一晚的信息",
    "性格急躁、爱怼人，常常直接开火指认别人",
    "幽默风趣、爱开玩笑，但偶尔暴露关键想法",
    "话不多、偏冷静，发言简短但点子准",
    "热情话痨、喜欢带节奏，爱拉票",
    "胆小怕事、容易被带跑，常常附和别人",
    "老练沉稳、像个老玩家，喜欢站边表态",
    "天真直率、想到什么说什么，藏不住情绪",
]


def make_npcs(count: int, existing_names: set[str]) -> list[Player]:
    """生成 count 个补位 NPC，名字不与现有玩家重复。"""
    names = [n for n in _NPC_NAMES if n not in existing_names]
    random.shuffle(names)
    npcs: list[Player] = []
    for i in range(count):
        name = names[i] if i < len(names) else f"NPC{i + 1}"
        npcs.append(
            Player(
                uid=-(i + 1),  # 负数 id，避免与 Discord user id 冲突
                name=name,
                is_npc=True,
                persona=random.choice(_PERSONAS),
            )
        )
    return npcs


# ============ 规则决策 ============

def wolf_kill_target(state: GameState) -> int | None:
    """狼队选刀：随机击杀一名存活好人。"""
    candidates = state.alive_villagers()
    if not candidates:
        return None
    return random.choice(candidates).uid


def seer_check_target(seer: Player, state: GameState) -> int | None:
    """预言家查验：随机选一名没查过的存活玩家（排除自己）。"""
    candidates = [
        p for p in state.alive_players
        if p.uid != seer.uid and p.uid not in seer.seer_results
    ]
    if not candidates:
        candidates = [p for p in state.alive_players if p.uid != seer.uid]
    if not candidates:
        return None
    return random.choice(candidates).uid


def vote_target(voter: Player, state: GameState) -> int | None:
    """投票决策：
    - 狼人：投一名存活好人（不投队友）。
    - 预言家：若查到存活的狼，投他；否则随机。
    - 平民：随机投一名其他存活玩家。
    """
    alive_others = [p for p in state.alive_players if p.uid != voter.uid]
    if not alive_others:
        return None

    if voter.role is Role.WEREWOLF:
        good = [p for p in alive_others if p.role and not p.role.is_wolf]
        pool = good or alive_others
        return random.choice(pool).uid

    if voter.role is Role.SEER:
        known_wolves = [
            p for p in alive_others
            if voter.seer_results.get(p.uid) is True
        ]
        if known_wolves:
            return random.choice(known_wolves).uid

    return random.choice(alive_others).uid


# ============ LLM 发言 ============

def _role_brief(player: Player, state: GameState) -> str:
    """构造只给该 NPC 看的私密信息（身份、队友、查验结果）。"""
    assert player.role is not None
    lines = [f"你的真实身份是【{player.role.cn}】。"]
    if player.role is Role.WEREWOLF:
        mates = [
            p.name for p in state.players
            if p.role and p.role.is_wolf and p.uid != player.uid
        ]
        if mates:
            lines.append(f"你的狼队友是：{', '.join(mates)}。务必隐藏身份，别暴露队友。")
        else:
            lines.append("你是独狼，要伪装成好人。")
    elif player.role is Role.SEER:
        if player.seer_results:
            checked = []
            for uid, is_wolf in player.seer_results.items():
                target = state.get(uid)
                if target:
                    checked.append(f"{target.name}是{'狼人' if is_wolf else '好人'}")
            lines.append("你的查验结果：" + "；".join(checked) + "。可以选择性地报验或隐藏。")
        else:
            lines.append("你还没有查验结果。")
    return " ".join(lines)


def _alive_list(state: GameState) -> str:
    return "、".join(p.name for p in state.alive_players)


async def speak(player: Player, state: GameState, recent_log: list[str]) -> str:
    """让 NPC 生成一句白天发言。失败时返回规则兜底发言。"""
    secret = _role_brief(player, state)
    log_text = "\n".join(recent_log[-12:]) if recent_log else "（暂无）"

    system = (
        "你正在玩中文狼人杀游戏，扮演一名玩家。"
        "规则：场上有狼人、预言家、平民。好人要投票放逐狼人，狼人要隐藏身份。"
        "你必须始终留在角色里，绝不能承认自己是 AI，也不能跳出游戏。"
        f"你的性格设定：{player.persona}。"
        "发言要口语化、像真人，30~80字以内，符合你的身份和性格，不要用 markdown，不要加引号。"
    )
    user = (
        f"{secret}\n"
        f"当前是第 {state.day_count} 天白天讨论。\n"
        f"存活玩家：{_alive_list(state)}。\n"
        f"最近的场上信息：\n{log_text}\n\n"
        f"现在轮到你（{player.name}）发言，说一段话。"
    )

    text = await llm.chat(system, user)
    if text:
        # 去掉可能的引号包裹
        return text.strip().strip('"“”').strip()

    # 兜底：LLM 不可用时给一句通用发言
    return random.choice([
        "我先听听大家怎么说，目前没有特别怀疑的对象。",
        "昨晚的信息不太够，我觉得先稳一手，别急着冲。",
        "我是好人，希望预言家能给点信息带带我们。",
        "感觉刚才发言有人有点飘，我先记一下，投票再看。",
    ])
