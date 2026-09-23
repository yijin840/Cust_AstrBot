"""预览：真实卡片走完整流程后，送给模型的材料长什么样。

用 2026-09-11 线上的真实卡片文本跑一遍（不依赖 AstrBot、不走网络，
小黑盒那类站点会短路返回），把最终 Prompt 材料打印出来自查：
模型手上到底有什么、有没有被明确告知「哪些没拿到」。
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))


# AstrBot 不在本地环境时打个桩，好让 core.summarizer 能被 import 进来
def _stub_astrbot() -> None:
    if "astrbot" in sys.modules:
        return
    pkg = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class _Logger:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    api.logger = _Logger()
    pkg.api = api
    sys.modules["astrbot"] = pkg
    sys.modules["astrbot.api"] = api


_stub_astrbot()

from core.config import ReadConfig  # noqa: E402
from core.extractor import card_item_from_text  # noqa: E402
from core.fetchers import fetch_url  # noqa: E402
from core.summarizer import build_content  # noqa: E402

# 原始线上数据（rowid=1395）
RAW = (
    "[卡片消息] 图文H5\n"
    "摘要: [分享]潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布\n"
    "desc: 下载小黑盒查看更多精彩内容\n"
    "jump_url: https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?h_camp=link"
    "&h_session_id=VrYGuyArG6zTrT0z&h_src=YXBwX3NoYXJl"
    "&link_id=9e8e726f0c52&new_post_share_style=true\n"
    "tag: 小黑盒\n"
    "title: 潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布"
)


def main():
    cfg = ReadConfig()

    item = card_item_from_text(RAW, cfg, sender="小帅")
    if item is None:
        print("!! 卡片没解析出来")
        return 1

    print("=" * 66)
    print("第 1 步  只解析卡片（L0）—— 只有标题时模型会编")
    print("=" * 66)
    print(item.render())

    print("\n" + "=" * 66)
    print("第 2 步  顺链接点进去（补抓）")
    print("=" * 66)
    res = asyncio.run(fetch_url(item.url))
    print(f"ok={res.ok}  layer={res.layer}")
    print(f"note={res.note}")
    print(f"extra={res.extra}")
    if res.title and not item.title:
        item.title = res.title
    if res.body:
        item.body = res.body
        item.layer = res.layer or item.layer
    if res.note:
        item.note = res.note
    for k, v in (res.extra or {}).items():
        if v and not item.extra.get(k):
            item.extra[k] = v

    print("\n" + "=" * 66)
    print("第 3 步  真正送给模型的材料")
    print("=" * 66)
    material = build_content([item], cfg.max_chars)
    print(material)

    print("\n" + "=" * 66)
    print("自查要点")
    print("=" * 66)
    checks = [
        ("材料里写明正文未取到", "正文：未取到" in material),
        ("材料里写明实际拿到什么", "本条实际拿到" in material),
        ("材料里给出读不到的原因", "App 内渲染" in material),
        ("材料里不出现卡片没有的细节", "上海" not in material),
        ("给了用户可自己点开的地址", "可直接点开的地址" in material),
    ]
    bad = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        bad += 0 if ok else 1
    print(f"\n结论：{'全部通过' if not bad else f'{bad} 项不合格'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
