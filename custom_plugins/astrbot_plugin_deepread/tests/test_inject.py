"""预读注入测试。不联网。

复现的是 2026-09-11 20:24 那次真实故障的修复路径：

  群里收到小黑盒卡片 → 模型没选中 deepread_share，改用 web_fetch 拿了个标题
  → 转头联网搜索 → 拿搜索结果的常识编出了「帖子内容」。

这里验证的是修复的关键点：
  · 抓不到正文时，注入块里必须明写「正文：未取到」——让模型无话可编；
  · 抓到正文时，正文必须原样进块；
  · 「指代检测」不能太敏感（闲聊不许触发旧帖注入）。

用法（Windows）:
  <python> tests/test_inject.py
"""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

# 本机没装 astrbot，给 core.summarizer 的 `from astrbot.api import logger` 打个桩
if "astrbot" not in sys.modules:
    _api = types.ModuleType("astrbot.api")
    _api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    _astrbot = types.ModuleType("astrbot")
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

from core.cache import ShareCache  # noqa: E402
from core.config import ReadConfig  # noqa: E402
from core.inject import (  # noqa: E402
    build_block,
    looks_like_referral,
    needs_fetch,
    selected,
)
from core.models import ReadItem  # noqa: E402

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


XHH_URL = (
    "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share"
    "?h_camp=link&h_session_id=STCcL03RaFN36zJ2&h_src=YXBwX3NoYXJl"
    "&link_id=f0737261f7ad&new_post_share_style=true"
)


def xhh_card() -> ReadItem:
    """线上那条真实卡片（QQ 官方机器人拍平后的形态）。"""
    return ReadItem(
        kind="card",
        title="阿米娅可爱捏≽^⚈⩊⚈^≼",
        desc="下载小黑盒查看更多精彩内容",
        url=XHH_URL,
        meta_fields={"tag": "小黑盒"},
    )


# ------------------------------------------------------------------ 1. 指代检测
def test_referral() -> None:
    print("\n[1] 指代检测（决定缓存路径要不要注入）")
    check_true("「这个小黑盒内容总结一下」→ 命中", looks_like_referral("这个小黑盒内容总结一下"))
    check_true("「刚才那个链接讲的啥」→ 命中", looks_like_referral("刚才那个链接讲的啥"))
    check_true("「看看这个卡片」→ 命中", looks_like_referral("看看这个卡片"))
    check("「今天天气不错」→ 不命中", looks_like_referral("今天天气不错"), False)
    check("「在吗」→ 不命中", looks_like_referral("在吗"), False)
    check("「帮我写个快排」→ 不命中", looks_like_referral("帮我写个快排"), False)


# ------------------------------------------------------------------ 2. 抓不到时
def test_no_body() -> None:
    print("\n[2] 只拿到标题（IP 被风控 / JS 空壳）—— 注入块必须让模型无话可编")
    cfg = ReadConfig.from_raw({})
    block = build_block([xhh_card()], cfg)
    check_true("生成了注入块", block is not None)
    check_true("写明正文未取到", "正文：未取到" in block)
    check_true("带上真实标题", "阿米娅可爱捏" in block)
    check_true("禁止用搜索补内容", "不要用联网搜索替它补内容" in block)
    check_true("禁止重复抓同一链接", "web_fetch" in block)
    check_true("声明这是真实抓取结果", "真实抓取结果" in block)


# ------------------------------------------------------------------ 3. 抓到正文
def test_with_body() -> None:
    print("\n[3] 抓到正文 —— 正文必须原样进块")
    cfg = ReadConfig.from_raw({})
    item = xhh_card()
    item.body = "阿米娅六星术师，罗德岛公开领袖。" * 5
    item.layer = "L2"
    item.ok = True
    block = build_block([item], cfg)
    check_true("正文进了块", "阿米娅六星术师" in block)
    # 「未取到」这个词在规则 4 里出现一次（是指路的说法），
    # 但**带标签的形态**（正文：未取到）只能来自条目本身，有正文时必须 0 次。
    check("条目没有被误标成「正文：未取到」", block.count("正文：未取到"), 0)
    check("规则 4 仍然点明了该怎么判读", block.count("未取到"), 1)
    check_true("标了实际拿到多少字", "本条实际拿到" in block)


