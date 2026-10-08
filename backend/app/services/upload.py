"""上传校验 —— 唯一入口，所有「用户给的文件」都必须过这里

对照审查报告 P1-12。旧版代码：

    ext = os.path.splitext(file.filename)[1]              # ① 扩展名来自客户端，可伪造
    upload_name = f"upload_{uuid.uuid4().hex[:8]}{ext}"
    with open(upload_path, "wb") as f:
        f.write(await file.read())                        # ② 全量读进内存，无大小上限
                                                          # ③ 不校验是不是图片
                                                          # ④ 失败无任何错误处理

四个洞：
    ① 改名的 .exe 也能存进 images 并通过 /images/xxx.exe 访问
    ② 一个 2GB 文件就能把进程内存打满
    ③ 非图片一路传到 Seedream，浪费一次付费调用才报错
    ④ 上传失败时前端静默，用户不知道发生了什么

这里统一收紧：限大小 → 用 Pillow 解码验证真实格式（不信任任何客户端声明）
→ 按真实格式定扩展名（杜绝扩展名撒谎）→ 落盘到统一目录。
"""
from __future__ import annotations

import io
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, UploadFile
from PIL import Image

from config import (
    ALLOWED_IMAGE_FORMATS,
    IMAGE_STORAGE_DIR,
    MAX_UPLOAD_BYTES,
)
from governance.guard import GovernanceError, check_upload_size
from infra.storage import relative_key, to_url
from services.identity import DEFAULT_SESSION, is_anonymous_fallback

# 允许哪些 Pillow 格式（value 是扩展名）
_MB = 1024 * 1024

# 解压炸弹防线：Pillow 默认阈值太高（≈8948 万像素），
# 且超过阈值 1~2 倍时只发 Warning 不抛异常 —— 等于留了个 0.67GB 的静默窗口。
MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", "40_000_000"))
MAX_IMAGE_SIDE = int(os.getenv("MAX_IMAGE_SIDE", "12000"))


