"""小助手知识索引 —— 文档与助手的「颗粒度对齐」守门测试

    python tests/test_helper_doc.py

背景：小助手的知识来自项目根的「使用说明.md」（唯一真源），但它**不把整篇塞进
prompt** —— 按 ## / ### 切成节，只注入命中的 1–3 节。于是有两处可能悄悄失配：

  ① 新功能写进了文档，但关键词表没登记 → 用户问"怎么修"取不到那一节，
     助手只能给概览，等于功能上线了却问不出来；
  ② 单节超过 900 字注入上限被截断 → 后半段内容（安装家族/示例图/世界观）
     永远进不了 prompt，助手会回答"没有这个功能"。

这个测试就守这两条：**问得到** + **不被截断**。
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("DISABLE_IMAGE_GENERATION", "1")

_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import importlib.util                                          # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "helper_router", _root / "routers" / "helper.py")
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)

from config import PROJECT_ROOT                                # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"\n      {detail}" if detail else ""))


DOC = (Path(PROJECT_ROOT) / "使用说明.md").read_text(encoding="utf-8")


def picked_titles(q: str) -> list[str]:
    out = helper._pick_sections(q)
    return [l.strip("【】") for l in out.splitlines() if l.startswith("【")]


# ── 测试 1：新能力必须"问得到" ────────────────────────────
print("[测试 1] 局部修复（新能力）能被检索到")
for q in ("生成的图树冠不对，怎么修？", "想改一处怎么做", "AI 能帮我找哪里不对吗",
          "修复要花额度吗", "修复和重新生成有什么区别"):
    titles = picked_titles(q)
    hit = any(t.startswith("四、生成与成品") or t.startswith("九、状态与额度")
              or t.startswith("十一、常见问题") for t in titles)
    check(f"「{q}」命中修复相关节", hit, str(titles))

# ── 测试 2：★ 打分不能被"节标题自匹配"霸榜 ────────────────
print("\n[测试 2] 记分只看提问（旧实现里第三节恒排第一的回归）")
titles = picked_titles("怎么把我做的风格装成家族")
check("装成家族 → 命中工坊的安装小节",
      any("安装为家族" in t for t in titles), str(titles))
check("不会只剩下「三、选风格与调参数」",
      titles != ["三、选风格与调参数"], str(titles))
titles = picked_titles("示例图在哪里设置")
check("示例图 → 命中工坊的示例图小节",
      any("示例图" in t for t in titles), str(titles))

# ── 测试 3：★ 单节不许超过注入上限（否则后半段被砍）────────
print("\n[测试 3] 没有超长节（>900 字会被截断）")
entries = helper._split_doc(DOC)
check("文档至少切出 10 节", len(entries) >= 10, str(len(entries)))
too_long = [(t, len(b)) for t, b in entries if len(b) > 900]
check("★ 每节都在 900 字注入上限内", not too_long, str(too_long))

# ── 测试 4：零命中时给概览且不超过预算 ────────────────────
print("\n[测试 4] 零命中的兜底")
out = helper._pick_sections("写段 Python 代码")
check("零命中会给概览", out.startswith("（以下为全部功能主题的概览"), out[:40])
check("概览有长度预算（≤3600 字）", len(out) <= 3600, str(len(out)))

# ── 测试 5：文档里写了的能力，关键词表要覆盖 ────────────────
print("\n[测试 5] 文档关键词覆盖度（新增章节必须登记关键词）")
missing = [t for t, _ in entries
           if " · " not in t and t not in helper._DOC_SECTION_KW]
check("每个一级章节都在关键词表里", not missing, str(missing))

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
