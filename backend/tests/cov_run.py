"""覆盖率测量入口 —— 逐文件跑，最后合并。

    python tests/cov_run.py

★★ 为什么要单独一个脚本，而不是 `coverage run tests/run_all.py`：
  run_all.py 是用 **subprocess 逐个跑测试文件**的
  （它必须这样，因为要隔离各测试的环境变量并收集退出码）。
  而 coverage 默认**只测量自己所在的那个进程** ——
  于是 `coverage run run_all.py` 只会记录 runner 自己那几行，
  26 个测试文件跑出来覆盖率是 **0.0%**。

  ★ 这比"没有覆盖率数据"更坏：
    一个明显失真的数字会让人以为"项目没测试"，
    而真实情况是 957 项断言全绿。所以必须换方法，不能将错就错。

  正确做法：**让每个测试文件各自在 coverage 下运行**，
  用 `--parallel-mode` 落各自的数据文件，最后 `coverage combine` 合并。
  这样子进程里的代码是真被测过的。

  ★ 口径与 run_all.py 完全一致（同一份 FILES 列表）：
    否则「覆盖率」和「测试套件」就不是同一件事，两个数字无法互相印证。
"""
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

HERE = Path(__file__).resolve().parent
RCTOR = HERE.parent / ".coveragerc"

# ── 从 run_all.py 里读 FILES，避免两处各写一份而漂移 ──────────
def load_files():
    txt = (HERE / "run_all.py").read_text(encoding="utf-8")
    start = txt.index("FILES = [")
    end = txt.index("]", start)
    block = txt[start:end]
    out = []
    for line in block.splitlines():
        line = line.split("#")[0].strip().strip(",").strip('"').strip("'")
        if line.endswith(".py"):
            out.append(line)
    return out


FILES = load_files()
if not FILES:
    print("★ 没能从 run_all.py 解析出 FILES —— 不猜，直接退出")
    sys.exit(2)

# 先清掉上一次的并行数据，避免旧文件混进结果（那会让数字虚高）
for f in HERE.glob(".coverage.*"):
    f.unlink()
(HERE / ".coverage").unlink(missing_ok=True)

print(f"逐文件测量 {len(FILES)} 个测试文件…\n")
ok, bad = [], []
for fn in FILES:
    p = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "--parallel-mode",
         "--rcfile=" + str(RCTOR), str(HERE / fn)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if p.returncode == 0:
        ok.append(fn)
        print(f"  PASS {fn}")
    else:
        bad.append(fn)
        print(f"  FAIL {fn} 退出码 {p.returncode}")
        tail = (p.stdout or "").strip().splitlines()[-3:]
        for t in tail:
            print(f"        {t}")

print(f"\n测量完成：{len(ok)} 通过 / {len(bad)} 失败")
if bad:
    print("★ 有测试文件没跑成功，覆盖率数字会偏低 —— 不合并，先报错")
    sys.exit(1)

subprocess.run([sys.executable, "-m", "coverage", "combine", "--rcfile=" + str(RCTOR)],
               check=True)
subprocess.run([sys.executable, "-m", "coverage", "report", "--rcfile=" + str(RCTOR)],
               check=True)
