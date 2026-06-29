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
import random
from collections import Counter

import discord
from discord import app_commands

import config
import llm
import npc
import userapi
from characters import CHARACTER_NPCS
from game.roles import BOARD_NAMES, Role, summarize_distribution
from game.state import GameState, Phase, Team
import database

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

    def _buried(self) -> bool:
        """面板下方是否又冒出了别的消息（发言/结算/遗言等），把面板顶上去了。

        面板从始至终是同一条消息、靠 edit 原地更新；可玩家发言、夜晚结算等都是
        新发的消息，会堆在面板下面，导致面板被埋在上面、每次操作都得往上翻。
        用频道最后一条消息是不是面板自己来判断面板有没有被顶上去。
        """
        if self.message is None:
            return False
        last_id = getattr(self.channel, "last_message_id", None)
        return last_id is not None and last_id != self.message.id

    async def ensure_bottom(self) -> None:
        """若面板已被下面的消息顶上去，删掉旧的，下次 show() 会重新发到频道底部。

        在「接下来全靠面板就地刷新、不再发新消息」的环节（如白天发言）开始前调用一次，
        让面板先归位到最底部，之后整段就地编辑都稳稳停在底部，玩家不用再往上翻。
        """
        if self.message is not None and self._buried():
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass
            self.message = None

    async def show(self, *, title: str, desc: str, color: int,
                   view: discord.ui.View | None = None, footer: str | None = None):
        embed = discord.Embed(title=title, description=desc, color=color)
        if footer:
            embed.set_footer(text=footer)
        # 有按钮要点、且面板已被下面的消息顶上去时，把旧面板删掉、重新发到频道最底部，
        # 省得玩家每次都要往上翻找按钮。纯展示更新（view=None）或面板本就在底部时就地
        # 编辑，避免频繁删发刷屏、闪烁。
        if self.message is not None and view is not None and self._buried():
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass
            self.message = None
        if self.message is None:
            self.message = await self.channel.send(embed=embed, view=view)
        else:
            # view=None 时清掉旧按钮，避免上一个阶段的按钮残留可点
            await self.message.edit(embed=embed, view=view)


def _speech_preview(text: str, limit: int = 350) -> str:
    """把发言压成一行显示在灯旁边；过长则截断，避免 embed 超长。"""
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def roster_block(state: GameState, *, speeches: bool = False,
                 speaking_uid: int | None = None) -> str:
    """存活玩家清单（带座位号）：🟢存活 / ⚫出局，夜晚/白天面板都用。

    speeches=True 时把每人本轮的发言/遗言直接显示在灯旁边（面板模式：发言不再
    另发消息往下堆），speaking_uid 指向的玩家显示「发言中…」。
    """
    lines = []
    for p in state.players:
        mark = "🟢" if p.alive else "⚫"
        badge = "👑" if p.is_sheriff else ""
        line = f"{mark}{badge} {p.label}"
        if speeches:
            if p.uid == speaking_uid:
                line += "：💬 发言中…"
            elif p.last_speech:
                line += f"：{_speech_preview(p.last_speech)}"
        lines.append(line)
    return "\n".join(lines)


def _npc_night_delay() -> float:
    """NPC 担任神职/狼时，假装真人在『睁眼→思考→点按钮』的随机延迟（秒）。

    夜晚行动如果 NPC 一律秒过（固定 2.5 秒），旁观者一眼就能看出某神职是 AI，
    进而反推谁是真人。这里给个随机的、像真人操作时长的停顿来打掩护。
    """
    return random.uniform(7.0, 17.0)


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
# 夜晚 · 守卫
# ============================================================
class GuardProtectSelect(discord.ui.Select):
    def __init__(self, guard, state: GameState, result: dict, done: asyncio.Event):
        options = []
        for p in state.alive_players:
            if p.uid == guard.last_guard_uid:
                continue  # 不能连续两晚守同一人
            label = f"{p.label}（🛡️守自己）" if p.uid == guard.uid else p.label
            options.append(discord.SelectOption(label=label, value=str(p.uid)))
        super().__init__(placeholder="选择今晚守护的对象…", min_values=1, max_values=1, options=options)
        self._guard = guard
        self._state = state
        self._result = result
        self._done = done

    async def callback(self, interaction: discord.Interaction):
        uid = int(self.values[0])
        self._result["uid"] = uid
        target = self._state.get(uid)
        self._done.set()
        await interaction.response.edit_message(
            content=f"🛡️ 今晚你守护了 **{target.label}**。", view=None)


