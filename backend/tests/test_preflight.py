"""Preflight 编译后自检 + Active Core Rules 相关性选择

    python tests/test_preflight.py

★ 这个文件守两件事：
  ① 造梦师 preflight 的 6 项移植检查，报得准、且不误报
  ② 规则门控从「位置截取」改成「按相关性选」之后，
     **没有 card_rule_meta 的老家族行为必须逐字不变**（零回归）
"""
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from services.family_renderer import load_families, render_family  # noqa: E402
from services.rules_engine import select_active_rules               # noqa: E402
from services.preflight import preflight                     # noqa: E402
from services.prompt_builder import build_prompt             # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"\n      {detail}" if detail else ""))


# ── 测试 1：媒介漂移 ────────────────────────────────────
print("[测试 1] 媒介漂移（造梦师 preflight #1/#7）")

w = preflight(
    spec={"card_medium": {"primary": "纸本丝网插画"}},
    segments={"creative": "温暖的纸面，浅景深虚化背景，35mm 镜头感"},
)
check("非摄影媒介 + 摄影化表述 → 报警",
      any("媒介漂移" in x for x in w), str(w))

w = preflight(
    spec={"card_medium": {"primary": "纸本丝网插画"}},
    segments={"creative": "温暖的纸面，手工网点与硬朗边缘"},
)
check("非摄影媒介 + 无非摄影表述 → 不报", not w, str(w))

w = preflight(
    spec={},
    segments={"creative": "浅景深，35mm 镜头"},
)
check("★ 未声明媒介 → 不报（宁可漏报不可误报）", not w, str(w))

# ── 测试 2：来源残留泄漏 ────────────────────────────────
print("\n[测试 2] 来源残留泄漏（造梦师 preflight #3）")

w = preflight(
    spec={"card_source_residue": ["参考人物的可识别服饰组合"]},
    segments={"forbid": "不得改变主体轮廓"},
    prompt="不得改变主体轮廓",
)
check("残留项未进 forbid → 报警", any("来源残留" in x for x in w), str(w))

w = preflight(
    spec={"card_source_residue": ["参考人物的可识别服饰组合"]},
    segments={"forbid": "不得出现：参考人物的可识别服饰组合"},
    prompt="不得出现：参考人物的可识别服饰组合",
)
check("残留项已在 forbid → 不报", not w, str(w))

w = preflight(spec={}, segments={"forbid": "什么都没有"})
check("无残留清单 → 不报", not w, str(w))

# ── 测试 3：USER-LOCKED 未落地 ──────────────────────────
print("\n[测试 3] USER-LOCKED 未落地（造梦师 preflight #6）")

fam_lock = {
    "id": "lk", "name": "锁定探针", "layout": "full", "forbid_scope": "whole",
    "hard_forbid": ["a", "b", "c", "d"], "allow_change": ["color"],
    "params": {},
    "dicts": {"palette_mode": {"warm": "暖调米白旧纸"}},
    "segments": {"preserve": "p", "creative": "c", "forbid": "f"},
}
w = preflight(
    spec=fam_lock, params={"palette_mode": "warm"},
    segments={"creative": "完全不相干的文案"}, prompt="完全不相干的文案",
    locked=["palette_mode"],
)
check("锁定参数取值未出现在提示词 → 报警",
      any("USER-LOCKED" in x for x in w), str(w))

w = preflight(
    spec=fam_lock, params={"palette_mode": "warm"},
    segments={"creative": "采用暖调米白旧纸"}, prompt="采用暖调米白旧纸",
    locked=["palette_mode"],
)
check("锁定参数取值已落地 → 不报", not w, str(w))

# ── 测试 4：过载与条数 ──────────────────────────────────
print("\n[测试 4] Prompt 过载 / forbid 条数（造梦师 preflight #9）")

w = preflight(
    spec={},
    segments={"preserve": "保" * 1000, "creative": "创" * 1000, "forbid": "禁" * 10},
)
check("三段总长超 1800 → 报警", any("提示词总长" in x for x in w), str(w))

w = preflight(
    spec={},
    segments={"forbid": "\n".join(f"禁止项{i}" for i in range(20))},
)
check("forbid 超 15 条 → 报警", any("forbid 条数" in x for x in w), str(w))

