"""游戏状态机：玩家、阶段、夜晚结算、投票、胜负判定（与 Discord 解耦）。"""
from __future__ import annotations

import enum
import random
from collections import Counter

from .player import Player
from .roles import Role, role_distribution


class Phase(enum.Enum):
    LOBBY = "lobby"        # 等待玩家加入
    NIGHT = "night"        # 夜晚（狼刀 / 预言家查验）
    DAY = "day"            # 白天发言
    VOTE = "vote"          # 投票放逐
    ENDED = "ended"        # 游戏结束


class Team(enum.Enum):
    WOLF = "wolf"
    VILLAGE = "village"


class GameState:
    def __init__(self, channel_id: int, host_id: int):
        self.channel_id = channel_id
        self.host_id = host_id
        self.phase = Phase.LOBBY
        self.players: list[Player] = []
        self.day_count = 0
        self.winner: Team | None = None
        # 本局私密讨论串的 id（无则在频道内进行）
        self.thread_id: int | None = None
        # 游戏实际进行的频道/讨论串 id；发言管控只作用于此
        self.play_channel_id: int | None = None
        # 白天轮流发言阶段：是否启用「只有当前发言人能说话」
        self.discussion_active: bool = False
        self.current_speaker_uid: int | None = None
        # 每晚的临时记录
        self.last_killed: Player | None = None

    # ---------- 玩家管理 ----------
    def get(self, uid: int) -> Player | None:
        return next((p for p in self.players if p.uid == uid), None)

    def add_human(self, uid: int, name: str) -> bool:
        if self.get(uid) is not None:
            return False
        self.players.append(Player(uid=uid, name=name, is_npc=False))
        return True

    def remove_human(self, uid: int) -> bool:
        p = self.get(uid)
        if p is None or p.is_npc:
            return False
        self.players.remove(p)
        return True

    @property
    def humans(self) -> list[Player]:
        return [p for p in self.players if not p.is_npc]

    @property
    def alive_players(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def alive_wolves(self) -> list[Player]:
        return [p for p in self.alive_players if p.role and p.role.is_wolf]

    def alive_villagers(self) -> list[Player]:
        return [p for p in self.alive_players if p.role and not p.role.is_wolf]

    # ---------- 开局 ----------
    def assign_roles(self) -> None:
        """打乱并分配角色。"""
        roles = role_distribution(len(self.players))
        random.shuffle(roles)
        for player, role in zip(self.players, roles):
            player.role = role
        # 按入座顺序分配座位号（1-based），用于轮流发言与互相称呼
        for i, player in enumerate(self.players, start=1):
            player.seat = i
        self.phase = Phase.NIGHT
        self.day_count = 0

    # ---------- 夜晚结算 ----------
    def resolve_night(self, kill_uid: int | None) -> Player | None:
        """根据狼队的击杀目标结算夜晚，返回被杀玩家（可能为 None=空刀）。"""
        self.last_killed = None
        if kill_uid is not None:
            victim = self.get(kill_uid)
            if victim and victim.alive:
                victim.alive = False
                self.last_killed = victim
        self.day_count += 1
        self.phase = Phase.DAY
        return self.last_killed

    # ---------- 投票结算 ----------
    def resolve_votes(self, votes: dict[int, int]) -> tuple[Player | None, bool]:
        """votes: {投票者 uid: 目标 uid}。

        返回 (被放逐玩家或 None, 是否平票)。平票则无人出局。
        """
        if not votes:
            return None, False
        tally = Counter(votes.values())
        top = tally.most_common()
        highest = top[0][1]
        leaders = [uid for uid, c in top if c == highest]
        if len(leaders) != 1:
            return None, True  # 平票
        exiled = self.get(leaders[0])
        if exiled and exiled.alive:
            exiled.alive = False
        return exiled, False

    # ---------- 胜负判定 ----------
    def check_winner(self) -> Team | None:
        wolves = len(self.alive_wolves())
        villagers = len(self.alive_villagers())
        if wolves == 0:
            self.winner = Team.VILLAGE
        elif wolves >= villagers:
            # 狼人数 >= 好人数，狼人获胜（屠边/屠城前置）
            self.winner = Team.WOLF
        else:
            return None
        self.phase = Phase.ENDED
        return self.winner

    def public_roles_reveal(self) -> str:
        """游戏结束时公开所有人的身份。"""
        lines = []
        for p in self.players:
            status = "存活" if p.alive else "出局"
            role = p.role.cn if p.role else "?"
            emoji = p.role.emoji if p.role else "❓"
            lines.append(f"{emoji} {p.label} —— {role}（{status}）")
        return "\n".join(lines)