class GuardGateView(discord.ui.View):
    def __init__(self, state: GameState, result: dict, done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self.state = state
        self.result = result
        self.done = done

    @discord.ui.button(label="守卫行动", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def act(self, interaction: discord.Interaction, button: discord.ui.Button):
        guard = self.state.alive_guard()
        if guard is None or guard.uid != interaction.user.id:
            await interaction.response.send_message(
                "🌙 天黑了，请闭眼等待守卫行动。", ephemeral=True)
            return
        view = discord.ui.View(timeout=self.timeout)
        view.add_item(GuardProtectSelect(guard, self.state, self.result, self.done))
        await interaction.response.send_message(
            "🛡️ 你是**守卫**，选择今晚守护的对象（不能连守同一人，可守自己）：",
            view=view, ephemeral=True)


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
# 警长竞选
# ============================================================
class SheriffRunView(discord.ui.View):
    """警长竞选报名阶段：存活玩家点按钮决定上警/不上警。"""

    def __init__(self, state: GameState, panel: Panel, candidates: set[int],
                 decided: set[int], human_ids: set[int], done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self.state = state
        self.panel = panel
        self.candidates = candidates
        self.decided = decided
        self.human_ids = human_ids
        self.done = done

    @discord.ui.button(label="上警", style=discord.ButtonStyle.success, emoji="🎖️")
    async def run_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in self.human_ids:
            await interaction.response.send_message("你不是存活玩家。", ephemeral=True)
            return
        self.candidates.add(uid)
        self.decided.add(uid)
        await interaction.response.send_message("🎖️ 你已报名竞选警长！", ephemeral=True)
        if self.decided >= self.human_ids:
            self.done.set()

    @discord.ui.button(label="不上警", style=discord.ButtonStyle.secondary, emoji="🙅")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in self.human_ids:
            await interaction.response.send_message("你不是存活玩家。", ephemeral=True)
            return
        self.candidates.discard(uid)
        self.decided.add(uid)
        await interaction.response.send_message("🙅 你选择不参与竞选。", ephemeral=True)
        if self.decided >= self.human_ids:
            self.done.set()


class SheriffVoteSelect(discord.ui.Select):
    """投票选警长——候选人之外的存活玩家投票。"""

    def __init__(self, voter_id: int, candidates: list, votes: dict[int, int],
                 allowed_ids: set[int], done: asyncio.Event):
        options = [discord.SelectOption(label=f"{c.label}", value=str(c.uid)) for c in candidates]
        super().__init__(placeholder="选择你支持的警长候选人…", min_values=1, max_values=1, options=options)
        self._voter = voter_id
        self._votes = votes
        self._allowed = allowed_ids
        self._done = done

    async def callback(self, interaction: discord.Interaction):
        choice = int(self.values[0])
        self._votes[self._voter] = choice
        await interaction.response.edit_message(
            content=f"🗳️ 已投票！（想改票就再点面板上的投票按钮）", view=None)
        if self._allowed and set(self._votes.keys()) >= self._allowed:
            self._done.set()


class SheriffVoteGateView(discord.ui.View):
    """竞选投票入口：非候选人点按钮弹出选单。"""

    def __init__(self, candidates: list, allowed_ids: set[int],
                 host_id: int, votes: dict[int, int], done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self._candidates = candidates
        self._allowed = allowed_ids
        self._host_id = host_id
        self._votes = votes
        self._done = done

    @discord.ui.button(label="投票选警长", style=discord.ButtonStyle.success, emoji="🗳️")
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in self._allowed:
            await interaction.response.send_message("你是候选人或非存活玩家，不能投票。", ephemeral=True)
            return
        view = discord.ui.View(timeout=self.timeout)
        view.add_item(SheriffVoteSelect(uid, self._candidates, self._votes, self._allowed, self._done))
        await interaction.response.send_message("请选择你支持的警长候选人：", view=view, ephemeral=True)

    @discord.ui.button(label="结束投票", style=discord.ButtonStyle.primary, emoji="⏩")
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self._host_id:
            await interaction.response.send_message("只有房主能提前结束投票。", ephemeral=True)
            return
        await interaction.response.send_message("⏩ 提前结束投票…", ephemeral=True)
        self._done.set()
        self.stop()


class SheriffTransferSelect(discord.ui.Select):
    """警长出局时选择移交警徽。"""

    def __init__(self, sheriff, state: GameState, result: dict, done: asyncio.Event):
        options = [discord.SelectOption(label="❌ 撕掉警徽", value="0")]
        options += [discord.SelectOption(label=p.label, value=str(p.uid))
                    for p in state.alive_players if p.uid != sheriff.uid]
        super().__init__(placeholder="选择警徽移交对象…", min_values=1, max_values=1, options=options)
        self._sheriff = sheriff
        self._state = state
        self._result = result
        self._done = done

    async def callback(self, interaction: discord.Interaction):
        val = int(self.values[0])
        self._result["uid"] = val if val != 0 else None
        self._done.set()
        if val == 0:
            await interaction.response.edit_message(content="❌ 你撕掉了警徽！", view=None)
        else:
            target = self._state.get(val)
            await interaction.response.edit_message(
                content=f"👑 你把警徽移交给了 **{target.label}**。", view=None)


class SheriffTransferGateView(discord.ui.View):
    """警长出局时的面板入口。"""

    def __init__(self, sheriff, state: GameState, result: dict, done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self._sheriff = sheriff
        self._state = state
        self._result = result
        self._done = done

    @discord.ui.button(label="移交警徽", style=discord.ButtonStyle.primary, emoji="👑")
    async def act(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self._sheriff.uid:
            await interaction.response.send_message("只有警长本人能操作。", ephemeral=True)
            return
        view = discord.ui.View(timeout=self.timeout)
        view.add_item(SheriffTransferSelect(self._sheriff, self._state, self._result, self._done))
        await interaction.response.send_message(
            "👑 你是警长，出局了——选择把警徽移交给谁，或撕掉警徽：", view=view, ephemeral=True)


async def phase_sheriff_election(bot, state: GameState, panel: Panel, channel, day_log: list[str]) -> None:
    """第一天白天讨论前：警长竞选（上警 → 竞选演讲 → 投票 → 宣布警长）。"""
    title = "🎖️ 第 1 天 · 警长竞选"

    # 1) 报名阶段：NPC 自动决定，真人点按钮
    candidates: set[int] = set()
    decided: set[int] = set()
    human_ids = {p.uid for p in state.alive_players if not p.is_npc}

    for p in state.alive_players:
        if p.is_npc:
            want = await npc.sheriff_want_run(p, state)
            if want:
                candidates.add(p.uid)
            decided.add(p.uid)

    if human_ids:
        done = asyncio.Event()
        view = SheriffRunView(state, panel, candidates, decided, human_ids, done, TURN)
        cand_npc_txt = "、".join(state.get(uid).label for uid in candidates) if candidates else "暂无"
        await panel.show(
            title=title,
            desc=(f"🎖️ **警长竞选报名**\n\n"
                  f"想参与竞选的玩家点『上警』，不想参与点『不上警』。\n"
                  f"已报名：{cand_npc_txt}\n\n{roster_block(state)}"),
            color=C_DAY, view=view,
        )
        await wait_event(done, TURN)
        view.stop()

    cand_players = [p for p in state.alive_players if p.uid in candidates]
    if len(cand_players) == 0:
        await panel.show(title=title, desc="没有人报名竞选，本局不设警长。\n\n" + roster_block(state), color=C_DAY)
        day_log.append("警长竞选：无人上警，本局不设警长。")
        await asyncio.sleep(2)
        return
    if len(cand_players) == 1:
        winner = cand_players[0]
        winner.is_sheriff = True
        state.sheriff_uid = winner.uid
        await panel.show(
            title=title,
            desc=f"只有 **{winner.label}** 报名，自动当选警长 👑\n\n{roster_block(state)}",
            color=C_DAY,
        )
        day_log.append(f"警长竞选：{winner.seat}号自动当选警长。")
        await asyncio.sleep(2)
        return

    # 2) 竞选演讲
    await panel.ensure_bottom()
    for p in state.players:
        p.last_speech = ""
    for player in cand_players:
        if player.is_npc:
            await panel.show(
                title=title,
                desc=roster_block(state, speeches=True, speaking_uid=player.uid),
                color=C_DAY, footer="候选人轮流发表竞选演讲",
            )
            async with channel.typing():
                try:
                    speech = await asyncio.wait_for(
                        npc.sheriff_speech(player, state, cand_players),
                        timeout=config.NPC_THINK_SECONDS)
                except asyncio.TimeoutError:
                    speech = "大家投我吧，我能带好节奏。"
                await asyncio.sleep(min(7.0, 1.5 + len(speech) * 0.12))
            player.last_speech = speech
            day_log.append(f"{player.seat}号(竞选): {speech}")
            await panel.show(title=title, desc=roster_block(state, speeches=True),
                             color=C_DAY, footer="候选人轮流发表竞选演讲")
            await asyncio.sleep(0.6)
        else:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            state.current_speaker_uid = player.uid
            view = SpeechGateView(player, fut, btn_label="竞选演讲", modal_title="你的竞选演讲",
                                  input_label="说说你为什么适合当警长", timeout=SPEAK + 120)
            await dm_user(player.uid, f"🎖️ 轮到你（{player.seat}号）发表竞选演讲了，回游戏面板点按钮。")
            await panel.show(
                title=title,
                desc=(roster_block(state, speeches=True, speaking_uid=player.uid)
                      + f"\n\n🎤 轮到 **{player.seat}号** 发表竞选演讲。"),
                color=C_DAY, view=view,
            )
            try:
                text = await asyncio.wait_for(fut, timeout=SPEAK)
                if text.strip():
                    player.last_speech = text
                    day_log.append(f"{player.seat}号(竞选): {text}")
                else:
                    player.last_speech = "（选择不发言）"
                    day_log.append(f"{player.seat}号(竞选): （沉默）")
            except asyncio.TimeoutError:
                player.last_speech = "（超时，跳过演讲）"
                day_log.append(f"{player.seat}号(竞选): （沉默/超时）")
            finally:
                state.current_speaker_uid = None
                view.stop()
            await panel.show(title=title, desc=roster_block(state, speeches=True),
                             color=C_DAY, footer="候选人轮流发表竞选演讲")

    # 3) 投票选警长（非候选人投票）
    voter_ids = {p.uid for p in state.alive_players if p.uid not in candidates}
    human_voter_ids = {uid for uid in voter_ids if not state.get(uid).is_npc}
    votes: dict[int, int] = {}

    if human_voter_ids:
        done = asyncio.Event()
        view = SheriffVoteGateView(cand_players, human_voter_ids, state.host_id, votes, done, VOTE)
        cand_txt = "、".join(f"**{p.label}**" for p in cand_players)
        await panel.show(
            title=title,
            desc=(roster_block(state, speeches=True)
                  + f"\n\n🗳️ 非候选人投票选警长（候选人：{cand_txt}）"),
            color=C_DAY,
        )
        vote_embed = discord.Embed(
            title="🗳️ 投票选警长",
            description=f"非候选人玩家点下方按钮投票（{VOTE} 秒内）。",
            color=C_DAY,
        )
        vote_msg = await channel.send(embed=vote_embed, view=view)
        await wait_event(done, VOTE)
        view.stop()
        try:
            await vote_msg.edit(view=None)
        except discord.HTTPException:
            pass

    # NPC（非候选人）投票
    for p in state.alive_players:
        if p.is_npc and p.uid not in candidates:
            t = await npc.sheriff_vote_decision(p, state, cand_players)
            if t is not None:
                votes[p.uid] = t

    # 4) 统计并宣布警长
    tally: dict[int, int] = {}
    for target_uid in votes.values():
        tally[target_uid] = tally.get(target_uid, 0) + 1

    if tally:
        lines = []
        for uid, count in sorted(tally.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"**{state.get(uid).label}**：{count} 票")
        await channel.send("📊 警长竞选结果：\n" + "\n".join(lines))

        top_count = max(tally.values())
        leaders = [uid for uid, c in tally.items() if c == top_count]
        if len(leaders) == 1:
            winner = state.get(leaders[0])
            winner.is_sheriff = True
            state.sheriff_uid = winner.uid
            await panel.show(
                title=title,
                desc=f"👑 **{winner.label}** 当选警长！\n\n{roster_block(state)}",
                color=C_DAY,
            )
            day_log.append(f"警长竞选：{winner.seat}号当选警长。")
        else:
            tied = "、".join(f"**{state.get(uid).label}**" for uid in leaders)
            await panel.show(
                title=title,
                desc=f"⚖️ {tied} 平票，本局不设警长。\n\n{roster_block(state)}",
                color=C_DAY,
            )
            day_log.append("警长竞选：平票，本局不设警长。")
    else:
        await panel.show(title=title, desc="无人投票，本局不设警长。\n\n" + roster_block(state), color=C_DAY)
        day_log.append("警长竞选：无人投票，本局不设警长。")

    await asyncio.sleep(2)


async def handle_sheriff_transfer(state: GameState, panel: Panel, channel,
                                  dead_player, day_log: list[str]) -> None:
    """如果出局的人是警长，处理警徽移交。"""
    if state.sheriff_uid is None or dead_player.uid != state.sheriff_uid:
        return
    title = "👑 警徽移交"

    if dead_player.is_npc:
        target_uid = await npc.sheriff_transfer(dead_player, state)
        if target_uid:
            target = state.get(target_uid)
            state.transfer_sheriff(target_uid)
            await channel.send(f"👑 **{dead_player.label}**（警长）把警徽移交给了 **{target.label}**。")
            day_log.append(f"{dead_player.seat}号(警长)把警徽移交给{target.seat}号。")
        else:
            state.transfer_sheriff(None)
            await channel.send(f"❌ **{dead_player.label}**（警长）撕掉了警徽！")
            day_log.append(f"{dead_player.seat}号(警长)撕掉了警徽。")
    else:
        result: dict = {"uid": "pending"}
        done = asyncio.Event()
        view = SheriffTransferGateView(dead_player, state, result, done, TURN)
        await dm_user(dead_player.uid, f"👑 你是警长且出局了，回游戏面板决定警徽移交。")
        await panel.show(
            title=title,
            desc=(f"👑 **{dead_player.label}** 是警长，出局了——\n"
                  f"请点按钮决定警徽移交给谁或撕掉警徽。\n\n{roster_block(state)}"),
            color=C_DAY, view=view,
        )
        await wait_event(done, TURN)
        view.stop()
        target_uid = result.get("uid", "pending")
        if target_uid == "pending":
            target_uid = None
        if target_uid:
            target = state.get(target_uid)
            state.transfer_sheriff(target_uid)
            await channel.send(f"👑 **{dead_player.label}**（警长）把警徽移交给了 **{target.label}**。")
            day_log.append(f"{dead_player.seat}号(警长)把警徽移交给{target.seat}号。")
        else:
            state.transfer_sheriff(None)
            await channel.send(f"❌ **{dead_player.label}**（警长）撕掉了警徽！")
            day_log.append(f"{dead_player.seat}号(警长)撕掉了警徽。")

    await panel.show(title=title, desc=roster_block(state), color=C_DAY)
    await asyncio.sleep(1.5)


# ============================================================
# 大厅
# ============================================================
class NpcPickSelect(discord.ui.Select):
    """房主在大厅挑选要加入本局的『角色 NPC』（多选；不选=自动补位）。"""

    def __init__(self, state: GameState, lobby: "LobbyView"):
        opts = [
            discord.SelectOption(
                label=c.name, value=c.name,
                description=((c.intro or "AI 角色")[:100]),  # 只显示公开简介，绝不暴露 persona
                default=(c.name in state.chosen_npc_names),
            )
            for c in CHARACTER_NPCS
        ]
        super().__init__(
            placeholder="选择要加入本局的 AI 角色（可多选）…",
            min_values=0, max_values=len(opts) or 1, options=opts,
        )
        self._state = state
        self._lobby = lobby

    async def callback(self, interaction: discord.Interaction):
        state = self._state
        host = state.host_id
        # 保护别人加的、或已绑了自有 API 的 NPC：房主的多选只管「房主名下、走 bot API」那批，
        # 不覆盖其它玩家用自己 key 认领的角色。
        protected = [n for n in state.chosen_npc_names
                     if n in state.npc_station or state.npc_owner.get(n) not in (None, host)]
        host_pick = [n for n in self.values if n not in protected]
        state.chosen_npc_names = protected + host_pick
        # 重建房主名下条目的归属；被房主取消选中的（bot API、房主名下）随重建自然移除
        for n in list(state.npc_owner):
            if state.npc_owner.get(n) == host and n not in state.npc_station and n not in host_pick:
                state.npc_owner.pop(n, None)
        for n in host_pick:
            state.npc_owner[n] = host
        picked = "、".join(self.values) if self.values else "（不指定，自动补位）"
        await interaction.response.edit_message(
            content=f"✅ 已设定本局 AI 角色：{picked}", view=None)
        await self._lobby.refresh()


# ============================================================
# 玩家私有 API + 自助加入 AI（需求4）
# ============================================================
def _resolve_npc_profile(state: GameState, name: str) -> dict | None:
    """开局时把 NPC 的「归属玩家 + 站名」解析成真正的 profile（url/key/model）。
    玩家中途编辑/删除了站，这里会用到最新值；站没了就退回默认 API。"""
    label = state.npc_station.get(name)
    owner = state.npc_owner.get(name)
    if not label or owner is None:
        return None
    st = userapi.get_station(owner, label)
    return st.as_profile() if st is not None else None


def _assign_station_to_npcs(state: GameState, uid: int, label: str,
                            selected: set[str]) -> None:
    """把『某玩家的某个站』指派给一组 NPC：选中的用这个站、取消的若原本指向这个站则解绑。"""
    # 取消勾选的（且原本就是这个玩家用这个站带的）→ 解绑、移出名单
    for n in list(state.npc_station):
        if (state.npc_station.get(n) == label and state.npc_owner.get(n) == uid
                and n not in selected):
            state.npc_station.pop(n, None)
            state.npc_owner.pop(n, None)
            if n in state.chosen_npc_names:
                state.chosen_npc_names.remove(n)
    # 勾选的 → 用这个玩家的这个站带它（用自己 API 的不限个数）
    for n in selected:
        if n not in state.chosen_npc_names:
            state.chosen_npc_names.append(n)
        state.npc_owner[n] = uid
        state.npc_station[n] = label


# ----- 新增 / 编辑 共用的保存 & 「拉模型→选」流程 -----
async def _persist_station(hub: "ApiHubView", uid: int, *, old_label: str | None,
                           label: str, base: str, key: str, model: str) -> tuple[bool, str]:
    """真正写入一个站（内存 + 数据库）。old_label=None 表示新增；非 None 表示编辑（可能改名）。
    不碰 interaction，只做数据。返回 (是否成功, 最终站名/错误说明)。"""
    state = hub.state
    if old_label is not None:
        old = userapi.get_station(uid, old_label)
        userapi.remove_station(uid, old_label)
        ok, info = userapi.add_station(uid, label, base, key, model)
        if not ok:
            if old:  # 改失败（多半重名）→ 把旧的原样加回去，别弄丢
                userapi.add_station(uid, old.label, old.base_url, old.api_key, old.model)
            return False, info
        if info != old_label:
            await database.delete_station(uid, old_label)
        await database.upsert_station(uid, info, base, key, model)
        # 本局里凡是该玩家用「旧站名」带的 NPC，改指向新站名
        for n, l in list(state.npc_station.items()):
            if l == old_label and state.npc_owner.get(n) == uid:
                state.npc_station[n] = info
        await hub.lobby.refresh()
        return True, info
    ok, info = userapi.add_station(uid, label, base, key, model)
    if ok:
        await database.upsert_station(uid, info, base, key, model)
    return ok, info


async def _finalize_station(interaction: discord.Interaction, hub: "ApiHubView", *,
                            old_label: str | None, label: str, base: str,
                            key: str, model: str) -> None:
    """新增/编辑提交后的统一收尾（interaction 尚未 response）：
    · 填了模型 → 用它测连通，过了就保存；
    · 没填模型 → 拉该站可用模型清单让你下拉选（或手动填）。"""
    if model:
        await interaction.response.send_message(
            f"⏳ 正在用模型「{model}」测试连接…", ephemeral=True)
        good, why = await llm.health_check_profile(
            {"name": label or "站", "base_url": base, "api_key": key, "model": model})
        if not good:
            await interaction.followup.send(
                f"🔴 测试失败：{why}\n（没有保存，请检查 url/key/模型名后重试）", ephemeral=True)
            return
        ok, info = await _persist_station(hub, interaction.user.id, old_label=old_label,
                                          label=label, base=base, key=key, model=model)
        await interaction.followup.send(
            (f"🟢 已保存站「{info}」（模型 {model}）。" if ok else f"❌ {info}"), ephemeral=True)
        return
    # 没填模型 → 拉清单来选（拉清单本身也是一次连通测试）
    await interaction.response.send_message("⏳ 正在拉取该站可用的模型…", ephemeral=True)
    okm, result = await llm.list_models(base, key)
    view = discord.ui.View(timeout=180)
    if okm and isinstance(result, list) and result:
        view.add_item(_ModelChoiceSelect(hub, old_label, label, base, key, result))
        view.add_item(_ManualModelButton(hub, old_label, label, base, key))
        await interaction.followup.send(
            f"🟢 连接成功，拉到 {len(result)} 个模型。选一个要用的（或手动填）：",
            view=view, ephemeral=True)
    elif okm:
        view.add_item(_ManualModelButton(hub, old_label, label, base, key))
        await interaction.followup.send(
            "🟡 连得上，但这个站没有返回可用模型清单。点下面手动填模型名（会再测一次连通）：",
            view=view, ephemeral=True)
    else:
        view.add_item(_ManualModelButton(hub, old_label, label, base, key))
        await interaction.followup.send(
            f"🔴 拉取模型失败：{result}\n可点下面手动填模型名再测一次连通：",
            view=view, ephemeral=True)


class _ManualModelModal(discord.ui.Modal, title="手动填模型名"):
    """站子不返回模型清单时，手动填一个模型名，并用它做一次真正的连通测试，过了才存。"""
    f_model = discord.ui.TextInput(label="模型名", placeholder="如 gemini-2.0-flash")

    def __init__(self, hub: "ApiHubView", old_label: str | None,
                 label: str, base: str, key: str):
        super().__init__()
        self._hub, self._old = hub, old_label
        self._label, self._base, self._key = label, base, key

    async def on_submit(self, interaction: discord.Interaction):
        model = str(self.f_model).strip()
        await interaction.response.send_message(
            f"⏳ 正在用模型「{model}」测试连接…", ephemeral=True)
        good, why = await llm.health_check_profile(
            {"name": self._label or "站", "base_url": self._base,
             "api_key": self._key, "model": model})
        if not good:
            await interaction.followup.send(
                f"🔴 测试失败：{why}\n（没有保存，请检查 url/key/模型名后重试）", ephemeral=True)
            return
        ok, info = await _persist_station(self._hub, interaction.user.id, old_label=self._old,
                                          label=self._label, base=self._base,
                                          key=self._key, model=model)
        await interaction.followup.send(
            (f"🟢 测试通过，已保存站「{info}」（模型 {model}）。" if ok else f"❌ {info}"),
            ephemeral=True)


class _ModelChoiceSelect(discord.ui.Select):
    """从该站拉到的模型清单里选一个，选定即保存（拉清单本身已经是连通测试）。"""

    def __init__(self, hub: "ApiHubView", old_label: str | None,
                 label: str, base: str, key: str, models: list[str]):
        self._hub, self._old = hub, old_label
        self._label, self._base, self._key = label, base, key
        opts = [discord.SelectOption(label=m[:100], value=m[:100]) for m in models[:25]]
        super().__init__(placeholder="选择该站要用的模型…", min_values=1,
                         max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        model = self.values[0]
        ok, info = await _persist_station(self._hub, interaction.user.id, old_label=self._old,
                                          label=self._label, base=self._base,
                                          key=self._key, model=model)
        msg = (f"🟢 连接正常，已保存站「{info}」（模型 {model}）。" if ok else f"❌ {info}")
        await interaction.response.edit_message(content=msg, view=None)


class _ManualModelButton(discord.ui.Button):
    def __init__(self, hub: "ApiHubView", old_label: str | None,
                 label: str, base: str, key: str):
        super().__init__(label="手动填模型名", style=discord.ButtonStyle.secondary, emoji="✏️")
        self._hub, self._old = hub, old_label
        self._label, self._base, self._key = label, base, key

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _ManualModelModal(self._hub, self._old, self._label, self._base, self._key))


class StationFormModal(discord.ui.Modal):
    """新增 / 编辑 API 站的统一表单。四个字段一致：站名 / URL / key / 首选模型(可选)。
    · old_label=None → 新增；非 None → 编辑该站（key 留空=保持原 key）。
    · 首选模型留空 → 提交后自动拉该站的模型清单让你下拉选（新增、编辑都一样）。"""

    def __init__(self, hub: "ApiHubView", old_label: str | None = None):
        self._hub = hub
        self._old = old_label
        st = userapi.get_station(hub.uid, old_label) if old_label else None
        super().__init__(title=(f"编辑站「{old_label}」"[:45] if old_label else "添加我的 API 站"))
        self.f_label = discord.ui.TextInput(
            label="配置名称（自取，方便你自己区分）", required=False, max_length=20,
            default=(st.label if st else None), placeholder="例如：OpenAI GPT-4")
        self.f_base = discord.ui.TextInput(
            label="Custom API URL（OpenAI 兼容）", required=(st is None),
            default=(st.base_url if st else None), placeholder="例如：https://api.openai.com/v1")
        self.f_key = discord.ui.TextInput(
            label=("Custom API 密钥（留空=保持原 key）" if st else "Custom API 密钥（只存内存、永不回显明文）"),
            required=False, placeholder=("不改就留空" if st else "sk-..."))
        self.f_model = discord.ui.TextInput(
            label="首选模型（可选，留空则获取模型列表再选）", required=False,
            default=(st.model if st else None), placeholder="例如：gpt-4")
        for it in (self.f_label, self.f_base, self.f_key, self.f_model):
            self.add_item(it)

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        old = userapi.get_station(uid, self._old) if self._old else None
        if self._old and old is None:
            await interaction.response.send_message("❌ 这个站已经不存在了。", ephemeral=True)
            return
        label = str(self.f_label).strip() or (old.label if old else "")
        base = str(self.f_base).strip() or (old.base_url if old else "")
        key = str(self.f_key).strip() or (old.api_key if old else "")  # 编辑时留空=沿用原 key
        model = str(self.f_model).strip()  # 留空 → 由 _finalize_station 去拉清单来选
        if not base or not key:
            await interaction.response.send_message("❌ Custom API URL 和密钥都不能为空。", ephemeral=True)
            return
        await _finalize_station(interaction, self._hub, old_label=self._old,
                                label=label, base=base, key=key, model=model)


class AssignStationNpcSelect(discord.ui.Select):
    """把『当前这个站』指派给哪些 AI 角色用（多选，像选 AI 角色那样）。"""

    def __init__(self, hub: "ApiHubView", label: str):
        self._hub = hub
        self._label = label
        state = hub.state
        opts = [discord.SelectOption(
            label=c.name, value=c.name,
            description=((c.intro or "AI 角色")[:100]),
            default=(state.npc_station.get(c.name) == label
                     and state.npc_owner.get(c.name) == hub.uid))
            for c in CHARACTER_NPCS][:25]
        super().__init__(placeholder=f"选哪些 AI 用你的站「{label}」（可多选）…",
                         min_values=0, max_values=len(opts) or 1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        _assign_station_to_npcs(self._hub.state, interaction.user.id,
                                self._label, set(self.values))
        picked = "、".join(self.values) if self.values else "（已全部取消）"
        await interaction.response.edit_message(
            content=f"✅ 站「{self._label}」现在带：{picked}", view=None)
        await self._hub.lobby.refresh()


class StationActionView(discord.ui.View):
    """选中某个站后的操作：编辑 / 删除 / 指派给 NPC。"""

    def __init__(self, hub: "ApiHubView", label: str):
        super().__init__(timeout=180)
        self._hub = hub
        self._label = label

    @discord.ui.button(label="编辑", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StationFormModal(self._hub, self._label))

    @discord.ui.button(label="删除", style=discord.ButtonStyle.danger, emoji="🗑")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        userapi.remove_station(uid, self._label)
        await database.delete_station(uid, self._label)
        # 解绑本局里用这个站带的 NPC（站没了就别让它指空）
        state = self._hub.state
        for n, l in list(state.npc_station.items()):
            if l == self._label and state.npc_owner.get(n) == uid:
                state.npc_station.pop(n, None)
                state.npc_owner.pop(n, None)
                if n in state.chosen_npc_names:
                    state.chosen_npc_names.remove(n)
        await interaction.response.edit_message(
            content=f"🗑 已删除站「{self._label}」（用它的 AI 也已撤下）。", view=None)
        await self._hub.lobby.refresh()

    @discord.ui.button(label="指派给 NPC", style=discord.ButtonStyle.success, emoji="🎭")
    async def assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not CHARACTER_NPCS:
            await interaction.response.send_message("目前没有可选的 AI 角色。", ephemeral=True)
            return
        view = discord.ui.View(timeout=180)
        view.add_item(AssignStationNpcSelect(self._hub, self._label))
        await interaction.response.edit_message(
            content=f"选哪些 AI 用你的站「{self._label}」：", view=view)


class StationManageSelect(discord.ui.Select):
    """『我的站』里选一个站，进入它的 编辑/删除/指派 操作。"""

    def __init__(self, hub: "ApiHubView"):
        self._hub = hub
        opts = [discord.SelectOption(
            label=s.label, value=s.label,
            description=f"{s.model}｜key {userapi.mask_key(s.api_key)}")
            for s in userapi.list_stations(hub.uid)][:25]
        super().__init__(placeholder="选一个站来 编辑/删除/指派给NPC…",
                         min_values=1, max_values=1,
                         options=opts or [discord.SelectOption(label="（无）", value="__none__")])

    async def callback(self, interaction: discord.Interaction):
        label = self.values[0]
        if label == "__none__":
            await interaction.response.edit_message(content="（你还没有站）", view=None)
            return
        await interaction.response.edit_message(
            content=f"站「{label}」要做什么？", view=StationActionView(self._hub, label))


class ApiHubView(discord.ui.View):
    """玩家私有 API 面板（ephemeral，每个玩家点开只对自己生效）：只有两个入口——
    『添加我的 API 站』和『我的站』（编辑/删除/指派给 NPC 都在『我的站』里）。"""

    def __init__(self, state: GameState, lobby: "LobbyView", uid: int):
        super().__init__(timeout=300)
        self.state = state
        self.lobby = lobby
        self.uid = uid

    @discord.ui.button(label="添加我的 API 站", style=discord.ButtonStyle.success, emoji="➕")
    async def add_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StationFormModal(self))

    @discord.ui.button(label="我的站", style=discord.ButtonStyle.secondary, emoji="📋")
    async def my_stations(self, interaction: discord.Interaction, button: discord.ui.Button):
        sts = userapi.list_stations(interaction.user.id)
        if not sts:
            await interaction.response.send_message(
                "（你还没有保存任何站。点『➕ 添加我的 API 站』来加。）", ephemeral=True)
            return
        body = "\n".join(
            f"{i+1}. **{s.label}** — 模型 `{s.model}`｜key {userapi.mask_key(s.api_key)}"
            for i, s in enumerate(sts))
        view = discord.ui.View(timeout=180)
        view.add_item(StationManageSelect(self))
        await interaction.response.send_message(
            f"🔐 你的私有站（只有你能看到、key 已打码）：\n{body}\n\n"
            "选一个站可以 ✏️编辑 / 🗑删除 / 🎭指派给某些 AI 角色用：",
            view=view, ephemeral=True)


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
                f"👥 本局人数：**{self.state.table_size} 人**（房主可点『6/8/10/12 人』切换；不足自动 AI 补位）。\n"
                f"⚠️ 本局为**面板模式**：全程在面板上行动，平时频道里不能直接打字，"
                f"轮到你时点面板按钮发言/行动。\n\n"
                f"📋 {summarize_distribution(self.state.table_size, self.state.board)}"
            ),
            color=C_INFO,
        )
        humans = self.state.humans
        chosen = self.state.chosen_npc_names if CHARACTER_NPCS else []
        # 真人(按加入次序) + 已选 NPC(标 🤖) 排进同一张名单，编号连续。
        # NPC 后面标 API 来源：🔑=有玩家用自己的 API 带它，否则走 bot 默认。
        rows = [f"{i+1}. {p.mention}" for i, p in enumerate(humans)]
        for j, name in enumerate(chosen):
            tag = " 🔑自带API" if name in self.state.npc_station else ""
            rows.append(f"{len(humans)+j+1}. 🤖{name}{tag}")
        roster = "\n".join(rows) if rows else "（还没有人加入）"
        e.add_field(name=f"已加入（{len(humans)} 真人 + {len(chosen)} AI）",
                    value=roster, inline=False)
        if CHARACTER_NPCS and not chosen:
            e.add_field(name="🎭 指定 AI 角色",
                        value="（未指定，自动用 AI 角色补位）", inline=False)
        e.set_footer(text="房主：可调『6/8/10/12 人』『选板子』『选 AI 角色』，再点『开始游戏』开局")
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

    @discord.ui.button(label="6/8/10/12 人", style=discord.ButtonStyle.secondary, emoji="👥")
    async def toggle_size(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.host_id:
            await interaction.response.send_message("只有房主能调整人数。", ephemeral=True)
            return
        if self.state.phase is not Phase.LOBBY:
            await interaction.response.send_message("游戏已经开始了。", ephemeral=True)
            return
        sizes = [6, 8, 10, 12]
        cur = self.state.table_size
        self.state.table_size = sizes[(sizes.index(cur) + 1) % len(sizes)] if cur in sizes else 6
        await interaction.response.send_message(
            f"👥 本局人数已设为 **{self.state.table_size} 人**：{summarize_distribution(self.state.table_size, self.state.board)}",
            ephemeral=True,
        )
        await self.refresh()

    @discord.ui.button(label="选板子", style=discord.ButtonStyle.secondary, emoji="🎲")
    async def cycle_board(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.host_id:
            await interaction.response.send_message("只有房主能切换板子。", ephemeral=True)
            return
        if self.state.phase is not Phase.LOBBY:
            await interaction.response.send_message("游戏已经开始了。", ephemeral=True)
            return
        # 在各预设之间循环切换
        order = [
            "auto", "simple", "hunter", "guard",
            "idiot", "knight",
            "hunter_idiot", "hunter_knight", "guard_idiot", "guard_knight",
            "classic", "classic_idiot", "classic_knight",
            "wolfking", "wolfking_knight",
        ]
        cur = self.state.board if self.state.board in order else "auto"
        self.state.board = order[(order.index(cur) + 1) % len(order)]
        await interaction.response.send_message(
            f"🎲 本局板子已设为 **{BOARD_NAMES.get(self.state.board, self.state.board)}**：\n"
            f"{summarize_distribution(self.state.table_size, self.state.board)}",
            ephemeral=True,
        )
        await self.refresh()

    @discord.ui.button(label="选 AI 角色", style=discord.ButtonStyle.secondary, emoji="🎭")
    async def pick_npc(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.state.host_id:
            await interaction.response.send_message("只有房主能配置 AI 角色。", ephemeral=True)
            return
        if self.state.phase is not Phase.LOBBY:
            await interaction.response.send_message("游戏已经开始了。", ephemeral=True)
            return
        if not CHARACTER_NPCS:
            await interaction.response.send_message(
                "目前还没有可选的 AI 角色（characters.py 里没登记）。", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(NpcPickSelect(self.state, self))
        await interaction.response.send_message(
            "挑选要进本局的 AI 角色（可多选；不选则自动补位）。\n"
            "选中的角色会优先入座，剩余空位用普通 AI 补满。",
            view=view, ephemeral=True,
        )

    @discord.ui.button(label="我的 API", style=discord.ButtonStyle.secondary, emoji="🔧")
    async def my_api(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.state.phase is not Phase.LOBBY:
            await interaction.response.send_message("游戏已经开始了。", ephemeral=True)
            return
        await interaction.response.send_message(
            "🔧 **我的 API**（只有你能看到，key 私密保存、绝不外显）\n"
            "· **添加我的 API 站**：填 url+key，自动拉可用模型并测连通，选模型才保存。\n"
            "· **我的站**：✏️编辑 / 🗑删除，并把某个站 🎭指派给想让它带的 AI 角色（用自己 API 不限个数）。",
            view=ApiHubView(self.state, self, interaction.user.id), ephemeral=True)

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


async def phase_guard(bot, state: GameState, panel: Panel) -> int | None:
    """守卫夜晚，返回今晚守护目标 uid（None = 没守 / 本局没有守卫）。

    与预言家同款拟真：不论守卫是真人 / AI / 本局没有，对外都显示同一块带按钮的面板，
    真人就等他点，AI / 空缺用随机延迟假装真人在操作，旁观者看不出差别。
    """
    guard = state.alive_guard()
    title = "🌙 第 %d 夜 · 守卫" % (state.day_count + 1)
    desc = ("🛡️ **守卫请睁眼**，守护一名玩家使其今晚不被狼刀。\n其他人请闭眼等待。\n\n"
            + roster_block(state))
    result: dict = {"uid": None}
    done = asyncio.Event()
    view = GuardGateView(state, result, done, timeout=TURN)
    await panel.show(title=title, desc=desc, color=C_NIGHT, view=view, footer="守卫：点『守卫行动』")
    guard_uid = None
    if guard is not None and not guard.is_npc:
        await wait_event(done, TURN)
        guard_uid = result["uid"]
    else:
        await asyncio.sleep(_npc_night_delay())
        if guard is not None:  # NPC 守卫：随机延迟后按规则选守护目标
            guard_uid = await npc.guard_target(guard, state)
    view.stop()
    if guard is not None:
        guard.last_guard_uid = guard_uid
    return guard_uid


async def phase_seer(bot, state: GameState, panel: Panel) -> None:
    # NPC 预言家先行动（AI 选最有价值的目标查验）
    for seer in [p for p in state.alive_players if p.role is Role.SEER and p.is_npc]:
        t = await npc.seer_check_target(seer, state)
        if t is not None:
            seer.seer_results[t] = bool(state.get(t).role.is_wolf)

    seer = state.alive_seer()
    title = "🌙 第 %d 夜 · 预言家" % (state.day_count + 1)
    desc = ("🔮 **预言家请睁眼**，查验一名玩家的身份。\n其他人请闭眼等待。\n\n"
            + roster_block(state))
    # 不论预言家是真人 / AI / 甚至本局没有预言家，对外都显示同一块带按钮的面板，
    # 旁观者看不出差别；真人就等他点按钮，AI / 空缺则用随机延迟假装真人在操作。
    done = asyncio.Event()
    view = SeerGateView(state, done, timeout=TURN)
    await panel.show(title=title, desc=desc, color=C_NIGHT, view=view,
                     footer="预言家：点『预言家查验』行动")
    if seer is not None and not seer.is_npc:
        await wait_event(done, TURN)
    else:
        await asyncio.sleep(_npc_night_delay())
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
    desc = ("🐺 **狼人请睁眼**，和队友商量后选择今晚要击杀的目标。\n其他人请闭眼等待。\n\n"
            + roster_block(state))

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
    # 对外始终显示同一块带『狼人行动』按钮的面板；有真人狼就等他点，全是 AI 狼则
    # 用随机延迟假装真人在私频商量+选刀，旁观者看不出狼里有没有真人。
    done = asyncio.Event()
    view = WolfGateView(bot, state, votes, human_wolves, done, timeout=TURN)
    await panel.show(title=title, desc=desc, color=C_NIGHT, view=view,
                     footer="狼人：点『狼人行动』进入狼人频道并选刀")
    if human_wolves:
        await wait_event(done, TURN)
    else:
        await asyncio.sleep(_npc_night_delay())
    view.stop()

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


async def phase_witch(bot, state: GameState, panel: Panel, kill_uid: int | None,
                      day_log: list[str] | None = None) -> dict:
    """女巫夜晚，返回 {"heal": bool, "poison": uid|None}。"""
    result = {"heal": False, "poison": None}
    witch = state.alive_witch()
    victim = state.get(kill_uid) if kill_uid else None
    title = "🌙 第 %d 夜 · 女巫" % (state.day_count + 1)

    # NPC 女巫先把救/毒决策算好（结果不对外显示，仅写进 result）。
    # 传入白天发言记录，让女巫能为「白天被推的恋人」按性格决定是否用毒报复攻击者。
    if witch is not None and witch.is_npc:
        heal, poison = await npc.witch_night_action(witch, state, kill_uid, day_log)
        if heal:
            witch.has_heal = False
        if poison is not None:
            witch.has_poison = False
        result["heal"], result["poison"] = heal, poison

    # 不论女巫是真人 / AI / 本局无女巫，对外都显示同一块带按钮的面板；真人就等他操作，
    # AI / 空缺则用随机延迟假装真人在用药，旁观者看不出女巫是不是 AI。
    done = asyncio.Event()
    view = WitchGateView(state, victim, result, done, timeout=TURN)
    await panel.show(
        title=title,
        desc=("🧪 **女巫请睁眼**。点下方按钮查看今晚情况，并决定是否用药。\n其他人请闭眼等待。\n\n"
              + roster_block(state)),
        color=C_NIGHT, view=view, footer="女巫：点『女巫行动』",
    )
    if witch is not None and not witch.is_npc:
        await wait_event(done, TURN)
    else:
        await asyncio.sleep(_npc_night_delay())
    view.stop()
    return result


async def collect_last_words(state: GameState, panel: Panel, channel, player,
                             title: str = "🪦 遗言",
                             day_log: list[str] | None = None) -> None:
    """出局玩家留遗言：NPC 由 LLM 生成；真人通过面板弹窗输入。
    遗言同样落在面板里出局者的 ⚫ 灯旁边，不再往下另发消息。"""
    if player.is_npc:
        text = await npc.last_word(player, state)
        player.last_speech = f"🪦 {text}" if text else "🪦 （没有留下遗言）"
        await panel.show(title=title, desc=roster_block(state, speeches=True), color=C_DAY)
        if day_log is not None and text:
            day_log.append(f"{player.seat}号(遗言): {text}")
        return
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    state.current_speaker_uid = player.uid
    view = SpeechGateView(player, fut, btn_label="留遗言", modal_title="你的遗言",
                          input_label="留下你的遗言（可留空）", timeout=SPEAK + 120)
    await dm_user(player.uid, f"🪦 你（{player.seat}号）出局了，回到游戏面板点『留遗言』留下遗言吧。")
    await panel.ensure_bottom()
    await panel.show(
        title=title,
        desc=(roster_block(state, speeches=True, speaking_uid=player.uid)
              + f"\n\n🪦 **{player.label}** 出局了，点按钮留下遗言（留完即继续）。"),
        color=C_DAY, view=view,
    )
    try:
        text = await asyncio.wait_for(fut, timeout=SPEAK)
        player.last_speech = f"🪦 {text}" if text.strip() else "🪦 （没有留下遗言）"
    except asyncio.TimeoutError:
        text = ""
        player.last_speech = "🪦 （没有留下遗言）"
    finally:
        state.current_speaker_uid = None
        view.stop()
    if day_log is not None and text and text.strip():
        day_log.append(f"{player.seat}号(遗言): {text.strip()}")
    await panel.show(title=title, desc=roster_block(state, speeches=True), color=C_DAY)


# ============================================================
# 猎人开枪
# ============================================================
class HunterShootSelect(discord.ui.Select):
    def __init__(self, hunter, state: GameState, result: dict, done: asyncio.Event):
        options = [discord.SelectOption(label="🚫 不开枪", value="0")]
        options += [discord.SelectOption(label=p.label, value=str(p.uid))
                    for p in state.alive_players if p.uid != hunter.uid]
        super().__init__(placeholder="选择开枪带走的对象…", min_values=1, max_values=1, options=options)
        self._state = state
        self._result = result
        self._done = done

    async def callback(self, interaction: discord.Interaction):
        uid = int(self.values[0])
        self._result["uid"] = uid
        self._done.set()
        if uid == 0:
            await interaction.response.edit_message(content="🚫 你选择不开枪。", view=None)
        else:
            await interaction.response.edit_message(
                content=f"🏹 你开枪带走了 **{self._state.get(uid).label}**。", view=None)


class HunterShootGateView(discord.ui.View):
    def __init__(self, hunter, state: GameState, result: dict, done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self.hunter = hunter
        self.state = state
        self.result = result
        self.done = done

    @discord.ui.button(label="开枪", style=discord.ButtonStyle.danger, emoji="🏹")
    async def act(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.hunter.uid:
            await interaction.response.send_message("只有猎人本人能开枪。", ephemeral=True)
            return
        view = discord.ui.View(timeout=self.timeout)
        view.add_item(HunterShootSelect(self.hunter, self.state, self.result, self.done))
        await interaction.response.send_message(
            "🏹 你是**猎人**，选择开枪带走谁：", view=view, ephemeral=True)


class WolfKingShootSelect(discord.ui.Select):
    def __init__(self, wolf_king, state: GameState, result: dict, done: asyncio.Event):
        options = [discord.SelectOption(label="🚫 不带人", value="0")]
        options += [discord.SelectOption(label=p.label, value=str(p.uid))
                    for p in state.alive_players if p.uid != wolf_king.uid]
        super().__init__(placeholder="选择带走的对象…", min_values=1, max_values=1, options=options)
        self._state = state
        self._result = result
        self._done = done

    async def callback(self, interaction: discord.Interaction):
        uid = int(self.values[0])
        self._result["uid"] = uid
        self._done.set()
        if uid == 0:
            await interaction.response.edit_message(content="🚫 你选择不带人。", view=None)
        else:
            await interaction.response.edit_message(
                content=f"👑🐺 你带走了 **{self._state.get(uid).label}**。", view=None)


class WolfKingShootGateView(discord.ui.View):
    def __init__(self, wolf_king, state: GameState, result: dict, done: asyncio.Event, timeout: int):
        super().__init__(timeout=timeout)
        self.wolf_king = wolf_king
        self.state = state
        self.result = result
        self.done = done

    @discord.ui.button(label="带走一人", style=discord.ButtonStyle.danger, emoji="👑")
    async def act(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.wolf_king.uid:
            await interaction.response.send_message("只有白狼王本人能选。", ephemeral=True)
            return
        view = discord.ui.View(timeout=self.timeout)
        view.add_item(WolfKingShootSelect(self.wolf_king, self.state, self.result, self.done))
        await interaction.response.send_message(
            "👑🐺 你是**白狼王**，选择带走谁：", view=view, ephemeral=True)


async def hunter_shoot(bot, state: GameState, panel: Panel, channel, hunter, day_log: list[str]) -> None:
    """猎人出局后开枪带走一人（被女巫毒死则 can_shoot=False，不会进来）。"""
    if hunter.role is not Role.HUNTER or not hunter.can_shoot:
        return
    hunter.can_shoot = False  # 一次性
    title = "🏹 猎人开枪"
    if hunter.is_npc:
        await panel.show(title=title, desc=f"🏹 **{hunter.label}** 是猎人，正在决定开枪带走谁……",
                         color=C_WIN_WOLF, footer="猎人正在开枪…")
        await asyncio.sleep(_npc_night_delay())  # 假装真人在犹豫开枪
        target_uid = await npc.hunter_shoot_target(hunter, state)
    else:
        result: dict = {"uid": None}
        done = asyncio.Event()
        view = HunterShootGateView(hunter, state, result, done, timeout=TURN)
        await dm_user(hunter.uid, "🏹 你是猎人且出局了，回游戏面板点『开枪』带走一人。")
        await panel.show(
            title=title,
            desc=f"🏹 **{hunter.label}** 是猎人，出局了——点下方按钮开枪带走一名玩家。",
            color=C_WIN_WOLF, view=view, footer="猎人：点『开枪』")
        await wait_event(done, TURN)
        view.stop()
        target_uid = result["uid"]

    if not target_uid:
        await channel.send(f"🏹 **{hunter.label}**（猎人）没有开枪。")
        return
    target = state.get(target_uid)
    if target is None or not target.alive:
        return
    target.alive = False
    await channel.send(
        f"🏹 **{hunter.label}**（猎人）开枪带走了 **{target.label}**，"
        f"其身份是 {target.role.emoji}**{target.role.cn}**。")
    day_log.append(f"{hunter.seat}号(猎人)开枪带走{target.seat}号。")
    await collect_last_words(state, panel, channel, target, day_log=day_log)
    await handle_sheriff_transfer(state, panel, channel, target, day_log)


async def wolf_king_shoot(bot, state: GameState, panel: Panel, channel, wolf_king, day_log: list[str]) -> None:
    """白狼王被投票放逐出局后可以带走一名玩家。"""
    if wolf_king.role is not Role.WOLF_KING:
        return
    title = "👑🐺 白狼王带人"
    if wolf_king.is_npc:
        await panel.show(title=title, desc=f"👑🐺 **{wolf_king.label}** 是白狼王，正在决定带走谁……",
                         color=C_WIN_WOLF, footer="白狼王正在选择…")
        await asyncio.sleep(_npc_night_delay())
        target_uid = await npc.wolf_king_shoot_target(wolf_king, state)
    else:
        result: dict = {"uid": None}
        done = asyncio.Event()
        view = WolfKingShootGateView(wolf_king, state, result, done, timeout=TURN)
        await dm_user(wolf_king.uid, "👑🐺 你是白狼王且被放逐了，回游戏面板点按钮带走一名玩家。")
        await panel.show(
            title=title,
            desc=f"👑🐺 **{wolf_king.label}** 是白狼王，被放逐了——点下方按钮带走一名玩家。",
            color=C_WIN_WOLF, view=view, footer="白狼王：点按钮选择带走谁")
        await wait_event(done, TURN)
        view.stop()
        target_uid = result["uid"]

    if not target_uid:
        await channel.send(f"👑🐺 **{wolf_king.label}**（白狼王）选择不带人。")
        return
    target = state.get(target_uid)
    if target is None or not target.alive:
        return
    target.alive = False
    await channel.send(
        f"👑🐺 **{wolf_king.label}**（白狼王）带走了 **{target.label}**，"
        f"其身份是 {target.role.emoji}**{target.role.cn}**。")
    day_log.append(f"{wolf_king.seat}号(白狼王)带走{target.seat}号。")
    await collect_last_words(state, panel, channel, target)


async def announce_deaths(bot, state: GameState, panel: Panel, channel, deaths: list, day_log: list[str]) -> None:
    title = "🌅 第 %d 天 · 天亮了" % state.day_count
    # 新的一天：清掉上一轮显示在灯旁的发言，面板从干净的灯牌开始
    for p in state.players:
        p.last_speech = ""
    if not deaths:
        await panel.show(title=title,
                         desc="昨晚是**平安夜**，无人死亡。\n\n" + roster_block(state), color=C_DAY)
        day_log.append(f"第{state.day_count}晚，平安夜。")
        await asyncio.sleep(2)
        return
    names = "、".join(d.label for d in deaths)
    await panel.show(title=title,
                     desc=f"昨晚倒下的是：**{names}**。\n\n" + roster_block(state), color=C_DAY)
    for d in deaths:
        day_log.append(f"第{state.day_count}晚，{d.seat}号出局。")
    await asyncio.sleep(1.5)
    for d in deaths:
        await collect_last_words(state, panel, channel, d, title=title, day_log=day_log)
        await handle_sheriff_transfer(state, panel, channel, d, day_log)
        # 猎人被狼刀出局可开枪（被女巫毒死时 can_shoot 已为 False）
        await hunter_shoot(bot, state, panel, channel, d, day_log)


class KnightDuelSelect(discord.ui.Select):
    def __init__(self, knight, state: GameState, result: dict, done: asyncio.Event):
        options = [discord.SelectOption(label=p.label, value=str(p.uid))
                    for p in state.alive_players if p.uid != knight.uid]
        super().__init__(placeholder="选择决斗对象…", min_values=1, max_values=1, options=options)
        self._state = state
        self._result = result
        self._done = done

    async def callback(self, interaction: discord.Interaction):
        uid = int(self.values[0])
        self._result["uid"] = uid
        self._done.set()
        await interaction.response.edit_message(
            content=f"⚔️ 你选择与 **{self._state.get(uid).label}** 决斗！", view=None)


async def resolve_knight_duel(state: GameState, panel: Panel, channel, knight, target_uid: int, day_log: list[str]) -> bool:
    """执行骑士决斗结算。返回 True 表示发生了决斗（讨论中断）。"""
    knight.has_dueled = True
    target = state.get(target_uid)
    if target is None or not target.alive:
        return False
    await channel.send(
        f"⚔️ **{knight.label}** 亮出了骑士身份牌，向 **{target.label}** 发起翻牌决斗！")
    await asyncio.sleep(2)
    if target.role and target.role.is_wolf:
        target.alive = False
        await channel.send(
            f"⚔️ **{target.label}** 是 {target.role.emoji}**{target.role.cn}**——狼人死亡！\n"
            f"骑士 **{knight.label}** 决斗成功！")
        day_log.append(f"{knight.seat}号(骑士)翻牌决斗{target.seat}号，对方是{target.role.cn}，狼死。")
    else:
        knight.alive = False
        await channel.send(
            f"⚔️ **{target.label}** 不是狼人——骑士 **{knight.label}** 决斗失败，自己出局！")
        day_log.append(f"{knight.seat}号(骑士)翻牌决斗{target.seat}号，对方不是狼，骑士自己出局。")
    return True


async def phase_discussion(bot, state: GameState, panel: Panel, channel, day_log: list[str]) -> None:
    title = "☀️ 第 %d 天 · 讨论" % state.day_count
    order = list(state.alive_players)
    order_txt = " → ".join(f"{p.seat}号" for p in order)
    # 整段讨论只靠面板就地刷新（发言显示在灯旁边、不再往下发消息），先把面板归位到
    # 频道底部，之后玩家就不用往上翻找面板了。
    await panel.ensure_bottom()
    duel_happened = False
    for player in order:
        if not player.alive:
            continue
        if duel_happened:
            break
        if player.is_npc:
            state.current_speaker_uid = None
            # 该 NPC 这格先标「发言中…」，其余人保留各自已说的话
            await panel.show(
                title=title,
                desc=roster_block(state, speeches=True, speaking_uid=player.uid),
                color=C_DAY, footer=f"按座位号轮流发言　{order_txt}",
            )
            # NPC 骑士：发言前先判断是否发起决斗
            if (player.role is Role.KNIGHT and not player.has_dueled):
                duel_target = await npc.knight_duel_decision(player, state, day_log)
                if duel_target is not None:
                    duel_happened = await resolve_knight_duel(
                        state, panel, channel, player, duel_target, day_log)
                    if duel_happened:
                        break
            # 先生成发言，再按字数模拟「真人打字」的停顿，避免 NPC 秒回暴露身份
            async with channel.typing():
                try:
                    # 硬时限：NPC 想太久/卡住就当本轮沉默，游戏继续，绝不「发言中」卡死
                    speech = await asyncio.wait_for(
                        npc.speak(player, state, day_log),
                        timeout=config.NPC_THINK_SECONDS)
                except asyncio.TimeoutError:
                    speech = ""
                await asyncio.sleep(min(9.0, 1.5 + len(speech) * 0.12))
            if speech:
                player.last_speech = speech
                day_log.append(f"{player.seat}号: {speech}")
            else:  # LLM 不可用时返回空串，标记沉默而不是凑写死台词
                player.last_speech = "（一时没接上话，跳过发言）"
                day_log.append(f"{player.seat}号: （沉默）")
            await panel.show(title=title, desc=roster_block(state, speeches=True),
                             color=C_DAY, footer=f"按座位号轮流发言　{order_txt}")
            await asyncio.sleep(0.6)
        else:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            state.current_speaker_uid = player.uid
            # view 的存活时间要比这一轮的等待时间长，否则玩家在输入框里打字时
            # view 先超时、按钮失效，就会出现「说到一半按钮没了、发不出去」。
            view = SpeechGateView(player, fut, btn_label="我要发言", modal_title="你的发言",
                                  input_label="输入你的发言", timeout=SPEAK + 120)
            # 骑士未决斗过：额外加一个「翻牌决斗」按钮
            duel_result: dict = {"uid": None}
            duel_done = asyncio.Event()
            is_knight = (player.role is Role.KNIGHT and not player.has_dueled)
            if is_knight:
                duel_btn = discord.ui.Button(label="翻牌决斗", style=discord.ButtonStyle.danger, emoji="⚔️")
                async def _duel_cb(interaction: discord.Interaction, _btn=duel_btn):
                    if interaction.user.id != player.uid:
                        await interaction.response.send_message("只有骑士本人能发起决斗。", ephemeral=True)
                        return
                    dv = discord.ui.View(timeout=SPEAK)
                    dv.add_item(KnightDuelSelect(player, state, duel_result, duel_done))
                    await interaction.response.send_message(
                        "⚔️ 你是**骑士**，选择决斗对象（对方是狼则狼死，否则你死）：",
                        view=dv, ephemeral=True)
                duel_btn.callback = _duel_cb
                view.add_item(duel_btn)
            await dm_user(player.uid, f"🎤 轮到你（{player.seat}号）发言了，回游戏面板点『我要发言』吧。")
            await panel.show(
                title=title,
                desc=(roster_block(state, speeches=True, speaking_uid=player.uid)
                      + f"\n\n🎤 轮到 **{player.seat}号** 发言（只有本人能点）；"
                        f"点下方按钮输入，发完即继续。"),
                color=C_DAY, view=view, footer="点『我要发言』弹出输入框",
            )
            # 等待发言或决斗（先到先得）
            speech_task = asyncio.create_task(asyncio.wait_for(fut, timeout=SPEAK))
            duel_task = asyncio.create_task(duel_done.wait()) if is_knight else None
            tasks = [speech_task]
            if duel_task:
                tasks.append(duel_task)
            done_set, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            state.current_speaker_uid = None
            view.stop()

            if duel_task in done_set and duel_result["uid"]:
                duel_happened = await resolve_knight_duel(
                    state, panel, channel, player, duel_result["uid"], day_log)
                if duel_happened:
                    break
            else:
                try:
                    text = speech_task.result()
                    if text and text.strip():
                        player.last_speech = text
                        day_log.append(f"{player.seat}号: {text}")
                    else:
                        player.last_speech = "（选择不发言）"
                        day_log.append(f"{player.seat}号: （沉默）")
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    player.last_speech = "（超时，跳过发言）"
                    day_log.append(f"{player.seat}号: （沉默/超时）")
            # 清掉按钮、把这位玩家的发言落在灯旁边
            await panel.show(title=title, desc=roster_block(state, speeches=True),
                             color=C_DAY, footer=f"按座位号轮流发言　{order_txt}")


async def phase_vote(bot, state: GameState, panel: Panel, channel, day_log: list[str]) -> None:
    alive = list(state.alive_players)
    # 白痴翻牌后失去投票权
    voters = [p for p in alive if not p.idiot_revealed]
    options = [("🙅 弃权（不投票）", "0")] + [(p.label, str(p.uid)) for p in alive]
    human_ids = {p.uid for p in voters if not p.is_npc}

    # value(uid) -> 展示名，给确认提示用
    label_map = {p.uid: p.label for p in alive}

    votes: dict[int, int] = {}
    if human_ids:
        done = asyncio.Event()
        view = VoteGateView(options, label_map, human_ids, state.host_id, votes, done, VOTE)
        await panel.show(
            title="🗳️ 第 %d 天 · 投票放逐" % state.day_count,
            desc=(roster_block(state, speeches=True)
                  + "\n\n存活玩家请在下方的投票面板里选择要放逐的人。"),
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

    for p in voters:
        if p.is_npc:
            t = await npc.vote_decision(p, state, day_log)
            if t is not None:
                votes[p.uid] = t

    real_votes = {v: t for v, t in votes.items() if t != 0}
    abstainers = [state.get(v).label for v, t in votes.items() if t == 0]

    if real_votes or abstainers:
        weighted_tally: dict[int, float] = {}
        for voter_uid, target_uid in real_votes.items():
            w = 1.5 if voter_uid == state.sheriff_uid else 1.0
            weighted_tally[target_uid] = weighted_tally.get(target_uid, 0.0) + w
        lines = []
        for target_uid, wcount in sorted(weighted_tally.items(), key=lambda x: x[1], reverse=True):
            voters = []
            for v, tt in real_votes.items():
                if tt == target_uid:
                    lbl = state.get(v).label
                    if v == state.sheriff_uid:
                        lbl += "👑"
                    voters.append(lbl)
            disp = f"{wcount:g}" if wcount != int(wcount) else str(int(wcount))
            lines.append(f"**{state.get(target_uid).label}**：{disp} 票（{', '.join(voters)}）")
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
    elif exiled.role is Role.IDIOT and exiled.alive:
        # 白痴翻牌免死（resolve_votes 里没标死，alive 仍为 True）
        await channel.send(
            f"🤡 **{exiled.label}** 被投票放逐——但翻出了身份牌 {exiled.role.emoji}**{exiled.role.cn}**！\n"
            f"白痴免死一次，但从此失去投票权。")
        day_log.append(f"{exiled.seat}号 被投票放逐，翻牌白痴免死（失去投票权）。")
    else:
        await channel.send(
            f"🔨 **{exiled.label}** 被放逐出局，身份是 {exiled.role.emoji}**{exiled.role.cn}**。")
        day_log.append(f"{exiled.seat}号 被投票放逐，身份是{exiled.role.cn}。")
        await collect_last_words(state, panel, channel, exiled,
                                 title="🗳️ 第 %d 天 · 投票放逐" % state.day_count,
                                 day_log=day_log)
        await handle_sheriff_transfer(state, panel, channel, exiled, day_log)
        # 猎人被票出可开枪带走一人
        await hunter_shoot(bot, state, panel, channel, exiled, day_log)
        # 白狼王被票出可带走一人
        await wolf_king_shoot(bot, state, panel, channel, exiled, day_log)


async def announce_end(channel, panel: Panel, state: GameState) -> None:
    if state.winner is Team.WOLF:
        title, color = "🐺 狼人阵营获胜！", C_WIN_WOLF
    else:
        title, color = "🎉 好人阵营获胜！", C_WIN_GOOD
    await panel.show(title=title, desc=state.public_roles_reveal(), color=color)


async def _save_game_record(state: GameState, day_log: list[str]) -> None:
    """对局结束后把这局存进数据库供复盘（数据库没启用时自动 no-op）。"""
    if not database.enabled or state.winner is None:
        return
    players = [{
        "seat": p.seat,
        "name": p.name,
        "is_npc": p.is_npc,
        "discord_id": (None if p.is_npc else str(p.uid)),
        "role": (p.role.cn if p.role else None),
        "alive": p.alive,
        "api_station": state.npc_station.get(p.name),  # 该 NPC 这局用了谁的哪个站
    } for p in sorted(state.players, key=lambda x: x.seat)]
    record = {
        "channel_id": str(state.channel_id),
        "board": state.board,
        "table_size": state.table_size,
        "winner": state.winner.value,
        "day_count": state.day_count,
        "record": {"players": players, "log": day_log},
    }
    await database.insert_game(record)


async def _purge_bot_messages(bot: discord.Client, channel, limit: int = 200) -> int:
    """删除 channel 里 bot 自己发的消息（最近 limit 条），返回删除数量。"""
    def is_mine(m: discord.Message) -> bool:
        return m.author.id == bot.user.id
    try:
        deleted = await channel.purge(limit=limit, check=is_mine)
        return len(deleted)
    except discord.Forbidden:
        n = 0
        async for m in channel.history(limit=limit):
            if is_mine(m):
                try:
                    await m.delete()
                    n += 1
                except discord.HTTPException:
                    pass
        return n
    except discord.HTTPException:
        return 0


async def _cleanup_after_game(bot: discord.Client, wolf_thread_id: int | None, origin_channel_id: int | None) -> None:
    """游戏结束 CLEANUP_DELAY_SECONDS 秒后：删除本局狼人专属子区，并清理频道里
    残留的狼人杀消息（如已经过期的大厅面板）。"""
    await asyncio.sleep(config.CLEANUP_DELAY_SECONDS)
    if wolf_thread_id:
        wt = bot.get_channel(wolf_thread_id)
        if isinstance(wt, discord.Thread):
            try:
                await wt.delete()
            except discord.HTTPException:
                pass
    if origin_channel_id:
        origin = bot.get_channel(origin_channel_id)
        if origin is not None:
            await _purge_bot_messages(bot, origin)


async def run_game(bot: discord.Client, state: GameState, channel) -> None:
    state.play_channel_id = channel.id
    panel = Panel(channel)
    try:
        # 1) NPC 补位（补到房主选的桌子人数；真人比这还多则以真人数为准）。
        #    玩家在大厅显式指定/认领的 AI 一定要坐下，所以桌子至少要容得下「真人+被指定AI」。
        human_names = {h.name for h in state.humans}
        chosen_seatable = [n for n in state.chosen_npc_names if n not in human_names]
        target = max(state.table_size, len(state.humans) + len(chosen_seatable))
        need = target - len(state.players)
        if need > 0:
            existing = {p.name for p in state.players}
            state.players.extend(
                npc.make_npcs(need, existing, state.chosen_npc_names or None))
        # 把玩家在大厅给各 NPC 指定的私有 API 站解析后绑到对应 NPC 上（没指定的走默认）
        for p in state.players:
            if p.is_npc and p.name in state.npc_station:
                p.api_profile = _resolve_npc_profile(state, p.name)
        if len(state.players) < 3:
            await channel.send("⚠️ 人数不足（至少 3 人），游戏取消。")
            return

        # 2) 分配角色（按配置的板子预设）
        state.assign_roles(state.board)
        await phase_reveal(state, panel)

        # 3) 昼夜循环（守卫 → 预言家 → 狼人 → 女巫）
        day_log: list[str] = []
        while True:
            # 每个昼夜周期都新开一块面板：上一天的面板（连同当天所有发言/票型）就留在
            # 频道里不动，方便大家往上翻、复盘前一天的发言和投票动机。
            panel = Panel(channel)
            guard_uid = await phase_guard(bot, state, panel)
            await phase_seer(bot, state, panel)
            kill_uid = await phase_wolves(bot, state, panel, channel)
            witch_res = await phase_witch(bot, state, panel, kill_uid, day_log)
            deaths = state.resolve_night(
                kill_uid, witch_res["heal"], witch_res["poison"], guard_uid)

            await announce_deaths(bot, state, panel, channel, deaths, day_log)
            if state.check_winner():
                break

            # 第一天白天讨论前：警长竞选（仅 classic 板 + 人数 >= 9）
            if state.day_count == 1 and state.has_sheriff:
                await phase_sheriff_election(bot, state, panel, channel, day_log)

            await phase_discussion(bot, state, panel, channel, day_log)
            await phase_vote(bot, state, panel, channel, day_log)
            if state.check_winner():
                break

        await announce_end(channel, panel, state)
        await _save_game_record(state, day_log)
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
        # 狼人子区留够时间让狼队复盘，CLEANUP_DELAY_SECONDS 秒后自动删除；
        # 同时清理频道里残留的狼人杀消息（如已经过期的大厅面板）。
        asyncio.create_task(
            _cleanup_after_game(bot, state.wolf_thread_id, state.channel_id))


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
    state.board = config.BOARD  # 环境变量作为默认板子，房主可在大厅按钮临时改
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
    n = await _purge_bot_messages(interaction.client, interaction.channel, count)
    await interaction.followup.send(f"🧹 已清理 {n} 条狼人杀消息。", ephemeral=True)


async def _api_station_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=p["name"][:100], value=p["name"])
        for p in llm.list_profiles()
        if current.lower() in p["name"].lower()
    ][:25]


@werewolf.command(name="api", description="查看 / 切换 AI 中转站（可限管理员）")
@app_commands.describe(station="要切换到的站名；留空则列出所有站并逐个测连通")
@app_commands.autocomplete(station=_api_station_autocomplete)
async def api_cmd(interaction: discord.Interaction, station: str | None = None):
    # 权限：配了 LLM_ADMIN_IDS 白名单就只许名单里的人用；没配则不限制
    if config.LLM_ADMIN_IDS and interaction.user.id not in config.LLM_ADMIN_IDS:
        await interaction.response.send_message(
            "🔒 你没有切换 AI 站点的权限。", ephemeral=True)
        return
    profiles = llm.list_profiles()

    if station is None:  # 列出所有站，并逐站测连通（不显示 model / key）
        await interaction.response.defer(ephemeral=True)
        results = await asyncio.gather(*[llm.health_check_profile(p) for p in profiles])
        lines = []
        for p, (ok, why) in zip(profiles, results):
            active = "✅在用 " if p["name"] == llm.active_name() else ""
            dot = "🟢 可用" if ok else f"🔴 {why}"
            lines.append(f"- {active}**{p['name']}** —— {dot}")
        await interaction.followup.send(
            f"🔌 **AI 中转站**（当前在用：**{llm.active_name()}**）\n"
            + "\n".join(lines)
            + "\n\n切换：`/werewolf api 站名`",
            ephemeral=True)
        return

    # 切换到指定站
    if not llm.switch_profile(station):
        names = "、".join(p["name"] for p in profiles) or "（未配置任何站）"
        await interaction.response.send_message(
            f"❓ 没有叫「{station}」的站。可选：{names}", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, why = await llm.health_check()
    status = ("🟢 连通正常，NPC 台词将由它生成。"
              if ok else f"🔴 切过去了，但自检失败：{why}（可再切回别的站）")
    await interaction.followup.send(
        f"🔌 已切换到 **{llm.active_name()}**\n{status}", ephemeral=True)


client.tree.add_command(werewolf)


@client.event
async def on_ready():
    log.info("已登录为 %s（id=%s）", client.user, client.user.id)
    # 中转站开机自检：一上线就告诉你 LLM 通不通，避免 NPC 全程沉默却查不到原因。
    log.info("LLM 中转站配置：base_url=%s  model=%s", config.OPENAI_BASE_URL, config.MODEL_NAME)
    ok, detail = await llm.health_check()
    if ok:
        log.info("✅ LLM 中转站连通正常，NPC 台词将由 LLM 实时生成。")
    else:
        log.error("❌ LLM 中转站不可用：%s", detail)
        log.error("   → 此状态下 NPC 会大面积『沉默』。请检查环境变量 "
                  "OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME（Railway 上同名变量）。")
    # 连接数据库（建表）；连上了就把玩家之前存过的 API 站读回内存（重启不丢）。
    await database.init()
    if database.enabled:
        n = await userapi.load_from_db()
        log.info("✅ 数据库已连接，载回 %d 个玩家的 API 站。", n)


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


@client.event
async def on_interaction(interaction: discord.Interaction):
    """兜底「孤儿按钮」：bot 重新部署 / 重启后，内存里的对局（games 字典 + 跑 run_game
    的协程 + 注册的 View）全没了，旧面板上的按钮再点就只会「交互失败」，整局看起来卡死。

    这里只拦**没有任何在内存里的对局认领**的组件交互（即重启后残留的旧面板），给玩家一个
    明确提示去重开；仍在进行中的对局有对应 View 处理，这里不插手（直接 return）。
    slash 命令（application_command）也不归这里管。
    """
    if interaction.type is not discord.InteractionType.component:
        return
    cid = interaction.channel_id
    # 只要还有哪一局在内存里引用了这个频道（主频道 / 游戏线程 / 狼人线程），就交给它的 View
    alive = any(
        cid in (s.channel_id, s.play_channel_id, s.thread_id, s.wolf_thread_id)
        for s in games.values()
    )
    if alive:
        return
    try:
        await interaction.response.send_message(
            "⚠️ 这一局已经失效了——bot 可能重启或重新部署过，**进行中的对局不会被保留**。\n"
            "请用 `/werewolf new` 重新开一局。",
            ephemeral=True,
        )
    except discord.HTTPException:
        # 交互已被别处响应 / 已过期，忽略即可
        pass


def run() -> None:
    missing = config.validate()
    if missing:
        raise SystemExit(
            "缺少必要配置：" + ", ".join(missing) + "\n请复制 .env.example 为 .env 并填写。")
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    run()