# ── 测试 5：漂移维度提示 ────────────────────────────────
print("\n[测试 5] 漂移维度提示（用视觉卡自己的 drift_warnings）")

w = preflight(spec={"card_drift_warnings": ["商业化", "CG 化", "媒介丢失"]},
              segments={"creative": "x"})
check("有 drift_warnings → 提示易跑偏维度", any("易跑偏" in x for x in w), str(w))

# ── 测试 6：★ 10 个真实家族必须零告警（防误报的底线）────
print("\n[测试 6] ★ 现有的 10 个家族全部零告警")

fams = load_families()
noisy = {}
for fid in sorted(fams):
    try:
        r = build_prompt(fid)
    except Exception as e:                      # 缺必填参数等属预期，不算告警
        continue
    pf = r.get("preflight") or []
    if pf:
        noisy[fid] = pf
check(f"10 个家族全部零 preflight 告警（实际加载 {len(fams)} 个）",
      not noisy, str(noisy)[:400])

check("build_prompt 返回体带 preflight 字段",
      "preflight" in build_prompt("zine"))

# ── 测试 7：Active Core Rules —— 老家族零回归 ───────────
print("\n[测试 7] ★ 无 card_rule_meta 的家族：门控行为逐字不变")

fam_rules = {
    "id": "rulesprobe", "name": "规则探针", "layout": "full", "forbid_scope": "whole",
    "hard_forbid": ["a", "b", "c", "d"], "allow_change": ["color"],
    "params": {},
    "segments": {"preserve": "p", "creative": "c", "forbid": "f"},
    "card_all_rules": [f"规则{i}：可观察关系描述" for i in range(1, 8)],   # 7 条
}
r4 = render_family(fam_rules, {}, {}, strict=False)
c4 = r4["segments"]["creative"]
check("旧行为保留：只注入前 5 条",
      "规则5" in c4 and "规则6" not in c4, c4[:120])
check("旧行为保留：带【视觉规则】块标题", "【视觉规则】" in c4)
check("无 meta 时不产生门控说明 warning",
      not any("视觉规则门控" in x for x in r4["warnings"]), str(r4["warnings"]))

# ── 测试 7b：★ 存量工坊家族（只有 card_all_rules）自动推导 ──
print("\n[测试 7b] ★ 无 card_rule_meta 的工坊家族：运行时自动推导轴")

# 模拟「本次改动之前」编译出的家族：有规则但没元数据。
# 把色彩/材质规则排在第 6、7 位 —— 位置截取会永久丢掉它们。
legacy_rules = [
    "构图：主体偏左下，大量留白",
    "空间：前景中景背景三层分明",
    "调度：次要元素沿边缘散布",
    "边缘：撕裂纤维边",
    "线条：轮廓干净无抗锯齿",
    "色彩：石灰白占四成，石板灰蓝作对比",   # ← 第 6 条
    "材质：手工网点与纸张纹理明显",          # ← 第 7 条
    "曝光：最亮处贴近纸白而不发光",
]
fam_legacy = dict(fam_rules)
fam_legacy["card_all_rules"] = legacy_rules
# 注意：不设 card_rule_meta
r_l = render_family(fam_legacy, {}, {}, strict=False)
c_l = r_l["segments"]["creative"]
check("★ 存量家族的色彩规则（第 6 条）被选中",
      "石灰白占四成" in c_l, c_l[:260])
check("★ 存量家族的材质规则（第 7 条）被选中",
      "手工网点" in c_l, c_l[:260])
check("自动推导保持安静（不产生门控告警噪音）",
      not any("视觉规则门控" in x for x in r_l["warnings"]), str(r_l["warnings"]))

# ── 测试 8：★ 按相关性选 —— 色彩/材质类恒在 ────────────
print("\n[测试 8] ★ 有 card_rule_meta：色彩/材质类规则必须入选")

