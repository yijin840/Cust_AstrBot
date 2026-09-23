"""图片阅读理解（v1.4.0）的离线单测。

核心要守住三件事：
  1. 图片字节能在「收到的那一刻」就读出来（temp 文件会被系统定时清掉）；
  2. 超长 data URL **绝不能进日志、也绝不能进 prompt 文本**；
  3. 读不到时如实降级，不糊弄。

用法（Windows）:
  <python> tests/test_images.py
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from core.images import (  # noqa: E402
    is_local_ref,
    pick_image_ref,
    read_image_data_url,
)
from core.models import KIND_CARD, KIND_IMAGE, ReadItem  # noqa: E402

PASS, FAIL = 0, 0

# 1x1 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def test_local_ref():
    section("1. 本地引用判定")

    check_true("绝对路径算本地", is_local_ref("/srv/astrbot/data/temp/a.jpg"))
    check_true("file:// 算本地", is_local_ref("file:///srv/x.jpg"))
    check_true("base64:// 算本地", is_local_ref("base64://AAAA"))
    check_true("data URL 算本地", is_local_ref("data:image/png;base64,AAAA"))
    check("http 不算本地", is_local_ref("https://multimedia.nt.qq.com.cn/x"), False)
    check("空串不算本地", is_local_ref(""), False)


def test_read_data_url():
    section("2. 读成 data URL")

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "shot.png")
        with open(p, "wb") as f:
            f.write(PNG_BYTES)

        out = read_image_data_url(p)
        check_true("本地 png 读出 data URL", out.startswith("data:image/png;base64,"))
        check_true("内容可还原",
                   base64.b64decode(out.split(",", 1)[1]) == PNG_BYTES)

        # file:// 形式（用 as_uri 生成，跨平台才合法）
        check_true("file:// 形式可读",
                   read_image_data_url(Path(p).as_uri()).startswith("data:image/png;base64,"))

    # base64:// 直通
    b64 = base64.b64encode(PNG_BYTES).decode()
    check_true("base64:// 直通",
               read_image_data_url(f"base64://{b64}").startswith("data:image/jpeg;base64,"))

    # data URL 原样返回
    raw = "data:image/png;base64,AAAA"
    check("已是 data URL 时原样返回", read_image_data_url(raw), raw)

    # 各种读不到
    check("不存在的文件返回空", read_image_data_url("/no/such/file.jpg"), "")
    check("http 地址这里不处理", read_image_data_url("https://a.com/x.jpg"), "")
    check("空串返回空", read_image_data_url(""), "")

    # 超大文件要拒绝（避免把几百 MB 带进内存）
    with tempfile.TemporaryDirectory() as d:
        big = os.path.join(d, "big.jpg")
        with open(big, "wb") as f:
            f.write(b"\xff" * 1024)
        check("超过上限时返回空", read_image_data_url(big, max_bytes=100), "")

    # 超大 base64:// 也要拒绝
    huge = base64.b64encode(b"\xff" * 1000).decode()
    check("超大 base64:// 返回空", read_image_data_url(f"base64://{huge}", max_bytes=100), "")


def test_pick_ref():
    section("3. 候选字段里挑引用")

    local, remote = pick_image_ref("", "/tmp/a.jpg", "https://x/b.jpg")
    check("本地优先于远程", local, "/tmp/a.jpg")
    check("远程被记下来", remote, "https://x/b.jpg")

    local2, remote2 = pick_image_ref("", "", "https://x/b.jpg")
    check("只有远程时 local 为空", local2, "")
    check("只有远程时 remote 有值", remote2, "https://x/b.jpg")

    check("全空时都是空", pick_image_ref("", ""), ("", ""))
    check("忽略 None", pick_image_ref(None, ""), ("", ""))


def test_image_item_render():
    section("4. 图片条目的渲染与边界声明")

    item = ReadItem(kind=KIND_IMAGE, url="/tmp/a.jpg")
    check("没数据时不算有内容", item.has_content(), False)

    item.extra["image_data_url"] = "data:image/png;base64," + "A" * 5000
    check_true("有数据时算有内容", item.has_content())

    rendered = item.render()
    check_true("材料里说明图片是附件", "附件" in rendered or "单独提供" in rendered)
    check("**超长 data URL 绝不进材料**", "data:image" in rendered, False)
    check_true("提醒只依据实际所见", "看不清" in rendered)
    check_true("got_line 标明已读取", "已读取" in item.got_line())
    check_true("summary_line 标明已读取", "图片已读取" in item.summary_line())

    # 只有链接的图片
    linked = ReadItem(kind=KIND_IMAGE, url="https://multimedia.nt.qq.com.cn/x")
    linked.extra["image_ref"] = "https://multimedia.nt.qq.com.cn/x"
    check_true("只有链接时也算有内容", linked.has_content())
    check_true("只有链接时 got_line 如实说明", "未读到本地数据" in linked.got_line())

    # URL 是 data 时也不能进 render
    d = ReadItem(kind=KIND_IMAGE, url="data:image/png;base64,AAAA")
    d.extra["image_data_url"] = "data:image/png;base64,AAAA"
    check("data URL 作为 url 时也不输出", "data:image" in d.render(), False)


def test_card_unaffected():
    section("5. 卡片条目行为未被影响")

    card = ReadItem(kind=KIND_CARD, title="标题", url="https://example.com/a")
    check_true("卡片正常有内容", card.has_content())
    check_true("卡片仍输出链接", "https://example.com/a" in card.render())
    check_true("卡片仍带边界声明", "本条实际拿到" in card.render())
    check_true("卡片 got_line 不含图片字样", "图片" not in card.got_line())


def main():
    print("=" * 52)
    print("deepread v1.4.0 图片阅读理解测试")
    print("=" * 52)

    test_local_ref()
    test_read_data_url()
    test_pick_ref()
    test_image_item_render()
    test_card_unaffected()

    print("\n" + "=" * 52)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
