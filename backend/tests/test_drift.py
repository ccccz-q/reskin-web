"""漂移诊断（services/drift.py）—— 「AI 找问题」的纯逻辑测试

    python tests/test_drift.py

守护四件事：
 ① parse_drifts：模型多说一句客套话 / 返回脏 JSON / kind 写错，都不能把
   付过费的结果整段丢掉，也不能把坏数据原样透传给前端
 ② diagnose_drifts：视觉模型挂掉 / 图片预处理失败 → ok=false + 可照做的话，
   绝不抛异常（诊断是增强不是前提）
 ③ 归一化：change/why 截断、kind 纠偏、空 change 丢弃
 ④ 降级路径给的是「一句能照做的话」，不是模型原始报错
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import services.drift as drift                          # noqa: E402
from services.drift import diagnose_drifts, parse_drifts  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"\n      {detail}" if detail else ""))


# ── 测试 1：解析的健壮性 ─────────────────────────────────
print("[测试 1] parse_drifts：脏输入不许炸，更不许白花钱")
good = '{"drifts": [{"change": "人物恢复侧脸", "kind": "drift", "why": "模板没要求"}]}'
r = parse_drifts(good)
check("正常 JSON → ok + 1 条", r["ok"] and len(r["drifts"]) == 1, str(r))
check("字段原样保留", r["drifts"][0]["change"] == "人物恢复侧脸"
      and r["drifts"][0]["kind"] == "drift")

r = parse_drifts('好的，这是诊断结果：\n{"drifts": [{"change": "多了眼镜", "kind": "drift"}]}\n希望有帮助')
check("★ 模型带客套话也能抠出 JSON（别把付费结果丢掉）",
      r["ok"] and r["drifts"][0]["change"] == "多了眼镜", str(r))

r = parse_drifts('{"drifts": [{"change": "转向了", "kind": "侧倾"}, {"change": "", "kind": "drift"}, "垃圾"]}')
check("非法 kind → 归为 drift（宁可让人看见，不静默藏起来）",
      r["ok"] and r["drifts"][0]["kind"] == "drift")
check("空 change 的条目被丢弃", all(d["change"] for d in r["drifts"]), str(r))

import json as _json
five = _json.dumps({"drifts": [{"change": "x" * 200, "kind": "drift"}] * 5})
r = parse_drifts(five)
check("超过 3 条只取 3 条", r["ok"] and len(r["drifts"]) == 3)
check("change 超长被截断", all(len(d["change"]) <= drift.MAX_CHANGE_CHARS for d in r["drifts"]))

for bad in ["", None, "完全不是 JSON", "{\"a\": 1", "[]"]:
    r = parse_drifts(bad)
    check(f"脏输入 {bad!r:.20} → ok=false 且 error 可照做",
          not r["ok"] and "手写" in r["error"], str(r))

# ── 测试 2：diagnose_drifts 的降级路径 ───────────────────
print("\n[测试 2] 视觉模型挂掉 → ok=false，绝不抛异常")


def _boom(*a, **k):
    raise RuntimeError("vision channel down")


orig_vision = drift.__dict__.get("vision")
import services.llm as llm_mod                            # noqa: E402

img_a = str(Path(os.environ.get("TEMP", "/tmp")) / "drift_a.png")
img_b = str(Path(os.environ.get("TEMP", "/tmp")) / "drift_b.png")
from PIL import Image                                     # noqa: E402
Image.new("RGB", (64, 64), (200, 180, 160)).save(img_a)
Image.new("RGB", (64, 64), (30, 40, 50)).save(img_b)

orig_prep = None
import services.card_extractor as ce                      # noqa: E402
orig_prep = ce._prepare_for_vlm


def fake_prep(path):
    return ("ZmFrZQ==", "image/png")


ce._prepare_for_vlm = fake_prep

# 情形 A：vision 抛 LLMError → 降级
orig_vision_fn = llm_mod.vision
llm_mod.vision = _boom
r = diagnose_drifts(img_a, img_b)
check("视觉模型异常 → ok=false 不抛", r["ok"] is False and "手写" in r["error"], str(r))
check("降级返回带耗时", isinstance(r.get("elapsed_sec"), (int, float)))

# 情形 B：预处理失败（图片坏掉）→ 降级
llm_mod.vision = lambda *a, **k: '{"drifts": []}'
ce._prepare_for_vlm = lambda p: (_ for _ in ()).throw(OSError("broken image"))
r = diagnose_drifts(img_a, img_b)
check("图片预处理失败 → ok=false 不抛", r["ok"] is False and "手写" in r["error"], str(r))

# 情形 C：正常路径
ce._prepare_for_vlm = fake_prep
captured: dict = {}


def fake_vision(messages, **kw):
    captured["messages"] = messages
    captured["kw"] = kw
    return '{"drifts": [{"change": "建筑转向了", "kind": "drift", "why": "模板未要求"}]}'


llm_mod.vision = fake_vision
r = diagnose_drifts(img_a, img_b)
check("正常路径 → ok + 1 条", r["ok"] and len(r["drifts"]) == 1, str(r))
check("消息里是 [文本, 图A, 图B] 三段",
      len(captured["messages"][0]["content"]) == 3
      and captured["messages"][0]["content"][0]["type"] == "text")
check("response_format=json_object 传给了视觉模型",
      captured["kw"].get("response_format") == {"type": "json_object"})

# 情形 D：带家族上下文 —— 模板刻意添加的元素不许被误判成"建议修"
llm_mod.vision = fake_vision
r = diagnose_drifts(img_a, img_b, "趣味涂鸦叙述者｜黑线小人物围绕主体展开小故事")
first_text = captured["messages"][0]["content"][0]["text"]
check("★ 家族上下文进了判定提示词",
      "趣味涂鸦叙述者" in first_text and "本该添加或改变" in first_text)
r = diagnose_drifts(img_a, img_b, "")
check("空 hint 不炸、不注入模板段",
      r["ok"] and "本该添加或改变" not in captured["messages"][0]["content"][0]["text"])

# 情形 E：★ 内置模板专用口径 —— 不许拿"模板没要求"当理由（2026-10-05 实测反馈）
llm_mod.vision = fake_vision
diagnose_drifts(img_a, img_b, "材料印章｜把照片转成材料拼贴与印章质感", builtin=True)
t_builtin = captured["messages"][0]["content"][0]["text"]
check("★ 内置模板口径注入", "内置" in t_builtin and "材料印章" in t_builtin)
check("★ 明令禁止『模板没要求』这类模板内部理由",
      "禁止" in t_builtin and "模板没要求" in t_builtin)
check("★ 内置口径给出可判的替代标准（与原照片矛盾）",
      "与原照片本身矛盾" in t_builtin)

llm_mod.vision = fake_vision
diagnose_drifts(img_a, img_b, "我自己做的风格｜用户自建", builtin=False)
t_user = captured["messages"][0]["content"][0]["text"]
check("用户自建模板仍走通用口径（可以说模板要求）",
      "内置" not in t_user and "我自己做的风格" in t_user)

# 恢复
ce._prepare_for_vlm = orig_prep
llm_mod.vision = orig_vision_fn

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
