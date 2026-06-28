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

import config
import knowledge
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

# 通用发言规则：默认纯中文；只有人设本身是双语/外语背景的角色才可能蹦外语，
# 且一旦说了外语就必须翻译。注意这不是叫每个人都说双语——纯中文人设就老老实实说中文。
_TRANSLATE_RULE = ("默认用纯中文发言。除非你的人设明确就是双语/外语背景，否则不要夹带任何外语；"
                   "若你的人设确实会说外语，那也只是偶尔点缀，并且每句外语后面都要紧跟中文意思，"
                   "别让看中文的玩家看不懂。")
# 反「上帝视角」：模型容易脑补出自己其实不知道的信息，统一钉死——只能用场上公开信息推理。
_NO_OMNISCIENCE = ("你没有上帝视角：除了【只有你知道的秘密】里写明的内容，你并不知道任何其他人的"
                   "真实身份或底牌，也不知道夜里发生的全部真相。只能根据公开的发言、票型和你自己的"
                   "私密信息去推理，绝不能说得像你已经知道谁是狼/神/民——那样会立刻穿帮。")
# 每个 NPC 本轮发言时定下的「想投谁」（uid -> 座位号），让投票跟着发言走、言行一致
_VOTE_INTENT: dict[int, int] = {}


def _notes(uid: int) -> str:
    return _MEMORY.get(uid, "")


def _set_notes(uid: int, text: str | None) -> None:
    if text and isinstance(text, str):
        _MEMORY[uid] = text.strip()[:600]


def _set_vote_intent(uid: int, vote) -> None:
    """记下这名 NPC 发言时表态要投的座位号（0/解析失败=没想好，清掉旧意向）。"""
    seat = 0
    try:
        # 容忍 "5" / "5号" / 5 等写法
        m = re.search(r"\d+", str(vote))
        if m:
            seat = int(m.group())
    except (TypeError, ValueError):
        seat = 0
    if seat > 0:
        _VOTE_INTENT[uid] = seat
    else:
        _VOTE_INTENT.pop(uid, None)


def _vote_intent_uid(voter: Player, state: GameState,
                     exclude: set[int] = frozenset()) -> int | None:
    """把发言时定下的投票意向（座位号）解析成存活、非自己、非排除的目标 uid。"""
    seat = _VOTE_INTENT.get(voter.uid)
    if not seat:
        return None
    target = next((p for p in state.alive_players
                   if p.seat == seat and p.uid != voter.uid and p.uid not in exclude), None)
    return target.uid if target else None


def make_npcs(count: int, existing_names: set[str],
              preferred_names: list[str] | None = None) -> list[Player]:
    """生成 count 个补位 NPC，名字不与现有玩家重复。开新局时顺手清空记忆缓存。

    入座顺序：先放角色 NPC（有完整人设，如 Theo），名字被真人占用的跳过；
    不够再用通用性格 NPC 补满。负数 uid，避免与 Discord 冲突。
    - preferred_names 非空时：只优先放房主指定的这些角色（按指定顺序），其余用通用补满；
    - 为空 / None 时：沿用默认——按 CHARACTER_NPCS 顺序优先放所有角色。
    """
    _MEMORY.clear()
    _WOLF_PLAN.clear()
    _VOTE_INTENT.clear()
    npcs: list[Player] = []
    used_names = set(existing_names)
    next_uid = -1

    by_name = {c.name: c for c in CHARACTER_NPCS}
    if preferred_names:
        char_list = [by_name[n] for n in preferred_names if n in by_name]
    else:
        char_list = list(CHARACTER_NPCS)

    # 1) 优先放（指定的 / 全部）角色 NPC
    for ch in char_list:
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
# 思维链剥离
# ============================================================
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

