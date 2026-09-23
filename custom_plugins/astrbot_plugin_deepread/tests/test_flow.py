"""端到端流程测试：用线上真实消息链跑完整链路。不联网。

复现的是 2026-09-11 线上真实故障场景：

  13:10:13  群友发了一张小黑盒卡片  ← 插件必须静默缓存它
  14:24:03  小帅说「总结一下这个里面信息」（没有引用）
            → 工具从缓存里把它捞出来

用法（Windows）:
  <python> tests/test_flow.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from core.cache import ShareCache  # noqa: E402
from core.config import ReadConfig  # noqa: E402
from core.extractor import collect_from_event  # noqa: E402

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


# ---------------------------------------------------------------- 假组件/假事件
# 用 type() 造类，让 type(x).__name__ 正好等于组件名，
# 这样 extractor 里的字符串分支（线上真实走的那条）能被完整覆盖。
Plain = type("Plain", (), {})
Reply = type("Reply", (), {})
At = type("At", (), {})

# 线上真实卡片文本（抄自 data_v4.db / platform_message_history）
CARD_TEXT = (
    "[卡片消息] 图文H5\n"
    "摘要: [分享]潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布\n"
    "tag: 小黑盒\n"
    "title: 潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布\n"
    "desc: 下载小黑盒查看更多精彩内容\n"
    "jump_url: https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?"
    "h_camp=link&h_session_id=VrYGuyArG6zTrT0z&h_src=YXBwX3NoYXJl&link_id=9e8e726f0c52"
)

GID = "default_testinstance:GroupMessage:TESTGROUP00000000000000000000000000"


def make_plain(text: str):
    seg = Plain()
    seg.text = text
    return seg


def make_reply(text: str = "", chain=None):
    seg = Reply()
    seg.id = "1"
    seg.chain = chain or []
    seg.message_str = text
    seg.text = ""
    return seg


class FakeEvent:
    def __init__(self, chain, message_str="", sender="小帅", origin=GID):
        self._chain = chain
        self.message_str = message_str
        self._sender = sender
        self.unified_msg_origin = origin

    def get_messages(self):
        return self._chain

    def get_sender_name(self):
        return self._sender


async def main() -> int:
    cfg = ReadConfig.from_raw({})
    cache = ShareCache(size=5, ttl=1800)

    print("=== 1. 群友发卡片 → 静默缓存（线上 13:10 那一刻）===")
    share_event = FakeEvent([make_plain(CARD_TEXT)], message_str=CARD_TEXT, sender="!?")
    items = await collect_from_event(share_event, cfg)
    usable = [i for i in items if not i.is_video and i.has_content()]
    check("抽到 1 条条目", len(usable), 1)
    check("类型是分享卡片", usable[0].kind, "card")
    check("标题解析正确", usable[0].title, "潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布")
    check_true("摘要解析正确", usable[0].desc.startswith("[分享]潮玩星球"))
    check_true("链接解析正确", usable[0].url.startswith("https://api.xiaoheihe.cn/"))

    for it in usable:
        cache.put(GID, it, sender="!?")
    check("缓存已写入", len(cache.recent(GID, 5)), 1)

    print("\n=== 2. 一小时后小帅说「总结一下这个里面信息」（无引用）===")
    ask_event = FakeEvent(
        [make_plain("总结一下这个里面信息")],
        message_str="总结一下这个里面信息",
        sender="小帅",
    )
    ask_items = await collect_from_event(ask_event, cfg)
    usable_ask = [i for i in ask_items if not i.is_video and i.has_content()]
    check("提问本身抽不到内容（符合预期）", len(usable_ask), 0)

    hit = [i for i in cache.recent(GID, 5) if i.has_content()]
    check_true("从缓存里捞到了卡片", len(hit) == 1)
    check("捞到的是同一张卡", hit[0].title, usable[0].title)

    prompt_piece = hit[0].render(4000)
    check_true("渲染后带标题", "潮玩星球" in prompt_piece)
    check_true("渲染后带摘要", "[分享]潮玩星球" in prompt_piece)
    check_true("渲染后带链接", "api.xiaoheihe.cn" in prompt_piece)
    check_true("渲染后带标签", "小黑盒" in prompt_piece)

    print("\n=== 3. 引用卡片问（线上 13:37 那一刻）===")
    reply_event = FakeEvent(
        [make_reply(CARD_TEXT), make_plain("小蜗总结一下这个里面信息")],
        message_str="小蜗总结一下这个里面信息",
        sender="小帅",
    )
    r_items = await collect_from_event(reply_event, cfg)
    r_usable = [i for i in r_items if not i.is_video and i.has_content()]
    check("引用也能抽到卡片", len(r_usable), 1)
    check("引用卡片标题一致", r_usable[0].title, usable[0].title)
    check_true("引用卡片摘要一致", r_usable[0].desc.startswith("[分享]潮玩星球"))

    # Reply 组件把文本放在 text 语法（另一种真实现象）
    r2 = make_reply()
    r2.message_str = ""
    r2.text = CARD_TEXT
    r2_event = FakeEvent([r2, make_plain("这个是啥")], message_str="这个是啥")
    r2_items = await collect_from_event(r2_event, cfg)
    r2_usable = [i for i in r2_items if not i.is_video and i.has_content()]
    check("Reply.text 里的卡片也能读到", len(r2_usable), 1)

    # ⚠️ 线上回归：qq_official 的引用卡片**同时**带 chain 和 message_str，
    # 两边都解析会让同一张卡被缓存两次（线上日志出现过「已缓存 2 条」）。
    r3 = make_reply(CARD_TEXT, chain=[make_plain(CARD_TEXT)])
    r3_event = FakeEvent([r3, make_plain("小蜗总结一下这个内容")], message_str="小蜗总结一下这个内容")
    r3_usable = [i for i in await collect_from_event(r3_event, cfg)
                 if not i.is_video and i.has_content()]
    check("chain + message_str 同时存在时不重复计数", len(r3_usable), 1)

    # Json 与 Plain 同时出现同一张卡（未来平台混发）也不重复
    r4_usable = [i for i in await collect_from_event(
        FakeEvent([make_plain(CARD_TEXT), make_plain(CARD_TEXT)], message_str=CARD_TEXT), cfg)
        if not i.is_video and i.has_content()]
    check("同一张卡出现两次只留一条", len(r4_usable), 1)

    print("\n=== 4. 群里日常闲聊不能误记 ===")
    for noise in ["上海好地方啊", "那两个5星要练吗", "不用吧", "好的"]:
        ev = FakeEvent([make_plain(noise)], message_str=noise)
        got = await collect_from_event(ev, cfg)
        check(f"闲聊不入库 {noise!r}",
              len([i for i in got if not i.is_video and i.has_content()]), 0)

    print("\n=== 5. 裸链接仍然要能读 ===")
    link = "https://www.xiaoheihe.cn/bbs/post_detail?post_id=123456"
    ev = FakeEvent([make_plain(f"看看这个 {link}")], message_str=f"看看这个 {link}")
    got = [i for i in await collect_from_event(ev, cfg) if not i.is_video]
    check("裸链接抽到 1 条", len(got), 1)
    check("裸链接地址正确", got[0].url, link)

    print("\n=== 6. 视频卡片要被挡掉 ===")
    video_card = (
        "[卡片消息] 视频\ntitle: 某视频\n"
        "jump_url: https://www.bilibili.com/video/BV1xx411c7mD"
    )
    ev = FakeEvent([make_plain(video_card)], message_str=video_card)
    got = await collect_from_event(ev, cfg)
    check("视频卡片被标记为视频", [i.is_video for i in got], [True])

    print("\n" + "=" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
