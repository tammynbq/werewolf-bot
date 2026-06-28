"""玩家私有 LLM 站子存储（仅内存，按 Discord 用户 ID 隔离）。

设计目标（需求4）：
- 每个玩家可以私下存好几个自己的中转站（名称 / base_url / key / model）。
- 任何指令都**读不到明文 key**——对外只给「已设置 / 未设置 + 打码」。
- 数据只存在内存里、按 uid 隔离；别的玩家拿不到、也列不出别人的站。
- bot 重启即清空（内存存储），重启后需重新录入。

注意：这是「服务器端 bot 记住」，并非真正存在玩家本地。运行 bot 的服务器
（如 Railway 账号）持有者技术上能访问进程内存——这一点无法对服务器主隐藏。
但对**其它玩家**而言是私密的：没有任何指令会把别人的 key 显示出来。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Station:
    label: str       # 玩家自取的站名（同一玩家内唯一）
    base_url: str
    api_key: str     # 仅内存保存，永不在任何消息里明文回显
    model: str

    def as_profile(self) -> dict:
        """转成 llm.chat(profile=) / health_check_profile 认的 dict 形态。"""
        return {"name": self.label, "base_url": self.base_url,
                "api_key": self.api_key, "model": self.model}


# uid -> 该玩家的站子列表（有序，按录入顺序）
_STORE: dict[int, list[Station]] = {}

# 单个玩家最多存几个站，防滥用
MAX_PER_USER = 10


def list_stations(uid: int) -> list[Station]:
    return list(_STORE.get(uid, []))


def has_stations(uid: int) -> bool:
    return bool(_STORE.get(uid))


def add_station(uid: int, label: str, base_url: str, api_key: str,
                model: str) -> tuple[bool, str]:
    """新增一个站。返回 (是否成功, 说明/最终站名)。"""
    label = (label or "").strip()
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    if not base_url or not api_key or not model:
        return False, "base_url / key / model 都不能为空"
    stations = _STORE.setdefault(uid, [])
    if len(stations) >= MAX_PER_USER:
        return False, f"最多只能存 {MAX_PER_USER} 个站子，先删一个再加"
    if not label:
        label = f"站{len(stations) + 1}"
    if any(s.label == label for s in stations):
        return False, f"你已经有一个叫「{label}」的站了，换个名字"
    stations.append(Station(label, base_url, api_key, model))
    return True, label


def remove_station(uid: int, label: str) -> bool:
    stations = _STORE.get(uid, [])
    for i, s in enumerate(stations):
        if s.label == label:
            stations.pop(i)
            return True
    return False


def get_station(uid: int, label: str) -> Station | None:
    for s in _STORE.get(uid, []):
        if s.label == label:
            return s
    return None


def mask_key(key: str) -> str:
    """给 key 打码，只露尾 4 位帮本人区分，绝不回显完整 key。"""
    if not key:
        return "（空）"
    if len(key) <= 4:
        return "••••"
    return "••••" + key[-4:]