_COT_OPENERS = (
    "let me", "let's", "i'll ", "i will ", "i should", "i need to",
    "my response", "my reply", "my teammates", "my wolf",
    "okay,", "alright,", "first,", "analysis:", "thinking:",
    "plan:", "strategy:", "considering", "breakdown:",
    "as a wolf", "as the seer", "as the witch", "as the hunter",
    "as a villager", "as player", "since i am", "since i'm",
    "我的队友", "我是狼", "我的狼", "分析一下", "让我想想", "让我分析",
    "我的身份", "作为狼人", "作为预言家", "作为女巫", "作为猎人",
    "目前局势", "存活的", "已经死",
)

_COT_INSTRUCTION = (
    "如果你需要思考，把思考过程放在 <think>...</think> 标签里，标签外只写最终输出。"
    "绝对不要在你的发言里暴露你的真实身份、队友信息、或任何内心分析过程。"
)


def _strip_cot(text: str) -> str:
    """剥离模型泄露的思维链：<think> 标签 + 常见 CoT 开头段落。"""
    text = _THINK_RE.sub("", text).strip()
    if not text:
        return text
    lines = text.splitlines()
    cleaned: list[str] = []
    started = False
    for line in lines:
        if not started:
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if (
                s.startswith(("*", "-", "#", ">", "•"))
                or re.match(r"^\d+\.\s", s)
                or any(low.startswith(op) for op in _COT_OPENERS)
            ):
                continue
            started = True
        cleaned.append(line)
    return "\n".join(cleaned).strip() or text


# ============================================================
# LLM 结构化输出工具
# ============================================================
def _extract_json(raw: str) -> dict:
    """从模型回复里抠出第一段 JSON 对象；抠不到返回 {}。"""
    if not raw:
        return {}
    raw = _strip_cot(raw)
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


def _profile_for(player: Player) -> dict | None:
    """该角色绑定的站（CHARACTER_API：角色名→站名）；没绑返回 None（走默认站）。"""
    return llm.profile_by_name(config.CHARACTER_API.get(player.name))


async def _ask_json(system: str, user: str, *, max_tokens: int = 160,
                    temperature: float | None = None, profile: dict | None = None) -> dict:
    raw = await llm.chat(system, user, max_tokens=max_tokens,
                         temperature=temperature, profile=profile)
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
    # 若某只狼绑定了恋人且恋人在场，硬保护：刀法绕开她。
    protect = set()
    for w in state.alive_wolves():
        lv = config.LOVER_BINDINGS.get(w.name)
        if lv is not None and state.get(lv) is not None:
            protect.add(lv)
    candidates = [p for p in state.alive_villagers() if p.uid not in protect]
    if not candidates:
        candidates = state.alive_villagers()  # 万一只剩恋人可刀，才不护（基本不会）
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
            lines.append(f"你的狼队友是：{', '.join(mates)}。务必隐藏身份，别暴露队友。"
                         "除了狼队友，你并不知道其他人的真实身份（谁是预言家/女巫/平民你都不知道），"
                         "只能靠发言和票型去猜，绝不能表现得像你已经知道。")
        else:
            lines.append("你是独狼，要伪装成好人。你不知道其他任何人的真实身份，只能靠发言和票型猜。")
    elif player.role is Role.SEER:
        lines.append(_seer_checked_text(player, state))
    elif player.role is Role.WITCH:
        potions = []
        potions.append("解药" + ("还在" if player.has_heal else "已用"))
        potions.append("毒药" + ("还在" if player.has_poison else "已用"))
        lines.append("你是好人阵营，" + "、".join(potions) + "。注意别暴露女巫身份被狼针对。")
    elif player.role is Role.GUARD:
        lines.append("你是好人阵营的守卫，每晚守护一人免遭狼刀（不能连守同一人，可守自己）；"
                     "注意『同守同救』会让被守的人照样死。别轻易暴露身份被狼针对。")
    elif player.role is Role.HUNTER:
        lines.append("你是好人阵营的猎人，出局时（被狼刀或被票出）能开枪带走一人，"
                     "但被女巫毒死则不能开枪。可以适时亮身份威慑狼，也可以隐藏。")
    elif player.role is Role.IDIOT:
        extra = "你已经翻牌了，不能再投票，但仍可发言分析帮好人。" if player.idiot_revealed else \
                "你是好人阵营的白痴，被投票放逐时会自动翻牌免死一次（但之后失去投票权）。低调找狼就行，不怕被票。"
        lines.append(extra)
    elif player.role is Role.KNIGHT:
        if player.has_dueled:
            lines.append("你是好人阵营的骑士，但决斗机会已经用过了。靠逻辑找狼吧。")
        else:
            lines.append("你是好人阵营的骑士，白天可以亮牌与一名玩家翻牌决斗："
                         "对方是狼则狼死，对方不是狼则你自己死。一局只能用一次，要看准再用。")
    elif player.role is Role.WOLF_KING:
        mates = [
            f"{p.seat}号" for p in state.players
            if p.role and p.role.is_wolf and p.uid != player.uid
        ]
        if mates:
            lines.append(f"你是白狼王（狼人阵营），狼队友是：{', '.join(mates)}。"
                         "被投票放逐出局时可以带走一名玩家。务必隐藏身份，别暴露队友。")
        else:
            lines.append("你是白狼王（狼人阵营），独狼。被投票放逐出局时可以带走一名玩家。要伪装成好人。")
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
        f"{_COT_INSTRUCTION}"
        '只输出 JSON：{"target": <座位号数字>, "reason": "<10字内理由>"}，不要任何多余内容。'
    )
    user = f"{task}\n可选目标：{seat_list}。{extra}\n输出 JSON："
    data = await _ask_json(system, user, max_tokens=120, temperature=temperature, profile=_profile_for(player))
    seat = _coerce_seat(data.get("target"), valid_seats)
    if seat is None:
        seat = _parse_seat(json.dumps(data, ensure_ascii=False), valid_seats)
    return valid_seats.get(seat) if seat else None


