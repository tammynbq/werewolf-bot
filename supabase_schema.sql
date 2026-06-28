-- ============================================================
-- 狼人杀 bot 的 Supabase 表结构
-- 用法：打开 Supabase 项目 → 左侧 SQL Editor → New query → 把整段粘进去 → Run。
-- 跑一次即可（用了 if not exists，重复跑也安全）。
-- ============================================================

-- 1) 玩家的私有 API 站（持久化 userapi.py：重启不丢，玩家不用重输）
create table if not exists api_stations (
    id          bigint generated always as identity primary key,
    discord_id  text not null,           -- 玩家的 Discord 用户 ID
    label       text not null,           -- 玩家自取的站名（同一玩家内唯一）
    base_url    text not null,
    api_key     text not null,           -- ⚠️ 明文存储，见下方安全说明
    model       text not null,
    created_at  timestamptz default now(),
    unique (discord_id, label)           -- upsert 靠这个唯一约束（on_conflict）
);

-- 2) 对局历史 / 复盘
create table if not exists games (
    id          bigint generated always as identity primary key,
    channel_id  text,
    board       text,                    -- 板子预设
    table_size  int,
    winner      text,                    -- 'wolf' / 'village'
    day_count   int,
    record      jsonb,                   -- 完整复盘：players[座位/身份/是否NPC/用了谁的站] + log[发言/投票]
    created_at  timestamptz default now()
);

-- ============================================================
-- 安全说明（重要）：
-- · bot 用的是 service_role key（服务端写库、绕过 RLS）。请务必把
--   SUPABASE_KEY 当成机密，只放在 Railway 环境变量里，别提交进仓库、别发出去。
-- · 上面两张表默认开启 RLS、且不建任何 public 策略，这样除了持有 service_role
--   key 的 bot 自己，anon（公开）key 读不到任何数据。下面两行确保 RLS 开着：
alter table api_stations enable row level security;
alter table games        enable row level security;
-- · api_key 目前是明文落库。拥有 Supabase 后台 / service_role key 的人能看到明文。
--   要更安全可以做「应用层加密」（bot 端用一把 SECRET 加密后再存密文）——需要的话告诉我，
--   我再帮你加。
-- ============================================================
