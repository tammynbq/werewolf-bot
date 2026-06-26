"""AI NPC：补位凑人数 + 全程 LLM 驱动的「真 AI 大脑」。

设计（v2，真 AI 化）：
- 决策与发言都走 LLM：刀谁 / 验谁 / 救毒 / 投谁 / 怎么说，都让模型推理后产出，
  并尽量让「发言」和「真实决策」一致，消除言行不一的人机感。
- 每个 NPC 有一本「私人笔记」(_MEMORY)：跨回合记住自己的怀疑/信任/盘算，越玩越像人。
- 狼队会配合真人：最终刀谁听真人狼队长的（在 bot.py 里结算）；NPC 狼的提议也走 LLM。
- 任何 LLM 失败 / 解析不出来时，自动回退到稳妥的规则，保证一局绝不卡死。
"""
from __future__ import annotations

import json
import random
import re

import llm
from characters import CHARACTER_NPCS
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


# ============================================================
# 私人记忆：每个 NPC 跨回合的盘算（uid -> 笔记文本）
# ============================================================
_MEMORY: dict[int, str] = {}
# 狼队当晚的统一刀法缓存：(id(state), day_count) -> uid，避免多只 NPC 狼各刀各的
_WOLF_PLAN: dict[tuple, int | None] = {}


def _notes(uid: int) -> str:
    return _MEMORY.get(uid, "")


def _set_notes(uid: int, text: str | None) -> None:
    if text and isinstance(text, str):
        _MEMORY[uid] = text.strip()[:600]


def make_npcs(count: int, existing_names: set[str]) -> list[Player]:
    """生成 count 个补位 NPC，名字不与现有玩家重复。开新局时顺手清空记忆缓存。

    入座顺序：先放 characters.py 里登记的「角色 NPC」（有完整人设，如 Theo），
    名字被真人占用的跳过；不够再用通用性格 NPC 补满。负数 uid，避免与 Discord 冲突。
    """
    _MEMORY.clear()
    _WOLF_PLAN.clear()
    npcs: list[Player] = []
    used_names = set(existing_names)
    next_uid = -1

    # 1) 优先放有完整人设的角色 NPC
    for ch in CHARACTER_NPCS:
        if len(npcs) >= count:
            break
        if ch.name in used_names:
            continue  # 名字被真人占了，让给真人
        npcs.append(Player(uid=next_uid, name=ch.name, is_npc=True, persona=ch.persona))
        used_names.add(ch.name)
        next_uid -= 1

    # 2) 不够的用通用性格 NPC 补满
    pool = [n for n in _NPC_NAMES if n not in used_names]
    random.shuffle(pool)
    while len(npcs) < count:
        name = pool.pop() if pool else f"NPC{len(npcs) + 1}"
        if name in used_names:
            continue
        npcs.append(
            Player(
                uid=next_uid,
                name=name,
                is_npc=True,
                persona=random.choice(_PERSONAS),
            )
        )
        used_names.add(name)
        next_uid -= 1

    return npcs


def _persona_clause(player: Player) -> str:
    """把人设渲染进 system 提示：通用 NPC 是一句性格，角色 NPC 是整段人物设定，
    统一成「你的人设：…」一个块，措辞与人称对齐（全程用「你」称呼这名玩家）。"""
    text = (player.persona or "").strip() or "普通玩家，性格随和、就事论事。"
    return f"你的人设：{text}"


# ============================================================
# LLM 结构化输出工具
# ============================================================
def _extract_json(raw: str) -> dict:
    """从模型回复里抠出第一段 JSON 对象；抠不到返回 {}。"""
    if not raw:
        return {}
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    blob = raw[start:end + 1]
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else {}
    except Exception:
        # 容错：去掉常见的尾逗号再试一次
        try:
            fixed = re.sub(r",\s*([}\]])", r"\1", blob)
            data = json.loads(fixed)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


async def _ask_json(system: str, user: str, *, max_tokens: int = 160,
                    temperature: float | None = None) -> dict:
    raw = await llm.chat(system, user, max_tokens=max_tokens, temperature=temperature)
    return _extract_json(raw)


