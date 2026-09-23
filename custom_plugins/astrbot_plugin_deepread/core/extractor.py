"""从消息链里抽取「待读条目」。

负责把 AstrBot 的各种消息段翻译成统一的 ReadItem：
    Json      → 分享卡片（OneBot 等平台）
    Plain     → 裸链接，**或 QQ 官方机器人拍平成文本的卡片**
    File      → 群内文件（读文本内容）
    Forward   → 合并转发（展开每个节点）
    Nodes     → 同上
    Image     → 图片（记录，交多模态）
    Reply     → 被回复的消息（卡片文本常挂在这里）

⚠️ 实测结论（2026-09-11 线上日志 + data_v4.db）：qq_official 平台上
**卡片不是 Json，而是 Plain**，内容形如
`[卡片消息] 图文H5\\n摘要: …\\ntitle: …\\njump_url: …`。
所以 Plain 分支必须优先尝试 `parse_card_text`，否则分享永远读不到。

组件导入做了双路径兼容：优先 astrbot.api，失败回落 astrbot.core。
"""

from __future__ import annotations

import os
from typing import Any

from .cards import (
    extract_host,
    extract_urls,
    is_video_card,
    parse_card,
    parse_card_text,
)
from .config import ReadConfig
from .images import pick_image_ref, read_image_data_url
from .models import (
    KIND_CARD,
    KIND_FILE,
    KIND_FORWARD,
    KIND_IMAGE,
    KIND_LINK,
    ReadItem,
)

# ---------------------------------------------------------------- 组件导入
try:  # AstrBot 对外 API 层
    from astrbot.api.message_components import (  # type: ignore
        File as CompFile,
        Forward,
        Image as CompImage,
        Json,
        Node,
        Nodes,
        Plain,
        Reply,
    )
    _COMPONENTS_OK = True
except Exception:  # pragma: no cover
    try:  # 回落核心层（parser 插件即用此路径）
        from astrbot.core.message.components import (  # type: ignore
            File as CompFile,
            Forward,
            Image as CompImage,
            Json,
            Node,
            Nodes,
            Plain,
            Reply,
        )
        _COMPONENTS_OK = True
    except Exception:
        class _Never:
            """组件不可用时的哨兵类型，isinstance 永远为 False。"""

        Json = Plain = Reply = Forward = Nodes = Node = CompFile = CompImage = _Never  # type: ignore
        _COMPONENTS_OK = False

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".csv", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".c", ".cpp", ".h",
    ".rs", ".sh", ".bat", ".ps1", ".sql", ".html", ".htm", ".css", ".xml",
    ".ini", ".toml", ".conf", ".cfg", ".env", ".srt", ".vtt",
}
MAX_FILE_BYTES = 2 * 1024 * 1024      # 单文件读取上限 2MB
MAX_FILE_CHARS = 20000
MAX_FORWARD_ITEMS = 40


def _ext(filename: str) -> str:
    return os.path.splitext((filename or "").lower())[1]


# ------------------------------------------------------------------ 抽卡片
def _item_from_info(info, cfg: ReadConfig, sender: str, kind: str = KIND_CARD) -> ReadItem:
    """把 CardInfo 组装成 ReadItem（Json 卡片与文本卡片共用）。"""
    item = ReadItem(
        kind=kind,
        title=info.title,
        desc=info.desc,
        url=info.url,
        preview=info.preview,
        meta_fields=dict(info.fields),
        sender=sender,
        layer="L0",
    )
    if info.card_type:
        item.extra["card_type"] = info.card_type
    if info.view:
        item.extra["view"] = info.view

    if cfg.skip_video_cards:
        hit, host = is_video_card(info, cfg.video_hosts)
        if hit:
            item.is_video = True
            item.video_host = host
            item.note = f"视频卡片（命中 {host}），已跳过"

    item.ok = item.has_content()
    if not item.title and item.url:
        item.title = item.url
    return item


def item_from_card(data: Any, cfg: ReadConfig, sender: str = "") -> ReadItem | None:
    """把 Json 消息段转成 ReadItem。"""
    info = parse_card(data)
    if info is None:
        return None
    return _item_from_info(info, cfg, sender)


def card_item_from_text(
    text: str, cfg: ReadConfig, sender: str = ""
) -> ReadItem | None:
    """把「拍平成纯文本的卡片」转成 ReadItem。

    qq_official 平台上卡片就是这个形态，P0 路径。
    """
    info = parse_card_text(text)
    if info is None:
        return None
    item = _item_from_info(info, cfg, sender)
    item.note = item.note or "卡片文本（QQ 官方机器人拍平）"
    return item


