"""内容抓取：L1 直连正文 + L2 官方 API。

为什么长这样
------------
实测（2026-09-11）结论：目标服务器是阿里云机房 IP，**风控拦的是 IP 不是客户端**。
httpx / curl_cffi（chrome 指纹）/ Playwright（真实 Chrome）三套客户端，
在公众号、知乎、B站网页、掘金、百度百科上**全部失败**。
所以抓正文这件事注定只能覆盖一部分站点，本模块的定位是「锦上添花」：
拿到了就补全正文，拿不到就退回卡片元数据，且如实说明。

覆盖情况（实测）：
  ✅ GitHub（API）、B站（API）、小黑盒（API·实时签名）、CSDN、V2EX、少数派、36氪、InfoQ
  ❌ 公众号、知乎、B站网页、掘金、百度百科（IP 被风控）

所有站点规则集中在本文件的 SITE_RULES，站点改版只需改这一处。
"""

from __future__ import annotations

import asyncio
import base64
import html as _html_mod
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

try:  # bs4 缺失时降级到内置解析器，功能不中断
    from bs4 import BeautifulSoup
    _BS4_BACKEND = "lxml"
    try:
        import lxml  # noqa: F401
    except Exception:
        _BS4_BACKEND = "html.parser"
except Exception:  # pragma: no cover
    BeautifulSoup = None
    _BS4_BACKEND = ""

try:
    from markdownify import markdownify as _md
except Exception:  # pragma: no cover
    _md = None

from .cards import extract_host
from .xiaoheihe import (
    API_PATH as XHH_API_PATH,
    API_URL as XHH_API_URL,
    BASE_PARAMS as XHH_BASE_PARAMS,
    REQUEST_HEADERS as XHH_HEADERS,
    build_sign_params,
    link_id_of,
    parse_link_tree,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 正文容器候选（通用兜底，按顺序尝试）
GENERIC_SELECTORS = (
    "article",
    "main",
    "[role=main]",
    "#js_content",                 # 公众号
    "#content_views",              # CSDN
    ".RichContent-inner",          # 知乎回答
    ".Post-RichText",              # 知乎专栏
    ".article-content",
    ".post-content",
    ".entry-content",
    ".markdown-body",              # GitHub / 各类文档站
    "#content", ".content",
)

# 进正文前要剔除的噪声节点
_STRIP = (
    "script", "style", "noscript", "iframe", "svg", "form", "nav", "header",
    "footer", "aside", "button", "textarea", "select",
    ".comment", ".comments", "#comments", ".sidebar", ".advert", ".ad",
    ".recommend", ".related", ".share", ".toolbar", ".breadcrumb",
)


@dataclass
class FetchResult:
    """一次抓取的结果。"""

    ok: bool = False
    title: str = ""
    body: str = ""
    layer: str = ""
    note: str = ""
    extra: dict | None = None


# --------------------------------------------------------------------------- #
# URL 安全
# --------------------------------------------------------------------------- #

def is_safe_url(url: str, reject_private: bool = True) -> tuple[bool, str]:
    """校验 URL 是否可抓。防 SSRF。"""
    if not url:
        return False, "空链接"
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False, "链接格式错误"
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议 {parsed.scheme}"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "缺少主机名"
    if not reject_private:
        return True, ""

    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False, "内网地址已拒绝"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""  # 域名，交给后续请求
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    ):
        return False, "内网地址已拒绝"
    return True, ""


# --------------------------------------------------------------------------- #
# 站点规则（改版只改这里）
# --------------------------------------------------------------------------- #

@dataclass
class SiteRule:
    name: str
    host_pattern: str
    selectors: tuple[str, ...] = ()
    api: str = ""          # "bilibili" / "github" / "" 走通用正文提取
    referer: str = ""


