"""渲染器测试 —— 重点验证「反推 forbid 两道过滤」与「占位符三个坑」

    python tests/test_renderer.py

★ 这个文件原本躺在 app/services/ 里（会跟着生产包一起被打包部署）。
  测试不该混进生产包，已迁到 tests/。
"""
import os
import sys
from pathlib import Path

# 让本文件既能被 pytest 收集，也能 python xxx.py 直接运行
_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from services.family_renderer import (  # noqa: E402
    load_families, render_family, render_to_prompt, resolve_allow_change,
    FamilyRenderError,
)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"\n      {detail}" if detail else ""))


# 一张"3 个人的风景照"提炼卡 —— 用来触发人数冲突
CARD = {
    "scene_type": "landscape",
    "has_person": True,
    "person_count": 3,
    "subject": {"name": "教堂塔楼", "weight": "核心主体"},
    "composition": {"horizon": "下三分之一", "perspective": "单点透视", "layers": ["石板路", "教堂", "山脊"]},
    "palette": [
        {"name": "石灰白", "hex": "#DCD7CC", "share": 0.42},
        {"name": "石板灰蓝", "hex": "#6E7A86", "share": 0.31},
    ],
    "light": {"dir": "右上 45°", "quality": "硬光"},
    "anchors": [
        {"type": "contour", "desc": "教堂塔楼的垂直轮廓", "materializable": True},
        {"type": "path", "desc": "石板路向纵深收敛的边线", "materializable": True},
    ],
    "risk_notes": ["前景有两个行人，易被模型增删"],
}

fams = load_families()
print(f"已加载 {len(fams)} 个家族\n")

# ── 测试 1：占位符三个坑 ──────────────────────────────
print("[测试 1] 占位符三个坑")
r = render_family(fams["zine"], {"abstraction": "high", "photo_ratio": 0.4}, CARD)
t = r["segments"]["creative"]
check("坑① 格式串 {:.0%} 被解析", "75%-85%" in t or "%" in t and "{" not in t, t[:200])
check("坑② 点号占位符 {subject.name} 被解析", "教堂塔楼" in r["segments"]["preserve"])
check("坑③ block 展开后无残留占位符", not r["unresolved"], str(r["unresolved"]))
check("插图占比由 photo_ratio 派生", "60%" in t, t[:200])

# ── 测试 2：反推 forbid 的两道过滤 ────────────────────
print("\n[测试 2] 反推 forbid 的两道过滤")

# 2a. zine 的 forbid_scope=real_region → forbid 必须带作用域标记
zine_forbid = render_family(fams["zine"], {}, CARD)["segments"]["forbid"]
check(
    "zine: 反推 forbid 带【撕纸窗口内的实景部分】作用域前缀",
    "【撕纸窗口内的实景部分】" in zine_forbid,
    zine_forbid,
)
check("zine: 人数约束出现在 forbid 里", "不得改变人物数量" in zine_forbid, zine_forbid)

# 2b. split_poster 的 allow_change 含 figure_add + detail_density
#     → 人数/地平线约束应被过滤（下半区本来就要重新抽象人群）
sp_forbid = render_family(fams["split_poster"], {}, CARD)["segments"]["forbid"]
check(
    "★ split_poster: 人数约束被 allow_change(figure_add) 过滤",
    "不得改变人物数量" not in sp_forbid,
    sp_forbid,
)
check(
    "★ split_poster: 光源约束保留（light 不在其 allow_change 里）",
    "光源方向" not in sp_forbid or "【" in sp_forbid,
    sp_forbid,
)

# 2b-1. ★ 最重要的一条：split_poster 的 allow_change 含 detail_density，
#       曾经把 identity 类约束（主体可辨认）一并过滤掉 → 保真彻底失守。
#       identity 维度必须豁免白名单，任何风格都无权改动「这还是不是同一张照片」。
check(
    "★★ split_poster: identity 约束豁免白名单（主体必须可辨认）",
    "教堂塔楼必须完整、清晰、可辨认" in sp_forbid,
    sp_forbid,
)
check(
    "★★ split_poster: risk_notes 也按 identity 处理（不被过滤）",
    "前景有两个行人" in sp_forbid,
    sp_forbid,
)
check(
    "★ split_poster: 地平线约束被 allow_change(detail_density) 过滤",
    "地平线高度" not in sp_forbid,
    sp_forbid,
)

