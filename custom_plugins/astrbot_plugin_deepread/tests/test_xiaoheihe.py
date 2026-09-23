"""小黑盒内容读取的离线单测。

分三块：
  1. **签名算法**——用线上抓包的真实三元组做基准向量。
     这是全插件最脆的一环：算法藏在分享页的混淆 bundle 里，
     一旦小黑盒改版就必须重新还原。有基准向量才能一眼看出「是不是签名坏了」，
     而不是把「签名失效」误报成「接口挂了」。
  2. **link_id 提取**——三种真实链接形态都要认得出。
  3. **响应解析**——用真实响应（tests/fixtures/xhh_tree.json，34KB 抓包）驱动，
     不联网。

用法（Windows）:
  <python> tests/test_xiaoheihe.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from core.xiaoheihe import (  # noqa: E402
    API_PATH,
    build_sign_params,
    is_xiaoheihe_url,
    link_id_of,
    make_nonce,
    parse_link_tree,
    sign_hkey,
    verify_alignment,
)

PASS, FAIL = 0, 0
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "xhh_tree.json")


def check(name: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         actual  = {actual!r}\n         expected= {expected!r}")


def check_true(name: str, cond, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"\n         {extra}" if extra else ""))


def check_in(name: str, needle: str, haystack: str) -> None:
    check_true(name, needle in (haystack or ""),
               extra=f"要找 {needle!r}，实际内容：\n{(haystack or '')[:600]}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------- #
# 1. 签名算法
# --------------------------------------------------------------------- #
def test_sign():
    section("1. hkey 签名算法（基准向量来自线上抓包）")

    # 2026-09-11 浏览器实际发出的那一发请求（分享页 /app/bbs/link/9e8e726f0c52）
    ORIG_TIME = 1789110819
    ORIG_NONCE = "47414276A2361361AE6F95E683356C21"  # secret-ok 过期签名样本
    ORIG_HKEY = "WI0I701"

    check("复现线上 hkey", sign_hkey(API_PATH, ORIG_TIME, ORIG_NONCE), ORIG_HKEY)
    check_true("自洽校验通过", verify_alignment(API_PATH, ORIG_TIME, ORIG_NONCE, ORIG_HKEY))

    # hkey 把 _time 也签进去了 —— 这是「不能抓一次重放」的根据
    check_true("改 _time 高位会产生不同签名（所以不能重放）",
               sign_hkey(API_PATH, ORIG_TIME + 1_000_000_000, ORIG_NONCE) != ORIG_HKEY)
    check_true("改 nonce 首位会产生不同签名",
               sign_hkey(API_PATH, ORIG_TIME, "5" + ORIG_NONCE[1:]) != ORIG_HKEY)
    check_true("改 path 早段会产生不同签名",
               sign_hkey("/xbbs/app/link/tree", ORIG_TIME, ORIG_NONCE) != ORIG_HKEY)

    # ⚠️ 反直觉但真实的特性：种子只取「交错后前 20 个字符」，
    # 而三个分量是 round-robin 交错的（i[0],o[0],s[0],i[1],o[1],s[1],…），
    # 于是每个分量只有**前 7 位**进入签名。这解释了一个曾经很迷惑的现象：
    # 拿抓到的 hkey 去重放，_time 改成「当前时间」在几分钟内居然能过，
    # 过一会儿又突然「非法请求」——因为 _time 是 10 位，前 7 位每约 181 秒才跨一格。
    # 这也正是「抓一次重放」这条路走不通的根本原因。
    check("_time 末位不影响签名（前 7 位没变）",
          sign_hkey(API_PATH, ORIG_TIME + 1, ORIG_NONCE), ORIG_HKEY)
    check("_time 变动在同一个七位窗口内仍等价",
          sign_hkey(API_PATH, ORIG_TIME + 100, ORIG_NONCE), ORIG_HKEY)
    check_true("跨过七位窗口就换签名",
               sign_hkey(API_PATH, ORIG_TIME + 10_000_000, ORIG_NONCE) != ORIG_HKEY)
    check("nonce 末位不影响签名",
          sign_hkey(API_PATH, ORIG_TIME, ORIG_NONCE[:-1] + "0"), ORIG_HKEY)
    check("path 末段不影响签名（前 7 位是 /bbs/ap）",
          sign_hkey("/bbs/app/link/treeXXXX", ORIG_TIME, ORIG_NONCE), ORIG_HKEY)

    # 形状：7 个字符，取自特定字母表
    hk = sign_hkey(API_PATH, ORIG_TIME, ORIG_NONCE)
    check("hkey 长度恒为 7", len(hk), 7)
    check_true("hkey 全部出自签名字母表",
               all(ch in "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89" for ch in hk))

    # path 归一化：前后多个斜杠应当等价
    check("path 归一化（多余斜杠不影响）",
          sign_hkey("//bbs/app/link/tree/", ORIG_TIME, ORIG_NONCE), ORIG_HKEY)

    # 现签的每套都要自洽
    sp = build_sign_params(API_PATH)
    check_true("现签三元组自洽",
               verify_alignment(API_PATH, sp._time, sp.nonce, sp.hkey))
    check_true("现签 hkey 仍是 7 位", len(sp.hkey) == 7)
    check_true("_time 是当前 unix 秒", abs(sp._time - __import__("time").time()) < 60)

    # nonce：32 位大写 hex，且两次不重复
    n1, n2 = make_nonce(), make_nonce()
    check("nonce 长度 32", len(n1), 32)
    check_true("nonce 是大写 hex", all(c in "0123456789ABCDEF" for c in n1))
    check_true("nonce 每次都不同", n1 != n2)

    # 签名对同一输入必须稳定（纯函数）
    check("签名可重复", sign_hkey(API_PATH, ORIG_TIME, ORIG_NONCE),
          sign_hkey(API_PATH, ORIG_TIME, ORIG_NONCE))


# --------------------------------------------------------------------- #
# 2. link_id 提取
# --------------------------------------------------------------------- #
def test_link_id():
    section("2. link_id 提取")

    check("api 分享链接",
          link_id_of("https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?"
                     "h_camp=link&h_src=YXBwX3NoYXJl&link_id=9e8e726f0c52"
                     "&new_post_share_style=true"),
          "9e8e726f0c52")
    check("网页地址", link_id_of("https://www.xiaoheihe.cn/app/bbs/link/9e8e726f0c52"),
          "9e8e726f0c52")
    check("带 query 的网页地址",
          link_id_of("https://www.xiaoheihe.cn/app/bbs/link/9e8e726f0c52?x_os_type=iOS"),
          "9e8e726f0c52")
    check("link/tree 接口地址",
          link_id_of("https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=abc123def456"),
          "abc123def456")
    check("heybox.com 域名", link_id_of("https://heybox.com/app/bbs/link/deadbeef00"), "deadbeef00")
    check("取不到时返回空串", link_id_of("https://www.xiaoheihe.cn/"), "")
    check("空串安全", link_id_of(""), "")
    check("非小黑盒链接不误取",
          link_id_of("https://example.com/bbs/link/9e8e726f0c52?link_id=zz"), "zz")

    check_true("域名识别：xiaoheihe", is_xiaoheihe_url("https://www.xiaoheihe.cn/x"))
    check_true("域名识别：heybox", is_xiaoheihe_url("https://heybox.com/x"))
    check("域名识别：其它站点", is_xiaoheihe_url("https://example.com/x"), False)


# --------------------------------------------------------------------- #
# 3. 真实响应解析
# --------------------------------------------------------------------- #
def test_parse_real():
    section("3. 真实响应解析（离线 fixture）")

    if not os.path.exists(FIXTURE):
        check_true(f"fixture 存在：{FIXTURE}", False)
        return
    with open(FIXTURE, encoding="utf-8") as f:
        payload = json.load(f)

    post = parse_link_tree(payload)
    check_true("解析成功", post.ok)

    check("标题", post.title, "潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布")
    check("作者", post.author, "不想当jerry")
    check("内部 linkid 回填", post.link_id, "9e8e726f0c52")
    check_true("create_at 已解析", post.create_at > 1_700_000_000)

    body = post.body
    # 正文必须来自帖子真实内容，而不是标题的脑补
    check_in("正文含「IPSTAR潮玩星球」", "IPSTAR潮玩星球", body)
    check_in("正文含开业日期", "2026年9月19日", body)
    check_in("正文含地址", "南京西路", body)
    check_in("正文含营业时间", "10:00-22:00", body)
    check_in("正文含餐品数量", "6款主食", body)
    check_in("标出帖子类型", "【小黑盒帖子】", body)

    # 元信息
    check_in("带作者与等级", "作者：不想当jerry", body)
    check_in("带 IP 属地", "IP 广东", body)
    check_in("带话题", "话题：怪物猎人：荒野", body)
    check_in("带互动数据", "评论 107", body)
    check_in("带点赞数", "点赞 182", body)
    check_in("带原创声明", "声明：原创", body)

    # 配图：正文内嵌的 <img data-original> + 独立 img 块，去重后 15 张
    check("配图数量", len(post.images), 15)
    check_true("配图都是 http(s) 链接",
               all(u.startswith("http") for u in post.images))
    check_true("配图无重复", len(set(post.images)) == len(post.images))
    check_in("正文里标出配图张数", "配图：15 张", body)

    # 热评
    check_in("带热评小节", "热评（前 3 条）", body)
    check_true("热评里没有 heybox:// 噪声", "heybox://" not in body)

    # 可点开的地址
    check("可点开地址", post.real_url,
          "https://www.xiaoheihe.cn/app/bbs/link/9e8e726f0c52")
    check("note 说明来源", post.note, "来自小黑盒分享接口（link/tree，实时签名）")

    # 正文里不该残留 HTML 标签
    check_true("正文无残留标签", "<p>" not in body and "<img" not in body)


# --------------------------------------------------------------------- #
# 4. 异常响应
# --------------------------------------------------------------------- #
def test_parse_errors():
    section("4. 异常响应要有准确说法")

    illegal = parse_link_tree({"status": "failed", "msg": "非法请求", "result": {}})
    check("签名被拒", illegal.ok, False)
    check_in("签名被拒的原因", "非法请求", illegal.note)
    check_in("提示算法可能变更", "变更", illegal.note)

    # 风控要求人机验证：签名是过关的，被挡是频率问题，必须与「签名坏了」区分开
    captcha = parse_link_tree({"status": "show_captcha", "msg": "", "result": {}})
    check("人机验证被识别", captcha.ok, False)
    check_in("说清是风控不是签名", "人机验证", captcha.note)
    check_in("指出频率是诱因", "频繁", captcha.note)
    check_in("给出可走的路", "截图", captcha.note)
    check_true("不与签名失效混淆", "变更" not in captcha.note)

    gone = parse_link_tree({"status": "ok", "result": {"link": {}}})
    check("帖子为空", gone.ok, False)
    check_in("说明帖子取不到", "没返回帖子内容", gone.note)

    other = parse_link_tree({"status": "failed", "msg": "请求失败了", "result": {}})
    check("其它失败", other.ok, False)
    check_in("带出原始 msg", "请求失败了", other.note)

    check("非 dict 输入不炸", parse_link_tree(None).ok, False)
    check("非 dict 输入有说明", bool(parse_link_tree("x").note), True)

    # link.text 直接是 HTML 字符串（老形态）时的兼容
    legacy = parse_link_tree({
        "status": "ok",
        "result": {"link": {
            "title": "老形态",
            "text": "<h2>标题</h2><p>正文一段</p><img data-original=\"https://x/y.jpg\"/>",
            "share_url": "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=old12345",
        }},
    })
    check_true("兼容 text 直接是 HTML", legacy.ok)
    check_in("老形态正文", "正文一段", legacy.body)
    check("老形态图片", legacy.images, ["https://x/y.jpg"])
    check("老形态 link_id", legacy.link_id, "old12345")

    # 纯图帖：没有文字，退回 description
    imgs_only = parse_link_tree({
        "status": "ok",
        "result": {"link": {
            "title": "纯图帖",
            "description": "这是服务端直出的真实摘要内容，长度足够",
            "text": json.dumps([{"type": "img", "url": "https://x/a.jpg"}]),
            "share_url": "https://api.xiaoheihe.cn/x?link_id=img1234567",
        }},
    })
    check_in("纯图帖退回摘要", "服务端直出的真实摘要", imgs_only.body)
    check("纯图帖收到图", imgs_only.images, ["https://x/a.jpg"])


def main():
    print("=" * 56)
    print("deepread 小黑盒读取测试")
    print("=" * 56)

    test_sign()
    test_link_id()
    test_parse_real()
    test_parse_errors()

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
