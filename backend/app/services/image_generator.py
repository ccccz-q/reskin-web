"""图像生成服务 —— 封装豆包 Seedream API（OpenAI 兼容协议）

════════ 2026-09-26 重写要点 ════════

修好的：
  1. **返回可直接用的 URL**（审查报告 P1-10）
     落盘到 images/2026-09-26/gen_xxx.jpg，但前端只取文件名拼 /images/gen_xxx.jpg
     → 丢掉日期子目录 → 404。现在 URL 由后端统一生成，前端不再做字符串拼接。

  2. **尺寸档位的重试阶梯**（审查报告 P2-16）
     旧代码 `longest < 1600 → "1k"`。但本项目实测的下限是 3,686,400 像素，
     而 1k 档（约 100 万像素）**恒低于下限** —— 小图必然浪费一次调用去撞 400。
     → 现在小图直接给最小合法档，并保留「遇到尺寸类报错自动升档重试」的兜底，
       这样即使将来模型换版本、常量变了，也不会静默失败。

  3. **绝对路径落盘**：不再依赖 cwd（旧 ./storage 导致项目下出现两份 storage）。

保留的（这几处原本就修得对，不要动）：
  - 替换 hardcode "2K" → resolve_size() 跟随原图
  - content-type 决定扩展名（4.5 只回 jpeg，写死 .png 是撒谎）
  - 文件名「日期/原图 stem/时间戳/随机」唯一化
"""
from __future__ import annotations

import base64
import math
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from openai import OpenAI

from config import (
    IMAGE_BACKEND,
    IMAGE_API_KEY,
    IMAGE_BASE_URL,
    IMAGE_MODEL,
    IMAGE_STORAGE_DIR,
    SEEDREAM_MODEL,
    IMAGE_TIMEOUT_SEC,
    IMAGE_ATTEMPT_TIMEOUT_SEC,
    IMAGE_TOTAL_BUDGET_SEC,
    REQUEST_TIMEOUT_SEC,
    assert_ready_for_generation,
)
from infra.logging import audit, logger, step
from infra.storage import resolve_subdir, to_url

# ★ api_key 为空时给占位值（2026-10-05 开源整理时实测）：
#   OpenAI 客户端在构造那一刻就会因为空 key 抛 OpenAIError，
#   于是"还没配 .env 的新用户"在 import 阶段就崩，连一句能照做的提示都看不到。
#   占位 key 让导入照常通过；真到出图时 assert_ready_for_generation() 会给出
#   「缺少生图配置，请设置 IMAGE_API_KEY」这类可执行的中文提示。
client = OpenAI(
    base_url=IMAGE_BASE_URL,
    api_key=IMAGE_API_KEY or "sk-not-configured",
    timeout=IMAGE_TIMEOUT_SEC,
    # ★ 关掉 SDK 自带重试：本文件自己有一层「尺寸被拒 → 升档重试」的阶梯。
    #   两层重试叠加会让一次失败最多产生 3×3 = 9 次付费调用，且掩盖真实错误。
    max_retries=0,
)


# ══════════════ 下载生成图的护栏 ══════════════
#
# generate 返回的是「图片 URL」，我们要再 GET 一次才能落盘。
# 这个 URL **完全由远端响应决定**，如果不设防，它就是一条现成的 SSRF 通道：
#   - 指向 127.0.0.1 / 169.254.169.254（云元数据）→ 内网探测
#   - allow_redirects 默认 True → 即使是合法 https 也能被 302 劫持到内网
#   - 响应体不设上限 → 一个"永不结束"的响应就能把进程内存打满
#   - 内容会被写进 storage/images/ 并通过 /images/ 提供 → 形成完整的回显通道
#
# 所以这里四件事都做：scheme 白名单 / host 白名单 / 禁重定向 / 体积硬上限。

MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(25 * 1024 * 1024)))

# ── 备用生图模型（默认关闭）──────────────────────────────────
# IMAGE_FALLBACK_MODELS 为空 = 只用 IMAGE_MODEL，与今天的行为完全一致。
# 填了（逗号分隔）才在主模型连续被拒时按序备用 —— 例如
#   IMAGE_FALLBACK_MODELS=gpt-image-2.5-flare,gpt-image-2.5-sunburst
# ★ 默认**不**替用户选备用模型：不同模型的画面倾向不同，
#   悄悄换模型等于悄悄改变产出，那是降质而不是兜底。要换由用户显式指定。
IMAGE_FALLBACK_MODELS = tuple(
    m.strip() for m in os.getenv("IMAGE_FALLBACK_MODELS", "").split(",") if m.strip()
)


def image_models() -> list[str]:
    """本次出图要依次尝试的模型 —— 主模型在前，备用在后（去重）"""
    out = [IMAGE_MODEL]
    for m in IMAGE_FALLBACK_MODELS:
        if m not in out:
            out.append(m)
    return out


