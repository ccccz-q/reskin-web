"""维度与反推 forbid —— 作用域前缀 / 维度裁剪 / 动态禁令 / allow_change 推导

════════ 从 family_renderer 拆出的原因（god module 渐进拆分第五刀）════════

这一族回答：「这次参数下，哪些维度不许动、哪些必须明令消失」。
作用域前缀（_SCOPE_PREFIX）把禁令钉到画面区域，维度表（ALL_DIMENSIONS）按
allow_change 反推该放行的维度，build_dynamic_forbid 据此拼出动态禁令段。

★ 依赖纪律：只依赖 param_resolver（AUTO_TOKEN / _looks_chinese）与 re，
  不得反向依赖 family_renderer。
★ 零行为变更保证：纯搬运；test_renderer 的反推 forbid 两道滤网用例守着。
"""
from __future__ import annotations

import re

from services.param_resolver import AUTO_TOKEN, _looks_chinese

_SCOPE_PREFIX = {
    "whole": "",
    "upper": "【上半实景区】",
    "real_region": "【撕纸窗口内的实景部分】",
    "subject": "【主体本身】",
    "face": "【人脸与身份特征】",
}

# 英文家族用英文作用域标记。
# 理由：作用域标记是给模型读的"约束适用区域"，必须和它所标记的约束同语言，
# 否则会产出「【主体本身】main subject must remain complete」这种混排。
# 中文家族维持原样（既是既定约定，也是模型母语）。
_SCOPE_PREFIX_EN = {
    "whole": "",
    "upper": "[upper photo region] ",
    "real_region": "[real region inside the torn window] ",
    "subject": "[the subject itself] ",
    "face": "[face and identity] ",
}

# detect_conflicts 用它判断"作用域标记在不在"，中英文都要认
SCOPE_MARKER_RE = re.compile(r"【|\[(?:upper photo region|real region|the subject|face and identity)")

# ★ 维度标签按「这条约束到底在保护什么」来定，不能一刀切。
#   教训：最初把 subject / risk_notes 都归到 detail_density，
#   结果 split_poster 的 allow_change 含 detail_density，
#   导致「主体必须可辨认」被一并过滤掉 —— 保留约束 0 条，保真彻底失守。
#
#   identity      = 主体身份、景物是否还在（任何风格都不允许改，永远不过滤）
#   figure_add    = 人物数量与动作
#   detail_density= 细节密度、地平线、纹理（艺术化重绘类允许改）
#   color / light / background / material / text_add / edge / object_form
IDENTITY = "identity"

# 全部允许的艺术维度 —— 模板工坊需要它来校验 allow_change。
# 手写 YAML 时这个值由人保证正确性，工坊把它交给模型之后就必须有清单。
ALL_DIMENSIONS = frozenset({
    "identity", "figure_add", "detail_density", "color", "light",
    "background", "material", "text_add", "edge", "object_form",
})

# 合法取值集合。
# ★ 这两个集合以前只存在于 YAML 注释和人的脑子里 —— 没有任何代码检查过。
#   手写时代无所谓，模板工坊让模型来填之后就成了必查项：
#   layout 决定画面结构，给错值整张图都错，而且不会报错。
#   集合直接由下面的 _SCOPE_PREFIX 推导，避免再出现第三份真源。
SCOPES = frozenset(_SCOPE_PREFIX.keys())
LAYOUTS = frozenset({"full", "split_v", "silhouette", "tear"})


def _clear_dimensions() -> tuple[str, ...]:
    return (IDENTITY,)


