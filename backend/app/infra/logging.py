"""基础设施层 —— 结构化日志与审计追踪

为什么单独一层
--------------
方案第 9 章要求「生成前确认 + 目录白名单 + 审计日志」。
旧版用 `print()` 打日志：没有级别、没有耗时、没有 trace_id，
出问题时无法回答「这次生成用的是哪个模板、哪套参数、花了多久」。

这里提供：
- trace_id：一次请求的唯一标识，串起「上传 → 渲染 → 生成 → 落盘」全链路
- 耗时装饰器：每一步的毫秒数（复试演示时能直接贴出来）
- 审计事件：生成行为留痕（落到本地 jsonl，不依赖任何外部服务）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Iterator

from config import STORAGE_DIR

LOG_DIR = STORAGE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_FILE = LOG_DIR / "audit.jsonl"

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=_FORMAT, datefmt="%H:%M:%S")
logger = logging.getLogger("travelnote")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def _safe_json(obj: Any, limit: int = 400) -> str:
    """日志里塞任意对象是危险的：调用方什么都可能写进 fields。

    实测踩点：`image_generator` 会把下载字节数写进 ctx（无害），
    但只要有人写进一个 requests.Response 或 PIL.Image，
    json.dumps 直接抛 TypeError → **日志把主流程带崩**。
    所以 default=str 兜底 + 长度截断。
    """
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = repr(obj)
    return s if len(s) <= limit else s[:limit] + f"…({len(s)}B)"


@contextmanager
def step(name: str, **fields: Any) -> Iterator[dict]:
    """记录一步操作的耗时，失败时也会记录

    用法：
        with step("render", family="zine") as ctx:
            result = render_family(...)
            ctx["chars"] = len(prompt)
    """
    ctx: dict[str, Any] = dict(fields)
    t0 = time.perf_counter()
    logger.info("→ %s %s", name, _safe_json(fields) if fields else "")
    try:
        yield ctx
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        logger.error("✗ %s 失败 %.0fms: %s", name, ms, type(e).__name__, exc_info=False)
        logger.debug("    详情: %s", e)
        raise
    else:
        ms = (time.perf_counter() - t0) * 1000
        extra = {k: v for k, v in ctx.items() if k not in fields}
        logger.info("✓ %s %.0fms %s", name, ms, _safe_json(extra) if extra else "")


def timed(func):
    """函数级耗时装饰器（同步 / 异步都兼容）"""
    if _is_coroutine(func):
        @wraps(func)
        async def awrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return await func(*args, **kwargs)
            finally:
                logger.debug("%s %.0fms", func.__name__, (time.perf_counter() - t0) * 1000)
        return awrapper

    @wraps(func)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            logger.debug("%s %.0fms", func.__name__, (time.perf_counter() - t0) * 1000)
    return wrapper


def _is_coroutine(fn) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


_audit_lock = threading.Lock()
AUDIT_MAX_BYTES = int(os.getenv("AUDIT_MAX_BYTES", str(5 * 1024 * 1024)))
_FIELD_CLIP = 500


def _rotate_audit_if_needed() -> None:
    """单个 jsonl 超过上限就滚动一次，保留一份 .1

    旧实现只 append 从不轮转：一个长期运行的进程会把审计文件写到无限大。
    简单滚一轮就够 —— 审计数据本来就不是长期存储。
    """
    try:
        if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size > AUDIT_MAX_BYTES:
            backup = AUDIT_FILE.with_suffix(".jsonl.1")
            if backup.exists():
                backup.unlink()
            AUDIT_FILE.rename(backup)
    except OSError as e:
        logger.warning("审计日志轮转失败: %s", e)


def audit(event: str, **payload: Any) -> None:
    """写一条审计日志（append-only jsonl）

    刻意不写密钥、不写完整原图二进制，只写可回溯的元信息。

    ★ 两处加固：
      ① 加锁。旧实现直接 open(...,'a') 写，并发线程（SSE 一开就有）
         会让两行交错写进同一个文件，jsonl 直接被写坏、后续解析全失败。
      ② 轮转 + 字段截断。旧实现永不轮转，且 payload 里塞什么就写什么，
         一个大字符串能把审计文件迅速撑爆。
    """
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
    }
    for k, v in payload.items():
        if isinstance(v, Path):
            v = str(v)
        elif isinstance(v, (bytes, bytearray)):
            v = f"<{len(v)} bytes>"
        elif not isinstance(v, (str, int, float, bool, type(None))):
            v = str(v)
        if isinstance(v, str) and len(v) > _FIELD_CLIP:
            v = v[:_FIELD_CLIP] + "…"
        record[k] = v

    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _audit_lock:
            _rotate_audit_if_needed()
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError as e:                       # 日志失败不该拖垮主流程
        logger.warning("审计日志写入失败: %s", e)


def recent_audit(limit: int = 20) -> list[dict]:
    """读最近 N 条审计记录（给排查 / 演示用）"""
    if not AUDIT_FILE.exists():
        return []
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def recent_audit_tail(limit: int = 20, max_bytes: int = 256 * 1024) -> list[dict]:
    """只读审计文件**尾部** max_bytes 字节，再取最后 N 条

    ★ 为什么不用 recent_audit：审计文件只增不减，全量 `read_text()` 在大站上
      会把整个文件读进内存 —— 联调时真的撞到过一次：审计接口 30 秒读超时，
      而且因为它是**阻塞 I/O 跑在事件循环里**，卡住的不只是这一个接口。
      seek 到尾部再读，是这类"只关心最近"的场景的标准做法。
    """
    if not AUDIT_FILE.exists():
        return []
    try:
        size = AUDIT_FILE.stat().st_size
        with AUDIT_FILE.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()          # 丢弃可能被截断的第一行
            raw = f.read()
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.decode("utf-8", errors="ignore").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