def image_plan() -> list[tuple[str, float]]:
    """[(模型, 调用前等待秒数), …]

    等待梯度 0 / 5 / 15 / 30 / 45 秒（共 5 次，累计等待 95s）。

    ★ 为什么从 0/3/8/15 拉长到这样（2026-10-05 别人实测截图）：
      用户的朋友测试时连续两次 generate_image 都返回「上游通道正忙」，
      而我在同一时段本机探针 **26.4s 一次就成功** —— 说明不是代码问题、
      也不是那张照片或提示词的问题，而是中转站图像组的**容量空窗期**：
      窗口一开全成功，窗口一关全失败。
      旧的 4 次 / 累计 26 秒，空窗稍长就整轮报废；既然用户在异步任务里
      等的是「一张图」而不是「毫秒级响应」，把耐心换成成功率是划算的。

    ★ 但也**不能无限等**：真正的上限由 _deadline（总预算）兜底，
      见 IMAGE_TOTAL_BUDGET_SEC —— 空窗期长达几分钟时宁可早点如实报错，
      也不要让用户的轮询挂到天荒地老。
    """
    models = image_models()
    primary_waits = (0.0, 5.0, 15.0, 30.0, 45.0)
    fallback_waits = (0.0, 5.0)
    plan: list[tuple[str, float]] = []
    for i, m in enumerate(models):
        for w in (primary_waits if i == 0 else fallback_waits):
            plan.append((m, w))
    return plan

# 允许下载图片的 host 后缀。默认值来自本机实际用的是火山方舟。
_DOWNLOAD_HOST_SUFFIXES = tuple(
    h.strip().lower()
    for h in os.getenv(
        "DOWNLOAD_HOST_SUFFIXES",
        ".volces.com,.volcengineapi.com,.byteimg.com,.byteimg.cn",
    ).split(",")
    if h.strip()
)


def assert_downloadable_url(url: str) -> None:
    """确认这个 URL 值得去下载 —— 挡 SSRF 的第一道闸

    ★ 这条白名单是给**客户端传进来的 URL** 用的（画廊打包下载等），
      域名必须显式放行，因为它可能来自任何人。
    """
    if not url or not url.strip():
        raise ValueError("生成结果里没有图片 URL")
    p = urlparse(url.strip())
    if p.scheme != "https":
        raise ValueError(f"拒绝下载：只允许 https，收到 {p.scheme or '空'}://")
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("拒绝下载：URL 中没有主机名")
    if not any(host == s.lstrip(".") or host.endswith(s) for s in _DOWNLOAD_HOST_SUFFIXES):
        raise ValueError(
            f"拒绝下载：主机 {host!r} 不在白名单内"
            f"（如需放行请在 .env 里配置 DOWNLOAD_HOST_SUFFIXES）"
        )


def assert_provider_image_url(url: str) -> None:
    """确认「上游返回的」图片 URL 可以取 —— 与上面那张白名单是**两回事**

    ★ 为什么必须分开（2026-10-05 实测踩坑）：
      换到 image2 原生分组后，上游不再返回 base64，改成返回
      `data[0].url = https://cdn.jd23kjs.work/...`。
      如果沿用客户端那套白名单，就得把中转站的 CDN 域名硬编码进去 ——
      而 CDN 域名会变、白名单一漏就是内网直连（SSRF）。
      实际上这里的威胁模型不一样：这个 URL 是**我们自己信任的供应商**返回的
      （我们已经把 API key 交给了它），不是陌生人塞进来的。
      所以这里的正确判据是「**绝不能是内网地址**」，而不是「域名在不在名单里」。

    判据：https + 主机解析出的**所有** IP 都不是私有/回环/链路本地/保留段。
    """
    import ipaddress
    import socket

    if not url or not url.strip():
        raise ValueError("上游返回里没有图片 URL")
    p = urlparse(url.strip())
    if p.scheme != "https":
        raise ValueError(f"拒绝取图：只允许 https，收到 {p.scheme or '空'}://")
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("拒绝取图：URL 中没有主机名")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"拒绝取图：域名 {host!r} 解析失败（{e}）") from e
    if not infos:
        raise ValueError(f"拒绝取图：域名 {host!r} 没有解析到任何地址")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"拒绝取图：{host} 解析到内网地址 {ip}")


def _download_image(url: str, *, provider: bool = False) -> tuple[bytes, str]:
    """流式下载 + 硬性体积上限 —— 绝不一次性 read 到内存

    返回 (内容, content_type)。content_type 决定落盘扩展名，
    取自**真实响应头**而不是猜（4.5 默认回 jpeg，写死 .png 是撒谎）。

    provider=True 走 assert_provider_image_url（上游返回的 URL，
    按「不能是内网」判定）；否则走客户端那套域名白名单。
    """
    if provider:
        assert_provider_image_url(url)
    else:
        assert_downloadable_url(url)
    buf = bytearray()
    with requests.get(
        url,
        timeout=(5, max(REQUEST_TIMEOUT_SEC, 30)),   # (连接, 读取) 分开，读取给足
        stream=True,
        allow_redirects=False,                        # ★ 不让 302 把我带去内网
    ) as resp:
        resp.raise_for_status()

        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"生成图过大：声明 {int(declared)} 字节，上限 {MAX_DOWNLOAD_BYTES}")

        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    f"生成图超出上限 {MAX_DOWNLOAD_BYTES} 字节，已中断下载"
                )
    if not buf:
        raise ValueError("下载到的生成图为空")
    return bytes(buf), ctype

# ══════════════ 实测常量（2026-09-26，doubao-seedream-4-5）══════════════
#
# ⚠️ 官方文档写"像素范围 [1280x720, 4096x4096]"，但实测传 1280x720 会被拒：
#      "image size must be at least 3686400 pixels"
#    文档与实测冲突时以实测为准（S0 结论）。
#
# 实测：
#   档位字符串：仅接受 1k / 2k / 4k（大小写不敏感）
#               1.5K / 3K 属于 5.0 pro/flash、5.0 lite，4.5 不支持
#   像素格式  ：WIDTHxHEIGHT，下限 3,686,400 像素
#   输出格式  ：4.5 仅支持 jpeg（1.5/5.0 才支持 png）→ 落盘扩展名不能用 .png
MIN_PIXELS = 3_686_400          # 实测下限（4.5）
VALID_SIZE_TIERS = ("1k", "2k", "4k")

