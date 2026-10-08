"""效果证据测量 —— 原图 vs 成品对比图集 + 三段式契约 A/B/C 消融实验

    python tests/evidence_run.py gallery    # 出对比图集（docs/gallery/）
    python tests/evidence_run.py ablation   # 出消融实验数据表
    python tests/evidence_run.py all

★ 为什么要做这个：
  项目的核心主张是「原图保真 + 创意叠加」，三段式契约
  （preserve / creative / forbid）是这个主张的**唯一实现手段**。
  但截至2026-10-08，全项目 grep「对照 / A/B / 盲评 / 评测集 / benchmark」
  是**0 命中**，README 里也没有任何效果证据。
  → 主张没有证据，等于没有主张。这是文档维度失分的根源。

★★ 两条不可退让的原则：
  1. **必须真实调用生图**，不得用任何方式伪造/生成"示意图"。
     这个项目的卖点是"效果是真的"，拿假图去证明它，是自毁信誉。
  2. **失败要留痕**。某组失败就写进结果表，不许悄悄跳过——
     一份"10 组全过"的表如果中间有3 组是靠重试凑出来的，
     那它比不交更坏。

⚠️ 会花真实 API 费用。gallery 约 3-5 次生图，ablation 约 20-30 次。
   每个样本都记耗时与成本档位，便于事后核对。
"""
import io
import json
import os
import random
import sys
import time
from pathlib import Path

# ★ sys.path 要指向 **backend/app**，不是 backend ——
#   根目录算错一级，报的是 ModuleNotFoundError: No module named 'services'，
#   看起来像"依赖没装"，实际是路径指错。包在 backend/app 下。
_ROOT = Path(__file__).resolve().parents[1]        # → backend/
sys.path.insert(0, str(_ROOT / "app"))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# ★ 不设 DISABLE_IMAGE_GENERATION —— 这个脚本就是要真出图。
OUT_GALLERY = _ROOT.parent / "docs" / "gallery"
OUT_DATA = _ROOT.parent / "docs" / "evidence"


# ══════════════════════════════════════════════════════════
# 素材：必须是**这个项目自己跑出来的**真实照片
# ══════════════════════════════════════════════════════════
def collect_sources():
    """挑真实照片作原图。

    ★★ 这一段我错了三轮才做对，过程值得留着：
      第 1 轮：从 storage/images 抓 gen_upload_* → 拿到的是 e2e 测试自己造的
            320×480 水彩简笔山景图（带"small steps, big steam" 字样）。
            ★ 拿简笔画当"保真度"证据毫无意义 —— 它本来就没有细节可保。
      第 2 轮：抓 upload_* → 235 张全是 320×480 PNG，同一批测试假图。
      第 3 轮：想用 提示词规范/ 里的真实照片 → 那是**别的 skill** 生成的
            "拾景纸刊"海报（1260×2092），不是本项目产物。
            ★ 拿别的东西证明自己，是伪造。
      ⇒ 真正的素材在 初版/测试图片/：两张真实航拍建筑照（原图，996×661 /
        984×754）配本项目早期跑出来的水墨 Sketch 成品（2048×1359）。
        那是这个项目**自己的**真实记录。
    ★ 判断标准不是「图好不好看」，是「**这张图是不是这个项目跑出来的**」。
    """
    base = _ROOT.parent.parent / "初版" / "测试图片"
    picked = []
    for p in sorted(base.glob("test?.png")):      # test1/test2，不含"生成"后缀
        try:
            from PIL import Image
            with Image.open(p) as im:
                w, h = im.size
            if min(w, h) >= 600:
                picked.append(p)
        except Exception:
            continue
    return picked


# ══════════════════════════════════════════════════════════
# 图集：同一张原图 × 若干风格家族
#
# ★ 家族是按**原图内容**选的，不是随手挑：
#   素材是两张航拍建筑（白墙黑瓦屋顶 + 大片黄绿地）。
#   zine（拾景纸刊）= 照片从撕纸边下显露、其余延展为旧纸抽象 ——
#   这个家族本来就是为"建筑/街景纪实照片"设计的，风格自洽。
#   若拿 second_world（科幻立体物体）去改一张乡村建筑，
#   出图会显得"风格与题材打架"，评委一眼就能看出是硬凑的。
# ══════════════════════════════════════════════════════════
FAMILIES = [
    ("zine", "拾景纸刊"),
    ("risograph_travel_print", "Riso 旅行版画"),
    ("material_pixel", "材料章印"),
    ("voxel_path_reflection", "体素林径"),
]


