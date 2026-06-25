# 🐺 werewolf-bot — Discord 狼人杀机器人

一个支持 **AI NPC 补位凑人数** 的 Discord 狼人杀 bot。LLM 走 **中转站**（OpenAI 兼容协议），
人数不够时用 AI NPC 自动补位，NPC 会用 LLM 生成自然的发言，并按规则做夜晚行动和投票。

## 特性

- 🎮 频道内开局，按钮加入大厅，房主一键开始
- 🤖 **AI NPC 补位**：真人不够自动补到设定人数
- 🧠 **NPC = LLM 发言 + 规则决策**：白天发言由 LLM 生成更像真人，夜晚刀人/查验/投票走规则（省 token、可预测）
- 🔮 最简版板子：**狼人 / 预言家 / 平民**（人数变化自动调整配置）
- 🌐 **LLM 走中转站**：OpenAI 兼容，配置 `base_url` 指向你的中转代理即可，不直连官方 API
- 🌙 夜晚行动走私信（DM），白天发言/投票走频道 UI

## 角色（最简版）

| 角色 | 说明 |
| --- | --- |
| 🐺 狼人 | 每晚和狼队友一起选择击杀一名玩家，目标杀光好人 |
| 🔮 预言家 | 每晚查验一名玩家是好人还是狼人，带领好人放逐狼 |
| 👤 平民 | 无技能，靠发言和投票找狼 |

> 板子按总人数自动分配：狼人约占 1/4（至少 1 只）、固定 1 个预言家、其余平民。

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
| `OPENAI_MODEL` | 模型名（取决于中转站，如 `gpt-4o-mini` / `deepseek-chat`） |
| `WEREWOLF_TOTAL_PLAYERS` | 一局总人数，人数不足用 NPC 补位（默认 6） |
| `WEREWOLF_TURN_SECONDS` | 单次发言/行动等待秒数（默认 60） |
| `WEREWOLF_NPC_TEMPERATURE` | NPC 发言温度（默认 0.9） |

> 「中转站」即 OpenAI 兼容的代理（如 one-api / new-api 等）。代码用官方 `openai`
> 库，只是把 `base_url` 指向中转站，所以任何兼容 `/v1/chat/completions` 的服务都能用。

## Discord 开发者后台设置

1. 在 [Developer Portal](https://discord.com/developers/applications) 创建应用 → Bot，复制 Token。
2. **Bot → Privileged Gateway Intents** 打开 **MESSAGE CONTENT INTENT**（读取白天发言需要）。
3. 邀请 bot 时勾选 `bot` 和 `applications.commands` 权限，频道需要「发送消息 / 嵌入链接」权限。
4. 人类玩家需允许「来自服务器成员的私信」，否则收不到身份和夜晚行动菜单（关闭私信会自动随机兜底）。

## 运行

```bash
python bot.py
```

## 玩法

1. 在频道里输入 `/werewolf new` 开一局，会弹出大厅。
2. 其他人点 **✅ 加入**，房主点 **▶️ 开始游戏**。
3. 人数不足时自动用 🤖AI NPC 补位到设定人数，并分配身份（人类查收私信）。
4. **夜晚**：狼人 / 预言家在私信里用菜单行动，NPC 自动行动。
5. **白天**：依次发言（轮到你时在频道发言；NPC 由 LLM 生成发言），然后投票放逐。
6. 重复昼夜，直到一方获胜，公开所有人身份。

斜杠命令：

- `/werewolf new` —— 开一局
- `/werewolf cancel` —— 取消本局（仅房主）
- `/werewolf status` —— 查看状态

## 项目结构

```
config.py        读取环境变量（Discord / 中转站 / 游戏参数）
llm.py           LLM 中转站客户端（OpenAI 兼容，懒加载）
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