# 故意把色彩/材质规则排在第 6、7 位 —— 旧的位置截取会永久丢掉它们
rules = [
    "构图：主体偏左下，大量留白",
    "构图：视线沿水平方向移动",
    "空间：前景中景背景三层分明",
    "调度：次要元素沿边缘散布",
    "边缘：撕裂纤维边",
    "色彩：石灰白占四成，石板灰蓝作对比",     # ← 第 6 条，决定像不像
    "材质：手工网点与纸张纹理明显",            # ← 第 7 条
    "曝光：最亮处贴近纸白而不发光",
]
meta = [
    {"text": rules[0], "axis": "composition", "weight": "conditional"},
    {"text": rules[1], "axis": "composition", "weight": "conditional"},
    {"text": rules[2], "axis": "spatial", "weight": "conditional"},
    {"text": rules[3], "axis": "blocking", "weight": "conditional"},
    {"text": rules[4], "axis": "material", "weight": "conditional"},
    {"text": rules[5], "axis": "color", "weight": "strong"},
    {"text": rules[6], "axis": "material", "weight": "strong"},
    {"text": rules[7], "axis": "exposure", "weight": "strong"},
]
fam_meta = dict(fam_rules)
fam_meta["card_all_rules"] = rules
fam_meta["card_rule_meta"] = meta

r5 = render_family(fam_meta, {}, {}, strict=False)
c5 = r5["segments"]["creative"]
check("★ 排在卡片第 6 位的色彩规则被选中（旧逻辑会丢）",
      "石灰白占四成" in c5, c5[:300])
check("★ 排在卡片第 7 位的材质规则被选中（旧逻辑会丢）",
      "手工网点" in c5, c5[:300])
check("选中条数不超过 5", c5.count("色彩：") + c5.count("材质：") <= 5)
check("产生门控说明 warning",
      any("视觉规则门控" in x for x in r5["warnings"]), str(r5["warnings"]))

# ── 测试 8b：★ 已被 creative 表达的规则不占名额（A/B 实测抓到的缺陷）──
print("\n[测试 8b] ★ 内容级去重：同义已表达的规则让位给新信息")

fam_dup = dict(fam_meta)
fam_dup["card_all_rules"] = [
    "构图：主体沿道路两侧由近及远缩小并指向消失点",      # creative 已有同义表述
    "色彩：暖色斜射光只照亮部分树干，其余保持中性灰蓝",  # 新信息
]
fam_dup["card_rule_meta"] = [
    {"text": fam_dup["card_all_rules"][0], "axis": "composition", "weight": "strong"},
    {"text": fam_dup["card_all_rules"][1], "axis": "color", "weight": "strong"},
]
# creative 里已含第一条规则的内容（无「构图：」前缀 → 精确匹配抓不到）
fam_dup["segments"] = {
    "preserve": "p",
    "creative": "让主体沿道路两侧由近及远缩小并指向消失点，保持纵深",
    "forbid": "f",
}
r_d = render_family(fam_dup, {}, {}, strict=False)
c_d = r_d["segments"]["creative"]
check("★ 已表达的构图规则不再注入（尽管是 strong）",
      "构图：主体沿道路两侧" not in c_d, c_d[:200])
check("带新信息的色彩规则正常注入",
      "暖色斜射光" in c_d, c_d[:200])
check("门控说明报出去重数量",
      any("去重已表达 1 条" in x for x in r_d["warnings"]), str(r_d["warnings"]))

# 全部规则都已表达 → 不注入任何规则块
fam_dup2 = dict(fam_dup)
fam_dup2["card_all_rules"] = ["构图：主体沿道路两侧由近及远缩小并指向消失点"]
fam_dup2["card_rule_meta"] = [{"text": fam_dup2["card_all_rules"][0],
                               "axis": "composition", "weight": "strong"}]
r_d2 = render_family(fam_dup2, {}, {}, strict=False)
check("全部规则已表达 → 不产生规则块",
      "【视觉规则】" not in r_d2["segments"]["creative"],
      r_d2["segments"]["creative"][:120])

# ── 测试 9：★ 换参数 → 选中的规则集合应该跟着变 ─────────
print("\n[测试 9] ★ 换一组参数，选中的规则应当不同")

fam_p = dict(fam_meta)
fam_p["params"] = {
    "palette_mode": {"type": "enum", "options": ["warm", "cool"], "default": "warm"},
}
fam_p["dicts"] = {
    "palette_mode": {"warm": "暖调米白旧纸", "cool": "冷调石板灰蓝"},
}
r_warm = render_family(fam_p, {"palette_mode": "warm"}, {}, strict=False)
r_cool = render_family(fam_p, {"palette_mode": "cool"}, {}, strict=False)
check("两组参数下都有色彩/材质规则（一票否决项恒在）",
      "石灰白占四成" in r_warm["segments"]["creative"]
      and "石灰白占四成" in r_cool["segments"]["creative"])

