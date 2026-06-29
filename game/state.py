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
        # 每个被指定 NPC 是谁加入的：{NPC名: 加入它的真人 uid}。用于人均上限统计 / 找配置归属。
        self.npc_owner: dict[str, int] = {}
        # 玩家给 NPC 指定用自己的哪个私有站：{NPC名: 站名(label)}。没有=走 bot 默认 API。
        # 只存「归属玩家 uid + 站名」这层引用，真正的 url/key/model 开局时再去 userapi 里解析，
        # 这样玩家中途编辑/删除站，开局自然用到最新配置。
        self.npc_station: dict[str, str] = {}
        # 本局桌子人数（房主在大厅选 6 / 12；不足用 AI 补位）
        self.table_size: int = 6
        # 本局板子预设（房主在大厅选；见 roles._PRESETS）。默认 auto，建局时由环境变量覆盖。
        self.board: str = "auto"
        # 警长 uid（classic 板有警长竞选；None = 没有警长或还没选出来）
        self.sheriff_uid: int | None = None
        # 测试模式：开局时指定某些座位的角色（{座位号: Role}）
        self.fixed_roles: dict[int, "Role"] | None = None
        # 夜晚行动详细记录（复盘用）：每晚一条 dict
        self.night_actions: list[dict] = []

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
    def assign_roles(self, board: str = "auto",
                     fixed_roles: dict[int, Role] | None = None) -> None:
        """打乱并分配角色与座位号。board 指定板子预设（见 roles._PRESETS）。

        fixed_roles: 测试模式用——{座位号: Role}，指定某些座位的角色，其余随机。
        """
        # 先打乱玩家列表，使座位号、发言顺序都与「加入顺序」无关，
        # 避免有人对照大厅入座名单反推出某号是谁（保证匿名不被解码）。
        random.shuffle(self.players)
        # 在打乱后的顺序上分配 1-based 座位号
        for i, player in enumerate(self.players, start=1):
            player.seat = i

        if fixed_roles:
            roles = role_distribution(len(self.players), board)
            random.shuffle(roles)
            used: list[Role] = []
            for seat, role in fixed_roles.items():
                p = next((p for p in self.players if p.seat == seat), None)
                if p is not None:
                    p.role = role
                    used.append(role)
            remaining_roles = list(roles)
            for r in used:
                if r in remaining_roles:
                    remaining_roles.remove(r)
            unassigned = [p for p in self.players if p.role is None]
            random.shuffle(remaining_roles)
            for player, role in zip(unassigned, remaining_roles):
                player.role = role
        else:
            roles = role_distribution(len(self.players), board)
            random.shuffle(roles)
            for player, role in zip(self.players, roles):
                player.role = role
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

    def record_night_action(self, night: int, *,
                            guard_uid: int | None = None,
                            seer_uid: int | None = None,
                            seer_target: int | None = None,
                            seer_result: bool | None = None,
                            wolf_kill: int | None = None,
                            witch_heal: bool = False,
                            witch_poison: int | None = None,
                            deaths: list["Player"] | None = None) -> None:
        """记录一晚的所有行动（复盘用）。"""
        action: dict = {"night": night}
        if guard_uid is not None:
            g = self.get(guard_uid)
            action["guard"] = f"守卫守护了{g.seat}号" if g else None
        if seer_uid is not None and seer_target is not None:
            st = self.get(seer_target)
            verdict = "狼人" if seer_result else "好人"
            action["seer"] = f"预言家({self.get(seer_uid).seat}号)查验了{st.seat}号→{verdict}" if st else None
        if wolf_kill is not None:
            wt = self.get(wolf_kill)
            action["wolf"] = f"狼人刀了{wt.seat}号" if wt else None
        else:
            action["wolf"] = "狼人空刀"
        if witch_heal:
            action["witch_heal"] = "女巫用了解药"
        if witch_poison is not None:
            wp = self.get(witch_poison)
            action["witch_poison"] = f"女巫毒了{wp.seat}号" if wp else None
        if deaths is not None:
            if deaths:
                action["result"] = "死亡：" + "、".join(f"{d.seat}号" for d in deaths)
            else:
                action["result"] = "平安夜"
        self.night_actions.append(action)

    def format_review(self, day_log: list[str]) -> str:
        """生成赛后复盘报告。"""
        import re as _re
        lines: list[str] = []
        lines.append("========== 赛后复盘 ==========")
        lines.append("")

        # 身份揭晓
        lines.append("[身份一览]")
        for p in sorted(self.players, key=lambda x: x.seat):
            status = "存活" if p.alive else "出局"
            role = p.role.cn if p.role else "?"
            tag = "(AI)" if p.is_npc else "(玩家)"
            lines.append(f"  {p.seat}号 {role} {tag}{p.name} -- {status}")
        lines.append("")

        # 按回合组织：夜晚行动 + 白天记录交替展示
        max_day = self.day_count or 1

        # 先把 day_log 里的条目按天分组
        day_groups: dict[int, list[str]] = {}
        current_day = 0
        for entry in day_log:
            m = _re.match(r"第(\d+)晚", entry)
            if m:
                current_day = int(m.group(1))
                continue
            if current_day not in day_groups:
                day_groups[current_day] = []
            day_groups[current_day].append(entry)

        # 按 night_actions 的索引和对应的白天记录交替展示
        for action in self.night_actions:
            night = action.get("night", "?")
            lines.append(f"[第{night}夜 - 行动详情]")
            if action.get("guard"):
                lines.append(f"  {action['guard']}")
            else:
                lines.append("  守卫：未守护 / 本局无守卫")
            if action.get("seer"):
                lines.append(f"  {action['seer']}")
            else:
                lines.append("  预言家：未查验 / 本局无预言家")
            if action.get("wolf"):
                lines.append(f"  {action['wolf']}")
            if action.get("witch_heal"):
                lines.append(f"  {action['witch_heal']}")
            if action.get("witch_poison"):
                lines.append(f"  {action['witch_poison']}")
            if action.get("result"):
                lines.append(f"  => {action['result']}")
            lines.append("")

            # 对应白天
            day_num = night
            day_entries = day_groups.get(day_num, [])
            if day_entries:
                lines.append(f"[第{day_num}天 - 白天记录]")
                for entry in day_entries:
                    lines.append(f"  {entry}")
                lines.append("")

        # 没有被分组的条目（可能是第0天或边界情况）
        for d in sorted(day_groups.keys()):
            if d == 0 or d not in [a.get("night") for a in self.night_actions]:
                entries = day_groups[d]
                if entries:
                    header = f"[第{d}天 - 白天记录]" if d > 0 else "[其他记录]"
                    lines.append(header)
                    for entry in entries:
                        lines.append(f"  {entry}")
                    lines.append("")

        # 胜负
        if self.winner:
            winner_text = "狼人阵营获胜" if self.winner is Team.WOLF else "好人阵营获胜"
            lines.append(f"[结局] {winner_text}")

        return "\n".join(lines)

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