def item_from_url(url: str, cfg: ReadConfig, sender: str = "",
                  kind: str = KIND_LINK) -> ReadItem:
    """把一个裸链接转成 ReadItem。"""
    item = ReadItem(kind=kind, url=url, sender=sender)
    host = extract_host(url)
    if cfg.skip_video_cards and host:
        for bad in cfg.video_hosts:
            if host == bad or host.endswith("." + bad):
                item.is_video = True
                item.video_host = bad
                item.note = f"视频链接（命中 {bad}），已跳过"
                return item
    item.ok = True
    return item


# ------------------------------------------------------------------ 抽文件
async def item_from_file(seg: Any, cfg: ReadConfig, sender: str = "") -> ReadItem:
    """读群内文件。文本类直接取出，其它类型如实说明。"""
    name = getattr(seg, "name", "") or "未知文件"
    item = ReadItem(kind=KIND_FILE, title=name, sender=sender, extra={"filename": name})

    if not cfg.enable_l3_local:
        item.note = "本地内容解析已关闭"
        return item

    path = ""
    try:
        if hasattr(seg, "get_file"):
            path = await seg.get_file()           # 异步下载或取本地路径
        elif hasattr(seg, "file"):
            path = seg.file
    except Exception as e:
        item.note = f"取文件失败：{type(e).__name__}"
        return item

    if not path or not os.path.exists(path):
        item.note = "文件未取得本地路径"
        return item

    ext = _ext(name) or _ext(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0

    if ext == ".pdf":
        text = _read_pdf(path)
        if text:
            item.body = text[:MAX_FILE_CHARS]
            item.ok = True
            item.layer = "L3"
            item.note = "PDF 文本已提取"
        else:
            item.note = "PDF 解析不可用（缺少 pypdf），仅记录文件名"
        return item

    if ext in TEXT_EXTS or size <= MAX_FILE_BYTES:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_FILE_CHARS * 2)
            if text.strip():
                item.body = text[:MAX_FILE_CHARS]
                item.ok = True
                item.layer = "L3"
                item.note = f"已读取文件内容（{size} 字节）"
            else:
                item.note = "文件内容为空"
        except Exception as e:
            item.note = f"读取失败：{type(e).__name__}"
    else:
        item.note = f"暂不支持的二进制格式（{size} 字节）"
    return item


def _read_pdf(path: str) -> str:
    """尝试抽取 PDF 文本；无可用库时返回空串。"""
    for mod in ("pypdf", "PyPDF2"):
        try:
            module = __import__(mod)
            reader = module.PdfReader(path)
            parts: list[str] = []
            for page in reader.pages[:40]:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    continue
            text = "\n".join(parts).strip()
            if text:
                return text
        except Exception:
            continue
    return ""


# ------------------------------------------------------------- 抽合并转发
def item_from_forward(seg: Any, cfg: ReadConfig, sender: str = "") -> ReadItem:
    """把合并转发展开成可读的聊天记录文本。"""
    item = ReadItem(kind=KIND_FORWARD, title="合并转发聊天记录", sender=sender)

    if not cfg.enable_l3_local:
        item.note = "本地内容解析已关闭"
        return item

    nodes = getattr(seg, "nodes", None)
    if not nodes and isinstance(seg, Node):
        nodes = [seg]
    if not nodes:
        item.note = "转发内容为空（可能只给了一个无法展开的 id）"
        return item

    lines: list[str] = []
    for node in list(nodes)[:MAX_FORWARD_ITEMS]:
        who = getattr(node, "name", "") or "某人"
        content = getattr(node, "content", None) or []
        pieces: list[str] = []
        for comp in content:
            cls = type(comp).__name__
            if cls == "Plain":
                raw = getattr(comp, "text", "") or ""
                text_card = parse_card_text(raw)
                if text_card:
                    pieces.append(
                        "[分享卡片] "
                        + " / ".join(
                            x for x in (text_card.title, text_card.desc, text_card.url) if x
                        )
                    )
                else:
                    pieces.append(raw)
            elif cls == "Json":
                info = parse_card(getattr(comp, "data", None))
                if info:
                    pieces.append(
                        "[分享卡片] "
                        + " / ".join(x for x in (info.title, info.desc, info.url) if x)
                    )
            elif cls == "Image":
                pieces.append("[图片]")
            elif cls == "File":
                pieces.append(f"[文件 {getattr(comp, 'name', '')}]")
            elif cls in ("Forward", "Nodes"):
                pieces.append("[嵌套转发]")
        text = " ".join(p for p in pieces if p).strip()
        if text:
            lines.append(f"{who}：{text}")

    if lines:
        item.body = "\n".join(lines)
        item.ok = True
        item.layer = "L3"
        item.note = f"展开 {len(lines)} 条记录"
    else:
        item.note = "转发里的内容未能展开"
    return item


