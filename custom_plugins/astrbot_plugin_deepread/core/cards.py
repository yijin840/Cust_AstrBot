"""QQ 分享卡片解析 —— 本插件的核心。

设计要点
--------
1. **两种卡片形态都要认，不能只认 Json。**

   a) **Json 卡片**（OneBot / aiocqhttp 等平台）
      结构是 `data["meta"][<card_type>]`，card_type 有 `news` / `detail_1` /
      `music` / `video` 等。**不硬编码类型**，遍历 meta 下所有子字典递归摊平。

   b) **拍平成纯文本的卡片**（QQ 官方机器人 qq_official，生产实测形态）
      QQ 官方 API 不下发 ark 卡片，客户端/服务端把它拍成一段 Plain 文本：

          [卡片消息] 图文H5
          摘要: [分享]潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布
          tag: 小黑盒
          title: 潮玩星球 ✖ 怪物猎人：荒野 官方授权主题店 详情公布
          desc: 下载小黑盒查看更多精彩内容
          jump_url: https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?...

      ⚠️ 这一条是踩坑踩出来的：v1.0 只认 Json，于是线上**永远解析不到卡片**，
      静默缓存恒为空，工具每次都回「没有可读的分享内容」。
      真实链上根本没有 Json 组件 —— 见 core/README 里的实测证据。

2. **字段分类而不是丢弃。**
   摊平后按用途分三类：跳转链接 / 图片 / 文本信息。
   链接按优先级挑（jump_url > jumpUrl > qqdocurl > url > …），
   且会剔除其实是图片的 URL（preview/icon/source_logo 常混在同一层）。

3. **卡片自带的元数据零成本。**
   这些字段是腾讯客户端替我们抓好的，随消息一起来，
   不需要任何网络请求 —— 这正是服务器机房 IP 环境下唯一稳定的信息源。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .models import IMAGE_KEYS, URL_KEYS


# 顶层字段也值得收，但不覆盖 meta 里的同名值
_TOP_KEYS = ("prompt", "desc", "title", "summary", "app", "view", "ver")

_IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|bmp|svg|heic)(?:\?|$)", re.I)

# 明显不是内容的字段，收进来只是噪声
_NOISE_KEYS = {
    "flag", "ver", "appid", "appidstr", "apptype", "ctime", "uin", "seq",
    "forward", "token", "style", "config", "extra", "ver", "verstr",
    "scene", "idx", "mid", "sn", "chksm", "biz", "itemid", "template_id",
    "templateid", "action", "actiontype", "share_url_type", "raw",
}

# 内部结构键，递归时要穿过去但本身不是内容
_WRAP_KEYS = {
    "meta", "config", "extra", "host", "detail", "detail_1", "news", "music",
    "video", "app", "appinfo", "sender", "source", "media", "thumb",
}


@dataclass
class CardInfo:
    """解析后的一张卡片。"""

    title: str = ""
    desc: str = ""
    url: str = ""
    preview: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    card_type: str = ""
    app: str = ""
    view: str = ""
    raw_keys: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.title or self.desc or self.url or self.fields)


def _flatten(node: Any, out: dict[str, str], depth: int = 0) -> None:
    """递归摊平任意嵌套结构，收集所有标量字符串字段。

    同名字段先到先得（深层覆盖浅层意义不大，且容易把噪声覆盖真实值）。
    """
    if depth > 5:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            k = str(key)
            if isinstance(value, dict):
                _flatten(value, out, depth + 1)
            elif isinstance(value, list):
                # 列表通常是对应关系，只处理其中的字典与字符串
                for item in value[:6]:
                    if isinstance(item, (dict, list)):
                        _flatten(item, out, depth + 1)
                    elif isinstance(item, str) and item.strip():
                        out.setdefault(k, item.strip())
            elif isinstance(value, str):
                v = value.strip()
                if v and k not in _NOISE_KEYS:
                    out.setdefault(k, v)
            elif isinstance(value, (int, float)) and k in (
                "time", "ctime", "pubtime", "createtime", "publish_time",
            ):
                out.setdefault(k, str(value))


def _looks_like_image(url: str) -> bool:
    return bool(_IMG_EXT_RE.search(url))


def _pick_url(fields: dict[str, str]) -> str:
    """从摊平后的字段里挑出最可能是「内容跳转链接」的那个。"""
    for key in URL_KEYS:
        value = fields.get(key, "")
        if value.startswith(("http://", "https://")) and not _looks_like_image(value):
            return value
    # 兜底：扫一遍所有 http 值，排除图片
    for key, value in fields.items():
        if key in IMAGE_KEYS:
            continue
        if value.startswith(("http://", "https://")) and not _looks_like_image(value):
            return value
    return ""


def _pick_preview(fields: dict[str, str]) -> str:
    for key in IMAGE_KEYS:
        value = fields.get(key, "")
        if value.startswith(("http://", "https://")):
            return value
    for value in fields.values():
        if value.startswith(("http://", "https://")) and _looks_like_image(value):
            return value
    return ""


def _title_from_prompt(prompt: str) -> str:
    """QQ 卡片的 prompt 形如「[分享] 标题」，剥掉前缀。"""
    if not prompt:
        return ""
    text = re.sub(r"^\s*[\[【][^\]】]{1,10}[\]】]\s*", "", prompt).strip()
    return text


def parse_card(data: dict | str) -> CardInfo | None:
    """解析 Json 消息段，抽出整张卡片的信息。

    Args:
        data: Json 组件的 data（dict 或 JSON 字符串）。

    Returns:
        CardInfo；无法解析成卡片时返回 None。
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None

    info = CardInfo(
        app=str(data.get("app") or ""),
        view=str(data.get("view") or ""),
    )

    fields: dict[str, str] = {}

    # 1) 主路径：遍历 meta 下所有子字典
    meta = data.get("meta")
    if isinstance(meta, dict):
        for key, sub in meta.items():
            if isinstance(sub, dict) and sub:
                if not info.card_type:
                    info.card_type = str(key)
                _flatten(sub, fields)
            elif isinstance(sub, str) and sub.strip():
                fields.setdefault(str(key), sub.strip())

    # 2) 兜底：顶层标量字段
    _flatten(
        {k: v for k, v in data.items() if k in _TOP_KEYS and k != "app"},
        fields,
    )

    info.raw_keys = sorted(fields.keys())

    if not fields and not info.app:
        return None

    info.title = (
        fields.get("title")
        or fields.get("t")
        or _title_from_prompt(fields.get("prompt", ""))
    )
    info.desc = (
        fields.get("desc")
        or fields.get("description")
        or fields.get("summary")
        or ""
    )
    info.url = _pick_url(fields)
    info.preview = _pick_preview(fields)

    # 收进 fields 的展示集合：剔除内部包装键与已单独提出的字段
    display: dict[str, str] = {}
    for key, value in fields.items():
        if key in _WRAP_KEYS or key in (
            "title", "t", "desc", "description", "summary", "prompt",
        ):
            continue
        if key in IMAGE_KEYS or value == info.url:
            continue
        display[key] = value
    info.fields = display

    if info.is_empty():
        return None
    return info