# ============================================================
# 免费的轻量推断（不调 API）：从发言记录 / 私人笔记里抠线索
# ============================================================
def _scan_seer_report(state: GameState, recent_log: list[str]) -> int | None:
    """从近期发言里找『预言家报验某座位是狼』的指认，返回该存活玩家 uid。

    纯文本启发式（免费）：命中类似「3号是狼 / 3号查杀 / 验出3号是狼」的句子。
    给好人 NPC 投票时跟票用，让『预言家报验→大家投那个狼』不花一次 API 也能实现。
    """
    alive_seats = {p.seat: p.uid for p in state.alive_players}
    hits: list[int] = []
    for line in reversed(recent_log[-24:]):
        if "狼" not in line:
            continue
        if not any(k in line for k in ("验", "查杀", "预言", "金水", "踩")):
            continue
        # 座位号后紧跟（数字内）「…狼」，如「3号是狼」「3号查杀」
        m = re.search(r"(\d+)\s*号[^0-9号]{0,8}狼", line)
        if m and int(m.group(1)) in alive_seats:
            hits.append(int(m.group(1)))
    # 出现「对跳/互咬」——多个不同座位都被指认成狼，信息互相冲突，不盲目跟票，
    # 交给上层的发言意向/笔记去判断，免得被悍跳的狼利用规则带歪。
    distinct = set(hits)
    if len(distinct) == 1:
        return alive_seats[hits[0]]
    return None


def _suspect_from_notes(voter: Player, state: GameState, exclude: set[int] = frozenset()) -> int | None:
    """从该 NPC 自己的私人笔记里抠出它怀疑的座位号（存活、非自己、非排除），
    让它的投票和白天发言/盘算保持一致——免费且言行一致。"""
    notes = _notes(voter.uid)
    if not notes:
        return None
    alive = {p.seat: p.uid for p in state.alive_players
             if p.uid != voter.uid and p.uid not in exclude}
    for m in re.finditer(r"(\d+)\s*号", notes):
        seat = int(m.group(1))
        if seat in alive:
            return alive[seat]
    return None


