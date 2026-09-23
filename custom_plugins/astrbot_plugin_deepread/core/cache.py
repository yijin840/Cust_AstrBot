"""最近分享缓存。

为什么需要它
------------
工具被调用时，拿到的是**用户当前那条消息**（例如「小蜗看看刚才那个卡片」），
这条消息里是没有卡片的。要让「刚才那个」「群里分享的那个」这类指代能落地，
就必须在消息流经过时**静默记下**最近的分享内容。

注意区分两件事：
  · 被动**记录** —— 只往缓存写，不回复、不解析、不打扰任何人。必须要有。
  · 被动**回复** —— 自动总结并发言。用户明确不要。

缓存按会话（群/私聊）隔离，带条数上限与过期时间，纯内存、不落盘。
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from .models import ReadItem


@dataclass
class CachedShare:
    """一条被记住的分享。"""

    item: ReadItem
    ts: float = field(default_factory=time.time)
    sender: str = ""
    msg_id: str = ""

    def age(self) -> float:
        return time.time() - self.ts


class ShareCache:
    """按会话保存最近若干条分享。"""

    def __init__(self, size: int = 5, ttl: int = 1800) -> None:
        self.size = max(1, size)
        self.ttl = max(60, ttl)
        self._store: dict[str, list[CachedShare]] = defaultdict(list)

    # ------------------------------------------------------------------ #
    def put(self, session_id: str, item: ReadItem, sender: str = "",
            msg_id: str = "") -> None:
        """记下一条分享。

        同一条内容再次出现时，视为「最近又被分享了一次」——
        刷新时间戳并挪到最新位置，而不是原地更新或堆叠。
        """
        if not session_id or not item.has_content():
            return

        bucket = self._store[session_id]
        key = self._key_of(item)
        for idx, rec in enumerate(bucket):
            if self._key_of(rec.item) == key:
                bucket.pop(idx)
                break

        bucket.append(CachedShare(item=item, sender=sender, msg_id=msg_id))
        if len(bucket) > self.size:
            del bucket[: len(bucket) - self.size]

    @staticmethod
    def _key_of(item: ReadItem) -> str:
        return item.url or f"{item.kind}:{item.title}"

    def _alive(self, session_id: str) -> list[CachedShare]:
        bucket = self._store.get(session_id) or []
        alive = [r for r in bucket if r.age() <= self.ttl]
        if len(alive) != len(bucket):
            self._store[session_id] = alive
        return alive

    def recent(self, session_id: str, limit: int = 5) -> list[ReadItem]:
        """取最近若干条（新的在前）。"""
        alive = self._alive(session_id)
        return [r.item for r in reversed(alive[-limit:])]

    def latest_age(self, session_id: str) -> float | None:
        """最近一条分享「多久以前」了；缓存为空返回 None。

        预读注入要靠它判断「刚才那条」到底还算不算刚才——
        一条半小时前的分享不该在用户随口提问时被塞进 prompt。
        """
        alive = self._alive(session_id)
        return alive[-1].age() if alive else None

    def search(self, session_id: str, keyword: str,
               limit: int = 5) -> list[ReadItem]:
        """按关键词在标题/摘要/卡片字段/正文里找。"""
        kw = (keyword or "").strip()
        if not kw:
            return []
        hits: list[ReadItem] = []
        for rec in reversed(self._alive(session_id)):
            item = rec.item
            haystack = " ".join(
                [item.title, item.desc, item.body, rec.sender]
                + list(item.meta_fields.values())
            )
            if kw in haystack:
                hits.append(item)
            if len(hits) >= limit:
                break
        return hits

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._store.clear()
        else:
            self._store.pop(session_id, None)

    def stats(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._store.items() if v}