def cmd_gallery():
    from services.family_renderer import load_families, render_family
    from services.image_generator import generate_image_with_reference

    srcs = collect_sources()
    if not srcs:
        print("★ storage 里没有可用的真实原图，先跑 tests/e2e_live.py 造素材")
        return 1
    print(f"找到 {len(srcs)} 张可用原图，取前 {min(2, len(srcs))} 张\n")

    # ★ render_family 返回的是**整个渲染结果 dict**（含 segments 三段），
    #   不是拼好的字符串。第一版我当成字符串用，拼出来是 dict 的 repr。
    #   真正要给生图的是 result["segments"] 里的正文。
    fams = load_families() or {}
    OUT_GALLERY.mkdir(parents=True, exist_ok=True)
    results = []

    def _body(fam):
        r = render_family(fam, {})
        segs = (r.get("segments") or {}) if isinstance(r, dict) else {}
        return "\n".join(str(segs.get(k) or "") for k in
                         ("preserve", "creative", "forbid")).strip()

    for si, src in enumerate(srcs[:2]):
        print(f"\n{'='*58}\n原图 {si+1}：{src.name}\n{'='*58}")
        for fid, label in FAMILIES:
            t0 = time.time()
            try:
                fam = fams.get(fid)
                if not fam:
                    print(f"  -- 跳过 {label}（家族不存在）")
                    continue
                prompt = _body(fam)
                r = generate_image_with_reference(str(src), prompt)
                dt = time.time() - t0
                if not r.get("success"):
                    print(f"  ✘ {label:<16} 生成失败（{dt:.0f}s）：{r.get('error')}")
                    results.append({"family": fid, "label": label, "ok": False,
                                    "error": r.get("error"), "src": src.name})
                    continue
                dst = OUT_GALLERY / f"{si+1}_{fid}.png"
                from PIL import Image
                with Image.open(r["image_path"]) as im:
                    im.convert("RGB").save(dst, quality=92)
                print(f"  ✔ {label:<16} {dt:.0f}s → {dst.name}")
                results.append({"family": fid, "label": label, "ok": True,
                                "elapsed": round(dt), "src": src.name,
                                "out": dst.name})
            except Exception as e:
                dt = time.time() - t0
                print(f"  ✘ {label:<16} 异常（{dt:.0f}s）：{type(e).__name__}: {e}")
                results.append({"family": fid, "label": label, "ok": False,
                                "error": f"{type(e).__name__}: {e}", "src": src.name})

    OUT_DATA.mkdir(parents=True, exist_ok=True)
    (OUT_DATA / "gallery.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n图集完成：{ok}/{len(results)} 成功 → {OUT_GALLERY}")
    return 0 if ok else 1


# ══════════════════════════════════════════════════════════
# 消融实验：A 无契约 / B 只 preserve / C 完整三段式
# ══════════════════════════════════════════════════════════
def _visual_check(src_path, out_path, checklist):
    """用 VLM 按 checklist 逐条判「创意指令有没有落地」。

    ★ 为什么用 VLM 判而不是像素比对：
      「有没有按指令加某个元素」是语义问题，结构相似度答不了。
    ★ 为什么 checklist 固定：判据必须可复现，
      每次跑都问同样的问题，否则两轮结果不可比。
    ★ 消息格式必须照 card_extractor 的实际用法：
      [{"role":"user","content":[{"type":"text",...},{"type":"image_url",...}]}]
      —— 第一版我猜成 chat-style 的 base64 字符串，那个格式不会报错，
      只会静默判不出东西（和"用错正则得到静默 0 条"是同一类错误）。
    """
    from services.llm import vision
    import base64 as _b64
    import mimetypes

    def _img(p):
        mime = mimetypes.guess_type(p)[0] or "image/png"
        with open(p, "rb") as f:
            b = _b64.b64encode(f.read()).decode()
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b}"}}

    lst = "\n".join(f"{i+1}. {c}" for i, c in enumerate(checklist))
    q = ("第一张图是原始照片，第二张图是按指令改绘后的成品。\n"
         f"请逐条判断第二张图里是否出现了该元素，只回答「是」或「否」，不要解释。\n"
         f"检查项：\n{lst}\n"
         "格式：每行一个，形如「1. 是」")
    try:
        txt = vision([{"role": "user",
                       "content": [{"type": "text", "text": q},
                                   _img(src_path), _img(out_path)]}])
        return _parse_checklist(txt, len(checklist))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "hit": None,
                "per_item": [], "unknown": len(checklist)}


def _parse_checklist(txt, n):
    import re
    hits = {}
    for line in str(txt or "").splitlines():
        m = re.search(r"(\d+)\s*[.、)）]\s*\**\s*(是|否|yes|no|有|没有)", line.strip(), re.I)
        if m:
            idx = int(m.group(1)) - 1
            hits[idx] = m.group(2).lower() in ("是", "yes", "有")
    got = [hits.get(i) for i in range(n)]
    return {"per_item": got,
            "hit": sum(1 for x in got if x is True),
            "unknown": sum(1 for x in got if x is None)}


