"""数据库持久化封装（PostgreSQL，可选，fail-soft）。

直连 PostgreSQL（Railway 自带数据库即可），用 asyncpg，全异步。
没配 DATABASE_URL 时整体降级为「未启用」，所有操作变 no-op，bot 照常用内存运行；
配了就自动连库、开机自动建表（无需手动跑 SQL）。任何数据库异常都只记日志、不抛出，
绝不因为数据库抽风把一局游戏 / 整个 bot 拖崩。

用法（Railway）：
1) 项目里 New → Database → Add PostgreSQL；
2) 在 bot 服务的 Variables 里加 DATABASE_URL = ${{Postgres.DATABASE_URL}}（引用即可，不用复制）；
3) 部署。启动时会自动建表。
"""
from __future__ import annotations

import json
import logging

import config

log = logging.getLogger("werewolf.db")

_pool = None
enabled = False

_SCHEMA = """
create table if not exists api_stations (
    id          bigserial primary key,
    discord_id  text not null,
    label       text not null,
    base_url    text not null,
    api_key     text not null,
    model       text not null,
    created_at  timestamptz default now(),
    unique (discord_id, label)
);
create table if not exists games (
    id          bigserial primary key,
    channel_id  text,
    board       text,
    table_size  int,
    winner      text,
    day_count   int,
    record      jsonb,
    created_at  timestamptz default now()
);
"""


async def init() -> None:
    """连接数据库并建表。没配 DATABASE_URL 就保持「未启用」。只需在启动时调一次。"""
    global _pool, enabled
    if enabled:
        return
    if not config.DATABASE_URL:
        log.info("ℹ️ 未配置 DATABASE_URL，数据库持久化关闭（用内存运行）。")
        return
    try:
        import asyncpg
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as con:
            await con.execute(_SCHEMA)
        enabled = True
        log.info("✅ 数据库已连接、表已就绪。")
    except Exception as exc:  # 缺包 / 连接串错 / 网络不通等
        log.error("❌ 数据库初始化失败，降级为内存运行：%s", exc)


# ========== 玩家 API 站（持久化 userapi.py）==========
async def fetch_all_stations() -> list[dict]:
    if not enabled or _pool is None:
        return []
    try:
        rows = await _pool.fetch(
            "select discord_id, label, base_url, api_key, model from api_stations")
        return [dict(r) for r in rows]
    except Exception as exc:
        log.error("读取 api_stations 失败：%s", exc)
        return []


async def upsert_station(discord_id: int, label: str, base_url: str,
                         api_key: str, model: str) -> None:
    if not enabled or _pool is None:
        return
    try:
        await _pool.execute(
            "insert into api_stations(discord_id, label, base_url, api_key, model) "
            "values($1, $2, $3, $4, $5) "
            "on conflict (discord_id, label) do update set "
            "base_url = excluded.base_url, api_key = excluded.api_key, model = excluded.model",
            str(discord_id), label, base_url, api_key, model)
    except Exception as exc:
        log.error("写入 api_stations 失败：%s", exc)


async def delete_station(discord_id: int, label: str) -> None:
    if not enabled or _pool is None:
        return
    try:
        await _pool.execute(
            "delete from api_stations where discord_id = $1 and label = $2",
            str(discord_id), label)
    except Exception as exc:
        log.error("删除 api_stations 失败：%s", exc)


# ========== 对局历史 / 复盘 ==========
async def insert_game(rec: dict) -> None:
    if not enabled or _pool is None:
        return
    try:
        await _pool.execute(
            "insert into games(channel_id, board, table_size, winner, day_count, record) "
            "values($1, $2, $3, $4, $5, $6::jsonb)",
            rec.get("channel_id"), rec.get("board"), rec.get("table_size"),
            rec.get("winner"), rec.get("day_count"),
            json.dumps(rec.get("record") or {}, ensure_ascii=False))
    except Exception as exc:
        log.error("写入 games 失败：%s", exc)