# ------------------------------------------------------------------ 4. 配图提示
def test_images_note() -> None:
    print("\n[4] 帖子有配图但本次没展开 —— 必须说明，否则模型会假装看过图")
    cfg = ReadConfig.from_raw({})
    item = xhh_card()
    item.body = "正文若干"
    item.extra["image_data_urls"] = ["data:image/jpeg;base64,AAAA"] * 3
    block = build_block([item], cfg)
    check_true("写明配图张数", "3 张配图" in block)
    check_true("要求不许猜图里内容", "不要猜" in block)


# ------------------------------------------------------------------ 5. 过滤规则
def test_selection() -> None:
    print("\n[5] 过滤：视频卡片与图片不能进注入块")
    cfg = ReadConfig.from_raw({})
    video = ReadItem(kind="card", title="某视频", url="https://www.bilibili.com/video/BV1xx")
    video.is_video = True
    image = ReadItem(kind="image", title="", extra={"image_data_url": "data:image/png;base64,AA"})
    text_card = xhh_card()
    picked = selected([video, image, text_card], 5)
    check("只挑出文本卡片", len(picked), 1)
    check("挑中的是卡片", picked[0].title, text_card.title)
    check("全是视频/图片时返回 None", build_block([video, image], cfg), None)
    check("空列表返回 None", build_block([], cfg), None)


# ------------------------------------------------------------------ 6. 配置解析
def test_config() -> None:
    print("\n[6] 新增配置项的默认值与解析")
    cfg = ReadConfig.from_raw({})
    check("enable_prefetch 默认开", cfg.enable_prefetch, True)
    check("prefetch_window 默认 600s", cfg.prefetch_window, 600)
    check("显式关闭生效", ReadConfig.from_raw({"enable_prefetch": False}).enable_prefetch, False)
    check("显式 0 生效（只读当前消息）",
          ReadConfig.from_raw({"prefetch_window": 0}).prefetch_window, 0)
    check("非法值回落到默认",
          ReadConfig.from_raw({"prefetch_window": "abc"}).prefetch_window, 600)
    check("超范围被夹住",
          ReadConfig.from_raw({"prefetch_window": 99999}).prefetch_window, 3600)


# ------------------------------------------------------------------ 7. 抓取门槛
def test_needs_fetch() -> None:
    print("\n[7] 抓取门槛：卡片必读，裸链接要看有没有在读它")
    card = xhh_card()  # kind="card"
    link = ReadItem(kind="link", url="https://example.com/a")
    rich_link = ReadItem(
        kind="link", url="https://example.com/b", title="某篇文章", desc="摘要"
    )
    check_true("卡片 + 无指代词 → 要抓", needs_fetch([card], "阿米娅可爱捏≽^⚈⩊⚈^≼"))
    check_true("卡片 + 有指代词 → 要抓", needs_fetch([card], "这个总结一下"))
    check("裸链接 + 无指代词 → 不抓", needs_fetch([link], "https://example.com/a"), False)
    check_true("裸链接 + 有指代词 → 要抓", needs_fetch([link], "看看 https://example.com/a"))
    check_true("带卡片信息的降级链接 → 要抓", needs_fetch([rich_link], "https://x/b"))
    check("空列表 → 不抓", needs_fetch([], "看看这个"), False)


# ------------------------------------------------------------------ 8. 缓存时效
def test_latest_age() -> None:
    print("\n[8] 缓存时效：判断「刚才那条」到底还算不算刚才")
    cache = ShareCache(size=5, ttl=1800)
    check("空缓存返回 None", cache.latest_age("s1"), None)
    cache.put("s1", xhh_card())
    age = cache.latest_age("s1")
    check_true("刚写入的 age 接近 0", age is not None and age < 1.0)
    check("会话隔离", cache.latest_age("s2"), None)


def main() -> int:
    print("=" * 62)
    print("deepread v1.6.0 预读注入测试")
    print("=" * 62)
    test_referral()
    test_no_body()
    test_with_body()
    test_images_note()
    test_selection()
    test_config()
    test_needs_fetch()
    test_latest_age()
    print("\n" + "=" * 62)
    print(f"通过 {PASS} / 失败 {FAIL}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
