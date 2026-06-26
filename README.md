# 🐺 werewolf-bot — Discord 狼人杀机器人

一个支持 **AI NPC 补位凑人数** 的 Discord 狼人杀 bot。LLM 走 **中转站**（OpenAI 兼容协议），
人数不够时用 AI NPC 自动补位，NPC 会用 LLM 生成自然的发言，并按规则做夜晚行动和投票。

## 特性

- 🎮 频道内开局，按钮加入大厅，房主一键开始
- 🪧 **全程面板模式**：身份确认、夜晚行动、白天发言、投票全在一块随阶段更新的面板上进行
- 🤫 **统一禁言**：游戏频道里平时不能直接打字，轮到你时点面板按钮（发言走弹窗输入框）才能行动
- 🐺 **狼人私密频道**：两只狼夜里进入专属私密线程，知道彼此身份并可实时商量，NPC 狼也会发言
- 🤖 **AI NPC 补位**：真人不够自动补到设定人数
- 🧠 **NPC = LLM 发言 + 规则决策**：白天发言由 LLM 生成更像真人，夜晚刀人/查验/用药/投票走规则（省 token、可预测）
- 🔮 板子：**狼人 ×2 / 预言家 / 女巫 / 平民**（人数变化自动调整配置）
- 🌐 **LLM 走中转站**：OpenAI 兼容，配置 `base_url` 指向你的中转代理即可，不直连官方 API

## 角色

| 角色 | 说明 |
| --- | --- |
| 🐺 狼人 | 每晚和狼队友在专属狼人频道商量后击杀一名玩家，目标杀光好人 |
| 🔮 预言家 | 每晚查验一名玩家是好人还是狼人，带领好人放逐狼 |
| 🧪 女巫 | 一瓶解药 + 一瓶毒药（各一次）：夜里得知谁被刀，可救活或毒死一人 |
| 👤 平民 | 无技能，靠发言和投票找狼 |

> 板子按总人数自动分配：狼人约占 1/3（至少 1 只），人够就配 1 预言家 + 1 女巫，其余平民。
> 经典 6 人局即 🐺×2 / 🔮×1 / 🧪×1 / 👤×2。

## 安装

```bash
pip install -r requirements.txt
cp .env.example .env   # 然后填写下面的配置
```

## 配置（`.env`）

| 变量 | 说明 |
| --- | --- |
| `DISCORD_TOKEN` | Discord Bot Token |
| `OPENAI_BASE_URL` | **中转站** 地址，如 `https://your-relay.com/v1` |
| `OPENAI_API_KEY` | 中转站分配的 key |
| `MODEL_NAME` | 模型名（变量名与 bq-bot 对齐，如 `claude-opus-4-8` / `gpt-4o-mini`） |
| `WEREWOLF_TOTAL_PLAYERS` | 一局总人数，人数不足用 NPC 补位（默认 6） |
| `WEREWOLF_TURN_SECONDS` | 单次发言/行动等待秒数（默认 60） |
| `WEREWOLF_NPC_TEMPERATURE` | NPC 发言温度（默认 0.9） |

> 「中转站」即 OpenAI 兼容的代理（如 one-api / new-api 等）。代码用官方 `openai`
> 库，只是把 `base_url` 指向中转站，所以任何兼容 `/v1/chat/completions` 的服务都能用。

## Discord 开发者后台设置

