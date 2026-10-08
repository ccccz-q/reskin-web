"""生成参赛用的**大尺寸对比图**

    python tests/make_evidence_figures.py

★ 为什么单独一个脚本（而不是临时拼一下）：
  对比图是**参赛材料的一部分**，会被评委反复看。
  它必须**可复现**—— 图片坏了要能重新生成，而不是重做一遍。

★ 三种图，各自解决一个表达问题：

  ① **原图 vs 成品**（compare_1 / compare_2）
     回答「你的处理到底动了什么」——
     评委需要一眼看到"主体没变、风格变了"。

  ② **三段式消融**（ablation）
     回答「三段式契约是否真的有用」——
     A（无契约）/ B（仅保真）/ C（完整三段式）三列并排。

  ③ **能力轴对照**（axes）
     回答「你说的保真到底指什么」——
     同一张原图在不同风格下的结构保持情况，
     体现"结构忠实与风格无关，身份可辨识与风格相关"。

★ 尺寸与可读性（用户明确要求图要大、看得清）：
  · 单张原图 1536×1024，缩到 **420px 宽**才放进对比图——
    比常见的 200-260px 大一倍，投影时后排也能看清
  · 输出 **WebP quality=90**（体积约为 PNG 的 1/8，肉眼无损）
  · 每张图有**标签栏**（原图 / 家族名），不靠"看图猜"
  · 上下留白足够，避免贴边

★ 硬性纪律：
  **所有图必须来自真实跑出来的成品**（docs/gallery 与 docs/evidence/ablation）。
  本脚本**只做拼接与标注，绝不生成任何图片内容**。
  缺图就明确报缺，不拿别的图凑数。
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "app"))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

GALLERY = _ROOT.parent / "docs" / "gallery"
ABLATION = _ROOT.parent / "docs" / "evidence" / "ablation"
OUT = _ROOT.parent / "docs" / "compare"
SOURCES = _ROOT.parent.parent / "初版" / "测试图片"

# ★ 展示宽度：比常规大 1.6-2 倍，投影时后排能看清
CELL_W = 420
GAP = 10
LABEL_H = 46
BG = (255, 255, 255)
FG = (28, 28, 30)


def _load_font():
    """中文字体；找不到就用 PIL 默认（信息仍会显示，只是字形是拉丁方块）。"""
    from PIL import ImageFont
    for path in ("C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
                 "C:/Windows/Fonts/simhei.ttf",    # 黑体
                 "C:/Windows/Fonts/simsun.ttc"):   # 宋体
        try:
            return ImageFont.truetype(path, 19)
        except Exception:                                    # noqa: BLE001
            continue
    return ImageFont.load_default()


def _label(sheet, x, y, w, text, font=None):
    """在 (x, y, x+w) 处画一条标签栏。返回下一行的 y。

    ★ 顺序很要紧：先用 paste 铺底色，**再**画文字。
      上一版把这两步的顺序弄反了（paste 覆盖了已画的字），
      结果标签栏全空 —— 而标签是评委理解这张图的唯一线索。
    """
    from PIL import ImageDraw
    sheet.paste((248, 247, 244), (x, y, x + w, y + LABEL_H))
    if text.strip():
        d = ImageDraw.Draw(sheet)
        d.text((x + 10, y + LABEL_H // 2), text, fill=FG, font=font,
               anchor="lm")
    return y + LABEL_H


def build_row(items: list[tuple[str, Path]], out_path: Path) -> dict:
    """把若干 (标签, 图路径) 拼成一张带标签栏的对比图。"""
    from PIL import Image

    font = _load_font()
    thumbs = []
    for label, p in items:
        if not p.exists():
            print(f"   ★ 缺图跳过：{p.name}")
            continue
        with Image.open(p) as im:
            im = im.convert("RGB")
            h = int(im.height * CELL_W / im.width)
            thumbs.append((label, im.resize((CELL_W, h), Image.LANCZOS)))
    if not thumbs:
        return {"ok": False, "reason": "没有可用图片"}

    body_h = max(t.height for _, t in thumbs)
    W = CELL_W * len(thumbs) + GAP * (len(thumbs) - 1)
    H = LABEL_H * 2 + body_h

    sheet = Image.new("RGB", (W, H), BG)
    _label(sheet, 0, 0, W, "", font)          # 顶部留白条，避免贴边
    x = 0
    for label, t in thumbs:
        # 图块
        sheet.paste(t, (x, LABEL_H))
        from PIL import ImageDraw
        ImageDraw.Draw(sheet).rectangle(
            [x, LABEL_H, x + CELL_W, LABEL_H + t.height],
            outline=(214, 213, 209), width=1)
        # 底部标签
        _label(sheet, x, LABEL_H + t.height, CELL_W, label, font)
        x += CELL_W + GAP

    OUT.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "WEBP", quality=90, method=6)
    return {"ok": True, "out": out_path.name,
            "size": f"{W}x{H}", "kb": out_path.stat().st_size // 1024,
            "cols": len(thumbs)}


# ══════════════════════════════════════════════════════════
def fig_compare():
    """图①：原图 vs 各风格成品"""
    print("[图①] 原图 vs 成品对比")
    # 挑最能体现差异的家族（按实测：这几个的结构与风格区分度高）
    PICKS = [("zine", "拾景纸刊"), ("full_restyle", "整体重绘"),
             ("risograph_travel_print", "Riso 版画"),
             ("material_pixel", "材料章印"),
             ("second_world", "第二世界")]
    results = []
    for sid in ("1", "2"):
        src = SOURCES / f"test{sid}.png"
        if not src.exists():
            print(f"   ★ 缺原图 test{sid}.png")
            continue
        items = [("原图（未处理）", src)]
        for fid, lab in PICKS:
            p = GALLERY / f"{sid}_{fid}.png"
            if p.exists():
                items.append((lab, p))
        if len(items) < 2:
            print(f"   ★ 样本{sid} 可用图太少")
            continue
        r = build_row(items, OUT / f"compare_{sid}.webp")
        print(f"   样本{sid}: {r}")
        results.append(r)
    return results


def fig_ablation():
    """图②：三段式消融 A/B/C"""
    print("[图②] 三段式契约消融实验")
    results = []
    for sid in ("1", "2"):
        src = SOURCES / f"test{sid}.png"
        if not src.exists():
            continue
        items = [("原图", src)]
        for g, lab in (("A", "A 无契约基线"), ("B", "B 仅保真段"),
                       ("C", "C 完整三段式")):
            p = ABLATION / f"s{sid}_{g}.png"
            if p.exists():
                items.append((lab, p))
        if len(items) < 3:
            print(f"   ★ 样本{sid} 消融图不全（{len(items)-1}/3 组）")
        if len(items) < 2:
            continue
        r = build_row(items, OUT / f"ablation_{sid}.webp")
        print(f"   样本{sid}: {r}")
        results.append(r)
    return results


def fig_axes():
    """图③：能力轴——同一原图在不同风格下的结构保持"""
    print("[图③] 能力轴对照（结构忠实与风格无关）")
    src = SOURCES / "test1.png"
    if not src.exists():
        return []
    items = [("原图", src)]
    for fid, lab in (("full_restyle", "写实·整体重绘"),
                     ("risograph_travel_print", "半写实·孔版印刷"),
                     ("zine", "半写实·纸刊拼贴"),
                     ("voxel_path_reflection", "抽象·体素重构"),
                     ("material_pixel", "抽象·材料章印")):
        p = GALLERY / f"1_{fid}.png"
        if p.exists():
            items.append((lab, p))
    if len(items) < 3:
        print("   ★ 可用图不足")
        return []
    r = build_row(items, OUT / "axes_1.webp")
    print(f"   {r}")
    return [r]


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"输出目录：{OUT}\n")
    allr = []
    if which in ("all", "compare"):
        allr += fig_compare()
    if which in ("all", "ablation"):
        allr += fig_ablation()
    if which in ("all", "axes"):
        allr += fig_axes()

    ok = [r for r in allr if r.get("ok")]
    print(f"\n完成 {len(ok)} 张：")
    for r in ok:
        print(f"   {r['out']:<24} {r['size']:<12} {r['kb']}KB  {r['cols']}列")

    # 把清单写进json，供文档引用（避免文档里写错图名/尺寸）
    (OUT / "figures.json").write_text(
        json.dumps(allr, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n清单 → {OUT / 'figures.json'}")
    print("★ 本脚本只做拼接与标注，不生成任何图片内容。"
          "所有图均来自真实跑出的成品。")