"""图片读取：把群里的图片变成可以直接喂给多模态模型的 data URL。

为什么必须「收到就转」
--------------------
qq_official 收到图片后会落到 AstrBot data 目录 `temp/media_image_*.jpg`，
而系统每天跑一次 `systemd-tmpfiles-clean`，**这些文件会被清掉**。
实测：线上数据里 image 组件指向的那个 jpg，几小时后再看已经不存在了。

所以路径不能只记不读——静默缓存的那一刻就要把字节读出来，
否则等群友问「刚才那张图」时，图早没了。

为什么要转 data URL 而不是把路径递给 provider
--------------------------------------------
provider（gemini_source）拿 image_urls 后自己会去解析，用的是它自己的
httpx client，会受 AstrBot 进程里 HTTP_PROXY 的影响；
这里由我们自己读字节，链路最短、也不依赖对方怎么实现。
"""

from __future__ import annotations

import base64
import os
import re
from urllib.parse import unquote, urlparse

# 单张图片上限。太大既撑内存，也可能顶到模型请求体上限。
MAX_IMAGE_BYTES = 3 * 1024 * 1024

_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

_BASE64_REF_RE = re.compile(r"^base64://(.+)$", re.S)
_DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,", re.I)


def _mime_of(path: str) -> str:
    return _MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "image/jpeg")


def is_local_ref(src: str) -> bool:
    """是不是一个「本机可读」的图片引用。"""
    if not src:
        return False
    if src.startswith(("base64://", "data:image/")):
        return True
    if src.startswith(("http://", "https://")):
        return False
    if src.startswith("file://"):
        return True
    # 插件跑在 Linux 上，图片路径形如 <data>/temp/media_image_*.jpg。
    # 这里显式认 POSIX 绝对路径，不依赖 os.path.isabs 的平台差异
    # （Windows 上 os.path.isabs("/srv/x") 会返回 False，而测试是在 Windows 上跑的）。
    return src.startswith("/") or os.path.isabs(src)


def _path_of(src: str) -> str:
    if src.startswith("file://"):
        parsed = urlparse(src)
        path = unquote(parsed.path)
        # 兼容 file://host/path 这种带 netloc 的写法
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            path = f"//{parsed.netloc}{path}"
        # Windows 的 file:///C:/x 解析出来是 /C:/x，把盘符前多出来的斜杠去掉
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        return path
    return src


def read_image_data_url(src: str, max_bytes: int = MAX_IMAGE_BYTES) -> str:
    """把图片引用读成 `data:image/...;base64,...`。

    支持 base64://、data URL、file:// 与本地绝对路径。
    读不到（不存在 / 过大 / 非本地）一律返回空串，由调用方决定怎么降级。
    """
    if not src:
        return ""

    if _DATA_URL_RE.match(src):
        return src

    m = _BASE64_REF_RE.match(src)
    if m:
        payload = m.group(1).strip()
        # 估算原始字节数，避免把超大图带进内存
        if len(payload) * 3 // 4 > max_bytes:
            return ""
        return f"data:image/jpeg;base64,{payload}"

    if src.startswith(("http://", "https://")):
        return ""  # 远程地址不在这里处理

    path = _path_of(src)
    if not path or not os.path.isfile(path):
        return ""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size <= 0 or size > max_bytes:
        return ""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    if not raw:
        return ""
    return f"data:{_mime_of(path)};base64,{base64.b64encode(raw).decode('ascii')}"


def pick_image_ref(*candidates: str) -> tuple[str, str]:
    """从若干候选字段里挑出图片引用。

    组件的 path / file / url 在不同平台、不同版本里落点不一样。
    这里把候选**全部扫完**再决定：本地可读的优先，远程地址作为兜底。
    不要扫到本地就提前 return —— 远程候选可能排在后面，会白白丢掉。

    Returns:
        (可直接读成本地文件的引用, 远程 URL)
    """
    local = ""
    remote = ""
    for cand in candidates:
        if not cand or not isinstance(cand, str):
            continue
        if cand.startswith(("http://", "https://")):
            if not remote:
                remote = cand
            continue
        if not local and is_local_ref(cand):
            local = cand
    return local, remote