1. 在 [Developer Portal](https://discord.com/developers/applications) 创建应用 → Bot，复制 Token。
2. **Bot → Privileged Gateway Intents** 打开 **MESSAGE CONTENT INTENT**（读取白天发言需要）。
3. 邀请 bot 时勾选 `bot` 和 `applications.commands`。频道权限需要：**发送消息 / 嵌入链接 /
   管理消息（统一禁言要删玩家消息）/ 创建私密讨论串 / 在讨论串发送消息**。
4. **无需开启私信**：身份查看、夜晚行动都通过频道内**只有本人可见的临时消息（ephemeral）**完成，不依赖 DM。

## 运行

```bash
python bot.py
```

## 部署到 Railway

本仓库已带 `Procfile`（`worker: python bot.py`）和 `.python-version`，Railway 会自动识别为
后台 worker（无需暴露端口）。

1. Railway → New Project → Deploy from GitHub repo，选 `tammynbq/werewolf-bot`。
2. Settings → 选择要部署的分支（如 `claude/discord-werewolf-ai-bot-nkjwmt` 或合并后的 `main`）。
3. Variables 里填环境变量（变量名与 bq-bot 一致，可直接复用）：
   `DISCORD_TOKEN`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL_NAME`，
   按需再加 `WEREWOLF_TOTAL_PLAYERS` 等。
4. 部署后看 Deploy Logs，出现 `已登录为 ...` 即代表 bot 上线。

> bot 是常驻 worker，不监听 HTTP 端口；Railway 不需要配 healthcheck / 端口。

## 玩法

1. 在频道里输入 `/werewolf new` 开一局，会弹出大厅。
2. 其他人点 **✅ 加入**，房主点 **▶️ 开始游戏**。
3. 人数不足时自动用 🤖AI NPC 补位并分配身份，点面板上的 **🔍 查看我的身份**（仅本人可见）确认；全部真人确认后自动天黑。
4. **夜晚**（面板按阶段提示）：
   - 🔮 预言家点 **预言家查验** 选目标，立刻知道好/狼；
   - 🐺 狼人点 **狼人行动** 进入**狼人专属频道**和队友商量并选刀；
   - 🧪 女巫点 **女巫行动**，得知今晚谁被刀，决定用解药救或用毒药毒。
5. **白天**：面板按座位号轮流点名，轮到你时点 **我要发言** 弹出输入框打字提交；NPC 由 LLM 生成发言。随后投票放逐。
6. 重复昼夜，直到一方获胜，公开所有人身份。

> 全程在一块面板上进行：身份/夜晚行动用 **ephemeral 临时消息**只对本人可见；游戏频道**统一禁言**，
> 一切通过面板按钮操作，**不需要私信**。狼人频道是唯一可以自由打字的地方（只有狼能看到）。

斜杠命令：

- `/werewolf new` —— 开一局
- `/werewolf cancel` —— 取消本局（仅房主）
- `/werewolf status` —— 查看状态

## 项目结构

```
config.py        读取环境变量（Discord / 中转站 / 游戏参数）
llm.py           LLM 中转站客户端（OpenAI 兼容，懒加载）
characters.py    带完整人设的「角色 NPC」登记表（Theo、闻人幸…），可继续追加
knowledge.py     狼人杀「常识速通」手册，注入 NPC 发言提示（已按本局板子适配）
npc.py           AI NPC：补位、规则决策、LLM 发言
bot.py           Discord 交互层 + 游戏主流程
game/
  roles.py       角色与板子分配
  player.py      玩家模型（人类 / NPC）
  state.py       游戏状态机：夜晚结算、投票、胜负判定
```

## 备注

- 核心规则（`game/`）与 Discord 解耦，方便单测和扩展更多角色（女巫、猎人、守卫…）。
- LLM 调用失败（网络/额度/模型名错误）不会让游戏崩溃，NPC 会用兜底发言继续。
- **角色 NPC**：`characters.py` 里登记带完整人设的 AI 玩家（默认含 `Theo`、`闻人幸`），
  补位时优先入座、说话风格稳定可辨识；要加新角色只需在 `CHARACTER_NPCS` 末尾追加一个
  `Character`（`persona` 用第二人称「你是…」书写）。名字被真人占用时该角色自动让位。
- **常识手册**：`knowledge.py` 的 `WEREWOLF_PLAYBOOK`（屠边、悍跳/倒钩/冲锋、看票型、
  表水、轮次计算、贴脸场外是大忌…）会注入 NPC 发言提示，让 NPC 真正会用黑话、看票型；
  已按本局板子（只有狼/预/女/民）适配，标注了警长/守卫/猎人等本局不存在的机制不要提。