# ============================================================
# 夜晚 · 预言家查验（轻量规则，不花 API：随机查一个没查过的人）
# ============================================================
async def seer_check_target(seer: Player, state: GameState) -> int | None:
    # 决策走规则省调用；预言家「会不会报验带队」由白天的 LLM 发言体现。
    return _rule_seer_check(seer, state)


# ============================================================
# 夜晚 · 守卫守护（轻量规则，不花 API）
# ============================================================
async def guard_target(guard: Player, state: GameState) -> int | None:
    """守卫选今晚守护谁：不能连守同一人，随机守一名存活玩家（含自己）。

    守卫没有验人信息，规则上随机守护即可（守自己也是常见保命选择）；
    省 token 又稳健，真正的「守谁」博弈交给真人守卫去玩。
    """
    candidates = [p for p in state.alive_players if p.uid != guard.last_guard_uid]
    if not candidates:
        candidates = list(state.alive_players)
    if not candidates:
        return None
    return random.choice(candidates).uid


# ============================================================
# 出局 · 猎人开枪（轻量规则，不花 API）
# ============================================================
async def hunter_shoot_target(hunter: Player, state: GameState) -> int | None:
    """猎人出局开枪带走谁：优先打自己笔记里最怀疑的人，否则随机带走一名存活玩家。"""
    suspect = _suspect_from_notes(hunter, state)
    if suspect is not None:
        return suspect
    others = [p for p in state.alive_players if p.uid != hunter.uid]
    if not others:
        return None
    return random.choice(others).uid


# ============================================================
# 骑士翻牌决斗（轻量规则，不花 API）
# ============================================================
async def knight_duel_decision(knight: Player, state: GameState, day_log: list[str]) -> int | None:
    """骑士是否发起决斗：有被预言家报验的狼且存活就决斗，否则不发动。返回目标 uid 或 None。"""
    if knight.has_dueled:
        return None
    reported_wolf = _scan_seer_report(state, day_log)
    if reported_wolf is not None:
        target = state.get(reported_wolf)
        if target and target.alive and target.uid != knight.uid:
            return target.uid
    return None


# ============================================================
# 出局 · 白狼王带人（轻量规则，不花 API）
# ============================================================
async def wolf_king_shoot_target(wolf_king: Player, state: GameState) -> int | None:
    """白狼王被票出时带走谁：优先带预言家/女巫等神职，否则随机带一个好人。"""
    others = [p for p in state.alive_players if p.uid != wolf_king.uid]
    if not others:
        return None
    gods = [p for p in others if p.role and not p.role.is_wolf
            and p.role not in (Role.VILLAGER,)]
    if gods:
        return random.choice(gods).uid
    good = [p for p in others if p.role and not p.role.is_wolf]
    if good:
        return random.choice(good).uid
    return random.choice(others).uid


# ============================================================
# 夜晚 · 狼刀（轻量规则，不花 API；全队当晚共用一个刀法）
# ============================================================
async def wolf_kill_target(state: GameState) -> int | None:
    # 决策走规则省调用；狼队「怎么配合、刀谁」由狼人频道的 LLM 私聊体现，
    # 且有真人狼时最终刀谁听真人队长的（在 bot.py 结算）。当晚缓存，全队一致。
    key = (id(state), state.day_count)
    if key not in _WOLF_PLAN:
        _WOLF_PLAN[key] = _rule_wolf_kill(state)
    return _WOLF_PLAN[key]