# 2b-2. second_world 的 allow_change 不含 color/light → 这两类约束应保留
sw_forbid = render_family(fams["second_world"], {}, CARD)["segments"]["forbid"]
check(
    "second_world: 光源约束保留（light 不在其 allow_change 里）",
    "光源方向" in sw_forbid,
    sw_forbid,
)
check(
    # P1-4 之后 second_world 默认 fusion、forbid_scope: whole——
    # whole 的作用域前缀是空串：动态 forbid 不带【】标记，直接以约束正文开头
    "second_world: whole 作用域无【】前缀（P1-4 整幅化后）",
    not sw_forbid.startswith("【") and sw_forbid.startswith("避免："),
    sw_forbid[:60],
)

# 2c. ★ 关键：rembrandt_dark 要求"单侧硬光"，反推的"不得改变光源方向"必须被跳过
restyle = fams["full_restyle"]
ac_rembrandt = resolve_allow_change(restyle, {"style": "rembrandt_dark"})
ac_natgeo = resolve_allow_change(restyle, {"style": "national_geo"})
check("full_restyle 动态白名单生效（rembrandt ≠ natgeo）", ac_rembrandt != ac_natgeo,
      f"rembrandt={ac_rembrandt} natgeo={ac_natgeo}")
check("rembrandt_dark 白名单含 light", "light" in ac_rembrandt)

rb_forbid = render_family(restyle, {"style": "rembrandt_dark"}, CARD)["segments"]["forbid"]
check(
    "★ rembrandt_dark: 「不得改变光源方向」被白名单过滤掉",
    "光源方向" not in rb_forbid,
    rb_forbid,
)
check("★ rembrandt_dark: 「不得改变主色」被白名单过滤掉", "主色" not in rb_forbid, rb_forbid)

ng_forbid = render_family(restyle, {"style": "national_geo"}, CARD)["segments"]["forbid"]
check(
    "★ national_geo: 「不得改变主色」保留（color 也在白名单里，同样过滤）",
    "主色" not in ng_forbid,
    ng_forbid,
)
check(
    "national_geo: 人数约束不受白名单影响（figure_add 不在白名单）",
    "不得改变人物数量" in ng_forbid,
    ng_forbid,
)

# 2d. surreal_collage 的 forbid_scope=subject
# 作用域标记现在按家族正文语言给：中文家族给【主体本身】，英文家族给 [the subject itself]，
# 避免「【主体本身】main subject must remain...」这种混排。两者等价，任一命中即通过。
sc_forbid = render_family(fams["surreal_collage"], {}, CARD)["segments"]["forbid"]
check(
    "surreal_collage: 带作用域前缀（英文家族用 [the subject itself]）",
    "【主体本身】" in sc_forbid or "[the subject itself]" in sc_forbid,
    sc_forbid,
)

# ── 测试 3：病句检测 ─────────────────────────────────
print("\n[测试 3] 拼接病句")
sw0 = render_family(fams["second_world"], {"figures": "0"}, CARD)["segments"]["creative"]
check("figures=0 时不出现「小人不是装饰」", "小人不是装饰" not in sw0, sw0[-300:])
sw3 = render_family(fams["second_world"], {"figures": "0_3"}, CARD)["segments"]["creative"]
check("figures=0_3 时出现互动描述", "使用者" in sw3 or "互动" in sw3)

# ── 测试 4：必填参数 ─────────────────────────────────
print("\n[测试 4] 必填参数与降级")
# portrait_epic 已按用户要求下线，这里用内联合成家族验证同一条路径
# （render_family 直接吃 dict，不需要真实文件）
REQUIRED_FAMILY = {
    "id": "required_probe", "name": "必填探针", "layout": "full",
    "forbid_scope": "whole", "hard_forbid": ["a", "b", "c", "d"],
    "allow_change": ["color"],
    "params": {
        "school": {"type": "string", "required": True},
        "major": {"type": "string", "required": True},
    },
    "segments": {
        "preserve": "p", "creative": "毕业于{school}的{major}", "forbid": "f",
    },
}
try:
    render_family(REQUIRED_FAMILY, {}, CARD)
    check("缺必填参数应报错", False, "未抛异常")
