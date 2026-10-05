"""参数与槽位解析 —— 类型强制 / 默认值填充 / auto 槽位兜底 / 卡片信号助手

════════ 从 family_renderer 拆出的原因（god module 渐进拆分第四刀）════════

这一族回答一个问题：「HTTP 传来的字符串参数 + 提炼卡 → 渲染取值表里的实际取值」。
类型强制（coerce_params）、默认值（fill_defaults）、auto 兜底（resolve_auto_slots）、
卡片信号助手（palette/anchors/subject）全部在此，与渲染主流程只通过
render_family 的三行调用相连。

★ 依赖纪律：本模块**只准**依赖 prompt_cleanup（纯文本工具），不得反向依赖
  family_renderer —— 渲染器 import 本模块，反向就是循环导入。
  AUTO_TOKEN 是渲染器与解析器共享的契约常量，定义在此、渲染器再导出。

★ 零行为变更保证：纯搬运 + 导入改写；test_renderer/test_preflight 全套守着。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from services.prompt_cleanup import _join

AUTO_TOKEN = "auto"      # 契约：凡是显式要求"由上游生成"的槽位，默认值写这个

_TRUE_TOKENS = {"true", "1", "yes", "on", "是", "y", "t"}


def _coerce_value(value: Any, ptype: str) -> Any:
    if value is None:
        return None
    if ptype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in _TRUE_TOKENS
    if ptype == "number":
        if isinstance(value, bool):
            return float(value)
        return float(value)
    if ptype == "list":
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, str):
            parts = [x.strip() for x in value.split(",") if x.strip()]
            return parts or []
        return [str(value)]
    return str(value)


def coerce_params(family: dict, raw: dict | None) -> tuple[dict, list[str]]:
    """按家族声明的 params 做类型强制 + 剔除未知参数

    返回 (规范化后的参数, 提示列表)
    - 未知参数一律丢弃：HTTP 传来的奇怪键不得参与占位符替换
    - 非法枚举值不抛错（由调用方决定 400 还是降级），只记录并返回默认前的原值
    """
    specs = (family.get("params") or {})
    out: dict[str, Any] = {}
    notes: list[str] = []

    for k, v in (raw or {}).items():
        if k not in specs:
            notes.append(f"未知参数 {k} 已忽略（未在家族 params 中声明）")
            continue
        spec = specs[k] or {}
        ptype = str(spec.get("type", "string"))
        try:
            cv = _coerce_value(v, ptype)
        except (TypeError, ValueError):
            notes.append(f"参数 {k} 取值 {v!r} 不合法，回退默认值")
            continue
        if ptype == "enum":
            options = spec.get("options") or []
            if options and str(cv) not in [str(o) for o in options]:
                notes.append(
                    f"参数 {k}={cv!r} 不在选项 {options} 内，回退默认值"
                )
                continue
        out[k] = cv

    return out, notes


def fill_defaults(family: dict, params: dict, skip: set | None = None) -> dict:
    """补齐缺失的参数默认值

    skip：USER-LOCKED 的参数名 —— 即使用户留空也不补默认值，
    让 missing_required 如实报告，而不是被默认值悄悄盖住用户的选择。
    """
    params = dict(params)
    skip = set(skip or ())
    for name, spec in (family.get("params") or {}).items():
        if name in skip:
            continue
        cur = params.get(name)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            default = spec.get("default")
            if default is not None and default != "":
                params[name] = _coerce_value(default, str(spec.get("type", "string")))
            elif spec.get("required"):
                params[name] = ""      # 必填但没给 → 留空，让 missing_required 捕获
    return params


# ─────────────── auto 槽位兜底（本轮核心修复）────────────────────

def _looks_chinese(family: dict) -> bool:
    """家族正文是中文还是英文 —— 决定兜底文案的语言

    surreal_collage 整篇英文，塞进「可以是手写消息、铭牌、象形符号……」
    这种中文会让模型左右为难。中英混排的提示词是提示词工程的大忌之一。
    """
    text = "".join(str(v) for v in (family.get("segments") or {}).values())
    for val in family.values():
        if isinstance(val, dict):
            text += "".join(str(v) for v in val.values() if isinstance(v, str))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    letters = len(re.findall(r"[A-Za-z]", text))
    return cjk >= letters


def _stable_pick(pool: tuple[str, ...], seed: str) -> str:
    """从候选池里做**确定性**选择

    为什么不用 random：同一张照片两次渲染必须得到同一个提示词，
    否则 A/B 对比实验（M6 基准）就失去意义 —— 你无法判断差异来自风格还是随机。
    """
    if not pool:
        return ""
    if len(pool) == 1:
        return pool[0]
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return pool[int(h[:8], 16) % len(pool)]


_INTERACTION_CN = ("撑住", "托起", "拉开", "推开", "拨动", "提起", "修补", "展开", "搬动", "翻转")
_INTERACTION_EN = (
    "holding it up", "pulling it open", "pushing it aside",
    "lifting it", "repairing it", "unfolding it", "leaning against it",
)
_OBJECT_FORM_CN = (
    "一块能被握住的形状", "一块轻便可挪动的薄片", "一个可以被托起的立体结构",
)
_OBJECT_FORM_EN = (
    "a shape that can be held in the hand",
    "a thin slab that can be slid aside",
    "a solid form that can be lifted",
)
_TEXTURE_CN = ("质感：自然纹理与手绘笔触", "质感：平面印刷感与细腻纹理")
_TEXTURE_EN = ("matte textures and subtle grain", "flat printed ink texture")


def _palette_names(card: dict, limit: int = 3) -> list[str]:
    palette = card.get("palette") or []
    names: list[str] = []
    for p in palette[:limit]:
        if isinstance(p, dict):
            nm = p.get("name") or p.get("hex")
            if nm:
                names.append(str(nm))
        elif p:
            names.append(str(p))
    return names


def _anchor_descs(card: dict, limit: int = 2) -> list[str]:
    anchors = card.get("anchors") or []
    return [str(a.get("desc")) for a in anchors[:limit] if isinstance(a, dict) and a.get("desc")]


def _raw_subject_name(card: dict) -> str:
    """提炼卡里有没有真的给出主体名（没有时用 None 语义让调用方换兜底话术）"""
    subject = card.get("subject")
    name = ""
    if isinstance(subject, dict):
        name = str(subject.get("name") or "").strip()
    elif isinstance(subject, str):
        name = subject.strip()
    if not name:
        name = str(card.get("subject_name") or "").strip()
    return "" if name.lower() == AUTO_TOKEN else name


def _subject_text(card: dict, zh: bool) -> str:
    name = _raw_subject_name(card)
    if name:
        return name
    # 没有提炼卡时的兜底：**按正文语言给**，避免 "Keep the 画面核心主体 recognizable" 这种中英混排。
    # ⚠️ 英文不带冠词 "the"：模板原文通常写成 "Keep the {subject} ..."，
    #    再来一个 the 就会变成 "Keep the the main subject"。
    return "画面核心主体" if zh else "the main subject"


def resolve_auto_slots(family: dict, params: dict, card: dict,
                       skip: set | None = None) -> tuple[dict, list[str]]:
    """把所有值为 'auto'（或空）的上游槽位替换成真实可用的文案

    这是本轮最重要的修复。原先 object_form / interaction / giant_element /
    flat_shapes / small_elements / inner_world 的 "auto" 会原样进入提示词。
    哭笑不得的是：模型真的会试图画一个叫 "auto" 的东西。

    优先级：提炼卡派生 > 确定性候选池 > 空（由后续残句清理删掉整句）
    skip：USER-LOCKED 的参数名 —— 即使值为 'auto' 也不自动解析，
    保留用户明确的选择（或让其进入 missing/unresolved 的如实报告）。
    """
    skip = set(skip or ())
    specs = family.get("params") or {}
    zh = _looks_chinese(family)
    params = dict(params)
    applied: list[str] = []

    subject = _subject_text(card, zh)
    anchors = _anchor_descs(card)
    palette = _palette_names(card)
    seed = "|".join([family.get("id", ""), subject] + anchors) or family.get("id", "")

    has_real_subject = bool(_raw_subject_name(card))

    for name, spec in specs.items():
        if name in skip:
            continue          # USER-LOCKED：用户显式设置过，auto 兜底不许碰
        val = params.get(name)
        # ⚠️ list 型参数的 default 写 "auto" 时，会被 _coerce_value 包成 ["auto"]。
        #    直接比字符串判等会漏判（surreal_collage.flat_shapes 就这么躲过去了），
        #    所以先归一化成标量。
        scalar = _join(val) if isinstance(val, (list, tuple)) else (
            "" if val is None else str(val)
        )
        is_auto = (val is None) or (scalar.strip().lower() == AUTO_TOKEN)
        if not is_auto:
            continue
        source = str(spec.get("source") or "")
        replacement = ""

        if name == "crossing_color":
            continue                                   # 由 _build_derived 专门处理

        if name == "materialize_anchor":
            replacement = anchors[0] if anchors else (
                f"{subject}最有辨识度的视觉结构" if (zh and has_real_subject)
                else "画面中最有辨识度的视觉结构" if zh
                else f"the most recognizable structure of {subject}" if has_real_subject
                else "the most recognizable structure in the photo"
            )

        elif name == "object_form":
            replacement = _stable_pick(_OBJECT_FORM_CN if zh else _OBJECT_FORM_EN, seed)
            if anchors:                                # 贴上锚点让它源自原图
                replacement = (f"由「{anchors[0]}」变形而来的" if zh else
                               f"a form derived from the {anchors[0]} of ") + replacement

        elif name == "interaction":
            replacement = _stable_pick(_INTERACTION_CN if zh else _INTERACTION_EN, seed)

        elif name == "inner_world":
            school = str(params.get("school") or "").strip()
            major = str(params.get("major") or "").strip()
            direction = str(params.get("direction") or "").strip()
            bits = [x for x in (major, school) if x]
            if zh:
                replacement = "、".join(bits) if bits else "知识与创造"
                if direction:
                    replacement += f"，指向「{direction}」"
            else:
                replacement = " and ".join(bits) if bits else "knowledge and creation"
                if direction:
                    replacement += f", reaching toward {direction}"

        elif name == "giant_element":
            base = anchors[0] if anchors else subject
            replacement = (
                f"一个由「{base}」放大而成的巨大意象" if zh
                else f"an oversized version of {base}, grown out of the photo itself"
            )

        elif name == "small_elements":
            base = anchors[-1] if anchors else subject
            replacement = (
                f"由「{base}」碎片化而来的细小元素" if zh
                else f"small fragments derived from {base}"
            )

        elif name == "flat_shapes":
            if palette:
                cols = "、".join(palette[:2]) if zh else " and ".join(palette[:2])
                replacement = (
                    f"两到三块大面积平涂色形，取色于{cols}" if zh
                    else f"two or three large flat shapes in {cols}"
                )
            else:
                replacement = (
                    "两到三块大面积平涂色形，颜色克制" if zh
                    else "two or three large flat shapes in restrained colors"
                )

        elif name == "subject":
            replacement = subject

        elif source in ("from_photo", "llm_generated"):
            # 未知的新槽位：给一个语言匹配的通用兜底，至少不是 "auto"
            replacement = _stable_pick(_TEXTURE_CN if zh else _TEXTURE_EN, seed)

        if replacement:
            params[name] = replacement
            applied.append(name)

    return params, applied


# ────────────────────── 派生量计算（规约 §7.2）──────────────────────

