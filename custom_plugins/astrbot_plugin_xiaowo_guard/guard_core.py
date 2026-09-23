# -*- coding: utf-8 -*-
"""小蜗输出护栏 - 纯逻辑层（不依赖 astrbot，可独立单测）。

职责：
1. 识别 [pass] 沉默标记（兼容全角括号/大小写/夹带零宽字符）
2. 匹配 22 条输出过滤正则（AI 自曝/客服腔/舞台说明/推理泄漏等）
"""
import json
import re

# 零宽字符：模型想吐空时曾被逼着吐过 U+200D，匹配前先清掉
ZW_CHARS = "\u200b\u200c\u200d\ufeff"

# [pass] 沉默标记：中英文括号都收，字母间允许空白，大小写不敏感
PASS_RE = re.compile(r"[\[［【（(]\s*p\s*a\s*s\s*s\s*[\]］】）)]", re.IGNORECASE)

# 剥掉标记后清理的边缘字符（避免剩一个句号导致误判为有内容）
_EDGE = " \t\r\n。，,．.、~！!？?…—-·“”‘’\"'()（）"


def clean_text(text: str) -> str:
    for ch in ZW_CHARS:
        text = text.replace(ch, "")
    return text


def load_filters(path: str):
    """加载输出过滤器 JSON，返回 [(name, compiled_regex), ...]"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for item in data.get("filters", []):
        out.append((item["name"], re.compile(item["pattern"])))
    return out


def decide(filters, text: str):
    """判定一条 LLM 回复的处理方式。

    返回 (action, rule, new_text)：
      action = "send"  → 放行
      action = "strip" → 剥掉 [pass] 标记后放行（new_text 为剩余内容）
      action = "block" → 整条丢弃（rule 为命中的规则名）
    """
    # 自带清洗，调用方不清零宽字符也不会漏判
    text = clean_text(text)
    stripped = text.strip()

    # 1) 整条就是 [pass] → 沉默
    if PASS_RE.fullmatch(stripped):
        return "block", "pass_marker", None

    # 2) [pass] 夹在其他内容里 → 剥标记，剩余为空也算沉默
    if PASS_RE.search(stripped):
        remain = PASS_RE.sub("", stripped).strip(_EDGE).strip()
        if not remain:
            return "block", "pass_marker_mixed", None
        return "strip", "pass_marker_mixed", remain

    # 3) 输出过滤器。markdown_list_leak 对含代码块的文本豁免
    #    （小蜗 v3.1 语言规则允许技术问题用代码块和分点）
    has_code = "```" in text
    for name, pat in filters:
        if name == "markdown_list_leak" and has_code:
            continue
        if pat.search(text):
            return "block", name, None

    return "send", None, None