def _coerce_seat(value, valid_seats: dict[int, int]) -> int | None:
    """把模型给的 target（可能是 int / "3" / "3号" / "投3号"）解析成合法座位号。"""
    if value is None:
        return None
    for m in re.findall(r"\d+", str(value)):
        s = int(m)
        if s in valid_seats:
            return s
    return None


def _parse_seat(raw: str, valid_seats: dict[int, int]) -> int | None:
    """从任意文本里抠出一个合法座位号，抠不到返回 None。"""
    if not raw:
        return None
    for m in re.findall(r"\d+", raw):
        seat = int(m)
        if seat in valid_seats:
            return seat
    return None


# ============================================================
# 规则兜底（LLM 不可用时用，保证流程推进）
# ============================================================
def _rule_wolf_kill(state: GameState) -> int | None:
    candidates = state.alive_villagers()
    if not candidates:
        return None
    return random.choice(candidates).uid


def _rule_seer_check(seer: Player, state: GameState) -> int | None:
    candidates = [
        p for p in state.alive_players
        if p.uid != seer.uid and p.uid not in seer.seer_results
    ]
    if not candidates:
        candidates = [p for p in state.alive_players if p.uid != seer.uid]
    if not candidates:
        return None
    return random.choice(candidates).uid


def _rule_vote(voter: Player, state: GameState) -> int | None:
    alive_others = [p for p in state.alive_players if p.uid != voter.uid]
    if not alive_others:
        return None
    if voter.role is Role.WEREWOLF:
        good = [p for p in alive_others if p.role and not p.role.is_wolf]
        return random.choice(good or alive_others).uid
    if voter.role is Role.SEER:
        known = [p for p in alive_others if voter.seer_results.get(p.uid) is True]
        if known:
            return random.choice(known).uid
    return random.choice(alive_others).uid


# ============================================================
# 私密信息 / 名单
# ============================================================
def _role_brief(player: Player, state: GameState) -> str:
    """构造只给该 NPC 看的私密信息（身份、队友、查验结果、药剂）。"""
    assert player.role is not None
    lines = [f"你的真实身份是【{player.role.cn}】。"]
    if player.role is Role.WEREWOLF:
        mates = [
            f"{p.seat}号" for p in state.players
            if p.role and p.role.is_wolf and p.uid != player.uid
        ]
        if mates:
            lines.append(f"你的狼队友是：{', '.join(mates)}。务必隐藏身份，别暴露队友。")
        else:
            lines.append("你是独狼，要伪装成好人。")
    elif player.role is Role.SEER:
        lines.append(_seer_checked_text(player, state))
    elif player.role is Role.WITCH:
        potions = []
        potions.append("解药" + ("还在" if player.has_heal else "已用"))
        potions.append("毒药" + ("还在" if player.has_poison else "已用"))
        lines.append("你是好人阵营，" + "、".join(potions) + "。注意别暴露女巫身份被狼针对。")
    return " ".join(lines)


def _seer_checked_text(seer: Player, state: GameState) -> str:
    if seer.seer_results:
        checked = []
        for uid, is_wolf in seer.seer_results.items():
            target = state.get(uid)
            if target:
                checked.append(f"{target.seat}号是{'狼人' if is_wolf else '好人'}")
        return "你的查验结果：" + "；".join(checked) + "。"
    return "你还没有查验结果。"


def _alive_roster(state: GameState) -> str:
    """存活玩家名单（只用座位号，全程匿名）。"""
    return "、".join(f"{p.seat}号" for p in state.alive_players)