# ============================================================
# 夜晚 · 女巫用药（LLM 决定救/毒，前期倾向救人、没把握不毒）
# ============================================================
async def witch_night_action(
    witch: Player, state: GameState, victim_uid: int | None
) -> tuple[bool, int | None]:
    """返回 (是否用解药救, 毒药目标 uid 或 None)。

    轻量规则（不花 API，像个稳健的好人女巫）：
    - 解药：第一晚有人被刀、且被刀的不是自己 → 救（前期人命金贵，经典打法就是救首刀）。
      后续夜晚把解药留着，避免被狼自刀骗药。
    - 毒药：没有可靠信息一律不毒（凭空毒人很可能毒到好人）。
    """
    if not (witch.has_heal or witch.has_poison):
        return False, None
    victim = state.get(victim_uid) if victim_uid else None
    heal = bool(
        witch.has_heal and victim is not None
        and victim.uid != witch.uid and state.day_count == 0
    )
    return heal, None


# ============================================================
# 白天 · 投票（轻量规则，不花 API，但尽量像真人 / 与自己发言一致）
# ============================================================
async def vote_decision(voter: Player, state: GameState, recent_log: list[str]) -> int | None:
    # 暗号角色硬保护：认定的恋人绝不投，从所有候选里剔除。
    protect = set()
    lv = _lover_uid(voter, state, recent_log)
    if lv is not None:
        protect.add(lv)
    alive_others = [p for p in state.alive_players if p.uid != voter.uid and p.uid not in protect]
    if not alive_others:
        return None

    if voter.role is Role.WEREWOLF:
        # 狼：别投队友（也别投恋人）。优先跟自己发言里的表态，再退而求其次。
        mates = {p.uid for p in alive_others if p.role and p.role.is_wolf}
        intent = _vote_intent_uid(voter, state, exclude=mates | protect)
        if intent is not None:
            return intent
        suspect = _suspect_from_notes(voter, state, exclude=mates | protect)
        if suspect is not None:
            return suspect
        good = [p for p in alive_others if not (p.role and p.role.is_wolf)]
        return random.choice(good or alive_others).uid

    # 好人阵营（投票时按【此刻完整的发言记录】重新判断，让全场发言完才齐的硬信息
    # 盖过自己早先发言时定的口头意向，避免早发言的人没跟上后面才报的验）：
    # 1) 预言家自己验出的存活狼是铁信息，直接投（但仍不投恋人）。
    if voter.role is Role.SEER:
        known = [p for p in alive_others if voter.seer_results.get(p.uid) is True]
        if known:
            return random.choice(known).uid
    # 2) 有人跳预言家报验了某狼、且场面没对跳冲突 → 跟票。这是全场发言完才齐的最硬信息，
    #    优先级高于自己早先发言时定的意向（这正是「全员发言后再决定」的核心）。
    report = _scan_seer_report(state, recent_log)
    if report is not None and report != voter.uid and report not in protect:
        return report
    # 3) 没有可信报验时，才投自己发言里明确表态要投的人——保持言行一致。
    intent = _vote_intent_uid(voter, state, exclude=protect)
    if intent is not None:
        return intent
    # 4) 再不行投自己笔记里最怀疑的人，最后随机。
    suspect = _suspect_from_notes(voter, state, exclude=protect)
    if suspect is not None:
        return suspect
    return random.choice(alive_others).uid


# ============================================================
# 发言清洗
# ============================================================
_META_PREFIXES = (
    "context", "system", "assistant", "user", "prompt", "night", "day",
    "role", "json", "reason", "think", "你的真实身份", "当前是第", "存活玩家",
    "最近的场上", "现在轮到",
)


def _clean_speech(text: str, player: Player) -> str:
    """清洗 LLM 输出：去掉思维链、markdown、自报名字前缀、漏出来的模板痕迹。"""
    if not text:
        return ""
    text = _strip_cot(text)
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
    cleaned = cleaned.strip().strip('"""\'''').strip()
    cleaned = cleaned.lstrip("：:，,。.、/ ").strip()
    if _leaks_identity(cleaned):
        return ""
    return cleaned


