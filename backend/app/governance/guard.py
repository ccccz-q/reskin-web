"""治理层 —— Agent 的预算、配额与安全边界

为什么单独一层
--------------
工程/harness 视角的一条硬经验：**凡是会让世界发生改变（花钱、写文件、发请求）的动作，
必须有一层统一的、可被审计的守门人**，而不是把 `if not allowed: return` 散在每个调用点。

本层负责三件事：

1. **花钱护栏**：`generate_image` 是本项目唯一会真实计费的动作。
   配额写在 DB 里而不是内存变量 —— 内存会在重启后清零，
   「重启一下额度就回来了」对一个要有说服力的工程来说是致命的。
2. **总开关**：`DISABLE_IMAGE_GENERATION=1` 时全局禁止出图。
   答辩/演示场景必备 —— 演示 PPT 时不想因为没有注意到某个按钮而烧钱。
3. **路径白名单**：确认参考图确实在存储目录内，堵住路径穿越。

一句话原则：**工具实现不做权限判断，判断统一在治理层**，
这样新增工具时不会因为「忘了加判断」而漏掉护栏。
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (                                             # noqa: E402
    IMAGE_STORAGE_DIR,
    MAX_GENERATIONS_PER_SESSION,
    MAX_UPLOAD_BYTES,
)
from infra.logging import audit, logger                           # noqa: E402
from infra.storage import ensure_within                           # noqa: E402
from services.context_store import (                                     # noqa: E402
    add_reservation,
    bump_counter,
    consume_reservation,
    get_counter,
    list_reservations,
    reset_counter,
)


class GovernanceError(Exception):
    """治理层拒绝 —— 调用方应转成 4xx 而不是 500

    它不是「程序出错了」，而是「这个请求被规则拦下了」。
    区分这两者对前端很重要：前者要报 bug，后者要给用户看的提示。
    """

    def __init__(self, message: str, *, code: str = "governance_denied", **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra


_MB = 1024 * 1024


# 总开关：演示 / 离线调试时把出图整个关掉，但预览、改参数照样能玩
DISABLE_IMAGE_GENERATION = str(
    os.getenv("DISABLE_IMAGE_GENERATION", "0")
).strip().lower() in ("1", "true", "yes", "on")


def _gen_key(thread_id: str) -> str:
    return f"gen:{thread_id}"


def remaining_quota(thread_id: str) -> dict:
    """查询剩余配额 —— 给 UI 显示「今天还能生成 N 张」

    ★ MAX_GENERATIONS_PER_SESSION <= 0 表示**不限额度**：
      remaining 返回 None（不是 0 —— 0 在 UI 上会被读成"用完了"），
      exhausted 恒为 False，limit 原样保留供诊断。
    """
    used = get_counter(_gen_key(thread_id))
    unlimited = MAX_GENERATIONS_PER_SESSION <= 0
    left = None if unlimited else max(0, MAX_GENERATIONS_PER_SESSION - used)
    return {
        "used": used,
        "limit": MAX_GENERATIONS_PER_SESSION,
        "remaining": left,
        "exhausted": False if unlimited else (left <= 0),
        "unlimited": unlimited,
        "global_switch_off": DISABLE_IMAGE_GENERATION,
    }


def check_generation_allowed(thread_id: str, *, reference_image: str | None = None) -> None:
    """只读检查 —— 回答「现在能不能出图」，不改动任何计数

    检查顺序刻意为：先看全局开关（最便宜），再看配额，最后校验文件。
    ⚠ 这只做「看」，真正出图必须走 `reserve_generation()`，
    否则「检查」与「真正写」之间会被并发请求穿过（TOCTOU）。
    """
    if DISABLE_IMAGE_GENERATION:
        raise GovernanceError(
            "当前已配置为禁止出图（DISABLE_IMAGE_GENERATION=1），"
            "所有预览类功能仍可正常使用。",
            code="generation_disabled",
        )

    quota = remaining_quota(thread_id)
    if quota["exhausted"]:
        raise GovernanceError(
            f"本会话已用完 {quota['limit']} 张生成额度"
            f"（治理项 MAX_GENERATIONS_PER_SESSION）。"
            f"想继续演示请在 .env 中调高该值，或调用 /api/chat/reset-quota 重置。",
            code="quota_exhausted",
            **quota,
        )

    if reference_image:
        assert_safe_image(reference_image)


# ══════════════ 预扣 / 释放：取代原先「先成功后才计数」的记账 ══════════════
#
# 为什么必须改成预扣（对照审查发现 P0-2）
# --------------------------------------
# 旧实现：check（只看不写） → 出图 → 成功才 +1，失败则 refund -1。
# 这条看似合理的链路有一个致命的记账错误：失败时退的那 1 张，
# 退的其实是**上一次成功生成**的账。实测序列：
#
#     初始 used=0 → 成功1次 used=1 → 失败1次(退还) used=0  ← 应为 1
#
# 于是只要「成功 + 失败」交替，used 永远被打回 0，
# MAX_GENERATIONS_PER_SESSION 这个成本护栏彻底失效。
#
# 新模型：**检查与占位合并为一把锁内的原子操作**，出图前先把额度占下来，
# 成功后什么都不做（已经占了），失败时凭票据冲正。
# 「没有票据就不许退」是这里的关键约束。

_reserve_lock = threading.Lock()

# 预扣票据存 DB 而不是进程内 set。原因见 context_store 里 reservations 段的注释：
#   内存票据会 ① 无界增长 ② 进程崩溃/重启后「已扣未结算」的额度永久泄漏。
RESERVATION_TTL_SEC = int(os.getenv("RESERVATION_TTL_SEC", "1800"))


def reserve_generation(thread_id: str, *, reference_image: str | None = None) -> str:
    """出图前原子占位，成功返回票据 token，失败抛 GovernanceError

    ★ 检查与占位在同一把锁内完成，中间不允许被别的请求插进来。
    """
    if DISABLE_IMAGE_GENERATION:
        raise GovernanceError(
            "当前已配置为禁止出图（DISABLE_IMAGE_GENERATION=1），"
            "所有预览类功能仍可正常使用。",
            code="generation_disabled",
        )
    if reference_image:
        assert_safe_image(reference_image)

    key = _gen_key(thread_id)
    with _reserve_lock:
        used = get_counter(key)
        # 不限额度（<=0）时只记数、不拦截
        if MAX_GENERATIONS_PER_SESSION > 0 and used >= MAX_GENERATIONS_PER_SESSION:
            raise GovernanceError(
                f"本会话已用完 {MAX_GENERATIONS_PER_SESSION} 张生成额度"
                f"（治理项 MAX_GENERATIONS_PER_SESSION）。",
                code="quota_exhausted",
                **remaining_quota(thread_id),
            )
        bump_counter(key, 1)
        token = uuid.uuid4().hex[:16]
        try:
            add_reservation(token, thread_id)
        except Exception:
            # 票据写不进去就把刚扣的额度还回去 —— 否则又是一次静默泄漏
            bump_counter(key, -1)
            raise
    audit("quota_reserved", thread_id=thread_id, used=used + 1)
    return token


def release_generation(thread_id: str, token: str | None, *, reason: str = "") -> dict:
    """出图失败时凭票据退还额度

    ★ 必须凭票据，且票据只能兑现一次。
      没有票据就退 = 允许任何人用「失败」把别人的账退掉。
      兑现走 `consume_reservation`（DELETE 的 rowcount 判定），是原子的。
    """
    if not token:
        logger.warning("退还额度被拒：没有票据（thread=%s）", thread_id)
        return remaining_quota(thread_id)

    if not consume_reservation(token):
        logger.warning("退还额度被拒：票据无效或已兑现（thread=%s）", thread_id)
        return remaining_quota(thread_id)

    with _reserve_lock:
        key = _gen_key(thread_id)
        if get_counter(key) > 0:
            bump_counter(key, -1)
    refunded = remaining_quota(thread_id)
    audit("quota_released", thread_id=thread_id, reason=reason[:200], used=refunded["used"])
    return refunded


def reconcile_reservations(max_age_sec: int | None = None) -> int:
    """启动对账：回收「预扣了但永远不会有结果」的残票，把额度还回去

    ★ 为什么必须有（审查发现 P1-4）
    ------------------------------
    进程在「已 reserve、还没 settle/release」之间被 kill（Ctrl+C、崩溃、重启），
    那张票就变成了孤儿：计数已经 +1，但没有任何路径会把它减回去，
    只能靠 /reset-quota 手动清。TTL 回收把这一步自动化。

    TTL 取 30 分钟 —— 一次 Agent 运行最慢也就几分钟，超过这个年龄的必然是残票。
    返回回收的条数。
    """
    ttl = RESERVATION_TTL_SEC if max_age_sec is None else max_age_sec
    stale = list_reservations(older_than_sec=ttl)
    recovered = 0
    for r in stale:
        if not consume_reservation(r["token"]):
            continue                      # 被别的路径抢先兑现了
        key = _gen_key(r["thread_id"])
        with _reserve_lock:
            if get_counter(key) > 0:
                bump_counter(key, -1)
                recovered += 1
        audit("quota_reconciled", thread_id=r["thread_id"],
              token=r["token"], ts=r["ts"])
    if recovered:
        logger.warning("额度对账：回收了 %d 张超时未结算的预扣", recovered)
    return recovered


def assert_safe_image(reference_image: str) -> Path:
    """确认参考图路径合法且在存储目录内"""
    p = Path(reference_image)
    if not p.exists():
        raise GovernanceError(f"参考图不存在：{p.name}", code="reference_missing")
    if not p.is_file():
        raise GovernanceError(f"参考图不是文件：{p.name}", code="reference_not_file")
    try:
        return ensure_within(p.resolve())
    except ValueError as e:
        raise GovernanceError(str(e), code="path_escape") from e


def check_upload_size(nbytes: int) -> None:
    """上传体积判定 —— 唯一的判定入口

    ★ 以前这份逻辑被原样复制进了 `services/upload.py`，而这里变成死代码。
    两份实现对同一份规则的duplicate——改一处必漂。
    现在 upload.py 必须调用本函数，由 services 层负责把 GovernanceError
    翻译成 HTTP 状态码（映射留在那里，判定留在这里）。
    """
    if nbytes <= 0:
        raise GovernanceError("上传文件为空", code="upload_empty")
    if nbytes > MAX_UPLOAD_BYTES:
        mb = nbytes / _MB
        limit = MAX_UPLOAD_BYTES / _MB
        raise GovernanceError(
            f"上传文件 {mb:.1f}MB 超过上限 {limit:.0f}MB，请压缩后再试",
            code="upload_too_large",
        )


class _GenerationGuard:
    """`generation_guard` 的产物：显式提交，否则自动退还"""

    __slots__ = ("thread_id", "token", "reference_image", "settled", "quota")

    def __init__(self, thread_id: str, token: str, reference_image: str | None):
        self.thread_id = thread_id
        self.token = token
        self.reference_image = reference_image or ""
        self.settled = False
        self.quota: dict | None = None

    def commit(self, *, size: str = "") -> dict:
        """出图成功 —— 预扣正式生效。**只能调一次**，重复调用直接抛。"""
        if self.settled:
            raise RuntimeError("generation_guard.commit() 只能调用一次")
        self.settled = True
        self.quota = settle_generation(self.thread_id, self.token, size=size,
                                      reference=self.reference_image)
        return self.quota

    def release(self, *, reason: str = "") -> dict | None:
        """退还（**幂等**）：已commit 过就是 no-op，所以可以放在 finally 里无条件调"""
        if self.settled:
            return None
        self.settled = True
        return release_generation(self.thread_id, self.token, reason=reason)


def new_generation_guard(thread_id: str, token: str, *,
                         reference_image: str | None = None) -> _GenerationGuard:
    """给「已经自己 reserve 过了」的调用点用

    为什么需要这个：HTTP 层的 reserve 要把 `GovernanceError` 翻译成
    429/403（前端要区分"额度用完"和"被禁用"），所以那一步必须显式写。
    但**归还**不该跟着写成 except 分支 —— 那正是漏退的来源。
    于是拆成两半：reserve 显式（为了映射错误码），归还交给 guard（幂等）。
    """
    return _GenerationGuard(thread_id, token, reference_image)


@contextmanager
def generation_guard(thread_id: str, *, reference_image: str | None = None):
    """**出图额度护栏**：预扣 → 成功 commit / 任何其它结局自动退还

    ★ 为什么要有这个（2026-10-06 评审自查发现的真实漏洞）：
      原来的写法是每个调用点自己「预扣 → try 出图 → except 特定异常才 release」。
      问题在于「except 只捕了某一种异常」：image.py 只捕 `ConfigMissing`，
      而 `generate_image_with_reference` 内部还会 `raise ValueError`
      （尺寸非法、上游返回体缺字段等）。这类异常穿过 except 直达 FastAPI，
      **票据就悬在那里** —— 用户没拿到图，额度却被扣着，只能等 TTL（30 分钟）
      或下次启动对账才回收。同一类洞还有一处：任务被判超时时只 cancel 不退还。

      凡是「先占用额度、再做一件可能失败的事」的地方，都应该用它：
      它把「归还」从**调用点的一个分支**变成**语言层面的保证** ——
      异常、提前 return、甚至 `raise HTTPException` 都自动退还，
      新增异常类型时不需要记得补 except。

    用法：
        with generation_guard(tid, reference_image=path) as g:
            result = do_expensive_thing()
            if not result.get("success"):
                raise SomeError(result["error"])      # 自动退还
            g.commit(size=result.get("size", ""))    # 只有成功才核销
    """
    token = reserve_generation(thread_id, reference_image=reference_image)
    guard = _GenerationGuard(thread_id, token, reference_image)
    try:
        yield guard
    except BaseException as e:                       # noqa: BLE001
        guard.release(reason=f"异常退出：{type(e).__name__}")
        raise
    if not guard.settled:
        # 没 commit 就走完了流程（= 没拿到图）→ 退还
        guard.release(reason="未提交即退出")


def settle_generation(thread_id: str, token: str, *, size: str, reference: str = "") -> dict:
    """出图成功 —— 预扣的部分正式生效

    不再重复 +1（额度在 `reserve_generation` 时已经占掉了）。

    ★ 必须在这里把票据作废（审查发现 P1-3）
    --------------------------------------
    旧实现只在 release 时 discard 票据，settle 不 discard。后果有两层：
      ⓐ 同一张票据「先成功后冲正」也能过 → `reserve → settle → release`
         可以把已经确认的额度退回去，账目不变式（成功不可退）被破坏；
      ⓑ 每次成功生成都会在 `_tokens` 里留一条永不清理的条目 → 无界增长。
    票据语义必须是「一次性兑现」，两个出口都要销毁它。
    """
    # 一次性兑现：不管成功还是失败，票据都只能被吃掉一次
    consume_reservation(token)

    q = remaining_quota(thread_id)
    audit("quota_consumed", thread_id=thread_id, used=q["used"], size=size,
          reference=os.path.basename(reference))
    if MAX_GENERATIONS_PER_SESSION > 0:
        logger.info("会话 %s 已生成 %d/%d 张", thread_id, q["used"],
                    MAX_GENERATIONS_PER_SESSION)
    else:
        logger.info("会话 %s 已生成 %d 张（不限额度）", thread_id, q["used"])
    return q


def reset_quota(thread_id: str) -> dict:
    reset_counter(_gen_key(thread_id))
    audit("quota_reset", thread_id=thread_id)
    return remaining_quota(thread_id)


def policy_snapshot(thread_id: str = "default") -> dict:
    """给 /api/health 看的治理策略快照"""
    return {
        "max_generations_per_session": MAX_GENERATIONS_PER_SESSION,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "storage_dir": str(IMAGE_STORAGE_DIR),
        "generation_disabled": DISABLE_IMAGE_GENERATION,
        "quota": remaining_quota(thread_id),
        "reservation_ttl_sec": RESERVATION_TTL_SEC,
    }


__all__ = [
    "GovernanceError",
    "DISABLE_IMAGE_GENERATION",
    "assert_safe_image",
    "check_generation_allowed",
    "check_upload_size",
    "policy_snapshot",
    "reconcile_reservations",
    "release_generation",
    "remaining_quota",
    "reserve_generation",
    "reset_quota",
    "settle_generation",
]
