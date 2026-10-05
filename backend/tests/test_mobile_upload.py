"""手机相册上传（MPO）+ 出图回填时机 + 小人数量硬约束

三件事都来自 2026-10-05 用户的真实反馈：

1. **同一相册里个别照片报「不支持 MPO」**
   MPO 是 Android 动态/连拍照片（带多帧扩展的 JPEG），Pillow 读出的 format 就是
   "MPO"，而白名单只有 JPEG/PNG/WebP ⇒ 整张被 415 拒掉。
   修法：取第 0 帧（主照片）**重编码**成基线 JPEG 落盘。
   ★ 为什么不能只改扩展名：文件里还留着 MPF 私有段，下游（gpt-image / <img>）
     未必认；重编码才是真兼容。

2. **生成成功后回到画布太慢**
   根因：图在 tool_end 时就好了，但引擎还要再跑一次**收尾 LLM** 才 emit("done")，
   而 done 才带 image_url ⇒ 用户在空白画布上多等一整轮 LLM。
   修法：图一落盘就 emit("image")，前端先上画布，收尾文案照常。

3. **小人数量对不上**（选 2/3/4/5/7 → 出 3/3/5/5/8）
   提示词只写了"加入 N 个"，是软性表述；模型会自然 ±1。
   修法：creative 段强调"恰好 N 个 + 落笔前先定好 N 个角色"，
   forbid 段加死约束"多画/漏画/画成两个都算错误"。

★ 全程离线：造真实的 MPO 文件、不发网络请求、不调生图。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import os                                                         # noqa: E402

os.chdir(Path(__file__).resolve().parent.parent / "app")

import services.upload as up                                      # noqa: E402
from fastapi import HTTPException                                 # noqa: E402
from PIL import Image, MpoImagePlugin                            # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


TMP = Path(tempfile.mkdtemp(prefix="upload_mpo_"))
SESSION = "aaaa1111-2222-3333-4444-555566667777"


class _F:
    """最小 UploadFile 替身"""

    def __init__(self, path: Path):
        self.file = open(path, "rb")
        self.filename = path.name
        self.size = path.stat().st_size
        self.content_type = "image/jpeg"


def _make_mpo(path: Path) -> Path:
    """用 Pillow 的 MPO 写入器造一个**合法**的两帧动态照片

    ★ 早先用「手工插 APP2 段」的方式造过一份，Pillow 直接判定为 malformed 并
      退回按普通 JPEG 处理 —— 那样根本走不到 MPO 分支，测试等于没测。
      必须用官方写入器才算真样本。
    """
    MpoImagePlugin.MpoImageFile
    first = Image.new("RGB", (1200, 900), (40, 80, 140))
    second = Image.new("RGB", (1200, 900), (200, 60, 60))
    first.save(path, "MPO", save_all=True, append_images=[second])
    return path


print("\n── 1. MPO（手机动态照片）能上传，并被转成标准 JPEG ──")
mp = _make_mpo(TMP / "motion.jpg")
with Image.open(mp) as im:
    src_fmt, src_frames = im.format, im.n_frames
check("样本确实是合法 MPO", src_fmt == "MPO" and src_frames == 2, f"{src_fmt}/{src_frames} 帧")

try:
    r = up.validate_and_save(_F(mp), "upload", SESSION)
    saved = Path(r["path"])
    with Image.open(saved) as im2:
        out_fmt, out_frames = im2.format, getattr(im2, "n_frames", 1)
        out_size = im2.size
    check("上传没有被 415 拒掉", True, f"{saved.name}")
    check("落盘是标准 JPEG（不再是 MPO）", out_fmt == "JPEG", out_fmt)
    check("只保留主帧（动态部分丢掉）", out_frames == 1, f"{out_frames} 帧")
    check("尺寸没变", out_size == (1200, 900), str(out_size))
    check("报给前端的格式已同步为 JPEG", r["format"] == "JPEG", r["format"])
    check("文件名是 .jpg", saved.suffix == ".jpg", saved.suffix)
    saved.unlink(missing_ok=True)
except HTTPException as e:
    check("上传没有被 415 拒掉", False, f"HTTP {e.status_code}: {e.detail}")

print("\n── 2. 普通图片不受影响（别把好路径改坏）──")
plain = TMP / "plain.jpg"
Image.new("RGB", (800, 600), (10, 120, 200)).save(plain, "JPEG", quality=90)
try:
    r = up.validate_and_save(_F(plain), "upload", SESSION)
    saved = Path(r["path"])
    check("普通 JPEG 照常上传", saved.exists() and r["format"] == "JPEG", r["url"][-24:])
    saved.unlink(missing_ok=True)
except HTTPException as e:
    check("普通 JPEG 照常上传", False, str(e.detail)[:50])

png = TMP / "plain.png"
Image.new("RGB", (400, 400), (0, 200, 0)).save(png, "PNG")
try:
    r = up.validate_and_save(_F(png), "upload", SESSION)
    saved = Path(r["path"])
    check("PNG 照常上传", saved.suffix == ".png", saved.suffix)
    saved.unlink(missing_ok=True)
except HTTPException as e:
    check("PNG 照常上传", False, str(e.detail)[:50])

print("\n── 3. 仍然要拦住真·不支持的格式（别变成什么都放行）──")
fake = TMP / "fake.png"
fake.write_bytes(b"not an image at all")
try:
    up.validate_and_save(_F(fake), "upload", SESSION)
    check("伪图片应被拒", False, "竟然放行了")
except HTTPException as e:
    check("伪图片应被拒", e.status_code == 400, f"HTTP {e.status_code}")

print("\n── 4. 出图回填：图一落盘就发事件，不等收尾 LLM ──")
import engine.loop as loopmod                                    # noqa: E402

src = Path(loopmod.__file__).read_text(encoding="utf-8")
check("引擎里有独立的 image 事件", 'emit("image"' in src)
check("推送发生在 tool_end 之后、下一轮 LLM 之前",
      src.index('emit("image"') > src.index('"tool_end"'))
check("同一张图只推一次（去重变量存在）", "emitted_image_url" in src)
check("done 事件仍带 image_url（前端兜底仍在）",
      '"done"' in src and "image_url" in src)

print(f"\n{'=' * 56}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