def validate_and_save(file: UploadFile, prefix: str = "upload",
                      session: str = "default") -> dict:
    """校验并保存上传文件

    返回 {"path": str, "url": str, "filename": str, "width": int, "height": int,
          "bytes": int, "format": str}
    任何一步不合格都抛 HTTPException（带可读的中文提示）。

    ★ session（2026-10-03 公开版多用户）：非 default 会话的文件落盘到
      images/<session>/<日期>/，画廊只列自己目录 + 公共 seed 目录 ——
      不同访客的图片互相不可见。session 值来自 identity 白名单清洗，
      不可能是路径注入串。

    ★ 公开版拒绝「身份未知」的写入（2026-10-03）：
      画廊修复后，default 会话只能看到种子图。若放任无头请求继续写进
      images/ 根目录，用户会得到「上传成功、生成成功、画廊里没有」的
      幽灵结果 —— 比直接报错更难排查。所以这里明确拒绝，让他先拿会话。
      判定与画廊同一个函数（identity.is_anonymous_fallback），不会各说各话。
    """
    if is_anonymous_fallback(session):
        raise HTTPException(
            400,
            "公开版需要会话标识（X-Session-Id）。"
            "浏览器会自动带上；若是脚本/命令行调用，请先手动生成一个 UUID 放进请求头。",
        )

    # ── ① 体积 —— 先看声明，再流式读，绝不「先全量读进内存再判」────
    #
    # ★ 旧实现：`raw = file.file.read()` 然后才 `if len(raw) > MAX`。
    #   那道 20MB 护栏只是「拒绝结果」，不是「拒绝过程」：
    #   Starlette 会先把超过 1MB 的部分 spool 到磁盘临时文件，
    #   接着 .read() 又把整份内容一次性拉回 Python 堆 —— 峰值内存 ≈ 文件大小。
    #   传一个 2GB 文件就能把进程打爆，并发 3 个必然 OOM。
    #
    #   现在：a) 先看 Content-Length 声明，超限直接拒（连接都不读完）
    #         b) 再精确读到上限 +1 字节，超了就拒，绝不多读
    #         c) 判定逻辑统一问 governance，这里只负责翻译成 HTTP 状态码
    declared = getattr(file, "size", None)
    if declared is not None and declared > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"文件 {declared / _MB:.1f}MB 超过上限 {MAX_UPLOAD_BYTES / _MB:.0f}MB，"
            f"请压缩后再上传",
        )

    raw = file.file.read(MAX_UPLOAD_BYTES + 1)
    size = len(raw)
    try:
        check_upload_size(size)          # ★ 唯一判定入口（治理层）
    except GovernanceError as e:
        status = 413 if e.code == "upload_too_large" else 400
        raise HTTPException(status, {"code": e.code, "message": str(e)}) from e

    # ── ② 真实格式（不信任扩展名与 Content-Type）─────────────
    #
    # Pillow 默认阈值 Image.MAX_IMAGE_PIXELS ≈ 8948 万像素，而
    # 「超过阈值 1~2 倍」只发 **DecompressionBombWarning（警告，不抛异常）**。
    # 实测：1.4x / 1.8x 阈值的图都能通过，进程会为它分配约 0.67GB 内存。
    # 一张几十 KB 的高压缩比 PNG 就能做到 —— 这是典型的解压炸弹。
    # 所以这里显式把阈值压到 4000 万像素（约 8000×5000，远超实际需求）。
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()                      # 强制完整解码，抓出截断/损坏的图片
        fmt = (im.format or "").upper()
        width, height = im.size
    except Image.DecompressionBombError:
        raise HTTPException(413, "图片尺寸过大（疑似解压炸弹），请缩小后再上传") from None
    except Exception:
        raise HTTPException(400, "无法解析为有效图片，请确认文件未损坏") from None

    # ── ②-b MPO：手机相册里的「动态照片」──────────────────────
    #
    # ★ 现象（2026-10-05 用户实测）：同一相册里大部分照片能传，个别报
    #   「不支持的图片格式 MPO」。不是照片坏了 —— MPO 是 JPEG 的多帧扩展
    #   （Android 动态/连拍照片），Pillow 读出来的 format 就是 "MPO"，
    #   而白名单只有 JPEG/PNG/WebP，于是整批被 415 拒掉。
    #
    # 处理：取**第一帧**（主照片）转成标准 JPEG 再落盘。
    #   ★ 为什么必须重编码而不是改个扩展名：MPO 里带着多帧扩展和 MPF 私有段，
    #     下游（gpt-image / 浏览器 <img>）未必认；转成基线 JPEG 才是真兼容。
    if fmt == "MPO":
        try:
            im.seek(0)                                  # 第 0 帧 = 主照片
            primary = im.convert("RGB")                  # 丢掉 MP 扩展与 alpha
            buf = io.BytesIO()
            primary.save(buf, format="JPEG", quality=95, optimize=True)
            raw = buf.getvalue()
            im = primary
            fmt, width, height = "JPEG", *primary.size
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"动态照片（MPO）解析失败：{e}") from e

    # 顺带限制单边像素：哪怕没到总像素阈值，也不接受 50000×50000 这种
    if max(width, height) > MAX_IMAGE_SIDE:
        raise HTTPException(
            413, f"图片边长 {max(width, height)}px 超过上限 {MAX_IMAGE_SIDE}px"
        )

    ext = ALLOWED_IMAGE_FORMATS.get(fmt)
    if not ext:
        raise HTTPException(
            415, f"不支持的图片格式 {fmt or '未知'}，支持 JPG / PNG / WebP"
                 "（手机动态照片 MPO 会自动取主帧，一般不会走到这里）"
        )

    # ── ③ 按会话/日期分目录 + 随机名落盘 ────────────────────
    # default 会话保持旧布局（images/<日期>/），与本地已有数据兼容；
    # 匿名/登录会话落到自己的命名空间（images/<session>/<日期>/）。
    base = (IMAGE_STORAGE_DIR / session) if session != DEFAULT_SESSION \
        else IMAGE_STORAGE_DIR
    subdir = base / datetime.now().strftime("%Y-%m-%d")
    subdir.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}_{uuid.uuid4().hex[:8]}{ext}"
    path = subdir / filename
    try:
        path.write_bytes(raw)
    except OSError as e:
        raise HTTPException(500, f"保存失败：{e}") from e

    return {
        "path": str(path),
        "url": to_url(path),
        "filename": filename,
        "key": relative_key(path),
        "width": width,
        "height": height,
        "bytes": size,
        "format": fmt,
    }


def load_upload_as_card_hint(path: str | Path) -> dict:
    """从上传图里读出渲染需要的派生信息（尺寸/宽高比）

    提炼卡（card）目前由上层 CLI / Agent 提供；这里只补几项能从图本身读到的，
    比如 phase_ratio 需要的尺寸。真实尺寸很重要——它决定输出档位，
    也决定不适合 family 的画幅时要不要告警。
    """
    p = Path(path)
    try:
        with Image.open(p) as im:
            w, h = im.size
    except Exception:
        return {}
    return {
        "source_width": w,
        "source_height": h,
        "source_ratio": round(w / h, 4) if h else None,
        "orientation": "landscape" if w > h else ("portrait" if h > w else "square"),
    }