# ============================================================
# 通用「选一个目标」决策（刀 / 验 / 投 / 毒 共用）
# ============================================================
async def _decide_target(
    player: Player, state: GameState, *, role_intro: str, task: str,
    valid_seats: dict[int, int], extra: str = "", temperature: float = 0.6,
) -> int | None:
    """让 LLM 推理后选一个座位号；解析失败返回 None（调用方走规则兜底）。"""
    if not valid_seats:
        return None
    seat_list = "、".join(f"{s}号" for s in sorted(valid_seats))
    system = (
        f"你正在玩中文《狼人杀》。{role_intro} "
        f"你是【{player.seat}号】。{_persona_clause(player)}"
        f"你的私人笔记：{_notes(player.uid) or '（暂无）'}。"
        "请像有脑子的老玩家一样认真推理后再决定，别乱选。"
        '只输出 JSON：{"target": <座位号数字>, "reason": "<10字内理由>"}，不要任何多余内容。'
    )
    user = f"{task}\n可选目标：{seat_list}。{extra}\n输出 JSON："
    data = await _ask_json(system, user, max_tokens=120, temperature=temperature)
    seat = _coerce_seat(data.get("target"), valid_seats)
    if seat is None:
        seat = _parse_seat(json.dumps(data, ensure_ascii=False), valid_seats)
    return valid_seats.get(seat) if seat else None


# ============================================================
# 夜晚 · 预言家查验（LLM 选最有价值的目标）
# ============================================================
async def seer_check_target(seer: Player, state: GameState) -> int | None:
    candidates = [
        p for p in state.alive_players
        if p.uid != seer.uid and p.uid not in seer.seer_results
    ]
    if not candidates:
        candidates = [p for p in state.alive_players if p.uid != seer.uid]
    if not candidates:
        return None
    valid = {p.seat: p.uid for p in candidates}
    uid = await _decide_target(
        seer, state,
        role_intro="你是预言家（好人），每晚查验一人得知好/狼。要挑最有信息价值的人查："
                   "优先查发言可疑、站边奇怪、或还没表态的关键位，别查已经基本确定的人。",
        task="今晚你要查验谁？",
        valid_seats=valid,
        extra=f"已查过的结果：{_seer_checked_text(seer, state)}",
        temperature=0.4,
    )
    return uid if uid is not None else _rule_seer_check(seer, state)


# ============================================================
# 夜晚 · 狼刀（LLM 选战略目标；全队当晚共用一个刀法）
# ============================================================
async def wolf_kill_target(state: GameState) -> int | None:
    key = (id(state), state.day_count)
    if key in _WOLF_PLAN:
        return _WOLF_PLAN[key]

    targets = [p for p in state.alive_players if not (p.role and p.role.is_wolf)]
    if not targets:
        _WOLF_PLAN[key] = None
        return None
    valid = {p.seat: p.uid for p in targets}

    thinker = next((w for w in state.alive_wolves() if w.is_npc), None)
    if thinker is None:
        uid = _rule_wolf_kill(state)  # 没有 NPC 狼时不值得花 token
    else:
        mates = "、".join(f"{w.seat}号" for w in state.alive_wolves())
        uid = await _decide_target(
            thinker, state,
            role_intro=f"你是狼人，狼队是[{mates}]，正在商量今晚刀谁。"
                       "要刀掉对好人最有用的人：跳出来的预言家、疑似女巫、或带节奏的强好人，"
                       "别刀自己队友，也别无脑刀边缘人。",
            task="今晚刀谁？",
            valid_seats=valid,
            temperature=0.6,
        )
        if uid is None:
            uid = _rule_wolf_kill(state)
    _WOLF_PLAN[key] = uid
    return uid