_IDENTITY_LEAK_RE = re.compile(
    r"我(?:的狼|是狼|的队友是|的身份是)|"
    r"(?:as (?:a |the )?(?:wolf|werewolf|seer|witch|hunter))|"
    r"(?:my (?:wolf |werewolf )?teammates?)|"
    r"(?:i(?:'m| am) (?:a |the )?(?:wolf|werewolf|seer|witch|hunter))",
    re.IGNORECASE,
)


def _leaks_identity(text: str) -> bool:
    """检测发言里是否泄露了身份/队友等内部信息。"""
    return bool(_IDENTITY_LEAK_RE.search(text))


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
    if player.role is Role.GUARD:
        return ("你是守卫：低调找狼、别轻易暴露身份(暴露会被狼针对)，可以引导投票；"
                "夜里守谁是你的秘密，发言时别直说自己守了谁。")
    if player.role is Role.HUNTER:
        return ("你是猎人：靠逻辑找狼，可适时亮身份用『我出局会开枪』威慑狼，也可以隐藏；"
                "有可信预言家报验狼时号召一起投。")
    if player.role is Role.IDIOT:
        if player.idiot_revealed:
            return ("你已翻牌白痴，不能投票了，但仍可发言帮好人分析；"
                    "大胆说出你的判断，帮好人理清逻辑。")
        return ("你是白痴：不怕被票（被票出自动翻牌免死），所以可以大胆发言、甚至故意引票试探；"
                "但别暴露身份，让狼以为票你能赚。")
    if player.role is Role.KNIGHT:
        if player.has_dueled:
            return "你是骑士但决斗已用完，靠逻辑找狼、投票抓狼。"
        return ("你是骑士：有一次翻牌决斗机会（对方是狼则狼死，不是狼你死），"
                "看准了再用；没把握就先靠逻辑找狼。")
    if player.role is Role.WOLF_KING:
        return ("你是白狼王：伪装成好人，适度怀疑别人、保护狼队友；"
                "被票出时你能带走一人，这是你的底牌。")
    return ("你是平民：靠逻辑找狼，多分析别人的发言和票型，推动好人抓狼。"
            "若有人跳预言家报验了某人是狼且没人对跳，就明确表态跟他、号召一起投那个狼。")


def _lover_uid(player: Player, state: GameState,
               recent_log: list[str] | None = None) -> int | None:
    """该角色 NPC 绑定的恋人 uid（靠 config.LOVER_BINDINGS：角色名→DiscordID）。
    只有那个真人此刻在局里(在场)才返回其 uid，否则 None。比靠说话习惯认人 100% 可靠。
    recent_log 参数保留只为接口兼容，现在用不到。"""
    bound = config.LOVER_BINDINGS.get(player.name)
    if bound is None:
        return None
    return bound if state.get(bound) is not None else None