# ★ 为什么默认不用 "1k"：1k 档≈100 万像素，远低于上面实测的 369 万下限。
#   按 tier 语义推算，1k 在很多画幅下都会被 400 拒掉。
#   → 小图直接给 "2k"，并在下方保留升档重试兜底。
#   TODO(验证)：若将来确认某画幅下 1k 也满足下限，可把 ORIGIN_TIER_SMALL 改回 "1k"。
MIN_SAFE_TIER = "2k"

ASPECT_TO_PIXELS: dict[str, str] = {
    "3:5": "1664x2776",     # 约 3:5.005，461 万像素
    "2:3": "1728x2592",     # 精确 2:3，448 万
    "3:4": "1920x2560",     # 精确 3:4，491 万
    "4:5": "2048x2560",     # 精确 4:5，524 万
    "1:1": "2560x2560",     # 精确 1:1，655 万
    "16:9": "2560x1440",    # 精确 16:9，369 万（恰为下限）
    "9:16": "1440x2560",    # 精确 9:16，369 万（恰为下限）
    "origin": "",           # 跟随原图 → 由 pick_size_for_image 决定档位
}

# 尺寸类报错的关键词 —— 命中才允许升档重试，避免把「没钱了」也当成尺寸问题重试
_SIZE_ERROR_HINTS = (
    "image size",
    "at least",
    "too small",
    "不支持的尺寸",
    "size must be",
    "pixel",
)


# ────────────────────────── 尺寸解析 ──────────────────────────

def pick_size_for_image(reference_image_path: str, default: str = MIN_SAFE_TIER) -> str:
    """按原图分辨率决定输出档位，遵守「输出尺寸跟随原图」原则

    读不到尺寸时返回 default，不静默写死。
    """
    try:
        from PIL import Image                     # 延迟导入：只有真要读尺寸时才依赖 Pillow
        with Image.open(reference_image_path) as im:
            w, h = im.size
    except Exception:
        return default

    longest = max(w, h)
    if longest < 3000:
        return MIN_SAFE_TIER                      # ≈2k：满足实测下限的最小档
    return "4k"


def resolve_size(
    reference_image_path: str,
    size: str | None = None,
    aspect: str | None = None,
    aspect_wins: bool = False,
) -> str:
    """解析输出尺寸

    - size 为 None / "auto" / "" → 按原图自动选档
    - size 为 1k/2k/4k          → 原样（小写规范化）
    - size 为 "WxH"             → 校验下限后返回
    - aspect 给定时（如 "3:4"）  → ASPECT_TO_PIXELS 查表，保证画幅与像素一致

    aspect_wins=True 时画幅优先（构图比档位更重要）；默认 size 优先，
    避免调用方"顺手传了家族默认画幅"就把 follow-original 的诉求悄悄压掉。
    """
    if aspect_wins and aspect in ASPECT_TO_PIXELS and ASPECT_TO_PIXELS[aspect]:
        return ASPECT_TO_PIXELS[aspect]

    if not size or str(size).strip().lower() in ("auto", "default"):
        if aspect in ASPECT_TO_PIXELS and ASPECT_TO_PIXELS[aspect]:
            return ASPECT_TO_PIXELS[aspect]
        return pick_size_for_image(reference_image_path)

    s = str(size).strip()
    low = s.lower()
    if low in VALID_SIZE_TIERS:
        return low

    if "x" in low:
        try:
            w, h = (int(x) for x in low.split("x", 1))
        except ValueError:
            raise ValueError(f"无法解析尺寸：{size!r}，应为 '1k'/'2k'/'4k' 或 'WxH'")
        if w <= 0 or h <= 0:
            raise ValueError(f"尺寸 {size!r} 含非正值")
        if w * h < MIN_PIXELS:
            raise ValueError(
                f"尺寸 {w}x{h} 仅 {w * h} 像素，低于模型下限 {MIN_PIXELS}"
                f"（实测 doubao-seedream-4-5 要求，S0 结论）"
            )
        return f"{w}x{h}"

    raise ValueError(
        f"不支持的尺寸：{size!r}（本模型实测仅支持 1k/2k/4k 或 WxH；"
        f"1.5K/3K 属 5.0 系列，4.5 不支持）"
    )


def _looks_like_size_error(msg: str) -> bool:
    low = (msg or "").lower()
    return any(h.lower() in low for h in _SIZE_ERROR_HINTS)


def _is_transient_image_error(e: Exception) -> bool:
    """这个生图错误值不值得再试一次 —— 判定口径**复用 services/llm**

    ★ 为什么不在本文件手写关键词：
      之前这里有一份独立的 `transient = ... or "502" in msg ...`，
      与 services/llm._is_transient 各写一半、各漏一半，结果是
      「同一类上游错误，文本通道重试、生图通道不重试」——
      用户视角就是「有时候能出图有时候不能」，且完全无法解释。
      口径必须只有一个。

    ★ 为什么这类 503 必须重试（2026-10-03 实测）：
      中转站的 503 是「当前没有可用通道」，容量**成批释放**，
      隔 2 秒重试常常赶不上，隔 8~15 秒才吃得到额度。
      而 503 是瞬时返回（不等你几十秒），多试两次几乎不增加用户等待。
    """
    from services.llm import _is_transient      # 延迟导入：避免模块级循环依赖
    try:
        return bool(_is_transient(e))
    except Exception:                            # 判定本身不该成为新的失败来源
        logger.warning("生图错误瞬时性判定失败，按非瞬时处理：%s", type(e).__name__)
        return False