# --------------------------------------------------------------- 文本卡片
# QQ 官方机器人把 ark 卡片拍平成纯文本时，首行的标记
CARD_TEXT_MARKERS = ("[卡片消息]", "【卡片消息】")

# 形如 `key: value` / `key：value`。
# key 不许含 `/`，且排除 `https`（否则会把 `https://x` 误读成 key=https）。
_FIELD_LINE_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_\-]{0,23}|[\u4e00-\u9fff]{1,12})\s*[:：]\s*(\S.*)$"
)

# 拍平文本里的字段名 → 内部规范名
_CARD_TEXT_ALIAS = {
    "标题": "title",
    "名称": "title",
    "摘要": "summary",      # 优先级最高：比推广用的 desc 有价值得多
    "简介": "summary",
    "描述": "desc",
    "标签": "tag",
    "来源": "source",
    "作者": "author",
    "分享者": "author",
    "链接": "url",
    "跳转链接": "url",
}

# 已经从 fields 里单独拎走的键，不再在「卡片信息」里重复展示
_HOISTED_KEYS = {
    "title", "t", "summary", "desc", "description", "summary_text",
    "url", "jump_url", "jumpUrl", "qqdocurl",
}


def _parse_field_line(line: str) -> tuple[str, str] | None:
    """把一行 `key: value` 拆成 (key, value)；不是字段行则返回 None。"""
    m = _FIELD_LINE_RE.match(line.strip())
    if not m:
        return None
    key, value = m.group(1).strip(), m.group(2).strip()
    if not key or not value:
        return None
    # `https://…` 这种整行就是链接的，不是字段行
    if key.lower() in ("http", "https") or value.startswith("//"):
        return None
    return key, value