# ============================================================
# 夜晚 · 女巫用药（LLM 决定救/毒，前期倾向救人、没把握不毒）
# ============================================================
async def witch_night_action(
    witch: Player, state: GameState, victim_uid: int | None
) -> tuple[bool, int | None]:
    """返回 (是否用解药救, 毒药目标 uid 或 None)。"""
    if not (witch.has_heal or witch.has_poison):
        return False, None

    victim = state.get(victim_uid) if victim_uid else None
    victim_txt = f"{victim.seat}号" if victim else "今晚没人被刀（平安夜）"
    alive_others = [p for p in state.alive_players if p.uid != witch.uid]
    valid = {p.seat: p.uid for p in alive_others}
    poison_list = "、".join(f"{s}号" for s in sorted(valid)) or "（无）"

    system = (
        "你正在玩中文《狼人杀》，你是女巫（好人）。你有一瓶解药(救今晚被狼刀的人)和一瓶毒药(毒死任意一人)，各只能用一次。"
        f"当前解药：{'还在' if witch.has_heal else '已用完'}；毒药：{'还在' if witch.has_poison else '已用完'}。"
        f"你是【{witch.seat}号】。{_persona_clause(witch)} 私人笔记：{_notes(witch.uid) or '（暂无）'}。\n"
        "用药原则：\n"
        "- 解药：前期(尤其第一晚)被刀的若可能是好人/神职，倾向于救；但若怀疑是狼自刀骗药可不救。\n"
        "- 毒药：没有较大把握(比如预言家验出的狼、或几乎坐实的狼)就留着别毒，乱毒很可能毒死好人。\n"
        "- 同一晚不要既救又毒。\n"
        '只输出 JSON：{"heal": true或false, "poison": <要毒的座位号；不毒填0>, "reason": "<10字内理由>"}。'
    )
    user = (
        f"现在是第 {state.day_count + 1} 晚。今晚被狼刀的是：{victim_txt}。\n"
        f"存活玩家：{_alive_roster(state)}。\n"
        f"可毒的目标：{poison_list}。\n输出 JSON："
    )
    data = await _ask_json(system, user, max_tokens=140, temperature=0.5)

    heal = False
    poison_uid = None
    if not data:
        # LLM 不可用：稳妥兜底——第一晚有人被刀就救，其余不动毒
        if witch.has_heal and victim and victim.uid != witch.uid and state.day_count == 0:
            heal = True
        return heal, None

    if witch.has_heal and victim is not None and victim.uid != witch.uid:
        heal = bool(data.get("heal"))
    if not heal and witch.has_poison:
        seat = _coerce_seat(data.get("poison"), valid)
        if seat:
            poison_uid = valid[seat]
    return heal, poison_uid


# ============================================================
# 白天 · 投票（所有身份都走 LLM，和当天发言保持一致）
# ============================================================
async def vote_decision(voter: Player, state: GameState, recent_log: list[str]) -> int | None:
    alive_others = [p for p in state.alive_players if p.uid != voter.uid]
    if not alive_others:
        return None
    valid = {p.seat: p.uid for p in alive_others}

    # 预言家若已验出存活的狼，直接投他（确定性最高，省一次调用）
    if voter.role is Role.SEER:
        known = [p for p in alive_others if voter.seer_results.get(p.uid) is True]
        if known:
            return random.choice(known).uid

    log_text = "\n".join(recent_log[-20:]) if recent_log else "（今天还没什么有效发言）"
    if voter.role is Role.WEREWOLF:
        intro = ("你是狼人，投票环节要把水搅浑：把一个好人推上去，别投自己狼队友。"
                 "如果有真预言家报验了你的狼队友，别傻乎乎跟着投队友——要么装糊涂投别人、"
                 "要么反咬预言家是假的，尽量保队友。")
    else:
        intro = (
            "你是好人阵营，目标是投出狼。最重要的一条：\n"
            "如果有人跳预言家并报验了某人是狼（类似『我是预言家，我验了X号是狼』），"
            "而且没有别人对跳预言家、也没有更可信的反驳，就【果断跟票】把那个被验出的狼投出去"
            "——这是好人抓狼最硬的信息，别犹豫、别另投他人。\n"
            "如果有两个人都跳预言家（对跳），再结合双方发言逻辑判断谁是真预言家，投他指认的狼。\n"
            "如果没人报验，才结合发言逻辑、前后矛盾、甩锅与站边，投最像狼的人。别乱投好人。")

    uid = await _decide_target(
        voter, state,
        role_intro=intro,
        task="现在投票放逐，你投谁？",
        valid_seats=valid,
        extra=f"今天的发言记录：\n{log_text}",
        temperature=0.5,
    )
    return uid if uid is not None else _rule_vote(voter, state)


