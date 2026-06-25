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


# ============================================================
# UI 组件
# ============================================================
class ChoiceSelect(discord.ui.Select):
    """DM 私聊里的单选（夜晚行动用）。"""

    def __init__(self, options: list[tuple[str, str]], future: asyncio.Future):
        super().__init__(
            placeholder="做出你的选择…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=l, value=v) for l, v in options],
        )
        self._future = future

    async def callback(self, interaction: discord.Interaction):
        if not self._future.done():
            self._future.set_result(self.values[0])
        await interaction.response.send_message("✅ 已选择。", ephemeral=True)


async def prompt_choice(
    bot: discord.Client,
    uid: int,
    content: str,
    options: list[tuple[str, str]],
    timeout: int,
) -> str | None:
    """私聊一名玩家，给出选择菜单，返回所选 value（超时/失败返回 None）。"""
    if not options:
        return None
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    view = discord.ui.View(timeout=timeout)
    view.add_item(ChoiceSelect(options, future))
    try:
        user = await bot.fetch_user(uid)
        dm = await user.create_dm()
        await dm.send(content, view=view)
    except discord.HTTPException:
        return None
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        view.stop()


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
async def dm_role(bot: discord.Client, state: GameState, player) -> None:
    """私聊告知人类玩家身份（狼人附带队友）。"""
    role = player.role
    text = f"# 你的身份：{role.emoji} {role.cn}\n{role.description}"
    if role is Role.WEREWOLF:
        mates = [
            p.label for p in state.players
            if p.role is Role.WEREWOLF and p.uid != player.uid
        ]
        if mates:
            text += "\n\n🐺 你的狼队友：" + "、".join(mates)
        else:
            text += "\n\n🐺 你是本局唯一的狼。"
    try:
        user = await bot.fetch_user(player.uid)
        dm = await user.create_dm()
        await dm.send(text)
    except discord.HTTPException:
        log.warning("无法私聊玩家 %s 的身份（可能关闭了私信）", player.name)


async def run_night(bot: discord.Client, state: GameState, channel) -> None:
    """夜晚：预言家查验 + 狼队击杀。"""
    await channel.send("🌙 **天黑请闭眼……** 狼人和预言家正在行动。")

    # ---- 预言家查验 ----
    for seer in [p for p in state.alive_players if p.role is Role.SEER]:
        if seer.is_npc:
            target_uid = npc.seer_check_target(seer, state)
        else:
            opts = [
                (p.label, str(p.uid))
                for p in state.alive_players if p.uid != seer.uid
            ]
            choice = await prompt_choice(
                bot, seer.uid, "🔮 你是**预言家**，选择今晚要查验的玩家：",
                opts, config.TURN_SECONDS,
            )
            target_uid = int(choice) if choice else npc.seer_check_target(seer, state)
        if target_uid is not None:
            target = state.get(target_uid)
            if target:
                is_wolf = bool(target.role and target.role.is_wolf)
                seer.seer_results[target_uid] = is_wolf
                if not seer.is_npc:
                    verdict = "🐺 狼人" if is_wolf else "✅ 好人"
                    try:
                        user = await bot.fetch_user(seer.uid)
                        await (await user.create_dm()).send(
                            f"🔮 查验结果：**{target.label}** 是 {verdict}"
                        )
                    except discord.HTTPException:
                        pass

    # ---- 狼队击杀 ----
    wolf_votes: list[int] = []
    for wolf in state.alive_wolves():
        if wolf.is_npc:
            t = npc.wolf_kill_target(state)
        else:
            opts = [
                (p.label, str(p.uid))
                for p in state.alive_players
                if not (p.role and p.role.is_wolf)
            ]
            choice = await prompt_choice(
                bot, wolf.uid, "🐺 你是**狼人**，选择今晚要击杀的玩家：",
                opts, config.TURN_SECONDS,
            )
            t = int(choice) if choice else npc.wolf_kill_target(state)
        if t is not None:
            wolf_votes.append(t)

    kill_uid = Counter(wolf_votes).most_common(1)[0][0] if wolf_votes else None
    state.resolve_night(kill_uid)


async def run_discussion(bot: discord.Client, state: GameState, channel, day_log: list[str]) -> None:
    """白天讨论：每名存活玩家依次发言。"""
    await channel.send("☀️ **天亮了，开始讨论。** 轮流发言，说说你的看法。")
    for player in list(state.alive_players):
        if player.is_npc:
            await channel.typing()
            speech = await npc.speak(player, state, day_log)
            await channel.send(f"**{player.label}**：{speech}")
            day_log.append(f"{player.name}: {speech}")
            await asyncio.sleep(1.2)  # 模拟打字节奏，避免刷屏
        else:
            await channel.send(
                f"🎤 轮到 {player.mention} 发言，请在 **{config.TURN_SECONDS} 秒**内于本频道发言。"
            )
            try:
                msg = await bot.wait_for(
                    "message",
                    timeout=config.TURN_SECONDS,
                    check=lambda m: m.author.id == player.uid
                    and m.channel.id == channel.id,
                )
                day_log.append(f"{player.name}: {msg.content}")
            except asyncio.TimeoutError:
                await channel.send(f"（{player.label} 超时，跳过发言）")
                day_log.append(f"{player.name}: （沉默/超时）")


async def run_vote(bot: discord.Client, state: GameState, channel, day_log: list[str]):
    """白天投票放逐。"""
    alive = list(state.alive_players)
    options = [(p.label, str(p.uid)) for p in alive]
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

    # 公示票型
    if votes:
        tally = Counter(votes.values())
        lines = []
        for target_uid, count in tally.most_common():
            target = state.get(target_uid)
            voters = [state.get(v).label for v, tt in votes.items() if tt == target_uid]
            lines.append(f"**{target.label}**：{count} 票（{', '.join(voters)}）")
        await channel.send("📊 投票结果：\n" + "\n".join(lines))

    exiled, tie = state.resolve_votes(votes)
    if tie or exiled is None:
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

        # 2) 分配角色 + 私聊身份
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
        e.set_footer(text="人类玩家请查收私信确认身份～")
        await channel.send(embed=e)

        for p in state.humans:
            await dm_role(bot, state, p)

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