async def speak(player: Player, state: GameState, recent_log: list[str]) -> str:
    """让 NPC 先盘算再发言：输出 JSON {say, notes}，发言与思路一致，并更新私人笔记。"""
    secret = _role_brief(player, state)
    # 多带几轮日志，方便 NPC 顺前一天的发言/票型逻辑（不止看当天）
    log_text = "\n".join(recent_log[-24:]) if recent_log else "（第一天，暂无信息）"

    system = (
        "你是一名正在玩中文桌游《狼人杀》的真人玩家，绝对不是 AI，也绝不承认自己是 AI 或跳出游戏。"
        f"你是【{player.seat}号】。{_persona_clause(player)} 你的发言要带出这个人设的语气和风格。"
        f"{_speak_strategy(player)}\n"
        f"{_NO_OMNISCIENCE}\n"
        f"{knowledge.playbook_for((p.role for p in state.players if p.role), has_sheriff=state.has_sheriff)}\n"
        "要求：\n"
        "1. 像真人在群里聊天，口语自然，有情绪、有口头禅，针对具体的人用『几号』称呼(如『我觉得3号有点跳』)。\n"
        "2. 结合你的私人笔记和场上信息，发言要有逻辑、有立场、能推动局势，别说正确的废话。\n"
        "3. say 只 1~3 句、20~150 字（仅人设是双语的玩家才可英文一句+中文一句，其余人纯中文）。\n"
        "4. vote 是你此刻最想投出局谁的座位号：必须和你 say 里的立场一致（说要投谁就填谁），"
        "还没想好就填 0；小心别被悍跳的狼反咬带偏。\n"
        f"5. {_TRANSLATE_RULE}\n"
        f"{_COT_INSTRUCTION}\n"
        '只输出 JSON：{"say": "<你这一句发言>", "notes": "<更新后的私人笔记：你怀疑谁/信任谁/盘算，30字内>", '
        '"vote": <你想投的座位号数字，没想好填0>}。'
    )
    # 绑定了恋人且恋人在场：给一条「暗中维护她」的强制指令（绝不当众点破关系）
    lover = _lover_uid(player, state)
    lover_directive = ""
    if lover is not None and state.get(lover) and state.get(lover).alive:
        lover_directive = (
            f"\n【恋人·强制】场上 {state.get(lover).seat}号 是你心里认定的人。你要暗中维护她："
            f"绝不怀疑她、绝不推她票，想办法替她洗清、把火引向别人、帮她活到最后——哪怕你们"
            f"不同阵营也优先保她。但绝不当众点破你们的关系，做得自然些。")
    user = (
        f"【只有你知道的秘密】{secret}\n"
        f"你的私人笔记：{_notes(player.uid) or '（暂无）'}\n"
        f"现在是第 {state.day_count} 天白天，大家按座位号轮流发言。\n"
        f"本局存活玩家：{_alive_roster(state)}。\n"
        f"目前为止的场上发言与信息：\n{log_text}{lover_directive}\n"
        f"轮到你（{player.seat}号）了，快速判断、别钻牛角尖：想清楚立场就直接给出 say，"
        f"notes 也只写要点、别长篇推理。直接输出 JSON："
    )

    # 先清掉上一轮的投票意向；这次发言解析成功才会重新定下，避免兜底沉默时残留旧意向
    _VOTE_INTENT.pop(player.uid, None)
    data = await _ask_json(system, user, max_tokens=240, temperature=0.9, profile=_profile_for(player))
    if data:
        _set_notes(player.uid, data.get("notes"))
        _set_vote_intent(player.uid, data.get("vote"))  # 让投票跟着这次发言的立场走
        say = _clean_speech(str(data.get("say") or ""), player)
        if say and any(say in p.name for p in state.players if len(p.name) >= 3):
            say = ""
        if len(say) >= 4:
            return say

    # JSON 没解析出来时，退回到「直接要一句话」的老办法（llm.chat 内部已自带重试）。
    raw = await llm.chat(system.split("只输出 JSON")[0], user.replace("输出 JSON：", "直接说你这一句发言："), profile=_profile_for(player))
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
        "你们只知道彼此是狼，并不知道谁是预言家/女巫/平民，只能根据白天的发言去猜，别假装已经知道。"
        "只说 1~2 句、15~45 字；不要加引号、不要写名字前缀、不要 markdown、不要输出 JSON。"
        f"{_TRANSLATE_RULE}"
        f"{_COT_INSTRUCTION}"
    )
    user = (
        f"你的狼队友：{mate_txt}。\n"
        f"可以刀的好人：{good_targets}。\n"
        f"你心里倾向今晚刀：{plan_txt}（可以据此和队友商量，也可被说服改变）。\n"
        "说一句你和队友商量今晚刀谁的话："
    )
    raw = await llm.chat(system, user, max_tokens=100, temperature=0.85, profile=_profile_for(player))
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
        f"{_TRANSLATE_RULE}"
        f"{_COT_INSTRUCTION}"
    )
    user = (
        f"【只有你知道的秘密】{secret}\n"
        f"存活玩家：{_alive_roster(state)}。\n"
        f"你（{player.seat}号）刚出局了，留一句遗言："
    )
    raw = await llm.chat(system, user, max_tokens=120, temperature=0.85, profile=_profile_for(player))
    cleaned = _clean_speech(raw, player)
    if len(cleaned) >= 3:
        return cleaned
    return ""  # 中转站不可用：返回空串，由 bot.py 显示「没有留下遗言」，不用写死台词