def cmd_ablation():
    from services.image_generator import generate_image_with_reference
    from services.family_renderer import load_families, render_family

    srcs = collect_sources()
    if not srcs:
        print("★ 没有可用原图，先跑 tests/e2e_live.py")
        return 1

    fams = load_families() or {}
    fam = fams.get("zine")
    if not fam:
        print("★ zine 家族不存在")
        return 1

    # ★ render_family 返回 dict，三段要从 result["segments"] 取，
    #   而且 preserve / creative / forbid 各自是**完整的一段正文**。
    r = render_family(fam, {})
    segs = r.get("segments") or {}
    preserve = str(segs.get("preserve") or "").strip()
    creative = str(segs.get("creative") or "").strip()
    forbid = str(segs.get("forbid") or "").strip()

    if not (preserve and creative):
        print(f"★ 三段不完整（preserve={len(preserve)} creative={len(creative)}），"
              f"family_id={r.get('family_id')}")
        return 1

    # A 组 = 直接描述风格、不含任何契约字段（对照基线）
    PROMPT_A = ("把这张照片处理成第二世界风格：科幻感的陌生世界，保留原图主体与构图。")
    # B 组 = 只给保真段（★ 关键：给完之后模型只知道「别改」，不知道「要改成什么」）
    PROMPT_B = preserve
    # C 组 = 完整三段式
    PROMPT_C = "\n".join(x for x in (preserve, creative, forbid) if x).strip()

    groups = [("A", "无契约", PROMPT_A),
              ("B", "仅保真段", PROMPT_B),
              ("C", "完整三段式", PROMPT_C)]
    checklist = ["原图中的主体仍在画面里",
                 "画面呈现科幻或异世界氛围",
                 "主体轮廓与姿态未明显改变"]

    OUT_DATA.mkdir(parents=True, exist_ok=True)
    outdir = OUT_DATA / "ablation"
    outdir.mkdir(exist_ok=True)

    rows = []
    n = min(3, len(srcs))
    print(f"三段长度：preserve={len(preserve)} creative={len(creative)} "
          f"forbid={len(forbid)} 字符\n")
    for si, src in enumerate(srcs[:n]):
        print(f"\n{'='*58}\n样本 {si+1}：{src.name}\n{'='*58}")
        for gid, label, prompt in groups:
            t0 = time.time()
            rec = {"sample": si + 1, "src": src.name,
                   "group": gid, "label": label, "prompt_len": len(prompt)}
            try:
                out = generate_image_with_reference(str(src), prompt)
                if not out.get("success"):
                    rec.update(ok=False, error=out.get("error"))
                    print(f"  ✘ {gid} {label:<12} 生成失败：{out.get('error')}")
                    rows.append(rec)
                    continue
                op = outdir / f"s{si+1}_{gid}.png"
                from PIL import Image
                with Image.open(out["image_path"]) as im:
                    im.convert("RGB").save(op, quality=90)
                chk = _visual_check(str(src), str(op), checklist)
                rec.update(ok=True, elapsed=round(time.time() - t0),
                           out=op.name, check=chk)
                hit = chk.get("hit")
                print(f"  ✔ {gid} {label:<12} {rec['elapsed']}s  命中 "
                      f"{hit}/{len(checklist)}"
                      f"{'（有未判定项）' if chk.get('unknown') else ''}"
                      + (f"  {chk.get('error')}" if chk.get("error") else ""))
                rows.append(rec)
            except Exception as e:
                rec.update(ok=False, error=f"{type(e).__name__}: {e}")
                print(f"  ✘ {gid} {label:<12} 异常：{e}")
                rows.append(rec)

    (OUT_DATA / "ablation.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    # ——汇总表：失败与未判定都要显式出现，不许悄悄消失——
    print(f"\n{'='*58}\n汇总（命中数 / 3）\n{'='*58}")
    for gid, label, _ in groups:
        rs = [r for r in rows if r["group"] == gid]
        okr = [r for r in rs if r.get("ok")]
        hits = [r["check"]["hit"] for r in okr
                if r.get("check") and r["check"].get("hit") is not None]
        avg_txt = f"{sum(hits)/len(hits):.2f}" if hits else "无有效判定"
        print(f"  {gid} {label:<12} 成功 {len(okr)}/{len(rs)}  平均命中 {avg_txt}")
    print(f"\n明细 → {OUT_DATA / 'ablation.json'}")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    sys.exit(0 if (
        (cmd_gallery() in (0, 1)) and
        (mode in ("ablation", "all") and cmd_ablation() in (0, 1))
    ) else 1)