"""卡片解析的离线单测。不依赖 AstrBot、不发网络请求。

用法（Windows）:
  <python> tests/test_cards.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from core.cards import (  # noqa: E402
    extract_host,
    extract_urls,
    is_video_card,
    parse_card,
    parse_card_text,
)
from core.config import ReadConfig  # noqa: E402

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


# ---------------------------------------------------------------- 样例卡片
# 第三方 App 卡片（小黑盒 / 潮玩星球这类），走 detail_1
CARD_DETAIL_1 = {
    "app": "com.tencent.structmsg",
    "view": "news",
    "prompt": "[分享] 潮玩星球 新品发售",
    "meta": {
        "detail_1": {
            "appid": "1101234567",
            "apptype": 0,
            "title": "潮玩星球 | 神秘盲盒系列今日开售",
            "desc": "限定款共 12 只，附赠隐藏款抽取卡",
            "qqdocurl": "https://www.xiaoheihe.cn/bbs/post_detail?post_id=123456",
            "preview": "https://imgheybox.max-c.com/cover/abc.jpg",
            "tag": "小黑盒",
            "host": {"nick": "小黑盒", "uin": 10001},
            "flag": 0,
            "ver": "0.0.0.1",
        }
    },
    "config": {"ctime": 1757500000, "forward": 1},
}

# 标准 news 图文卡片
CARD_NEWS = {
    "app": "com.tencent.structmsg",
    "view": "news",
    "meta": {
        "news": {
            "title": "这篇公众号文章讲了什么",
            "desc": "第 1 段摘要，第 2 段摘要",
            "jumpUrl": "https://mp.weixin.qq.com/s/AbCdEfGhIjK",
            "preview": "https://mmbiz.qpic.cn/xxx/0",
            "tag": "微信公众号",
        }
    },
}

# 视频卡片（应被识别并跳过）
CARD_VIDEO = {
    "app": "com.tencent.structmsg",
    "view": "video",
    "meta": {
        "video": {
            "title": "某个视频",
            "desc": "视频简介",
            "jumpUrl": "https://www.bilibili.com/video/BV1GJ411x7h7",
            "preview": "https://i0.hdslb.com/xxx.jpg",
        }
    },
}

# 结构未知的卡片（本插件必须仍能挖出信息）
CARD_UNKNOWN_SHAPE = {
    "app": "com.tencent.structmsg",
    "view": "custom",
    "meta": {
        "weird_app_card": {
            "nested": {"deep": {"title": "深层标题", "desc": "深层描述"}},
            "shareUrl": "https://example.com/post/1",
            "cover": "https://example.com/c.png",
        }
    },
}

# JSON 字符串形态
import json  # noqa: E402

CARD_AS_STRING = json.dumps(CARD_DETAIL_1, ensure_ascii=False)


def main() -> int:
    print("=== 1. detail_1 卡片（小黑盒/潮玩星球类）===")
    info = parse_card(CARD_DETAIL_1)
    check_true("解析成功", info is not None)
    check("标题", info.title, "潮玩星球 | 神秘盲盒系列今日开售")
    check("摘要", info.desc, "限定款共 12 只，附赠隐藏款抽取卡")
    check("跳转链接(qqdocurl)", info.url, "https://www.xiaoheihe.cn/bbs/post_detail?post_id=123456")
    check("缩略图", info.preview, "https://imgheybox.max-c.com/cover/abc.jpg")
    check("卡片类型", info.card_type, "detail_1")
    check_true("来源字段保留(host.nick 或 tag)", "小黑盒" in str(info.fields))
    check_true("噪声字段已剔除(appid)", "appid" not in info.fields)
    check_true("噪声字段已剔除(flag/ver)", "flag" not in info.fields and "ver" not in info.fields)

    print("\n=== 2. news 卡片（公众号类）===")
    info2 = parse_card(CARD_NEWS)
    check("标题", info2.title, "这篇公众号文章讲了什么")
    check("跳转链接(jumpUrl)", info2.url, "https://mp.weixin.qq.com/s/AbCdEfGhIjK")
    check("标签", info2.fields.get("tag"), "微信公众号")

    print("\n=== 3. 视频卡片识别 ===")
    info3 = parse_card(CARD_VIDEO)
    cfg = ReadConfig.from_raw({})
    hit, host = is_video_card(info3, cfg.video_hosts)
    check_true("B站视频卡片被识别", hit)
    check("命中原因", host in ("bilibili.com", "view=video"), True)

    print("\n=== 4. 未知结构的卡片仍能挖出信息 ===")
    info4 = parse_card(CARD_UNKNOWN_SHAPE)
    check_true("解析成功", info4 is not None)
    check("深层标题被挖出", info4.title, "深层标题")
    check("深层描述被挖出", info4.desc, "深层描述")
    check("shareUrl 被识别为跳转链接", info4.url, "https://example.com/post/1")

    print("\n=== 5. JSON 字符串输入 ===")
    info5 = parse_card(CARD_AS_STRING)
    check_true("字符串形态可解析", info5 is not None)
    check("标题一致", info5.title, info.title)

    print("\n=== 6. 非卡片输入 ===")
    check("None 输入", parse_card(None), None)
    check("空 dict 输入", parse_card({}), None)
    check("垃圾字符串输入", parse_card("not a json"), None)
    check("普通 dict（无 meta）", parse_card({"foo": "bar"}), None)

    print("\n=== 7. URL 工具 ===")
    check("取 host", extract_host("https://www.xiaoheihe.cn/bbs/post"), "www.xiaoheihe.cn")
    check("取 host 带端口", extract_host("http://example.com:8080/a"), "example.com")
    check("取 host 空", extract_host(""), "")
    check(
        "文本提链接",
        extract_urls("看看这个 https://a.com/x 还有 https://b.cn/y。"),
        ["https://a.com/x", "https://b.cn/y"],
    )
    check("文本无链接", extract_urls("没有链接"), [])

    print("\n=== 8. 视频黑名单判定 ===")
    for u, expect in [
        ("https://www.bilibili.com/video/BV1xx", True),
        ("https://b23.tv/abc", True),
        ("https://v.douyin.com/abc/", True),
        ("https://www.xiaoheihe.cn/bbs/post_detail?post_id=1", False),
        ("https://mp.weixin.qq.com/s/xxx", False),
        ("https://github.com/a/b", False),
    ]:
        host = extract_host(u)
        got = any(host == b or host.endswith("." + b) for b in cfg.video_hosts)
        check(f"视频判定 {host}", got, expect)

    print("\n=== 9. ReadItem 渲染（摘要不能丢）===")
    from core.models import KIND_CARD, ReadItem

    item = ReadItem(
        kind=KIND_CARD,
        title="潮玩星球开售",
        desc="限定款共 12 只，附赠隐藏款抽取卡",
        url="https://www.xiaoheihe.cn/bbs/post_detail?post_id=1",
        meta_fields={"tag": "小黑盒"},
    )
    rendered = item.render(500)
    check_true("渲染含标题", "标题：潮玩星球开售" in rendered)
    check_true("渲染含摘要", "摘要：限定款共 12 只" in rendered)
    check_true("渲染含链接", "链接：https://www.xiaoheihe.cn" in rendered)
    check_true("渲染含卡片字段", "标签：小黑盒" in rendered)
    check_true("只有摘要也算有内容", ReadItem(kind=KIND_CARD, desc="只有摘要").has_content())
    check_true("回执提到摘要", "摘要" in item.summary_line())

    # ---------------------------------------------------------------- #
    print("\n=== 10. 文本卡片（QQ 官方机器人拍平形态，线上真实样本）===")
    # 样本抄自线上脱敏后的 platform_message_history
    CARD_TEXT_HEYBOX = (
        "[卡片消息] 图文H5\n"
        "摘要: [分享]潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布\n"
        "tag: 小黑盒\n"
        "title: 潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布\n"
        "desc: 下载小黑盒查看更多精彩内容\n"
        "jump_url: https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?"
        "h_camp=link&h_session_id=VrYGuyArG6zTrT0z&h_src=YXBwX3NoYXJl&link_id=9e8e726f0c52"
    )
    info = parse_card_text(CARD_TEXT_HEYBOX)
    check_true("文本卡片可解析", info is not None)
    check("文本卡片 card_type", info.card_type, "图文H5")
    check("文本卡片标题", info.title, "潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布")
    check_true("摘要取的是「摘要」而不是推广 desc", info.desc.startswith("[分享]潮玩星球"))
    check_true("链接取自 jump_url", info.url.startswith("https://api.xiaoheihe.cn/"))
    check("文本卡片标签", info.fields.get("tag"), "小黑盒")
    check_true("推广 desc 不单独展示", "desc" not in info.fields)

    # 字段顺序不同也要稳（摘要仍然优先）
    CARD_TEXT_REORDERED = (
        "[卡片消息] 图文H5\n"
        "desc: 下载小黑盒查看更多精彩内容\n"
        "jump_url: https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=9e8e\n"
        "title: 怪物猎人荒野主题店\n"
        "摘要: 潮玩星球官方授权主题店详情\n"
        "tag: 小黑盒"
    )
    info2 = parse_card_text(CARD_TEXT_REORDERED)
    check_true("乱序也可解析", info2 is not None)
    check("乱序摘要优先", info2.desc, "潮玩星球官方授权主题店详情")
    check("乱序标题", info2.title, "怪物猎人荒野主题店")

    # 小程序卡片（音乐），带 source_logo
    CARD_TEXT_MINIAPP = (
        "[卡片消息] 小程序\n"
        "摘要: [QQ小程序]08年美国讽刺金曲《朋友的酒》（英语填词cover）“空城计の小曲”\n"
        "source_logo: http://miniapp.gtimg.cn/public/appicon/432b76be3a548fc128acaa6c1ec90131_200.jpg\n"
        "title: 08年美国讽刺金曲《朋友的酒》\n"
        "preview: https://p.qpic.cn/x.jpg\n"
        "jump_url: https://miniapp.gtimg.cn/app/x?appid=110"
    )
    info3 = parse_card_text(CARD_TEXT_MINIAPP)
    check_true("小程序卡片可解析", info3 is not None)
    check("小程序 card_type", info3.card_type, "小程序")
    check_true("source_logo 被当作图片不占链接位", info3.url.startswith("https://miniapp.gtimg.cn/app/"))
    check("preview 优先于 source_logo", info3.preview, "https://p.qpic.cn/x.jpg")
    check_true("source_logo 兜底可用", parse_card_text(
        "[卡片消息] 小程序\ntitle: 无封面卡\nsource_logo: http://miniapp.gtimg.cn/a.jpg\n"
        "jump_url: https://miniapp.gtimg.cn/app/y"
    ).preview.startswith("http://miniapp.gtimg.cn/"))

    # 负样本：普通聊天文本绝不能误判成卡片
    for noise in [
        "上海好地方啊",
        "小蜗总结一下这个里面信息",
        "那两个5星要练吗",
        "",
        "   ",
        "https://www.xiaoheihe.cn/bbs/post_detail?post_id=1",
    ]:
        check(f"普通文本不误判 {noise[:14]!r}", parse_card_text(noise), None)

    # 关键字段别被 URL 里的冒号带偏
    info4 = parse_card_text("jump_url: https://a.com/x?y=1\n标题: 测试")
    check_true("URL 不被误读成字段", info4 is not None and info4.url == "https://a.com/x?y=1")
    check("中文键「标题」可识别", info4.title, "测试")

    # 视频文本卡片要能识别出来
    check_true(
        "文本卡片视频判定",
        is_video_card(parse_card_text("[卡片消息] 视频\ntitle: 某视频\njump_url: https://x.com/1"), cfg.video_hosts)[0],
    )

    print("\n" + "=" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
