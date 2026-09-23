import asyncio
import re
import sqlite3
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At, Reply
from astrbot.core.platform.message_type import MessageType

import pathlib as _pl
# data 目录 = 本文件所在目录(data/plugins/<plugin>) 向上两级
DB_PATH = str(_pl.Path(__file__).resolve().parents[2] / "plugin_data" / "astrbot_plugin_self_learning" / "messages.db")
HISTORY_LIMIT = 14
SKIP_TOKEN = "[SKIP]"
# [patched] 模型常不严格按 [SKIP] 输出，会写成 {skip} / skip / (skip) 等变体，
# 原严格相等判定漏网后会被 _strip_fragments 剥成 "skip" 直接发出去。这里放宽匹配。
SKIP_RE = re.compile(r"^[\ \t\[{<(（]*skip[\ \t\]}>）)]*[\s。.、,，!！]*$", re.I)

JUNK_RE = re.compile(
    r"(call_\d{3,}|function_call|tool_call|finish_reason|"
    r"Traceback \(most recent|^\s*File \"[^\"]+\.py\"|"
    r"\b(500|502|503|504)\b.*(UNAVAILABLE|error|错误)|UNAVAILABLE)",
    re.I,
)
FRAG_RE = re.compile(
    r"[\s,，;；]*\{?[\"\']?(name|id|args|function)[\"\']?\s*[:=]\s*[\"\']?(call_\w+|[\w/.\-]{0,30})[\"\']?\}?\s*[,，;；]?\s*(\}\s*)?$",
    re.I,
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# RAG 检索块回显特征（kb_mgr 注入格式被模型原样吐出）
KB_DUMP_RE = re.compile(r"【知识 \d+】|^来源: .+\.[a-z]+|相关度: 0\.\d+", re.M)

# 纯符号/空壳回复（模型降级后吐出 "]" "..." 之类无意义内容）
NOISE_RE = re.compile(r"[^\w\u4e00-\u9fff\U0001F300-\U0001FAFF\u2600-\u27BF]")
NULL_RE = re.compile(r"^(null|none|undefined|n/?a|无|空)$", re.I)


AWARENESS_TMPL = """

【开口前的语境自省（内心思考，不要在回复里提及这段落）】
上方 <system_reminder> 里是你们上次说话之后群里的对话记录，先读完它再开口，弄清谁在跟谁说话。
现在这条消息：[{sender}] {message}
{mode_block}"""

MODE_ADDRESSED = """
像真人一样先想一秒：结合上方群聊上下文判断，这条消息在跟谁说话？
- 在找你（@你、回你的消息、直接问你）→ 正常回应，记得叫对对方的名字，把问题讲清楚。
- 要你写笑话、段子、故事等创作 → 下方【已联网检索的背景资料】是刚查到的真实信息，必须基于资料里的设定和梗来写；资料没有的不要乱编，更不要把不同世界观搞混（比如中古战锤的矮人 ≠ 战锤40k）。
- 是别人俩在聊 → 用平时的语气顺嘴插一句，接得上就接，接不上就顺着话题说一句自己的看法，别硬凑。
- 群友斗嘴、玩梗、互发表情包 → 不强行科普，用一句话吐槽或附和就行。
工具调用的原始 JSON、call_xxx 之类的函数名、报错堆栈都是内部信息，绝对不许出现在回复里。"""

MODE_PASSIVE = """
这条消息【没有点名找你】，是群友之间在聊。你现在只是顺嘴搭一腔：
- 最多一句话，最好不超过 20 个字，像"XX说得对""这游戏我劝退过"这种顺嘴一提。
- 绝对禁止：分段、换行、列表、标题、序号、加粗、详细解释。一个气泡说完就闭嘴。
- 别展开、别科普、别给建议清单。用一句自然的话回应就行，不需要长篇大论。
工具调用的原始 JSON、call_xxx 之类的函数名、报错堆栈都是内部信息，绝对不许出现在回复里。"""


@register("astrbot_plugin_reply_gate", "jin", "reply_gate",
          "群聊语境感知 + 发送前垃圾拦截：判断该不该说、对谁说；工具调用/报错片段不出群")
class ReplyGate(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    BOT_NAMES = ("小蜗", "蜗蜗", "蜗牛")
    CREATE_RE = re.compile(r"笑话|段子|故事")

    def _explicitly_addressed(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id() or "")
        if self_id and self_id != "qq_official":
            for comp in event.get_messages():
                if isinstance(comp, At) and str(comp.qq) == self_id:
                    return True
                if isinstance(comp, Reply) and str(getattr(comp, "sender_id", "")) == self_id:
                    return True
        # 文本名字呼叫：消息里直接喊了名字（没用 QQ @ 功能）
        # wake_prefix(含"小蜗")命中后 WakingCheckStage 会把前缀从 event.message_str 剥掉，
        # 所以这里优先用适配器写入的原文 message_obj.message_str 做名字匹配。
        _mo = getattr(event, "message_obj", None)
        _raw = (getattr(_mo, "message_str", "") or "") if _mo is not None else ""
        if _raw and any(name in _raw for name in self.BOT_NAMES):
            return True
        text = event.get_message_str() or ""
        if any(name in text for name in self.BOT_NAMES):
            return True
        return False

    def _recent_history(self, group_id: str) -> str:
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
            rows = con.execute(
                "SELECT ts, who, msg FROM ("
                " SELECT timestamp AS ts, COALESCE(NULLIF(sender_name,''),'群友') AS who,"
                "  message AS msg FROM raw_messages WHERE group_id = ?"
                " UNION ALL"
                " SELECT timestamp AS ts, '小蜗' AS who, message AS msg"
                "  FROM bot_messages WHERE group_id = ?"
                ") ORDER BY ts DESC LIMIT ?",
                (group_id, group_id, HISTORY_LIMIT),
            ).fetchall()
            con.close()
        except Exception as e:
            logger.warning(f"[reply_gate] 读取群历史失败: {e}")
            return "(无历史记录)"
        now = time.time()
        lines = []
        for ts, who, msg in reversed(rows):
            ago = int(max(0, now - int(ts)) // 60)
            text = (msg or "").strip().replace("\n", " ")[:80]
            lines.append(f"[{ago}分钟前] {who}: {text}")
        return "\n".join(lines) if lines else "(无历史记录)"

    def _sender_name(self, event: AstrMessageEvent) -> str:
        try:
            return getattr(event.message_obj.sender, "nickname", "") or "群友"
        except Exception:
            return "群友"

    def _strip_fragments(self, text: str) -> str:
        prev = None
        t = text.strip()
        while prev != t:
            prev = t
            t = FRAG_RE.sub("", t).strip()
            t = t.strip(",，;；}{ \n")
        return t

    async def _creation_background(self, event: AstrMessageEvent) -> str:
        """创作类请求（笑话/段子/故事）先联网查背景，返回要注入的文本块。"""
        try:
            _mo = getattr(event, "message_obj", None)
            q = (getattr(_mo, "message_str", "") or "") if _mo is not None else ""
            q = q or (event.get_message_str() or "")
            if not self.CREATE_RE.search(q):
                return ""
            for name in self.BOT_NAMES:
                q = q.replace(name, "")
            q = re.sub(r"^(再)?(给我|帮我)?(来|讲|说|写)(一个|一条|个)?", "", q).strip() or q.strip()
            if not q:
                return ""
            md = self.context.get_registered_star("astrbot_plugin_gemini_search")
            inst = getattr(md, "star_cls", None)
            if inst is None:
                logger.warning("[reply_gate] 未找到 gemini_search 插件实例，跳过背景检索")
                return ""
            t0 = time.time()
            result = await asyncio.wait_for(
                inst.gemini_search(event, f"{q} 的背景资料和经典梗"), timeout=30
            )
            result = (result or "").strip()[:5000]
            if not result:
                return ""
            logger.info("[reply_gate] 创作背景检索完成(%.1fs): %s -> %d字" % (time.time() - t0, q, len(result)))
            return (
                "\n【已联网检索的背景资料（必须基于这些真实信息创作，资料里没有的不要编；只许吸收内容，绝对禁止复述资料原文、来源标注、相关度等格式）】\n"
                + result
            )
        except Exception as e:
            logger.warning(f"[reply_gate] 创作背景检索失败: {e}")
            return ""

    @filter.on_llm_request()
    async def inject_context_awareness(self, event: AstrMessageEvent, req):
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            _search_block = ""
            if self._explicitly_addressed(event):
                mode = MODE_ADDRESSED
                logger.info("[reply_gate] 模式=点名回复")
                _search_block = await self._creation_background(event)
            else:
                mode = MODE_PASSIVE
                logger.info("[reply_gate] 模式=非点名插话（超简短）")
            block = AWARENESS_TMPL.format(
                sender=self._sender_name(event),
                message=(event.get_message_str() or "")[:200],
                mode_block=mode,
                skip=SKIP_TOKEN,
            )
            if _search_block:
                block += _search_block
            req.system_prompt = (req.system_prompt or "") + block
        except Exception as e:
            logger.warning(f"[reply_gate] 注入语境感知失败: {e}")

    @filter.on_llm_response()
    async def sanitize(self, event: AstrMessageEvent, resp):
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            raw = getattr(resp, "completion_text", "") or ""
            text = raw.strip()
            if text == SKIP_TOKEN or SKIP_RE.match(text):
                event.stop_event()
                logger.info(f"[reply_gate] 模型判断无需发言，已静默: {text[:24]!r}")
                return

            if not NOISE_RE.sub("", text) or NULL_RE.match(text):
                event.stop_event()
                logger.warning(f"[reply_gate] 拦截无意义回复: {text[:40]!r}")
                return
            if JUNK_RE.search(text) and (len(text) < 400 or not CJK_RE.search(text)):
                event.stop_event()
                logger.warning(f"[reply_gate] 拦截内部信息泄漏: {text[:80]!r}")
                return
            if KB_DUMP_RE.search(text):
                text = KB_DUMP_RE.split(text)[0].rstrip()
                cleaned0 = self._strip_fragments(text)
                logger.warning(f"[reply_gate] 拦截知识库检索块回显，截断后剩余 {len(cleaned0)} 字")
                text = cleaned0
                if not text.strip():
                    event.stop_event()
                    logger.warning("[reply_gate] 截断后为空，已静默")
                    return
            cleaned = self._strip_fragments(text)
            if cleaned != text.strip():
                logger.warning(f"[reply_gate] 清理尾部碎片: {text[-40:]!r} -> {cleaned[-20:]!r}")
            if not cleaned.strip():
                event.stop_event()
                logger.warning("[reply_gate] 清理后为空，已静默")
                return
            resp.completion_text = cleaned
        except Exception as e:
            logger.warning(f"[reply_gate] sanitize 失败: {e}")