sel_warm, _ = select_active_rules(fam_p, rules, "", {"palette_mode": "warm"}, {})
sel_cool, _ = select_active_rules(fam_p, rules, "", {"palette_mode": "cool"}, {})
check("选中的规则是 3–5 条",
      3 <= len(sel_warm) <= 5 and 3 <= len(sel_cool) <= 5,
      f"warm={len(sel_warm)} cool={len(sel_cool)}")

# ── 测试 10：preflight 永不打挂出图 ─────────────────────
print("\n[测试 10] preflight 是旁挂检查，异常必须被吞掉")

class _Boom(dict):
    def get(self, *a, **k):
        raise RuntimeError("故意炸")

out = preflight(spec=_Boom(), segments={"creative": "x"})
check("spec 读取异常时不抛出，返回空列表", out == [], str(out))

# ── 测试 11：工坊侧的规则元数据推导 ────────────────────
print("\n[测试 11] ★ 视觉卡 → card_rule_meta 的推导（不给模型加输出负担）")

try:
    from services.style_forge import _derive_rule_meta
except Exception as e:                                  # 工坊依赖较重，缺了就跳过
    _derive_rule_meta = None
    print(f"  (跳过：style_forge 不可导入 —— {type(e).__name__})")

if _derive_rule_meta:
    card_demo = {
        "core_rules": [
            "构图：主体偏左下，大量留白",
            "色彩：石灰白占四成，石板灰蓝作对比色",
            "材质：手工网点与纸张纹理明显",
            "曝光：最亮处贴近纸白而不发光",
        ],
        "transfer_scope": {
            "strong": ["色彩结构", "材质处理", "曝光行为", "纹理与捕捉特性"],
            "conditional": [], "do_not": [],
        },
    }
    m = {x["text"][:5]: x for x in _derive_rule_meta(card_demo)}
    check("构图规则 → composition",
          m["构图：主体"]["axis"] == "composition", str(m.get("构图：主体")))
    check("色彩规则 → color",
          m["色彩：石灰"]["axis"] == "color", str(m.get("色彩：石灰")))
    check("★ 材质规则含「纸」但不被误判成 medium",
          m["材质：手工"]["axis"] == "material", str(m.get("材质：手工")))
    check("曝光规则 → exposure",
          m["曝光：最亮"]["axis"] == "exposure", str(m.get("曝光：最亮")))
    check("命中 strong 档的规则 → weight=strong",
          m["色彩：石灰"]["weight"] == "strong", str(m.get("色彩：石灰")))
    check("未命中 strong 档 → weight=conditional",
          m["构图：主体"]["weight"] == "conditional", str(m.get("构图：主体")))

    check("空规则表 → 空元数据", _derive_rule_meta({}) == [])

    # ★ 这两个是实测抓到的真 bug（10-05）：只看全文关键词会把轴判错
    #   「调度…沿画面边缘散布」里的"边缘"命中 material
    #   「材质…平面印刷感」里的"印刷"命中 medium（medium 在词表里排第一）
    #   判错 = 必选池选错人 = 阶段 B 的价值直接归零。现在改为优先看轴标签。
    tricky = {
        "core_rules": [
            "调度：次要元素沿画面边缘散布，不居中",
            "材质：手工网点与纸张纹理明显，平面印刷感",
            "边缘：撕裂纤维边，无抗锯齿的硬朗轮廓",
            "媒介：丝网印刷的平版插画，不是摄影",
        ],
        "transfer_scope": {"strong": ["色彩结构", "材质处理"], "conditional": [], "do_not": []},
    }
    tm = {x["text"][:2]: x for x in _derive_rule_meta(tricky)}
    check("★ 调度规则不被「边缘」误导成 material",
          tm["调度"]["axis"] == "blocking", str(tm.get("调度")))
    check("★ 材质规则不被「印刷」误导成 medium",
          tm["材质"]["axis"] == "material", str(tm.get("材质")))
    check("边缘规则 → material", tm["边缘"]["axis"] == "material", str(tm.get("边缘")))
    check("媒介规则 → medium", tm["媒介"]["axis"] == "medium", str(tm.get("媒介")))

