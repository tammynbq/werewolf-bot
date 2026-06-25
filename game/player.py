"""玩家模型（人类或 AI NPC）。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .roles import Role


@dataclass
class Player:
    # 人类玩家用 Discord user id；NPC 用负数 id 避免冲突
    uid: int
    name: str
    is_npc: bool = False
    role: Role | None = None
    alive: bool = True
    seat: int = 0   # 座位号（1-based），开局分配，用于轮流发言与互相称呼

    # 仅 NPC 用：性格设定，喂给 LLM 让发言有差异
    persona: str = ""

    # 预言家的查验记录： {目标 uid: 是否为狼}
    seer_results: dict[int, bool] = field(default_factory=dict)

    # 女巫的药剂（各一次）：解药 / 毒药是否还在
    has_heal: bool = True
    has_poison: bool = True

    @property
    def mention(self) -> str:
        """在 Discord 里展示用：人类用 @提及，NPC 用名字。"""
        if self.is_npc:
            return f"🤖{self.name}"
        return f"<@{self.uid}>"

    @property
    def label(self) -> str:
        """匿名展示名：开局分配座位后只显示座位号（如「5号」），
        不暴露真实昵称，也不区分真人/NPC，做到全程匿名。
        座位未分配（大厅阶段）时退回到名字以便辨认。"""
        if self.seat:
            return f"{self.seat}号"
        prefix = "🤖" if self.is_npc else "🧑"
        return f"{prefix}{self.name}"