SITE_RULES: tuple[SiteRule, ...] = (
    SiteRule(
        name="github",
        host_pattern=r"(^|\.)github\.com$",
        selectors=("article.markdown-body", ".markdown-body"),
        api="github",
        referer="https://github.com/",
    ),
    SiteRule(
        name="bilibili",
        host_pattern=r"(^|\.)(bilibili\.com|b23\.tv)$",
        api="bilibili",
        referer="https://www.bilibili.com/",
    ),
    SiteRule(
        name="csdn",
        host_pattern=r"(^|\.)csdn\.net$",
        selectors=("#content_views",),
        referer="https://blog.csdn.net/",
    ),
    SiteRule(
        name="zhihu",
        host_pattern=r"(^|\.)(zhihu\.com|zhuanlan\.zhihu\.com)$",
        selectors=(".RichContent-inner", ".Post-RichText", ".QuestionRichText"),
        referer="https://www.zhihu.com/",
    ),
    SiteRule(
        name="wechat",
        host_pattern=r"(^|\.)mp\.weixin\.qq\.com$",
        selectors=("#js_content",),
    ),
    SiteRule(name="juejin", host_pattern=r"(^|\.)juejin\.cn$",
             selectors=(".article-content", ".markdown-body")),
    SiteRule(name="v2ex", host_pattern=r"(^|\.)v2ex\.com$",
             selectors=(".topic_content", ".markdown_body")),
    SiteRule(name="sspai", host_pattern=r"(^|\.)sspai\.com$",
             selectors=(".article-body", ".content")),
    SiteRule(name="36kr", host_pattern=r"(^|\.)36kr\.com$"),
    SiteRule(name="infoq", host_pattern=r"(^|\.)infoq\.cn$"),
    SiteRule(name="xiaoheihe", host_pattern=r"(^|\.)(xiaoheihe\.cn|heybox\.com)$",
             api="xiaoheihe", referer="https://www.xiaoheihe.cn/"),
    SiteRule(name="weibo", host_pattern=r"(^|\.)(weibo\.com|weibo\.cn)$",
             selectors=(".article", ".WB_text")),
    SiteRule(name="jianshu", host_pattern=r"(^|\.)jianshu\.com$",
             selectors=("article", ".show-content")),
    SiteRule(name="cnblogs", host_pattern=r"(^|\.)cnblogs\.com$",
             selectors=("#cnblogs_post_body",)),
    SiteRule(name="segmentfault", host_pattern=r"(^|\.)segmentfault\.com$",
             selectors=(".article", ".fmt")),
)


def match_rule(url: str) -> SiteRule | None:
    host = extract_host(url)
    if not host:
        return None
    for rule in SITE_RULES:
        if re.search(rule.host_pattern, host):
            return rule
    return None


# --------------------------------------------------------------------------- #
# 已知「设计上就不给外部程序读」的站点
# --------------------------------------------------------------------------- #
# 这些站点不是「抓取失败」，而是走不通——直接给出准确解释，
# 比跑一次请求再报一个含糊错误有用得多，也省掉一次无谓等待。
#
# ⚠️ 2026-09-11 更正：**小黑盒已从这里移除**。
# 之前把它归类为「读不到」是**误判**：当时只试了 x_client_type=web / heybox_app，
# 而分享页自己用的是 x_client_type=weboutapp。换成正确的客户端标识 + 现算签名
# （见 core/xiaoheihe.py）后，link/tree 接口能稳定返回完整帖子正文。
# 教训：把「我没试通」写成「对方设计上不给读」之前，先把对方的真实请求抓下来看一眼。
#
# 目前为空。保留这个机制是因为它确实省事：命中时不用发请求就能给出准确原因。
UNREADABLE_SITES: tuple[tuple[str, str, str], ...] = ()


def match_unreadable(url: str) -> tuple[str, str] | None:
    """命中已知读不到的站点，返回 (站点名, 原因)。"""
    host = extract_host(url)
    if not host:
        return None
    for pattern, name, reason in UNREADABLE_SITES:
        if re.search(pattern, host):
            return name, reason
    return None


# 分享类链接的「跳转目标」推导：不请求也能算出人能点开的真实地址。
_SHARE_UNWRAP: tuple[tuple[str, str], ...] = (
    # api.xiaoheihe.cn/v3/bbs/app/api/web/share?...&link_id=xxx
    #   → https://www.xiaoheihe.cn/app/bbs/link/<link_id>
    (
        r"link_id=([0-9a-zA-Z]+)",
        "https://www.xiaoheihe.cn/app/bbs/link/{0}",
    ),
)


def unwrap_share_url(url: str) -> str:
    """把 API 形式的分享链接换算成用户能直接点开的网页地址。

    小黑盒卡片给的 jump_url 是 api 域名的接口地址，点开体验差；
    这里纯字符串换算，不发请求。
    """
    if not url:
        return ""
    if "xiaoheihe" not in url and "heybox" not in url:
        return url
    for pattern, template in _SHARE_UNWRAP:
        m = re.search(pattern, url)
        if m:
            return template.format(m.group(1))
    return url


