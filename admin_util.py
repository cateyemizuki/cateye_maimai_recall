"""cateye 管理员判定统一模块（随插件复制分发）。

规则（0.2.0 起）：
- 管理员名单 = 宿主 `plugin.permission` ∪ 插件 `[rules] admin_users`；
- 插件 `admin_users` 是可选可见配置，默认留空表示自动跟随宿主；
- 两个来源都按纯 ID 去重（`qq:10001` 与 `10001` 视为同一人）；
- 宿主名单读取失败时只保留显式配置的插件管理员；两者都为空时返回空列表
  （fail-closed，绝不放行所有人）。
"""

from __future__ import annotations

from typing import Any, Iterable, List


def plain_id(entry: Any) -> str:
    """任意形态的 ID 条目 → 纯数字 ID 字符串；无法解析返回空串。

    支持：'qq:10001' / '平台:10001' / '10001' / 数字 / {'user_id': ...} 等。
    """
    if isinstance(entry, dict):
        entry = entry.get("user_id") or entry.get("id") or ""
    if isinstance(entry, int):
        return str(entry)
    if not isinstance(entry, str):
        return ""
    s = entry.strip()
    if not s:
        return ""
    if ":" in s:
        s = s.rsplit(":", 1)[1].strip()
    return s if s.isdigit() else ""


def _unwrap(source: Any) -> Any:
    """兼容 SDK 未解包时的 ``{"success":..., "value": [...]}`` 形态。"""
    if isinstance(source, dict):
        for key in ("value", "permissions", "permission", "admins", "users"):
            if key in source:
                return source[key]
        if "user_id" in source or "id" in source:
            return [source]
    return source


def dedup_admins(entries: Any) -> List[str]:
    """把任意形态的管理员条目归一为纯 ID 并去重（保持首次出现顺序）。"""
    entries = _unwrap(entries)
    if isinstance(entries, (str, int)):
        entries = [entries]
    if not isinstance(entries, Iterable) or isinstance(entries, (bytes, bytearray)):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for entry in entries:
        pid = plain_id(entry)
        if pid and pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def merge_admins(*sources: Any) -> List[str]:
    """合并多个管理员来源，按纯 ID 去重；来源为空时返回空列表。"""
    out: List[str] = []
    seen: set[str] = set()
    for source in sources:
        for pid in dedup_admins(source):
            if pid not in seen:
                seen.add(pid)
                out.append(pid)
    return out
