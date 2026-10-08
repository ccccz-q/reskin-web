"""自修处置决策（services/repair_decision.py）—— Agent 判断链的测试

    python tests/test_repair_decision.py

★ 为什么这个模块需要测试：
  它在**工坊主链路上**—— 每一次家族提炼的校验自修轮都会经过它。
  而且它引入了两个新风险面：
    ① **模型给出无效 action** → 会走错处置路径，白烧调用
    ② **决策函数自己抛异常** → 会把整条提炼链路带崩
  所以除了"判得准不准"，更必须测**"坏情况下不会把链路弄挂"**。
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

from services.repair_decision import classify_and_route   # noqa: E402

PASS, FAIL = 0, 0
VALID = {"code_repair", "normalize", "recompile",
         "creative_regen", "give_up", "none"}


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


print("[测试 1] 规则能判对常见形态（且不花钱）")
# ★ 用例必须**全部命中规则**，不得有一条依赖模型。
#   实测教训（2026-09 同步时暴露）：这一组里原先有
#   「参数 option_labels 是数组」→ normalize，
#   而这句话**不在规则词表里**，要真的调模型才能判 →
#   有模型时通过、无模型时 fallback 到 recompile → 失败。
#   ⇒ 后果是「测试结果随外部服务可用性而变」，
#     而自测套件的**全部价值就在于它永远可复现**。
#   ⇒ 现在只放规则能覆盖的形态；模型的判准由测试 4 用假 LLM 单独验。
cases = [
    (["未解析占位符 {figure_count_desc}"], "code_repair", "悬空占位符"),
    (["segments 里有悬空引用"], "code_repair", "悬空引用"),
    (["结构异常：不是字典"], "normalize", "形状异常"),
    (["hard_forbid 不足 4 条"], "recompile", "保真条目不足"),
    (["layout 值不在允许范围内"], "recompile", "枚举越界"),
    (["缺必填字段 description"], "recompile", "缺必填"),
    (["创意不足，视觉方案单薄"], "creative_regen", "创意不足"),
]
for errs, want, why in cases:
    d = classify_and_route(errs)
    check(f"{why} → {want}", d["action"] == want,
          f"得到 {d['action']}（来源 {d['source']}）")

print("\n[测试 2] 规则优先：命中时不调用模型")
d = classify_and_route(["未解析占位符 {xxx}"])
check("命中规则时 source=rule", d["source"] == "rule", d["source"])
check("规则命中时置信度为 high", d["confidence"] == "high", d["confidence"])

print("\n[测试 3] ★ 坏情况下绝不抛异常（主链路不能被它带崩）")
for bad in [None, [], "", ["", None], [123], {"x": 1}, ["错误"] * 50]:
    try:
        d = classify_and_route(bad)
        ok = isinstance(d, dict) and d.get("action") in VALID
    except Exception as e:                                       # noqa: BLE001
        ok = False
        print(f"       ↑ {bad!r} 抛了 {type(e).__name__}: {e}")
    check(f"classify_and_route({bad!r}) 不抛且给出合法 action", ok)

print("\n[测试 4] ★ 模型给出垃圾输出时的降级")
# 模拟模型返回无法解析的内容 / 非法action
import services.repair_decision as rd              # noqa: E402

_orig_chat = None
try:
    import services.llm as _llm
except Exception:                                             # noqa: BLE001
    _llm = None


def _install_fake_llm(reply):
    """把 llm.chat 换成假的，用来验证降级路径。"""
    import types
    fake = types.ModuleType("services.llm")
    fake.chat = lambda *a, **k: reply
    sys.modules["services.llm"] = fake


def _restore_llm():
    if _llm is not None:
        sys.modules["services.llm"] = _llm


for bad_reply, desc in [
    ("这不是 JSON", "模型返回非JSON"),
    ('{"action": "瞎写的动作"}', "模型给出非法 action"),
    ('{"action": 123}', "action 类型错"),
    ("", "模型返回空"),
]:
    _install_fake_llm(bad_reply)
    try:
        d = classify_and_route(["某个没见过的错误形态ABCDEF"])
        ok = isinstance(d, dict) and d.get("action") in VALID
    except Exception as e:                                    # noqa: BLE001
        ok = False
        print(f"       ↑ {desc} 抛了 {type(e).__name__}: {e}")
    finally:
        _restore_llm()
    check(f"{desc} → 仍给出合法 action", ok)


def _boom(*a, **k):
    raise RuntimeError("上游挂了")


_install_fake_llm(_boom)
try:
    d = classify_and_route(["没见过的形态XYZ"])
    ok = d["action"] in VALID
except Exception as e:                                        # noqa: BLE001
    ok = False
    print(f"       ↑ 模型调用抛异常时抛出: {e}")
finally:
    _restore_llm()
check("★ 模型调用抛异常时降级而非崩溃", ok,
      f"action={d.get('action')} source={d.get('source')}")

print("\n[测试 5] 空错误列表 → 不该修")
d = classify_and_route([])
check("errors=[] → action=none", d["action"] == "none", str(d["action"]))
check("errors=[] → 不花钱（source=rule）", d["source"] == "rule", d["source"])

print("\n[测试 6] ★★ 判据互斥性守卫（防复发）")
# 实测踩过：`creative_regen` 的词表含「不足」、`recompile` 也含「不足」，
# 于是「创意不足」被判成 recompile —— **因为 recompile 排在前面**。
# 这类 bug 不会报错，只会让决策静默地永远走向第一个分支。
from services.repair_decision import _RULES          # noqa: E402

seen = {}
dup = []
for action, kws, _ in _RULES:
    for k in kws:
        if k in seen and seen[k] != action:
            dup.append((k, seen[k], action))
        seen[k] = action
check("★ 判据词表互不重叠", not dup,
      f"重叠 {len(dup)} 处：{dup[:3]}" if dup else f"{len(seen)} 个关键词无重叠")

# ★ 更强的一条：同一句错误不得被两个分支同时"命中"
#   （词表不重叠是必要条件，这里再验证实际判别不串）
#   ⚠️ 注意：不是每句错误都会被规则命中——
#   「参数 option_labels 是数组」不在任何词表里，它是**模型**判成 normalize 的
#   （source=model）。所以这里只要求「至多命中一个」，不要求「必须命中」。
probe = [
    (["创意不足，视觉方案单薄"], "creative_regen"),
    (["hard_forbid 不足 4 条"], "recompile"),
    (["option_labels 是数组，已归一为列表"], "normalize"),
    (["未解析占位符 {x}"], "code_repair"),
]
for errs, want in probe:
    d = classify_and_route(errs)
    hit_actions = [a for a, kws, _ in _RULES if any(k in " ".join(errs) for k in kws)]
    check(f"「{errs[0][:14]}」至多命中一个分支且判为 {want}",
          len(set(hit_actions)) <= 1 and d["action"] == want,
          f"命中 {hit_actions or '无（交模型）'}，判为 {d['action']}"
          f"（{d['source']}）")

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)