# --------------------------------------------------------------------------- #
# HTML → 正文
# --------------------------------------------------------------------------- #

def _clean_text(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _unescape(text: str) -> str:
    """把 HTML 实体还原成字符（&amp; &#39; 之类）。"""
    if not text:
        return ""
    try:
        return _clean_text(_html_mod.unescape(text))
    except Exception:
        return _clean_text(text)


def html_to_text(html: str) -> str:
    """HTML 转纯文本（带简单降级）。"""
    if not html:
        return ""
    if _md is not None:
        try:
            md = _md(html, heading_style="ATX", strip=["img"])
            return _clean_text(md)
        except Exception:
            pass
    if BeautifulSoup is None:
        return _clean_text(re.sub(r"<[^>]+>", "", html))
    return _clean_text(BeautifulSoup(html, _BS4_BACKEND).get_text("\n", strip=True))


_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_ATTR_RE = re.compile(
    r"""([a-zA-Z][a-zA-Z0-9:_-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)

# 典型的前端挂载点（SPA 空壳的标志）
_SPA_MARKERS = (
    '<div id="app">', "<div id='app'>",
    '<div id="root">', "<div id='root'>",
    'id="app"></div>', 'id="root"></div>',
    'type="module"', "type=module",
    "__NUXT__", "__NEXT_DATA__", "vite-legacy",
)


def _meta_attrs(tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _META_ATTR_RE.finditer(tag):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        if key not in out:
            out[key] = val
    return out


def extract_meta(html: str) -> tuple[str, str, str]:
    """从 HTML 里抽 (标题, 描述, 图片) —— OG / description / <title>。

    这是**正文抓不到时的兜底**：很多站点的正文在 JS 里，
    但给分享用的 OG 元数据是服务端直出的，往往含正文开头一段。
    """
    if not html:
        return "", "", ""
    title = desc = image = ""
    for tag in _META_TAG_RE.findall(html[:200_000]):
        attrs = _meta_attrs(tag)
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = (attrs.get("content") or "").strip()
        if not content:
            continue
        if key in ("og:title", "twitter:title") and not title:
            title = content
        elif key in ("og:description", "twitter:description", "description") and not desc:
            desc = content
        elif key in ("og:image", "twitter:image") and not image:
            image = content
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = _clean_text(m.group(1))
    return _unescape(title), _unescape(desc), _unescape(image)


def looks_like_spa_shell(html: str, text: str) -> bool:
    """拿到的 HTML 是不是一个「空壳」（正文全靠 JS 渲染）。

    判据：抽出来的文本很短，而 HTML 头部有典型的前端挂载点 / 模块脚本。
    """
    if len(text) >= 200:
        return False
    head = html[:8000]
    return any(marker in head for marker in _SPA_MARKERS)


def extract_article(html: str, selectors: tuple[str, ...] = ()) -> tuple[str, str]:
    """从 HTML 里抽正文。返回 (标题, 正文)。

    策略：站点选择器 → 通用选择器 → 文本密度兜底。
    """
    if not html:
        return "", ""
    if BeautifulSoup is None:
        return "", _clean_text(re.sub(r"<[^>]+>", " ", html))

    soup = BeautifulSoup(html, _BS4_BACKEND)

    title = ""
    if soup.title:
        title = soup.title.get_text(strip=True)
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)

    try:
        for sel in _STRIP:
            for node in soup.select(sel):
                node.decompose()
    except Exception:
        pass

    def _body_of(node) -> str:
        try:
            return html_to_text(str(node))
        except Exception:
            return ""

    # 1) 站点指定选择器
    for sel in selectors:
        try:
            node = soup.select_one(sel)
        except Exception:
            continue
        if node:
            body = _body_of(node)
            if len(body) >= 150:
                return title, body

    # 2) 通用选择器
    for sel in GENERIC_SELECTORS:
        try:
            node = soup.select_one(sel)
        except Exception:
            continue
        if node:
            body = _body_of(node)
            if len(body) >= 200:
                return title, body

    # 3) 密度兜底：整棵树上文本最长的容器
    best, best_len = None, 0
    try:
        for node in soup.find_all(["div", "section", "article"]):
            text = node.get_text(strip=True)
            if len(text) > best_len:
                best, best_len = node, len(text)
    except Exception:
        pass
    if best is not None and best_len >= 200:
        return title, _body_of(best)

    fallback = soup.body or soup
    return title, _body_of(fallback)


# --------------------------------------------------------------------------- #
# L2：官方 API（免登录，实测可用）
# --------------------------------------------------------------------------- #

async def fetch_bilibili(client: httpx.AsyncClient, url: str) -> FetchResult:
    """B站官方 view API：拿标题/UP主/时长/简介/字幕。无需登录。"""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    params: dict[str, str] = {}
    if m:
        params["bvid"] = m.group(1)
    else:
        m2 = re.search(r"av(\d+)", url, re.I)
        if not m2:
            return FetchResult(note="未识别到 BV/av 号", layer="L2")
        params["aid"] = m2.group(1)

    try:
        r = await client.get(
            "https://api.bilibili.com/x/web-interface/view",
            params=params,
            headers={"Referer": "https://www.bilibili.com/"},
        )
        data = (r.json() or {}).get("data") or {}
    except Exception as e:
        return FetchResult(note=f"B站 API 失败：{type(e).__name__}", layer="L2")

    if not data:
        return FetchResult(note="B站 API 未返回数据", layer="L2")

    title = data.get("title") or ""
    owner = (data.get("owner") or {}).get("name") or ""
    duration = data.get("duration") or 0
    desc = (data.get("desc") or "").strip()
    stat = data.get("stat") or {}
    pages = data.get("pages") or []
    cid = pages[0].get("cid") if pages else None

    lines = ["【B站视频信息】"]
    if owner:
        lines.append(f"UP主：{owner}")
    if duration:
        lines.append(f"时长：{duration // 60}分{duration % 60}秒")
    if stat:
        lines.append(
            f"数据：播放 {stat.get('view', 0)}、点赞 {stat.get('like', 0)}、"
            f"弹幕 {stat.get('danmaku', 0)}"
        )
    if desc:
        lines.append(f"简介：{desc}")

    # 尝试字幕（有则非常有价值）
    subtitle_text = ""
    if params.get("bvid") and cid:
        try:
            r2 = await client.get(
                "https://api.bilibili.com/x/player/v2",
                params={"bvid": params["bvid"], "cid": str(cid)},
                headers={"Referer": "https://www.bilibili.com/"},
            )
            subs = (((r2.json() or {}).get("data") or {})
                    .get("subtitle") or {}).get("subtitles") or []
            if subs:
                sub_url = subs[0].get("subtitle_url") or ""
                if sub_url.startswith("//"):
                    sub_url = "https:" + sub_url
                if sub_url:
                    r3 = await client.get(sub_url)
                    body_json = (r3.json() or {}).get("body") or []
                    subtitle_text = "".join(
                        seg.get("content", "") for seg in body_json
                    )[:4000]
                    if subtitle_text:
                        lines.append(f"字幕（{subs[0].get('lan_doc', '')}）：")
                        lines.append(subtitle_text)
        except Exception:
            pass

    note = "来自 B站官方 API"
    if not subtitle_text:
        note += "（该视频无字幕或字幕未开放）"
    return FetchResult(ok=True, title=title, body="\n".join(lines),
                       layer="L2", note=note)


async def fetch_github(client: httpx.AsyncClient, url: str) -> FetchResult:
    """GitHub REST API：仓库描述、star、语言、话题 + README 全文。"""
    m = re.search(r"github\.com/([^/\s?#]+)/([^/\s?#]+)", url)
    if not m:
        return FetchResult(note="未识别到 owner/repo", layer="L2")
    owner, repo = m.group(1), m.group(2).removesuffix(".git")

    headers = {"Accept": "application/vnd.github+json"}
    try:
        r = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}", headers=headers
        )
        if r.status_code != 200:
            return FetchResult(note=f"GitHub API HTTP {r.status_code}", layer="L2")
        d = r.json()
    except Exception as e:
        return FetchResult(note=f"GitHub API 失败：{type(e).__name__}", layer="L2")

    lines = ["【GitHub 仓库信息】"]
    if d.get("description"):
        lines.append(f"简介：{d['description']}")
    lines.append(f"Star：{d.get('stargazers_count', 0)}　Fork：{d.get('forks_count', 0)}")
    if d.get("language"):
        lines.append(f"主要语言：{d['language']}")
    if d.get("topics"):
        lines.append("话题：" + "、".join(d["topics"][:10]))
    if d.get("updated_at"):
        lines.append(f"最近更新：{d['updated_at'][:10]}")
    if d.get("homepage"):
        lines.append(f"主页：{d['homepage']}")

    # README
    readme = ""
    for branch in (d.get("default_branch") or "main", "master"):
        try:
            r2 = await client.get(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            )
            if r2.status_code == 200 and r2.text.strip():
                readme = r2.text[:6000]
                break
        except Exception:
            continue
    if readme:
        lines.append("README：")
        lines.append(readme)

    return FetchResult(
        ok=True,
        title=d.get("full_name") or f"{owner}/{repo}",
        body="\n".join(lines),
        layer="L2",
        note="来自 GitHub API" + ("（含 README）" if readme else "（README 未取到）"),
    )


# 单张配图的字节上限。超过就放弃这张（宁可少一张，也不要把请求体撑大）。
MAX_POST_IMAGE_BYTES = 1_500_000

_MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


async def _image_data_url(client: httpx.AsyncClient, url: str) -> str:
    """把一张远程配图抓成 data URL；任何异常都返回空串（不拖垮正文抓取）。

    为什么自己下载而不是把 URL 递给 provider：
    provider 用它自己的 httpx client 取图，会受 AstrBot 进程里 HTTP_PROXY 的影响
    （见 core/images.py 的同款说明）。自己读字节，链路最短、也最可控。
    """
    try:
        r = await client.get(url, headers={"Referer": "https://www.xiaoheihe.cn/"})
        if r.status_code != 200:
            return ""
        raw = r.content or b""
        if not raw or len(raw) > MAX_POST_IMAGE_BYTES:
            return ""
        mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            for ext, guess in _MIME_BY_EXT.items():
                if url.lower().split("?")[0].endswith(ext):
                    mime = guess
                    break
            else:
                mime = "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        return ""


async def fetch_xiaoheihe(
    client: httpx.AsyncClient, url: str, image_budget: int = 0
) -> FetchResult:
    """小黑盒帖子：调 link/tree 接口，hkey/_time/nonce 每次现签。

    签名算法在 core/xiaoheihe.py（从分享页 bundle 里还原出来的）。
    这里只负责发请求 + 重试 + 组装结果。

    Args:
        image_budget: 顺带把前 N 张配图抓成 data URL（0 = 不抓）。
    """
    link_id = link_id_of(url)
    if not link_id:
        return FetchResult(note="没从链接里认出小黑盒帖子 id", layer="L2")

    last_note = ""
    for attempt in range(2):
        sign = build_sign_params(XHH_API_PATH)
        params = dict(XHH_BASE_PARAMS)
        params.update({
            "link_id": link_id,
            "hkey": sign.hkey,
            "_time": str(sign._time),
            "nonce": sign.nonce,
        })
        try:
            r = await client.get(XHH_API_URL, params=params, headers=XHH_HEADERS)
            payload = r.json()
        except Exception as e:
            last_note = f"小黑盒接口请求失败：{type(e).__name__}"
            continue

        post = parse_link_tree(payload)
        if not post.ok:
            last_note = post.note
            # 只有「签名被拒」才值得换一组重签重试。
            # show_captcha 是 IP 级风控，立刻重试只会雪上加霜——放着，让它自己凉。
            if attempt == 0 and "非法请求" in post.note:
                continue
            break

        extra: dict = {"real_url": post.real_url or url}
        if post.images:
            extra["images"] = post.images
            if image_budget > 0:
                picked = post.images[:image_budget]
                data_urls = await asyncio.gather(
                    *(_image_data_url(client, u) for u in picked)
                )
                data_urls = [d for d in data_urls if d]
                if data_urls:
                    extra["image_data_urls"] = data_urls
                    extra["images_attached"] = len(data_urls)
        return FetchResult(
            ok=True, title=post.title, body=post.body,
            layer="L2", note=post.note, extra=extra,
        )

    return FetchResult(note=last_note or "小黑盒接口没返回可用内容", layer="L2")


# --------------------------------------------------------------------------- #
# L1：通用抓取
# --------------------------------------------------------------------------- #

async def fetch_url(
    url: str,
    timeout: int = 15,
    allow_api: bool = True,
    reject_private: bool = True,
    image_budget: int = 0,
) -> FetchResult:
    """抓取一个链接。先看是否命中专用 API，否则走通用正文提取。

    抓不到正文时不再只报一句含糊的失败：会补一层 OG 元数据兜底，
    并把「站点不给读 / 页面是 JS 空壳 / 站点风控」这几种情况区分开——
    模型拿到准确原因，才不会拿常识去编内容填坑。

    Args:
        image_budget: 允许顺带抓多少张配图（目前只有小黑盒用得上）。
    """
    safe, reason = is_safe_url(url, reject_private)
    if not safe:
        return FetchResult(note=reason, layer="L1")

    # 已知读不到的站点：直接给准确解释，不做无谓请求（也省一次超时等待）
    blocked_site = match_unreadable(url)
    if blocked_site:
        name, why = blocked_site
        return FetchResult(
            ok=False,
            layer="L0",
            note=why,
            extra={"site": name, "real_url": unwrap_share_url(url)},
        )

    rule = match_rule(url)
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if rule and rule.referer:
        headers["Referer"] = rule.referer

    try:
        # trust_env=False：**刻意绕开 HTTP_PROXY/HTTPS_PROXY 环境变量**。
        # AstrBot 进程环境里设了 HTTP_PROXY=http://127.0.0.1:7890（mihomo），
        # httpx 默认会读它，于是抓取被绑到代理节点上——节点一抖抓取就全线失败
        # （这台机器已因同类问题踩过 Gemini ConnectError 的坑）。
        # 实测本机直连能力足够（直连 Google / r.jina.ai 均 200），
        # 且直连访问国内站点的结果反而更正常，故固定走直连。
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=timeout,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        ) as client:
            # L2 优先：官方 API 的信息质量远高于抓网页
            if allow_api and rule and rule.api == "bilibili":
                return await fetch_bilibili(client, url)
            if allow_api and rule and rule.api == "github":
                return await fetch_github(client, url)
            if allow_api and rule and rule.api == "xiaoheihe":
                return await fetch_xiaoheihe(client, url, image_budget=image_budget)

            r = await client.get(url)
            html = r.text or ""
            site = rule.name if rule else "generic"
            selectors = rule.selectors if rule else ()

            title, body = extract_article(html, selectors)
            m_title, m_desc, m_image = extract_meta(html)
            title = title or m_title

            extra: dict = {"real_url": str(r.url), "http": r.status_code}
            if m_image:
                extra["og_image"] = m_image

            # 明显的风控/验证页识别
            low = html[:4000]
            blocked = any(
                k in low for k in ("环境异常", "安全验证", "security control",
                                   "Access Denied", "Just a moment", "验证码")
            )
            if blocked:
                return FetchResult(
                    ok=False, title=title, layer="L1", extra=extra,
                    note=f"站点 {site} 返回风控/验证页（服务器机房 IP 被拦截），未取到正文",
                )

            if len(body) >= 80:
                return FetchResult(
                    ok=True, title=title, body=body, layer="L1", extra=extra,
                    note=f"来自 {site} 网页正文",
                )

            # 正文不足 → 分情况给准确原因，能兜多少兜多少
            if m_desc and len(m_desc) >= 40:
                return FetchResult(
                    ok=True, title=title, body=m_desc, layer="L1", extra=extra,
                    note=f"正文在 JS 里渲染，服务端只输出了分享用的页面描述（{site}）",
                )
            if looks_like_spa_shell(html, body):
                return FetchResult(
                    ok=False, title=title, layer="L1", extra=extra,
                    note=f"页面是动态渲染的空壳（{site}），服务端 HTML 里没有正文",
                )
            return FetchResult(
                ok=False, title=title, layer="L1", extra=extra,
                note=f"站点 {site} 未提取到有效正文（HTTP {r.status_code}）",
            )
    except httpx.TimeoutException:
        return FetchResult(note=f"抓取超时（>{timeout}s）", layer="L1")
    except Exception as e:
        return FetchResult(note=f"抓取失败：{type(e).__name__}", layer="L1")


async def fetch_many(urls: list[str], timeout: int = 15,
                     allow_api: bool = True, reject_private: bool = True,
                     concurrency: int = 3, image_budget: int = 0) -> dict[str, FetchResult]:
    """并发抓取多个链接。"""
    if not urls:
        return {}
    sem = asyncio.Semaphore(max(1, concurrency))
    out: dict[str, FetchResult] = {}

    async def _one(u: str) -> None:
        async with sem:
            out[u] = await fetch_url(
                u, timeout, allow_api, reject_private, image_budget=image_budget
            )

    await asyncio.gather(*(_one(u) for u in urls), return_exceptions=True)
    return out
