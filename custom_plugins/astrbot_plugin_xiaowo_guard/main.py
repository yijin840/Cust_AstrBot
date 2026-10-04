# -*- coding: utf-8 -*-
"""小蜗输出护栏。

两层拦截，均在发送前：
1. on_llm_response：LLM 回复整体为 [pass]（沉默标记）→ 标记拦截；
   命中 22 条输出过滤正则（AI 自曝/客服腔/舞台说明/推理泄漏）→ 标记拦截；
   [pass] 夹在其他内容 → 剥掉标记放行剩余部分。
2. on_decorating_result（priority=999，先于其他装饰插件执行）：
   命中拦截 → event.stop_event()，调度器保证 Send 阶段不会执行。

设计取舍：
- 拦截时不动 LLMResponse 本体，避免与空输出重试链路（EmptyModelOutputError，
  6 模型 × 3 次）产生交互；[pass] 留在历史里反而是"沉默出口"的合法样本。
- 仅挂 LLM 响应钩子，不影响指令和普通命令。
- markdown_list_leak 对含代码块的文本豁免（v3.1 允许技术问题用分点）。
"""
import os

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .guard_core import clean_text, decide, load_filters

PLUGIN_ID = "astrbot_plugin_xiaowo_guard"


@register(PLUGIN_ID, "jin", "小蜗输出护栏：拦截 [pass] 沉默标记 + 发送前 22 条输出过滤正则", "1.0.0")
class XiaowoGuard(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._pending = {}  # id(event) -> rule（event 不支持动态属性时的兜底）
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output_filters.json")
        try:
            self._filters = load_filters(path)
            logger.info(f"[{PLUGIN_ID}] 已加载 {len(self._filters)} 条输出过滤器")
        except Exception as e:
            self._filters = []
            logger.error(f"[{PLUGIN_ID}] 加载 output_filters.json 失败，过滤器层不生效: {e}")

    def _mark_block(self, event: AstrMessageEvent, rule: str) -> None:
        try:
            event._xg_guard_block = rule
        except Exception:
            self._pending[id(event)] = rule

    def _take_block(self, event: AstrMessageEvent):
        rule = getattr(event, "_xg_guard_block", None)
        if rule is not None:
            self._pending.pop(id(event), None)
            return rule
        return self._pending.pop(id(event), None)

    @filter.on_llm_response()
    async def guard_llm_response(self, event: AstrMessageEvent, resp):
        try:
            if getattr(resp, "is_chunk", False):
                return
            if getattr(resp, "role", "") in ("err", "tool"):
                return
            raw = resp.completion_text or ""
            text = clean_text(raw)
            action, rule, new_text = decide(self._filters, text)
            if action == "block":
                logger.info(
                    f"[{PLUGIN_ID}] 拦截 rule={rule} "
                    f"session={event.unified_msg_origin} 原文={text[:60]!r}"
                )
                self._mark_block(event, rule)
            elif action == "strip":
                logger.info(
                    f"[{PLUGIN_ID}] 剥离 [pass] 标记 rule={rule} 剩余={new_text[:40]!r}"
                )
                resp.completion_text = new_text
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 处理异常(放行不拦): {e}")

    @filter.on_decorating_result(priority=999)
    async def guard_decorate(self, event: AstrMessageEvent):
        rule = self._take_block(event)
        if rule is None:
            return
        logger.info(f"[{PLUGIN_ID}] 阻止发送 rule={rule}")
        # 🔴 2026-10-04 关键修复：**不能只靠 stop_event**。
        #
        # 本版本（v4.28.1）的 scheduler 对「含 yield 语句的 stage」走洋葱分支：
        #   async for _ in agen:  is_stopped? -> 递归后续 stage
        # 而 `else` 分支里那句「await coroutine; if is_stopped(): break」才是真正
        # 阻止后续 stage 的地方。ResultDecorateStage.process 因为
        # content_safe_check_reply=True（content_safety.also_use_in_response=true）
        # 而**含有 yield 语句**，于是永远走洋葱分支 —— 它的 stop_event 之后，
        # RespondStage 照样被执行。
        # 实测（2026-10-04 23:27）：命中 fake_sub_url、stop_event 也调了，
        # 但 respond.stage:212 依然打出「Prepare to send - <假地址>」= 消息发了出去。
        #
        # 所以这里必须**直接清空结果链**：RespondStage 开头就有
        #   if not result.chain and ...: return
        # 空链会被它自己挡下，不发送、也不触发 after_message_sent。
        try:
            res = event.get_result()
            if res is not None and res.chain:
                res.chain = []
                logger.info(f"[{PLUGIN_ID}] 已清空结果链（rule={rule}）")
        except Exception as e:
            logger.error(f"[{PLUGIN_ID}] 清空结果链失败，仍尝试 stop_event: {e}")
        event.stop_event()

