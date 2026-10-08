"""提示词文本清理 —— 残句修剪 / 拼接伪影 / 重复冠词（纯函数，无任何 IO）

════════ 从 family_renderer 拆出的原因（god module 渐进拆分第二刀）════════

这一族函数只做一件事：把渲染产生的文本毛边收拾干净。
输入是字符串，输出是字符串，不读家族、不读卡、不碰 IO —— 是最纯粹的纯函数块。
抽成独立模块后，preflight（User-LOCKED 探针）与渲染主流程共用同一份实现，
不再需要从巨型文件里「顺带 import」。

★ 零行为变更保证：纯搬运，判定逻辑逐字未动；全套测试守着。
"""
from __future__ import annotations

import re
from typing import Any

# ★ 拼接伪影清洗（实测 2026-10-03）：dicts 描述本身是完整句（"采用竖向画面…"），
#   插进"采用{x_desc}…"的句式模板后叠成「采用采用竖向画面」「使用使用轻微景深」
#   「将将死亡界面」——语法破损会让生图模型困惑。渲染端统一兜底。
_LEAD_VERB_RE = re.compile(
    r"^(采用|使用|运用|保持|呈现|营造|打造|强调|突出|放置|安排|让|使得|将)[，,、]?\s*")
_DUP_VERB_RE = re.compile(r"(采用|使用|运用|保持|呈现|营造|打造|将|让){2,}")


def _strip_lead_verb(text: str) -> str:
    """剥掉 dicts 描述开头的引导动词，让它变成可嵌入的名词短语。"""
    t = str(text or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _LEAD_VERB_RE.sub("", t, count=1)
    return t.strip()


def tidy_join_artifacts(text: str) -> str:
    """规整拼接叠词：「采用采用」→「采用」、「使用使用」→「使用」。"""
    return _DUP_VERB_RE.sub(lambda m: m.group(1), str(text or ""))


_DANGLING_PATTERNS = (
    r"[：:]\s*[。；;，,、）)]*\s*$",      # 冒号后面什么都没有
    r"^\s*[，。；、）)\]】]+[\s。，；]*$",   # 整行只剩标点
    r"^[ \t]*$",                        # 空行（保留调用方的段落结构）
)
_EMPTY_BRACKETS = (
    (r"（\s*）", ""),
    (r"\(\s*\)", ""),
    (r"【\s*】", ""),
    (r"\[\s*\]", ""),
)
_AUTO_SENTINEL_RE = re.compile(r"\bauto\b", re.IGNORECASE)


def _line_has_dangling(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    return any(re.search(p, s) for p in _DANGLING_PATTERNS)


def _prune_broken_sentences(text: str, drop_auto: bool = True) -> tuple[str, int]:
    """删掉因空值而失去意义的句子，返回 (清洗后文本, 删掉的行数)

    最后一道保险丝：即便上游所有解析都失手，也绝不把 "auto" 发给模型。
    """
    if not text:
        return "", 0
    kept: list[str] = []
    dropped = 0
    for raw_line in text.splitlines():
        line = raw_line
        # ★ 先看「原文非空、清洗后变空」—— 这是最需要被看见的一类：
        #   占位符解析成了空串，整句凭空消失。旧实现按清洗后的 line 判断，
        #   空行当然不算 dropped，于是这类消失的计数恒为 0，
        #   诊断字段静默骗人（实测 surreal_collage 的 {dynamic_forbid} 行
        #   被删掉了，而 dropped=0）。
        was_meaningful = bool(raw_line.strip())
        for pat, rep in _EMPTY_BRACKETS:
            line = re.sub(pat, rep, line)
        # 保险丝：整行含裸 auto → 直接弃用该句
        if drop_auto and _AUTO_SENTINEL_RE.search(line):
            # 例外：auto 是合法英文单词的一部分（如 automatic）不误伤
            if re.search(r"\bauto\b(?!matic)", line, re.IGNORECASE):
                dropped += 1
                continue
        if _line_has_dangling(line):
            if was_meaningful:
                dropped += 1
            continue
        kept.append(line.rstrip())

    out = "\n".join(kept)
    out = _collapse_duplicate_articles(out)
    # 收尾：连续空行压成一个、重复空格与重复标点收敛
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"([。；，、])\1+", r"\1", out)
    return out.strip(), dropped


def _collapse_duplicate_articles(text: str) -> str:
    """修掉 "Keep the the main subject" 这类重复冠词

    主语文案的兜底必须自带冠词（模板写 "an oversized version of {subject}"），
    但另一些模板写 "Keep the {subject}" —— 同一个兜底值塞进两种句式，
    必然有一处重复。与其纠结让哪种句式迁就哪种，不如统一在这一步收干净。
    """
    text = re.sub(r"\b(the|a|an)\s+\1\b", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(of|with|Keep|distort)\s+(the|a|an)\s+\2\b", r"\1 \2", text,
                  flags=re.IGNORECASE)
    return text




def _join(items: Any, sep: str = "、") -> str:
    if items is None:
        return ""
    if isinstance(items, str):
        return items
    if isinstance(items, bool):
        return str(items)
    if isinstance(items, (int, float)):
        return str(items)
    if isinstance(items, (list, tuple, set)):
        return sep.join(str(x) for x in items)
    return str(items)


# 高饱和候选色（当原图色板无法计算饱和度时兜底）