def _is_policy_image_error(e: Exception) -> bool:
    """命中上游内容策略 —— 重试无效，必须早退 + 给用户可执行的话

    口径同样复用 services/llm（避免又出现两份各写一半的表）。
    """
    from services.llm import looks_like_policy_error

    try:
        return bool(looks_like_policy_error(e))
    except Exception:
        return False


def friendly_error(e: Exception | str) -> str:
    """把上游异常翻译成一句用户看得懂、并且知道下一步该做什么的话

    ★ 为什么要这一层（2026-10-04 用户实测）：
      用户连点两次，第一次必弹 `BadRequestError: Error code: 400 - {...原始英文...}`，
      第二次就成功 —— 原文一路穿到界面上，既吓人又毫无 actionable 信息。
      原始报文一律进日志（排障要用），界面上只留中文 + 该怎么办。
    """
    # ★ 调用方给的是**字符串**（常见的 `friendly_error(last_err)`），
    #   也有直接给异常对象的 —— 两种形态必须得到同一个答案。
    #   曾经只在 isinstance(e, Exception) 分支里做策略判定，
    #   结果同一条策略报错：传异常给用户"换个说法"、传字符串却给兜底文案。
    from services.llm import looks_like_policy_error

    raw = str(e)
    low = raw.lower()
    if isinstance(e, Exception):
        if looks_like_policy_error(e):
            return "这段描述触发了上游的内容审核，换个说法再试一次（比如去掉品牌名、人物身份等字眼）。"
    if any(h in low for h in ("content polic", "moderation", "safety system",
                              "unsafe", "blocked by", "敏感", "违规", "审核")):
        return "这段描述触发了上游的内容审核，换个说法再试一次（比如去掉品牌名、人物身份等字眼）。"
    if any(h in low for h in ("您的请求无法", "无法完成", "no available",
                              "upstream", "gateway", "bad gateway")):
        # ★ 必须说清楚「不是你的照片/描述的锅」（2026-10-05 实测）：
        #   中转站图像分组整组不可用时，5 个模型 id 全部 502/503，
        #   用户看到的是自己的操作失败 —— 很自然会以为是自己那张图有问题，
        #   于是反复换图重试，其实每一次都在撞同一堵墙。
        return ("生图服务那边暂时没有可用通道（已自动重试多次）。"
                "这与你的照片和描述无关，等几分钟再试就好。")
    if "timeout" in low or "timed out" in low:
        return "上游响应超时（这次请求比较重），请稍后再试。"
    if "401" in low or "unauthor" in low or "api key" in low:
        return "服务密钥有问题，请联系开发者确认配置。"
    return "生图这一步失败了，请稍后重试；若反复出现请把这段话告诉开发者。"


# ── gpt-image 的画幅：只有三档 ──────────────────────────────
# images/edits 只接受 1024x1024 / 1536x1024 / 1024x1536，也就是 1:1 / 3:2 / 2:3。
OAI_ASPECT_SIZES = {
    "1:1": "1024x1024",
    "2:3": "1024x1536",
    "3:2": "1536x1024",
}


def _aspect_ratio(spec: str) -> float:
    """把 "3:4" 变成 0.75 —— 用于找最接近的可表达画幅"""
    try:
        w, h = (int(x) for x in str(spec).split(":", 1))
    except (ValueError, TypeError):
        return 1.0
    return (w / h) if h else 1.0


def _aspect_distance(a: float, b: float) -> float:
    """画幅之间该用「比例距离」而不是「绝对差」

    反例：5:4（1.25）到 1:1（1.0）与到 3:2（1.5）的**绝对差都是 0.25**，
    用绝对差会出现平局，且平局的胜者取决于字典顺序 —— 那不是规则，是巧合。
    改用 log(a/b) 之后：1.25 离 1.5 更近（0.182 < 0.223），判定稳定且符合直觉。
    """
    if a <= 0 or b <= 0:
        return float("inf")
    return abs(math.log(a / b))


def choose_oai_size(reference_image_path: str, aspect: str | None,
                    aspect_wins: bool = False) -> str:
    """给 gpt-image 选画幅 —— 只有三档可表达，所以「能不能表达」是首要问题

    ★ 为什么要单独定规则（2026-10-03 实测，第二次踩同一个坑）：
      家族的默认画幅多是 3:4、4:5，而 gpt-image 只认 1:1 / 2:3 / 3:2。
      旧代码无条件用家族画幅，于是**方形原图被硬改成 1024×1536 竖版** ——
      用户看到的就是「我的方图怎么变成长条了」。
      而项目立身之本是**原图保真**，构图与比例属于"强继承"档（见 card 的
      transfer_scope）。所以规则改成：

        · 家族画幅**恰好**是这三档之一   → 尊重家族；
        · 表达不了（3:4、4:5、16:9 …）  → **跟随原图**（保真优先）；
        · 调用方显式 aspect_wins=True   → 取最接近的一档（用户自己选了画幅）。

      这样 `size=None` 那个「遵守跟随原图」的注释才不是一句空话。
    """
    try:
        from PIL import Image
        with Image.open(reference_image_path) as im:
            w, h = im.size
        origin = "1024x1536" if h > w else ("1536x1024" if w > h else "1024x1024")
    except Exception:                          # 读不到尺寸就用方图，不猜
        return "1024x1024"

    spec = str(aspect or "").strip()
    if not spec or spec == "origin":
        return origin
    if spec in OAI_ASPECT_SIZES:
        return OAI_ASPECT_SIZES[spec]
    if aspect_wins:
        target = _aspect_ratio(spec)
        best = min(OAI_ASPECT_SIZES.items(),
                   key=lambda kv: _aspect_distance(_aspect_ratio(kv[0]), target))
        return best[1]
    return origin


