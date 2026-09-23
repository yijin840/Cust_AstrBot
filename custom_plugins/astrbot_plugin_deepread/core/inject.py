"""把「已经读到的分享正文」直接塞进模型的请求里。

为什么要有这个模块
------------------
v1.5.0 只提供了一个 LLM 工具 `deepread_share`，指望模型在用户问「这个卡片讲的啥」
时自己挑中它。线上实测（2026-09-11 20:24，某测试群）：

    Agent 使用工具: ['web_fetch']
    使用工具：web_fetch，参数：{'url': 'https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?...'}
    Tool `web_fetch` Result: 小黑盒 - 玩家高能聚集地      ← 只有标题（页面是 JS 空壳）
    Agent 使用工具: ['gemini_search']                     ← 转头去搜
    Tool `gemini_search` Result: 阿米娅…普遍认为可爱…      ← 搜索结果
    回复：帖子里主要是在发阿米娅的各种可爱同人图…底下全是一群刀客塔在疯狂复读…

`deepread_share` 的历史调用次数是 **0**。模型有几十个工具可选，它挑了最字面、
最好理解的那个，然后拿搜索结果的常识补出了「帖子内容」。这不是提示词能根治的问题，
「可选」的工具在竞争里永远可能输。

所以这一层做的是把「可选」变成「必然发生」：
在请求真正发给模型之前，把当前消息里（或刚缓存的）分享内容抓下来，
作为临时内容块挂到用户消息末尾。模型不需要选工具，它一睁眼就看得见正文。

不做的事
--------
· 不主动发言（只在别人要问的时候把材料备好）
· 不落历史（`mark_as_temp()`，不会污染对话上下文，也不影响力前缀缓存）
· 抓不到也不编——抓不到时注入的是一句「正文没取到 + 原因」，
  这恰恰是抑制模型去搜索编造的**最有效**的一招。
"""

from __future__ import annotations

import re

from .models import KIND_IMAGE, KIND_LINK, ReadItem
from .summarizer import build_content

# 「用户在指代刚才那条分享」的信号词。
# 只在缓存路径上用——避免每来一句闲聊就把十分钟前的帖子糊到模型脸上。
_REFER = re.compile(
    r"刚刚|刚才|刚发|这个|那个|这条|那条|上面|楼上|前面|"
    r"分享|卡片|链接|帖子|视频|图|讲(的)?啥|说的啥|总结|看看|读一下|啥内容"
)

_TEMPLATE = """【已替你读好：群里这条分享的真实内容】

{material}

【使用方式（必须遵守）】
1. 上面是**真实抓取结果**，回答这块内容时直接用它。
2. 不要再用 web_fetch / 网页抓取去取同一个链接——那只会拿到 JS 空壳的标题。
3. 不要用联网搜索替它补内容。搜索结果不是这篇帖子的内容，
   把它讲成帖子的内容就是编造。
4. 如果材料里某条的正文位置写着「未取到」，那就是真没取到。如实说没读到，
   可以转述采集说明里的原因，宁可答得少。
5. 如果用户这轮问的其实不是这条分享，忽略上面这段，正常回答。"""


def looks_like_referral(text: str) -> bool:
    """消息看起来在指代「刚才那条分享」吗？"""
    return bool(_REFER.search(text or ""))


def needs_fetch(items: list[ReadItem], text: str) -> bool:
    """当前消息里的这几条，值不值得为它去抓一次。

    门槛不是「是不是链接」，而是「有没有读它的意图」：

    · **卡片 / 文件 / 转发**（kind 不是裸链接）—— 用户特意分享过来的，
      本身就是意图，直接读。
    · **裸链接** —— 群里路过贴个链接的场景太多。只有这轮消息看起来
      **在问它**（「看看这个」「总结一下」）才去抓，免得每次贴链接都让
      小蜗先卡上几秒，还塞一堆没人要的内容。
    """
    if not items:
        return False
    for item in items:
        if item.kind != KIND_LINK:
            return True
        # 卡片被降级成纯链接的情况：只要还留着卡片的标题/摘要/字段，
        # 就说明它本来就带着分享语义，不算路过
        if item.title or item.desc or item.meta_fields:
            return True
    return looks_like_referral(text)



def selected(items: list[ReadItem], max_items: int) -> list[ReadItem]:
    """挑出值得注入的条目：跳过视频卡片与纯图片（图片要另走多模态通路）。"""
    picked: list[ReadItem] = []
    for item in items:
        if item.is_video:
            continue
        if item.kind == KIND_IMAGE:
            continue
        if not item.has_content():
            continue
        picked.append(item)
        if len(picked) >= max_items:
            break
    return picked


def build_block(items: list[ReadItem], cfg) -> str | None:
    """渲染成注入用的一段文本；没有可用条目时返回 None。"""
    picked = selected(items, cfg.max_items)
    if not picked:
        return None

    material = build_content(picked, cfg.max_chars)

    images = 0
    for item in picked:
        images += len(item.extra.get("image_data_urls") or [])
    if images:
        material += (
            f"\n\n（该帖另有 {images} 张配图，本次没有展开给你；"
            "涉及图里内容时直说图没看到，不要猜。）"
        )
    return _TEMPLATE.format(material=material)
