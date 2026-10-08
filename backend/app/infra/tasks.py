"""通用后台任务中心 —— 长任务的统一托管 + 事件流 + 协作式取消

════════════ 为什么会有这个文件（2026-10-03 线上事故）════════════

把「换颜」发到 WorkBuddy sites 之后，用户反馈：上传图片后一直卡在
`extract_card` 这一步，然后弹错。

实测证据：

    POST /api/helper/recommend  → HTTP 504   耗时 60.099544s
    POST /api/chat/stream       → HTTP 200   耗时 41.46s（这还只是不带工具的一次对话）

**60.099 秒** —— 这个数字精确到小数后三位都不是巧合：云端前面有反向代理，
它给每个 HTTP 请求设了 **60 秒硬超时**，到点直接掐断返回 504。

而这条链路的真实耗时是：

    一次普通对话 41s + extract_card 的视觉模型 30~60s + 出图 43s  →  远超 60s

更要命的是**这个问题在本地开发中根本不会暴露**：本机直连 8000 端口，
没有反代，没有 60 秒限制，跑 7 分钟也没人管你。所以它是典型的
「只在发布后才炸」的一类 bug —— 不实测线上永远发现不了。

════════════ 唯一的正确解法 ════════════

**不是"让它跑快一点"**。那意味着砍掉视觉模型、降低质量 —— 用户明确不接受。

正确做法是把「请求」和「耗时」**彻底脱钩**：

    提交任务 → 毫秒级返回 task_id → 前端轮询任务的进度与结果

这样每一个 HTTP 请求都是瞬时完成的，60 秒限制从**原理上**失效，
而且任务爱跑多久跑多久（本项目提炼风格本来就要 7 分钟）。

模板工坊的后台提炼（routers/forge.py 的 `_TASKS`）早就这么做了，
这个文件是把那套经验抽出来做成公共设施，供对话链路复用。

════════════ 八条护栏（每一条都是被真实坑出来的）════════════

1. **并发上限**：同时跑的任务太多会拖垮这台小机器，也会让每个人的任务
   都变慢。拿不到位置就明确 429，而不是把线程堆上去让所有人一起卡死。

2. **每会话同类型唯一**：防止用户手抖点两下就提交两个并发出图任务
   （每次都是真金白银的生图调用）。

3. **总时限**：任务最多跑 `TASK_TIMEOUT_SEC` 秒，超时由清扫线程置为失败。
   没有这道闸，一个卡在 LLM 里的任务会永远占着并发位。

4. **TTL 清理**：完成的任务也要消失，否则内存只增不减（进程要跑几个月）。

5. **会话归属**：任务只能被发起它的会话查询，别人的 task_id 一律 404
   （用 404 而不是 403 —— 403 会泄露"这个 ID 存在"）。

6. **事件上限**：token 流式文本不逐条存事件（那会指数膨胀），
   改成累积到 `text` 字段，前端自己算增量 —— 内存可控且一样有流式观感。

7. **异常兜底**：线程内任何异常都不能静默 —— 一律捕获、写日志、置 failed、
   给出可读错误。后台线程里的异常如果没人接，用户看到的就是永久转圈。

8. **协作式取消**：给任务函数一个 `should_abort()` 探针，由它自己在安全点
   收尾。Python 没有安全的强制杀线程手段，粗暴 kill 只会留下脏数据。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from typing import Any, Callable

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (                                             # noqa: E402
    TASK_MAX_EVENTS,
    TASK_MAX_RUNNING,
    TASK_TIMEOUT_SEC,
    TASK_TTL_SEC,
)
from infra.logging import audit, logger                          # noqa: E402

# ── 任务状态（终态三个：done / failed / cancelled）──────────────────
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
_TERMINAL = (DONE, FAILED, CANCELLED)

# emit 的保留事件名：这几个由任务中心自己解释，不进 events 列表
_SYSTEM_EVENTS = frozenset({"token", "phase", "done", "error", "close"})


class TaskBusy(Exception):
    """并发已满 / 同一会话已有同类任务在跑 —— 由路由层翻译成友好提示"""


class Task:
    """一个后台任务的可变状态。所有字段只在 `_LOCK` 内读写。"""

    __slots__ = ("task_id", "kind", "name", "owner", "status", "phase",
                 "events", "text", "result", "error", "cancel",
                 "created_at", "started_at", "finished_at", "thread", "meta")

    def __init__(self, task_id: str, kind: str, name: str, owner: str, meta: dict):
        self.task_id = task_id
        self.kind = kind
        self.name = name
        self.owner = owner
        self.status = QUEUED
        self.phase = "排队中"
        self.events: list[tuple[str, Any]] = []
        self.text = ""                 # 流式文本累积（不在 events 里重复存）
        self.result: Any = None
        self.error: str | None = None
        self.cancel = threading.Event()
        self.created_at = time.time()
        self.started_at = 0.0
        self.finished_at: float | None = None
        self.thread: threading.Thread | None = None
        self.meta = meta or {}

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        if not self.started_at:
            return 0.0
        return round(end - self.started_at, 1)


# ══════════════════ 注册表 ══════════════════

_TASKS: dict[str, Task] = {}
_LOCK = threading.RLock()
_SWEEPER_STARTED = False
_SWEEP_INTERVAL_SEC = 20
# ★ 2026-10-06：清扫线程的停止信号（见 _sweeper / shutdown_sweeper）
_SWEEP_STOP = threading.Event()


def _emit(task: Task, event: str, data: Any) -> None:
    """把任务函数发出的事件落到任务对象里

    ★ token 单独走 `text` 累积：一次对话可能产生几百上千个 token 事件，
      逐个塞进列表既撑内存又没意义 —— 前端要的是「截止现在的全文」，
      自己按长度算增量即可（见 snapshot 里返回的 text）。
    """
    if event in _SYSTEM_EVENTS:
        if event == "token":
            piece = (data or {}).get("text") if isinstance(data, dict) else None
            if piece:
                task.text += str(piece)
            return
        if event == "phase":
            task.phase = str((data or {}).get("text") or data or "")[:80]
            return
        if event == "close":
            return
        with _LOCK:
            if event == "done":
                task.result = data
                task.status = DONE
                task.finished_at = time.time()
            elif event == "error":
                task.error = str((data or {}).get("error") or data or "任务出错")
                task.status = FAILED
                task.finished_at = time.time()
        return

    with _LOCK:
        if len(task.events) >= TASK_MAX_EVENTS:
            # 事件型洪泛保护：超上限后记一条，不再无限增长
            logger.warning("任务 %s 事件数超过上限 %d，后续事件不再记录",
                           task.task_id, TASK_MAX_EVENTS)
            task.events.append(("task_truncated", {
                "error": f"事件过多（>{TASK_MAX_EVENTS}），已停止记录中间步骤。",
            }))
            return
        task.events.append((event, data if data is not None else {}))


def _sweeper() -> None:
    """清扫：① 总时限超时的任务 ② TTL 到期的已完成任务

    ★ 2026-10-06：改成可退出的。此前是 `while True: time.sleep(...)`，
      没有停止路径 —— 应用关闭 / 测试反复起进程时线程就一直在那儿醒着，
      既是资源泄漏，也让"这个进程到底还剩什么"变得不可知。
      现在用 `_SWEEP_STOP` 事件退出，`shutdown_sweeper()` 供 lifespan 调用。

    ★ 这里**故意不退还额度**：清扫器手里没有票据（预扣发生在 tools/registry 的
      护栏里，它属于正在跑的那个工作线程）。它只做一件事 —— `t.cancel.set()`，
      工作线程在下一个探针点退栈时会经过护栏的 `finally`，归还自动发生。
      进程被硬杀时走另一条兜底：启动时 `reconcile_reservations()` 按 TTL 回收。
      **职责分散在两处、但都有落点**，比让清扫器去猜"该退谁的账"更可靠。
    """
    while not _SWEEP_STOP.is_set():
        # 用 wait 而不是 sleep：既能周期清扫，也能被立刻唤醒退出
        if _SWEEP_STOP.wait(_SWEEP_INTERVAL_SEC):
            return
        now = time.time()
        try:
            with _LOCK:
                victims: list[Task] = []
                expired_ids: list[str] = []
                for tid, t in list(_TASKS.items()):
                    if t.status in _TERMINAL:
                        if now - (t.finished_at or now) > TASK_TTL_SEC:
                            expired_ids.append(tid)
                    elif t.started_at and now - t.started_at > TASK_TIMEOUT_SEC:
                        victims.append(t)
                for tid in expired_ids:
                    _TASKS.pop(tid, None)
                for t in victims:
                    t.cancel.set()                 # 先礼貌地请它停
                    t.status = FAILED
                    t.error = f"任务超过总时限（{TASK_TIMEOUT_SEC // 60} 分钟）已自动停止"
                    t.finished_at = now
            if expired_ids:
                logger.info("已清理 %d 个过期任务", len(expired_ids))
            for t in victims:
                logger.error("任务 %s（%s）超时未结束，已置失败", t.task_id, t.name)
                audit("task_timeout", task_id=t.task_id, task_name=t.name,
                      elapsed=int(TASK_TIMEOUT_SEC))
        except Exception:                          # 清扫线程自己绝不能死
            logger.exception("任务清扫线程异常")


def _ensure_sweeper() -> None:
    global _SWEEPER_STARTED
    if _SWEEPER_STARTED:
        return
    with _LOCK:
        if _SWEEPER_STARTED:
            return
        _SWEEP_STOP.clear()
        threading.Thread(target=_sweeper, daemon=True, name="task-sweeper").start()
        _SWEEPER_STARTED = True


def shutdown_sweeper(timeout: float = 2.0) -> bool:
    """停掉清扫线程（应用关闭 / 测试收尾用）

    返回是否真的停掉了。幂等 —— 没启动过也返回 True。
    """
    global _SWEEPER_STARTED
    _SWEEP_STOP.set()
    with _LOCK:
        started = _SWEEPER_STARTED
        _SWEEPER_STARTED = False
    if not started:
        return True
    # daemon=True 的线程不会挡住进程退出，所以这里只给一个短宽限期：
    # 目标是"让它别再醒着"，不是"保证它一定停"（sleep 已被事件替换，通常立刻返回）
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _sweeper_alive():
            return True
        time.sleep(0.05)
    logger.warning("清扫线程未在 %.1fs 内退出（不影响进程退出，它是daemon）", timeout)
    return False


def _sweeper_alive() -> bool:
    return any(t.name == "task-sweeper" and t.is_alive()
               for t in threading.enumerate())


# ══════════════════ 对外 API ══════════════════

def submit(*, name: str, kind: str, owner: str,
           fn: Callable[[Callable[[str, Any], None], Callable[[], bool]], Any],
           meta: dict | None = None) -> str:
    """登记一个后台任务并立刻启动线程，返回 task_id（本函数不阻塞）

    fn 的签名：`fn(emit, should_abort) -> result`
      - `emit(event, data)` 推过程事件，可随时调用（线程安全）
      - `should_abort()` 在自己的安全点问一下，返回 True 就该收尾了
      - 返回值会被记到 result 里；fn 内部抛异常由本模块捕获并置 failed
    """
    _ensure_sweeper()

    with _LOCK:
        running = sum(1 for t in _TASKS.values() if t.status in (QUEUED, RUNNING))
        if running >= TASK_MAX_RUNNING:
            raise TaskBusy(f"同时进行的服务已达上限（{TASK_MAX_RUNNING} 个），请稍后再试")
        dup = next((t for t in _TASKS.values()
                    if t.owner == owner and t.kind == kind
                    and t.status in (QUEUED, RUNNING)), None)
        if dup is not None:
            raise TaskBusy("你有一个同类任务正在进行中，请等它结束后再发起")

        task_id = f"{kind}_" + uuid.uuid4().hex[:12]
        task = Task(task_id=task_id, kind=kind, name=name, owner=owner,
                    meta=meta or {})
        task.started_at = time.time()
        task.status = RUNNING
        _TASKS[task_id] = task

    def emit(event: str, data: Any = None) -> None:
        try:
            _emit(task, event, data)
        except Exception:                          # emit 出问题不能拖垮任务本身
            logger.exception("任务事件写入失败 %s/%s", task_id, event)

    def runner() -> None:
        try:
            emit("phase", {"text": task.name})
            result = fn(emit, task.cancel.is_set)
            with _LOCK:
                if task.status not in _TERMINAL:
                    task.result = result
                    task.status = CANCELLED if task.cancel.is_set() else DONE
                    task.finished_at = time.time()
                    if task.status == CANCELLED:
                        task.error = "已按你的要求中止"
            audit("task_done", task_id=task_id, task_name=name, kind=kind,
                  status=task.status)
        except Exception as e:                     # ★ 后台线程里的异常必须有归宿
            logger.exception("后台任务失败 %s（%s）", task_id, name)
            with _LOCK:
                task.status = FAILED
                task.error = f"{type(e).__name__}: {e}"
                task.finished_at = time.time()
            audit("task_failed", task_id=task_id, task_name=name, kind=kind,
                  error=f"{type(e).__name__}: {str(e)[:200]}")

    th = threading.Thread(target=runner, daemon=True, name=f"task-{task_id}")
    task.thread = th
    try:
        th.start()
    except Exception:                              # 线程起不来：立刻摘掉，别留僵尸任务
        with _LOCK:
            _TASKS.pop(task_id, None)
        logger.exception("任务线程启动失败 %s", task_id)
        raise TaskBusy("服务暂时无法启动新任务，请稍后再试") from None
    return task_id


def _get(task_id: str, owner: str) -> Task:
    with _LOCK:
        t = _TASKS.get(task_id)
    if t is None or t.owner != owner:
        # 不存在 / 不是本人的 → 统一 404，避免泄露「这个 ID 存在」
        raise KeyError(task_id)
    return t


def snapshot(task_id: str, owner: str, cursor: int = 0) -> dict:
    """取任务快照 + 从 cursor 之后的增量事件

    返回里带 `text`（累积全文）和 `text_len`，前端按已渲染长度切片取增量，
    既省内存又保留一边打字一边出现的观感。
    """
    t = _get(task_id, owner)
    with _LOCK:
        events = [{"i": i, "event": ev, "data": d}
                  for i, (ev, d) in enumerate(t.events) if i >= cursor]
        return {
            "task_id": t.task_id,
            "kind": t.kind,
            "name": t.name,
            "status": t.status,
            "phase": t.phase,
            "elapsed_sec": t.elapsed,
            "cancelling": t.cancel.is_set() and t.status not in _TERMINAL,
            "result": t.result,
            "error": t.error,
            "events": events,
            "cursor": len(t.events),
            "text": t.text,
            "text_len": len(t.text),
        }


def cancel(task_id: str, owner: str) -> bool:
    """请求中止（协作式）：只是置标志，由任务函数在安全点自己收尾"""
    t = _get(task_id, owner)
    if t.status in _TERMINAL:
        return False
    t.cancel.set()
    audit("task_cancel", task_id=task_id, task_name=t.name)
    logger.info("任务收到中止请求 %s", task_id)
    return True


def active(owner: str) -> list[dict]:
    """列出我还没结束的任务 —— 刷新页面后用来找回跑着的任务"""
    with _LOCK:
        mine = [t for t in _TASKS.values()
                if t.owner == owner and t.status in (QUEUED, RUNNING)]
        return [{
            "task_id": t.task_id, "kind": t.kind, "name": t.name,
            "status": t.status, "phase": t.phase, "elapsed_sec": t.elapsed,
        } for t in sorted(mine, key=lambda x: x.created_at)]


def stats() -> dict:
    """给 /api/health 看的计数（不含任何任务内容）"""
    with _LOCK:
        by_status: dict[str, int] = {}
        for t in _TASKS.values():
            by_status[t.status] = by_status.get(t.status, 0) + 1
        running = sum(v for k, v in by_status.items() if k in (QUEUED, RUNNING))
    return {"total": len(_TASKS), "by_status": by_status,
            "running": running, "max_running": TASK_MAX_RUNNING,
            "timeout_sec": TASK_TIMEOUT_SEC, "ttl_sec": TASK_TTL_SEC}


def reset_for_tests() -> None:
    """测试专用：清空注册表（生产代码绝不调用）"""
    with _LOCK:
        for t in _TASKS.values():
            t.cancel.set()
        _TASKS.clear()