def parse_card_text(text: str) -> CardInfo | None:
    """解析「被拍平成纯文本的卡片」（QQ 官方机器人生产形态）。

    Args:
        text: 消息段的纯文本，形如 `[卡片消息] 图文H5\\n摘要: …\\njump_url: …`。

    Returns:
        CardInfo；不像卡片文本时返回 None。
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None

    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        return None

    card_type = ""
    head = lines[0]
    for marker in CARD_TEXT_MARKERS:
        if head.startswith(marker):
            card_type = head[len(marker):].strip()
            lines = lines[1:]
            break

    # 既没有卡片标记，又没有字段行 —— 判定为普通聊天文本，不当作卡片
    if not card_type and not any(_parse_field_line(ln) for ln in lines[:3]):
        return None

    fields: dict[str, str] = {}
    for line in lines:
        parsed = _parse_field_line(line)
        if parsed is None:
            continue
        key, value = parsed
        key = _CARD_TEXT_ALIAS.get(key, key)
        fields.setdefault(key, value)

    if not fields:
        return None

    info = CardInfo(card_type=card_type or "text_card")
    info.raw_keys = sorted(fields.keys())

    info.title = fields.get("title") or fields.get("t") or ""
    # 摘要优先：推广语 desc 常是「下载 App 看更多」这种噪声
    info.desc = (
        fields.get("summary")
        or fields.get("desc")
        or fields.get("description")
        or ""
    )
    info.url = _pick_url(fields)
    info.preview = _pick_preview(fields)

    display: dict[str, str] = {}
    for key, value in fields.items():
        if key in _WRAP_KEYS or key in _HOISTED_KEYS:
            continue
        if key in IMAGE_KEYS or value == info.url:
            continue
        display[key] = value
    info.fields = display

    if info.is_empty():
        return None
    return info


def is_video_card(info: CardInfo, video_hosts: set[str]) -> tuple[bool, str]:
    """判断一张卡片是否为视频卡片。返回 (是否视频, 命中的域名)。"""
    # 1) view 字段
    if info.view.lower() == "video":
        return True, "view=video"

    # 2) meta 里带 video 子键
    if "video" in info.raw_keys or info.card_type == "video":
        return True, "card_type=video"

    # 3) 文本卡片的子类型直接写了视频（如 `[卡片消息] 视频`）
    ct = (info.card_type or "").lower()
    if "video" in ct or "视频" in info.card_type:
        return True, f"card_type={info.card_type}"

    # 3) URL 命中视频站
    host = extract_host(info.url)
    if host:
        for bad in video_hosts:
            bad = bad.strip().lower()
            if not bad:
                continue
            if host == bad or host.endswith("." + bad):
                return True, bad
    return False, ""


def extract_host(url: str) -> str:
    """取 URL 的 host（小写、去掉端口）。"""
    if not url:
        return ""
    m = re.match(r"^https?://([^/?#]+)", url.strip(), re.I)
    if not m:
        return ""
    host = m.group(1).lower()
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    return host.split(":")[0]


def extract_urls(text: str) -> list[str]:
    """从纯文本里提取所有 http(s) 链接（去重、保序）。"""
    if not text:
        return []
    found = re.findall(r"https?://[^\s<>\"'）)】\]]+", text)
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        u = u.rstrip(".,;，。；、")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
