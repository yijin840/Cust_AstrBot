"""深读（astrbot_plugin_deepread）

定位：**给分享内容修一条不依赖模型自觉的通路**，不是给用户加一堆指令。

v1.5.0 只挂了 LLM 工具 `deepread_share`，指望模型自己挑中它。实测模型会改挑
通用 `web_fetch`——拿到 JS 空壳的标题，再去联网搜索，最后拿搜索结果的常识
编出一篇「帖子内容」。工具调用次数：0。

v1.6.0 起主路径改成**预读注入**（`on_llm_request`）：在请求发给模型之前，
把当前消息里的分享（或用户正在指代的最近一条分享）抓下来，作为临时内容块
挂到用户消息末尾。模型不需要选工具，睁眼就看得见正文；抓不到时它看到的是
「正文没取到 + 原因」，这比任何「不许编造」的提示词都管用。

四个必须有但用户看不见的机制：

  1. **预读注入**（`on_llm_request`）—— 主路径，保证模型看得见正文（v1.6.0）
  2. **静默缓存**（`on_message_cache`）
     只有记录，没有回复。否则「刚才那个」这类指代永远找不到东西可读。
  3. **被引用消息解析**
     你引用那条分享消息问「这个是啥」，内容从被引用消息里取。
  4. **工具 `deepread_share`** —— 兜底路径：用户明确说「去读一下那个链接」时，
     模型主动调用仍然能用（读的是同一条通路，结果写回缓存）。

取内容优先级：
  当前消息（含被引用） → 缓存里按关键词找 → 缓存里最近的

设计取舍详见同目录 DESIGN.md。
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.cache import ShareCache
from .core.cards import CARD_TEXT_MARKERS, extract_urls
from .core.config import ReadConfig
from .core.extractor import collect_from_event, dedupe, item_from_url
from .core.fetchers import fetch_many, match_rule
from .core.inject import build_block, looks_like_referral, needs_fetch
from .core.models import KIND_IMAGE, ReadItem
from .core.summarizer import build_receipt, summarize

# 预读注入用的内容块类型。
# 用 TextPart + mark_as_temp()：挂在用户消息末尾，不写进对话历史，
# 也就不影响前缀缓存、不会在后续轮次里反复出现。
try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 版本差异兜底
    TextPart = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_deepread"

# 值得缓存的消息段类型。
# 注意 QQ 官方机器人把卡片拍平成 Plain 文本（见 core/cards.py），
# 所以 Plain 不在这个集合里，靠下面的文本标记兜住。
# Image 必须在：图片落地文件会被系统定时清掉，只有收到的那一刻读得到。
_SHARE_SEGMENTS = {"Json", "Image", "File", "Forward", "Nodes", "Node", "Reply"}

# 预读注入只关心「能变成文字进 prompt」的那些。
# 图片不在里面：注入块由 build_block() 生成，它本来就会跳过图片
# （图片走多模态，见 _collect_images），所以读它纯属白读一遍几百 KB。
_INJECTABLE_SEGMENTS = _SHARE_SEGMENTS - {"Image"}


@register(
    PLUGIN_NAME,
    "jin",
    "让小蜗读懂群里分享的内容：卡片顺链接读正文，读不到就如实说、不编造。",
    "v1.6.0",
    "",
)
class DeepReadPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.cfg = ReadConfig.from_raw(self.config)
        self.cache = ShareCache(size=self.cfg.cache_size, ttl=self.cfg.cache_ttl)
        logger.info(
            f"[deepread] 已加载。工具 deepread_share 已就绪，"
            f"静默缓存 {self.cfg.cache_size} 条 / {self.cfg.cache_ttl}s"
        )

    # ------------------------------------------------------------------ #
    # 内部：配置 / 补抓 / 取内容
    # ------------------------------------------------------------------ #
    def _reload_cfg(self) -> ReadConfig:
        """每次调用重新读配置，WebUI 改动即时生效。"""
        self.cfg = ReadConfig.from_raw(self.config)
        self.cache.size = self.cfg.cache_size
        self.cache.ttl = self.cfg.cache_ttl
        return self.cfg

    @staticmethod
    def _usable(items: list[ReadItem]) -> list[ReadItem]:
        return [i for i in items if not i.is_video and i.has_content()]

    @staticmethod
    def _collect_images(items: list[ReadItem], cfg: ReadConfig) -> list[str]:
        """收集图片附件（data URL 优先，其次链接），上限由配置控制。

        两个来源：
          1. 群友直接发的图（KIND_IMAGE）——「收到就转」存下的 data URL
          2. 抓正文时顺带取回的帖子配图（item.extra["image_data_urls"]）
             小黑盒这类图文帖，菜单/价目表往往只存在于图片里，正文文字不全
        """
        if not cfg.enable_images:
            return []
        out: list[str] = []
        for item in items:
            if item.kind == KIND_IMAGE:
                ref = item.extra.get("image_data_url") or item.extra.get("image_ref")
                if ref:
                    out.append(str(ref))
            else:
                for data_url in (item.extra.get("image_data_urls") or []):
                    if data_url:
                        out.append(str(data_url))
            if len(out) >= cfg.max_images:
                break
        return out[: cfg.max_images]

    async def _enrich(self, items: list[ReadItem], cfg: ReadConfig) -> None:
        """给有链接但没正文的条目补抓内容（L1/L2）。

        抓不到也要留下**准确原因**（fetch_url 内部会区分「站点不给读」
        「页面是 JS 空壳」「站点风控」），这条原因会一路带到模型面前，
        模型才不会拿常识去补内容。
        """
        if not (cfg.enable_l1_direct or cfg.enable_l2_api):
            return

        targets: list[str] = []
        for item in items:
            # 图片不走链接抓取：它的内容已经以 data URL 存在 extra 里了
            if item.kind == KIND_IMAGE:
                continue
            if item.is_video or not item.url or item.body:
                continue
            rule = match_rule(item.url)
            if rule and rule.api:
                if cfg.enable_l2_api:
                    targets.append(item.url)
            elif cfg.enable_l1_direct:
                targets.append(item.url)

        if not targets:
            return

        logger.debug(f"[deepread] 待补抓 {len(targets)} 条: {targets}")
        results = await fetch_many(
            targets,
            timeout=cfg.fetch_timeout,
            allow_api=cfg.enable_l2_api,
            reject_private=cfg.reject_private_ip,
            concurrency=3,
            image_budget=cfg.max_images if cfg.enable_images else 0,
        )
        for item in items:
            res = results.get(item.url)
            if res is None:
                continue
            if res.title and not item.title:
                item.title = res.title
            if res.body:
                item.body = res.body
                item.layer = res.layer or item.layer
            if res.note:
                item.note = res.note
            # 带上「人能点开的真实地址」等辅助信息
            if res.extra:
                for key, value in res.extra.items():
                    if value and not item.extra.get(key):
                        item.extra[key] = value

        got = sum(1 for i in items if i.body)
        logger.info(
            f"[deepread] 补抓完成：{got}/{len(targets)} 条拿到正文"
            + ("" if got else "（仅拿到标题/摘要，已标注原因）")
        )

    async def _pick_items(
        self, event: AstrMessageEvent, cfg: ReadConfig, hint: str = ""
    ) -> tuple[list[ReadItem], str]:
        """决定要读哪些内容，返回 (条目, 来源说明)。

        优先级：hint 里的链接 → 当前消息（含被引用） → 缓存关键词 → 缓存最近
        """
        session = event.unified_msg_origin

        # 1) hint 直接给了链接
        if hint:
            urls = extract_urls(hint)
            if urls:
                return [item_from_url(u, cfg, kind="link")
                        for u in urls[: cfg.max_items]], "来自你给的链接"

        # 2) 当前消息（引用分享时会带上被引用的消息链）
        try:
            items = await collect_from_event(event, cfg)
        except Exception as e:
            logger.error(f"[deepread] 抽取当前消息失败: {type(e).__name__}: {e}")
            items = []
        usable = self._usable(items)
        if usable:
            return usable[: cfg.max_items], "来自当前消息"

        # 3) 缓存：先按关键词找
        if hint:
            hit = self.cache.search(session, hint, cfg.max_items)
            hit = self._usable(hit)
            if hit:
                logger.debug(f"[deepread] 缓存命中关键词「{hint}」: {len(hit)} 条")
                return hit, f"来自最近分享（按「{hint}」匹配）"

        # 4) 缓存里最近的
        recent = self._usable(self.cache.recent(session, cfg.max_items))
        if recent:
            return recent, "来自本群最近分享"

        return [], ""

    # ------------------------------------------------------------------ #
    # 预读注入：把「可选工具」变成「必然看得见」
    # ------------------------------------------------------------------ #
    @staticmethod
    def _looks_like_share(chain: list, text: str, segments: set[str] | None = None) -> bool:
        """快筛：这条消息里有没有可能藏着分享/链接。

        缓存与预读注入共用同一道闸，原因不一样：
        · 别的地方：绝大多数消息不是分享，没必要走完整抽取；
        · **预读注入尤其需要**：`collect_from_event` 会把图片读成 data URL，
          而这个钩子**每轮 LLM 请求都跑**。群里有人发张图，
          没有这道闸就是每轮白读一遍几百 KB 的文件。

        `segments` 用来收窄关注范围：预读注入传 `_INJECTABLE_SEGMENTS`，
        把纯图片消息排除在外（图片本来就不进注入块，读了纯属浪费）。
        """
        kinds = _SHARE_SEGMENTS if segments is None else segments
        try:
            names = {type(seg).__name__ for seg in chain}
        except Exception:
            names = set()
        if names & kinds:
            return True
        if any(m in text for m in CARD_TEXT_MARKERS):
            return True
        return bool(extract_urls(text))

    async def _prefetch_block(self, event: AstrMessageEvent,
                              cfg: ReadConfig) -> str | None:
        """抓好正文并渲染成注入块；没有可注入的东西时返回 None。

        两条来源，语义完全不同，不能混为一谈：

        1. **当前消息里就带着分享/链接** —— 用户刚发过来，必定要读。
           这轮消息自带分享时，就只认它，不去掺和缓存里的旧东西。
        2. **缓存里最近一条** —— 用户这轮没发东西，但在指代「刚才那个」。
           必须同时满足「消息像在指代」+「还在时间窗内」，否则一句闲聊
           就会把十分钟前的帖子糊到模型脸上。
        """
        session = event.unified_msg_origin

        try:
            text = event.message_str or ""
        except Exception:
            text = ""

        try:
            chain = event.get_messages() or []
        except Exception:
            chain = []

        items: list[ReadItem]
        origin: str

        if self._looks_like_share(chain, text, _INJECTABLE_SEGMENTS):
            try:
                current = self._usable(await collect_from_event(event, cfg))
            except Exception as e:
                logger.debug(f"[deepread] 预读抽取失败: {type(e).__name__}: {e}")
                current = []
            # 自带分享但判断不值得读（路过贴的裸链接）→ 这一轮就不注入，
            # 不回头去拿缓存里的旧帖
            if not current or not needs_fetch(current, text):
                return None
            items, origin = current, "当前消息"
        else:
            if cfg.prefetch_window <= 0:
                return None
            age = self.cache.latest_age(session)
            if age is None or age > cfg.prefetch_window:
                return None
            if not looks_like_referral(text):
                return None
            items = self._usable(self.cache.recent(session, cfg.max_items))
            if not items:
                return None
            origin = f"缓存（{age:.0f}s 前）"

        await self._enrich(items, cfg)

        # 回写缓存：把「补抓过正文」的版本存回去。
        # 这样下一轮读「刚才那个」是零成本，不必再打一次接口。
        for item in items:
            self.cache.put(session, item, sender=item.sender or "")

        block = build_block(items, cfg)
        if block:
            got = sum(len(i.body) for i in items)
            logger.info(
                f"[deepread] 预读注入 {len(items)} 条（{origin}，正文 {got} 字）"
            )
        return block

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """在请求发给模型前，把分享正文挂上去。

        这是本插件**唯一**不依赖模型自觉的路径：模型看不到正文时，
        它会去 web_fetch 拿个标题，再拿搜索结果编出一篇「帖子内容」。
        与其教它选对工具，不如让它一开始就看得见。
        """
        cfg = self._reload_cfg()
        if not cfg.enable_prefetch:
            return

        try:
            block = await self._prefetch_block(event, cfg)
        except Exception as e:
            logger.warning(f"[deepread] 预读注入失败: {type(e).__name__}: {e}")
            return
        if not block:
            return

        try:
            if TextPart is not None:
                req.extra_user_content_parts.append(
                    TextPart(text=block).mark_as_temp()
                )
            else:
                # 版本兜底：挂到 system_prompt 末尾
                req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + block
        except Exception as e:
            logger.warning(f"[deepread] 注入内容块失败: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ #
    # 静默缓存：只记录，不回复
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_cache(self, event: AstrMessageEvent):
        """把群里的分享静默记下来，供之后的「刚才那个」使用。

        这里刻意不 yield 任何结果 —— 它不会发言、不会解析正文，
        只是让后来的提问有东西可读。
        """
        try:
            chain = event.get_messages() or []
        except Exception:
            return

        text = ""
        try:
            text = event.message_str or ""
        except Exception:
            pass

        # 快速预筛：绝大多数消息不是分享，直接放过
        if not self._looks_like_share(chain, text):
            return

        cfg = self._reload_cfg()
        try:
            items = await collect_from_event(event, cfg)
        except Exception as e:
            logger.debug(f"[deepread] 缓存抽取失败: {type(e).__name__}: {e}")
            return

        session = event.unified_msg_origin
        try:
            sender = event.get_sender_name() or ""
        except Exception:
            sender = ""

        stored = 0
        titles: list[str] = []
        for item in dedupe(self._usable(items)):
            if item.is_video:
                continue
            self.cache.put(session, item, sender=sender)
            stored += 1
            if item.kind == KIND_IMAGE:
                # 图片的 url 可能是超长 data URL，绝不能进日志
                titles.append(
                    "[图片]" if item.extra.get("image_data_url") else "[图片·未读到]"
                )
            else:
                titles.append((item.title or item.url or "无标题")[:40])

        if stored:
            # 有意用 INFO：分享是低频事件，这条日志是「插件到底有没有在记东西」
            # 唯一的线上观测点（debug 级别在生产被全局日志级别吃掉）。
            logger.info(
                f"[deepread] 已缓存 {stored} 条分享 | {sender or '?'} | "
                + " ; ".join(titles)
            )
        else:
            logger.debug("[deepread] 本轮无可缓存分享")

    # ------------------------------------------------------------------ #
    # 唯一入口：LLM 工具
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="deepread_share")
    async def deepread_share(self, event: AstrMessageEvent, hint: str = ""):
        """读取并总结群里分享的网站内容（分享卡片、网页链接、群文件、合并转发）。

        当用户让你看某个分享、或询问某个链接/卡片里讲什么时调用，例如：
        「看看这个卡片」「刚才那个链接讲的啥」「群里分享的那篇文章说什么」。
        用户可以引用那条分享消息，也可以直接说链接或标题关键词。

        注意：视频分享（B站/抖音/快手/YouTube）不由本工具处理。

        Args:
            hint(string): 可选。线索，可以是链接、标题片段、分享者昵称或关键词。留空则读最近一条分享。
        """
        cfg = self._reload_cfg()
        hint = (hint or "").strip()

        try:
            items, origin = await self._pick_items(event, cfg, hint)
        except Exception as e:
            logger.error(f"[deepread] 取内容异常: {type(e).__name__}: {e}")
            return f"读取失败：{type(e).__name__}"

        if not items:
            logger.info(f"[deepread] 未取到内容 | hint={hint!r} | 会话缓存 {len(self.cache.recent(event.unified_msg_origin, 20))} 条")
            return (
                "我这边没有可读的分享内容。"
                "你可以把链接直接发我，或者引用那条分享消息再让我看看。"
            )

        logger.info(f"[deepread] 取内容：{origin}，{len(items)} 条")

        await self._enrich(items, cfg)

        # 顺手回写缓存：这一轮补抓到的正文留给后续「刚才那个」用
        for item in items:
            self.cache.put(event.unified_msg_origin, item, sender=item.sender or "")

        # 图片走多模态：单独收集成附件，文本材料里只放说明
        image_urls = self._collect_images(items, cfg)
        if image_urls:
            logger.info(f"[deepread] 另附图片 {len(image_urls)} 张，交多模态识别")

        ok, text, reason = await summarize(
            self.context, items, cfg, event.unified_msg_origin,
            image_urls=image_urls,
        )
        if not ok:
            logger.warning(f"[deepread] 总结失败: {reason}")
            receipt = build_receipt(items, [])
            return f"读到了内容但总结没成功（{reason}）：\n{receipt}"

        logger.debug(f"[deepread] 完成，{origin}，{len(items)} 条")
        return text

    async def terminate(self) -> None:
        self.cache.clear()
        logger.info("[deepread] 已卸载。")