# ============================================================
# 发言清洗
# ============================================================
_META_PREFIXES = (
    "context", "system", "assistant", "user", "prompt", "night", "day",
    "role", "json", "reason", "think", "你的真实身份", "当前是第", "存活玩家",
    "最近的场上", "现在轮到",
)


def _clean_speech(text: str, player: Player) -> str:
    """清洗 LLM 输出：去掉 markdown、自报名字前缀、漏出来的模板痕迹。"""
    if not text:
        return ""
    text = text.replace("**", "").replace("```", "").replace("`", "")
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
    cleaned = re.sub(r"^\s*\d*\s*号?\s*[·.、]?\s*" + re.escape(player.name) + r"\s*[:：]\s*", "", cleaned)
    cleaned = re.sub(r"^\s*\d+\s*号\s*[:：]\s*", "", cleaned)
    cleaned = re.sub(r"^\s*🤖?\s*" + re.escape(player.name) + r"\s*[:：]\s*", "", cleaned)
    cleaned = cleaned.strip().strip('"“”\'‘’').strip()
    cleaned = cleaned.lstrip("：:，,。.、/ ").strip()
    return cleaned


# ============================================================
# 白天 · 发言（决策式发言：先盘算再开口，并更新私人笔记）
# ============================================================
def _speak_strategy(player: Player) -> str:
    if player.role is Role.WEREWOLF:
        return ("你要伪装成好人：适度怀疑别人、保护狼队友、必要时悍跳或带歪节奏，但别太刻意露馅。")
    if player.role is Role.SEER:
        return ("你是预言家：如果查到狼或局势需要，可以跳出来报验带队(说清验了谁、是好是狼)；"
                "也可以视情况先隐藏。要让好人跟上你的信息。")
    if player.role is Role.WITCH:
        return ("你是女巫：低调找狼，别轻易暴露身份(暴露会被狼针对)，但可以引导投票。"
                "若有可信的预言家报验了狼，就声援他、号召大家投那个狼。")
    return ("你是平民：靠逻辑找狼，多分析别人的发言和票型，推动好人抓狼。"
            "若有人跳预言家报验了某人是狼且没人对跳，就明确表态跟他、号召一起投那个狼。")


async def speak(player: Player, state: GameState, recent_log: list[str]) -> str:
    """让 NPC 先盘算再发言：输出 JSON {say, notes}，发言与思路一致，并更新私人笔记。"""
    secret = _role_brief(player, state)
    log_text = "\n".join(recent_log[-14:]) if recent_log else "（第一天，暂无信息）"

    system = (
        "你是一名正在玩中文桌游《狼人杀》的真人玩家，绝对不是 AI，也绝不承认自己是 AI 或跳出游戏。"
        f"你是【{player.seat}号】。{_persona_clause(player)} 你的发言要带出这个人设的语气和风格。"
        f"{_speak_strategy(player)}\n"
        "要求：\n"
        "1. 像真人在群里聊天，口语自然，有情绪、有口头禅，针对具体的人用『几号』称呼(如『我觉得3号有点跳』)。\n"
        "2. 结合你的私人笔记和场上信息，发言要有逻辑、有立场、能推动局势，别说正确的废话。\n"
        "3. say 只 1~3 句、20~70 字。\n"
        '只输出 JSON：{"say": "<你这一句发言>", "notes": "<更新后的私人笔记：你怀疑谁/信任谁/盘算，30字内>"}。'
    )
    user = (
        f"【只有你知道的秘密】{secret}\n"
        f"你的私人笔记：{_notes(player.uid) or '（暂无）'}\n"
        f"现在是第 {state.day_count} 天白天，大家按座位号轮流发言。\n"
        f"本局存活玩家：{_alive_roster(state)}。\n"
        f"目前为止的场上发言与信息：\n{log_text}\n\n"
        f"轮到你（{player.seat}号）了，先想再说，输出 JSON："
    )

    data = await _ask_json(system, user, max_tokens=240, temperature=0.9)
    if data:
        _set_notes(player.uid, data.get("notes"))
        say = _clean_speech(str(data.get("say") or ""), player)
        if say and any(say in p.name for p in state.players if len(p.name) >= 3):
            say = ""
        if len(say) >= 4:
            return say

    # JSON 没解析出来时，退回到「直接要一句话」的老办法（llm.chat 内部已自带重试）。
    raw = await llm.chat(system.split("只输出 JSON")[0], user.replace("输出 JSON：", "直接说你这一句发言："))
    say = _clean_speech(raw, player)
    if len(say) >= 4:
        return say
    # 中转站彻底不可用：返回空串，由 bot.py 标记这名 NPC 本轮沉默，
    # 不再用写死的台词凑数（避免「每次台词都像抽取的」）。
    return ""


