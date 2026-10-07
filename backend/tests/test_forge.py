"""模板工坊验收 —— 从「风格理论 + 参考图」提炼家族，并支持迭代与入库

    python tests/test_forge.py

★ 为什么要单独一个文件
----------------------
工坊是本项目里唯一「由模型产出结构、再由本地校验器裁决」的路径。
这两者的交界处最容易出问题：模型给的东西不合规、本地存不下、
迭代换了 id 导致安装变成另一个家族。每一条都要钉住。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import services.context_store as cs  # noqa: E402
from services import forge_spec_guard as guard      # noqa: E402
import config                                                  # noqa: E402
import services.llm as llm_mod                                # noqa: E402
from services import style_forge as sf                         # noqa: E402

cs.SQLITE_PATH = Path(tempfile.mkdtemp(prefix="forge_")) / "f.db"

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


print("=== 1. 模板库 CRUD（这一节会直接命中「uuid 未导入」那个 NameError）===")
fid = cs.save_forge(lineage="ln1", version=1, spec={"id": "riso", "name": "孔版"},
                    name="孔版", prompt="测试提示词", theory="理论", feedback="")
check("存一版成功", bool(fid), str(fid))
row = cs.get_forge(fid)
check("能读回", row is not None and row["version"] == 1, str(row and row["version"]))
check("spec 被反序列化成 dict", isinstance(row.get("spec"), dict), str(type(row.get("spec"))))
check("images 被反序列化成 list", isinstance(row.get("images"), list))
check("family_id 正确", row.get("family_id") == "riso", str(row.get("family_id")))

check("下一版版本号 +1", cs.next_forge_version("ln1") == 2)
fid2 = cs.save_forge(lineage="ln1", version=2, spec={"id": "riso", "name": "孔版 v2"},
                     feedback="再暗一点")
check("同 lineage 共存两版", len(cs.list_forge(lineage="ln1")) == 2)
check("按版本号升序", [r["version"] for r in cs.list_forge(lineage="ln1")] == [1, 2])

cs.mark_forge_installed(fid2)
check("标记已安装", cs.get_forge(fid2)["installed"] == 1)
st = cs.forge_stats()
check("统计可读", st["saved"] == 2 and st["lineages"] == 1, str(st))
check("已安装计数", st["installed"] == 1, str(st))

check("删除一版", cs.delete_forge(fid2) is True)
check("删除后读不到", cs.get_forge(fid2) is None)
check("删除不存在的返回 False", cs.delete_forge("nope") is False)

print()
print("=== 2. 提示词装配（草稿还没安装，走渲染器而不是 build_prompt）===")
from services.family_renderer import render_family, render_to_prompt  # noqa: E402

spec = {
    "kind": "family", "id": "risograph_print", "name": "Risograph 孔版",
    "description": "鲜艳专色、网点质感、多色叠印错位",
    "icon": "◍", "layout": "full", "forbid_scope": "whole",
    "suitable": ["风景旅行照", "城市街拍"],
    "hard_forbid": ["连续调照片质感", "平滑渐变", "高光阴影立体渲染", "四色印刷网点"],
    "allow_change": ["color", "light", "detail_density"],
    "params": {},
    "segments": {
        "preserve": "保留原图的主体轮廓、人物姿态与景物相对位置。",
        "creative": "用 Risograph 孔版印刷的方式重画：每次只用一种专色，"
                    "靠网点密度表现明暗，多色叠印时允许轻微错位。",
        "forbid": "- 不许出现连续调照片质感\n- 不许出现平滑渐变",
    },
}
r = render_family(spec, {}, {}, strict=False)
prompt = render_to_prompt(r)
check("能渲染", bool(prompt) and len(prompt) > 40, f"{len(prompt)} 字")
check("三段都在", all(s in prompt for s in ("保留原图", "孔版", "不许出现")))

print()
print("=== 3. 校验器确实会拦住不合格的草稿 ===")
from services.family_renderer import validate_family, FamilyRenderError  # noqa: E402

bad_cases = {
    "缺 segments": {"id": "a", "layout": "full", "forbid_scope": "whole",
                    "hard_forbid": ["x"], "params": {}},
    "layout 非法": {**spec, "layout": "not_a_layout"},
    "forbid_scope 非法": {**spec, "forbid_scope": "nowhere"},
    "hard_forbid 非列表": {**spec, "hard_forbid": "不许出现渐变"},
    "segments 缺 forbid": {**spec, "segments": {"preserve": "a", "creative": "b"}},
}
for label, doc in bad_cases.items():
    try:
        validate_family(doc, source="test")
        check(f"拦住「{label}」", False, "竟然通过了")
    except FamilyRenderError:
        check(f"拦住「{label}」", True)

print()
print("=== 4. 正常草稿必须能通过校验 ===")
try:
    validate_family(spec, source="test")
    check("合格草稿通过校验", True)
except FamilyRenderError as e:
    check("合格草稿通过校验", False, str(e))

print()
print("=== 5. id 归一化（模型常给中文或带空格）===")
from services.style_forge import _normalize  # noqa: E402

for raw, expect in [
    ({"id": "Risograph Print"}, "risograph_print"),
    ({"id": "孔版印刷"}, "孔版印刷".lower()),
    ({"id": ""}, None),                       # 空则自动生成
    ({"id": "a-b c!"}, "a_b_c_"),
]:
    got = _normalize(raw)["id"]
    if expect is None:
        check("空 id 自动补一个", got.startswith("forged_"), got)
    else:
        check(f"id {raw['id']!r} → {got}", got == expect, f"期望 {expect}")

print()
print("=== 6. 参考图证据转文本 ===")
from PIL import Image  # noqa: E402
from services.style_forge import _reference_evidence  # noqa: E402

d = Path(tempfile.mkdtemp(prefix="ref_"))
p1 = d / "a.jpg"
Image.new("RGB", (800, 600), (240, 60, 40)).save(p1, quality=90)
ev, warns = _reference_evidence([str(p1)], use_vlm=False)
check("产生一条证据", len(ev) == 1)
check("含色板", bool(ev[0]["palette"]), str(ev[0]["palette"][:1]))
check("含朝向", ev[0]["orientation"] == "landscape")
check("本地档会给出提示", any("本地取色" in w for w in warns), str(warns))

ev2, _ = _reference_evidence([str(d / "missing.jpg")], use_vlm=False)
check("不存在的图被跳过", len(ev2) == 0)

print()

print()
print("=== 7. ★ 参数名兜底（模型给中文键名时）===")
d = _normalize({
    "id": " Risograph Travel ",
    "params": {
        "套色方案": {"type": "enum", "options": ["双色怀旧", "三色海滨"],
                     "default": "三色海滨"},
        "dot_density": {"type": "enum", "options": ["coarse", "fine"],
                        "default": "coarse", "label": "网点密度"},
        "x!y": {"type": "bool"},
    },
})
check("id 被规整", d["id"] == "risograph_travel", d["id"])
keys = list(d["params"].keys())
check("中文键名不再是 key", "套色方案" not in keys, str(keys))
check("英文键名原样保留", "dot_density" in keys, str(keys))
check("非法字符被替换", "x_y" in keys, str(keys))
check("★ 原名保留在 label 里",
      d["params"]["dot_density"].get("label") == "网点密度"
      and any(v.get("label") == "套色方案" for v in d["params"].values()),
      str({k: v.get("label") for k, v in d["params"].items()}))

print()
print("=== 8. 单参数自带中文名时，schema 必须优先用它 ===")
from services.family_renderer import params_schema  # noqa: E402

spec2 = {
    "id": "riso2", "name": "孔版", "layout": "full", "forbid_scope": "whole",
    "hard_forbid": ["a", "b", "c", "d"], "allow_change": ["color"],
    "params": {
        "ink_layers": {"type": "enum", "label": "套色层数",
                       "option_labels": {"two": "双色", "three": "三色"},
                       "options": ["two", "three"], "default": "two"},
    },
    "segments": {"preserve": "p", "creative": "c", "forbid": "f"},
}
sch = params_schema(spec2)[0]
check("label 用草稿自带的", sch["label"] == "套色层数", sch["label"])
check("option_labels 用草稿自带的",
      sch["option_labels"] == {"two": "双色", "three": "三色"}, str(sch["option_labels"]))

print()
print()
print("=== 9. ★ 视觉卡三层派生（含 P1-2 回归：risk_notes 是 str）===")
from services.card_extractor import build_visual_card, check_observable_rules  # noqa: E402

# 复现审查 P1-2 的原始现场：risk_notes 是 str 而不是 list
card_str_risk = {
    "palette": [{"name": "米白", "hex": "#F0DDBF", "ratio": 0.6},
                {"name": "青", "hex": "#2E5E4E", "ratio": 0.3}],
    "orientation": "landscape",
    "light_hint": "中间调",
    "risk_notes": "画面含招牌文字与品牌标识",       # ← str，不是 list
}
vc = build_visual_card(card_str_risk)
check("core_rules 有内容", len(vc["core_rules"]) >= 3, str(len(vc["core_rules"])))
check("★ str 型 risk_notes 也能进 source_residue",
      any("招牌" in r or "文字" in r for r in vc["source_residue"]),
      str(vc["source_residue"]))
check("transfer_scope 三档齐全",
      all(k in vc["transfer_scope"] for k in ("strong", "conditional", "do_not")))
check("原图重绘语义：主体在强继承档",
      any("主体" in x for x in vc["transfer_scope"]["strong"]))

# 空卡边界
vc_empty = build_visual_card({})
check("空卡不崩", isinstance(vc_empty, dict))
check("空卡 core_rules 为空", vc_empty["core_rules"] == [])

# 单色 + ratio 缺失
vc_one = build_visual_card({"palette": [{"name": "青", "hex": "#2E5E4E"}]})
check("单色也有规则", len(vc_one["core_rules"]) >= 1, str(vc_one["core_rules"]))
check("ratio 缺失不崩", isinstance(vc_one["core_rules"], list))

print()
print("=== 10. ★ 形容词校验（可观察性规则）===")
check("命中「电影感」", "电影感" in check_observable_rules("很有电影感的画面"))
check("命中「胶片感」", "胶片感" in check_observable_rules("整张图都是胶片感"))
check("正常关系描述通过", check_observable_rules("高光接近纸白而不提亮整幅") == [])
check("「复古」在黑名单", "复古" in check_observable_rules("复古的色调"))

print()
print("=== 11. ★ _enforce_residue（残留拦截）===")
from services.style_forge import _enforce_residue  # noqa: E402

spec0 = {"segments": {"preserve": "p", "creative": "c", "forbid": "- 不许出现文字水印"}}
spec0["hard_forbid"] = ["完整描摹照片"]

# 残留已覆盖 → 不追加
r0 = _enforce_residue(spec0, {"source_residue": ["文字水印"]})
check("已覆盖则不动", "不得出现" not in r0["segments"]["forbid"])

# 残留未覆盖 → 自动补进 forbid
spec1 = {"segments": {"preserve": "p", "creative": "c", "forbid": "- 不许出现文字水印"},
         "hard_forbid": ["完整描摹照片"]}
r1 = _enforce_residue(spec1, {"source_residue": ["画面中的招牌文字与品牌标识", "门口的石狮子"]})
check("★ 未覆盖的残留自动补进 forbid",
      "招牌文字" in r1["segments"]["forbid"] and "石狮子" in r1["segments"]["forbid"],
      r1["segments"]["forbid"][-60:])

# 残留含 {} → 消毒（否则变成未解析占位符，_check 会报错并烧自修轮）
spec2 = {"segments": {"preserve": "p", "creative": "c", "forbid": "f"},
         "hard_forbid": ["a", "b", "c", "d"]}
r2 = _enforce_residue(spec2, {"source_residue": ["不要出现 {logo} 标志\n第二行"]})
check("★ 残留里的 {} 被消毒（不会变成占位符）",
      "{" not in r2["segments"]["forbid"], repr(r2["segments"]["forbid"][-50:]))

# hard_forbid 是 list 且含非 str → 不崩
r3 = _enforce_residue({"segments": {"preserve": "p", "creative": "c", "forbid": "f"},
                       "hard_forbid": ["x", 123, None]},
                      {"source_residue": ["未覆盖的残留项"]})
check("hard_forbid 含非 str 不崩", isinstance(r3["segments"]["forbid"], str))

# segments.forbid 是 list → 不崩且追加成功
spec4 = {"segments": {"preserve": "p", "creative": "c", "forbid": ["已有条目"]},
         "hard_forbid": ["x", "y", "z", "w"]}
r4 = _enforce_residue(spec4, {"source_residue": ["新的残留项"]})
check("forbid 为 list 时也正常追加", "新的残留项" in r4["segments"]["forbid"])

# 空 residue → 原样返回
check("空 residue 原样返回",
      _enforce_residue(spec0, {})["segments"] == spec0["segments"])

print()
print("=== 12. 视觉卡进 forge 返回（residue 拦截接线后）===")
fake_card = {"core_rules": ["规则一", "规则二", "规则三", "规则四", "规则五"],
             "source_residue": ["某品牌标识"]}
spec5 = {"id": "probe", "name": "探针", "layout": "full", "forbid_scope": "whole",
         "hard_forbid": ["a", "b", "c", "d"], "allow_change": ["color"],
         "params": {}, "segments": {"preserve": "p", "creative": "c", "forbid": "f"}}
spec6 = _enforce_residue(spec5, fake_card)
check("拦截后 forbid 包含残留", "某品牌标识" in spec6["segments"]["forbid"])

print()
print("=== 13. 造梦师建卡字段聚合（_synthesize）===")
from services.style_forge import _synthesize  # noqa: E402
d1 = {"core_rules": ["主体保持摄影质感", "草地连续"],
      "core_subjects": "戴墨镜的旅人",
      "spatial_invariants": ["方形画幅", "俯拍放射"],
      "semantic_nucleus": "人躺草地的静止片刻",
      "emotional_residue": "游戏化的休止感",
      "discard_list": ["远处栏杆", "天空"],
      "transformation_opportunities": ["像素道具可放大为节点"],
      "source_residue": ["水印"], "medium": {"primary": "摄影+像素混合"}}
d2 = {"core_rules": ["主体保持摄影质感", "俯拍构图"], "core_subjects": "躺卧人物",
      "spatial_invariants": ["方形画幅"], "semantic_nucleus": "静止的倒地瞬间",
      "emotional_residue": "休止感", "discard_list": ["天空"],
      "transformation_opportunities": [], "source_residue": [],
      "medium": {"primary": "摄影"}}
card = _synthesize([d1, d2], "像素风格")
check("语义核取共识", bool(card.get("semantic_nucleus")))
check("情感残留取共识", bool(card.get("emotional_residue")))
check("丢弃清单取并集", card.get("discard_list") == ["远处栏杆", "天空"])
check("转换机会取并集", card.get("transformation_opportunities") == ["像素道具可放大为节点"])
check("shared_grammar 仍由代码投票", bool((card.get("shared_grammar") or {}).get("core_subjects")))

print()
print("=== 14. QC 消失语境豁免（discard 类不误报矛盾）===")
from services.family_qc import qc_family
spec_qc = {"id": "tq", "name": "t", "layout": "full", "forbid_scope": "whole",
           "default_aspect": "origin", "allow_change": ["color"],
           "hard_forbid": ["不得出现远处栏杆", "不得添加无关水印",
                           "商业广告感", "模糊低质量纹理"],
           "params": {"tone": {"type": "enum", "label": "t",
                               "options": ["a"], "default": "a"}},
           "dicts": {"tone": {"a": "暗"}},
           "segments": {"preserve": "p",
                        "creative": "俯拍场景，远处栏杆和天空必须从画面中消失。",
                        "forbid": "禁止出现远处栏杆。"}}
_, qb, wq, _ = qc_family(spec_qc)
clash = [w for w in wq if "矛盾" in w]
check("discard 语境不误报矛盾", not clash, clash)

print()
print("=== 15. ★ 流程备注不进禁令清单（10-03「YOU DIED 反转」教训）===")
from services.style_forge import _enforce_residue as _er15  # noqa: E402

# 解构模型把观察备注写进残留 → 整条丢弃，正常禁令保留
spec_n = {"segments": {"preserve": "p", "creative": "c", "forbid": "f"},
          "hard_forbid": ["a", "b", "c", "d"]}
rn = _er15(spec_n, {"source_residue": [
    "参考人物的可识别服饰组合",                                    # 正常禁令 → 保留
    "画面中的具体人物及其可辨认服饰组合；面部细节未观察到",        # 备注 → 丢弃
    "“YOU DIED!”、分数、按钮文字均为画面中可见元素；是否保留应由后续任务要求决定。",  # 矛盾源 → 丢弃
    "人物面部细节不足以确认身份",                                  # 备注 → 丢弃
]})
fz = rn["segments"]["forbid"]
check("正常残留仍被追加", "可识别服饰组合" in fz, fz[-60:])
check("「未观察到」备注被丢弃", "未观察到" not in fz)
check("「是否保留应由后续」备注被丢弃", "是否保留应由后续" not in fz)
check("「不足以确认」备注被丢弃", "不足以确认" not in fz)

# 全部是备注 → 不追加任何东西
rn2 = _er15(spec_n, {"source_residue": ["品牌标识未观察到", "是否保留由后续决定"]})
check("全备注残留不动 forbid", "不得出现" not in rn2["segments"]["forbid"])

# QC ⑧：备注混进 forbid → blocker（最后一道闸）
spec_qc2 = {"id": "tn", "name": "t", "layout": "full", "forbid_scope": "whole",
            "default_aspect": "origin", "allow_change": ["color"],
            "hard_forbid": ["不得出现远处栏杆"],
            "params": {"tone": {"type": "enum", "label": "t",
                                "options": ["a"], "default": "a"}},
            "dicts": {"tone": {"a": "暗"}},
            "segments": {"preserve": "p",
                         "creative": "完整的死亡标题、分数、双按钮与准星界面。",
                         "forbid": "禁止出现远处栏杆。\n"
                                   "- 不得出现：“YOU DIED!”、分数、按钮文字、"
                                   "准星均为画面中可见元素；是否保留应由后续任务要求决定。"}}
_, nb, _, _ = qc_family(spec_qc2)
check("QC 拦下备注式禁令（blocker）",
      any("备注" in b for b in nb), nb)
# 正常禁令不误杀
_, nb2, _, _ = qc_family(spec_qc)
check("正常 forbid 不触发备注 blocker", not any("备注" in b for b in nb2), nb2)

print()
print("=== 16. ★ 手改提示词（prompt_override）全链路 ===")
# 用临时库，绝不碰真实 agent.db
import tempfile  # noqa: E402
import services.context_store as cs  # noqa: E402
_tmpdir = tempfile.mkdtemp(prefix="forge_t16_")
cs.SQLITE_PATH = Path(_tmpdir) / "t.db"
cs._initialized_for = None                 # 强制重新初始化（触发列迁移）
cs.init_db(force=True)

fid16 = cs.save_forge(lineage="t16", version=1, spec={"id": "t16f", "name": "T"},
                      prompt="自动渲染的提示词原文，长度足以超过二十个字的底线。")
check("新版本 prompt_override 默认为空",
      not (cs.get_forge(fid16).get("prompt_override") or ""))

check("写入手改提示词", cs.set_forge_prompt(fid16, "用户手改后的提示词全文，同样超过二十字底线。"))
row16 = cs.get_forge(fid16)
check("读取到手改提示词", row16.get("prompt_override") == "用户手改后的提示词全文，同样超过二十字底线。")
check("原自动渲染 prompt 不被覆盖", "自动渲染" in (row16.get("prompt") or ""))

check("清空=恢复自动渲染", cs.set_forge_prompt(fid16, ""))
check("恢复后 override 为空", not (cs.get_forge(fid16).get("prompt_override") or ""))
check("不存在的 id 返回 False", not cs.set_forge_prompt("nope", "x"))

# prompt_builder 短路：override 非空 → 直接返回文本，不渲染 spec
from services.prompt_builder import _render_via_family  # noqa: E402
fam = {"id": "t16f", "name": "T", "layout": "full", "forbid_scope": "whole",
       "allow_change": [], "params": {}, "dicts": {},
       "segments": {"preserve": "p", "creative": "c", "forbid": "f"},
       "prompt_override": "手改提示词全文，生成时应原样返回而不渲染三段式。"}
r16 = _render_via_family("t16f", fam, {}, {})
check("★ override 短路：prompt 原样返回", r16["prompt"].startswith("手改提示词全文"), r16["prompt"][:40])
check("★ override 短路带 warning", any("手动修改" in w for w in r16["warnings"]))
check("extra_prompt 仍追加到手改文本后",
      _render_via_family("t16f", fam, {}, {}, extra_prompt="追加内容")["prompt"]
      .endswith("追加内容"))
# 恢复自动 → 走渲染（补全结构必需字段，验证真的回到了三段式渲染）
fam2 = {**fam, "prompt_override": "", "hard_forbid": ["a", "b", "c", "d"]}
check("清空 override 后恢复自动渲染",
      _render_via_family("t16f", fam2, {}, {})["prompt"] != "手改提示词全文，生成时应原样返回而不渲染三段式。")

print()

# ══════════════════════════════════════════════════════════
# 模型输出形状容错（2026-10-07 端到端实测抓到的真缺陷）
# ══════════════════════════════════════════════════════════
# ★ 事故：工坊提炼跑到「编译家族模板」阶段（已花 170 秒、几十次调用）时，
#   forge_spec_guard._normalize 里`params.items()` 抛
#   AttributeError: 'list' object has no attribute 'items' ——
#   模型把 params 返回成了**列表**。整条流水线归零，用户只看到"提炼失败"。
#   模型输出形状有偏差是常态，这条断言就是不许它再变成事故。
print("\n[测试 6] params 形状容错（list / 非 dict / 缺失）")
_base = {"id": "x", "name": "测试家族", "segments": {"主体": "{{subject.name}}"},
         "hard_forbid": [], "suitable": [], "allow_change": []}

for label, params, expect_min in [
    ("正常 dict", {"笔触": {"type": "select", "options": ["a", "b"]}}, 1),
    ("★ 列表（事故现场）",
     [{"name": "笔触", "type": "select", "options": ["a", "b"]},
      {"name": "配色", "type": "select", "options": ["暖", "冷"]}], 2),
    # ★ 事故现场：列表项没有 name/id，只有"参数名"中文键或单键嵌套
    ("列表项用中文「参数名」",
     [{"参数名": "笔触", "type": "select", "options": ["a", "b"]},
      {"参数名": "配色", "type": "select", "options": ["暖", "冷"]}], 2),
    ("列表项是单键嵌套（{笔触:{...}}）",
     [{"笔触": {"type": "select", "options": ["a", "b"]}},
      {"配色": {"type": "select", "options": ["暖", "冷"]}}], 2),
    ("列表项只有 label", [{"label": "笔触", "type": "select"}], 1),
    ("列表但元素不是 dict", ["不是字典", 123], 0),
    ("params 是字符串（离谱形状）", "我给忘了", 0),
]:
    try:
        out = guard._normalize({**_base, "params": params})
        got = len(out.get("params") or {})
        check(f"{label} → 不抛异常且归一为 dict（{got} 项）",
              isinstance(out.get("params"), dict), str(type(out.get("params"))))
        check(f"{label} → 保留 {expect_min} 项参数",
              got >= expect_min, f"实际 {got}")
    except Exception as e:                                       # noqa: BLE001
        check(f"{label} → 不抛异常且归一为 dict", False,
              f"抛了 {type(e).__name__}: {e}")

check("缺 params 键也不炸（按空处理）",
      isinstance(guard._normalize(dict(_base)).get("params"), dict))

# ══════════════════════════════════════════════════════════
# 阶段超时与整条链路硬闸（2026-10-07）
# ══════════════════════════════════════════════════════════
# ★ 为什么要有：原来**只有文本降级路径**有超时（ANALYZE_TIMEOUT_SEC），
#   VLM 看图那条主路径完全不限时 —— 上游一卡，用户就是"转圈到天荒地旧"。
#   这组断言守着三件事：阈值真的接上了、降级真的会走、预算到点不丢草稿。
print("\n[测试 7] 阶段超时与总预算闸")
import inspect as _inspect                              # noqa: E402

check("config 里有VLM 单图超时且不小于实测最坏值(124s)",
      config.FORGE_VLM_TIMEOUT_SEC >= 124,
      f"{config.FORGE_VLM_TIMEOUT_SEC}s（实测并发 6 张时最慢一次 124.6s）")
check("编译超时 >= 实测 45s 的 2 倍", config.FORGE_COMPILE_TIMEOUT_SEC >= 90,
      f"{config.FORGE_COMPILE_TIMEOUT_SEC}s")
check("自修轮超时 >= 实测 70s", config.FORGE_REPAIR_TIMEOUT_SEC >= 70,
      f"{config.FORGE_REPAIR_TIMEOUT_SEC}s")
check("总预算闸默认开启（>0）", config.FORGE_TOTAL_BUDGET_SEC > 0,
      f"{config.FORGE_TOTAL_BUDGET_SEC}s")

_vlm_src = _inspect.getsource(sf._vlm_json)
check("★ _vlm_json 真的把 timeout 传给了 vision()",
      "timeout=" in _vlm_src)
_light_src = _inspect.getsource(sf._light_json)
check("★ _light_json 真的把 timeout 传给了 chat()", "timeout=timeout" in _light_src)

# 降级路径：把 vision 打超时，验证"这张图降级但整条链路不死"
_orig_vision = llm_mod.vision
try:
    def _boom(*a, **k):
        raise llm_mod.LLMError("模拟上游卡住（触发超时）")
    llm_mod.vision = _boom
    # 直接调内部路径：_decode_single 会捕获并降级
    d = sf._decode_single("/nonexistent.png", None, {}, use_vlm=True, style_prompt="")
    check("★ 上游卡住时该张图降级而不是抛异常", isinstance(d, dict), str(type(d)))
except Exception as e:                                        # noqa: BLE001
    check("★ 上游卡住时该张图降级而不是抛异常", False,
          f"抛了 {type(e).__name__}: {e}")
finally:
    llm_mod.vision = _orig_vision

# ── 中止与预算闸：必须**离线**验证，不许发真实调用 ──
#★ 踩过的坑：第一版设FORGE_TOTAL_BUDGET_SEC=1 然后调 forge()，
#   想"撞预算闸"—— 但预算闸是在**阶段之间**检查的，
#   中间那两次意图解析（真实 LLM 调用）拦不住，
#   于是测试真的花了 60 秒、真的调了两次 API。
#   那��破坏了这个测试套件最核心的性质：**零真实调用**（README 里明写了）。
#   现在改成：用 cancelled 探针（ forge 第一行就会问）验证中止路径，
#   预算闸则用**源码静态断言**证明它接在 _stop() 上。
res = sf.forge("任意风格", cancelled=lambda: True)
check("cancelled 探针能立刻中止（离线，不发任何调用）",
      bool(res.get("cancelled")), str(res)[:80])
check("中止时不产出草稿（不冒充成功）", not res.get("spec"), str(res.get("spec"))[:60])

_src = _inspect.getsource(sf.forge)
check("★ 总预算闸接在中止探针 _stop() 里（全流程唯一探针，不会漏）",
      "_deadline" in _src and "FORGE_TOTAL_BUDGET_SEC" in _src)
check("★ 预算到点走「收尾」而非「取消」（草稿不丢）",
      "if _stop() and not _budget_hit:" in _src)
check("★ 预算到点会把实情写进 warnings（如实告知，不静悄悄）",
      "_budget_hit:" in _src and "warnings.append(msg)" in _src)

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
