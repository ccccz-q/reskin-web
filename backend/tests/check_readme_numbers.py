"""校验 README 里的测试数字与实测是否一致

    python tests/check_readme_numbers.py

★★ 为什么需要这个脚本：
  本项目反复栽在同一种失真上 —— **文档里的数字与实测不符**：
    · 曾把「CI 是红的」写进 README，而实际全绿（审查 D1）
    · 测试数写25 文件/957 断言，实际 26/1150（审查 D2）
    · 2026-10-08 独立审查又查出 1018 写成 1150、覆盖率 68.1% 实为 68.2%
    · 我自己在同一天里把 test_config_boot 的 13 改成 19 后忘了更新表格，
      表内合计 1199 而实测 1205—— 差6 项。

  这些都不是"忘了改"这种低级失误的偶发，而是**没有校验机制**：
  人肉维护几十个数字，必然会漏。

  ⇒ 这个脚本把「README 的数字」变成**可自动验证的东西**。
    凡新增/修改测试文件后跑一次，不一致就退出码 1。
    ★ 与「文档不符=不可信」这个项目最大的失真来源直接对着干。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_BACKEND = _TESTS.parent
_ROOT = _BACKEND.parent
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def _run_all() -> str:
    """跑一次自测，返回它的完整输出（而不是靠读旧日志）。"""
    env = dict(os.environ)
    env.setdefault("DISABLE_IMAGE_GENERATION", "1")
    r = subprocess.run(
        [sys.executable, str(_TESTS / "run_all.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(_BACKEND), env=env,
    )
    return r.stdout or ""


print("[1] 跑一次自测拿实测数据（约 3-4 分钟）…")
out = _run_all()
if "全部通过" not in out:
    print("★ 自测本身没全绿，先修测试再谈文档数字。")
    print(out[-1500:])
    sys.exit(1)

# 从总览段解析：文件 → 断言数（无汇总行的记 None）
overview = out[out.index("总览"):]
real: dict[str, int | None] = {}
for line in overview.splitlines():
    m = re.match(r"\s+PASS\s+(\S+\.py)\s+([\d.]+)s\s+(.*)", line)
    if not m:
        continue
    n = re.search(r"结果：(\d+) 通过", m.group(3))
    real[m.group(1)] = int(n.group(1)) if n else None

no_summary = [f for f, v in real.items() if v is None]


def _count_pass_lines(fname: str) -> int:
    """对不打印汇总行的文件，数它的通过行（README 的 B 口径）。"""
    env = dict(os.environ)
    env.setdefault("DISABLE_IMAGE_GENERATION", "1")
    r = subprocess.run(
        [sys.executable, str(_TESTS / fname)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(_BACKEND), env=env, timeout=180,
    )
    return len(re.findall(r"^\s*(?:OK|✓|✅)", r.stdout or "", re.M))


print(f"\n[2] 实测：{len(real)} 个文件，其中 {len(no_summary)} 个不打印汇总行")

total_a = sum(v for v in real.values() if v is not None)
total_b = 0
for f in no_summary:
    c = _count_pass_lines(f)
    total_b += c
    real[f] = None# 保持 None 以示"B 口径"，数值单独记
total = total_a + total_b
print(f"    A 口径（有汇总行）= {total_a}")
print(f"    B 口径（数通过行）= {total_b}（{len(no_summary)} 个文件）")
print(f"    合计 = {total}")

print("\n[3] 核对 README")
readme = _ROOT / "README.md"
if not readme.exists():
    readme = _BACKEND.parent / "README.md"
txt = readme.read_text(encoding="utf-8")

sec = txt[txt.index("## 自测"):txt.index("### 覆盖率")] if "### 覆盖率" in txt else txt
table = {m[0]: int(m[1]) for m in
         re.findall(r"\|\s*`(test_\w+\.py)`\s*\|[^|]*\|\s*(\d+)\s*\|", sec)}

check(f"README 表里有 {len(table)} 行，测试文件实际 {len(real)} 个",
      len(table) == len(real), f"表 {len(table)} vs 实际 {len(real)}")

mismatch = [(f, table[f], real[f]) for f in table
            if isinstance(real.get(f), int) and table[f] != real[f]]
check("每个文件的断言数都对得上", not mismatch,
      f"不符 {len(mismatch)} 个")
for f, tab, act in mismatch:
    print(f"        ★ {f}: README={tab} 实际={act}")

missing = set(real) - set(table)
check("没有漏掉的文件", not missing, f"漏 {sorted(missing) if missing else '无'}")

tbl_sum = sum(table.values())
check("表内合计等于实测总数", tbl_sum == total,
      f"表内 {tbl_sum} vs 实测 {total}")

m = re.search(r"(\d+)\s*个文件\*\*全部通过\*\*，共\s*\*\*(\d+)\s*项断言", txt)
if m:
    check(f"首行「{m.group(1)} 个文件 / {m.group(2)} 断言」与实测一致",
          int(m.group(1)) == len(real) and int(m.group(2)) == total,
          f"实际 {len(real)} / {total}")
else:
    check("首行能找到「N 个文件 / M 断言」", False, "格式变了")

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("★ 若这里红了，**先改 README 数字再提交** —— "
      "文档与实测不符是本项目最大的信誉失分来源。")
sys.exit(1 if FAIL else 0)