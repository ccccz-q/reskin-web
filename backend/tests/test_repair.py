"""repair_image 修复工具 —— 造梦师 Decode Repair 的最小实现

    python tests/test_repair.py

 守四件事：
 ① 修复提示词是「外科指令」：只说改什么 + 明令其余不动（绝不塞家族三段式）
 ② 参考图必须是**上一版成品**，不是用户原图
 ③ 治理三段式（预扣→结算/退还）在成功/失败路径都正确
 ④ 工具在 schema / SPEND / TERMINAL / IMPLEMENTATIONS 四处都注册齐了
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")   # 测试期间锁死花钱开关

_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import tools.registry as reg                             # noqa: E402
from tools.registry import ToolContext, tool_repair_image, _build_repair_prompt  # noqa: E402
from contracts.tools import SPEND_TOOLS                  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"\n      {detail}" if detail else ""))


def make_ctx(**kw) -> ToolContext:
    return ToolContext(thread_id="t-repair", **kw)


# ── 测试 1：修复提示词的形状 ────────────────────────────
print("[测试 1] 修复提示词 = 外科指令（不塞家族三段式）")
p = _build_repair_prompt("树冠恢复成方块体素拼接，不要圆润球体")
check("包含要修的那一处", "方块体素" in p)
check("带【保持不变】明令", "保持不变" in p and "一律不动" in p)
check("不包含家族三段式字样（preserve/forbid 不出现）",
      "preserve" not in p.lower() and "forbid" not in p.lower())
long_p = _build_repair_prompt("超长修复描述" * 100)
check("超长修正被截断到 300 字内（外科指令应一句话说得清）",
      len("超长修复描述" * 100) > 300 and "…" in long_p)

# ── 测试 1b：多处修正 + 既定约束（2026-10-05 用户实测反馈）──
print("\n[测试 1b] 多处修正逐条编号 + 既定约束重申")
p2 = _build_repair_prompt("删除顶部新增的卡通人物\n删除右上角英文文案及引导线")
check("两行 → 逐条编号 2 处", "2 处" in p2 and "2. 删除右上角" in p2, p2[:200])
check("多处仍然带【保持不变】明令", "保持不变" in p2)
p3 = _build_repair_prompt("a\nb\nc\nd\ne")
check("超过 3 条只取 3 条（改动太多不可控）", "3 处" in p3 and "4." not in p3)
cons = _build_repair_prompt("修一下", ["用户自定义要求：画面保持温暖色调",
                                    "家族硬禁令：不出现文字水印"])
check("既定约束被重申进修复指令", "必须继续遵守的既定约束" in cons
      and "保持温暖色调" in cons and "文字水印" in cons)
check("单处修正维持原来的 1 处措辞", "1 处" in p)
p_empty = _build_repair_prompt("   ")
check("空白输入不炸（兜底一条）", "1 处" in p_empty)

# ── 测试 2：参数与前置条件 ──────────────────────────────
print("\n[测试 2] 前置条件校验")
r = tool_repair_image({}, make_ctx())
check("缺 change → 报错并给 hint", not r.get("success") and "hint" in r, str(r)[:120])

ctx = make_ctx(artifacts={})
r = tool_repair_image({"change": "把树冠修成方块"}, ctx)
check("★ 没有生成结果时拒绝修复（修复对象是上一版成品）",
      "还没有可修复" in r.get("error", ""), str(r)[:120])

r = tool_repair_image({"change": "x"},
                      make_ctx(allow_spend=False,
                               artifacts={"image_path": __file__}))
check("预览模式 → 拒绝", "预览模式" in r.get("error", ""), str(r)[:120])

# ── 测试 3：★ 成功路径 —— 参考图必须是上一版成品 ─────────
print("\n[测试 3] ★ 成功路径：参考图 = 上一版成品（不是用户原图）")

calls: list[dict] = []


def fake_gen(**kw):
    calls.append(kw)
    return {"success": True, "url": "http://x/n.jpg",
            "image_path": "/tmp/new_image.jpg", "size": "1024x1024"}


orig_gen = reg.generate_image_with_reference
orig_reserve = reg.reserve_generation
orig_settle = reg.settle_generation
reg.generate_image_with_reference = fake_gen
reg.reserve_generation = lambda thread_id, reference_image=None: "tok"
reg.settle_generation = lambda *a, **k: {"left": 9}

import tempfile
last_generated = os.path.join(tempfile.gettempdir(), "repair_probe_last.jpg")
Path(last_generated).write_text("fake", encoding="utf-8")   # 让 os.path.exists 通过
ctx = make_ctx(image_path=os.path.join(tempfile.gettempdir(), "user_original.jpg"),
               artifacts={"image_path": last_generated,
                          "prompt": "旧家族提示词"})
try:
    r = tool_repair_image({"change": "树冠恢复方块体素"}, ctx)
finally:
    pass

check("修复成功", r.get("success") is True, str(r)[:120])
check("★ 参考图是上一版成品（不是用户原图）",
      calls and calls[0]["reference_image_path"] == last_generated,
      str(calls[0]["reference_image_path"] if calls else "未调用"))
check("★ 修复提示词里没有旧家族提示词（不重新解码重写）",
      calls and "旧家族提示词" not in calls[0]["prompt"])
check("新图顶替旧图成为最新生成结果",
      ctx.artifacts["image_path"] == "/tmp/new_image.jpg")
check("修复留痕（change 记入 artifacts）",
      ctx.artifacts.get("last_repair", {}).get("change") == "树冠恢复方块体素")
check("返回体要求模型如实转述修改内容",
      "只改了" in r.get("note", ""), str(r.get("note"))[:80])

# ── 测试 4：失败路径 —— 额度必须退还 ────────────────────
print("\n[测试 4] 生成失败 → 额度退还")

released: list = []


def fake_fail(**kw):
    return {"success": False, "error": "上游超时"}


reg.generate_image_with_reference = fake_fail
reg.release_generation = lambda thread_id, token, reason="": released.append(reason) or {"ok": 1}
# 测试 3 结束时 artifacts 指向假路径 /tmp/new_image.jpg（磁盘上不存在），
# 会被前置条件拦下 —— 指回真实存在的临时文件，才能走到生成与退额路径
ctx.artifacts["image_path"] = last_generated
r = tool_repair_image({"change": "再修一次"}, ctx)
check("失败返回 error", not r.get("success"), str(r)[:100])
check("★ 失败时额度已退还", len(released) == 1, str(released))

# ── 测试 5：注册完整性 ──────────────────────────────────
print("\n[测试 5] 四处注册齐全")
check("IMPLEMENTATIONS", "repair_image" in reg.IMPLEMENTATIONS)
check("TERMINAL_TOOLS（修完收尾，交给用户决定下一步）",
      "repair_image" in reg.TERMINAL_TOOLS)
check("SPEND_TOOLS（真实计费）", "repair_image" in SPEND_TOOLS)
from contracts.tools import TOOL_NAMES                   # noqa: E402
check("工具 schema 已声明（进入 TOOL_NAMES）", "repair_image" in TOOL_NAMES)

# 恢复
reg.generate_image_with_reference = orig_gen
reg.reserve_generation = orig_reserve
reg.settle_generation = orig_settle

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
