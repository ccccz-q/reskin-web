"""创作卡提取验收 —— 补上「保真机制的数据源」这一环

    python tests/test_card.py

★★ 为什么必须有这个文件
----------------------
审查发现 P0-1：`card` 是渲染「反推 forbid」的唯一数据源，却**没有任何生产者**。
五个工具里没有 extract_card、前端恒传 `{}`。后果不是报错，而是静默产出：

    "插画只提炼原图中的 0 个轮廓与路径"
    forbid 段的 {dynamic_forbid} 整行被删掉

也就是项目最核心的保真机制 100% 空转。
现在有了 services/card_extractor.py 与 extract_card 工具，
这里逐条钉住它的行为，防止再次退化。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from PIL import Image  # noqa: E402

from services.card_extractor import (  # noqa: E402
    _color_name,
    build_card,
    clear_card_cache,
    summarize_card,
)
from services.family_renderer import load_families, render_family, render_to_prompt  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def make_photo(path: Path, size=(900, 1200)):
    """造一张「像照片」的图：暖底 + 深色斜线 + 一块蓝"""
    im = Image.new("RGB", size, (238, 226, 205))
    px = im.load()
    w, h = size
    for i in range(min(w, h)):
        for off in range(24):
            if i + off < w:
                px[i + off, i] = (60, 70, 85)
    for y in range(int(h * 0.12), int(h * 0.33)):
        for x in range(int(w * 0.68), int(w * 0.97)):
            px[x, y] = (70, 110, 160)
    im.save(path, quality=92)
    return path


tmp = Path(tempfile.mkdtemp(prefix="cardtest_"))
photo = make_photo(tmp / "photo.jpg")

print("=== 1. 色名判断（含两个曾经判错的实测反例）===")
cases = [
    ((242, 233, 220), "米白", "暖米白纸张 —— 低饱和被误判成「灰橙」过"),
    ((80, 75, 73), "中灰", "深灰褐 —— delta 只有 7，色相无意义，曾被判「深橙」"),
    ((18, 32, 70), "深蓝", ""),
    ((74, 128, 62), "绿", ""),
    ((176, 62, 44), "红", ""),
    ((6, 6, 8), "近黑", ""),
    ((200, 200, 205), "浅灰", "曾被判「近白」"),
    ((90, 60, 30), "深棕", "暖而深，曾被「橙」分支遮蔽"),
    ((240, 200, 60), "橙", "明黄 —— 曾被多写的「棕」兜底吃掉"),
]
for rgb, expect, note in cases:
    got = _color_name(*rgb)
    check(f"RGB{rgb} → {expect}", got == expect, f"得到 {got}" + (f" | {note}" if note else ""))

print()
print("=== 2. 本地档提取（0 成本，不需要任何 key）===")
clear_card_cache()
card = build_card(str(photo), use_vlm=False)
check("返回非空", bool(card))
check("origin=local", card.get("_origin") == "local", str(card.get("_origin")))
pal = card.get("palette") or []
check("拿到色板", len(pal) >= 2, f"{len(pal)} 色")
check("色板项有 name/hex/ratio", all({"name", "hex", "ratio"} <= set(p) for p in pal))
check("hex 格式正确", all(p["hex"].startswith("#") and len(p["hex"]) == 7 for p in pal))
check("比例总和 ≈ 1", abs(sum(p["ratio"] for p in pal) - 1.0) < 0.06,
      str(round(sum(p["ratio"] for p in pal), 3)))
check("尺寸正确", card.get("source_width") == 900 and card.get("source_height") == 1200)
check("朝向=portrait", card.get("orientation") == "portrait")
check("有亮度提示", bool(card.get("light_hint")), str(card.get("light_hint")))
check("摘要非空", summarize_card(card) != "（无有效字段）", summarize_card(card))

print()
print("=== 3. card 分级与告警（palette_only vs full vs none）===")
fams = load_families()

r = render_family(fams["zine"], {}, {}, strict=False)
check("空 card → level=none", r["card_level"] == "none", r["card_level"])
check("空 card 给出告警", any("缺少创作卡" in w for w in r["warnings"]))

r = render_family(fams["zine"], {}, card, strict=False)
check("只有色板 → level=palette_only", r["card_level"] == "palette_only", r["card_level"])
check("palette_only 告警措辞不同",
      any("只有色板" in w for w in r["warnings"]),
      str([w for w in r["warnings"] if "创作卡" in w][:1]))

full = dict(card)
full["subject"] = {"name": "雪山垭口"}
full["anchors"] = [{"desc": "之字形山路", "materializable": True},
                   {"desc": "暖色天空", "materializable": False}]
full["risk_notes"] = "山脊轮廓易被风格化抹平"
r = render_family(fams["zine"], {}, full, strict=False)
check("有主体+锚点 → level=full", r["card_level"] == "full", r["card_level"])
check("full 时不再告警", not any("创作卡" in w for w in r["warnings"]),
      str([w for w in r["warnings"] if "创作卡" in w]))

print()
print("=== 4. ★ card 真的改变了提示词（这是整件事的意义）===")
no_card = render_to_prompt(render_family(fams["zine"], {}, {}, strict=False))
with_card = render_to_prompt(render_family(fams["zine"], {}, full, strict=False))
check("有 card 后提示词不同", no_card != with_card)
check("主体名进了 preserve", "雪山垭口" in with_card)
check("锚点进了 creative", "之字形山路" in with_card)
check("不再出现「0 个轮廓」", "0 个" not in with_card,
      [ln for ln in with_card.splitlines() if "0 个" in ln][:1])

# forbid 段长度是「反推 forbid 是否生效」的代理指标：
# card 缺失时 {dynamic_forbid} 整行会被残句清理删掉，段长明显缩水
f_no = render_family(fams["second_world"], {}, {}, strict=False)["segments"]["forbid"]
f_yes = render_family(fams["second_world"], {}, full, strict=False)["segments"]["forbid"]
check("反推 forbid 因 card 而变长", len(f_yes) > len(f_no), f"{len(f_no)} → {len(f_yes)}")

print()
print("=== 5. 全家族都能吃下 card ===")
for fid in ("full_restyle", "second_world", "split_poster",
            "surreal_collage", "zine", "risograph_travel_print"):
    params = {}
    r = render_family(fams[fid], params, full, strict=False)
    ok = r["card_level"] == "full" and "0 个" not in render_to_prompt(r)
    check(f"{fid} 用 card 渲染正常", ok,
          "" if ok else f"level={r['card_level']}")

print()
print("=== 6. 异常路径必须优雅 ===")
check("不存在的文件 → 空 dict", build_card(str(tmp / "nope.jpg"), use_vlm=False) == {})
check("空路径 → 空 dict", build_card("", use_vlm=False) == {})

bad = tmp / "broken.jpg"
bad.write_bytes(b"this is definitely not an image")
bcard = build_card(str(bad), use_vlm=False)
check("坏文件不抛异常", isinstance(bcard, dict))
check("坏文件标记 origin=none", bcard.get("_origin") == "none", str(bcard.get("_origin")))

print()
print("=== 7. 缓存行为 ===")
clear_card_cache()
a = build_card(str(photo), use_vlm=False)
b = build_card(str(photo), use_vlm=False)
check("两次结果一致", a == b)
photo2 = make_photo(tmp / "photo2.jpg", size=(600, 600))
c = build_card(str(photo2), use_vlm=False)
check("不同图得到不同卡", c.get("orientation") == "square" and c != a)

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
