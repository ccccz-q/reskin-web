"""核心规则选择引擎 —— 从「视觉卡全量规则」里选 3-5 条进提示词

════════ 从 family_renderer 拆出的原因（god module 渐进拆分第一刀）════════

family_renderer.py 曾同时承载「家族加载/校验/渲染/占位符/残句清理/规则选择」，
70KB 单文件让 review 与并行开发都困难。规则选择是其中**内聚度最高**的一块：
输入只有（家族文档, 参数, 提炼卡, creative 文本），输出只有（选中的规则, 说明），
与渲染主流程只通过一个函数调用相连 —— 最适合第一个抽出去。

★ 零行为变更保证：本次是纯搬运 + 导入改写，全部判定逻辑逐字未动；
  test_preflight 60 项 + test_renderer 55 项守着，跑绿即等价。

口径来源：造梦师 prompt-compiler.md 的 Active Core Rules 选择判据
  优先：确立媒介身份 / 与当前场景直接相关 / 强继承权重高
  降级：仅存档的 / 与 USER-LOCKED 冲突的条件继承 / **已被表达过的重复项**
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ─────────────────── Active Core Rules 选择 ───────────────────

# ★ 决定「像不像参考图」的轴 —— 造梦师判据里优先级最高的两类：
#   ① 确立媒介身份的规则；② 与当前场景直接相关的规则。
#   色彩 / 材质 / 曝光 / 媒介 是相似度的一票否决项：它们丢了，构图再对也不像。
_MUST_AXES = frozenset({"medium", "color", "material", "exposure"})

# 权重序：强继承 > 条件继承 / 未标注
_WEIGHT_RANK = {"strong": 2, "conditional": 1}

# ─────────── 轴推导（从规则文本推断它属于哪个视觉轴）───────────
#
# ★ 为什么词表放在 family_renderer 而不是 style_forge：
#   style_forge 依赖 family_renderer，反向 import 会成环。
#   而"按相关性选规则"这件事发生在渲染侧，词表跟着选择逻辑走才不会有两份。
#
# ★ 为什么优先看规则开头的轴标签（形如「色彩：…」）：
#   只在全文里找关键词会判错 —— 实测「调度：次要元素沿画面**边缘**散布」
#   被判成 material（"边缘"命中材质词）、「材质：…平面**印刷**感」被判成 medium
#   （"印刷"命中媒介词，且 medium 排在词表第一）。8 条规则错 2 条，
#   而判错 = 必选池选错人 = 整个相关性选择失去意义。
_AXIS_LABEL_HINT = (
    ("medium", ("媒介", "主媒介", "画种")),
    ("exposure", ("曝光", "明度", "影调", "亮度")),
    ("material", ("材质", "纹理", "质感", "表面", "边缘", "笔触", "网点", "颗粒")),
    ("color", ("色彩", "色板", "配色", "颜色", "色")),
    ("composition", ("构图",)),
    ("spatial", ("空间", "层次", "纵深", "前景")),
    ("blocking", ("调度", "排布", "遮挡", "动线")),
    ("light", ("光位", "光源", "光线", "光")),
)

# 全文关键词（只在轴标签缺失或不认识时才用）
_AXIS_KEYWORDS = (
    ("medium", ("媒介", "印刷", "插画", "版画", "丝网", "平版", "凸版",
                "颜料", "手绘", "水墨", "像素", "拼贴")),
    ("exposure", ("曝光", "明度", "影调", "高光", "暗部", "亮度", "最亮")),
    ("material", ("材质", "纹理", "质感", "颗粒", "表面", "网点", "笔触", "边缘")),
    ("color", ("色彩", "色板", "饱和", "对比色", "点缀色", "占比", "冷调", "暖调")),
    ("composition", ("构图", "留白", "负空间", "对称", "偏置", "居中")),
    ("spatial", ("空间", "前景", "中景", "背景", "纵深", "层次")),
    ("blocking", ("调度", "遮挡", "并置", "动线", "排布")),
    ("light", ("光位", "光源", "光线", "侧光", "逆光", "硬光", "柔光")),
)

# 视觉卡未给 strong 档时的默认强继承轴（对齐造梦师 Strong Transfer 固有清单：
# 色彩结构 / 光影行为 / 曝光行为 / 材质处理 / 纹理与捕捉特性 / 媒介 / 渲染特性）
_DEFAULT_STRONG_AXES = frozenset({"medium", "color", "material", "exposure"})


def _axis_of_rule(rule: str) -> str:
    """判定规则的轴 —— 先看开头的轴标签，再退回到全文关键词匹配"""
    label = ""
    for sep in ("：", ":"):
        if sep in rule:
            label = rule.split(sep, 1)[0].strip()
            break
    if label:
        for name, keys in _AXIS_LABEL_HINT:
            if any(k in label for k in keys):
                return name
    for name, keys in _AXIS_KEYWORDS:
        if any(k in rule for k in keys):
            return name
    return ""


def derive_rule_meta(rules: list[str], strong_text: str = "") -> list[dict]:
    """从规则文本推导 axis / weight（不给模型加输出负担）

    weight 判据：轴名命中 `transfer_scope.strong` 里的类别名 → strong。
    ★ 不能用字面重叠度：strong 档是「色彩结构」这类四字类别名，
      规则是「色彩：石灰白占四成…」，只共享「色彩」两字，2-gram 重叠恒为 1。
    """
    out: list[dict] = []
    for r in rules:
        r = str(r)
        if not r.strip():
            continue
        axis = _axis_of_rule(r)
        if strong_text:
            hints = _AXIS_STRONG_HINT.get(axis, ())
            weight = "strong" if any(h in strong_text for h in hints) else "conditional"
        else:
            weight = "strong" if axis in _DEFAULT_STRONG_AXES else "conditional"
        out.append({"text": r, "axis": axis, "weight": weight})
    return out


# 各轴在 transfer_scope.strong 里的典型表述（见上：按轴名匹配，不按字面重叠）
_AXIS_STRONG_HINT = {
    "medium": ("媒介", "渲染"),
    "color": ("色彩", "色"),
    "material": ("材质", "纹理"),
    "exposure": ("曝光", "明度"),
    "light": ("光影", "光"),
}

# 至少 / 至多注入几条（对齐造梦师「Active Core Rules 3–5 条」）
_MIN_ACTIVE_RULES = 3
_MAX_ACTIVE_RULES = 5
_MAX_MUST_RULES = 3

# ★ 「已被 creative 表达过」的判定阈值：规则的 2-gram 有这么多比例已出现在
#   creative 里，就视为重复表达，不再占用规则块名额。
#   造梦师明写要降级 "duplicates already expressed"——重复的约束只值一次出现。
#   实测（voxel 探针，8 条规则）：已表达组 60%-90%，新信息组 22%-43%，
#   0.55 正好落在两组之间的空档。
_COVER_RATIO = 0.55


def _covered_by(rule: str, creative_text: str) -> bool:
    """规则内容是否已被 creative 表达（2-gram 覆盖率）

    ★ 为什么不能只用前 20 字精确匹配：规则带着「构图：」这类轴前缀，
      而 creative 里同一句话没有前缀 —— 精确匹配全部漏掉。
      后果（A/B 实测抓到）：creative 已写明的构图指令，规则块里又占一个名额，
      把真正带新信息的色彩/曝光规则挤下去。
    """
    if not rule or not creative_text:
        return False
    grams = {rule[i:i + 2] for i in range(len(rule) - 1)}
    if not grams:
        return False
    return sum(1 for g in grams if g in creative_text) / len(grams) >= _COVER_RATIO


def _rule_meta_index(family: dict) -> dict[str, dict]:
    """把 card_rule_meta 转成「规则前 20 字 → 元数据」的索引

    ★ 为什么按前 20 字索引：card_all_rules 是 list[str]，card_rule_meta 是并行的
      元数据数组，两者靠**规则文本**对齐。但渲染时可能已做过 sanitize，
      文本未必逐字相等 —— 用前缀匹配容忍这些差异（与旧门控的 r[:20] 同口径）。

    兼容三种形状（模型/工坊产出不总是规整）：
      - list[dict]  [{"text": "...", "axis": "color", "weight": "strong"}, ...]
      - dict        {"规则文本": {"axis": ..., "weight": ...}}
      - 其它 / 缺失 → 空索引（调用方回落到旧的位置截取逻辑）
    """
    raw = family.get("card_rule_meta")
    idx: dict[str, dict] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                idx[str(k)[:20]] = v
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("text") or item.get("rule") or "")[:20]
            if key:
                idx[key] = item
    return idx


def _overlap_score(rule: str, ctx: str) -> int:
    """规则与「当前场景上下文」的字面重叠度（确定性打分，不引入 LLM）

    用 2-gram 计数而不是子串包含：子串包含对长句不公平，
    2-gram 能稳定反映「这条规则在说当前这件事吗」。

    ctx 为空时返回 0 —— 没有上下文就无从判断相关性，退化为按权重与原序排。
    """
    if not rule or not ctx:
        return 0
    n = 2
    grams = {rule[i:i + n] for i in range(len(rule) - n + 1)}
    return sum(1 for g in grams if g in ctx)


def _scene_context(family: dict, params: dict, card: dict) -> str:
    """拼出「当前这一次」的场景上下文，用于给规则打相关性分

    取三路信号：
      ① 参数选中项对应的中文文案（dicts）—— 用户这次真正选了什么
      ② 提炼卡的色板色名 —— 客观测出来的颜色
      ③ 主体名 —— 画面在说谁
    """
    parts: list[str] = []
    dicts = family.get("dicts") or {}
    if isinstance(dicts, dict):
        for group, mapping in dicts.items():
            if not isinstance(mapping, dict):
                continue
            picked = params.get(group)
            desc = mapping.get(picked)
            if isinstance(desc, str) and desc:
                parts.append(desc)
    for p in ((card or {}).get("palette") or []):
        if isinstance(p, dict) and p.get("name"):
            parts.append(str(p["name"]))
    subj = (card or {}).get("subject")
    if isinstance(subj, dict) and subj.get("name"):
        parts.append(str(subj["name"]))
    return " ".join(parts)


def select_active_rules(
    family: dict,
    all_rules: list[str],
    creative_text: str,
    params: dict | None = None,
    card: dict | None = None,
    limit: int = _MAX_ACTIVE_RULES,
) -> tuple[list[str], str]:
    """从完整规则里选 3–5 条「当前相关」的（造梦师 Active Core Rules）

    返回 (选中的规则, 选择方式说明) —— 后者写进 warning 便于排查。

    ★ 为什么原来不够（问题诊断）
    ----------------------------
    旧实现是**位置截取**：

        active = [r for r in all_rules[:8] if r[:20] not in creative_text][:5]

    后果：视觉卡的第 6、7、8 条规则（通常是构图/调度/空间类）**永远进不了提示词**，
    而第 1–5 条里如果混着与本次参数无关的规则，就会一直占着名额。
    于是「换个参数，进提示词的还是那 5 条」—— 决定像不像的色彩/材质类规则
    可能正好排在第 6 位，被永久挤掉。

    造梦师的选择判据（`prompt-compiler.md` 第 77 行）：
      优先：确立媒介身份 / 与当前 Scene Master 直接相关 / 强继承权重高 / 缺失会明显丢失参考
      降级：仅存档用的 / 与 USER-LOCKED 冲突的条件继承 / 已被媒介约束表达过的重复项

    ★ 零回归保证
    ------------
    家族没有 `card_rule_meta` → 返回 None 标记，调用方**完全走旧的位置截取逻辑**。
    10 个手写家族都没有这个字段 → 行为与改动前逐字一致。
    """
    params = dict(params or {})
    card = dict(card or {})

    # 已在 creative 里写过的规则不重复注入（工坊编译时可能已吃进 4–6 条）
    candidates = [r for r in all_rules if r[:20] not in creative_text]

    meta_idx = _rule_meta_index(family)
    how = "meta"
    if not meta_idx:
        if not candidates:
            return [], "legacy"
        # ★ 运行时兜底推导：本次改动**之前**编译出的家族只有 card_all_rules，
        #   没有 card_rule_meta（元数据是工坊编译时才存档的）。
        #   不在这里推导的话，所有存量家族会永远走位置截取 ——
        #   阶段 B 要等用户重新提炼一次才生效，那等于没生效。
        #   代价是轴由文本推断（可能不准），但"推断的轴"仍远好于"固定取前 5 条"。
        meta_idx = {x["text"][:20]: x for x in derive_rule_meta(candidates)}
        if not meta_idx:
            return [], "legacy"
        how = "auto"
        # 静默升级（不打扰用户），但留 debug 级日志供离线评估推导准确率
        logger.debug("规则门控：家族 %s 无 card_rule_meta，已按文本自动推导轴",
                     family.get("id"))

    # ★ 内容级去重（放在 meta 解析之后、打分之前）：
    #   前缀精确匹配只能抓到逐字相同的，抓不到「同义已表达」——
    #   规则带着「构图：」轴前缀，creative 里同一句话没有前缀，精确匹配全漏。
    #   已表达过的规则不占名额，把名额让给带新信息的规则（造梦师降级判据）。
    #   A/B 实测抓到的反面教材：creative 已写明的构图指令在规则块里又占一格，
    #   把真正带新信息的色彩/曝光规则挤了下去。
    n_dup = sum(1 for r in candidates if _covered_by(r, creative_text))
    if n_dup:
        candidates = [r for r in candidates if not _covered_by(r, creative_text)]

    ctx = _scene_context(family, params, card)

    def meta_of(rule: str) -> dict:
        return meta_idx.get(rule[:20], {}) or {}

    scored: list[tuple[int, int, int, str, dict]] = []
    for i, r in enumerate(candidates):
        m = meta_of(r)
        axis = str(m.get("axis") or "").lower()
        weight = str(m.get("weight") or "").lower()
        # 第三个元素是原下标 i：排序键升序，平手时**靠前的规则先选**。
        # ⚠️ 这里曾写成 -i —— 升序下 -i 反而让靠后的先选，
        #    无轴规则全平手时会选中规则 3-7 而不是 1-5（测试 7 抓到）。
        scored.append((_overlap_score(r, ctx), _WEIGHT_RANK.get(weight, 1), i, r, m))
        # 记 axis 供下面分池用
        scored[-1] = (scored[-1][0], scored[-1][1], scored[-1][2], r,
                      {"axis": axis, "weight": weight})

    # ① 必选池：媒介 / 色彩 / 材质 / 曝光 —— 最多 3 条
    #
    # ★ 为什么不要求 weight == "strong"：
    #   `card_rule_meta` 目前由代码从视觉卡推导（见 style_forge._derive_rule_meta），
    #   weight 是**推断值**而非视觉卡的显式声明，推导有误差。
    #   若把 strong 作为必选条件，一次推断失误就让"色彩/材质类恒在"这条
    #   一票否决保证失效 —— 那还不如不做相关性选择。
    #   所以这里只看 axis（也是推断的，但轴名由关键词命中，比权重可靠得多），
    #   weight 只作为池内排序依据。
    must_pool = [x for x in scored if x[4]["axis"] in _MUST_AXES]
    must_pool.sort(key=lambda x: (-x[1], -x[0], x[2]))
    must = [x[3] for x in must_pool[:_MAX_MUST_RULES]]

    # ② 其余：按 权重 → 相关性 → 原序 排序（权重优先于相关性，
    #    因为「强继承」是视觉卡显式声明的迁移承诺，比字面重叠更可靠）
    rest_pool = [x for x in scored if x[3] not in must]
    rest_pool.sort(key=lambda x: (-x[1], -x[0], x[2]))

    selected = list(must)
    for x in rest_pool:
        if len(selected) >= limit:
            break
        selected.append(x[3])

    # ③ 保底：不足 3 条且还有候选时补齐（造梦师要求 Active 至少 3 条）
    if len(selected) < _MIN_ACTIVE_RULES:
        for x in rest_pool:
            if len(selected) >= _MIN_ACTIVE_RULES:
                break
            if x[3] not in selected:
                selected.append(x[3])

    # 恢复原卡片顺序输出（提示词里按卡的次序读更自然，选择是筛选不是重排）
    order = {r: i for i, r in enumerate(all_rules)}
    selected.sort(key=lambda r: order.get(r, 999))

    how = (f"{how}:按相关性选（必选 {len(must)} 条 / 共 {len(selected)} 条，"
           f"候选 {len(candidates)} 条，去重已表达 {n_dup} 条）")
    if not ctx:
        how += "，无场景上下文（仅按权重）"
    return selected, how


