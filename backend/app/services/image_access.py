"""图片读取授权 —— 会话隔离在「取图」这一层的唯一入口

════════ 为什么会有这个文件 ════════

画廊列表已经按会话隔离（谁也列不到别人的图），但**取图曾经完全不设防**：
`app.mount("/images", StaticFiles(...))` 是静态挂载，任何知道路径的人都能拿。
配合「画廊会把 URL 明文发给调用方」这一点，等于门没锁、只是没挂门牌。

所以这里只认一条规则（``may_read``）：

    这张图的**路径第一段**，是不是**请求者的会话**？

    · _seed/（公共展示图）         → 人人可读
    · <自己的 sid>/...            → 本人可读
    · <别人的 sid>/...            → 404（不是 403：不给「它存在」这个信息）
    · 内网（PUBLIC_MODE=0）default → 全可读（单机时代的旧语义，保留）

════════ 为什么浏览器够得着这个校验 ════════

`<img src>` **发不出自定义请求头** —— 前端把 sid 放在 X-Session-Id 里，
图片请求一次也带不上。所以同时还把会话凭证写进了 **Cookie**（middleware 负责），
浏览器对同站图片会自动带上它。请求头与 Cookie 缺一不可：只靠请求头，
图片请求等于永远匿名；只靠 Cookie，脚本调用方又会失去身份。

★ 放 services/ 而不是 infra/：这里要读 identity 的会话常量，
  而 identity 又依赖 infra —— 反过来 import 就成环了。
"""
from __future__ import annotations

from pathlib import Path

# 公共资源目录：所有访客都能看
#   _seed/      种子展示图（首页示例流）
#   examples/   **家族示例图**（2026-10-05 补：示例图一进发布包就撞上了鉴权 ——
#               访客没有会话 → default → 非本人目录 → 404，家族卡片集体裂图。
#               它和种子图同性质：产品自带的公共素材，不是任何人的产出。）
_PUBLIC_DIRS = ("_seed", "seed", "examples")


def _public_mode() -> bool:
    from config import PUBLIC_MODE
    return PUBLIC_MODE


def owner_of(abs_path: str | Path) -> str:
    """图片绝对路径 → 它所属的命名空间（路径第一段）；不属于任何会话时给空串"""
    from config import IMAGE_STORAGE_DIR

    try:
        rel = Path(abs_path).resolve().relative_to(Path(IMAGE_STORAGE_DIR).resolve())
    except ValueError:
        return ""                      # 越界：调用方应先走 normalize_reference
    parts = rel.parts
    return parts[0] if len(parts) > 1 else ""


def may_read(abs_path: str | Path, sid: str) -> bool:
    """请求者 sid 有没有资格读这张图 —— 列表/取图/缩略图/打包共用这一个答案

    ★ 一致性是硬要求：曾经「列表按会话过滤、取图却门户大开」，
      两条路径各判一半，用户视角就是「刷新时有、直连时也有」的随机行为。
    """
    from services.identity import DEFAULT_SESSION

    try:
        p = Path(abs_path).resolve()
    except OSError:
        return False
    if not p.is_file():
        return False

    first = owner_of(p)
    if not first:
        # 存储根目录的平铺文件：只有内网那个唯一用户能看
        if sid == DEFAULT_SESSION:
            return not _public_mode()
        return False
    if first.startswith("."):
        return False                    # .thumbs 等派生缓存，不当作品外发
    if first in _PUBLIC_DIRS:
        return True

    if sid == DEFAULT_SESSION:
        return not _public_mode()       # 内网放行；公开版的「身份未知」一律拒

    return first == sid


_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}


def media_type(abs_path: str | Path) -> str:
    """按扩展名给 Content-Type —— 不给浏览器嗅探的机会"""
    return _MEDIA_TYPES.get(Path(abs_path).suffix.lower(), "application/octet-stream")