# ============================================================
# 夜晚 · 狼人私聊（在狼人频道商量，提议和当晚刀法一致）
# ============================================================
async def wolf_chat(player: Player, mates: list[Player], state: GameState) -> str:
    """NPC 狼人在狼人私密频道里和队友商量一句（只有狼能看到）。"""
    mate_txt = "、".join(f"{m.seat}号" for m in mates) if mates else "（暂无）"
    good_targets = "、".join(
        f"{p.seat}号" for p in state.alive_players if not (p.role and p.role.is_wolf)
    )
    # 让 NPC 的提议和今晚的统一刀法对齐，显得真在配合
    plan = await wolf_kill_target(state)
    plan_txt = f"{state.get(plan).seat}号" if plan and state.get(plan) else "还没定"
    system = (
        "你正在玩中文《狼人杀》，你是狼人，现在在只有狼队友能看到的私密狼人频道里商量今晚刀谁。"
        f"你是【{player.seat}号】。{_persona_clause(player)}"
        "像真人在狼队小群里聊天：简短、直接、商量口吻，可以提议刀某个具体的人、问队友意见或附和队友。"
        "只说 1~2 句、15~45 字；不要加引号、不要写名字前缀、不要 markdown、不要输出 JSON。"
    )
    user = (
        f"你的狼队友：{mate_txt}。\n"
        f"可以刀的好人：{good_targets}。\n"
        f"你心里倾向今晚刀：{plan_txt}（可以据此和队友商量，也可被说服改变）。\n"
        "说一句你和队友商量今晚刀谁的话："
    )
    raw = await llm.chat(system, user, max_tokens=100, temperature=0.85)
    cleaned = _clean_speech(raw, player)
    if len(cleaned) >= 4:
        return cleaned
    return ""  # 中转站不可用：本轮不发狼聊（由 bot.py 跳过），不用写死台词


# ============================================================
# 出局遗言
# ============================================================
async def last_word(player: Player, state: GameState) -> str:
    """出局的 NPC 留一句遗言。"""
    secret = _role_brief(player, state)
    system = (
        "你正在玩中文《狼人杀》，扮演一名刚刚出局的玩家，现在留一句简短遗言，绝不承认自己是 AI。"
        f"你是【{player.seat}号】。{_persona_clause(player)} 私人笔记：{_notes(player.uid) or '（暂无）'}。"
        "遗言要贴合身份与性格：好人可以喊话、提醒站边、给信息(预言家可以报验)；狼人可以继续伪装或卖好人。"
        "只说 1~2 句、15~50 字，口语化；不要加引号、不要写名字前缀、不要用 markdown、不要输出 JSON。"
    )
    user = (
        f"【只有你知道的秘密】{secret}\n"
        f"存活玩家：{_alive_roster(state)}。\n"
        f"你（{player.seat}号）刚出局了，留一句遗言："
    )
    raw = await llm.chat(system, user, max_tokens=120, temperature=0.85)
    cleaned = _clean_speech(raw, player)
    if len(cleaned) >= 3:
        return cleaned
    return ""  # 中转站不可用：返回空串，由 bot.py 显示「没有留下遗言」，不用写死台词
