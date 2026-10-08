"""spec_repair —— 「安装前体检与修复」的单元测试

    python tests/test_spec_repair.py

★ 为什么这个文件需要测试（2026-10-08 独立审查指出）：
  `services/spec_repair.py` 170 行、**覆盖率 0.0%** ——
  一行都没被单元测试执行过，端到端虽间接走过但没有任何断言针对它。
  而它的职责是「装得进去、用起来才炸」的最后一道闸：
  模型写坏的家族在这里被拦住，修不好的直接拒绝安装。
  ★ **一道决定"坏东西能不能装进系统"的闸门没有测试**，
  是比覆盖率数字难看得多的问题。

★ 本测试只打纯函数（`repair_spec` / `_repair_placeholders` / `_norm_double_braces`），
  不调模型、不落盘、不花钱。

覆盖的五类缺陷形态，全部来自该模块 docstring 里记录的**实测复现**形态：
  ① 双花括号 `{{subject.name}}` → 应归一为单花括号
  ② `option_labels` 写成 list（应为 dict）→ 应转成按位置配对的 dict
  ③ enum 缺 options / default 越界 → 应降级或修正
  ④ layout / forbid_scope 取值越界 → 应回退到允许值
  ⑤ hard_forbid 不足 4 条 /缺段 / allow_change 含未知维度 → 应补齐
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from services.spec_repair import (            # noqa: E402
    _norm_double_braces,
    _repair_placeholders,
    repair_spec,
)

PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


CARD = {
    "subject": {"name": "登机口老爷爷"},
    "anchors": [{"desc": "斜挎的布包"}, {"desc": "褪色军绿帽"}],
}

print("[测试 1] 双花括号归一（缺陷形态①）")
check("{{x}} → {x}", _norm_double_braces("{{subject.name}}") == "{subject.name}",
      _norm_double_braces("{{subject.name}}"))
check("普通单花括号不动", _norm_double_braces("{x}") == "{x}")
check("空输入不炸", _norm_double_braces("") == "")
check("None 也安全", _norm_double_braces(None) == "")

print("\n[测试 2] 占位符消解")
notes = []
# ⚠️ anchors 是**列表**且字段名是 desc —— 这一条踩过：
#   第一版按 card["anchor"]={"desc":...} 写断言，拿到的是默认文案「视觉锚点」。
seg = _repair_placeholders("画面里有{画面核心主体} 与{视觉锚点}", CARD, notes)
check("含「主体」的槽位被填为 Scene Card 主体", "登机口老爷爷" in seg, seg[:60])
check("含「锚点」的槽位被填为 anchors 列表内容",
      "布包" in seg and "军绿帽" in seg, seg[:60])
check("消解后不留任何 {xxx}", "{" not in seg, seg[:60])
check("修复动作写进了 notes（不静悄悄）", len(notes) >= 2, f"{len(notes)} 条")

notes2 = []
seg2 = _repair_placeholders("保留{figure_count_desc}", CARD, notes2)
check("已声明参数的占位符原样保留", "figure_count_desc" in seg2, seg2[:50])
check("合法槽位不产生 notes", notes2 == [], f"{notes2}")

notes3 = []
# 无占位符时必须**一字不改** —— 这是「修复层不擅自改正文」的保证
orig = "一座石桥，横跨平静水面"
seg3 = _repair_placeholders(orig, CARD, notes3)
check("无占位符时正文一字不改", seg3 == orig, seg3[:40])
check("无占位符时不产生 notes", notes3 == [])

notes4 = []
seg4 = _repair_placeholders("{{画面核心主体}}", CARD, notes4)
check("★ 双花括号的主语槽位也能消解（①②叠加的真实形态）",
      "登机口老爷爷" in seg4, seg4[:50])

check("空卡片时主语槽位有兜底文案",
      "主体" in _repair_placeholders("{画面核心主体}", {}, []),
      _repair_placeholders("{画面核心主体}", {}, [])[:30])

print("\n[测试 3] 必填字段与枚举回退（缺陷形态④）")
spec, notes = repair_spec({}, None)
check("★ 空 spec 也能修出可用的 id", bool(spec.get("id")), spec.get("id", ""))
check("缺 name 时用 id 兜底", spec.get("name") == spec.get("id"))
check("kind 默认 family", spec.get("kind") == "family", spec.get("kind"))
check("★ layout 越界回退为 full", spec.get("layout") == "full", spec.get("layout"))
check("forbid_scope 越界回退为 whole", spec.get("forbid_scope") == "whole",
      spec.get("forbid_scope"))
check("★ default_aspect 缺失补 origin（跟随原图）",
      spec.get("default_aspect") == "origin", spec.get("default_aspect"))
check("每次修复都留下说明", len(notes) >= 5, f"{len(notes)} 条")

spec2, _ = repair_spec({"layout": "乱七八糟", "forbid_scope": "???"}, None)
check("★ 非法的 layout 值被回退", spec2["layout"] == "full", spec2["layout"])
check("★ 非法的 forbid_scope 值被回退", spec2["forbid_scope"] == "whole")

print("\n[测试 4] allow_change / hard_forbid / segments（缺陷形态⑤）")
spec3, notes3 = repair_spec({"allow_change": ["color", "不存在的维度"]}, None)
check("★ allow_change 剔掉未知维度",
      "不存在的维度" not in spec3["allow_change"], str(spec3["allow_change"]))
check("保留合法维度", "color" in spec3["allow_change"], str(spec3["allow_change"]))

spec4, _ = repair_spec({"allow_change": "color"}, None)
check("allow_change 是字符串时归一为列表",
      isinstance(spec4["allow_change"], list), str(spec4["allow_change"]))

spec5, notes5 = repair_spec({"hard_forbid": ["只有一条"]}, None)
check("★ hard_forbid 补齐到至少 4 条", len(spec5["hard_forbid"]) >= 4,
      f"{len(spec5['hard_forbid'])} 条")
check("原有条目不被丢弃", "只有一条" in spec5["hard_forbid"])
check("补齐动作写进 notes", any("hard_forbid" in n for n in notes5))

spec6, _ = repair_spec({"hard_forbid": "不是列表"}, None)
check("hard_forbid 形状异常时安全处理",
      isinstance(spec6["hard_forbid"], list), str(type(spec6["hard_forbid"])))

spec7, notes7 = repair_spec({"segments": {"preserve": "保留原图主体"}}, None)
segs = spec7.get("segments") or {}
check("segments 三段齐全", isinstance(segs, dict) and len(segs) >= 3,
      f"{len(segs) if isinstance(segs, dict) else type(segs)}")
check("原有段内容不被覆盖",
      str(segs.get("preserve")) == "保留原图主体", str(segs.get("preserve"))[:30])
check("缺段被补的说明写进 notes", any("segment" in n.lower() for n in notes7),
      f"{len(notes7)} 条")

print("\n[测试 5] params 的形状与 enum 完整性（缺陷形态②③）")
spec8, notes8 = repair_spec(
    {"params": {"mode": {"type": "enum",
                         "options": ["a", "b", "c"],
                         "option_labels": ["标签A", "标签B", "标签C"]}}},
    None,
)
mode = (spec8.get("params") or {}).get("mode") or {}
check("★ option_labels 是 list 时被转成 dict",
      isinstance(mode.get("option_labels"), dict), str(mode.get("option_labels"))[:60])
check("★ 转成 dict 后按位置配对正确",
      (mode.get("option_labels") or {}).get("a") == "标签A",
      str(mode.get("option_labels"))[:60])
check("转换动作写进 notes", any("option_labels" in n for n in notes8))

spec9, notes9 = repair_spec({"params": {"weird": {"type": "enum"}}}, None)
weird = (spec9.get("params") or {}).get("weird") or {}
check("★ enum 缺 options 时降级为 string", weird.get("type") == "string",
      str(weird.get("type")))

spec10, _ = repair_spec(
    {"params": {"mode": {"type": "enum", "options": ["a", "b"], "default": "z"}}}, None)
mode10 = (spec10.get("params") or {}).get("mode") or {}
check("★ default 越界时被修正为某个合法选项",
      mode10.get("default") in ("a", "b"), str(mode10.get("default")))

spec11, notes11 = repair_spec({"params": {"bad": "不是字典"}}, None)
check("结构异常的参数被移除而不是留着炸", "bad" not in (spec11.get("params") or {}))
check("移除动作写进 notes", any("结构异常" in n for n in notes11))

print("\n[测试 6] 健壮性：任何输入都不能把体检本身弄挂")
for bad in [None, {}, {"params": None}, {"segments": None}, {"dicts": None},
            {"id": ""}, {"name": None}, {"hard_forbid": None}]:
    try:
        out, _n = repair_spec(bad, None)
        ok = isinstance(out, dict)
    except Exception as e:                                  # noqa: BLE001
        ok = False
        print(f"       ↑ {bad} 抛了 {type(e).__name__}: {e}")
    check(f"repair_spec({bad!r}) 不抛异常", ok)

try:
    out, _n = repair_spec({"params": {"m": {"type": "enum", "options": []}}}, {"subject": "字符串形态"})
    ok = isinstance(out, dict)
except Exception as e:                                      # noqa: BLE001
    ok = False
check("card.subject 是字符串时也能处理", ok)

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)