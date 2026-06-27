"""角色定义与板子预设。

板子用「预设」管理：每个预设是一组特殊角色配置，狼人数按总人数 ≈1/3 自动计算，其余补平民。
预设里的狼阵营角色（白狼王）会替换掉一个普通狼人，而不是额外增加。

可用角色：
  好人神职：预言家 / 女巫 / 猎人 / 守卫 / 白痴 / 骑士
  狼人阵营：狼人 / 白狼王
  普通好人：平民
"""
from __future__ import annotations

import enum


class Role(enum.Enum):
    WEREWOLF = "werewolf"
    WOLF_KING = "wolf_king"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    GUARD = "guard"
    IDIOT = "idiot"
    KNIGHT = "knight"
    VILLAGER = "villager"

    @property
    def cn(self) -> str:
        return {
            Role.WEREWOLF: "狼人",
            Role.WOLF_KING: "白狼王",
            Role.SEER: "预言家",
            Role.WITCH: "女巫",
            Role.HUNTER: "猎人",
            Role.GUARD: "守卫",
            Role.IDIOT: "白痴",
            Role.KNIGHT: "骑士",
            Role.VILLAGER: "平民",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Role.WEREWOLF: "🐺",
            Role.WOLF_KING: "👑🐺",
            Role.SEER: "🔮",
            Role.WITCH: "🧪",
            Role.HUNTER: "🏹",
            Role.GUARD: "🛡️",
            Role.IDIOT: "🤡",
            Role.KNIGHT: "⚔️",
            Role.VILLAGER: "👤",
        }[self]

    @property
    def is_wolf(self) -> bool:
        return self in (Role.WEREWOLF, Role.WOLF_KING)

    @property
    def description(self) -> str:
        return {
            Role.WEREWOLF: "每晚和狼队友一起选择一名玩家击杀，并可在专属狼人频道里私下商量。目标：杀光好人。",
            Role.WOLF_KING: "狼人阵营，夜里和狼队友一起刀人。被投票放逐出局时可以带走一名玩家（类似猎人，但是狼）。目标：杀光好人。",
            Role.SEER: "每晚可查验一名玩家，得知其是『好人』还是『狼人』。目标：带领好人放逐所有狼。",
            Role.WITCH: "有一瓶解药和一瓶毒药（各一次）：夜里得知谁被刀，可用解药救活，或用毒药毒死一人。目标：帮好人放逐所有狼。",
            Role.HUNTER: "出局时（被狼刀或被票出）可以开枪带走一名玩家；但若是被女巫毒死则无法开枪。目标：帮好人放逐所有狼。",
            Role.GUARD: "每晚守护一名玩家使其当晚不被狼刀，不能连续两晚守同一人；注意『同守同救』（守卫和女巫同时救一人）该人仍会死。目标：帮好人放逐所有狼。",
            Role.IDIOT: "被投票放逐出局时自动翻牌免死一次，但翻牌后永久失去投票权。目标：帮好人放逐所有狼。",
            Role.KNIGHT: "白天发言时可以亮出身份，选择与一名玩家「翻牌决斗」：对方是狼则狼死，对方不是狼则骑士自己死。一局只能用一次。目标：帮好人放逐所有狼。",
            Role.VILLAGER: "没有特殊技能，靠发言和投票找出狼人。目标：放逐所有狼。",
        }[self]


# ============================================================
# 板子预设：每个预设 = 一组特殊角色，狼按人数比例算，其余补平民
# ============================================================
# 神职按优先级排列：人不够时从尾部开始砍。
# 狼阵营角色（白狼王）会替换一个普通狼位，不额外增加狼数。
_PRESETS: dict[str, list[Role] | None] = {
    "auto": None,
    # --- 基础板（适合 6~8 人入门） ---
    "simple": [Role.SEER, Role.WITCH],
    "hunter": [Role.SEER, Role.WITCH, Role.HUNTER],
    "guard": [Role.SEER, Role.WITCH, Role.GUARD],
    # --- 双神板（适合 8~10 人） ---
    "idiot": [Role.SEER, Role.WITCH, Role.IDIOT],
    "knight": [Role.SEER, Role.WITCH, Role.KNIGHT],
    "hunter_idiot": [Role.SEER, Role.WITCH, Role.HUNTER, Role.IDIOT],
    "hunter_knight": [Role.SEER, Role.WITCH, Role.HUNTER, Role.KNIGHT],
    "guard_idiot": [Role.SEER, Role.WITCH, Role.GUARD, Role.IDIOT],
    "guard_knight": [Role.SEER, Role.WITCH, Role.GUARD, Role.KNIGHT],
    # --- 经典四神（适合 10~12 人） ---
    "classic": [Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD],
    "classic_idiot": [Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.IDIOT],
    "classic_knight": [Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.KNIGHT],
    # --- 白狼王局（一只狼变白狼王） ---
    "wolfking": [Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.WOLF_KING],
    "wolfking_knight": [Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.WOLF_KING, Role.KNIGHT],
}

