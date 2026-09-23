"""小黑盒（xiaoheihe / heybox）帖子内容读取。

为什么需要这个模块
------------------
小黑盒的 bbs 帖子页是纯 CSR 的 SPA：服务端返回的 HTML 只有 2.7KB 的空壳，
正文全靠 JS 渲染。所以「抓网页」这条路永远拿不到内容。

但它自己会调一个接口拿真实帖子数据：

    GET https://api.xiaoheihe.cn/bbs/app/link/tree

这个接口**可以直接调**，返回的 JSON 里 `result.link` 就是完整帖子
（标题、正文 HTML、作者、话题、点赞/评论数），`result.comments` 是热评。

拦路虎只有签名
--------------
接口要求 `hkey` / `_time` / `nonce` 三个参数自洽，乱填一律 `非法请求`。
三者的关系在分享页的 bundle 里（`bbs-post_share_v2/app/index-sAj0v08M.js`）：

    const tr = ["a","b","e","g","h","i","m","n","o","p","q","r","s","t","u","w"];
    //  tr[4]+"k"+tr[2]+"y"          = "hkey"
    //  "_"+tr[13]+tr[5]+tr[6]+tr[2] = "_time"
    //  tr[7]+tr[8]+tr[7]+"c"+tr[2]  = "nonce"

    function dF(path){
      let t, n, r, i = pb(), o = tr[3];           // tr[3] === "g"
      t = ~~(+gb.w()/1e3);                        // _time = unix 秒
      r = MD5(t + Date.now() + Math.random() + pb() + i).toUpperCase();   // nonce
      n = gb[o](path, t, r);                      // gb.g = _t(path, t+1, nonce)
      return {version:"999.0.4", hkey:n, _time:t, nonce:r};
    }

`_t` 就是那个签名函数，本模块的 `sign_hkey` 是它的逐句复刻，
已用线上抓包的真实三元组验证过（`_time=1789110819` / `nonce=47414276…`
→ `hkey=WI0I701`，完全一致，见 tests/test_xiaoheihe.py）。

**为什么不能「抓一次包然后重放」**：hkey 把 `_time` 也签进去了，
所以任何一部分变动都会破坏三者的自洽——
  · 换 nonce、hkey 不变            → 非法请求
  · hkey 不变、_time 改成当前时间   → 非法请求
  · 三者自洽（本模块实时计算）      → 通过
唯一可行的是把算法算出来，每次现签。

`x_client_type=weboutapp` 是另一个坑：分享页自己用的就是它，
换成 `web` / `heybox_app` 会被拒（这是之前误判「小黑盒读不到」的原因）。

⚠️ 已知运营风险：**同一个出口 IP 短时间请求过多会被风控升级成「要求人机验证」**
（返回 `{"status":"show_captcha"}`）。注意这时**签名是过关的**——
我们拿当初抓包的真实基准签名去请求，一样被挡。
所以这不是算法问题，是频率问题；实测连续调试几十次后出现，隔一段时间会自己恢复。
对策只有两条：别高频调、被挡了就如实说明并建议用户发截图（截图走多模态，不受影响）。

如果哪天接口又开始返回「非法请求」，说明算法改了，重新逆向的入口是：
  1. 打开分享页，取 HTML 里的 `<script src>` 列表，重点是
     `https://static.max-c.com/static/heybox/webapp/@heybox-h5/bbs-post_share_v2/app/index-*.js`
  2. 在 bundle 里搜 `_rnd` —— 签名相关的几个函数（`dF` / `_t` / `gb` / `tr` / `pb`）
     都聚在 `kN`（`_rnd="15:…"`）那一段附近
  3. 用本文件的基准向量对答案：`/bbs/app/link/tree` + `1789110819` +
     `47414276A2361361AE6F95E683356C21` 必须算出 `WI0I701`  # secret-ok 签名算法基准向量
"""

from __future__ import annotations

import hashlib
import html as _html_mod
import json
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# 签名算法（逐句复刻自分享页 bundle）
# --------------------------------------------------------------------------- #

# 签名用的字母表，36 个字符（注意顺序是打乱的，不是字典序）
ALPHA = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"

# 签名覆盖的请求路径。换接口必须同步改这里，否则 hkey 对不上。
API_PATH = "/bbs/app/link/tree"