def _next_tier(current: str) -> str | None:
    """返回下一档；已经是最大档则返回 None"""
    if current.startswith(("1", "2", "3", "4", "5", "6", "7", "8", "9")) and "x" in current:
        return "4k"                       # 显式像素值被拒 → 改用档位让模型自己算画幅
    ladder = ["1k", MIN_SAFE_TIER, "4k"]
    try:
        i = ladder.index(current.lower())
    except ValueError:
        return MIN_SAFE_TIER
    return ladder[i + 1] if i + 1 < len(ladder) else None


# ────────────────────────── 主流程 ──────────────────────────

def _aspect_mismatch_warning(save_path: str, requested_size: str | None,
                            reference_image_path: str | None = None) -> str:
    """出图后校验宽高比（最后一道闸，10-03 新增 / 10-04 修正判据）。

    ★ 2026-10-04 修正过一次判据，理由是实测出来的**大面积误报**：
      旧实现拿「输出图」和**原图宽高比**比 —— 但 gpt-image 只有 1:1 / 2:3 / 3:2
      三档可用（见 choose_oai_size），意味着任何 4:3 / 3:4 / 16:9 的原图
      注定要对不上，偏差 25% ≫ 8% 阈值 ⇒ **每一张非方图都会弹警告**。
      实测：4:3 横图要 1536×1024、拿到 1536×1024（完全符合要求）照样报警。
      用户看到的是"生成尺寸不符"，可图其实没问题 —— 校验器自己成了故障源。

      正确的判据是：**输出 vs 我们向上游实际请求的尺寸**。
      上游不听话（要竖的给方的）才是真问题，那才值得提示。
      「原图比例无法精确表达」是**已知取舍**，不是错误，只进日志（debug 级）。

    返回空串 = 通过；返回文案 = 纯提示，不阻断（图已生成、钱已花）。
    """
    try:
        from PIL import Image
        with Image.open(save_path) as im:
            ow, oh = im.size
        actual = ow / oh if oh else 0

        # 我们到底要了多大 —— 这是唯一公平的评判基准
        want_w = want_h = 0
        if requested_size and "x" in str(requested_size):
            try:
                want_w, want_h = (int(x) for x in str(requested_size).lower().split("x", 1))
            except ValueError:
                want_w = want_h = 0
        if not (want_w and want_h):
            logger.debug("画幅校验跳过：拿不到请求尺寸（requested=%r）", requested_size)
            return ""
        expect = want_w / want_h
        if expect and abs(actual - expect) / expect > 0.08:
            logger.warning("画幅偏差：上游给了 %dx%d，我们要求 %dx%d",
                           ow, oh, want_w, want_h)
            return (f"输出画幅（{ow}×{oh}）与我们要求的（{want_w}×{want_h}）不一致——"
                    "如画面观感异常，请重新生成一次")

        # 只是记录「原图比例无法精确表达」——这是三档画幅的已知取舍，不是错误
        if reference_image_path:
            try:
                with Image.open(reference_image_path) as im:
                    rw, rh = im.size
                if rh and abs(actual - rw / rh) / (rw / rh) > 0.08:
                    logger.info("原图 %dx%d（%s）无法精确表达三档画幅，已按最接近的 %dx%d 输出",
                                rw, rh, "竖图" if rh > rw else "横图", ow, oh)
            except Exception:                                        # noqa: BLE001
                pass
    except Exception as e:                     # 校验失败绝不影响出图结果
        logger.debug("画幅校验跳过：%s", e)
    return ""