# ── 测试 11b：★ USER-LOCKED 检查与渲染同口径（审查 P1-1 误报修复）──
print("\n[测试 11b] ★ dicts 文案带引导动词时不得误报「锁定未落地」")

# voxel_path_reflection.yaml 实锤的误报形态：渲染会剥掉「使用/让」等引导动词，
# 探针若用原始 desc 找「使用明亮蓝天天光」，渲染产物是「明亮蓝天天光」→ 必然误报
fam_verb = {
    "id": "verb", "name": "动词探针", "layout": "full", "forbid_scope": "whole",
    "hard_forbid": ["a", "b", "c", "d"], "allow_change": ["color"],
    "params": {},
    "dicts": {"light_mode": {"clear_day": "使用明亮蓝天天光，方向自左上"}},
    "segments": {"preserve": "p", "creative": "c", "forbid": "f"},
}
w = preflight(
    spec=fam_verb, params={"light_mode": "clear_day"},
    segments={"creative": "x"}, prompt="明亮蓝天天光，方向自左上",
    locked=["light_mode"],
)
check("★ 引导动词被剥后仍能匹配 → 零误报", not w, str(w))

w = preflight(
    spec=fam_verb, params={"light_mode": "clear_day"},
    segments={"creative": "x"}, prompt="完全无关的画面",
    locked=["light_mode"],
)
check("真的没落地时仍然要报", any("USER-LOCKED" in x for x in w), str(w))

# ── 测试 11c：阈值单一事实源（审查 P2-5）────────────────
print("\n[测试 11c] ★ preflight 与 renderer 共享同一份阈值常量")

import services.family_renderer as fr          # noqa: E402
import services.preflight as pf                # noqa: E402
check("过载阈值与 renderer 一致",
      pf._MAX_TOTAL_CHARS == fr._MAX_TOTAL_CHARS
      and pf._MAX_FORBID_CLAUSES == fr._MAX_FORBID_CLAUSES)
# `is` 对大整数不可靠（CPython 只缓存 -5..256），改用结构断言：
# preflight 源码里不允许再出现阈值字面量 —— 出现了就是又写了一份
_src = Path(pf.__file__).read_text(encoding="utf-8")
check("★ preflight 源码无阈值字面量（杜绝双份事实源）",
      "1800" not in _src and "超过 15 条" not in _src)

# ── 测试 12：返回体形状一致 ────────────────────────────
print("\n[测试 12] ★ 所有渲染源都必须带 preflight 字段（形状一致）")

from services.prompt_builder import available_sources, safe_build   # noqa: E402

missing_shape = []
for s in available_sources():
    r = safe_build(s["id"], None, None)
    if not isinstance(r, dict):
        continue
    if r.get("ok") is False:
        continue                                   # 缺必填参数等，不检查
    if "preflight" not in r:
        missing_shape.append(s["id"])
check(f"全部渲染源返回体都含 preflight（共 {len(available_sources())} 个源）",
      not missing_shape, str(missing_shape))

# ── 测试 13：载荷分级裁剪 —— 禁令永不截断 ────────────────
print("\n[测试 13] ★ 载荷分级：hard_forbid 永不截断")

from services.family_renderer import (                       # noqa: E402
    truncate_gracefully, NEVER_TRUNCATE_KEYS, MAX_RULE_CHARS,
)

long_forbid = [f"禁止项{i}：这是一段很长的禁令描述用来测试截断行为" for i in range(30)]
fam_c = {
    "id": "cprobe", "name": "截断探针", "layout": "full", "forbid_scope": "whole",
    "hard_forbid": long_forbid, "allow_change": ["color"],
    "params": {"note": {"type": "string", "default": ""}},
    "segments": {"preserve": "保留{{note}}", "creative": "创作{{note}}",
                 "forbid": "{{hard_forbid_joined}}"},
}
raw_len = len("、".join(long_forbid))
r_c = render_family(fam_c, {"note": "短"}, {}, strict=False)
fb = r_c["segments"]["forbid"]
check(f"hard_forbid {raw_len} 字完整保留（旧代码同样安全，现在改为显式白名单）",
      len(fb) == raw_len and "…" not in fb, f"{len(fb)} vs {raw_len}")
