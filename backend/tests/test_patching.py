"""测试依赖完整性 —— 防止「清理未使用导入」打坏测试打桩入口

    python tests/test_patching.py

★ 这份测试存在的理由是一起**真实发生过的事故**（2026-10-08）：

  我接入 ruff 清理 F401（未使用导入），把 tools/registry.py 里的
  `settle_generation` / `release_generation` 当成"未使用"删了，
  test_repair.py 立刻报：
      AttributeError: module 'tools.registry' has no attribute 'settle_generation'

  根因不是"我手抖"，是**静态检查的结构性盲区**：
    · ruff 判断一个导入是否"未使用"，只扫描**本文件内**的引用；
    · 但测试是通过 `模块.属性 = lambda...` 打桩的 ——
      这种访问在 ruff 眼里**根本不存在**。
    · 于是「被测试打桩的关键入口」在ruff 眼里 = 未使用 = 可删。
    · 而删掉之后，只有真正跑那个测试才会发现。

  ★ 最坏的形态不是"立刻红"，而是：
    某个名字**当前没有任何测试用到**，被删掉时全绿；
    三个月后有人写测试用到它 → 新测试红了，
    而真凶是三个月前那次「看起来很干净的清理」。
    写测试的人成了替罪羊。

所以这份测试的职责：**把「测试依赖了哪些产品模块的名字」显式钉住**，
让任何删导入的改动在提交前就被挡住。

覆盖两形态（都用 AST，不用正则 —— 正则漏匹配时是静默的）：
  ① `from services.x import name`      —— 直接 import 私有名
  ② `mod.attr = ...`                    —— 模块属性打桩
"""
import ast
import io
import os
import sys
from pathlib import Path

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

_TESTS = Path(__file__).resolve().parent
_APP = _TESTS.parent / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# 只关心这些顶层包 —— 测试引用的产品代码都在这里
_PRODUCT = ("services", "routers", "governance", "infra",
            "engine", "tools", "agents", "config", "llm")

PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def _mod_file(mod: str):
    return _APP / (mod.replace(".", "/") + ".py")


def _names_in(mod: str):
    """模块里出现的所有标识符（含定义与引用）。"""
    p = _mod_file(mod)
    if not p.exists():
        return None  # 模块不存在 —— 正常（只存在于私库/公开版其中之一）
    txt = io.open(p, encoding="utf-8").read()
    tree = ast.parse(txt)
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[-1])
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
    return out


print("=== 1. 收集测试对产品模块的依赖（AST，非正则）===")

direct = []      # (module, name, testfile, lineno)
alias = {}       # alias -> module
assigns = []     # (alias, attr, testfile, lineno)

for fn in sorted(os.listdir(_TESTS)):
    if not fn.endswith(".py"):
        continue
    p = _TESTS / fn
    tree = ast.parse(io.open(p, encoding="utf-8").read())

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and \
                node.module.split(".")[0] in _PRODUCT:
            for a in node.names:
                nm = (a.asname or a.name).strip()
                # ★ 只关心「原名被直接使用」的情况。
                #   `from services.style_forge import _resolve_unknown_slots as _rus`
                #   这类改名导入，as 之后的别名是**测试自己的局部名**，
                #   模块里本来就不该有 `_rus` —— 第一版没判asname，
                #   于是报了 6 处假失效。误报的检查等于没有检查。
                if a.asname:
                    # 仍需检查原始名字（import 本身必须存在）
                    orig = a.name.split(".")[-1]
                    nm_for_check = orig
                else:
                    nm_for_check = nm
                direct.append((node.module, nm_for_check, fn, node.lineno))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _PRODUCT:
                    alias[a.asname or a.name] = a.name
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    assigns.append((t.value.id, t.attr, fn, node.lineno))

print(f"  直接 import 产品名字：{len(direct)} 处")
print(f"  import 产品模块为别名：{len(alias)} 个")
print(f"  别名.属性 = ... 打桩：{len(assigns)} 处")

check("★ AST 确实收集到了依赖（不是静默的 0）",
      len(direct) + len(assigns) > 100,
      f"direct={len(direct)} assigns={len(assigns)}")