def _resp_field(obj: Any, name: str) -> Any:
    """读上游响应里的一个字段 —— **对象和 dict 都认**

    ★ 为什么要兼容 dict：走 SDK 时拿到的是 pydantic 对象，
      但中转站偶尔会把响应原样透传成 JSON dict（换分组、改协议时最常见）。
      只认 getattr 的话，换个上游实现就从「字段改名」升级成「整条链路崩」。
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _resp_field_names(obj: Any) -> list[str]:
    """尽量列出响应项的字段名 —— 排障时「它到底返回了什么」比「它没返回什么」有用"""
    if obj is None:
        return []
    if isinstance(obj, dict):
        return sorted(obj.keys())
    for attr in ("model_fields_set", "__fields_set__"):
        names = getattr(obj, attr, None)
        if names:
            return sorted(names)
    d = getattr(obj, "__dict__", None)
    return sorted(d.keys()) if d else []


def _extract_image_bytes(resp: Any, model_name: str) -> tuple[bytes | None, str, str]:
    """从上游响应里取出图片字节 —— **兼容两种回图形态**

    返回 (content, content_type, reason)；content 为 None 表示没取到，
    reason 只进日志/错误详情（给用户看的是 friendly_error 的中文）。

    ① data[0].b64_json —— 官方 OpenAI 形态，一直都是这条
    ② data[0].url      —— 2026-10-05 实测：换到「image2 原生分组」后
       上游改成了这条（https://cdn.jd23kjs.work/...，18.8s 就出图）。
       旧代码只认 ①，于是「图已经生成好」被我们判成失败 ——
       上游改协议而我们不跟，是最容易被误判成「服务坏了」的一类故障。

    ★ 为什么 URL 形态要用 provider=True 而不是域名白名单：
      白名单是给「客户端传来的地址」设计的（可能是任何人塞的）；
      这里的地址是我们信任的供应商返回的，硬编码 CDN 域名反而脆。
      判据换成「https 且不解析到内网」，既不误伤也不会成为 SSRF 通道。
    """
    data = _resp_field(resp, "data")
    item = (data or [None])[0] if isinstance(data, (list, tuple)) else None
    b64 = _resp_field(item, "b64_json")
    img_url = _resp_field(item, "url")

    if b64:
        try:
            return base64.b64decode(b64), "image/png", ""
        except Exception as e:                       # noqa: BLE001
            return None, "", f"b64_json 解码失败：{type(e).__name__}: {e}"

    if img_url:
        # ValueError（安全拒绝 / 超限）往上抛给调用方，让它按「下载被拒」记录审计
        content, ctype = _download_image(img_url, provider=True)
        return content, ctype, ""

    fields = _resp_field_names(item) or "（拿不到字段名）"
    return None, "", f"既没有 b64_json 也没有 url；data[0] 字段={fields}（模型 {model_name}）"


_LEGACY_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 成品文件名的时间戳尾戳：_<yyyymmdd>_<hhmmss>_<uuid片段>
#   ★ 片段长度写成 4~8 而不是固定 6：旧的实现先把整名截到 40 字符再落盘，
#     于是第二代成品的尾戳会被砍掉 2 位（…_908d 而不是 …_908dbd）。
#     认尾戳的目的是「连修链条不越滚越长」，所以宁可放宽也不要漏判。
_GEN_TAIL_RE = re.compile(r"_\d{8}_\d{6}_[0-9a-f]{4,8}$")


def _generated_dir(reference_image_path: str, now: datetime | None = None) -> Path:
    """生成图落盘目录 —— 与参考图同一个会话，但**深度恒定**

    ★ 为什么不能直接拿 ref_parent 往下拼（2026-10-05 实测实锤）：
      旧写法是 `ref_parent / <今天>`。而修复链路的参考图是**上一版成品**，
      上一版成品已经躺在 `<sid>/<日期>/<日期>/` 里 —— 于是每修一次就多嵌一层。
      实测第三次路径：
        images/<sid>/2026-10-05/2026-10-05/2026-10-05/gen_gen_upload_….png
      Windows MAX_PATH(260) 下连修几轮就写不进去，画廊 rglob 也越扫越深。

      现在改成「命名空间 + 今天」两级，与 upload.py 的会话约定同源：
        · 有会话命名空间 → images/<sid>/<今天>/
        · 旧版平铺 / _seed / examples → images/<今天>/（保持既有布局）
      无论修到第几版，深度都一样。**异常/越界参考图**退回到旧行为，不砸主流程。
    """
    now = now or datetime.now()
    try:
        ref = Path(reference_image_path).resolve()
        rel = ref.relative_to(Path(IMAGE_STORAGE_DIR).resolve())
    except (OSError, ValueError):
        return resolve_subdir(IMAGE_STORAGE_DIR, now)
    parts = rel.parts
    head = parts[0] if len(parts) > 1 else ""
    # 第一段是个日期目录 → 说明是旧版平铺（default 会话的历史数据），
    # 不要把它当成命名空间，否则会嵌出 <日期>/<日期> 两层。
    base = Path(IMAGE_STORAGE_DIR)
    if head and not _LEGACY_DATE_DIR_RE.match(head):
        base = base / head
    return resolve_subdir(base, now)


def _base_stem(reference_image_path: str) -> str:
    """成品文件名里「原始素材那一段」 —— 连续修复时不许层层叠前缀

    旧写法直接用参考图的 stem：参考图本身是上一版成品时，名字会一路长成
    gen_gen_gen_upload_xxx_时间戳_uniq_时间戳_uniq_时间戳_uniq.png，
    既难读又逼近路径长度上限。这里把历次修复加的 `gen_` 前缀和时间戳尾
    逐层剥掉，让第 N 次修复与第 1 次的命名长度一致。
    剥不动时就停（循环有上限），宁可名字长一点也不无限循环。
    """
    name = os.path.splitext(os.path.basename(reference_image_path))[0]
    for _ in range(8):
        m = _GEN_TAIL_RE.search(name)
        if m:
            name = name[: m.start()]
            continue
        if name.startswith("gen_"):
            name = name[4:]
            continue
        break
    return (name or "image")[:40]


def _save_generated(content: bytes, content_type: str, reference_image_path: str) -> str:
    """按响应头的真实类型落盘，返回绝对路径

    ★ 多用户（2026-10-03 公开版）：生成图跟参考图放同一个会话命名空间 ——
      参考图已由上传层落在 images/<会话>/<日期>/，生成图继承该会话后，
      画廊按会话列目录就自动隔离，无需把 session 一路传穿生图层。
      ★ 2026-10-05：继承的方式从「参考图的父目录」改成「命名空间根目录」
        （见 `_generated_dir`）——修复链路会把目录一层层套深，必须截断。
      参考图不在存储目录内（异常场景）→ 回退到存储根下的今天目录。
    """
    ctype = (content_type or "").lower()
    if "png" in ctype:
        out_ext = ".png"
    elif "webp" in ctype:
        out_ext = ".webp"
    elif "jpeg" in ctype or "jpg" in ctype:
        out_ext = ".jpg"
    else:
        out_ext = ".jpg"      # 4.5 默认 jpeg，兜底也用 jpg

    stem = _base_stem(reference_image_path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    uniq = uuid.uuid4().hex[:6]
    subdir = _generated_dir(reference_image_path)
    filename = f"gen_{stem}_{stamp}_{uniq}{out_ext}"
    path = subdir / filename
    with open(path, "wb") as f:
        f.write(content)
    return str(path)


def generate_image_with_reference(
    reference_image_path: str,
    prompt: str,
    size: str | None = None,
    aspect: str | None = None,
    aspect_wins: bool = False,
    max_images: int = 1,
) -> dict[str, Any]:
    """参考图生图

    返回 {"success", "image_path", "url", "filename", "size", "error", "penultimate"}
    失败时 error 是给调用方/用户看的中文说明，成功时 url 可直接给前端用。
    """
    assert_ready_for_generation()          # 没配 key 就在这里给出可读报错，而不是 401

    if not os.path.exists(reference_image_path):
        return {"success": False, "error": f"参考图不存在：{reference_image_path}"}

    with step("参考图编码", path=os.path.basename(reference_image_path)):
        try:
            with open(reference_image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
            ext = os.path.splitext(reference_image_path)[1].lower()
            mime = "image/png" if ext == ".png" else (
                "image/webp" if ext == ".webp" else "image/jpeg"
            )
        except OSError as e:
            return {"success": False, "error": f"读取参考图失败：{e}"}

    resolved = resolve_size(reference_image_path, size, aspect, aspect_wins)

    # ── OpenAI images 后端（images/edits + images/generations）─────
    # images/edits（带参考图，multipart），返回 b64_json，直接落盘不走 URL 下载
    # —— 原 SSRF 护栏（host 白名单等）只对"远端给 URL 再去下载"的模式有意义，
    #    b64 模式下内容不落地网络请求，天然没有这条通道。
    if IMAGE_BACKEND == "openai":
        oai_size = resolved                    # 先给默认值：读尺寸就崩时也能如实回报
        attempts = 0                           # 真实尝试次数：结果里要如实回报（旧版写死 1 是谎报）
        try:
            with open(reference_image_path, "rb") as f:
                ref_bytes = f.read()
            # ★ 画幅三档选择：只接受 1:1 / 2:3 / 3:2，家族画幅表达不了时跟随原图。
            #   完整规则见 choose_oai_size（实测教训：方图被强改成竖版长条）。
            oai_size = choose_oai_size(reference_image_path, aspect, aspect_wins)
            plan = image_plan()
            last_err = ""
            resp = None
            used_model = IMAGE_MODEL
            # ★ 整轮预算：把所有重试和等待都关进一个 deadline 里。
            #   没有它，5 步 × 150s 的上限叠起来又能拖到十几分钟 ——
            #   而那正是用户截图里「等到怀疑人生然后失败」的成因。
            deadline = time.monotonic() + max(60, IMAGE_TOTAL_BUDGET_SEC)
            per_attempt = max(30, min(IMAGE_ATTEMPT_TIMEOUT_SEC, IMAGE_TIMEOUT_SEC))
            for idx, (model, wait) in enumerate(plan):
                remaining = deadline - time.monotonic()
                if remaining <= 5:
                    logger.warning("生图总预算已用尽（%.0fs），停止重试", IMAGE_TOTAL_BUDGET_SEC)
                    last_err = last_err or "上游持续无可用通道，已超过本轮等待预算"
                    break
                if wait:
                    time.sleep(min(wait, max(0.0, remaining - 1)))    # 别睡过 deadline
                attempts += 1
                eff_timeout = max(20.0, min(float(per_attempt), remaining))
                try:
                    with step("调用 gpt-image", size=oai_size, model=model,
                              attempt=attempts, timeout=int(eff_timeout)):
                        resp = client.images.edit(
                            model=model,
                            image=(os.path.basename(reference_image_path), ref_bytes, mime),
                            prompt=prompt,
                            n=max(1, min(int(max_images), 4)),
                            size=oai_size,    # ★ 画幅跟原图/家族走（缺失时模型自选 → 方图变长条）
                            timeout=eff_timeout,
                        )
                    used_model = model
                    break
                except Exception as e:
                    # ★ 中转站上游波动是常态：502/503/504/429/超时都是**临时性**错误
                    #   （实测一次真实生成就是 502 失败、手动重试才成功；
                    #    2026-10-03 线上又连续 3 次 503「没有可用通道」）。
                    #   判定复用 services/llm 的同一套口径，避免两处各写一半、
                    #   结果一边重试一边不重试。
                    last_err = f"{type(e).__name__}: {e}"
                    if _is_policy_image_error(e):
                        # ★ 策略类是重试无效的：同样的提示词再来一万次也一样。
                        #   早退 + 给一句能改的话，别白烧钱也别让用户干等。
                        logger.warning("gpt-image 内容策略拒绝（第 %d 次）：%s",
                                       attempts, last_err[:160])
                        audit("generate_image_policy_blocked", attempt=attempts,
                              model=model, error=last_err[:200])
                        break
                    if _is_transient_image_error(e) and idx < len(plan) - 1:
                        logger.warning("gpt-image 暂时性失败（第 %d 次，模型 %s），%.0fs 后重试：%s",
                                       attempts, model, wait, last_err[:120])
                        audit("generate_image_retry", attempt=attempts,
                              model=model, error=last_err[:200])
                        continue
                    break

            if resp is None:
                logger.error("gpt-image 生图失败（共 %d 次尝试）：%s", attempts, last_err)
                audit("generate_image_failed", backend="openai", attempts=attempts,
                      error=last_err[:300])
                return {"success": False, "image_path": None, "url": None,
                        "filename": None, "size": oai_size,
                        # ★ 给用户看的必须是「怎么办」，不是上游原文
                        "error": friendly_error(last_err),
                        "error_raw": last_err[:500],
                        "attempts": attempts}

            # ★ 上游有两种回图形态，两种都得认（2026-10-05 实测）：
            #   ① data[0].b64_json —— 官方 OpenAI 形态（我们一直是这条）
            #   ② data[0].url      —— 换到 image2 原生分组后**改成了这条**
            #      （实测 https://cdn.jd23kjs.work/...，18.8s 就出图）
            #   之前只认 ①，于是图明明生成了，却被判成"没有 b64_json"直接失败 ——
            #   典型的「上游改协议、我们不跟」造成的假故障。判定见 _extract_image_bytes。
            content, ctype, raw_reason = _extract_image_bytes(resp, used_model)
            if content is None:
                logger.error("gpt-image 返回里没有可用图片（模型 %s）：%s",
                             used_model, raw_reason)
                return {"success": False, "image_path": None, "url": None,
                        "filename": None, "size": oai_size,
                        "error": "生图服务返回了无法识别的结果格式，请稍后再试。",
                        "error_raw": raw_reason,
                        "attempts": attempts}
            save_path = _save_generated(content, ctype, reference_image_path)
            audit("generate_image", backend="openai", model=used_model,
                  reference=os.path.basename(reference_image_path),
                  size=oai_size, prompt_chars=len(prompt), attempts=attempts,
                  out=os.path.basename(save_path))
            return {
                "success": True,
                "image_path": save_path,
                "url": to_url(save_path),
                "filename": os.path.basename(save_path),
                "size": oai_size,
                # 判据是「我们向上游要的尺寸」，不是原图比例（见函数 docstring）
                "aspect_warning": _aspect_mismatch_warning(
                    save_path, oai_size, reference_image_path),
                "error": None,
                "attempts": attempts,
            }
        except Exception as e:
            # 流程本身出错（读图失败、落盘失败、底层库异常）——
            # 与上面「逐次尝试」的错误区分开：这里一次都没真正发出生图请求。
            last_err = f"{type(e).__name__}: {e}"
            logger.error("gpt-image 生图流程失败：%s", last_err)
            audit("generate_image_failed", backend="openai", phase="pipeline",
                  error=last_err[:300])
            return {"success": False, "image_path": None, "url": None,
                    "filename": None, "size": oai_size, "error": last_err,
                    "attempts": attempts}

    attempts = 0
    last_err = ""

    # ★ 尺寸重试阶梯：最多尝试 3 次不同档位，每次都要确实遇到"尺寸类"报错才继续
    while attempts < 3:
        attempts += 1
        try:
            with step("调用 Seedream", size=resolved, attempt=attempts):
                response = client.images.generate(
                    model=SEEDREAM_MODEL,
                    prompt=prompt,
                    size=resolved,
                    n=max(1, min(int(max_images), 4)),
                    response_format="url",
                    extra_body={
                        "image": f"data:{mime};base64,{img_b64}",
                        "watermark": False,
                    },
                    timeout=REQUEST_TIMEOUT_SEC,
                )
            image_url = response.data[0].url

            with step("下载生成图") as ctx:
                try:
                    # 同样是上游返回的地址 → provider=True（按「不能是内网」判定，
                    # 不必把每家 CDN 都写进白名单）
                    content, ctype = _download_image(image_url, provider=True)
                except ValueError as e:
                    # SSRF / 超限：这是**安全拒绝**，不是可重试的尺寸问题
                    logger.error("生成图下载被拒（%s）", e)
                    audit("download_blocked", reason=str(e)[:200],
                          host=urlparse(image_url).hostname or "")
                    return {
                        "success": False, "image_path": None, "url": None,
                        "filename": None, "size": resolved, "error": str(e),
                        "attempts": attempts,
                    }
                except requests.RequestException as e:
                    last_err = f"下载生成图失败：{type(e).__name__}: {e}"
                    logger.error("下载生成图失败：%s", e)
                    return {
                        "success": False, "image_path": None, "url": None,
                        "filename": None, "size": resolved, "error": last_err,
                        "attempts": attempts,
                    }
                ctx["bytes"] = len(content)
                # content-type 来自真实响应头，由它决定扩展名
                save_path = _save_generated(content, ctype, reference_image_path)

            audit(
                "generate_image",
                reference=os.path.basename(reference_image_path),
                size=resolved,
                attempts=attempts,
                prompt_chars=len(prompt),
                out=os.path.basename(save_path),
            )
            return {
                "success": True,
                "image_path": save_path,
                "url": to_url(save_path),
                "filename": os.path.basename(save_path),
                "size": resolved,
                # 判据是「我们向上游要的尺寸」，不是原图比例（见函数 docstring）
                "aspect_warning": _aspect_mismatch_warning(
                    save_path, resolved, reference_image_path),
                "error": None,
                "attempts": attempts,
            }

        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if not _looks_like_size_error(str(e)):
                break
            nxt = _next_tier(resolved)
            if not nxt:
                break
            logger.warning("尺寸 %s 被拒（%s），升档到 %s 重试", resolved, str(e)[:80], nxt)
            resolved = nxt

    logger.error("生图失败：%s", last_err)
    audit("generate_image_failed", reference=os.path.basename(reference_image_path),
          error=last_err[:300], attempts=attempts)
    return {
        "success": False,
        "image_path": None,
        "url": None,
        "filename": None,
        "size": resolved,
        "error": last_err,
        "attempts": attempts,
    }
