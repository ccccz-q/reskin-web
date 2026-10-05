"""家族渲染器 —— 把 family YAML + 参数 + 提炼卡 渲染成三段式提示词

严格实现 `提示词框架/family_schema_v1.md` 的规约，特别是 §7.1.1 的三个实现坑：

    ① 占位符可能带格式串：{illustration_ratio:.0%}
       → 正则必须吃掉冒号后的格式部分
    ② 占位符可能含点号：{subject.name}
       → 不能用 str.format()，必须用正则替换
    ③ block 展开后的内容里还可能嵌占位符：{text_block} 里有 {text_lang_desc}
       → 必须做多轮渲染

════════ 2026-09-26 重写：本轮修掉的两类「静默降级」════════

这两类都不会报错、不抛异常，只是**悄悄产出劣质提示词**——
比崩溃更危险，因为测试全绿。

**类型一：auto 泄漏。**
标准库里 source: llm_generated / from_photo 的槽位（object_form / interaction /
inner_world / giant_element / flat_shapes / small_elements / subject / crossing_color）
本意是「由上游 LLM 或提炼卡填入」。但上游没填时，默认值 "auto" 被原样发给模型。
实测渲染结果：

    One impossible giant element: auto.
    他们必须真的与这块摄影物体互动（auto），而不是站在旁边摆姿势。

坑点清单 #5 只修了 crossing_color 一个，其余 7 个漏网。
→ 现在统一由 resolve_auto_slots() 兜底，且最后有一道「auto 绝不外发」的保险丝。

**类型二：名字对不上导致的空值。**
surreal_collage 声明的参数叫 flat_shapes，段里引用的占位符却是 {flat_shapes_desc}，
而它没有 dicts.flat_shapes 来做「枚举值 → 文案」的映射。
旧逻辑里 str.format 找不到就填空串 → 占位符确实"消失了"，于是 unresolved 检测
也认为没问题（因为它只检测"占位符是否还在文本里"）。实测渲染结果：

    The background is replaced by huge flat matte color shapes: .   ← 内容整段消失
     in graduated sizes following an arc.                          ← 句首残留空格

→ 现在：① 把 X_desc 自动桥接到参数 X 的实义值；② 渲染后清理因空值产生的残句；
  ③ 把「解析结果为空」也计入诊断，不再是静默通过。

依赖：**零本地依赖**（只用 yaml）。刻意如此设计——校验器、测试脚本、渲染器
共用同一份占位符解析逻辑，任何一处都能独立 import 而不牵扯 FastAPI / config。
反直觉但重要：**加载/校验逻辑多处重复实现，本身就是 bug 温床**（见 sync_families.py 的教训）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Callable

import yaml

logger = logging.getLogger(__name__)

TEMPLATES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "templates")
)
FAMILIES_DIR = os.path.join(TEMPLATES_DIR, "families")

# ⚠️ 坑① 坑②：必须允许"点号"和"冒号后的格式串"
# ⚠️ 坑④（实测 2026-10-01）：提炼产物会把占位符写成**双花括号** {{subject.name}}
#   （模型在 YAML 语境里"多写一层括号"防转义）。旧正则只认单括号 → 内层先被替换，
#   外层残留成 {画面核心主体} → strict 渲染报未解析 → 家族装了却用不了
#   （voxel_path_reflection 实例）。这里把 {{x}} 与 {x} 一视同仁：
#   渲染替换、unresolved 检测、占位符清洗三条路径同时受益。
PLACEHOLDER_RE = re.compile(r"\{\{?([\w.]+)(?::([^{}]*))?\}\}?")

# ⚠️ 坑③：block 展开后还会引入新占位符，需要多轮渲染才会收敛
MAX_PASSES = 3


# 单个「用户可控值」的最大字符数。超过就截断 —— 防止某个参数被塞进长篇大论
# 把 prompt 的 token 成本悄悄放大（成本治理的一部分）。
MAX_PARAM_CHARS = int(os.getenv("MAX_PARAM_CHARS", "300"))

# ★ 载荷分级裁剪（造梦师「Prompt Overload」规则的移植）
#
# 造梦师的原文判据：
#   「若提示词过载，先删低价值支撑语法、冗余形容词、低价值排除项。
#     不许牺牲 USER-LOCKED 事实、媒介身份、已观察到的机制、已选中的 Active Core Rules。」
#
# 落地成**三级上限**，从"不可牺牲"到"可牺牲"：
MAX_EXTRA_PROMPT_CHARS = int(os.getenv("MAX_EXTRA_PROMPT_CHARS", "1200"))
# 防滥用硬顶：超过大概率是误粘贴整篇文档。用户自定义提示词原则上**不截断**，
# 只在这道硬顶处截断且必须醒目告知（见 prompt_builder._apply_extra_prompt）。
MAX_EXTRA_PROMPT_HARD_CAP = int(os.getenv("MAX_EXTRA_PROMPT_HARD_CAP", "6000"))
MAX_RULE_CHARS = 240

# 永不截断的取值键 —— 保真与禁令的载体，截掉等于"没写"。
#
# ★ 为什么必须**显式**声明：
#   现在的 render_family 里，限长只作用于 `user_keys`，而 hard_forbid_joined /
#   dynamic_forbid 恰好不在其中，所以它们天然安全。但这是**隐式机制** ——
#   哪天有人把这两个键加进 user_keys（比如想让它们可参数化），
#   整套保真约束就会被静默截断，而所有测试仍然全绿（没有一条断言 forbid 不截断）。
#   把它写成常量并显式判断，是把"碰巧安全"变成"结构上安全"。
NEVER_TRUNCATE_KEYS = frozenset({
    "hard_forbid_joined",     # 家族硬禁令
    "dynamic_forbid",         # 反推出来的动态禁令
})

# 分句边界：优雅截断时优先在这些字符处落刀，避免把一句话砍成半截
_CLAUSE_BOUNDARY = "。！？；，、,.!?;\n"

# 优雅截断的最小保留比例：找不到边界时至少留这么多，避免截出半句话
_GRACEFUL_FLOOR = 0.6

# ★ 三段式负载阈值（唯一事实源，preflight 也从这里引入）
_MAX_TOTAL_CHARS = 1800          # 三段总字数上限
_MAX_FORBID_CLAUSES = 15         # forbid 分句条数上限


class FamilyRenderError(Exception):
    """渲染失败（字段缺失、占位符无法解析等）"""


# ────────────────────────────── 加载 ──────────────────────────────

# 家族 YAML 必需字段
FAMILY_REQUIRED = ("id", "layout", "forbid_scope", "hard_forbid", "params", "segments")
SEGMENT_NAMES = ("preserve", "creative", "forbid")

# 由 _build_derived() / 渲染主流程无条件生成的占位符。
#  validate_family 检查 "{X_desc} 有没有人提供" 时必须放行这些，
#  否则 side_desc / palette_keep / anchor_count 这类合法派生量会被误判成悬空。
#  ⚠️ 这张表必须和 _build_derived 的实际返回键保持一致 ——
#     由 assert_derived_keys_complete() 在测试里做双向校验，防止再次漂移。
DERIVED_KEYS = frozenset({
    "subject", "subject.name",
    "illustration_ratio", "photo_ratio",
    "anchor_count", "anchor_desc", "anchor_desc_short", "materialize_anchor",
    "detail_removal", "crossing_color",
    "layers_desc", "palette_desc", "palette_keep",
    "text_lang", "text_lang_desc", "text_align", "text_align_desc",
    "split_ratio", "fixed_palette",
    "direction", "direction_desc", "school", "major", "gravity_center",
    "side_desc",
    # 模板工坊 / Riso 家族引入的参数派生量
    # （dicts.<param> 存在时，<param>_desc 由渲染器自动派生，这里登记以免误判悬空）
    "palette_mode_desc", "dot_size_desc", "coverage_desc",
    "detail_fidelity_desc", "drum_banding_desc", "misregistration_desc",
    "paper_desc", "ink_count_desc",
    "dynamic_forbid", "hard_forbid_joined",
    "style_block", "text_block", "edge_block", "swatches_block",
    "figures_block", "graffiti_block",
})


def assert_derived_keys_complete() -> list[str]:
    """自检：DERIVED_KEYS 与 _build_derived 的实际产出是否一致

    返回缺失/多余的键列表，空列表表示一致。测试里调用它来锁住这张表。
    """
    actual = set(_build_derived({}, {}, {}).keys())
    missing = sorted(actual - DERIVED_KEYS)
    extra = sorted(DERIVED_KEYS - actual - {
        "dynamic_forbid", "hard_forbid_joined",
        "style_block", "text_block", "edge_block",
        "swatches_block", "figures_block", "graffiti_block",
    })
    return [f"DERIVED_KEYS 缺少实际产出的键: {missing}",
            f"DERIVED_KEYS 含无效键: {extra}"] if (missing or extra) else []


def read_family_yaml(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"（第 {mark.line + 1} 行）" if mark else ""
        raise FamilyRenderError(f"YAML 语法错误 {os.path.basename(path)}{where}: {e}") from e
    if not isinstance(data, dict):
        raise FamilyRenderError(f"{os.path.basename(path)} 顶层不是映射")
    return data


def validate_family(doc: dict, source: str = "?") -> None:
    """加载即校验 —— 让缺口在加载时暴露，而不是等用户点「生成」

    校验器和运行时必须共用这一份规则，否则会出现「6/6 校验通过、运行时行为不符」。
    """
    missing = [k for k in FAMILY_REQUIRED if k not in doc]
    if missing:
        raise FamilyRenderError(f"家族 {source} 缺必需字段: {missing}")

    # ★ 枚举字段的**取值**必须校验（模板工坊暴露出来的缺口）
    # ----------------------------------------------------------
    # 手写 YAML 的时代，这些字段由懂这套结构的人填，值基本不会错，
    # 所以只查「有没有」就够了。
    # 模板工坊把产出方换成了模型 —— 它会编出 `layout: poster` 这种不在集合里的值。
    # 不校验的话：草稿能通过校验、能被安装，然后渲染出意义不明的东西，
    # 用户根本不知道哪里错了（layout 决定画面结构，错了整张图都错）。
    layout = doc.get("layout")
    if str(layout) not in LAYOUTS:
        raise FamilyRenderError(
            f"家族 {source} 的 layout={layout!r} 不在允许值内，可选 {sorted(LAYOUTS)}"
        )

    scope = doc.get("forbid_scope")
    if str(scope) not in SCOPES:
        raise FamilyRenderError(
            f"家族 {source} 的 forbid_scope={scope!r} 不在允许值内，可选 {sorted(SCOPES)}"
        )

    for key in ("hard_forbid", "suitable"):
        val = doc.get(key)
        if val is not None and not isinstance(val, list):
            raise FamilyRenderError(
                f"家族 {source} 的 {key} 必须是列表，实际是 {type(val).__name__}"
            )
    hf = doc.get("hard_forbid") or []
    if len(hf) < 4:
        raise FamilyRenderError(
            f"家族 {source} 的 hard_forbid 至少 4 条（保真底线），实际 {len(hf)} 条"
        )

    allow = doc.get("allow_change")
    if allow is not None:
        unknown_dims = [d for d in allow if str(d) not in ALL_DIMENSIONS]
        if unknown_dims:
            raise FamilyRenderError(
                f"家族 {source} 的 allow_change 含未知维度 {unknown_dims}，"
                f"可选 {sorted(ALL_DIMENSIONS)}"
            )

    segments = doc.get("segments") or {}
    if not isinstance(segments, dict):
        raise FamilyRenderError(f"家族 {source} 的 segments 必须是映射")
    for name in SEGMENT_NAMES:
        if name not in segments:
            raise FamilyRenderError(f"家族 {source} 的 segments 缺少 {name} 段")

    if doc.get("allow_change_mode") == "dynamic":
        if not isinstance(doc.get("style_allow_change"), dict):
            raise FamilyRenderError(
                f"家族 {source} 声明 allow_change_mode=dynamic 却没有 style_allow_change"
            )
    elif not isinstance(doc.get("allow_change"), list):
        raise FamilyRenderError(f"家族 {source} 缺 allow_change 列表")

    for pname, spec in (doc.get("params") or {}).items():
        if not isinstance(spec, dict):
            raise FamilyRenderError(f"家族 {source} 的 params.{pname} 必须是映射")
        if spec.get("type") == "enum" and not spec.get("options"):
            raise FamilyRenderError(f"家族 {source} 的 params.{pname} 是 enum 却没有 options")

    # ★ 补齐这条后，second_world / split_poster 的 {substrate_desc} 悬空才被发现：
    #   params 声明了枚举、segments 引用了 _desc，但 dicts 里没有对应映射。
    slots = set(collect_placeholders(doc))
    dicts = set(doc.get("dicts") or {})
    raw_params = doc.get("params") or {}
    for ph in slots:
        if ph in DERIVED_KEYS:
            continue
        if not ph.endswith("_desc"):
            continue
        base = ph[:-5]
        if base in dicts:
            continue
        spec = raw_params.get(base)
        if spec is None:
            raise FamilyRenderError(
                f"家族 {source} 引用了 {{{ph}}}，但 dicts 里没有 {base}，"
                f"params 里也没有名为 {base} 的参数 —— 该占位符会渲染成空串"
            )
        # ★ 关键判据：X_desc 期望的是「人话描述」，而不是枚举字面量。
        #   - params.X 是 enum   → 必须在 dicts.X 登记「枚举值 → 中文文案」。
        #     否则会渲染出「下半部分使用warm_ivory背景」这种把 token 当句子的东西。
        #     （second_world / split_poster 的 substrate 就栽在这里）
        #   - params.X 是 string/list → 它的取值本身就是描述文本，允许桥接。
        #     （surreal_collage 的 flat_shapes 属于这一种）
        if str((spec or {}).get("type", "string")) == "enum":
            raise FamilyRenderError(
                f"家族 {source} 的 {base} 是 enum 型参数，引用 {{{ph}}} 时必须在 "
                f"dicts.{base} 里登记「枚举值 → 文案」映射，否则会把枚举名直接拼进句子"
            )

    # ★ enum 的每个 option 都必须在 dicts 里有对应文案。
    #   为什么要把检查放到加载期：漏登记一个 option 的后果不是"少一段文案"，
    #   而是渲染期走兜底分支 —— 实测 split_poster 的 substrate 有 none、
    #   dicts 只登记了 warm_ivory，结果 /api/image/render 直接 500，
    #   而且 /generate 路径上因为异常类型没被捕获，还会**永久吃掉一张额度**。
    #   加载期炸掉，改 YAML 的人立刻就知道。
    for pname, spec in (doc.get("params") or {}).items():
        spec = spec or {}
        if str(spec.get("type", "")) != "enum":
            continue
        mapping = (doc.get("dicts") or {}).get(pname)
        if not isinstance(mapping, dict):
            continue                      # 没 dicts 的由上面的规则管
        known = {str(k) for k in mapping}
        missing = [str(o) for o in (spec.get("options") or []) if str(o) not in known]
        if missing:
            raise FamilyRenderError(
                f"家族 {source} 的 params.{pname} 声明了 option {missing}，"
                f"但 dicts.{pname} 里没有对应文案 —— 渲染时会解析不到描述。"
                f"已登记的是 {sorted(known)}"
            )


def collect_placeholders(doc: dict) -> list[str]:
    """递归收集所有文本里引用的占位符 —— 必须进 xxx_blocks / styles，
    否则会漏掉藏在 text_block 里的 {text_lang_desc}（校验器的老教训）"""
    texts: list[str] = []
    for v in (doc.get("segments") or {}).values():
        if isinstance(v, str):
            texts.append(v)
    for val in doc.values():
        if isinstance(val, dict):
            for vv in val.values():
                if isinstance(vv, str):
                    texts.append(vv)
                elif isinstance(vv, dict):
                    for vvv in vv.values():
                        if isinstance(vvv, str):
                            texts.append(vvv)
    found: set[str] = set()
    for t in texts:
        # ⚠️ PLACEHOLDER_RE 有两个捕获组，findall 会返回元组。必须取 group(1)
        found |= {m.group(1) for m in PLACEHOLDER_RE.finditer(t)}
    return sorted(found)


def load_families(families_dir: str | None = None) -> dict[str, dict]:
    """加载 families/*.yaml，按 id 索引（加载即校验）"""
    directory = families_dir or FAMILIES_DIR
    families: dict[str, dict] = {}
    if not os.path.isdir(directory):
        return families
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        data = read_family_yaml(os.path.join(directory, fn))
        if data.get("kind") != "family":
            continue
        validate_family(data, source=fn)
        fid = data.get("id")
        if fid:
            families[fid] = data
    return families


def get_family(family_id: str) -> dict | None:
    """按 id 取家族（**每次都读盘 + 全量校验，无缓存**）

    ⚠️ 请求路径不要直接用这个 —— 请走 template_manager.get_family_by_id()
    （lru_cache，且工坊装新家族后会 clear_cache()）。本函数的角色：
      ① CLI / 测试直连磁盘的入口；
      ② prompt_builder 的**兜底**（template_manager 里找不到时的最后一线，
         例如刚落盘还没进缓存的极端窗口）—— 正常请求几乎不会走到，
         所以无缓存可以接受；把它当主路径用才是 bug（审查 P2-6）。
    """
    return load_families().get(family_id)


# ────────────────────── 参数类型强制 ──────────────────────────────
# HTTP 表单 / JSON 里一切都是字符串，而 YAML 里 figures 是 "0"、graffiti 是 bool、
# color_swatches 是 bool、fixed_palette 是 list。不强制类型转换，
# swatches_blocks.get(True) 会因为拿到字符串 "true" 而永远取不到欏，静默失效。

# ─────────────── 参数与槽位解析（已拆至 param_resolver.py）───────────────
# god module 拆分第四刀（2026-10-05）：类型强制/默认值/auto 兜底/卡片信号整体搬走。
# AUTO_TOKEN 与下列名字在此再导出，旧调用方（render 主流程与测试）不断。
from services.param_resolver import (                            # noqa: E402,F401
    AUTO_TOKEN,
    _TRUE_TOKENS,
    _looks_chinese,
    _subject_text,
    coerce_params,
    fill_defaults,
    resolve_auto_slots,
)
from services.prompt_cleanup import _join                        # noqa: E402,F401

_HIGH_SAT_FALLBACK = ["朱红", "高纯度亮橙", "钴蓝"]


def _saturation(hex_str: str) -> float:
    """粗略估算饱和度：max-min 通道差 / 255。取不到就返回 -1"""
    if not hex_str or not isinstance(hex_str, str):
        return -1.0
    h = hex_str.lstrip("#")
    if len(h) != 6:
        return -1.0
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return -1.0
    return (max(r, g, b) - min(r, g, b)) / 255.0


def _pick_crossing_color(params: dict, palette: list) -> str:
    """决定跨界色。

    参数值不是 auto 时原样使用；是 auto 时从原图色板里挑饱和度最高的那个，
    挑不到再按顺序取第一个有名色的，最后才用兜底色。
    """
    val = params.get("crossing_color")
    if val and str(val).strip().lower() != AUTO_TOKEN:
        return str(val)

    named = [p for p in palette if isinstance(p, dict) and p.get("name")]
    if named:
        best = max(named, key=lambda p: _saturation(p.get("hex", "")))
        if _saturation(best.get("hex", "")) > 0.35:
            return f"{best['name']}（取自原图）"
        return f"{named[0]['name']} 的高饱和版本（取自原图并提升纯度）"
    return _HIGH_SAT_FALLBACK[0]


def _build_derived(family: dict, params: dict, card: dict) -> dict[str, str]:
    """计算规约 §7.2 的派生量占位符"""
    zh = _looks_chinese(family)

    photo_ratio = params.get("photo_ratio")
    try:
        ratio_f = float(photo_ratio)
    except (TypeError, ValueError):
        ratio_f = None

    anchors = card.get("anchors") or []
    selected = [a for a in anchors if a.get("selected", False)] or anchors[:3]
    subject = card.get("subject") or {}
    subject_name = subject.get("name") if isinstance(subject, dict) else str(subject or "")
    if not subject_name or str(subject_name).strip().lower() == AUTO_TOKEN:
        subject_name = ""
    palette = card.get("palette") or []
    composition = card.get("composition") or {}
    light = card.get("light") or {}

    default_subject = _subject_text(card, zh)

    derived: dict[str, str] = {
        # 主体
        "subject.name": subject_name or default_subject,
        "subject": subject_name or default_subject,
        # 比例
        "illustration_ratio": f"{1 - ratio_f:.0%}" if ratio_f is not None else "60%",
        "photo_ratio": f"{ratio_f:.0%}" if ratio_f is not None else "40%",
        # 锚点
        # 注意：句子里已写"提炼原图中的 N 个 {anchor_desc}"，所以 anchor_desc 内部
        # 不能再重复数量词，并且要和前面的"个"字衔接自然 —— 用顿号连接即可
        # ⚠️ 旧写法 `str(len(selected)) or "1"` 是死分支：
        #    str(0) == "0" 是**非空字符串**，永远 truthy，`or "1"` 永不生效。
        #    配合 card 恒空的路径，就渲染出「提炼原图中的 0 个轮廓与路径」——
        #    语法正确、语义荒谬、模型会照做。
        "anchor_count": str(len(selected) or 1),
        "anchor_desc": _join([a.get("desc") for a in selected if a.get("desc")]) or "轮廓与路径",
        "anchor_desc_short": _join(
            [a.get("desc") for a in selected if a.get("desc")]
        ) or "轮廓与路径",
        "materialize_anchor": (
            next((a.get("desc") for a in selected if a.get("materializable")), None)
            or (selected[0].get("desc") if selected else "")
            or str(params.get("materialize_anchor") or "")
            or ("画面中最有辨识度的视觉结构" if zh else "the most recognizable structure")
        ),
        # 抽象度 → 细节删除比例（规约 §7.2 detail_removal）
        "detail_removal": {
            "low": "30%-40%",
            "mid": "60%-70%",
            "high": "75%-85%",
        }.get(params.get("abstraction"), "60%-70%"),
        # ★ crossing_color: 参数值为 auto 时必须从原图色板里挑一个高饱和色，
        #   绝不能把字面量 "auto" 发给模型
        "crossing_color": _pick_crossing_color(params, palette),
        # 构图与色调
        "layers_desc": _join(composition.get("layers")) or (
            "天空、主体、地面" if zh else "sky, main subject, ground"
        ),
        "palette_desc": _join(
            [p.get("name") if isinstance(p, dict) else str(p) for p in palette[:3]]
        ) or ("暖米白、雾蓝灰、暖赭" if zh else "warm ivory, muted blue-grey, warm ochre"),
        "palette_keep": "",
        # 其他
        "text_lang": params.get("text_lang", "en"),
        "text_lang_desc": "英文" if params.get("text_lang", "en") == "en" else "中文",
        "text_align": params.get("text_align", "left"),
        "text_align_desc": "左对齐" if params.get("text_align", "left") == "left" else "居中",
        "split_ratio": params.get("split_ratio", "1:1"),
        "fixed_palette": _join(params.get("fixed_palette")) or "warm ivory、muted navy",
        "direction": params.get("direction", ""),
        "direction_desc": f"，{params['direction']}" if params.get("direction") else "",
        "school": params.get("school", ""),
        "major": params.get("major", ""),
        "gravity_center": light.get("dir", ""),
        # portrait_epic 的剪影朝向（规约 §7.2 side_desc）
        # ★ 取值里不能含「剪影」二字：模板里已有两种句式 ——
        #   「巨大的{side_desc}剪影」和「贴合本人的{side_desc}轮廓」。
        #   取值带「剪影」时前者渲染成「侧脸剪影剪影」（实测踩过）。
        "side_desc": {
            "left": ("面向左侧的侧脸" if zh else "left-facing profile"),
            "right": ("面向右侧的侧脸" if zh else "right-facing profile"),
            "front": ("正面" if zh else "front-facing"),
        }.get(params.get("silhouette_side"), "侧脸" if zh else "profile"),
    }

    # fidelity=heavy 时补充色板保留要求
    if params.get("fidelity") == "heavy" and palette:
        names = _join([p.get("name") for p in palette if isinstance(p, dict) and p.get("name")])
        if names:
            derived["palette_keep"] = f"保持原图主色不变：{names}。"
    if not derived["palette_keep"]:
        derived["palette_keep"] = ""

    return derived


# ────────────── 反推 forbid（规约 §5.2 + 两道过滤）──────────────

# ─────────────── 维度与反推 forbid（已拆至 dynamic_forbid.py）───────────────
# god module 拆分第五刀（2026-10-05）。以下再导出供渲染主流程与本文件
# 内的 validate_family（运行时查找）继续使用；新代码请 import dynamic_forbid。
from services.dynamic_forbid import (                             # noqa: E402,F401
    ALL_DIMENSIONS,
    LAYOUTS,
    SCOPES,
    SCOPE_MARKER_RE,
    build_dynamic_forbid,
    resolve_allow_change,
)

_ABSTRACT_WORDS = ("合并", "删除", "简化", "抽象", "概括", "压缩", "转化为", "大色块")
_PRESERVE_WORDS = ("不得改变", "必须保留", "保持原", "不得移除", "不得删")


def detect_conflicts(segments: dict[str, str]) -> list[str]:
    """渲染前冲突检测：creative 与 forbid 关键词打架（作用域重叠时）"""
    warnings: list[str] = []
    creative = segments.get("creative", "")
    forbid = segments.get("forbid", "")

    hits_abstract = [w for w in _ABSTRACT_WORDS if w in creative]
    hits_preserve = [w for w in _PRESERVE_WORDS if w in forbid]
    if hits_abstract and hits_preserve:
        # 若 forbid 已带作用域标记，说明两道滤网生效了，不算冲突
        if not SCOPE_MARKER_RE.search(forbid):
            warnings.append(
                f"creative 含抽象类词 {hits_abstract}，forbid 含保真类词 {hits_preserve}，"
                f"且 forbid 未标作用域 —— 可能互斥，建议检查 forbid_scope"
            )

    if len([x for x in forbid.split("；") if x.strip()]) > _MAX_FORBID_CLAUSES:
        warnings.append(f"forbid 条数超过 {_MAX_FORBID_CLAUSES} 条，会稀释 creative 权重")

    total = sum(len(v) for v in segments.values())
    if total > _MAX_TOTAL_CHARS:
        warnings.append(f"提示词总长 {total} 字，超过 {_MAX_TOTAL_CHARS} 字阈值（待 S0 实测校准）")

    return warnings


# ─────────────────── 核心规则选择（已拆至 rules_engine.py）───────────────────
# god module 拆分第一刀（2026-10-05）：选择逻辑整体搬走，此处仅保留再导出 ——
# 旧调用方（test_preflight 等）的 `from services.family_renderer import
# select_active_rules` 继续可用；新代码请直接 import services.rules_engine。
from services.rules_engine import select_active_rules  # noqa: E402,F401

# ───────────────────────── 渲染主流程 ─────────────────────────

def sanitize_value(value: Any, max_chars: int | None = None) -> str:
    """净化「用户可控的值」—— 挡住提示词注入

    ★ 为什么必须净化（审查发现 P2-9）
    ------------------------------
    占位符替换用的是正则而非 `str.format`（这是对的），但
    `render_family` 会做最多 MAX_PASSES=3 轮替换，而每轮都对整个字符串重新扫描。
    于是**第 N 轮替换出去的内容，会在第 N+1 轮被当成占位符再展开一次**：

        params = {"subject": "{hard_forbid_joined}"}
        → 第 1 轮：{subject}   → "{hard_forbid_joined}"
        → 第 2 轮：刚写进去的文本被识别为占位符 → 展开成整段 forbid 内容

    后果非常严重：项目自我标榜的「原图保真」靠 forbid 段约束模型，
    而任何用户只要在某个 string 型参数里写 `{hard_forbid_joined}`，
    就能把 forbid 的内容搬运进 creative/preserve 段 ——
    **保真约束可以被客户端单方面中和**。

    对策：任何取值在进 values 表之前先剥掉 `{...}` 形态。
    提示词里本来就不该出现裸花括号占位符，剥掉不影响正常文案。

    ★ 关于 max_chars（审查发现 P2-12）
    ---------------------------------
    限长的本意是防「用户往参数里塞几千字放大 token 成本」，
    但第一版对**所有**取值统一限长，于是 `hard_forbid_joined`、`style_block`、
    `dynamic_forbid` 这些 **YAML 里的可信长文本**也会被截断。
    当前 6 个家族最长取值都 < 300 所以没坏，但任何一段 YAML 一旦超过就会被
    静默截成 `…` —— 一个「改内容就悄悄坏」的陷阱。
    所以 max_chars=None 表示**不截断**，由调用方按来源决定。
    """
    s = value if isinstance(value, str) else str(value)
    s = PLACEHOLDER_RE.sub("", s)
    if max_chars is not None and len(s) > max_chars:
        s = truncate_gracefully(s, max_chars)
    return s


def truncate_gracefully(text: str, max_chars: int) -> str:
    """按**分句边界**截断，而不是从中间一刀砍断

    旧行为 `s[:300] + "…"` 的问题：落刀点完全由字数决定，
    实测 600 字的参数值被砍成「…很长的参数值很长的参数值…」——
    最后一个分句永远是残的，读起来像被嚼碎的句子，模型也容易理解偏。

    现在先在 [60%, 100%] 的窗口里找最后一个分句边界，从那儿落刀。
    找不到边界（比如一整段没有标点）才退回硬截断。

    ★ 仍然保留末尾的「…」：截断这件事必须**可见**。
      静悄悄地把文本变短，比截断本身更危险 —— 调用方无从察觉。
    """
    if max_chars is None or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    floor = int(max_chars * _GRACEFUL_FLOOR)
    for i in range(len(window) - 1, floor - 1, -1):
        if window[i] in _CLAUSE_BOUNDARY:
            return window[:i] + "…"
    return window + "…"


def _keyword_arg_render(text: str, values: dict[str, str]) -> str:
    """占位符替换 —— 用正则而非 str.format（坑②），并处理格式串（坑①）"""

    def repl(m: re.Match) -> str:
        key, fmt = m.group(1), m.group(2)
        raw = values.get(key)
        if raw is None:
            return m.group(0)  # 未提供则保留，由外层报告
        if fmt:
            # 仅支持 {x:.0%} 这类百分比格式，够用且安全
            if fmt.endswith("%"):
                try:
                    dec = int(fmt[2:-1]) if fmt.startswith(".") else 0
                    return f"{float(raw):.{dec}%}"
                except (ValueError, TypeError):
                    pass
            return str(raw)
        return str(raw)

    return PLACEHOLDER_RE.sub(repl, text or "")


def _bridge_missing_desc(family: dict, values: dict) -> list[str]:
    """★ 类型二修复：段里引用 {X_desc}，但 X 是参数且家族没有 dicts.X 时做桥接

    surreal_collage 就是这种情况：params 叫 flat_shapes，段里写 {flat_shapes_desc}。
    旧逻辑下 X_desc 查不到 → 填空串 → 占位符"消失" → unresolved 检测也认为没问题。

    这里把 X_desc 桥接到 X 的实义值，实在没有就记进 empty_slots 让调用方知道。
    """
    bridged: list[str] = []
    for ph in collect_placeholders(family):
        if not ph.endswith("_desc") or ph in values:
            continue
        base = ph[:-5]
        if base in values and str(values[base]).strip():
            values[ph] = str(values[base])
            bridged.append(ph)
    return bridged



# ─────────────────── 文本清理（已拆至 prompt_cleanup.py）───────────────────
# god module 拆分第二刀（2026-10-05）：残句/伪影/冠词清洗整体搬走，此处仅再导出。
from services.prompt_cleanup import (                               # noqa: E402,F401
    _prune_broken_sentences,
    _strip_lead_verb,
    tidy_join_artifacts,
)

def _card_signal_level(card: dict) -> str:
    """创作卡的信息量分级 —— 决定告警该说什么

    ★ 为什么要分级而不是「有/没有」二分：
      加了本地档提取之后，card 几乎总是非空的（至少有色板），
      但「只有色板」和「有主体+锚点」对保真的价值差着一个量级：
        - 色板够用 → palette_keep、fidelity=heavy 的保留约束能正确生成
        - 主体/锚点缺失 → 「反推 forbid」拿不到「这张照片的这条山脊必须保留」，
          只剩模板里写死的通用约束
      如果继续用二分，就会变成「要么没告警（骗人），要么每次都告警（噪音）」。
    """
    if not card:
        return "none"
    subject = card.get("subject")
    name = subject.get("name") if isinstance(subject, dict) else subject
    has_subject = bool(str(name or "").strip())
    anchors = [a for a in (card.get("anchors") or [])
               if isinstance(a, dict) and str(a.get("desc") or "").strip()]
    if has_subject or anchors:
        return "full"
    if card.get("palette"):
        return "palette_only"
    return "none"


def _card_has_signal(card: dict) -> bool:
    """这张提炼卡里有没有真正的信息？（保留给外部调用方，语义 = 非 none）"""
    return _card_signal_level(card) != "none"


def _card_warning(card: dict) -> str:
    level = _card_signal_level(card)
    if level == "full":
        return ""
    if level == "palette_only":
        return (
            "创作卡只有色板：未配置视觉模型（VISION_MODEL）或视觉识别失败，"
            "主体与视觉锚点无法提炼。保真仍可用，但「反推 forbid」降级为通用约束，"
            "强度低于设计预期。"
        )
    return (
        "缺少创作卡：主体名、视觉锚点、原图色板均未提炼，"
        "「反推 forbid」与锚点数相关的句子已降级为通用表述，保真强度低于预期。"
    )


def render_family(
    family: dict,
    params: dict | None = None,
    card: dict | None = None,
    strict: bool = True,
    locked: list | None = None,
) -> dict:
    """把家族 + 参数 + 提炼卡渲染成三段式

    locked：用户在 UI 里**显式设置过**的参数名列表（USER-LOCKED）。
          这些参数跳过默认值填充与 auto 兜底 —— 即使值为空，
          也保留用户的选择并让 missing_required 如实报告，
          而不是被系统悄悄覆盖（造梦师的 USER-LOCKED > 系统默认 原则）。

    返回 {family_id, params, allow_change, segments, warnings, unresolved,
          missing_required, coerced_notes, auto_resolved, bridged, dropped_lines}
    """
    card = dict(card or {})

    validate_family(family, source=family.get("id", "?"))

    # 1) 参数类型强制（HTTP 传来的都是字符串，不做强制会让 bool/枚举静默失配）
    params, coerced_notes = coerce_params(family, params)
    # 2) 补默认值（USER-LOCKED 的参数跳过 —— 用户显式选择 > 系统默认）
    lock_set = {str(x) for x in (locked or [])}
    params = fill_defaults(family, params, skip=lock_set)
    # 3) ★ auto 槽位兜底 —— 绝不让 "auto" 流进提示词（同样尊重 USER-LOCKED）
    params, auto_resolved = resolve_auto_slots(family, params, card, skip=lock_set)

    missing_required = [
        name
        for name, spec in (family.get("params") or {}).items()
        if spec.get("required") and not str(params.get(name, "") or "").strip()
    ]

    # 组装取值表
    values: dict[str, str] = {}

    # ★ unresolved 必须在**用到它之前**就初始化。
    #   第一版把它声明在下面 1165 行（渲染循环那里），但 dicts 分支已经在用它，
    #   属于「函数内局部变量提前引用」→ UnboundLocalError。
    unresolved: list[str] = []

    # 4) 参数值 —— 用户可控，所以：剥占位符 + **限长**
    for k, v in params.items():
        values[k] = sanitize_value(
            v if isinstance(v, str) else _join(v, ", "), MAX_PARAM_CHARS
        )

    # 记住哪些键是用户来源的 —— 统一净化那一趟要据此区分限长（见下）

    # 5) dicts：枚举值 → 文案
    for group, mapping in (family.get("dicts") or {}).items():
        if not isinstance(mapping, dict) or not mapping:
            continue
        picked = params.get(group)
        desc = mapping.get(picked) if picked is not None else None
        if desc is None and picked is not None:
            # bool 键在 YAML 里是真 bool（true:/false:），HTTP 来的是字符串
            if isinstance(picked, str) and picked.lower() in _TRUE_TOKENS:
                desc = mapping.get(True)
            elif isinstance(picked, str) and picked.lower() in ("false", "0", "no", "off"):
                desc = mapping.get(False)
        if desc is None:
            # 旧实现：next(iter(mapping.values())) —— 静默取字典第一个值。
            # 这比取空串更危险：空串会让 `_prune_broken_sentences` 把整句删掉，
            # 而猜出来的值会拼成一句**语法正常但描述对象错误**的话。
            # 例：dicts.substrate 有 old_yellow / art_paper，参数没选时
            #     所有请求都会被写上「暖调米白做旧艺术纸」。
            # 正确做法：留空，让残句清理删掉整句，并记进 unresolved 供诊断。
            #
            # ⚠️ 这里必须是 append 且 unresolved 已经初始化好。
            #    第一版写的是 `unresolved.add(...)` —— 既用错了方法（它是 list），
            #    又用在了 `unresolved = []` 之前，触发 UnboundLocalError。
            #    而 split_poster 的 substrate 正好有 options 里没登记进 dicts 的值
            #    （enum 有 none、dicts 只有 warm_ivory），
            #    于是「合法参数」直接把 /api/image/render 打成 500。
            unresolved.append(f"{group}_desc")
            desc = ""
        values[f"{group}_desc"] = _strip_lead_verb(str(desc))

    # 6) xxx_blocks：按对应参数取整段
    for key, val in family.items():
        if key.endswith("_blocks") and isinstance(val, dict):
            group = key[:-7]
            picked = params.get(group)
            block = ""
            if picked is not None:
                block = val.get(picked, "")
                if not block and isinstance(picked, bool):
                    block = val.get(picked, "")
                if not block:
                    alt = str(picked).lower()
                    if alt in _TRUE_TOKENS:
                        block = val.get(True, "")
                    elif alt in ("false", "0", "no", "off"):
                        block = val.get(False, "")
            values[f"{group}_block"] = block or ""

    # text_block 特例：按 text_role 取
    text_blocks = family.get("text_blocks") or {}
    if text_blocks:
        values["text_block"] = text_blocks.get(params.get("text_role", ""), "") or ""

    # style_block（full_restyle 专属）
    styles = family.get("styles") or {}
    if styles:
        values["style_block"] = str(styles.get(params.get("style"), "") or "")

    # 7) 派生量
    values.update(_build_derived(family, params, card))

    # 8) ★ {X_desc} 桥接
    bridged = _bridge_missing_desc(family, values)

    # 9) 反推 forbid
    allow_change = resolve_allow_change(family, params)
    values["dynamic_forbid"] = build_dynamic_forbid(family, params, card, allow_change)

    # 10) hard_forbid 连接
    values["hard_forbid_joined"] = _join(family.get("hard_forbid") or [])

    # 11) 创意槽（来自 params 或 card）
    for slot in (
        "object_form", "interaction", "inner_world", "giant_element",
        "flat_shapes_desc", "small_elements_desc", "detail_constraint",
        "materialize_anchor",
    ):
        values.setdefault(slot, str(params.get(slot, "") or ""))

    # ★ 统一的净化闸口：不管值来自 params、card 还是派生计算，
    #   进渲染循环之前一律剥掉裸占位符形态。
    #   放在这里而不是散在各处，是因为「多轮渲染会二次展开」这条特性
    #   需要对**所有**取值同时成立 —— 只净化一半等于没净化。
    #
    #   但**限长只作用于用户来源的键**：派生量与 *_block 是 YAML 里的可信长文本，
    #   对它们限长会让「某段文案超过 300 字就被静默截断」成为一个改内容才发现的坑。
    user_keys = set(params.keys()) | {"subject", "subject.name", "anchor_desc",
                                      "anchor_desc_short", "materialize_anchor"}

    # ★ 三级上限，显式判断（见 NEVER_TRUNCATE_KEYS 的注释）
    def _cap_for(k: str) -> int | None:
        if k in NEVER_TRUNCATE_KEYS:
            return None          # ① 不可牺牲：保真与禁令
        if k in user_keys:
            return MAX_PARAM_CHARS   # ② 可牺牲：用户参数（成本治理）
        return None              # ③ YAML 可信长文本（*_block / 派生量）

    values = {k: sanitize_value(v, _cap_for(k)) for k, v in values.items()}

    # ⚠️ 坑③：多轮渲染
    segments: dict[str, str] = {}
    dropped_total = 0
    for sname in ("preserve", "creative", "forbid"):
        text = (family.get("segments") or {}).get(sname, "")
        rendered = text
        for _ in range(MAX_PASSES):
            new = _keyword_arg_render(rendered, values)
            if new == rendered:
                break
            rendered = new
        left = sorted({m.group(1) for m in PLACEHOLDER_RE.finditer(rendered)})
        if left:
            unresolved.extend(f"{sname}:{x}" for x in left)
        # 残句清理 + auto 保险丝
        rendered, dropped = _prune_broken_sentences(rendered, drop_auto=True)
        dropped_total += dropped
        segments[sname] = tidy_join_artifacts(rendered.strip())

    # 空槽兜底（规约 §7.3）：block 为空时不留下双空行
    for k, v in segments.items():
        segments[k] = re.sub(r"\n{3,}", "\n\n", v).strip()

    warnings = list(coerced_notes)
    warnings += detect_conflicts(segments)
    if dropped_total:
        warnings.append(f"清理了 {dropped_total} 行残缺语句（占位符解析为空或残留 auto）")

    # ★ 「提炼卡信息量不足」必须被显式看见（审查发现 P0-1）
    #
    # card 是唯一贯穿全链路的数据：主体名、可视觉化锚点、原图色板、风险点。
    # 没有它时不会报错，而是**静默产出垃圾指令**：
    #
    #     zine 的 creative: "插画只提炼原图中的 0 个轮廓与路径，删除 75%-85% 的细节"
    #     6/6 家族的 forbid: "{dynamic_forbid}" 整行被静默删掉（行数 2→1）
    #
    # 现在 card 有了生产者（services/card_extractor.py），但分两档：
    # 本地档只能给色板，主体/锚点要视觉模型。所以告警也分两级：
    #   palette_only → 色板可用、反推 forbid 降级
    #   none         → 连色板都没有
    card_level = _card_signal_level(card)
    card_warning = _card_warning(card)
    if card_warning:
        warnings.append(card_warning)

    if strict and missing_required:
        raise FamilyRenderError(
            f"缺少必填参数：{missing_required}（家族 {family.get('id')}）"
        )
    if strict and unresolved:
        raise FamilyRenderError(f"存在未解析占位符：{unresolved}")

    # ── ★ 核心规则门控（造梦师 Compiler Priority Gate 的移植）──
    #
    # 模板工坊编译出的家族带 `card_all_rules`（视觉卡的 5–8 条全部规则，仅存档）。
    # 门控原则：**全量留档，只选 3–5 条当前相关的进 prompt** ——
    # 把整张卡灌进提示词会让信息密度失控，稀释真正重要的约束。
    #
    # 注入位置：creative 段末尾的【视觉规则】块。
    # 普通家族没有这个字段 → 完全不受影响（零回归）。
    all_rules = [r for r in (family.get("card_all_rules") or [])
                 if isinstance(r, str) and r.strip()]
    if all_rules:
        creative_text = segments.get("creative", "")

        # ★ 选择策略：有 card_rule_meta 就按相关性选，没有就走旧的位置截取（零回归）
        picked, how = select_active_rules(
            family, all_rules, creative_text, params, card
        )
        if how == "legacy":
            # 完全没有可用规则元数据 → 行为与改动前逐字一致
            active = [r for r in all_rules[:8] if r[:20] not in creative_text][:5]
        else:
            active = picked
            # ★ 只有「工坊显式给了 card_rule_meta」才提示；运行时自动推导的
            #   存量家族保持安静 —— 否则每次渲染都多一条噪音告警。
            if how.startswith("meta"):
                warnings.append(f"视觉规则门控：{how}")
        # ★ 超长规则：压缩而不是丢弃
        #
        # 旧实现是「整条丢弃」，理由是"截断会产生误导性指令"。
        # 但造梦师的载荷裁剪规则明确写着：
        #   「Do not sacrifice ... the selected Active Core Rules to keep ornamental detail.」
        #   —— 已选中的核心规则属于**不可牺牲**的一档，宁可削别的也不许丢它。
        # 一条色彩/材质规则被整个丢掉，等于"像不像"少了一个一票否决项。
        #
        # 折中：按分句边界压缩到 MAX_RULE_CHARS（规则一般把关系写在前面，
        # 压掉的多半是后半段的补充说明）；压完已经短到没有信息量的才丢弃。
        kept, compressed, dropped = [], 0, 0
        for r in active:
            if len(r) > MAX_RULE_CHARS:
                c = truncate_gracefully(r, MAX_RULE_CHARS)
                if len(c) >= 20:
                    kept.append(c)
                    compressed += 1
                else:
                    dropped += 1
                continue
            kept.append(r)
        if compressed:
            warnings.append(
                f"视觉规则过长已压缩 {compressed} 条至 {MAX_RULE_CHARS} 字以内"
                f"（造梦师：已选中的核心规则不可整条丢弃）"
            )
        if dropped:
            warnings.append(f"视觉规则过长已丢弃 {dropped} 条（压缩后无信息量，规则应 ≤{MAX_RULE_CHARS} 字）")
        if kept:
            block = "\n".join(f"{i}. {sanitize_value(r, MAX_RULE_CHARS)}" for i, r in enumerate(kept, 1))
            segments["creative"] = (
                creative_text.rstrip()
                + "\n\n【视觉规则】（必须遵守的可观察关系）\n" + block
            ).strip()

    return {
        "family_id": family.get("id"),
        "params": params,
        "allow_change": allow_change,
        "segments": segments,
        "warnings": warnings,
        "unresolved": unresolved,
        "missing_required": missing_required,
        "coerced_notes": coerced_notes,
        "auto_resolved": auto_resolved,
        "bridged": bridged,
        "dropped_lines": dropped_total,
        "card_level": card_level,
    }


def render_to_prompt(result: dict) -> str:
    """把三段式拼成最终提示词（preserve → creative → forbid）"""
    s = result["segments"]
    parts = [s.get("preserve", ""), s["creative"], s["forbid"]]
    return "\n\n".join(p for p in parts if p)


# ─────────────────── 给前端 / 契约层用的元信息 ───────────────────

def params_schema(family: dict) -> list[dict]:
    """把 params 声明转成 UI 可用的 schema —— 「params 即 UI schema」

    这是方案 §3.10.9 的核心杠杆：新增家族时**前端零改动**。
    前端拿到它就能自动渲染出滑杆 / 下拉 / 开关，不需要为每个家族写组件。

    ★ 中文名（label / option_labels）也在这里补齐（审查后修）
    -------------------------------------------------------
    旧实现写的是 `"label": name` —— 于是前端显示的就是参数名本身，
    实测界面上出现「style / palette_source / render / resolution_hint」，
    选项更是 `ink_wash`、`warm_ivory`、`from_photo`、`0_2` 这种 token。
    中文名交给 services/param_labels.py（数据源 templates/_labels.yaml），
    前端退化为「有就用、没有才兜底」，那条杠杆才完整。
    """
    from services.param_labels import option_label, param_label

    out: list[dict] = []
    for name, spec in (family.get("params") or {}).items():
        spec = spec or {}
        options = [str(o) for o in (spec.get("options") or [])]
        item = {
            "name": name,
            # 优先用 YAML 里显式写的 label（模板工坊产出的草稿会自带中文名），
            # 其次查全局标签表，最后才是参数名本身
            "label": str(spec.get("label") or param_label(name)),
            "type": str(spec.get("type", "string")),
            "default": spec.get("default"),
            "required": bool(spec.get("required")),
            "source": spec.get("source"),
        }
        if options:
            item["options"] = options
            # 允许单参数自带选项中文名（工坊产出的新风格一定不在全局表里），
            # 没有的再回落到全局表
            # ★ 形状容错（实测 2026-10-01）：提炼产物会把 option_labels 写成 list
            #   （与 options 按位置对应的数组）——直接 .get() 会 AttributeError，
            #   一个坏参数毒死整个 /api/families。list 时按位置转 dict；其余非 dict 忽略。
            own_raw = spec.get("option_labels")
            if isinstance(own_raw, list):
                own = dict(zip([str(o) for o in options], [str(x) for x in own_raw]))
            elif isinstance(own_raw, dict):
                own = own_raw
            else:
                own = {}
            item["option_labels"] = {
                o: str(own.get(o) or option_label(name, o)) for o in options
            }
        if "range" in spec:
            item["range"] = list(spec["range"])
        out.append(item)
    return out


def family_meta(family: dict) -> dict:
    """给 GET /api/families 用的精简元信息"""
    return {
        "id": family.get("id"),
        "name": family.get("name"),
        "icon": family.get("icon", "🎨"),
        "description": family.get("description", ""),
        "layout": family.get("layout"),
        "default_aspect": family.get("default_aspect"),
        "forbid_scope": family.get("forbid_scope"),
        "allow_change": resolve_allow_change(family, {}),
        "allow_change_mode": family.get("allow_change_mode", "static"),
        "suitable": family.get("suitable", []),
        # ★ 「不适合」清单（2026-10-01 经用户审核落地）：只写进 not_suitable 字段，
        #   不参与渲染，专门给「小助手看图推荐」做排除判断 ——
        #   推荐质量的一半来自知道"这张图不该用哪个风格"。
        "not_suitable": family.get("not_suitable", []),
        "params": params_schema(family),
        "variants": [
            {"id": v.get("id"), "name": v.get("name"), "params": v.get("params", {})}
            for v in (family.get("variants") or [])
        ],
    }


if __name__ == "__main__":
    import sys

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    fams = load_families()
    print(f"已加载 {len(fams)} 个家族：{list(fams)}\n")
    for fid, fam in fams.items():
        try:
            r = render_family(fam, {}, {}, strict=False)
            lens = {k: len(v) for k, v in r["segments"].items()}
            flag = "  ⚠必填缺失" if r["missing_required"] else ""
            print(f"OK   {fid:16} 段长={lens}{flag}")
            if r["auto_resolved"]:
                print(f"       auto 已兜底: {r['auto_resolved']}")
            if r["bridged"]:
                print(f"       _desc 已桥接: {r['bridged']}")
            for w in r["warnings"]:
                print(f"       ! {w}")
        except FamilyRenderError as e:
            print(f"ERR  {fid:16} {e}")
