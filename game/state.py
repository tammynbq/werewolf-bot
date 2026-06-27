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
        # 狼人专属私密线程 id（夜里狼队私聊用）
        self.wolf_thread_id: int | None = None
        # 面板模式：全程统一禁言，只有「当前发言/行动人」能通过面板操作
        self.current_speaker_uid: int | None = None
        # 每晚的死亡记录（可能 0~2 人：狼刀 + 女巫毒）
        self.night_deaths: list[Player] = []
        # 房主在大厅指定要加入本局的「角色 NPC」名字（空=不指定，自动补位）
        self.chosen_npc_names: list[str] = []
        # 本局桌子人数（房主在大厅选 6 / 12；不足用 AI 补位）
        self.table_size: int = 6
        # 本局板子预设（房主在大厅选；见 roles._PRESETS）。默认 auto，建局时由环境变量覆盖。
        self.board: str = "auto"
        # 警长 uid（classic 板有警长竞选；None = 没有警长或还没选出来）
        self.sheriff_uid: int | None = None

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

    def alive_seer(self) -> Player | None:
        return next((p for p in self.alive_players if p.role is Role.SEER), None)

    def alive_witch(self) -> Player | None:
        return next((p for p in self.alive_players if p.role is Role.WITCH), None)

    def alive_guard(self) -> Player | None:
        return next((p for p in self.alive_players if p.role is Role.GUARD), None)

    def alive_hunter(self) -> Player | None:
        return next((p for p in self.alive_players if p.role is Role.HUNTER), None)

    @property
    def has_sheriff(self) -> bool:
        """本局是否启用警长竞选（classic 板 + 人数 >= 9）。"""
        return self.board == "classic" and len(self.players) >= 9

    def sheriff_player(self) -> Player | None:
        if self.sheriff_uid is None:
            return None
        return self.get(self.sheriff_uid)

    def transfer_sheriff(self, new_uid: int | None) -> None:
        """警徽移交：传给 new_uid，或 None 表示撕警徽。"""
        old = self.sheriff_player()
        if old:
            old.is_sheriff = False
        if new_uid is not None:
            p = self.get(new_uid)
            if p and p.alive:
                p.is_sheriff = True
                self.sheriff_uid = new_uid
                return
        self.sheriff_uid = None

    # ---------- 开局 ----------
    def assign_roles(self, board: str = "auto") -> None:
        """打乱并分配角色与座位号。board 指定板子预设（见 roles._PRESETS）。"""
        # 先打乱玩家列表，使座位号、发言顺序都与「加入顺序」无关，
        # 避免有人对照大厅入座名单反推出某号是谁（保证匿名不被解码）。
        random.shuffle(self.players)
        roles = role_distribution(len(self.players), board)
        random.shuffle(roles)
        for player, role in zip(self.players, roles):
            player.role = role
        # 在打乱后的顺序上分配 1-based 座位号
        for i, player in enumerate(self.players, start=1):
            player.seat = i
        self.phase = Phase.NIGHT
        self.day_count = 0

    # ---------- 夜晚结算 ----------
    def resolve_night(
        self,
        kill_uid: int | None,
        witch_heal: bool = False,
        poison_uid: int | None = None,
        guard_uid: int | None = None,
    ) -> list[Player]:
        """结算夜晚，返回本晚死亡的玩家列表（0~2 人）。

        kill_uid    狼队击杀目标（None / 0 = 空刀）。
        witch_heal  女巫是否对狼刀目标使用了解药（救活）。
        poison_uid  女巫毒药目标（None = 没毒）。
        guard_uid   守卫守护目标（None = 没守 / 没有守卫）。

        守护规则：被守 + 没救 → 活；被救 + 没守 → 活；**同守同救** → 死（两层保护抵消）；
        都没有 → 死。即「守」和「救」恰好命中一个才救得活（异或）。毒药无视守护与解药。
        """
        deaths: list[Player] = []

        # 狼刀：守护与解药「恰好一个」命中才救得活（同守同救 = 死）
        if kill_uid:
            victim = self.get(kill_uid)
            if victim and victim.alive:
                guarded = (guard_uid is not None and guard_uid == kill_uid)
                saved = guarded != bool(witch_heal)  # XOR：同守同救则 False
                if not saved:
                    victim.alive = False
                    deaths.append(victim)

        # 女巫毒药（无视守护/解药）；被毒死的猎人不能开枪
        if poison_uid:
            poisoned = self.get(poison_uid)
            if poisoned and poisoned.alive and poisoned not in deaths:
                poisoned.alive = False
                poisoned.can_shoot = False  # 经典规则：猎人被毒无法开枪
                deaths.append(poisoned)

        self.night_deaths = deaths
        self.day_count += 1
        self.phase = Phase.DAY
        return deaths

    # ---------- 投票结算 ----------
    def resolve_votes(self, votes: dict[int, int]) -> tuple[Player | None, bool]:
        """votes: {投票者 uid: 目标 uid}。

        返回 (被放逐玩家或 None, 是否平票)。平票则无人出局。
        白痴首次被票出时免死（翻牌），由调用方处理。
        警长的票权重 1.5（四舍五入用浮点累加再比较）。
        """
        if not votes:
            return None, False
        tally: dict[int, float] = {}
        for voter_uid, target_uid in votes.items():
            weight = 1.5 if voter_uid == self.sheriff_uid else 1.0
            tally[target_uid] = tally.get(target_uid, 0.0) + weight
        top = sorted(tally.items(), key=lambda x: x[1], reverse=True)
        highest = top[0][1]
        leaders = [uid for uid, c in top if c == highest]
        if len(leaders) != 1:
            return None, True  # 平票
        exiled = self.get(leaders[0])
        if exiled and exiled.alive:
            # 白痴首次被票出：翻牌免死，但失去投票权
            if exiled.role is Role.IDIOT and not exiled.idiot_revealed:
                exiled.idiot_revealed = True
                return exiled, False  # 返回但不标死，调用方处理公告
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
        """游戏结束时公开所有人的身份，并揭晓每个座位号背后的真实玩家。"""
        lines = []
        for p in sorted(self.players, key=lambda x: x.seat):
            status = "存活" if p.alive else "出局"
            role = p.role.cn if p.role else "?"
            emoji = p.role.emoji if p.role else "❓"
            # 结算时解除匿名：座位号 → 真实玩家（NPC 显示机器人名）
            lines.append(f"{emoji} {p.label}（{p.mention}）—— {role}（{status}）")
        return "\n".join(lines)