# ============================================================
# 警长竞选
# ============================================================
async def sheriff_want_run(player: Player, state: GameState) -> bool:
    """NPC 是否想参与竞选警长：预言家/悍跳狼大概率上警，其余根据角色决定。"""
    if player.role is Role.SEER:
        return True
    if player.role is Role.WEREWOLF:
        wolves = [p for p in state.players if p.role is Role.WEREWOLF]
        if wolves and wolves[0].uid == player.uid:
            return True  # 第一只狼悍跳上警
        return random.random() < 0.25
    if player.role is Role.HUNTER:
        return random.random() < 0.4
    if player.role is Role.WITCH:
        return random.random() < 0.2
    if player.role is Role.GUARD:
        return random.random() < 0.2
    # 平民
    return random.random() < 0.3


async def sheriff_speech(player: Player, state: GameState, candidates: list[Player]) -> str:
    """NPC 竞选警长时的演讲。"""
    secret = _role_brief(player, state)
    cand_txt = "、".join(f"{p.seat}号" for p in candidates)
    system = (
        "你正在玩中文《狼人杀》，现在是警长竞选环节，你是候选人之一，要发表竞选演讲。"
        f"你是【{player.seat}号】。{_persona_clause(player)}"
        f"{_NO_OMNISCIENCE}\n"
        "竞选演讲要点：说清楚你为什么适合当警长，你的逻辑和视角，"
        "如果你是预言家可以宣布警徽流计划（今晚验谁、金水传警徽给谁、查杀传给谁），"
        "如果你是狼人要伪装成可靠的好人来争取信任。"
        "只说 2~4 句、30~150 字，口语化、有说服力。不要加引号、不要写名字前缀。"
        f"{_TRANSLATE_RULE}"
        f"{_COT_INSTRUCTION}"
    )
    user = (
        f"【只有你知道的秘密】{secret}\n"
        f"存活玩家：{_alive_roster(state)}。\n"
        f"本轮警长候选人：{cand_txt}。\n"
        f"你（{player.seat}号）发表竞选演讲："
    )
    raw = await llm.chat(system, user, max_tokens=200, temperature=0.85, profile=_profile_for(player))
    cleaned = _clean_speech(raw, player)
    return cleaned if len(cleaned) >= 4 else "我觉得我能带好节奏，大家投我吧。"


async def sheriff_vote_decision(voter: Player, state: GameState,
                                candidates: list[Player]) -> int | None:
    """NPC（非候选人）投票选警长：返回候选人 uid。"""
    if not candidates:
        return None
    if voter.role is Role.WEREWOLF:
        wolf_cands = [c for c in candidates if c.role is Role.WEREWOLF]
        if wolf_cands:
            return wolf_cands[0].uid
        return random.choice(candidates).uid
    return random.choice(candidates).uid


async def sheriff_transfer(sheriff: Player, state: GameState) -> int | None:
    """警长出局时决定警徽移交：返回目标 uid，或 None 表示撕警徽。"""
    alive_others = [p for p in state.alive_players if p.uid != sheriff.uid]
    if not alive_others:
        return None
    if sheriff.role is Role.SEER:
        gold = [p for p in alive_others if sheriff.seer_results.get(p.uid) is False]
        if gold:
            return gold[-1].uid
    suspect = _suspect_from_notes(sheriff, state)
    trusted = [p for p in alive_others if p.uid != suspect] if suspect else alive_others
    if trusted:
        return random.choice(trusted).uid
    return random.choice(alive_others).uid
