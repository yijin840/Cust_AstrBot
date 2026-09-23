"""静默缓存单测。不依赖 AstrBot。

用法: <python> tests/test_cache.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from core.cache import ShareCache  # noqa: E402
from core.models import KIND_CARD, KIND_LINK, ReadItem  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望: {expected!r}\n         实际: {actual!r}")


def check_true(name: str, cond: bool) -> None:
    check(name, bool(cond), True)


def card(title: str, url: str = "", desc: str = "") -> ReadItem:
    return ReadItem(kind=KIND_CARD, title=title, desc=desc, url=url,
                    meta_fields={"tag": "小黑盒"})


def main() -> int:
    S = "aiocqhttp:GroupMessage:123"

    print("=== 1. 基本存取与顺序（新的在前）===")
    c = ShareCache(size=5, ttl=1800)
    c.put(S, card("第一条", "https://a.com/1"))
    time.sleep(0.01)
    c.put(S, card("第二条", "https://a.com/2"))
    time.sleep(0.01)
    c.put(S, card("第三条", "https://a.com/3"))
    got = [i.title for i in c.recent(S)]
    check("最近在前", got, ["第三条", "第二条", "第一条"])

    print("\n=== 2. 同链接去重刷新，不堆叠 ===")
    c.put(S, card("第一条改标题", "https://a.com/1"))
    got = [i.title for i in c.recent(S)]
    check("不重复且刷新到最新", got, ["第一条改标题", "第三条", "第二条"])

    print("\n=== 3. 条数上限 ===")
    c2 = ShareCache(size=3, ttl=1800)
    for n in range(6):
        c2.put(S, card(f"第{n}条", f"https://b.com/{n}"))
    got = [i.title for i in c2.recent(S, limit=10)]
    check("只保留最近 3 条", got, ["第5条", "第4条", "第3条"])

    print("\n=== 4. TTL 过期 ===")
    c3 = ShareCache(size=5, ttl=60)
    c3.put(S, card("会过期的", "https://c.com/1"))
    check_true("刚放进可读到", len(c3.recent(S)) == 1)
    for rec in c3._store[S]:
        rec.ts = time.time() - 999
    check("过期后读不到", c3.recent(S), [])

    print("\n=== 5. 会话隔离 ===")
    c4 = ShareCache(size=5, ttl=1800)
    A, B = "sessA", "sessB"
    c4.put(A, card("A群的卡片", "https://x.com/a"))
    c4.put(B, card("B群的卡片", "https://x.com/b"))
    check("A 群只看到自己的", [i.title for i in c4.recent(A)], ["A群的卡片"])
    check("B 群只看到自己的", [i.title for i in c4.recent(B)], ["B群的卡片"])

    print("\n=== 6. 关键词检索 ===")
    c5 = ShareCache(size=5, ttl=1800)
    c5.put(S, card("潮玩星球新品开售", "https://h.com/1", desc="盲盒系列"))
    time.sleep(0.01)
    c5.put(S, card("AI 编程工具横评", "https://h.com/2", desc="六款助手实测"))
    check("命中标题", [i.title for i in c5.search(S, "潮玩")], ["潮玩星球新品开售"])
    check("命中摘要", [i.title for i in c5.search(S, "六款")], ["AI 编程工具横评"])
    check("命中卡片字段", [i.title for i in c5.search(S, "小黑盒")],
          ["AI 编程工具横评", "潮玩星球新品开售"])
    check("无命中返回空", c5.search(S, "不存在的东西"), [])
    check("空关键词返回空", c5.search(S, ""), [])

    print("\n=== 7. 空内容不入缓存 ===")
    c6 = ShareCache(size=5, ttl=1800)
    c6.put(S, ReadItem(kind=KIND_LINK))          # 什么都没有
    check("空条目被拒", c6.recent(S), [])
    c6.put("", card("没有会话 id"))                # 无 session
    check("无会话被拒", c6.stats(), {})

    print("\n=== 8. clear ===")
    c7 = ShareCache(size=5, ttl=1800)
    c7.put("s1", card("x", "https://q.com/1"))
    c7.put("s2", card("y", "https://q.com/2"))
    c7.clear("s1")
    check("只清指定会话", sorted(c7.stats().keys()), ["s2"])
    c7.clear()
    check("全清", c7.stats(), {})

    print("\n=== 9. 缺少 url 时用 标题+类型 去重 ===")
    c8 = ShareCache(size=5, ttl=1800)
    c8.put(S, card("同样的标题"))
    c8.put(S, card("同样的标题"))
    check("同标题不堆叠", len(c8.recent(S)), 1)

    print("\n" + "=" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
