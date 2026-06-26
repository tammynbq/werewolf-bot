"""Discord 狼人杀 bot —— 面板模式交互层。

设计要点：
- **全程单一面板**：一条 embed 消息随阶段更新（身份确认 → 天黑 → 预言家 → 狼人 → 女巫
  → 天亮 → 发言 → 投票 → 结算）。
- **统一禁言**：游戏频道里所有人默认不能直接打字（消息会被删），一切通过面板按钮进行；
  轮到你说话时，点面板按钮弹出输入框打字，提交后大家才看到。
- **狼人私密线程**：狼队有专属的私密频道，夜里可看到队友并实时商量，NPC 狼也会发言。

核心规则在 game/ 包，NPC 在 npc.py，LLM 在 llm.py。
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

REVEAL_SECONDS = config.REVEAL_SECONDS
TURN = config.TURN_SECONDS
SPEAK = config.SPEAK_SECONDS  # 真人打字发言/遗言的时限（给慢手留足时间）
VOTE = config.VOTE_SECONDS    # 投票时限（太短视图会超时变死、点了就交互失败）

# 子区慢速模式：Discord 慢速上限为 6 小时（21600 秒）。开到最大，
# 配合面板模式的统一禁言，确保玩家无法在子区里自由打字发言。
THREAD_SLOWMODE_SECONDS = 21600

# 颜色
C_NIGHT = 0x2B2D31
C_DAY = 0xFEE75C
C_INFO = 0x5865F2
C_WIN_WOLF = 0xED4245
C_WIN_GOOD = 0x57F287


# ============================================================
# 面板：一条随阶段更新的 embed 消息
# ============================================================
class Panel:
    def __init__(self, channel):
        self.channel = channel
        self.message: discord.Message | None = None

    async def show(self, *, title: str, desc: str, color: int,
                   view: discord.ui.View | None = None, footer: str | None = None):
        embed = discord.Embed(title=title, description=desc, color=color)
        if footer:
            embed.set_footer(text=footer)
        if self.message is None:
            self.message = await self.channel.send(embed=embed, view=view)
        else:
            # view=None 时清掉旧按钮，避免上一个阶段的按钮残留可点
            await self.message.edit(embed=embed, view=view)


def roster_block(state: GameState) -> str:
    """存活玩家清单（带座位号），夜晚/白天面板都用。"""
    lines = []
    for p in state.players:
        mark = "🟢" if p.alive else "⚫"
        lines.append(f"{mark} {p.label}")
    return "\n".join(lines)


async def wait_event(event: asyncio.Event, timeout: int) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def dm_user(uid: int, text: str) -> None:
    """私信提醒某个真人玩家（如『轮到你发言了』）。

    匿名模式下不在公开频道 @ 人，改用私信单独提示当事人，既给提醒又不暴露
    座位号背后是谁。私信关闭/失败时静默忽略。
    """
    try:
        user = client.get_user(uid) or await client.fetch_user(uid)
        if user is not None:
            await user.send(text)
    except discord.HTTPException:
        pass


# ============================================================
# 身份确认面板
# ============================================================
class RoleRevealView(discord.ui.View):
    """每个人点按钮只给自己看身份；全部真人确认后自动天黑。"""

    def __init__(self, state: GameState, panel: Panel, done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self.state = state
        self.panel = panel
        self.done = done
        self.confirmed: set[int] = set()

    @discord.ui.button(label="查看我的身份", style=discord.ButtonStyle.primary, emoji="🔍")
    async def reveal(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.state.get(interaction.user.id)
        if p is None or p.role is None:
            await interaction.response.send_message("你不在本局游戏里。", ephemeral=True)
            return
        text = (f"你的座位号：**{p.seat}号**（全程匿名，大家只看得到座位号）\n"
                f"你的身份：{p.role.emoji} **{p.role.cn}**\n{p.role.description}")
        if p.role is Role.WEREWOLF:
            mates = [m.label for m in self.state.players
                     if m.role is Role.WEREWOLF and m.uid != p.uid]
            text += "\n\n🐺 你的狼队友：" + ("、".join(mates) if mates else "无（你是独狼）")
            text += "\n夜里点面板的『狼人行动』即可进入狼人频道和队友商量。"
        await interaction.response.send_message(text, ephemeral=True)

        self.confirmed.add(p.uid)
        humans = {h.uid for h in self.state.humans}
        await self._refresh(len(humans))
        if humans and self.confirmed >= humans:
            self.done.set()

    async def _refresh(self, total_humans: int):
        try:
            await self.panel.show(
                title="🎬 游戏开始 · 确认身份",
                desc=(f"请各位点下方按钮查看自己的身份（只有你自己可见）。\n"
                      f"全部真人确认后将自动天黑。\n\n{roster_block(self.state)}"),
                color=C_INFO,
                view=self,
                footer=f"已确认 {len(self.confirmed)}/{total_humans} 名真人玩家",
            )
        except discord.HTTPException:
            pass


# ============================================================
# 夜晚 · 预言家
# ============================================================
class SeerCheckSelect(discord.ui.Select):
    def __init__(self, seer, state: GameState, done: asyncio.Event):
        options = [discord.SelectOption(label=p.label, value=str(p.uid))
                   for p in state.alive_players if p.uid != seer.uid]
        super().__init__(placeholder="选择今晚查验的对象…", min_values=1, max_values=1, options=options)
        self._seer = seer
        self._state = state
        self._done = done

    async def callback(self, interaction: discord.Interaction):
        target = self._state.get(int(self.values[0]))
        is_wolf = bool(target.role and target.role.is_wolf)
        self._seer.seer_results[target.uid] = is_wolf
        verdict = "🐺 狼人" if is_wolf else "✅ 好人"
        self._done.set()
        await interaction.response.edit_message(
            content=f"🔮 查验结果：**{target.label}** 是 {verdict}", view=None
        )


class SeerGateView(discord.ui.View):
    def __init__(self, state: GameState, done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self.state = state
        self.done = done

    @discord.ui.button(label="预言家查验", style=discord.ButtonStyle.primary, emoji="🔮")
    async def act(self, interaction: discord.Interaction, button: discord.ui.Button):
        seer = self.state.alive_seer()
        if seer is None or seer.uid != interaction.user.id:
            await interaction.response.send_message(
                "🌙 天黑了，请闭眼等待预言家行动。", ephemeral=True
            )
            return
        view = discord.ui.View(timeout=self.timeout)
        view.add_item(SeerCheckSelect(seer, self.state, self.done))
        await interaction.response.send_message(
            "🔮 你是**预言家**，选择今晚查验的对象：", view=view, ephemeral=True
        )


# ============================================================
# 夜晚 · 狼人
# ============================================================
class WolfKillSelect(discord.ui.Select):
    def __init__(self, wolf, state: GameState, votes: dict[int, int],
                 expected: set[int], done: asyncio.Event):
        # 允许自刀：狼队友和自己也可选（骗解药/搏信任的经典战术），但加标注以免误点。
        options = [discord.SelectOption(label="🌙 空刀（今晚不杀人）", value="0")]
        for p in state.alive_players:
            is_wolf = bool(p.role and p.role.is_wolf)
            if p.uid == wolf.uid:
                opt = discord.SelectOption(
                    label=f"{p.label}（🔪自刀·杀自己）", value=str(p.uid), emoji="🐺")
            elif is_wolf:
                opt = discord.SelectOption(
                    label=f"{p.label}（🐺狼队友）", value=str(p.uid), emoji="🐺")
            else:
                opt = discord.SelectOption(label=p.label, value=str(p.uid))
            options.append(opt)
        super().__init__(placeholder="选择今晚击杀的目标…", min_values=1, max_values=1, options=options)
        self._wolf = wolf
        self._state = state
        self._votes = votes
        self._expected = expected
        self._done = done

    async def callback(self, interaction: discord.Interaction):
        val = int(self.values[0])
        self._votes[self._wolf.uid] = val
        if val == 0:
            content = "🐺 你选择**今晚空刀**。（可重选覆盖）"
        else:
            content = f"🐺 你选择击杀 **{self._state.get(val).label}**。（可重选覆盖）"
        if self._expected and self._expected <= set(self._votes.keys()):
            self._done.set()
        await interaction.response.edit_message(content=content, view=None)


class WolfGateView(discord.ui.View):
    """夜晚面板上的『狼人行动』按钮：进入狼人私密频道 + 选刀。"""

    def __init__(self, bot, state: GameState, votes: dict[int, int],
                 expected: set[int], done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.state = state
        self.votes = votes
        self.expected = expected
        self.done = done

    @discord.ui.button(label="狼人行动", style=discord.ButtonStyle.danger, emoji="🐺")
    async def act(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.state.get(interaction.user.id)
        if p is None or not p.alive or p.role is not Role.WEREWOLF:
            await interaction.response.send_message(
                "🌙 天黑了，请闭眼等待。", ephemeral=True
            )
            return
        mates = [m for m in self.state.alive_wolves() if m.uid != p.uid]
        mate_txt = "、".join(m.label for m in mates) if mates else "无（你是独狼）"
        thread = self.bot.get_channel(self.state.wolf_thread_id) if self.state.wolf_thread_id else None
        thread_hint = f"\n👉 去 {thread.mention} 和队友商量。" if isinstance(thread, discord.Thread) else ""
        try:
            if isinstance(thread, discord.Thread):
                await thread.add_user(interaction.user)
        except discord.HTTPException:
            pass
        view = discord.ui.View(timeout=self.timeout)
        view.add_item(WolfKillSelect(p, self.state, self.votes, self.expected, self.done))
        await interaction.response.send_message(
            f"🐺 你是**狼人**，队友：{mate_txt}。{thread_hint}\n选择今晚击杀目标：",
            view=view, ephemeral=True,
        )


# ============================================================
# 夜晚 · 女巫
# ============================================================
class WitchActionView(discord.ui.View):
    """女巫私有面板：解药救人 / 毒药毒人 / 不行动。"""

    def __init__(self, witch, state: GameState, victim, result: dict, done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self.witch = witch
        self.state = state
        self.victim = victim
        self.result = result  # {"heal": bool, "poison": uid|None}
        self.done = done

        if witch.has_heal and victim is not None:
            self.add_item(self._HealButton(self))
        if witch.has_poison:
            self.add_item(self._PoisonSelect(self))
        self.add_item(self._SkipButton(self))

    class _HealButton(discord.ui.Button):
        # 注意：不要用 self.parent —— discord.py 2.4+ 的 UI 组件已有只读属性 `parent`，
        # 赋值会抛 AttributeError 导致整个女巫面板构造失败（表现为「交互失败」）。
        def __init__(self, owner):
            super().__init__(label=f"用解药救 {owner.victim.label}", style=discord.ButtonStyle.success, emoji="💉")
            self.owner = owner

        async def callback(self, interaction: discord.Interaction):
            self.owner.result["heal"] = True
            self.owner.witch.has_heal = False
            self.owner.done.set()
            await interaction.response.edit_message(
                content=f"💉 你使用了**解药**，救活了 {self.owner.victim.label}。", view=None
            )

    class _PoisonSelect(discord.ui.Select):
        def __init__(self, owner):
            options = [discord.SelectOption(label=p.label, value=str(p.uid))
                       for p in owner.state.alive_players if p.uid != owner.witch.uid]
            super().__init__(placeholder="🧪 用毒药毒死…", min_values=1, max_values=1, options=options)
            self.owner = owner

        async def callback(self, interaction: discord.Interaction):
            uid = int(self.values[0])
            self.owner.result["poison"] = uid
            self.owner.witch.has_poison = False
            self.owner.done.set()
            await interaction.response.edit_message(
                content=f"🧪 你使用了**毒药**，毒死了 {self.owner.state.get(uid).label}。", view=None
            )

    class _SkipButton(discord.ui.Button):
        def __init__(self, owner):
            super().__init__(label="今晚不行动", style=discord.ButtonStyle.secondary, emoji="🙅")
            self.owner = owner

        async def callback(self, interaction: discord.Interaction):
            self.owner.done.set()
            await interaction.response.edit_message(content="🙅 你今晚选择不使用药剂。", view=None)


class WitchGateView(discord.ui.View):
    def __init__(self, state: GameState, victim, result: dict, done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self.state = state
        self.victim = victim
        self.result = result
        self.done = done

    @discord.ui.button(label="女巫行动", style=discord.ButtonStyle.primary, emoji="🧪")
    async def act(self, interaction: discord.Interaction, button: discord.ui.Button):
        witch = self.state.alive_witch()
        if witch is None or witch.uid != interaction.user.id:
            await interaction.response.send_message(
                "🌙 天黑了，请闭眼等待女巫行动。", ephemeral=True
            )
            return
        if self.victim is not None:
            info = f"今晚 **{self.victim.label}** 倒下了。"
        else:
            info = "今晚是平安夜，暂时无人被刀。"
        await interaction.response.send_message(
            f"🧪 你是**女巫**。{info}\n请选择你的行动：",
            view=WitchActionView(witch, self.state, self.victim, self.result, self.done, self.timeout or TURN),
            ephemeral=True,
        )


# ============================================================
# 发言 / 遗言：面板按钮 → 弹窗输入
# ============================================================
class SpeechModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, fut: asyncio.Future):
        super().__init__(title=title)
        self._fut = fut
        self.text = discord.ui.TextInput(
            label=label, style=discord.TextStyle.paragraph,
            max_length=400, required=False, placeholder="在这里输入，提交后大家才能看到…",
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("✅ 已发送。", ephemeral=True)
        if not self._fut.done():
            self._fut.set_result(self.text.value or "")


class SpeechGateView(discord.ui.View):
    """只有当前轮到的玩家能点按钮弹出输入框。"""

    def __init__(self, player, fut: asyncio.Future, btn_label: str, modal_title: str,
                 input_label: str, timeout: int):
        super().__init__(timeout=timeout)
        self.player = player
        self.fut = fut
        self.modal_title = modal_title
        self.input_label = input_label
        # 动态改按钮文字（children[0] 即下面这个按钮）
        self.children[0].label = btn_label

    @discord.ui.button(label="发言", style=discord.ButtonStyle.primary, emoji="✍️")
    async def _btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.player.uid:
            await interaction.response.send_message(
                "⏳ 还没轮到你，请等点到你时再操作。", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            SpeechModal(self.modal_title, self.input_label, self.fut)
        )


# ============================================================
# 投票面板
# ============================================================
class VoteChoiceSelect(discord.ui.Select):
    """投票下拉选单——只放在『私有(ephemeral)消息』里，和夜晚预言家/狼的选单同款套路。

    历史 bug：之前把下拉选单直接挂在公开频道消息上，玩家一选就『交互失败/出了点问题』。
    Discord 里公开消息上的下拉选单交互不稳定，改成『公开按钮 → 弹私有选单(edit_message)』
    这条已被夜晚阶段验证可用的链路即可。
    """

    def __init__(self, voter_id: int, options, label_map: dict[int, str],
                 votes: dict[int, int], allowed_ids: set[int], done: asyncio.Event):
        super().__init__(
            placeholder="选择要放逐的人…", min_values=1, max_values=1,
            options=[discord.SelectOption(label=l, value=v) for l, v in options],
        )
        self._voter = voter_id
        self._label_map = label_map
        self._votes = votes
        self._allowed = allowed_ids
        self._done = done

    async def callback(self, interaction: discord.Interaction):
        try:
            choice = int(self.values[0])
        except (ValueError, IndexError):
            await interaction.response.edit_message(
                content="没读到你的选择，请重新点面板上的『投票』按钮再选一次。", view=None)
            return
        self._votes[self._voter] = choice
        picked = "🙅 弃权（不投票）" if choice == 0 else self._label_map.get(choice, "该玩家")
        await interaction.response.edit_message(
            content=f"🗳️ 已记录：你投了 **{picked}**。（想改票就再点面板上的『投票』按钮）",
            view=None,
        )
        # 所有存活真人都投完了 → 立即结束投票
        if self._allowed and set(self._votes.keys()) >= self._allowed:
            self._done.set()


class VoteGateView(discord.ui.View):
    """公开面板上的投票入口：每个人点『投票』各自弹出私有选单；房主可提前结束。"""

    def __init__(self, options, label_map: dict[int, str], allowed_ids: set[int],
                 host_id: int, votes: dict[int, int], done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self._options = options
        self._label_map = label_map
        self._allowed = allowed_ids
        self._host_id = host_id
        self._votes = votes
        self._done = done

    @discord.ui.button(label="投票", style=discord.ButtonStyle.success, emoji="🗳️")
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self._allowed:
            await interaction.response.send_message("你不是存活玩家，不能投票。", ephemeral=True)
            return
        view = discord.ui.View(timeout=self.timeout)
        view.add_item(VoteChoiceSelect(
            interaction.user.id, self._options, self._label_map,
            self._votes, self._allowed, self._done))
        already = self._votes.get(interaction.user.id)
        tip = ""
        if already is not None:
            picked = "🙅 弃权" if already == 0 else self._label_map.get(already, "该玩家")
            tip = f"（你当前投的是 **{picked}**，重选即可改票）\n"
        await interaction.response.send_message(
            f"{tip}请选择要放逐的人：", view=view, ephemeral=True)

    @discord.ui.button(label="结束投票并公布", style=discord.ButtonStyle.primary, emoji="⏩")
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self._host_id:
            await interaction.response.send_message("只有房主能提前结束投票。", ephemeral=True)
            return
        await interaction.response.send_message("⏩ 提前结束投票，公布结果…", ephemeral=True)
        self._done.set()
        self.stop()


# ============================================================
# 大厅
# ============================================================
class LobbyView(discord.ui.View):
    def __init__(self, bot: discord.Client, state: GameState, thread: discord.Thread | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.state = state
        self.thread = thread
        self.message: discord.Message | None = None

    def embed(self) -> discord.Embed:
        if self.thread is not None:
            where = f"🔒 本局在私密讨论串进行：{self.thread.mention}\n点 **加入** 后才能看到串里的内容。\n\n"
        else:
            where = ""
        e = discord.Embed(
            title="🐺 狼人杀大厅",
            description=(
                f"{where}"
                f"点击 **加入** 入座，房主点 **开始游戏** 即可开局。\n"
                f"人数不足会自动用 🤖AI NPC 补位到 **{config.TOTAL_PLAYERS}** 人。\n"
                f"⚠️ 本局为**面板模式**：全程在面板上行动，平时频道里不能直接打字，"
                f"轮到你时点面板按钮发言/行动。\n\n"
                f"📋 {summarize_distribution(config.TOTAL_PLAYERS)}"
            ),
            color=C_INFO,
        )
        humans = self.state.humans
        roster = "\n".join(f"{i+1}. {p.mention}" for i, p in enumerate(humans)) if humans else "（还没有人加入）"
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
        if self.thread is not None:
            try:
                await self.thread.add_user(interaction.user)
                await interaction.response.send_message(
                    f"已加入！去 {self.thread.mention} 参与游戏 🐺", ephemeral=True)
            except discord.HTTPException:
                await interaction.response.send_message(
                    "已加入！（把你拉进讨论串失败，请确认能看到该串）", ephemeral=True)
        else:
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
        if self.thread is not None:
            try:
                await self.thread.remove_user(interaction.user)
            except discord.HTTPException:
                pass
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
            await interaction.response.send_message("至少要有 1 名真人玩家才能开始。", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        target = self.thread or interaction.channel
        if self.thread is not None:
            await interaction.followup.send(f"▶️ 游戏在 {self.thread.mention} 开始了！", ephemeral=True)
        asyncio.create_task(run_game(self.bot, self.state, target))


# ============================================================
# 狼人私密线程
# ============================================================
def _parent_text_channel(channel):
    if isinstance(channel, discord.Thread):
        return channel.parent
    return channel


async def ensure_wolf_thread(bot, state: GameState, channel) -> discord.Thread | None:
    """为有真人的狼队创建/复用一个私密狼人频道，并把真人狼拉进去。"""
    human_wolves = [w for w in state.alive_wolves() if not w.is_npc]
    if not human_wolves:
        return None
    thread = None
    if state.wolf_thread_id:
        ch = bot.get_channel(state.wolf_thread_id)
        if isinstance(ch, discord.Thread):
            thread = ch
    if thread is None:
        parent = _parent_text_channel(channel)
        if not isinstance(parent, discord.TextChannel):
            return None
        try:
            thread = await parent.create_thread(
                name="🐺 狼人频道",
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=1440,
            )
            state.wolf_thread_id = thread.id
            await thread.send("🐺 这里是**狼人专属频道**，只有狼能看到，可以在这里自由打字商量。")
        except discord.HTTPException:
            return None
    for w in human_wolves:
        member = bot.get_user(w.uid)
        if member:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                pass
    return thread


# ============================================================
# 游戏流程
# ============================================================
async def phase_reveal(state: GameState, panel: Panel) -> None:
    done = asyncio.Event()
    if not state.humans:
        return
    view = RoleRevealView(state, panel, done, timeout=REVEAL_SECONDS + 60)
    await panel.show(
        title="🎬 游戏开始 · 确认身份",
        desc=(f"请各位点下方按钮查看自己的身份（只有你自己可见）。\n"
              f"全部真人确认后将自动天黑。\n\n{roster_block(state)}"),
        color=C_INFO, view=view,
        footer=f"已确认 0/{len(state.humans)} 名真人玩家",
    )
    await wait_event(done, REVEAL_SECONDS + 30)
    view.stop()


async def phase_seer(bot, state: GameState, panel: Panel) -> None:
    # NPC 预言家先行动（AI 选最有价值的目标查验）
    for seer in [p for p in state.alive_players if p.role is Role.SEER and p.is_npc]:
        t = await npc.seer_check_target(seer, state)
        if t is not None:
            seer.seer_results[t] = bool(state.get(t).role.is_wolf)

    seer = state.alive_seer()
    desc = "🔮 **预言家请睁眼**，查验一名玩家的身份。\n其他人请闭眼等待。"
    if seer is None:
        await panel.show(title="🌙 第 %d 夜 · 预言家" % (state.day_count + 1),
                         desc="🔮 预言家请行动……（夜色中似乎没有动静）", color=C_NIGHT)
        await asyncio.sleep(2)
        return
    if seer.is_npc:
        await panel.show(title="🌙 第 %d 夜 · 预言家" % (state.day_count + 1),
                         desc=desc, color=C_NIGHT, footer="预言家正在行动…")
        await asyncio.sleep(2.5)
        return
    done = asyncio.Event()
    view = SeerGateView(state, done, timeout=TURN)
    await panel.show(title="🌙 第 %d 夜 · 预言家" % (state.day_count + 1),
                     desc=desc, color=C_NIGHT, view=view,
                     footer="预言家：点『预言家查验』行动")
    await wait_event(done, TURN)
    view.stop()


async def phase_wolves(bot, state: GameState, panel: Panel, channel) -> int | None:
    """狼人夜晚，返回最终击杀目标 uid（None / 0 = 空刀）。"""
    votes: dict[int, int] = {}
    # NPC 狼先投（AI 选战略目标；全队当晚共用一个刀法）
    for wolf in [w for w in state.alive_wolves() if w.is_npc]:
        t = await npc.wolf_kill_target(state)
        if t is not None:
            votes[wolf.uid] = t

    human_wolves = {w.uid for w in state.alive_wolves() if not w.is_npc}
    title = "🌙 第 %d 夜 · 狼人" % (state.day_count + 1)
    desc = "🐺 **狼人请睁眼**，和队友商量后选择今晚要击杀的目标。\n其他人请闭眼等待。"

    if human_wolves:
        thread = await ensure_wolf_thread(bot, state, channel)
        # NPC 狼在狼人频道里发言商量
        if isinstance(thread, discord.Thread):
            npc_wolves = [w for w in state.alive_wolves() if w.is_npc]
            for nw in npc_wolves:
                mates = [m for m in state.alive_wolves() if m.uid != nw.uid]
                try:
                    line = await npc.wolf_chat(nw, mates, state)
                    if line:  # LLM 不可用时返回空串，本轮就不发狼聊
                        await thread.send(f"🐺 **{nw.label}**：{line}")
                except discord.HTTPException:
                    pass
        done = asyncio.Event()
        view = WolfGateView(bot, state, votes, human_wolves, done, timeout=TURN)
        await panel.show(title=title, desc=desc, color=C_NIGHT, view=view,
                         footer="狼人：点『狼人行动』进入狼人频道并选刀")
        await wait_event(done, TURN)
        view.stop()
    else:
        await panel.show(title=title, desc=desc, color=C_NIGHT, footer="狼人正在行动…")
        await asyncio.sleep(2.5)

    # 没投的真人狼兜底
    for w in state.alive_wolves():
        if w.uid not in votes:
            t = await npc.wolf_kill_target(state)
            if t is not None:
                votes[w.uid] = t

    if not votes:
        return None
    # 配合真人：有真人狼出手时，最终刀谁听真人狼队长的（NPC 配合），否则取多数。
    human_picks = [votes[w.uid] for w in state.alive_wolves()
                   if not w.is_npc and w.uid in votes]
    kill = human_picks[-1] if human_picks else Counter(votes.values()).most_common(1)[0][0]
    return None if kill == 0 else kill


async def phase_witch(bot, state: GameState, panel: Panel, kill_uid: int | None) -> dict:
    """女巫夜晚，返回 {"heal": bool, "poison": uid|None}。"""
    result = {"heal": False, "poison": None}
    witch = state.alive_witch()
    victim = state.get(kill_uid) if kill_uid else None
    title = "🌙 第 %d 夜 · 女巫" % (state.day_count + 1)

    if witch is None:
        return result
    if witch.is_npc:
        heal, poison = await npc.witch_night_action(witch, state, kill_uid)
        if heal:
            witch.has_heal = False
        if poison is not None:
            witch.has_poison = False
        result["heal"], result["poison"] = heal, poison
        await panel.show(title=title, desc="🧪 女巫请行动……", color=C_NIGHT, footer="女巫正在行动…")
        await asyncio.sleep(2.5)
        return result

    done = asyncio.Event()
    view = WitchGateView(state, victim, result, done, timeout=TURN)
    await panel.show(
        title=title,
        desc="🧪 **女巫请睁眼**。点下方按钮查看今晚情况，并决定是否用药。\n其他人请闭眼等待。",
        color=C_NIGHT, view=view, footer="女巫：点『女巫行动』",
    )
    await wait_event(done, TURN)
    view.stop()
    return result


async def collect_last_words(state: GameState, panel: Panel, channel, player) -> None:
    """出局玩家留遗言：NPC 由 LLM 生成；真人通过面板弹窗输入。"""
    if player.is_npc:
        text = await npc.last_word(player, state)
        if text:
            await channel.send(f"🪦 **{player.label}** 的遗言：{text}")
        else:  # LLM 不可用时返回空串，按真人沉默同款措辞处理
            await channel.send(f"（{player.label} 没有留下遗言。）")
        return
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    state.current_speaker_uid = player.uid
    view = SpeechGateView(player, fut, btn_label="留遗言", modal_title="你的遗言",
                          input_label="留下你的遗言（可留空）", timeout=SPEAK)
    await dm_user(player.uid, f"🪦 你（{player.seat}号）出局了，回到游戏面板点『留遗言』留下遗言吧。")
    await panel.show(
        title="🪦 遗言",
        desc=f"**{player.label}** 出局了，请点按钮留下遗言（不限时，留完即继续）。",
        color=C_DAY, view=view,
    )
    try:
        text = await asyncio.wait_for(fut, timeout=SPEAK)
        if text.strip():
            await channel.send(f"🪦 **{player.label}** 的遗言：{text}")  # label 已是匿名座位号
        else:
            await channel.send(f"（{player.label} 没有留下遗言。）")
    except asyncio.TimeoutError:
        await channel.send(f"（{player.label} 没有留下遗言。）")
    finally:
        state.current_speaker_uid = None
        view.stop()


async def announce_deaths(state: GameState, panel: Panel, channel, deaths: list, day_log: list[str]) -> None:
    if not deaths:
        await panel.show(title="🌅 第 %d 天 · 天亮了" % state.day_count,
                         desc="昨晚是**平安夜**，无人死亡。\n\n" + roster_block(state), color=C_DAY)
        day_log.append(f"第{state.day_count}晚，平安夜。")
        await asyncio.sleep(2)
        return
    names = "、".join(d.label for d in deaths)
    await panel.show(title="🌅 第 %d 天 · 天亮了" % state.day_count,
                     desc=f"昨晚倒下的是：**{names}**。\n\n" + roster_block(state), color=C_DAY)
    for d in deaths:
        day_log.append(f"第{state.day_count}晚，{d.seat}号出局。")
    await asyncio.sleep(1.5)
    for d in deaths:
        await collect_last_words(state, panel, channel, d)


async def phase_discussion(bot, state: GameState, panel: Panel, channel, day_log: list[str]) -> None:
    order = list(state.alive_players)
    order_txt = " → ".join(f"{p.seat}号" for p in order)
    for player in order:
        if not player.alive:
            continue
        if player.is_npc:
            state.current_speaker_uid = None
            await panel.show(
                title="☀️ 第 %d 天 · 讨论" % state.day_count,
                desc=f"轮到 **{player.label}** 发言…\n\n📋 顺序：{order_txt}",
                color=C_DAY, footer="按座位号轮流发言",
            )
            # 先生成发言，再按字数模拟「真人打字」的停顿，避免 NPC 秒回暴露身份
            async with channel.typing():
                speech = await npc.speak(player, state, day_log)
                await asyncio.sleep(min(9.0, 1.5 + len(speech) * 0.12))
            if speech:
                await channel.send(f"💬 **{player.label}**：{speech}")
                day_log.append(f"{player.seat}号: {speech}")
            else:  # LLM 不可用时返回空串，标记沉默而不是凑写死台词
                await channel.send(f"（{player.label} 一时没接上话，跳过发言。）")
                day_log.append(f"{player.seat}号: （沉默）")
            await asyncio.sleep(0.6)
        else:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            state.current_speaker_uid = player.uid
            view = SpeechGateView(player, fut, btn_label="我要发言", modal_title="你的发言",
                                  input_label="输入你的发言", timeout=SPEAK)
            await dm_user(player.uid, f"🎤 轮到你（{player.seat}号）发言了，回游戏面板点『我要发言』吧。")
            await panel.show(
                title="☀️ 第 %d 天 · 讨论" % state.day_count,
                desc=(f"🎤 现在轮到 **{player.seat}号** 发言（请对号入座，只有本人能点发言）。\n"
                      f"已私信提醒当事人；点下方按钮输入发言，不限时、发完即继续（其他人请稍候）。\n\n"
                      f"📋 顺序：{order_txt}"),
                color=C_DAY, view=view, footer="点『我要发言』弹出输入框",
            )
            try:
                text = await asyncio.wait_for(fut, timeout=SPEAK)
                if text.strip():
                    await channel.send(f"💬 **{player.label}**：{text}")
                    day_log.append(f"{player.seat}号: {text}")
                else:
                    await channel.send(f"（{player.label} 选择不发言。）")
                    day_log.append(f"{player.seat}号: （沉默）")
            except asyncio.TimeoutError:
                await channel.send(f"（{player.label} 超时，跳过发言）")
                day_log.append(f"{player.seat}号: （沉默/超时）")
            finally:
                state.current_speaker_uid = None
                view.stop()


async def phase_vote(bot, state: GameState, panel: Panel, channel, day_log: list[str]) -> None:
    alive = list(state.alive_players)
    options = [("🙅 弃权（不投票）", "0")] + [(p.label, str(p.uid)) for p in alive]
    human_ids = {p.uid for p in alive if not p.is_npc}

    # value(uid) -> 展示名，给确认提示用
    label_map = {p.uid: p.label for p in alive}

    votes: dict[int, int] = {}
    if human_ids:
        done = asyncio.Event()
        view = VoteGateView(options, label_map, human_ids, state.host_id, votes, done, VOTE)
        await panel.show(
            title="🗳️ 第 %d 天 · 投票放逐" % state.day_count,
            desc="存活玩家请在下方的投票面板里选择要放逐的人。",
            color=C_DAY,
        )
        vote_embed = discord.Embed(
            title="🗳️ 投票放逐",
            description=(f"存活玩家点下方 **🗳️ 投票** 按钮选择要放逐的人（{VOTE} 秒内）。\n"
                        f"全员投完会立即公布（房主也可点『结束投票』提前公布）。"),
            color=C_DAY,
        )
        vote_msg = await channel.send(embed=vote_embed, view=view)
        await wait_event(done, VOTE)
        view.stop()
        try:
            await vote_msg.edit(view=None)  # 投票结束后清掉按钮，避免残留可点
        except discord.HTTPException:
            pass

    for p in alive:
        if p.is_npc:
            t = await npc.vote_decision(p, state, day_log)
            if t is not None:
                votes[p.uid] = t

    real_votes = {v: t for v, t in votes.items() if t != 0}
    abstainers = [state.get(v).label for v, t in votes.items() if t == 0]

    if real_votes or abstainers:
        tally = Counter(real_votes.values())
        lines = []
        for target_uid, count in tally.most_common():
            voters = [state.get(v).label for v, tt in real_votes.items() if tt == target_uid]
            lines.append(f"**{state.get(target_uid).label}**：{count} 票（{', '.join(voters)}）")
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
            f"🔨 **{exiled.label}** 被放逐出局，身份是 {exiled.role.emoji}**{exiled.role.cn}**。")
        day_log.append(f"{exiled.seat}号 被投票放逐，身份是{exiled.role.cn}。")
        await collect_last_words(state, panel, channel, exiled)


async def announce_end(channel, panel: Panel, state: GameState) -> None:
    if state.winner is Team.WOLF:
        title, color = "🐺 狼人阵营获胜！", C_WIN_WOLF
    else:
        title, color = "🎉 好人阵营获胜！", C_WIN_GOOD
    await panel.show(title=title, desc=state.public_roles_reveal(), color=color)


async def run_game(bot: discord.Client, state: GameState, channel) -> None:
    state.play_channel_id = channel.id
    panel = Panel(channel)
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

        # 2) 分配角色
        state.assign_roles()
        await phase_reveal(state, panel)

        # 3) 昼夜循环
        day_log: list[str] = []
        while True:
            await phase_seer(bot, state, panel)
            kill_uid = await phase_wolves(bot, state, panel, channel)
            witch_res = await phase_witch(bot, state, panel, kill_uid)
            deaths = state.resolve_night(kill_uid, witch_res["heal"], witch_res["poison"])

            await announce_deaths(state, panel, channel, deaths, day_log)
            if state.check_winner():
                break

            await phase_discussion(bot, state, panel, channel, day_log)
            await phase_vote(bot, state, panel, channel, day_log)
            if state.check_winner():
                break

        await announce_end(channel, panel, state)
    except Exception:
        log.exception("游戏运行出错")
        await channel.send("❌ 游戏出现内部错误，已结束本局。")
    finally:
        games.pop(state.channel_id, None)
        if isinstance(channel, discord.Thread):
            try:
                await channel.send("🗂️ 本局结束，讨论串已归档收起。")
                await channel.edit(archived=True)
            except discord.HTTPException:
                pass
        # 顺手归档狼人频道
        if state.wolf_thread_id:
            wt = bot.get_channel(state.wolf_thread_id)
            if isinstance(wt, discord.Thread):
                try:
                    await wt.edit(archived=True)
                except discord.HTTPException:
                    pass


# ============================================================
# Bot 与命令
# ============================================================
class WerewolfBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
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
            ephemeral=True)
        return
    state = GameState(channel_id=cid, host_id=interaction.user.id)
    state.add_human(interaction.user.id, interaction.user.display_name)
    games[cid] = state

    thread: discord.Thread | None = None
    channel = interaction.channel
    if isinstance(channel, discord.TextChannel):
        try:
            thread = await channel.create_thread(
                name=f"🐺 狼人杀 · {interaction.user.display_name}",
                type=discord.ChannelType.private_thread,
                invitable=False, auto_archive_duration=1440,
                slowmode_delay=THREAD_SLOWMODE_SECONDS,
            )
            await thread.add_user(interaction.user)
            await thread.send(
                "🐺 这里是本局的**私密房间**，只有点了『加入』的人能看到。\n"
                "房主在频道里点『开始游戏』即可开局，游戏全程在这里以**面板模式**进行。")
            state.thread_id = thread.id
        except discord.HTTPException:
            thread = None

    view = LobbyView(client, state, thread)
    await interaction.response.send_message(embed=view.embed(), view=view)
    view.message = await interaction.original_response()


def _resolve_game(interaction: discord.Interaction) -> tuple[int, GameState | None]:
    ch = interaction.channel
    cid = ch.parent_id if isinstance(ch, discord.Thread) else interaction.channel_id
    return cid, games.get(cid)


@werewolf.command(name="cancel", description="取消本频道当前这一局（仅房主）")
async def cancel_game(interaction: discord.Interaction):
    cid, state = _resolve_game(interaction)
    if state is None:
        await interaction.response.send_message("本频道没有进行中的游戏。", ephemeral=True)
        return
    if interaction.user.id != state.host_id:
        await interaction.response.send_message("只有房主能取消游戏。", ephemeral=True)
        return
    games.pop(cid, None)
    await interaction.response.send_message("🛑 本局已取消。", ephemeral=True)
    if state.thread_id is not None:
        thread = interaction.client.get_channel(state.thread_id)
        if isinstance(thread, discord.Thread):
            try:
                await thread.send("🛑 本局已被房主取消，讨论串归档收起。")
                await thread.edit(archived=True)
            except discord.HTTPException:
                pass


@werewolf.command(name="status", description="查看本频道游戏状态")
async def status(interaction: discord.Interaction):
    _cid, state = _resolve_game(interaction)
    if state is None:
        await interaction.response.send_message("本频道没有进行中的游戏。", ephemeral=True)
        return
    await interaction.response.send_message(
        f"阶段：**{state.phase.value}**　玩家数：**{len(state.players)}**　"
        f"存活：**{len(state.alive_players)}**", ephemeral=True)


@werewolf.command(name="clear", description="清理本频道最近的狼人杀消息")
@app_commands.describe(count="要扫描清理的最近消息条数（默认 100，最多 200）")
async def clear(interaction: discord.Interaction, count: int = 100):
    if interaction.channel_id in games:
        await interaction.response.send_message(
            "本频道还有一局进行中，请先用 `/werewolf cancel` 结束，再清理。", ephemeral=True)
        return
    count = max(1, min(count, 200))
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    me_id = interaction.client.user.id

    def is_mine(m: discord.Message) -> bool:
        return m.author.id == me_id

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


@client.event
async def on_message(message: discord.Message):
    """面板模式：游戏频道里统一禁言——删掉所有人直接发的消息（狼人频道除外）。"""
    if message.author.bot:
        return
    # 找到一局正在用这个频道进行游戏的 state
    state = next((s for s in games.values() if s.play_channel_id == message.channel.id), None)
    if state is None:
        return
    # 狼人专属频道不禁言
    if state.wolf_thread_id and message.channel.id == state.wolf_thread_id:
        return
    try:
        await message.delete()
        await message.channel.send(
            f"🤫 {message.author.mention} 本局是面板模式，请通过上方面板按钮行动/发言。",
            delete_after=4,
        )
    except discord.HTTPException:
        pass


def run() -> None:
    missing = config.validate()
    if missing:
        raise SystemExit(
            "缺少必要配置：" + ", ".join(missing) + "\n请复制 .env.example 为 .env 并填写。")
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    run()
