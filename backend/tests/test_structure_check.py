"""structure_check 测试 —— 结构保真校验（出图后自动跑的那道）

    python tests/test_structure_check.py

★ 为什么单独测它（而不是并入别的测试文件）：
  它在**主链路**上（每次出图后都会跑）。
  它要是抛异常，用户看到的是"点了没反应"——
  而它的本意只是"顺手检查一下"，不该有能力把生图搞挂。

⇒ 所以这个文件的第一优先级不是"测得准"，
  而是「**任何异常都不许抛出来**」—— 包括：
  · 图不存在
  · 图是坏的（内容不是图片）
  · 极端尺寸（1×1）
  · 入参是None / 非字符串
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "app"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import services.structure_check as sc   # noqa: E402

PASS, FAIL = 0, 0
TMP = Path(__file__).parent / "_sc_tmp"
TMP.mkdir(exist_ok=True)


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def _mk(path, size=(400, 400), color=(120, 140, 160), mode="RGB"):
    from PIL import Image
    Image.new(mode, size, color if mode == "RGB" else 128).save(path)
    return path


print("[测试 1] 正常情况：同图 → 高分；异图 → 明显更低")
a = _mk(TMP / "a.png")
b = _mk(TMP / "b.png")
r = sc.structure_score(a, a)
check("同一张图 → 相似度≈1.0", (r["structure_sim"] or 0) >= 0.99,
      f"得到 {r['structure_sim']}")
r2 = sc.structure_score(a, b)
check("结构相似度是 0~1 的数字",
      r2["structure_sim"] is None or 0.0 <= r2["structure_sim"] <= 1.0,
      f"得到 {r2['structure_sim']}")
check("返回含 status 字段", "status" in r2 and r2["status"] in
      ("pass", "warn", "fail", "unknown"), r2.get("status"))

print("\n[测试 2] ★★ 健壮性：任何坏输入都不许抛异常")
bad_cases = [
    ("不存在的原图", lambda: sc.structure_score(TMP / "nope.png", a)),
    ("不存在的成品", lambda: sc.structure_score(a, TMP / "nope.png")),
    ("两边都不存在", lambda: sc.structure_score(TMP / "x.png", TMP / "y.png")),
    ("入参是 None", lambda: sc.structure_score(None, None)),        # noqa
    ("入参是数字", lambda: sc.structure_score(123, 456)),
    ("入参是目录", lambda: sc.structure_score(TMP, TMP)),
]
for label, fn in bad_cases:
    try:
        r = fn()
        ok = isinstance(r, dict) and r.get("status") == "unknown"
        check(f"★ {label} → 不抛且标 unknown", ok, f"status={r.get('status')}")
    except Exception as e:                                     # noqa: BLE001
        check(f"★ {label} → 不抛且标 unknown", False,
              f"竟然抛了 {type(e).__name__}")

# 坏图：文件存在但不是图片
bad = TMP / "bad.png"
bad.write_bytes(b"this is definitely not a png")
try:
    r = sc.structure_score(bad, a)
    check("★ 坏图文件 → 不抛且标 unknown",
          r.get("status") == "unknown", r.get("verdict"))
except Exception as e:                                         # noqa: BLE001
    check("★ 坏图文件 → 不抛且标 unknown", False, f"抛了 {type(e).__name__}")

print("\n[测试 3] 判定入口：阈值与 should_regenerate")
r = sc.check_structure(a, a)
check("同图 → 不该重生成", r["should_regenerate"] is False, f"sim={r['structure_sim']}")
# 极端阈值：任何图都会被判要重生成
r = sc.check_structure(a, a, min_sim=1.0001)
check("阈值设为不可能达到 → should_regenerate=True",
      r["should_regenerate"] is True, f"sim={r['structure_sim']} vs {r['min_sim']}")
# ★ 测不出来时**不拦截**
r = sc.check_structure(TMP / "nope.png", a)
check("★ 测量失败 → 不拦截（宁可漏判不可判死）",
      r["should_regenerate"] is False, r.get("verdict"))
check("测量失败时 min_sim 仍回填", "min_sim" in r)

print("\n[测试 4] 阈值可配置（不硬编码在业务里）")
old = os.environ.get("STRUCTURE_MIN_SIM")
try:
    os.environ["STRUCTURE_MIN_SIM"] = "0.10"
    import importlib
    importlib.reload(sc)
    check("环境变量能改阈值", abs(sc.DEFAULT_MIN_SIM - 0.10) < 1e-9,
          f"得到 {sc.DEFAULT_MIN_SIM}")
finally:
    if old is None:
        os.environ.pop("STRUCTURE_MIN_SIM", None)
    else:
        os.environ["STRUCTURE_MIN_SIM"] = old
    importlib.reload(sc)

print("\n[测试 5] 清理")
try:
    for p in TMP.glob("*"):
        p.unlink()
    TMP.rmdir()
    check("临时目录已清理", not TMP.exists())
except Exception as e:                                         # noqa: BLE001
    check("临时目录清理", False, str(e))

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)