# ------------------------------------------------------------------ 主入口
async def collect_from_chain(
    chain: list[Any],
    cfg: ReadConfig,
    sender: str = "",
    depth: int = 0,
) -> list[ReadItem]:
    """遍历一段消息链，抽出所有待读条目。"""
    items: list[ReadItem] = []
    if not chain or depth > 2:
        return items

    plain_buf: list[str] = []

    for seg in chain:
        cls = type(seg).__name__

        if cls == "Json" or (Json and isinstance(seg, Json)):
            one = item_from_card(getattr(seg, "data", None), cfg, sender)
            if one:
                items.append(one)

        elif cls in ("Nodes", "Node", "Forward") or (
            Forward and isinstance(seg, (Forward, Node, Nodes))
        ):
            items.append(item_from_forward(seg, cfg, sender))

        elif cls == "File" or (CompFile and isinstance(seg, CompFile)):
            items.append(await item_from_file(seg, cfg, sender))

        elif cls == "Image" or (CompImage and isinstance(seg, CompImage)):
            # 组件的 path / file / url 在不同平台落点不一样，三个都试
            raw = [
                str(getattr(seg, "path", "") or ""),
                str(getattr(seg, "file", "") or ""),
                str(getattr(seg, "url", "") or ""),
            ]
            local, remote = pick_image_ref(*raw)
            item = ReadItem(kind=KIND_IMAGE, url=local or remote, sender=sender)

            data_url = read_image_data_url(local) if local else ""
            if data_url:
                # **收到就转**：qq_official 把图落到 data/temp，
                # 而系统每天清一次 temp，只记路径的话等群友来问时图就没了。
                item.extra["image_data_url"] = data_url
                item.layer = "L3"
                item.ok = True
                item.note = "图片已读取，内容交给多模态识别"
            elif remote:
                item.extra["image_ref"] = remote
                item.ok = True
                item.note = "只拿到图片链接，尝试交由多模态读取"
            else:
                item.note = "图片没能读到本地文件（临时文件可能已被系统清理，请重发一次）"
            items.append(item)

        elif cls == "Plain":
            text = getattr(seg, "text", "") or ""
            # QQ 官方机器人的卡片就是一段 Plain 文本，先按卡片解析
            card = card_item_from_text(text, cfg, sender)
            if card:
                items.append(card)
            else:
                plain_buf.append(text)

        elif cls == "Reply":
            # 被回复的消息也要读。
            # 引用分享卡片时，卡片文本可能落在 chain / message_str / text 任一处。
            before = len(items)
            sub_chain = getattr(seg, "chain", None) or []
            if sub_chain:
                sub = await collect_from_chain(sub_chain, cfg, sender, depth + 1)
                items.extend(sub)

            quoted = ""
            for attr in ("message_str", "text"):
                value = getattr(seg, attr, "") or ""
                if value.strip():
                    quoted = value
                    break

            # 子链已经解析出内容时，被引用文本往往是同一份内容的另一种形态。
            # 实测：qq_official 的引用卡片同时带 chain 和 message_str，
            # 两边都解析会导致**同一张卡片被缓存两次**（日志「已缓存 2 条」）。
            if quoted and len(items) == before:
                card = card_item_from_text(quoted, cfg, sender)
                if card:
                    items.append(card)
                else:
                    plain_buf.append(quoted)

    # 纯文本兜底：从文本里找链接
    text = "\n".join(plain_buf).strip()
    if text:
        for url in extract_urls(text)[: cfg.max_items]:
            items.append(item_from_url(url, cfg, sender))
    return dedupe(items)


def dedupe(items: list[ReadItem]) -> list[ReadItem]:
    """去掉重复条目。

    同一条消息里，一张卡片可能以多种形态出现（Json 组件 / 引用链 / 引用文本），
    任意两条路径命中同一份内容就会产生重复。这里按「类型 + 链接 + 标题 + 卡片字段」
    做去重，保序。
    """
    seen: set[tuple] = set()
    out: list[ReadItem] = []
    for item in items:
        key = (
            item.kind,
            item.url or "",
            item.title or "",
            tuple(sorted(item.meta_fields.items())),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


async def collect_from_event(event: Any, cfg: ReadConfig) -> list[ReadItem]:
    """从 AstrMessageEvent 抽取条目。"""
    try:
        chain = event.get_messages()
    except Exception:
        chain = []

    try:
        sender = event.get_sender_name() or ""
    except Exception:
        sender = ""

    items = await collect_from_chain(list(chain or []), cfg, sender)

    # 没有拿到任何条目时，退而用纯文本再试一次
    if not any(i.ok for i in items):
        try:
            text = event.message_str or ""
        except Exception:
            text = ""
        if text:
            for u in extract_urls(text)[: cfg.max_items]:
                items.append(item_from_url(u, cfg, sender))

    return dedupe(items)
