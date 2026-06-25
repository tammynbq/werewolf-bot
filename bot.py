"""Discord 狼人杀 bot —— 交互层。

负责：slash 命令、大厅加入、NPC 补位开局、夜晚 DM 行动、白天发言与投票 UI，
以及驱动整局游戏流程。核心规则在 game/ 包，NPC 在 npc.py，LLM 在 llm.py。
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter

import discord
from discord import app_commands

import config
import npc
from game.roles import Role, summarize_distribution
from game.state import GameState, Phase, Team

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("werewolf.bot")

# 每个频道一局：channel_id -> GameState
games: dict[int, GameState] = {}

# 开局后留给玩家查看身份的秒数
REVEAL_SECONDS = config.REVEAL_SECONDS


# ============================================================
# UI 组件
# ============================================================
class NightActions:
    """一夜之中收集到的人类/NPC 行动（共享给各 ephemeral 菜单写入）。"""

    def __init__(self, expected: set[int]):
        self.wolf_votes: dict[int, int] = {}   # 狼 uid -> 击杀目标 uid
        self.acted: set[int] = set()           # 本夜已完成行动的玩家 uid
        self.expected = expected               # 需要等待行动的人类 uid（狼+预言家）
        self.done = asyncio.Event()

    def mark(self, uid: int) -> None:
        self.acted.add(uid)
        if self.expected and self.expected <= self.acted:
            self.done.set()  # 所有该行动的人都点完了，提前结束等待


class WolfKillSelect(discord.ui.Select):
    """狼人在 ephemeral 菜单里选择击杀目标。"""

    def __init__(self, wolf, state: GameState, actions: NightActions):
        options = [discord.SelectOption(label="🌙 空刀（今晚不杀人）", value="0")] + [
            discord.SelectOption(label=p.label, value=str(p.uid))
            for p in state.alive_players if not (p.role and p.role.is_wolf)
        ]
        super().__init__(placeholder="选择今晚击杀的目标…", min_values=1, max_values=1, options=options)
        self._wolf = wolf
        self._state = state
        self._actions = actions

    async def callback(self, interaction: discord.Interaction):
        val = int(self.values[0])
        self._actions.wolf_votes[self._wolf.uid] = val  # 0 = 空刀
        self._actions.mark(self._wolf.uid)
        if val == 0:
            content = "🐺 你选择**今晚空刀**（不杀人）。（可重新点按钮修改）"
        else:
            target = self._state.get(val)
            content = f"🐺 你选择击杀 **{target.label}**。（可重新点按钮修改）"
        await interaction.response.edit_message(content=content, view=None)


class SeerCheckSelect(discord.ui.Select):
    """预言家在 ephemeral 菜单里选择查验目标，立即回报结果。"""

    def __init__(self, seer, state: GameState, actions: NightActions):
        options = [
            discord.SelectOption(label=p.label, value=str(p.uid))
            for p in state.alive_players if p.uid != seer.uid
        ]
        super().__init__(placeholder="选择今晚查验的对象…", min_values=1, max_values=1, options=options)
        self._seer = seer
        self._state = state
        self._actions = actions

    async def callback(self, interaction: discord.Interaction):
        target = self._state.get(int(self.values[0]))
        is_wolf = bool(target.role and target.role.is_wolf)
        self._seer.seer_results[target.uid] = is_wolf
        self._actions.mark(self._seer.uid)
        verdict = "🐺 狼人" if is_wolf else "✅ 好人"
        await interaction.response.edit_message(
            content=f"🔮 查验结果：**{target.label}** 是 {verdict}", view=None
        )


class RoleActionView(discord.ui.View):
    """点「夜晚行动」后，根据身份给出的 ephemeral 菜单。"""

    def __init__(self, player, state: GameState, actions: NightActions, timeout: int):
        super().__init__(timeout=timeout)
        if player.role is Role.WEREWOLF:
            self.add_item(WolfKillSelect(player, state, actions))
        elif player.role is Role.SEER:
            self.add_item(SeerCheckSelect(player, state, actions))


class NightPanelView(discord.ui.View):
    """夜晚公开面板：一个按钮，按下后只给本人看 ephemeral 行动菜单。"""

    def __init__(self, state: GameState, actions: NightActions, timeout: int):
        super().__init__(timeout=timeout)
        self.state = state
        self.actions = actions

    @discord.ui.button(label="进行夜晚行动", style=discord.ButtonStyle.primary, emoji="🌙")
    async def act(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.state.get(interaction.user.id)
        if p is None:
            await interaction.response.send_message("你不在本局游戏里。", ephemeral=True)
            return
        if not p.alive:
            await interaction.response.send_message("你已经出局，无法行动。", ephemeral=True)
            return
        if p.role is Role.WEREWOLF:
            mates = [
                m.label for m in self.state.players
                if m.role is Role.WEREWOLF and m.uid != p.uid
            ]
            mate_txt = "队友：" + "、".join(mates) if mates else "你是本局唯一的狼"
            content = f"🐺 你是**狼人**（{mate_txt}）。选择今晚击杀目标："
        elif p.role is Role.SEER:
            content = "🔮 你是**预言家**。选择今晚要查验的对象："
        else:
            await interaction.response.send_message(
                "👤 你是**平民**，今晚没有行动，安心睡觉～", ephemeral=True
            )
            return
        await interaction.response.send_message(
            content, view=RoleActionView(p, self.state, self.actions, button.view.timeout or 60),
            ephemeral=True,
        )


class RoleRevealView(discord.ui.View):
    """身份查看面板：每个人点按钮，只给自己看到身份。"""

    def __init__(self, state: GameState, timeout: int):
        super().__init__(timeout=timeout)
        self.state = state

    @discord.ui.button(label="查看我的身份", style=discord.ButtonStyle.secondary, emoji="🔍")
    async def reveal(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.state.get(interaction.user.id)
        if p is None or p.role is None:
            await interaction.response.send_message("你不在本局游戏里。", ephemeral=True)
            return
        text = f"你的身份：{p.role.emoji} **{p.role.cn}**\n{p.role.description}"
        if p.role is Role.WEREWOLF:
            mates = [
                m.label for m in self.state.players
                if m.role is Role.WEREWOLF and m.uid != p.uid
            ]
            text += "\n\n🐺 你的狼队友：" + ("、".join(mates) if mates else "无（你是独狼）")
        await interaction.response.send_message(text, ephemeral=True)


class VoteSelect(discord.ui.Select):
    """白天公开投票（共享，多个存活玩家都能投）。"""

    def __init__(self, options: list[tuple[str, str]], allowed_ids: set[int], votes: dict[int, int]):
        super().__init__(
            placeholder="选择要放逐的玩家…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=l, value=v) for l, v in options],
        )
        self._allowed = allowed_ids
        self._votes = votes

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id not in self._allowed:
            await interaction.response.send_message(
                "你不是存活玩家，不能投票。", ephemeral=True
            )
            return
        self._votes[interaction.user.id] = int(self.values[0])
        target = self.values[0]
        await interaction.response.send_message(
            "🗳️ 已记录你的投票（可重选覆盖）。", ephemeral=True
        )


class VoteView(discord.ui.View):
    def __init__(self, options: list[tuple[str, str]], allowed_ids: set[int], timeout: int):
        super().__init__(timeout=timeout)
        self.votes: dict[int, int] = {}
        self.add_item(VoteSelect(options, allowed_ids, self.votes))


class LobbyView(discord.ui.View):
    """大厅：加入 / 退出 / 开始。"""

    def __init__(self, bot: discord.Client, state: GameState):
        super().__init__(timeout=None)
        self.bot = bot
        self.state = state
        self.message: discord.Message | None = None

    def embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🐺 狼人杀大厅",
            description=(
                f"点击 **加入** 入座，房主点 **开始游戏** 即可开局。\n"
                f"人数不足会自动用 🤖AI NPC 补位到 **{config.TOTAL_PLAYERS}** 人。\n\n"
                f"📋 {summarize_distribution(config.TOTAL_PLAYERS)}"
            ),
            color=0x5865F2,
        )
        humans = self.state.humans
        if humans:
            roster = "\n".join(f"{i+1}. {p.mention}" for i, p in enumerate(humans))
        else:
            roster = "（还没有人加入）"
        e.add_field(name=f"已加入玩家（{len(humans)}）", value=roster, inline=False)
        e.set_footer(text="房主：点『开始游戏』开局")
        return e

    async def refresh(self):
        if self.message:
            await self.message.edit(embed=self.embed(), view=self)

    @discord.ui.button(label="加入", style=discord.ButtonStyle.success, emoji="✅")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state.phase is not Phase.LOBBY:
            await interaction.response.send_message("游戏已经开始了。", ephemeral=True)
            return
        ok = self.state.add_human(interaction.user.id, interaction.user.display_name)
        if not ok:
            await interaction.response.send_message("你已经在房间里了。", ephemeral=True)
            return
        await interaction.response.send_message("已加入！", ephemeral=True)
        await self.refresh()

    @discord.ui.button(label="退出", style=discord.ButtonStyle.secondary, emoji="🚪")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state.phase is not Phase.LOBBY:
            await interaction.response.send_message("游戏已经开始了。", ephemeral=True)
            return
        ok = self.state.remove_human(interaction.user.id)
        if not ok:
            await interaction.response.send_message("你不在房间里。", ephemeral=True)
            return
        await interaction.response.send_message("已退出。", ephemeral=True)
        await self.refresh()

    @discord.ui.button(label="开始游戏", style=discord.ButtonStyle.primary, emoji="▶️")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.host_id:
            await interaction.response.send_message("只有房主能开始游戏。", ephemeral=True)
            return
        if self.state.phase is not Phase.LOBBY:
            await interaction.response.send_message("游戏已经开始了。", ephemeral=True)
            return
        if not self.state.humans:
            await interaction.response.send_message(
                "至少要有 1 名真人玩家才能开始。", ephemeral=True
            )
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        # 启动游戏主流程
        channel = interaction.channel
        asyncio.create_task(run_game(self.bot, self.state, channel))


# ============================================================
# 游戏流程
# ============================================================
async def reveal_roles(state: GameState, channel) -> None:
    """开局：贴一个『查看身份』按钮，每个人点了只有自己能看到身份（ephemeral）。"""
    if not state.humans:
        return
    view = RoleRevealView(state, timeout=1800)
    await channel.send(
        "🔍 **身份已分配**，请各位点下面按钮（只有你自己能看到）确认身份。"
        f"\n{REVEAL_SECONDS} 秒后天黑。",
        view=view,
    )
    await asyncio.sleep(REVEAL_SECONDS)


async def run_night(bot: discord.Client, state: GameState, channel) -> None:
    """夜晚：预言家查验 + 狼队击杀（人类走频道内 ephemeral 菜单，NPC 走规则）。"""
    # 需要等待行动的人类（狼 / 预言家）
    expected = {
        p.uid for p in state.alive_players
        if not p.is_npc and p.role in (Role.WEREWOLF, Role.SEER)
    }
    actions = NightActions(expected)

    # ---- NPC 先按规则行动 ----
    for seer in [p for p in state.alive_players if p.role is Role.SEER and p.is_npc]:
        t = npc.seer_check_target(seer, state)
        if t is not None:
            seer.seer_results[t] = bool(state.get(t).role.is_wolf)
    for wolf in [p for p in state.alive_wolves() if p.is_npc]:
        t = npc.wolf_kill_target(state)
        if t is not None:
            actions.wolf_votes[wolf.uid] = t

    # ---- 人类通过频道按钮行动 ----
    if expected:
        view = NightPanelView(state, actions, timeout=config.TURN_SECONDS)
        msg = await channel.send(
            f"🌙 **天黑请闭眼……** 狼人和预言家请点下面按钮行动"
            f"（{config.TURN_SECONDS} 秒，其他人请勿点）。",
            view=view,
        )
        try:
            await asyncio.wait_for(actions.done.wait(), timeout=config.TURN_SECONDS)
        except asyncio.TimeoutError:
            pass
        view.stop()
        for child in view.children:
            child.disabled = True
        await msg.edit(view=view)
    else:
        await channel.send("🌙 **天黑请闭眼……**")

    # ---- 没点的人类自动用规则兜底，保证流程推进 ----
    for wolf in state.alive_wolves():
        if not wolf.is_npc and wolf.uid not in actions.wolf_votes:
            t = npc.wolf_kill_target(state)
            if t is not None:
                actions.wolf_votes[wolf.uid] = t
    for seer in [p for p in state.alive_players if p.role is Role.SEER]:
        if not seer.is_npc and seer.uid not in actions.acted:
            t = npc.seer_check_target(seer, state)
            if t is not None:
                seer.seer_results[t] = bool(state.get(t).role.is_wolf)

    kill_uid = (
        Counter(actions.wolf_votes.values()).most_common(1)[0][0]
        if actions.wolf_votes else None
    )
    if kill_uid == 0:  # 0 = 狼队选择空刀
        kill_uid = None
    state.resolve_night(kill_uid)


async def run_discussion(bot: discord.Client, state: GameState, channel, day_log: list[str]) -> None:
    """白天讨论：每名存活玩家按座位号依次发言。"""
    order = list(state.alive_players)  # players 已按座位号排序
    order_txt = " → ".join(f"{p.seat}号" for p in order)
    await channel.send(
        "☀️ **天亮了，开始讨论。** 按座位号轮流发言：\n"
        f"📋 {order_txt}"
    )
    for player in order:
        if player.is_npc:
            await channel.typing()
            speech = await npc.speak(player, state, day_log)
            await channel.send(f"**{player.label}**：{speech}")
            day_log.append(f"{player.seat}号{player.name}: {speech}")
            await asyncio.sleep(1.2)  # 模拟打字节奏，避免刷屏
        else:
            await channel.send(
                f"🎤 现在轮到 **{player.seat}号** 玩家 {player.mention} 发言，"
                f"请在 **{config.TURN_SECONDS} 秒**内于本频道发言。"
            )
            try:
                msg = await bot.wait_for(
                    "message",
                    timeout=config.TURN_SECONDS,
                    check=lambda m: m.author.id == player.uid
                    and m.channel.id == channel.id,
                )
                day_log.append(f"{player.seat}号{player.name}: {msg.content}")
            except asyncio.TimeoutError:
                await channel.send(f"（{player.label} 超时，跳过发言）")
                day_log.append(f"{player.seat}号{player.name}: （沉默/超时）")


async def run_vote(bot: discord.Client, state: GameState, channel, day_log: list[str]):
    """白天投票放逐。"""
    alive = list(state.alive_players)
    # 0 = 弃权哨兵；放在最前面
    options = [("🙅 弃权（不投票）", "0")] + [(p.label, str(p.uid)) for p in alive]
    human_ids = {p.uid for p in alive if not p.is_npc}

    votes: dict[int, int] = {}
    if human_ids:
        view = VoteView(options, human_ids, config.TURN_SECONDS)
        msg = await channel.send(
            f"🗳️ **投票放逐阶段**（{config.TURN_SECONDS} 秒）！存活玩家请在下方选择：",
            view=view,
        )
        await view.wait()
        votes.update(view.votes)
        for child in view.children:
            child.disabled = True
        await msg.edit(view=view)

    # NPC 按规则投票
    for p in alive:
        if p.is_npc:
            t = npc.vote_target(p, state)
            if t is not None:
                votes[p.uid] = t

    # 分出弃权票（目标 0）与有效票
    real_votes = {v: t for v, t in votes.items() if t != 0}
    abstainers = [state.get(v).label for v, t in votes.items() if t == 0]

    # 公示票型
    if real_votes or abstainers:
        tally = Counter(real_votes.values())
        lines = []
        for target_uid, count in tally.most_common():
            target = state.get(target_uid)
            voters = [state.get(v).label for v, tt in real_votes.items() if tt == target_uid]
            lines.append(f"**{target.label}**：{count} 票（{', '.join(voters)}）")
        if abstainers:
            lines.append(f"🙅 弃权：{len(abstainers)} 票（{', '.join(abstainers)}）")
        await channel.send("📊 投票结果：\n" + "\n".join(lines))

    exiled, tie = state.resolve_votes(real_votes)
    if not real_votes:
        await channel.send("🤐 全员弃权，本轮无人被放逐。")
        day_log.append("本轮全员弃权，无人出局。")
    elif tie or exiled is None:
        await channel.send("⚖️ 平票，本轮无人被放逐。")
        day_log.append("投票平票，无人出局。")
    else:
        await channel.send(
            f"🔨 **{exiled.label}** 被放逐出局，身份是 {exiled.role.emoji}**{exiled.role.cn}**。"
        )
        day_log.append(f"{exiled.name} 被投票放逐，身份是{exiled.role.cn}。")


async def announce_end(channel, state: GameState) -> None:
    winner = state.winner
    if winner is Team.WOLF:
        title = "🐺 狼人阵营获胜！"
        color = 0xED4245
    else:
        title = "🎉 好人阵营获胜！"
        color = 0x57F287
    e = discord.Embed(title=title, description=state.public_roles_reveal(), color=color)
    await channel.send(embed=e)


async def run_game(bot: discord.Client, state: GameState, channel) -> None:
    """整局游戏主循环。"""
    try:
        # 1) NPC 补位
        target = max(config.TOTAL_PLAYERS, len(state.humans))
        need = target - len(state.players)
        if need > 0:
            existing = {p.name for p in state.players}
            state.players.extend(npc.make_npcs(need, existing))

        if len(state.players) < 3:
            await channel.send("⚠️ 人数不足（至少 3 人），游戏取消。")
            return

        # 2) 分配角色 + 频道内 ephemeral 查看身份
        state.assign_roles()
        e = discord.Embed(
            title="🎬 游戏开始！",
            description=(
                f"本局共 **{len(state.players)}** 人\n"
                f"{summarize_distribution(len(state.players))}\n\n"
                + "\n".join(f"{p.label}" for p in state.players)
            ),
            color=0x5865F2,
        )
        e.set_footer(text="点下方『查看我的身份』确认身份（只有你自己可见）")
        await channel.send(embed=e)

        await reveal_roles(state, channel)

        # 3) 昼夜循环
        day_log: list[str] = []
        while True:
            await run_night(bot, state, channel)
            victim = state.last_killed
            if victim:
                await channel.send(f"🌅 天亮了。昨晚 **{victim.label}** 倒下了。")
                day_log.append(f"第{state.day_count}晚，{victim.name}被杀。")
            else:
                await channel.send("🌅 天亮了。昨晚是平安夜，无人死亡。")
                day_log.append(f"第{state.day_count}晚，平安夜。")

            if state.check_winner():
                break

            await run_discussion(bot, state, channel, day_log)
            await run_vote(bot, state, channel, day_log)

            if state.check_winner():
                break

        await announce_end(channel, state)
    except Exception:
        log.exception("游戏运行出错")
        await channel.send("❌ 游戏出现内部错误，已结束本局。")
    finally:
        games.pop(state.channel_id, None)


# ============================================================
# Bot 与命令
# ============================================================
class WerewolfBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # 读取白天发言需要
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


client = WerewolfBot()
werewolf = app_commands.Group(name="werewolf", description="狼人杀游戏")


@werewolf.command(name="new", description="在本频道开一局狼人杀")
async def new_game(interaction: discord.Interaction):
    cid = interaction.channel_id
    if cid in games:
        await interaction.response.send_message(
            "本频道已经有一局进行中（或正在等待开始）。用 `/werewolf cancel` 可取消。",
            ephemeral=True,
        )
        return
    state = GameState(channel_id=cid, host_id=interaction.user.id)
    state.add_human(interaction.user.id, interaction.user.display_name)
    games[cid] = state
    view = LobbyView(client, state)
    await interaction.response.send_message(embed=view.embed(), view=view)
    view.message = await interaction.original_response()


@werewolf.command(name="cancel", description="取消本频道当前这一局（仅房主）")
async def cancel_game(interaction: discord.Interaction):
    cid = interaction.channel_id
    state = games.get(cid)
    if state is None:
        await interaction.response.send_message("本频道没有进行中的游戏。", ephemeral=True)
        return
    if interaction.user.id != state.host_id:
        await interaction.response.send_message("只有房主能取消游戏。", ephemeral=True)
        return
    games.pop(cid, None)
    await interaction.response.send_message("🛑 本局已取消。")


@werewolf.command(name="status", description="查看本频道游戏状态")
async def status(interaction: discord.Interaction):
    state = games.get(interaction.channel_id)
    if state is None:
        await interaction.response.send_message("本频道没有进行中的游戏。", ephemeral=True)
        return
    await interaction.response.send_message(
        f"阶段：**{state.phase.value}**　玩家数：**{len(state.players)}**　"
        f"存活：**{len(state.alive_players)}**",
        ephemeral=True,
    )


@werewolf.command(name="clear", description="清理本频道最近的狼人杀消息")
@app_commands.describe(count="要扫描清理的最近消息条数（默认 100，最多 200）")
async def clear(interaction: discord.Interaction, count: int = 100):
    if interaction.channel_id in games:
        await interaction.response.send_message(
            "本频道还有一局进行中，请先用 `/werewolf cancel` 结束，再清理。",
            ephemeral=True,
        )
        return
    count = max(1, min(count, 200))
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    me_id = interaction.client.user.id

    def is_mine(m: discord.Message) -> bool:
        return m.author.id == me_id

    # 优先批量删除（需要『管理消息』权限）；权限不足时退化为逐条删自己的消息
    try:
        deleted = await channel.purge(limit=count, check=is_mine)
        n = len(deleted)
    except discord.Forbidden:
        n = 0
        async for m in channel.history(limit=count):
            if is_mine(m):
                try:
                    await m.delete()
                    n += 1
                except discord.HTTPException:
                    pass
    await interaction.followup.send(f"🧹 已清理 {n} 条狼人杀消息。", ephemeral=True)


client.tree.add_command(werewolf)


@client.event
async def on_ready():
    log.info("已登录为 %s（id=%s）", client.user, client.user.id)


def run() -> None:
    missing = config.validate()
    if missing:
        raise SystemExit(
            "缺少必要配置：" + ", ".join(missing) + "\n请复制 .env.example 为 .env 并填写。"
        )
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    run()
