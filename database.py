"""Supabase 持久化封装（可选，fail-soft）。

没配 SUPABASE_URL / SUPABASE_KEY 时整体降级为「未启用」，所有操作变 no-op，
bot 照常用内存运行——本地/没配数据库也能跑；配好了就自动持久化。

supabase-py 是同步客户端，这里所有网络调用都用 asyncio.to_thread 包一层，
避免阻塞 discord 的事件循环。任何数据库异常都只记日志、不抛出，绝不因为数据库
抽风而把一局游戏 / 整个 bot 拖崩。

需要的两张表见仓库根目录 supabase_schema.sql（到 Supabase 后台 SQL Editor 跑一次）。
"""
from __future__ import annotations

import asyncio
import logging

import config

log = logging.getLogger("werewolf.db")

_client = None
enabled = False


def _init() -> None:
    global _client, enabled
    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        log.info("ℹ️ 未配置 SUPABASE_URL / SUPABASE_KEY，数据库持久化关闭（用内存运行）。")
        return
    try:
        from supabase import create_client
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        enabled = True
        log.info("✅ Supabase 客户端已就绪。")
    except Exception as exc:  # 缺包 / url、key 不合法等
        log.error("❌ Supabase 初始化失败，降级为内存运行：%s", exc)


_init()


async def _run(fn):
    """把同步的 supabase 调用丢到线程里跑，避免阻塞事件循环。"""
    return await asyncio.to_thread(fn)


# ========== 玩家 API 站（持久化 userapi.py）==========
async def fetch_all_stations() -> list[dict]:
    if not enabled:
        return []
    try:
        res = await _run(lambda: _client.table("api_stations").select("*").execute())
        return res.data or []
    except Exception as exc:
        log.error("读取 api_stations 失败：%s", exc)
        return []


async def upsert_station(discord_id: int, label: str, base_url: str,
                         api_key: str, model: str) -> None:
    if not enabled:
        return
    try:
        await _run(lambda: _client.table("api_stations").upsert({
            "discord_id": str(discord_id),
            "label": label,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
        }, on_conflict="discord_id,label").execute())
    except Exception as exc:
        log.error("写入 api_stations 失败：%s", exc)


async def delete_station(discord_id: int, label: str) -> None:
    if not enabled:
        return
    try:
        await _run(lambda: _client.table("api_stations").delete()
                   .eq("discord_id", str(discord_id)).eq("label", label).execute())
    except Exception as exc:
        log.error("删除 api_stations 失败：%s", exc)


# ========== 对局历史 / 复盘 ==========
async def insert_game(record: dict) -> None:
    if not enabled:
        return
    try:
        await _run(lambda: _client.table("games").insert(record).execute())
    except Exception as exc:
        log.error("写入 games 失败：%s", exc)