def _md5_lower(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def _vb(s: str, n: int) -> str:
    """JS `vb(e, ALPHA, n)`：字母表先 `slice(0, n)`（n 为负则去掉尾部 |n| 个），
    再把 s 的每个字符按码位取模映射上去。"""
    alpha = ALPHA[:n]
    if not alpha:
        return ""
    return "".join(alpha[ord(ch) % len(alpha)] for ch in s)


def _mb(s: str) -> str:
    """JS `mb(e, ALPHA)`：整表映射（不截断）。"""
    return "".join(ALPHA[ord(ch) % len(ALPHA)] for ch in s)


def _uf(arrs: list[str]) -> str:
    """JS `uF`：把若干等长/不等长字符串按列交错合并（round-robin）。"""
    if not arrs:
        return ""
    out: list[str] = []
    for i in range(max(len(a) for a in arrs)):
        for a in arrs:
            if i < len(a):
                out.append(a[i])
    return "".join(out)


# 一小撮 8 位混淆轮（Qv / Oo / Ds / qa / Ic）
def _qv(v: int) -> int:
    return ((v << 1) ^ 27) & 255 if (v & 128) else (v << 1) & 255


def _oo(v: int) -> int:
    return _qv(v) ^ v


def _ds(v: int) -> int:
    return _oo(_qv(v))


def _qa(v: int) -> int:
    return _ds(_oo(_qv(v)))


def _ic(v: int) -> int:
    return _qa(v) ^ _ds(v) ^ _oo(v)


def _cf(codes: list[int]) -> list[int]:
    """JS `cF` 的复刻。

    ⚠️ 这里的坑：JS 里 `cF` 收到的是 `md5.slice(-6)` 的 **6 个**字符码，
    它只重写 `e[0..3]`，然后把**整条原数组** return 出去
    （`e[4]`、`e[5]` 原样保留）。所以外层 `fF(cF(...))` 求和的是 6 个数。
    少算这两个字节 → 校验位差 1（`WI0I700` vs `WI0I701`），服务端不认。
    """
    e = list(codes)
    if len(e) < 4:
        e = e + [0] * (4 - len(e))
    t0 = _ic(e[0]) ^ _qa(e[1]) ^ _ds(e[2]) ^ _oo(e[3])
    t1 = _oo(e[0]) ^ _ic(e[1]) ^ _qa(e[2]) ^ _ds(e[3])
    t2 = _ds(e[0]) ^ _oo(e[1]) ^ _ic(e[2]) ^ _qa(e[3])
    t3 = _qa(e[0]) ^ _ds(e[1]) ^ _oo(e[2]) ^ _ic(e[3])
    e[0], e[1], e[2], e[3] = t0, t1, t2, t3
    return e


def sign_hkey(path: str, _time: int, nonce: str) -> str:
    """算出 hkey（7 个字符）。

    对应 JS 的 `gb.g(path, _time, nonce)` → `_t(path, _time + 1, nonce)`。
    注意那个 **+1** 是 `gb.g` 干的，不是 `_t` 本身的语义。

    ⚠️ 反直觉但真实的特性：种子只取「交错后前 20 个字符」，
    而三个分量是按 round-robin 交错的（i[0],o[0],s[0],i[1],o[1],s[1],…），
    所以每个分量只有**前 7 位**真正进入签名。

    这件事解释了一个曾经很迷惑的现象：拿抓包到的 hkey 去重放，
    把 `_time` 换成「当前时间」在几分钟内居然能过，过一会儿又突然「非法请求」。
    因为 `_time` 是 10 位十进制数，前 7 位要每约 181 秒才跨一格——
    这也正是「抓一次重放」走不通的根本原因（不是 nonce 有 TTL，是签名窗口在滑）。
    """
    t = int(_time) + 1
    # JS 先做一次「去掉空段、首尾补 /」的归一化
    norm = "/" + "/".join(seg for seg in path.split("/") if seg) + "/"

    i = _vb(str(t), -2)     # _time 映射
    o = _mb(norm)           # path 映射
    s = _mb(nonce)          # nonce 映射
    seed = _uf([i, o, s])[:20]
    digest = _md5_lower(seed)

    checksum = str(sum(_cf([ord(ch) for ch in digest[-6:]])) % 100).zfill(2)
    return _vb(digest[:5], -4) + checksum


def make_nonce() -> str:
    """生成一个 nonce。

    JS 里 nonce = `MD5(_time + Date.now() + Math.random() + pb() + pb()).toUpperCase()`，
    其中 `pb()` 是浏览器指纹（WebGL renderer / canvas 指纹 + UA + 时区 + 语言）。
    服务端无法复算那个指纹，nonce 对它只是个不透明随机串——
    它只会拿收到的 nonce 重算 hkey 做比对。所以这里用随机数即可，
    但仍然生成成 32 位大写 hex 的形状，避免任何形状校验上的意外。
    """
    now = time.time()
    parts = [
        str(int(now)),
        str(int(now * 1000)),
        repr(random.random()),
        repr(random.random()),
        repr(random.random()),
    ]
    return _md5_upper("".join(parts))


@dataclass
class SignParams:
    """一次请求要带的签名三元组。"""

    hkey: str
    _time: int
    nonce: str


def build_sign_params(path: str = API_PATH, now: float | None = None) -> SignParams:
    """现签一组自洽的 (hkey, _time, nonce)。"""
    ts = int(now if now is not None else time.time())
    nonce = make_nonce()
    return SignParams(hkey=sign_hkey(path, ts, nonce), _time=ts, nonce=nonce)


def verify_alignment(path: str, _time: int, nonce: str, hkey: str) -> bool:
    """自检：给定的三元组是否自洽（用于单测与线上排障）。"""
    return sign_hkey(path, _time, nonce) == hkey


# --------------------------------------------------------------------------- #
# 请求参数
# --------------------------------------------------------------------------- #

API_URL = "https://api.xiaoheihe.cn" + API_PATH

# 这组参数是从分享页的真实请求上抄下来的，不要凭感觉改。
# 尤其是 x_client_type=weboutapp —— 它是分享页专用的客户端标识。
BASE_PARAMS: dict[str, str] = {
    "app": "heybox",
    "heybox_id": "",
    "os_type": "web",
    "x_app": "heybox_website",
    "x_client_type": "weboutapp",
    "x_os_type": "iOS",
    "x_client_version": "",
    "version": "999.0.4",
    "web_version": "2.5",
    "is_share": "1",
    "offset": "0",
    "limit": "3",
    "use_concept_type": "",
}

# 必须是移动端 UA：参数里声明了 x_os_type=iOS，用桌面 UA 与之冲突。
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

REQUEST_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Referer": "https://www.xiaoheihe.cn/",
    "Accept": "application/json, text/plain, */*",
}