except FamilyRenderError as e:
    check("缺必填参数报错", "school" in str(e), str(e))

pe = render_family(REQUIRED_FAMILY, {"school": "某某大学", "major": "计算机"}, CARD)
check("补齐后可渲染", "某某大学" in pe["segments"]["creative"], pe["segments"]["creative"][:200])

# ── 测试 5：全家族 × 全变体渲染冒烟 ──────────────────
print("\n[测试 5] 全家族 × 预设变体渲染冒烟")
for fid, fam in fams.items():
    for v in fam.get("variants", []):
        params = dict(v.get("params") or {})
        if fam.get("params", {}).get("school", {}).get("required"):
            params.setdefault("school", "测试大学")
            params.setdefault("major", "测试专业")
        try:
            res = render_family(fam, params, CARD)
            prompt = render_to_prompt(res)
            ok = prompt and not res["unresolved"]
            check(f"{fid}/{v['id']} ({len(prompt)}字)", ok, str(res["unresolved"]))
        except FamilyRenderError as e:
            check(f"{fid}/{v['id']}", False, str(e))

# ── 测试 6：冲突检测 ─────────────────────────────────
print("\n[测试 6] 冲突检测")
zr = render_family(fams["zine"], {}, CARD)
check(
    "zine 的「人群合并成大色块」不会误报冲突（因 forbid 已带作用域）",
    not any("互斥" in w for w in zr["warnings"]),
    str(zr["warnings"]),
)

print(f"\n{'='*46}")
print()
print("=== 测试 13：★ USER-LOCKED / OPEN（参数锁定）===")
fam_locked = {
    "id": "lockprobe", "name": "锁定探针", "layout": "full", "forbid_scope": "whole",
    "hard_forbid": ["a", "b", "c", "d"], "allow_change": ["color", "light"],
    "params": {
        "substrate": {"type": "enum", "options": ["warm_ivory", "kraft"],
                      "default": "warm_ivory"},
        "mood": {"type": "string", "default": "安静"},
    },
    "dicts": {"substrate": {"warm_ivory": "暖米白纸", "kraft": "牛皮纸"}},
    "segments": {"preserve": "基底：{{substrate_desc}}",
                 "creative": "氛围是{{mood}}，基底是{{substrate_desc}}",
                 "forbid": "f"},
}
# 用户未动 mood → 默认"安静"生效
r1 = render_family(fam_locked, {"substrate": "warm_ivory"}, {}, strict=False)
check("未锁定时默认值生效", "安静" in r1["segments"]["creative"])
# 用户把 mood 清空并锁定 → 不补默认值，残句清理删掉该句
r2 = render_family(fam_locked, {"substrate": "warm_ivory", "mood": ""}, {}, strict=False,
                   locked=["mood"])
check("★ locked 参数不补默认值", "安静" not in r2["segments"]["creative"],
      repr(r2["segments"]["creative"]))
# 用户显式选 kraft → 正常生效（cover 无关）
r3 = render_family(fam_locked, {"substrate": "kraft"}, {}, strict=False)
check("用户显式值生效", "牛皮纸" in r3["segments"]["creative"])

print()
print("=== 测试 14：★ 核心规则门控（card_all_rules 只取 3-5 条进 prompt）===")
fam_rules = {
    "id": "rulesprobe", "name": "规则探针", "layout": "full", "forbid_scope": "whole",
    "hard_forbid": ["a", "b", "c", "d"], "allow_change": ["color"],
    "params": {},
    "segments": {"preserve": "p", "creative": "c", "forbid": "f"},
    "card_all_rules": [f"规则{i}：可观察关系描述" for i in range(1, 8)],   # 7 条
}
r4 = render_family(fam_rules, {}, {}, strict=False)
check("只注入前 5 条（门控）",
      "规则5" in r4["segments"]["creative"] and "规则6" not in r4["segments"]["creative"],
      str(r4["segments"]["creative"])[:80])
check("带【视觉规则】块标题", "【视觉规则】" in r4["segments"]["creative"])
# 无 card_all_rules 的家族不受影响
r5 = render_family(fam_locked, {}, {}, strict=False)
check("普通家族不受影响", "【视觉规则】" not in r5["segments"]["creative"])

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