check("NEVER_TRUNCATE_KEYS 显式包含禁令载体键",
      {"hard_forbid_joined", "dynamic_forbid"} <= set(NEVER_TRUNCATE_KEYS))

# ── 测试 14：优雅截断 —— 在分句边界落刀 ─────────────────
print("\n[测试 14] 优雅截断（不把句子砍成半截）")

t = "第一句说主体轮廓。第二句说色彩关系。第三句说材质纹理。第四句被砍断的地方"
g = truncate_gracefully(t, 24)
# 落刀点之后紧跟着的应该是原文的某个分句标点（实现会吃掉该标点再加「…」）
cut_at = len(g) - 1
check("落刀在分句边界（原文紧邻字符是分句标点，未把句子砍成半截）",
      g.endswith("…") and t[cut_at:cut_at + 1] in "。！？；，、", repr(g))
check("截断后长度不超过上限", len(g) <= 24, repr(g))
check("未超限时不改动", truncate_gracefully(t, 9999) == t)
check("无标点长串退回硬截断但仍标「…」",
      truncate_gracefully("啊" * 500, 100).endswith("…"))

# ── 测试 15：用户自定义提示词不截断（产品原则，2026-10-05 用户确认）──
print("\n[测试 15] ★ 用户自定义提示词：不截断（精准优先），仅硬顶防滥用")

mid = "用户认真写的创作要求。" * 60        # 660 字，旧逻辑会砍到 300
b_mid = build_prompt("zine", {}, {}, extra_prompt=mid)
check("660 字的自定义要求完整落地（旧逻辑会砍到 300）",
      len(mid) - len(b_mid["segments"]["creative"]) < 400,
      f"creative={len(b_mid['segments']['creative'])} 原文={len(mid)}")
check("未超限时落地说明不含截断提示",
      "截断" not in (b_mid.get("extra_applied") or ""), str(b_mid.get("extra_applied")))

long_soft = "超长的创作要求。" * 300      # 2400 字：超建议长度但不超硬顶
b_soft = build_prompt("zine", {}, {}, extra_prompt=long_soft)
check("★ 超建议长度（2400 字）仍完整写入，不截断",
      long_soft in b_soft["segments"]["creative"],
      f"creative={len(b_soft['segments']['creative'])}")
check("★ 超建议长度只提醒不砍",
      "已完整写入" in (b_soft.get("extra_applied") or ""), str(b_soft.get("extra_applied")))

huge = "超长的创作要求。" * 1200         # 9600 字：超硬顶
b_huge = build_prompt("zine", {}, {}, extra_prompt=huge)
check("★ 超硬顶才截断，且醒目告知（绝不静默）",
      "硬上限" in (b_huge.get("extra_applied") or "")
      and "已截断" in (b_huge.get("extra_applied") or ""),
      str(b_huge.get("extra_applied")))

# ── 测试 16：超长视觉规则压缩而非整条丢弃 ────────────────
print("\n[测试 16] ★ 超长视觉规则：压缩保留，不整条丢弃（造梦师：Active Rules 不可牺牲）")

fam_r = dict(fam_c)
fam_r["card_all_rules"] = ["正常长度的规则一条", "超长的色彩规则" + "补充说明" * 80]
r_r = render_family(fam_r, {"note": ""}, {}, strict=False)
c_r = r_r["segments"]["creative"]
check("超长规则被压缩后保留（旧逻辑整条丢弃）",
      "超长的色彩规则" in c_r, c_r[:200])
check("压缩有 warning 说明",
      any("压缩" in x for x in r_r["warnings"]), str(r_r["warnings"]))
# 行首有「N. 」编号前缀，故留出 5 字余量
rule_lines = [x for x in c_r.splitlines() if "超长的色彩规则" in x]
check("压缩后不超过 MAX_RULE_CHARS（含编号前缀余量）",
      rule_lines and all(len(x) <= MAX_RULE_CHARS + 5 for x in rule_lines),
      str([len(x) for x in rule_lines]))

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
