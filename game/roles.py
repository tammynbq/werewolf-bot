"""角色定义与板子（狼人 / 预言家 / 女巫 / 猎人 / 守卫 / 平民）。

板子用「预设」管理：每个预设是一组神职配置，狼人数按总人数 ≈1/3 自动计算，其余补平民。
- `auto`    —— 旧行为：按人数自动缩放（预言家≥4人、女巫≥6人），向后兼容。
- `simple`  —— 预言家 + 女巫（不含新角色，零引擎风险）。
- `hunter`  —— 预言家 + 女巫 + 猎人。
- `guard`   —— 预言家 + 女巫 + 守卫。
- `classic` —— 预言家 + 女巫 + 猎人 + 守卫（经典 12 人「预女猎守」屠边局）。

12 人时各预设都给 4 狼；选哪个由 config.BOARD（环境变量 WEREWOLF_BOARD）决定。
"""
from __future__ import annotations

import enum


class Role(enum.Enum):
    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    GUARD = "guard"
    VILLAGER = "villager"

    @property
    def cn(self) -> str:
        return {
            Role.WEREWOLF: "狼人",
            Role.SEER: "预言家",
            Role.WITCH: "女巫",
            Role.HUNTER: "猎人",
            Role.GUARD: "守卫",
            Role.VILLAGER: "平民",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Role.WEREWOLF: "🐺",
            Role.SEER: "🔮",
            Role.WITCH: "🧪",
            Role.HUNTER: "🏹",
            Role.GUARD: "🛡️",
            Role.VILLAGER: "👤",
        }[self]

    @property
    def is_wolf(self) -> bool:
        return self is Role.WEREWOLF

    @property
    def description(self) -> str:
        return {
            Role.WEREWOLF: "每晚和狼队友一起选择一名玩家击杀，并可在专属狼人频道里私下商量。目标：杀光好人。",
            Role.SEER: "每晚可查验一名玩家，得知其是『好人』还是『狼人』。目标：带领好人放逐所有狼。",
            Role.WITCH: "有一瓶解药和一瓶毒药（各一次）：夜里得知谁被刀，可用解药救活，或用毒药毒死一人。目标：帮好人放逐所有狼。",
            Role.HUNTER: "出局时（被狼刀或被票出）可以开枪带走一名玩家；但若是被女巫毒死则无法开枪。目标：帮好人放逐所有狼。",
            Role.GUARD: "每晚守护一名玩家使其当晚不被狼刀，不能连续两晚守同一人；注意『同守同救』（守卫和女巫同时救一人）该人仍会死。目标：帮好人放逐所有狼。",
            Role.VILLAGER: "没有特殊技能，靠发言和投票找出狼人。目标：放逐所有狼。",
        }[self]


# ============================================================
# 板子预设：每个预设 = 一组「好人神职」，狼按人数比例算，其余补平民
# ============================================================
# 神职按优先级排列：人不够时从尾部（守卫→猎人→女巫）开始砍，预言家最后才砍。
_PRESETS: dict[str, list[Role] | None] = {
    "auto": None,  # 特殊：走 role_distribution 的人数自适应逻辑
    "simple": [Role.SEER, Role.WITCH],
    "hunter": [Role.SEER, Role.WITCH, Role.HUNTER],
    "guard": [Role.SEER, Role.WITCH, Role.GUARD],
    "classic": [Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD],
}

# 给大厅/文档展示用的中文名
BOARD_NAMES: dict[str, str] = {
    "auto": "自动按人数",
    "simple": "极简局（预言家+女巫）",
    "hunter": "猎人局（预言家+女巫+猎人）",
    "guard": "守卫局（预言家+女巫+守卫）",
    "classic": "标准猎守局（预言家+女巫+猎人+守卫）",
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
    if villagers < 0:  # 极小人数兜底
        witches = 0
        villagers = total - wolves - seers
    return (
        [Role.WEREWOLF] * wolves
        + [Role.SEER] * seers
        + [Role.WITCH] * witches
        + [Role.VILLAGER] * villagers
    )


def _preset_distribution(total: int, specials: list[Role]) -> list[Role]:
    """按预设的神职清单 + 比例狼数 + 平民补齐生成板子；人不够时从尾部砍神职。"""
    wolves = _wolves_for(total)
    specials = list(specials)
    villagers = total - wolves - len(specials)
    while villagers < 0 and specials:
        specials.pop()  # 砍掉优先级最低的神职（守卫→猎人→女巫…）
        villagers = total - wolves - len(specials)
    return [Role.WEREWOLF] * wolves + specials + [Role.VILLAGER] * max(0, villagers)


def normalize_board(board: str | None) -> str:
    """把配置里的板子名规整成已知预设；未知/空时退回 auto。"""
    key = (board or "auto").strip().lower()
    return key if key in _PRESETS else "auto"


def role_distribution(total: int, board: str = "auto") -> list[Role]:
    """根据总人数与板子预设返回角色列表。

    board="auto"（默认）保持旧行为（按人数自适应）；其余预设见 `_PRESETS`。
    经典 12 人 classic 局即：🐺×4　🔮×1　🧪×1　🏹×1　🛡️×1　👤×4。
    """
    total = max(3, total)
    board = normalize_board(board)
    specials = _PRESETS[board]
    if specials is None:
        return _auto_distribution(total)
    return _preset_distribution(total, specials)


def summarize_distribution(total: int, board: str = "auto") -> str:
    roles = role_distribution(total, board)
    order = [Role.WEREWOLF, Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.VILLAGER]
    counts = {r: sum(1 for x in roles if x is r) for r in order}
    parts = [f"{r.emoji}{r.cn} ×{counts[r]}" for r in order if counts[r] > 0]
    board = normalize_board(board)
    name = BOARD_NAMES.get(board, board)
    return f"{total} 人 · {name}：" + "　".join(parts)