BOARD_NAMES: dict[str, str] = {
    "auto": "自动按人数",
    "simple": "预女局",
    "hunter": "预女猎",
    "guard": "预女守",
    "idiot": "预女痴",
    "knight": "预女骑",
    "hunter_idiot": "预女猎痴",
    "hunter_knight": "预女猎骑",
    "guard_idiot": "预女守痴",
    "guard_knight": "预女守骑",
    "classic": "预女猎守（经典）",
    "classic_idiot": "预女猎守痴",
    "classic_knight": "预女猎守骑",
    "wolfking": "预女猎守+白狼王",
    "wolfking_knight": "预女猎守骑+白狼王",
}


def _wolves_for(total: int) -> int:
    """狼人数 ≈ 总人数 1/3（至少 1），且保证好人不少于狼，避免开局劣势。"""
    wolves = max(1, total // 3)
    return min(wolves, max(1, (total - 1) // 2))


def _auto_distribution(total: int) -> list[Role]:
    """旧的人数自适应板子：狼≈1/3，≥4 人配预言家，≥6 人配女巫，其余平民。"""
    wolves = _wolves_for(total)
    seers = 1 if total >= 4 else 0
    witches = 1 if total >= 6 else 0
    villagers = total - wolves - seers - witches
    if villagers < 0:
        witches = 0
        villagers = total - wolves - seers
    return (
        [Role.WEREWOLF] * wolves
        + [Role.SEER] * seers
        + [Role.WITCH] * witches
        + [Role.VILLAGER] * villagers
    )


def _preset_distribution(total: int, specials: list[Role]) -> list[Role]:
    """按预设的特殊角色清单生成板子。

    狼阵营特殊角色（白狼王）替换一个普通狼位；好人神职从平民位扣。
    人不够时从尾部砍特殊角色。
    """
    specials = list(specials)
    total_wolves = _wolves_for(total)
    wolf_specials = [r for r in specials if r.is_wolf]
    good_specials = [r for r in specials if not r.is_wolf]

    regular_wolves = total_wolves - len(wolf_specials)
    villagers = total - total_wolves - len(good_specials)

    while villagers < 0 and good_specials:
        good_specials.pop()
        villagers = total - total_wolves - len(good_specials)
    while regular_wolves < 0 and wolf_specials:
        wolf_specials.pop()
        regular_wolves = total_wolves - len(wolf_specials)

    return (
        [Role.WEREWOLF] * max(0, regular_wolves)
        + wolf_specials
        + good_specials
        + [Role.VILLAGER] * max(0, villagers)
    )


def normalize_board(board: str | None) -> str:
    """把配置里的板子名规整成已知预设；未知/空时退回 auto。"""
    key = (board or "auto").strip().lower()
    return key if key in _PRESETS else "auto"


def role_distribution(total: int, board: str = "auto") -> list[Role]:
    """根据总人数与板子预设返回角色列表。"""
    total = max(3, total)
    board = normalize_board(board)
    specials = _PRESETS[board]
    if specials is None:
        return _auto_distribution(total)
    return _preset_distribution(total, specials)


# 展示用的角色排列顺序
_DISPLAY_ORDER = [
    Role.WEREWOLF, Role.WOLF_KING, Role.SEER, Role.WITCH,
    Role.HUNTER, Role.GUARD, Role.IDIOT, Role.KNIGHT, Role.VILLAGER,
]


def summarize_distribution(total: int, board: str = "auto") -> str:
    roles = role_distribution(total, board)
    counts = {r: sum(1 for x in roles if x is r) for r in _DISPLAY_ORDER}
    parts = [f"{r.emoji}{r.cn} ×{counts[r]}" for r in _DISPLAY_ORDER if counts[r] > 0]
    board = normalize_board(board)
    name = BOARD_NAMES.get(board, board)
    return f"{total} 人 · {name}：" + "　".join(parts)
