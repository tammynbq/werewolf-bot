"""AI NPC：补位凑人数、规则化夜晚/投票决策、LLM 生成白天发言。

设计：
- 决策（狼刀、查验、投票）走简单规则，省 token、行为可预测。
- 发言（白天讨论）走 LLM 中转站生成，让 NPC 像真人。
"""
from __future__ import annotations

import random
import re

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


def _alive_roster(state: GameState) -> str:
    """带座位号的存活玩家名单，方便 NPC 用『几号』互相称呼。"""
    return "、".join(f"{p.seat}号{p.name}" for p in state.alive_players)


# 兜底发言：LLM 不可用 / 返回垃圾时用，保证不刷乱码
_FALLBACK_LINES = [
    "我先听听大家怎么说，目前没有特别怀疑的对象。",
    "昨晚信息不太够，我先稳一手，别急着冲。",
    "我是好人，希望预言家能给点信息带带我们。",
    "刚才有人发言有点飘，我先记着，投票再看。",
    "我觉得别急着起票，再多听两轮发言。",
]

# 模型偶尔会漏出提示词/模板痕迹，这些开头的行直接丢弃
_META_PREFIXES = (
    "context", "system", "assistant", "user", "prompt", "night", "day",
    "role", "你的真实身份", "当前是第", "存活玩家", "最近的场上", "现在轮到",
)


def _clean_speech(text: str, player: Player) -> str:
    """清洗 LLM 输出：去掉 markdown、自报名字前缀、漏出来的模板痕迹。"""
    if not text:
        return ""
    text = text.replace("**", "").replace("```", "").replace("`", "")
    # 逐行过滤掉像提示词/模板的行
    good: list[str] = []
    for line in text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        probe = ln.lower().lstrip("/*#-—·:：。. ")
        if any(probe.startswith(p) for p in _META_PREFIXES):
            continue
        good.append(ln)
    cleaned = " ".join(good).strip()
    # 去掉模型自报的「N号名字：」「名字：」前缀
    cleaned = re.sub(r"^\s*\d*\s*号?\s*[·.、]?\s*" + re.escape(player.name) + r"\s*[:：]\s*", "", cleaned)
    cleaned = re.sub(r"^\s*\d+\s*号\s*[:：]\s*", "", cleaned)
    cleaned = re.sub(r"^\s*🤖?\s*" + re.escape(player.name) + r"\s*[:：]\s*", "", cleaned)
    # 去引号 / 去掉开头残留的标点 / 收尾空白
    cleaned = cleaned.strip().strip('"“”\'‘’').strip()
    cleaned = cleaned.lstrip("：:，,。.、/ ").strip()
    return cleaned


async def speak(player: Player, state: GameState, recent_log: list[str]) -> str:
    """让 NPC 生成一句白天发言。失败/垃圾时返回规则兜底发言。"""
    secret = _role_brief(player, state)
    log_text = "\n".join(recent_log[-12:]) if recent_log else "（第一天，暂无信息）"

    system = (
        "你是一名正在玩中文桌游《狼人杀》的真人玩家，绝对不是 AI，也绝不承认自己是 AI 或跳出游戏。"
        "场上身份有狼人、预言家、平民：好人靠发言和投票找出并放逐狼人；狼人要隐藏身份、误导好人、保护狼队友。"
        f"你是【{player.seat}号·{player.name}】，性格是「{player.persona}」，发言要带出这种性格。\n"
        "发言要求：\n"
        "1. 像真人在群里聊天，口语自然，可以带点情绪、口头禅或调侃。\n"
        "2. 要结合本局信息针对具体的人表态，用『几号』称呼别人，比如『我觉得3号有点可疑』『同意5号』。\n"
        "3. 只说 1~3 句，20~70 字，别写小作文。\n"
        "4. 只输出你这句话本身：不要加引号、不要写自己的名字或『X号：』开头、不要用 markdown、不要写括号说明、不要解释你在做什么。"
    )
    user = (
        f"【只有你知道的秘密】{secret}\n\n"
        f"现在是第 {state.day_count} 天白天，大家按座位号轮流发言。\n"
        f"本局存活玩家：{_alive_roster(state)}。\n"
        f"目前为止的场上发言与信息：\n{log_text}\n\n"
        f"轮到你（{player.seat}号·{player.name}）了，直接说你这一句发言："
    )

    raw = await llm.chat(system, user)
    cleaned = _clean_speech(raw, player)
    # 防止模型只是复读某个玩家的名字（不是真正发言）
    if cleaned and any(cleaned in p.name for p in state.players if len(p.name) >= 3):
        cleaned = ""
    # 太短或清洗后为空，多半是模型抽风，走兜底
    if len(cleaned) >= 4:
        return cleaned
    return random.choice(_FALLBACK_LINES)


_LAST_WORD_FALLBACK = [
    "好人加油，把狼揪出来，别让我白死！",
    "我走了，剩下的就靠你们了……",
    "记住这轮的票型，别再投错好人。",
    "唉，没想到这么快下去，盘好局势！",
]


async def last_word(player: Player, state: GameState) -> str:
    """出局的 NPC 留一句遗言。"""
    secret = _role_brief(player, state)
    system = (
        "你正在玩中文《狼人杀》，扮演一名刚刚出局的玩家，现在留一句简短遗言，绝不承认自己是 AI。"
        f"你是【{player.seat}号·{player.name}】，性格「{player.persona}」。"
        "遗言要贴合身份与性格：好人可以喊话、提醒站边、给信息；狼人可以继续伪装或卖好人。"
        "只说 1~2 句、15~50 字，口语化；不要加引号、不要写名字前缀、不要用 markdown。"
    )
    user = (
        f"【只有你知道的秘密】{secret}\n"
        f"存活玩家：{_alive_roster(state)}。\n"
        f"你（{player.seat}号·{player.name}）刚出局了，留一句遗言："
    )
    raw = await llm.chat(system, user, max_tokens=120)
    cleaned = _clean_speech(raw, player)
    if len(cleaned) >= 3:
        return cleaned
    return random.choice(_LAST_WORD_FALLBACK)