MAX_BODY_CHARS = 20000
MAX_COMMENTS = 5


# --------------------------------------------------------------------------- #
# link_id 提取
# --------------------------------------------------------------------------- #

# 三种真实见过的形态：
#   https://www.xiaoheihe.cn/app/bbs/link/9e8e726f0c52
#   https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?...&link_id=9e8e726f0c52
#   https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=9e8e726f0c52
_LINK_ID_PATTERNS = (
    r"[?&]link_id=([0-9A-Za-z]+)",
    r"/bbs/link/([0-9A-Za-z]+)",
    r"/link/([0-9A-Za-z]{8,})",
)


def link_id_of(url: str) -> str:
    """从各种形态的小黑盒链接里取出 link_id，取不到返回空串。"""
    if not url:
        return ""
    for pattern in _LINK_ID_PATTERNS:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return ""


def is_xiaoheihe_url(url: str) -> bool:
    return "xiaoheihe.cn" in (url or "") or "heybox.com" in (url or "")


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #

@dataclass
class XhhPost:
    """从小黑盒 link/tree 响应里解析出来的帖子。"""

    ok: bool = False
    link_id: str = ""
    title: str = ""
    body: str = ""
    images: list[str] = field(default_factory=list)
    author: str = ""
    create_at: int = 0
    note: str = ""

    @property
    def real_url(self) -> str:
        return f"https://www.xiaoheihe.cn/app/bbs/link/{self.link_id}" if self.link_id else ""


