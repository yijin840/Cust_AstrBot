"""插件配置快照。

把 AstrBot 传进来的原始 dict 归一化成一个不可变的 dataclass，
避免在业务代码里到处 `cfg.get(...)` 并处理类型不一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_VIDEO_HOSTS = (
    "bilibili.com,b23.tv,acfun.cn,douyin.com,iesdouyin.com,kuaishou.com,"
    "youtube.com,youtu.be,tiktok.com,channels.weixin.qq.com,ixigua.com,"
    "huya.com,douyu.com"
)


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_int(value, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _as_str(value, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


@dataclass
class ReadConfig:
    """深读插件的运行时配置。"""

    cache_size: int = 5
    cache_ttl: int = 1800
    skip_video_cards: bool = True
    video_hosts: set[str] = field(default_factory=set)
    enable_l1_direct: bool = True
    enable_l2_api: bool = True
    enable_l3_local: bool = True
    max_chars: int = 12000
    fetch_timeout: int = 15
    max_items: int = 5
    summary_model: str = ""
    persona_style: bool = True
    reject_private_ip: bool = True
    # 图片阅读理解：用户把内容截图发来也读得懂
    enable_images: bool = True
    max_images: int = 3
    # 预读注入：在请求发给模型之前就把分享正文抓好塞进去，
    # 不指望模型在多工具竞争里选中 deepread_share（实测它会选 web_fetch）
    enable_prefetch: bool = True
    prefetch_window: int = 600

    @classmethod
    def from_raw(cls, raw: dict | None) -> "ReadConfig":
        raw = raw or {}
        hosts_raw = _as_str(raw.get("video_hosts"), DEFAULT_VIDEO_HOSTS)
        hosts = {
            h.strip().lower().lstrip("*.")
            for h in hosts_raw.replace("\n", ",").split(",")
            if h.strip()
        }
        return cls(
            cache_size=_as_int(raw.get("cache_size"), 5, 1, 20),
            cache_ttl=_as_int(raw.get("cache_ttl"), 1800, 60, 86400),
            skip_video_cards=_as_bool(raw.get("skip_video_cards"), True),
            video_hosts=hosts,
            enable_l1_direct=_as_bool(raw.get("enable_l1_direct"), True),
            enable_l2_api=_as_bool(raw.get("enable_l2_api"), True),
            enable_l3_local=_as_bool(raw.get("enable_l3_local"), True),
            max_chars=_as_int(raw.get("max_chars"), 12000, 500, 60000),
            fetch_timeout=_as_int(raw.get("fetch_timeout"), 15, 3, 60),
            max_items=_as_int(raw.get("max_items"), 5, 1, 20),
            summary_model=_as_str(raw.get("summary_model")),
            persona_style=_as_bool(raw.get("persona_style"), True),
            reject_private_ip=_as_bool(raw.get("reject_private_ip"), True),
            enable_images=_as_bool(raw.get("enable_images"), True),
            max_images=_as_int(raw.get("max_images"), 3, 1, 10),
            enable_prefetch=_as_bool(raw.get("enable_prefetch"), True),
            prefetch_window=_as_int(raw.get("prefetch_window"), 600, 0, 3600),
        )