def build_dynamic_forbid(
    family: dict, params: dict, card: dict, allow_change: list[str]
) -> str:
    """从提炼卡反推 forbid，并过两道滤网：

    过滤一 forbid_scope：只在家族声明的区域内生效
    过滤二 allow_change：家族/风格本来就要改的维度直接跳过

    ⚠️ identity 类约束（主体身份、景物是否还在）**永不参与过滤二**，
    因为它保护的是"这还是不是同一张照片"，任何风格都没有权力改。
    """
    scope = family.get("forbid_scope", "whole")
    zh = _looks_chinese(family)
    prefix = (_SCOPE_PREFIX if zh else _SCOPE_PREFIX_EN).get(scope, "")
    lines: list[str] = []
    notes: list[str] = []   # 诊断用：记录被过滤掉的条目

    def add(dim: str, text: str) -> None:
        if dim != IDENTITY and dim in allow_change:   # 过滤二（identity 豁免）
            notes.append(f"[过滤] {dim}: {text[:24]}")
            return
        lines.append(f"{prefix}{text}")

    # ── identity 类：永不过滤 ──────────────────────────
    # 注意卡片结构：subject 既可能是 {"name": "..."}，也可能是裸字符串，两种都要吃得下。
    subject = card.get("subject")
    subject_name = ""
    if isinstance(subject, dict):
        subject_name = str(subject.get("name") or "").strip()
    elif isinstance(subject, str):
        subject_name = subject.strip()
    if not subject_name:
        subject_name = str(card.get("subject_name") or "").strip()
    if subject_name and subject_name.lower() != AUTO_TOKEN:
        if zh:
            add(IDENTITY, f"{subject_name}必须完整、清晰、可辨认，不得消失、被裁碎或沦为装饰")
        else:
            add(IDENTITY, f"{subject_name} must remain complete, clearly recognizable, "
                          f"never removed, cropped away or reduced to decoration")

    for note in card.get("risk_notes") or []:
        add(IDENTITY, str(note))

    anchors = card.get("anchors") or []
    for a in anchors:
        if isinstance(a, dict) and a.get("selected") and a.get("must_keep") and a.get("desc"):
            add(IDENTITY, f"{a['desc']}必须保留、清晰可辨，不得被抽象化抹除")

    # ── 非 identity 类：受白名单约束 ────────────────────
    if card.get("has_person"):
        n = card.get("person_count")
        n_txt = f"（当前 {n} 人）" if n else ""
        if zh:
            add("figure_add", f"不得改变人物数量{n_txt}、不得改变人物动作与服装")
        else:
            add("figure_add", f"never change the number of people{n_txt}, "
                              f"their poses or their clothing")

    comp = card.get("composition") or {}
    if comp.get("horizon"):
        if zh:
            add("detail_density", f"不得改变地平线高度（{comp['horizon']}）与画面透视")
        else:
            add("detail_density", f"never change the horizon height ({comp['horizon']}) "
                                  f"or the perspective")

    if card.get("palette"):
        if zh:
            add("color", "不得把画面主色改为其他色系，保持原有饱和度特征")
        else:
            add("color", "never switch the dominant palette to another hue family; "
                         "keep its original saturation character")

    light = card.get("light") or {}
    if light.get("dir"):
        if zh:
            add("light", f"不得改变光源方向（{light['dir']}）与光影关系")
        else:
            add("light", f"never change the light direction ({light['dir']}) "
                         f"or the light-shadow relationship")

    # 无锚点时的降级提示（规约 §3「找不到可用锚点」）
    if family.get("id") == "second_world" and not any(
        a.get("materializable") for a in (card.get("anchors") or [])
    ):
        lines.append("照片中没有明显可物化的结构，请优先从主体本身寻找可触碰的形态")

    sep = "；" if zh else "; "
    tail = "。" if zh else ""          # 中文句表以句号收尾
    joined = sep.join(lines)
    return joined + (tail if joined else "")


# ─────────────────── allow_change / 冲突检测 ───────────────────

def resolve_allow_change(family: dict, params: dict) -> list[str]:
    """解析 allow_change —— 支持静态与动态两种形态（规约 §5.-1）"""
    if family.get("allow_change_mode") == "dynamic":
        table = family.get("style_allow_change") or {}
        style = params.get("style")
        values = table.get(style)
        if values is None and isinstance(table, dict) and table:
            # style 未给时用第一个风格的白名单，避免退化成"全不过滤"
            values = next(iter(table.values()))
        return list(values or [])
    return list(family.get("allow_change") or [])


# 冲突检测词表（规约 §8）
