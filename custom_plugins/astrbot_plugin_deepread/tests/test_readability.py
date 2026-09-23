"""v1.3.0「可读性判定 + 边界声明」的离线单测。

覆盖四件事：
  1. 「已知读不到」名单的正确性（v1.5.0 起小黑盒已从这里移除）；
  2. 分享用的 API 链接能换算成人可点开的网页地址；
  3. 页面元数据（OG）兜底与 SPA 空壳识别；
  4. 送进模型的材料里显式声明「实际拿到什么」——这是抑制编造的关键。

不依赖 AstrBot，不做任何网络请求。

用法（Windows）:
  <python> tests/test_readability.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from core.fetchers import (  # noqa: E402
    extract_meta,
    fetch_url,
    is_safe_url,
    looks_like_spa_shell,
    match_rule,
    match_unreadable,
    unwrap_share_url,
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
        print(f"  [FAIL] {name}\n         actual  = {actual!r}\n         expected= {expected!r}")


def check_true(name: str, cond) -> None:
    check(name, bool(cond), True)


# 真实卡片里的 jump_url（2026-09-11 线上数据）
REAL_JUMP = (
    "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?h_camp=link"
    "&h_session_id=VrYGuyArG6zTrT0z&h_src=YXBwX3NoYXJl"
    "&link_id=9e8e726f0c52&new_post_share_style=true"
)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------- #
# 1. 已知读不到的站点
# --------------------------------------------------------------------- #
def test_unreadable():
    section("1. 已知读不到的站点识别")

    # ⚠️ v1.5.0 起小黑盒**不再**属于「读不到」——
    # 之前判定为读不到是误判（用错了 x_client_type），现已走 link/tree 接口取真内容。
    # 这几条断言反过来，防止有人又把它塞回 UNREADABLE_SITES。
    check("小黑盒不再被标记为读不到", match_unreadable(REAL_JUMP), None)
    check("www 页面也不再被标记",
          match_unreadable("https://www.xiaoheihe.cn/app/bbs/link/9e8e726f0c52"), None)
    check("heybox.com 同样不被标记",
          match_unreadable("https://heybox.com/app/bbs/link/x"), None)

    check("普通站点不误判", match_unreadable("https://example.com/a/b"), None)
    check("GitHub 不误判", match_unreadable("https://github.com/foo/bar"), None)
    check("B站不误判", match_unreadable("https://www.bilibili.com/video/BV1xx"), None)
    check("空链接不误判", match_unreadable(""), None)

    # 小黑盒必须走 L2 专用接口，而不是被当成普通网页去抓
    rule = match_rule("https://www.xiaoheihe.cn/app/bbs/link/9e8e726f0c52")
    check_true("SITE_RULES 里留有 xiaoheihe 规则", rule is not None)
    check("xiaoheihe 规则指向专用 API", rule.api if rule else "", "xiaoheihe")


# --------------------------------------------------------------------- #
# 2. 分享链接换算
# --------------------------------------------------------------------- #
def test_unwrap():
    section("2. 分享链接换算成人可点开的地址")

    check("小黑盒 api → www",
          unwrap_share_url(REAL_JUMP),
          "https://www.xiaoheihe.cn/app/bbs/link/9e8e726f0c52")
    check("非分享链接原样返回",
          unwrap_share_url("https://example.com/a?b=1"),
          "https://example.com/a?b=1")
    check("空串返回空", unwrap_share_url(""), "")
    check("小黑盒站点域名但不含 link_id 时原样返回",
          unwrap_share_url("https://www.xiaoheihe.cn/"), "https://www.xiaoheihe.cn/")


# --------------------------------------------------------------------- #
# 3. 元数据兜底 + SPA 空壳
# --------------------------------------------------------------------- #
def test_meta_and_spa():
    section("3. 页面元数据兜底 / SPA 空壳识别")

    html = """<html><head>
        <title>普通标题</title>
        <meta property="og:title" content="OG 标题">
        <meta property="og:description" content="分享描述 &amp; 带实体">
        <meta name="description" content="普通描述">
        <meta property="og:image" content="https://cdn.example.com/a.jpg">
        </head><body><div id="app"></div></body></html>"""

    title, desc, image = extract_meta(html)
    check("OG 标题优先于 <title>", title, "OG 标题")
    check("OG 描述优先于普通 description", desc, "分享描述 & 带实体")
    check("OG 图片被取出", image, "https://cdn.example.com/a.jpg")

    # 只有 title 的页面
    t2, d2, i2 = extract_meta("<html><head><title>只有标题</title></head></html>")
    check("只有 <title> 时也能取到", t2, "只有标题")
    check("无描述时 desc 为空", d2, "")

    # 属性顺序颠倒（content 在前）
    t3, d3, _ = extract_meta(
        '<meta content="反序描述" property="og:description">'
        '<meta content="反序标题" property="og:title">'
    )
    check("content 在 property 之前也能解析", (t3, d3), ("反序标题", "反序描述"))

    # 单引号属性
    t4, _, _ = extract_meta("<meta property='og:title' content='单引号标题'>")
    check("单引号属性可解析", t4, "单引号标题")

    check("空 HTML 安全", extract_meta(""), ("", "", ""))

    # SPA 空壳
    spa = (
        '<!doctype html><html><head>'
        '<script type="module" src="/assets/index.js"></script>'
        '</head><body><div id="app"></div></body></html>'
    )
    check_true("典型 SPA 空壳被识别", looks_like_spa_shell(spa, "首页 登录"))

    plain = "<html><body><article>" + "正文内容" * 100 + "</article></body></html>"
    check("正文够长时不算空壳", looks_like_spa_shell(plain, "正文内容" * 100), False)

    shell = '<html><body><div id="app"></div></body></html>'
    check("有个挂载点但没脚本也算空壳", looks_like_spa_shell(shell, ""), True)


# --------------------------------------------------------------------- #
# 4. 送进模型的材料要划清边界
# --------------------------------------------------------------------- #
def test_render_boundary():
    section("4. 材料里显式声明实际拿到了什么")

    only_title = ReadItem(kind="card", title="潮玩星球联名详情公布", url="https://x/y")
    check("没正文时写明「正文：未取到」", "正文：未取到" in only_title.render(), True)
    check("收尾声明只有标题", "本条实际拿到：标题" in only_title.render(), True)
    check_true("got_line 只有标题", only_title.got_line() == "标题")

    full = ReadItem(kind="link", title="标题", body="正文" * 50)
    check_true("有正文时 got_line 带字数", "正文 100 字" in full.got_line())
    check_true("有正文时不出现「未取到」", "正文：未取到" not in full.render())

    # real_url 要露出来，方便用户自己点
    with_real = ReadItem(kind="card", title="t", url=REAL_JUMP)
    with_real.extra["real_url"] = "https://www.xiaoheihe.cn/app/bbs/link/9e8e726f0c52"
    check_true("可点开的地址出现在材料里",
               "可直接点开的地址：https://www.xiaoheihe.cn/app/bbs/link/9e8e726f0c52"
               in with_real.render())

    # 采集说明要进材料（模型据此转述原因）
    noted = ReadItem(kind="card", title="t", url="https://x/y")
    noted.note = "站点返回风控/验证页（服务器机房 IP 被拦截），未取到正文"
    check_true("采集说明进入材料", "机房 IP 被拦截" in noted.render())

    # 帖子配图已经作为附件给到模型时，要在边界声明里体现
    with_imgs = ReadItem(kind="link", title="t", body="正文" * 50)
    with_imgs.extra["image_data_urls"] = ["data:image/jpeg;base64,AAA"]
    check_true("配图计入实际拿到的东西", "配图 1 张已附" in with_imgs.got_line())

    empty = ReadItem(kind="card")
    check_true("什么都拿不到时的措辞", empty.got_line() == "什么都没拿到")


# --------------------------------------------------------------------- #
# 5. 抓取入口的拒绝路径（不发请求）
# --------------------------------------------------------------------- #
def test_fetch_guards():
    section("5. 抓取入口：不发请求就能拒掉的几种情况")

    # SSRF 防护
    res = asyncio.run(fetch_url("http://127.0.0.1/admin"))
    check("内网地址被拒", res.ok, False)
    check_true("拒绝原因可读", "内网" in res.note)

    res2 = asyncio.run(fetch_url("file:///etc/passwd"))
    check("非 http(s) 被拒", res2.ok, False)

    res3 = asyncio.run(fetch_url(""))
    check("空链接被拒", res3.ok, False)

    res4 = asyncio.run(fetch_url("https://localhost.localdomain/x"))
    check("localhost 被拒", res4.ok, False)

    check("内网判定可关闭（reject_private=False 时不拦）",
          is_safe_url("http://127.0.0.1/admin", reject_private=False)[0], True)


def main():
    print("=" * 52)
    print("deepread 可读性判定测试")
    print("=" * 52)

    test_unreadable()
    test_unwrap()
    test_meta_and_spa()
    test_render_boundary()
    test_fetch_guards()

    print("\n" + "=" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
