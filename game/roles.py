"""角色定义与板子（狼人 / 预言家 / 女巫 / 平民）。"""
from __future__ import annotations

import enum


class Role(enum.Enum):
    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    VILLAGER = "villager"

    @property
    def cn(self) -> str:
        return {
            Role.WEREWOLF: "狼人",
            Role.SEER: "预言家",
            Role.WITCH: "女巫",
            Role.VILLAGER: "平民",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Role.WEREWOLF: "🐺",
            Role.SEER: "🔮",
            Role.WITCH: "🧪",
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
            Role.VILLAGER: "没有特殊技能，靠发言和投票找出狼人。目标：放逐所有狼。",
        }[self]


def role_distribution(total: int) -> list[Role]:
    """根据总人数返回角色列表。

    规则：狼人 ≈ 总人数的 1/3（至少 1）；人够就配 1 预言家 + 1 女巫；其余平民。
    经典 6 人局即：🐺×2　🔮×1　🧪×1　👤×2。
    """
    total = max(3, total)
    wolves = max(1, total // 3)
    # 保证好人阵营人数不少于狼，避免一开局就劣势板
    wolves = min(wolves, max(1, (total - 1) // 2))

    seers = 1 if total >= 4 else 0
    witches = 1 if total >= 6 else 0
    villagers = total - wolves - seers - witches
    if villagers < 0:  # 极小人数兜底
        witches = 0
        villagers = total - wolves - seers

    roles: list[Role] = (
        [Role.WEREWOLF] * wolves
        + [Role.SEER] * seers
        + [Role.WITCH] * witches
        + [Role.VILLAGER] * villagers
    )
    return roles


def summarize_distribution(total: int) -> str:
    roles = role_distribution(total)
    wolves = sum(1 for r in roles if r is Role.WEREWOLF)
    seers = sum(1 for r in roles if r is Role.SEER)
    witches = sum(1 for r in roles if r is Role.WITCH)
    villagers = sum(1 for r in roles if r is Role.VILLAGER)
    parts = [f"🐺狼人 ×{wolves}"]
    if seers:
        parts.append(f"🔮预言家 ×{seers}")
    if witches:
        parts.append(f"🧪女巫 ×{witches}")
    parts.append(f"👤平民 ×{villagers}")
    return f"{total} 人局：" + "　".join(parts)
