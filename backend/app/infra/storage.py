"""基础设施层 —— 存储路径管理

单一真源原则
------------
所有「文件落在哪」的判断都在这里，别处不再出现裸字符串路径。
这样将来换 Electron sidecar 或 Docker（容器内路径完全不同）时，改一个文件就够。

提供：
- to_url()：绝对路径 → 前端可直接访问的 /images/... URL
- from_url()：URL → 绝对路径（带目录穿越防护）
- resolve_generated_dir()：生成图按日期分子目录
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from config import GENERATED_SUBDIR_FMT, IMAGE_STORAGE_DIR
from infra.logging import logger

def retired_seed_names() -> set[str]:
    """退役种子图名单 —— 打包放过的图里，哪些已经不想再展示了

    ★ 为什么要这么绕（2026-10-05 实测）：
      发布平台是**上传覆盖**语义 —— 包里删掉某个文件，线上那份**不会消失**
      （与 agent.db 同一个坑）。所以"把图从首屏删掉"没法靠删文件实现。

      于是改成应用层过滤：文件还在磁盘上（平台删不掉），但
      ① 画廊扫描直接跳过它；② /thumb 与 /images 也不再认它。
      名单放在 ``storage/images/_seed/retired.txt``（纯文本、一行一个文件名），
      它会随包上传，因此**发布一次即生效**，而且是白名单式的显式登记 ——
      不靠"文件不存在"这种隐式约定，过期后也不会自己复活。

    解析容错：文件不存在 / 读失败 / 行内注释，一律跳过，不影响启动。
    """
    marker = Path(IMAGE_STORAGE_DIR) / "_seed" / "retired.txt"
    try:
        if not marker.exists():
            return set()
        out: set[str] = set()
        for line in marker.read_text(encoding="utf-8").splitlines():
            name = line.split("#", 1)[0].strip()
            if name:
                out.add(name)
        return out
    except OSError as e:                                # 读不了就当没有，别把画廊搞挂
        logger.warning("读取退役种子图名单失败：%s", e)
        return set()


def is_retired_seed(path: Path | str) -> bool:
    """这个文件是否已被列入退役名单（按文件名匹配）"""
    try:
        return Path(path).name in retired_seed_names()
    except (OSError, ValueError):
        return False


__all__ = [
    "IMAGE_STORAGE_DIR",
    "to_url",
    "from_url",
    "relative_key",
    "resolve_subdir",
    "ensure_within",
    "retired_seed_names",
    "is_retired_seed",
]


def ensure_within(path: Path, root: Path | str | None = None) -> Path:
    """目录穿越防护：解析后必须仍在 root 之内

    防止 `../../etc/passwd` 这类输入被当作图片路径处理。

    ★ root 在**调用时**求值，不在定义时固化：
      曾经写成 `def ensure_within(path, root: Path = IMAGE_STORAGE_DIR)`，
      默认参数在 import 那一刻就绑死了本模块的 IMAGE_STORAGE_DIR，
      而同文件的 `from_url` / `relative_key` 用的是**运行期**那份全局变量。
      于是测试或运维把存储根重定向之后：拼路径用新根、校验却用旧根，
      每张图都报「路径越界」，而错误信息里的两个目录长得毫不相干 ——
      排查成本极高，必须让同一个模块只有一种口径。
    """
    resolved = Path(path).resolve()
    root_resolved = _resolved_root(root)
    if resolved == root_resolved or root_resolved in resolved.parents:
        return resolved
    raise ValueError(f"路径越界：{path} 不在允许目录 {root_resolved} 内")


# ★ 存储根的 resolve() 结果memo —— 因为它在一个进程内是**常量**。
#   实测（2026-10-07 画廊压测）：Windows 上 `Path.resolve()` 要走
#   GetFinalPathNameByHandle，**每个文件约 0.6ms**；画廊要给 831 个文件
#   各拼一次 URL，于是光是"把根目录解析一遍"就重复了 831 次，
#   实测占冷扫描 0.55s 里的约 0.28s —— 比真正的 stat 还贵。
#   根只有一个，解析一次就够。
#   ★ 为什么可以缓存：`resolve()` 的结果只取决于路径本身，
#     而根目录在进程生命周期内不该变（测试要改也会先改
#     `config.IMAGE_STORAGE_DIR`，key 随之变化 → 自然失效）。
#   ★ 缓存 key 用「原始根字符串」而不是解析结果：这样运维改根目录时
#     key 会变、缓存自动作废，不会拿旧根去校验新路径（那正好是
#     ensure_within 注释里警告过的"两个目录长得毫不相干"）。
_RESOLVED_ROOT_CACHE: dict[str, Path] = {}


def _resolved_root(root: Path | str | None = None) -> Path:
    """存储根的绝对化结果（进程内缓存）

    相对路径**不缓存**：它的 resolve() 依赖进程 cwd，
    而 cwd 在运行期可能被chdir 改变 —— 缓存下来就是一颗定时炸弹。
    """
    raw = str(root if root is not None else IMAGE_STORAGE_DIR)
    if not os.path.isabs(raw):
        return Path(raw).resolve()
    cached = _RESOLVED_ROOT_CACHE.get(raw)
    if cached is None:
        cached = Path(raw).resolve()
        _RESOLVED_ROOT_CACHE[raw] = cached
    return cached


def relative_key(path: Path, *, strict: bool = False) -> str:
    """绝对路径 → 相对 image 根目录的 key（用于生成 URL 与去重）

    落盘结构: IMAGE_STORAGE_DIR/2026-09-26/gen_xxx.jpg
    key 就是:  2026-09-26/gen_xxx.jpg

    ★ strict=True 时越界直接抛错（默认降级为 basename 并留日志）。
      曾经的坑：越界时静默返回 basename，to_url 也就**完全不做越界校验** ——
      一个存到目录外的文件照样能拼出 /images/xxx.jpg 的 URL。
    """
    try:
        return str(Path(path).resolve().relative_to(_resolved_root()))
    except ValueError:
        if strict:
            raise
        logger.warning("relative_key 越界（降级为文件名）：%s", path)
        return Path(path).name


def to_url(path: Path | str, *, strict: bool = False) -> str:
    """本地绝对路径 → 前端可直接访问的 URL

    审查报告 P1-10 的根因：
        image_generator 落盘到 `images/2026-09-26/gen_xxx.jpg`
        前端却只取文件名拼 `/images/gen_xxx.jpg` → 丢了日期子目录 → 404
    → 现在由后端统一给出完整 URL，前端不做任何字符串拼接。
    """
    return "/images/" + relative_key(Path(path), strict=strict).replace("\\", "/")


def from_url(url_or_key: str) -> Path:
    """URL / key → 本地绝对路径（带越界防护）"""
    key = url_or_key.replace("\\", "/")
    # ★ 逐个剥前缀；剥完剩下的才是 key。原实现里循环体内的
    #   `if key.startswith(("http://","https://")): continue` 是死分支
    #   （剥完前缀不可能再以协议头开头），疑似本意是拒绝却写成了空操作。
    for prefix in ("/images/", "images/", "http://", "https://"):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break                      # 只剥一层，剥完交给下面的统一检查
    if "://" in key:                   # 剥完仍含协议头 → 远程 URL，不是本地存储
        raise ValueError(f"不是本地存储路径：{url_or_key}")
    if "\x00" in key:                  # NUL 字节在 Windows 路径里会截断，显式拒绝
        raise ValueError("存储 key 含非法字符（NUL）")
    key = key.lstrip("/")
    if not key or key.startswith("."):
        raise ValueError(f"非法存储 key：{url_or_key}")
    return ensure_within(Path(IMAGE_STORAGE_DIR) / key)


def resolve_subdir(base: Path | None = None, now: datetime | None = None) -> Path:
    """按日期生成子目录，并保证存在

    为什么要分子目录：单张示图反复生成（变体对比 / 版本链 / 回归测试）
    若全平铺在同一个目录，几千张后 ls 一次要几秒，且不利于按天归档。
    """
    base = Path(base or IMAGE_STORAGE_DIR)
    now = now or datetime.now()
    sub = base / now.strftime(GENERATED_SUBDIR_FMT)
    sub.mkdir(parents=True, exist_ok=True)
    return sub