def _plain(html: str) -> str:
    """把一小段 HTML 拍成纯文本。

    这里刻意用轻量实现而不是 fetchers.html_to_text：
    评论里常夹 `<a href="heybox://%7B...%7D">` 这类 App 内跳转，
    markdownify 会渲染成一大坨 `[文字](heybox://…)`，纯噪声。
    """
    if not html:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    s = re.sub(r"</(p|div|li|h[1-6])>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html_mod.unescape(s)
    # App 内协议链接的残留（href 被剥掉后可能留下裸的 heybox://…）
    s = re.sub(r"heybox://\S+", "", s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def _img_urls_from_html(html: str) -> list[str]:
    """正文 HTML 里内嵌的图片（小黑盒用 data-original 存原图）。

    `src` 放最后：它是懒加载占位，真图在 `data-original`。
    """
    out: list[str] = []
    for attr in ("data-original", "data-src", "src"):
        for m in re.finditer(rf'{attr}=["\'](https?://[^"\']+)["\']', html or ""):
            url = m.group(1)
            if url not in out:
                out.append(url)
    return out


def _img_path(url: str) -> str:
    """图片的「身份」：去掉协议、主机和查询串，只留路径。

    同一条帖子里的同一张图有两种写法，主机和 query 都不一样：
      · HTML 内嵌：https://imgheybox.max-c.com/web/bbs/…/1cda691….jpeg
      · 图片块  ：https://imgheybox1.max-c.com/web/bbs/…/1cda691….jpeg?imageMogr2/…
    不按路径归一，一张图会被数成两张（实测 15 张变 30 张）。
    """
    try:
        path = urlparse(url).path
    except Exception:
        path = ""
    return path or url.split("?")[0]


def _parse_blocks(raw) -> tuple[str, list[str]]:
    """解析 `link.text`，返回 (正文纯文本, 图片 URL 列表)。

    `text` 字段是**一个 JSON 字符串**（不是 HTML），decode 后是块数组：
        [{"type": "html", "text": "<p>…</p><h2>…</h2>"},
         {"type": "img",  "url": "https://imgheybox…", "width":…, "height":…}, …]
    正文块自己还内嵌 `<img data-original=…>`（同一批图），两处都要收但不能重复计。
    也兼容「text 直接就是 HTML 字符串」的老形态。
    """
    if not raw:
        return "", []
    if isinstance(raw, (list, dict)):
        blocks = raw if isinstance(raw, list) else [raw]
    else:
        text = str(raw).strip()
        try:
            blocks = json.loads(text)
            if not isinstance(blocks, list):
                blocks = [blocks]
        except Exception:
            return _plain(text), _img_urls_from_html(text)

    parts: list[str] = []
    block_imgs: list[tuple[str, str]] = []
    inline_imgs: list[tuple[str, str]] = []
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        html = blk.get("text")
        if html:
            for u in _img_urls_from_html(str(html)):
                inline_imgs.append((_img_path(u), u))
            chunk = _plain(str(html))
            if chunk:
                parts.append(chunk)
        elif blk.get("type") == "img" or "url" in blk:
            url = str(blk.get("url") or "").strip()
            if url.startswith("http"):
                block_imgs.append((_img_path(url), url))

    # 独立图片块优先：它的 URL 带 CDN 缩略参数（?imageMogr2/…/thumbnail/850x1450），
    # 下载体积可控；HTML 内嵌的是原图，只在图片块没覆盖到某张时补上。
    images: list[str] = []
    seen: set[str] = set()
    for key, url in block_imgs + inline_imgs:
        if key in seen:
            continue
        seen.add(key)
        images.append(url)
    return "\n".join(parts).strip(), images


def _iter_comments(res: dict):
    """热评是「楼中楼」结构：comments[i]["comment"] 是一个列表，[0] 是主楼。"""
    for floor in (res.get("comments") or []):
        if not isinstance(floor, dict):
            continue
        inner = floor.get("comment")
        if isinstance(inner, list):
            for c in inner:
                if isinstance(c, dict):
                    yield c
        elif isinstance(inner, dict):
            yield inner


def _fmt_time(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return ""


def parse_link_tree(payload: dict) -> XhhPost:
    """把 link/tree 的响应解析成 XhhPost（含渲染好的正文材料）。"""
    if not isinstance(payload, dict):
        return XhhPost(note="接口返回的不是 JSON 对象")

    status = payload.get("status")
    msg = str(payload.get("msg") or "").strip()

    if status != "ok":
        # 风控要求人机验证。这不是签名问题——签名过了才会走到这一步，
        # 触发条件通常是**同一个出口 IP 短时间请求过多**（实测：连续调试几十次后出现，
        # 此时连当初抓包拿到的那组基准签名也一样被挡）。
        # 处理原则：如实说，并给用户一条能走的路（截图），别装作读不到。
        if status == "show_captcha":
            return XhhPost(note=(
                "小黑盒本次要求人机验证（同一 IP 请求过于频繁时会被风控挡下），"
                "暂时取不到帖子正文；过一会儿再问，或把帖子截图发我"
            ))
        if "非法请求" in msg:
            return XhhPost(note="小黑盒接口拒绝了请求签名（非法请求），签名算法可能已变更")
        return XhhPost(note=f"小黑盒接口返回失败：{msg or status or '未知原因'}")

    res = payload.get("result") or {}
    link = res.get("link") or {}
    if not link:
        return XhhPost(note="接口没返回帖子内容（可能已被删除或仅作者可见）")

    title = str(link.get("title") or "").strip()
    link_id = str(link.get("share_url") or "")
    link_id = link_id_of(link_id) or str(link.get("linkid") or "")

    full_text, images = _parse_blocks(link.get("text"))
    description = _plain(str(link.get("description") or ""))

    # 正文为空时（纯图帖）退回 description，它也是服务端直出的真实内容
    body_text = full_text or description

    user = link.get("user") or {}
    author = str(user.get("username") or "").strip()
    level = ((user.get("level_info") or {}).get("level")) if isinstance(user.get("level_info"), dict) else None
    medals = user.get("medals") or user.get("medal") or []
    medal_names = [
        str(m.get("name")) for m in medals
        if isinstance(m, dict) and m.get("name")
    ][:2]

    lines: list[str] = ["【小黑盒帖子】"]
    if author:
        who = author
        tags = []
        if level:
            tags.append(f"Lv{level}")
        tags.extend(medal_names)
        if tags:
            who += "（" + " · ".join(tags) + "）"
        lines.append(f"作者：{who}")
    created = _fmt_time(link.get("create_at"))
    ip_loc = str(link.get("ip_location") or "").strip()
    if created:
        lines.append(f"发布：{created}" + (f"（IP {ip_loc}）" if ip_loc else ""))

    topics = [
        str(t.get("name")) for t in (link.get("topics") or [])
        if isinstance(t, dict) and t.get("name")
    ]
    if topics:
        lines.append("话题：" + "、".join(topics[:5]))

    stats: list[str] = []
    if link.get("comment_num") not in (None, ""):
        stats.append(f"评论 {link.get('comment_num')}")
    if link.get("favour_count") not in (None, ""):
        stats.append(f"点赞 {link.get('favour_count')}")
    if link.get("forward_num") not in (None, ""):
        stats.append(f"转发 {link.get('forward_num')}")
    if stats:
        lines.append("数据：" + " · ".join(stats))

    declaration = (link.get("post_declaration_v2") or {})
    if isinstance(declaration, dict) and declaration.get("content_source_desc"):
        lines.append(f"声明：{declaration['content_source_desc']}")

    if body_text:
        if len(body_text) > MAX_BODY_CHARS:
            body_text = body_text[:MAX_BODY_CHARS] + f"\n……（正文过长，已截断，原长 {len(full_text)} 字）"
        lines.append("正文：")
        lines.append(body_text)
    else:
        lines.append("正文：帖子没有文字内容（可能是纯图帖）")

    if images:
        lines.append(f"配图：{len(images)} 张")

    comments_out: list[str] = []
    for c in list(_iter_comments(res))[:MAX_COMMENTS]:
        txt = _plain(str(c.get("text") or "")).replace("\n", " ").strip()
        if not txt:
            continue
        favour = c.get("favour_count")
        suffix = f"（赞 {favour}）" if favour not in (None, "", 0) else ""
        comments_out.append(f"  · {txt[:200]}{suffix}")
    if comments_out:
        lines.append(f"热评（前 {len(comments_out)} 条）：")
        lines.extend(comments_out)

    return XhhPost(
        ok=True,
        link_id=link_id,
        title=title,
        body="\n".join(lines),
        images=images,
        author=author,
        create_at=int(link.get("create_at") or 0),
        note="来自小黑盒分享接口（link/tree，实时签名）",
    )
