"""角色定义与板子（最简版：狼人 / 预言家 / 平民）。"""
from __future__ import annotations

import enum


class Role(enum.Enum):
    WEREWOLF = "werewolf"
    SEER = "seer"
    VILLAGER = "villager"

    @property
    def cn(self) -> str:
        return {
            Role.WEREWOLF: "狼人",
            Role.SEER: "预言家",
            Role.VILLAGER: "平民",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Role.WEREWOLF: "🐺",
            Role.SEER: "🔮",
            Role.VILLAGER: "👤",
        }[self]

    @property
    def is_wolf(self) -> bool:
        return self is Role.WEREWOLF

    @property
    def description(self) -> str:
        return {
            Role.WEREWOLF: "每晚和狼队友一起选择一名玩家击杀。目标：杀光好人。",
            Role.SEER: "每晚可查验一名玩家，得知其是『好人』还是『狼人』。目标：带领好人放逐所有狼。",
            Role.VILLAGER: "没有特殊技能，靠发言和投票找出狼人。目标：放逐所有狼。",
        }[self]


def role_distribution(total: int) -> list[Role]:
    """根据总人数返回角色列表（最简版板子）。

    规则：狼人数 ≈ 总人数的 1/4，至少 1 只；固定 1 个预言家；其余平民。
    """
    total = max(3, total)
    wolves = max(1, total // 4)
    # 保证好人阵营（含预言家）人数多于狼人，避免一开局就劣势板
    wolves = min(wolves, (total - 1) // 2)
    seers = 1
    villagers = total - wolves - seers

    roles: list[Role] = (
        [Role.WEREWOLF] * wolves
        + [Role.SEER] * seers
        + [Role.VILLAGER] * villagers
    )
    return roles


def summarize_distribution(total: int) -> str:
    roles = role_distribution(total)
    wolves = sum(1 for r in roles if r is Role.WEREWOLF)
    seers = sum(1 for r in roles if r is Role.SEER)
    villagers = sum(1 for r in roles if r is Role.VILLAGER)
    return f"{total} 人局：🐺狼人 ×{wolves}　🔮预言家 ×{seers}　👤平民 ×{villagers}"
