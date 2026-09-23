"""深读插件的数据模型。

一个「待读条目」（ReadItem）代表群里分享的一样东西：
一张卡片、一个链接、一个文件、一段合并转发，或一张图片。

三层信息在结构里是分开的，方便如实告知用户「哪些拿到了、哪些没拿到」：
  meta_fields  —— 卡片自带字段（L0，永远可用）
  body         —— 抓到的正文（L1/L2，可能为空）
  note         —— 采集过程中的说明与失败原因
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 条目类型
KIND_CARD = "card"        # QQ 分享卡片
KIND_LINK = "link"        # 裸链接
KIND_FILE = "file"        # 群内文件
KIND_FORWARD = "forward"  # 合并转发聊天记录
KIND_IMAGE = "image"      # 图片
KIND_TEXT = "text"        # 纯文本（通常作为附加上下文）
KIND_REPLY = "reply"      # 被回复消息里的分享

KIND_LABEL = {
    KIND_CARD: "分享卡片",
    KIND_LINK: "链接",
    KIND_FILE: "文件",
    KIND_FORWARD: "合并转发",
    KIND_IMAGE: "图片",
    KIND_TEXT: "文本",
    KIND_REPLY: "被回复的分享",
}

# 卡片字段的人话标签：把 meta 里千奇百怪的 key 映射成可读名
FIELD_LABELS: dict[str, str] = {
    "title": "标题",
    "t": "标题",
    "desc": "摘要",
    "description": "摘要",
    "summary": "摘要",
    "content": "内容",
    "tag": "标签",
    "tags": "标签",
    "source": "来源",
    "appname": "来源应用",
    "nick": "作者",
    "nickname": "作者",
    "name": "名称",
    "author": "作者",
    "pubtime": "发布时间",
    "time": "时间",
    "biz_name": "公众号",
    "music_name": "歌曲",
    "singer": "歌手",
    "album": "专辑",
}

# 跳转链接候选键，按优先级排列。
# 同时收 camelCase（Json 卡片）与 snake_case（QQ 官方机器人拍平的文本卡片）两种写法。
URL_KEYS = (
    "jumpUrl",
    "jump_url",
    "qqdocurl",
    "qq_doc_url",
    "url",
    "musicUrl",
    "music_url",
    "shareUrl",
    "share_url",
    "sourceUrl",
    "source_url",
    "appUrl",
    "app_url",
    "targetUrl",
    "target_url",
)

# 图片类键（不当正文用，单独记录）
IMAGE_KEYS = (
    "preview",
    "icon",
    "imageUrl",
    "cover",
    "thumb",
    "thumbnail",
    "picurl",
    "source_logo",
    "sourceLogo",
    "logo",
)


@dataclass
class ReadItem:
    """一个待读条目。"""

    kind: str = KIND_CARD
    title: str = ""
    desc: str = ""
    url: str = ""
    body: str = ""
    meta_fields: dict[str, str] = field(default_factory=dict)
    preview: str = ""
    sender: str = ""
    layer: str = ""
    ok: bool = False
    is_video: bool = False
    video_host: str = ""
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def header(self) -> str:
        """一行标识，用于日志和「读到了什么」的回执。"""
        label = KIND_LABEL.get(self.kind, self.kind)
        name = self.title or self.url or self.extra.get("filename") or "（无标题）"
        return f"[{label}] {name}"

    def got_line(self) -> str:
        """一行说明「这一条实际拿到了什么」。

        这是给模型划边界用的：材料里显式写出「只拿到标题」，
        模型才不容易拿常识去补细节。
        """
        if self.kind == KIND_IMAGE:
            if self.extra.get("image_data_url"):
                return "图片（已读取，可直接识别）"
            if self.extra.get("image_ref"):
                return "图片链接（未读到本地数据）"
            return "什么都没拿到"
        bits: list[str] = []
        if self.title:
            bits.append("标题")
        if self.desc:
            bits.append("摘要")
        if self.meta_fields:
            bits.append(f"卡片字段 {len(self.meta_fields)} 项")
        if self.body:
            bits.append(f"正文 {len(self.body)} 字")
        attached = self.extra.get("image_data_urls") or []
        if attached:
            bits.append(f"配图 {len(attached)} 张已附")
        return "、".join(bits) if bits else "什么都没拿到"

    def render(self, max_chars: int = 12000) -> str:
        """渲染成送给模型的一段文本。"""
        buf: list[str] = []
        label = KIND_LABEL.get(self.kind, self.kind)

        # 图片单独走一条渲染路径：图片数据是以 image_urls 参数另附的，
        # 文本材料里只做说明，绝不能把 data URL 塞进来（几百 KB，会撑爆 prompt）。
        if self.kind == KIND_IMAGE:
            buf.append(f"### 分享内容 {label}")
            if self.sender:
                buf.append(f"分享者：{self.sender}")
            if self.extra.get("image_data_url"):
                buf.append("（图片已作为附件单独提供，请直接读图中的文字与信息）")
            elif self.extra.get("image_ref"):
                buf.append(f"（图片地址：{self.extra['image_ref']}）")
            if self.note:
                buf.append(f"（采集说明：{self.note}）")
            buf.append("注意：图片内容只能来自你实际看到的那张图，看不清就说看不清。")
            return "\n".join(buf)

        buf.append(f"### 分享内容 {label}")
        if self.sender:
            buf.append(f"分享者：{self.sender}")
        if self.title:
            buf.append(f"标题：{self.title}")
        # 摘要是拿不到正文时最有价值的信息，必须单独输出
        if self.desc:
            buf.append(f"摘要：{self.desc}")
        if self.url and not self.url.startswith("data:"):
            buf.append(f"链接：{self.url}")

        # 分享链接常常是接口地址，这里给出人能直接点开的那个
        real = str(self.extra.get("real_url") or "")
        if real and real != self.url:
            buf.append(f"可直接点开的地址：{real}")

        # 卡片自带字段（L0）
        if self.meta_fields:
            buf.append("卡片信息：")
            for key, value in self.meta_fields.items():
                if not value:
                    continue
                shown = FIELD_LABELS.get(key, key)
                # 卡片元数据本身很短，不做截断
                buf.append(f"  - {shown}：{value}")

        # 正文（L1/L2/L3）
        if self.body:
            body = self.body
            if len(body) > max_chars:
                body = body[:max_chars] + f"\n……（正文过长，已截断，原长 {len(self.body)} 字）"
            buf.append("正文：")
            buf.append(body)
        else:
            buf.append("正文：未取到")

        if self.note:
            buf.append(f"（采集说明：{self.note}）")

        # 收尾这句是抑制编造的关键：把「手上有什么」摆在模型眼前
        buf.append(f"【本条实际拿到：{self.got_line()}。没有列出的信息就是没拿到，不要自行补全】")

        return "\n".join(buf)

    def has_content(self) -> bool:
        """是否拿到了任何有实质意义的内容。"""
        if self.kind == KIND_IMAGE:
            # 图片只要读到了数据或链接就算有内容——文字由多模态模型出
            return bool(
                self.extra.get("image_data_url") or self.extra.get("image_ref")
            )
        return bool(self.title or self.desc or self.meta_fields or self.body)

    def summary_line(self) -> str:
        """给用户看的一行回执。"""
        layer = f"（{self.layer}）" if self.layer else ""
        if self.kind == KIND_IMAGE:
            if self.extra.get("image_data_url"):
                state = "图片已读取"
            elif self.extra.get("image_ref"):
                state = "只有图片链接"
            else:
                state = "未取到图片"
            return f"{self.header()} → {state}{layer}"
        bits: list[str] = []
        if self.title:
            bits.append("标题")
        if self.desc:
            bits.append("摘要")
        if self.meta_fields:
            bits.append(f"卡片字段 {len(self.meta_fields)} 项")
        if self.body:
            bits.append(f"正文 {len(self.body)} 字")
        state = "、".join(bits) if bits else "未取到内容"
        layer = f"（{self.layer}）" if self.layer else ""
        return f"{self.header()} → {state}{layer}"