print("\n=== 2. 每一处直接 import 的名字都还存在 ===")
miss_direct = []
for mod, name, tf, ln in direct:
    names = _names_in(mod)
    if names is None:
        continue          # 模块只存在于另一库，跳过
    if name not in names:
        miss_direct.append((mod, name, tf, ln))
check(f"直接 import 的 {len(direct)} 处全部有效",
      not miss_direct,
      f"失效 {len(miss_direct)} 处")
for mod, name, tf, ln in miss_direct:
    print(f"       ★ from {mod} import {name}   ← {tf}:{ln}")

print("\n=== 3. 每一处模块属性打桩都还成立 ===")
# ★ 两种情况必须分开（第一版没分，报了 1 处假失效）：
#   (a) 模块文件里**静态存在**的名字 → 删了就是真事故，必须挡。
#   (b) 静态文件里没有、但测试在用 —— 通常是**运行时动态创建**的属性。
#       实例：test_concurrency 设好 os.environ["DISABLE_IMAGE_GENERATION"]="0"
#       之后 `config.DISABLE_IMAGE_GENERATION = 0` 才成立，
#       而 config.py 里根本没有这个名字（它来自 env 读取）。
#       这类**不该报失效** —— 否则检查开始制造噪声，
#       而噪声的真正代价是让人养成忽略警告的习惯（同 O5 立场）。
#   判定：静态找不到时，全大写命名= 配置项，几乎必然来自 env/动态注入 → 放行。
miss_attr = []
dynamic_ok = []
for a_name, attr, tf, ln in assigns:
    mod = alias.get(a_name)
    if not mod:
        continue
    names = _names_in(mod)
    if names is None:
        continue
    if attr in names:
        continue
    if attr.isupper():          # DISABLE_IMAGE_GENERATION / MAX_AGENT_STEPS 这类
        dynamic_ok.append((mod, attr, tf, ln))
        continue
    miss_attr.append((mod, attr, tf, ln))
n_checked = len([1 for a in assigns if alias.get(a[0])])
check(f"打桩用的 {n_checked} 处属性全部存在",
      not miss_attr,
      f"失效 {len(miss_attr)} 处；另 {len(dynamic_ok)} 处为运行时动态属性（已放行）")
for mod, attr, tf, ln in miss_attr:
    print(f"★ {mod}.{attr}   ← {tf}:{ln}")
if dynamic_ok:
    print("  以下为配置项（来自 env，静态文件里本就不存在，属正常）：")
    seen = set()
    for mod, attr, tf, ln in dynamic_ok:
        if (mod, attr) not in seen:
            seen.add((mod, attr))
            print(f"       · {mod}.{attr}")
for mod, attr, tf, ln in miss_attr:
    print(f"       ★ {mod}.{attr}   ← {tf}:{ln}")

print("\n=== 4. 回归锁定：本次事故的具体位置 ===")
# 这条断言看着冗余（上面已经查过），但它的作用是**把事故写进代码**：
# 后人若问「为什么这里不能删导入」，git blame 会指到这段注释。
reg = _names_in("tools.registry")
if reg is None:
    print("  （tools.registry 不存在，跳过）")
else:
    check("★ tools.registry.settle_generation 存在（test_repair 在打桩它）",
          "settle_generation" in reg,
          "2026-10-08 删掉它 → test_repair AttributeError")
    check("★ tools.registry.release_generation 存在（test_repair 在打桩它）",
          "release_generation" in reg,
          "同上")

print("\n=== 5. 本测试自身的自检（防止它自己坏了却报绿）===")
# 如果 AST 解析失败，上面会抛异常；这里确认「故意找不存在的东西」确实找不到，
# 证明 _names_in 的判定不是恒真。
fake = _names_in("tools.registry")
check("★ _names_in 对不存在的名字返回 False（判定不是恒真）",
      fake is not None and "__这个名字一定不存在__" not in fake)
check("★ _names_in 对不存在的模块返回 None（不是空集合）",
      _names_in("services.__无此模块__") is None)

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
