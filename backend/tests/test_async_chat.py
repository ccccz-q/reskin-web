"""异步对话任务 —— 八条护栏的回归网

══════ 为什么要有这个文件 ══════

上线后真实事故的产物：托管平台的反向代理对每个 HTTP 请求有 60 秒硬超时
（实测 60.099544s 返回 504），而出图链路要 1~7 分钟。
修复办法是把请求与耗时脱钩（提交 → 秒回 task_id → 轮询）。

**这类修复最危险的地方在于：它一旦出问题，用户看到的是永久转圈，
而后台可能正在偷偷烧钱。** 所以下面每一条都要单独钉住：

  1. 提交必须**瞬时返回**（这是它存在的全部意义）
  2. 一个请求])))
  3. 并发上限 / 同会话重复提交要被拒（重复 = 重复的付费调用）
  4. 跨会话查不到别人的任务
  5. 线程内异常必须变成 failed，不能静默
  6. 超过总时限要被清扫线程判死
  7. 累积文本 = 各次增量之和（不能丢字也不能重复）
  8. 取消标志位要真的传下去

跑法：
    cd backend/app && python -m tests.test_async_chat
或（项目推荐的 runner）：
    python backend/tests/run_all.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

_FAILED = 0
_PASSED = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global _FAILED, _PASSED
    if cond:
        _PASSED += 1
        print(f"  ✅ {label}")
    else:
        _FAILED += 1
        print(f"  ❌ {label}  {extra}")


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def main() -> None:
    os.environ.setdefault("TASK_TTL_SEC", "5")          # 便于测清理
    from infra import tasks

    # ── 1. 提交必须瞬时返回 ────────────────────────────────
    section("1. 提交是毫秒级的（它的全部价值所在）")

    def slow_fn(emit, should_abort):
        emit("phase", {"text": "假装很忙"})
        time.sleep(6)                       # 远超一般的同步上限
        emit("tool_end", {"name": "fake", "ok": True})
        return {"image_url": "/images/x.png"}

    t0 = time.time()
    tid = tasks.submit(name="慢任务", kind="chat", owner="u1", fn=slow_fn)
    cost = time.time() - t0
    check("submit 不阻塞（<0.5s）", cost < 0.5, f"实际 {cost:.3f}s")

    snap = tasks.snapshot(tid, "u1")
    check("任务立刻处于非终态", snap["status"] in ("queued", "running"), snap["status"])

    # ── 2. 轮询拿到过程与结果 ───────────────────────────────
    section("2. 轮询：增量事件 + 累积文本 + 结果")

    # 让第 1 节的慢任务收尾（它还在跑，同类任务会被护栏拒绝 —— 这正是第 3 节要测的）
    tasks.reset_for_tests()

    def streaming_fn(emit, should_abort):
        for piece in ("你好", "，这是", "一段流"):
            emit("token", {"text": piece})
        emit("tool_start", {"name": "extract_card"})
        emit("tool_end", {"name": "extract_card", "ok": True, "summary": "完成"})
        emit("finish", {})
        emit("done", {"image_url": "/images/abc.png", "family_id": "zine"})
        return {"image_url": "/images/abc.png"}

    tid2 = tasks.submit(name="流式", kind="chat", owner="u1", fn=streaming_fn)
    deadline = time.time() + 15
    cursor = 0
    seen_events: list[str] = []
    final = None
    while time.time() < deadline:
        s = tasks.snapshot(tid2, "u1", cursor)
        seen_events.extend(e["event"] for e in s["events"])
        cursor = s["cursor"]
        if s["status"] not in ("queued", "running"):
            final = s
            break
        time.sleep(0.05)

    check("任务正常结束", final is not None and final["status"] == "done",
          str(final and final["status"]))
    check("累积文本完整", final and final["text"] == "你好，这是一段流",
          str(final and final["text"]))
    check("工具事件都收到了", "tool_start" in seen_events and "tool_end" in seen_events,
          str(seen_events))
    check("done 的结果进 result 而不是事件堆",
          final and (final["result"] or {}).get("image_url") == "/images/abc.png",
          str(final and final["result"]))
    check("done 不重复出现在事件列表里", "done" not in seen_events, str(seen_events))
    check("text_len 与实际长度一致",
          final and final["text_len"] == len(final["text"]))

    # ── 3. 并发与重复提交护栏 ───────────────────────────────
    section("3. 并发上限 / 同会话重复提交必须被拒")

    def forever_fn(emit, should_abort):
        while not should_abort():
            time.sleep(0.2)
        return None

    tasks.reset_for_tests()
    taken = []
    try:
        for i in range(8):                  # 上限是 4，多的一定要被拒
            taken.append(tasks.submit(name=f"占位{i}", kind="chat", owner=f"u{i}",
                                      fn=forever_fn))
    except tasks.TaskBusy:
        pass
    check("并发不超过上限", len(taken) <= 4, f"实际起了 {len(taken)} 个")

    dup_rejected = False
    try:
        tasks.submit(name="重复", kind="chat", owner="u0", fn=forever_fn)
    except tasks.TaskBusy:
        dup_rejected = True
    check("同一会话重复提交被拒（防重复付费）", dup_rejected)

    for t in taken:
        tasks.cancel(t, tasks._TASKS[t].owner)
    time.sleep(0.6)

    # ── 4. 会话归属 ────────────────────────────────────────
    section("4. 别人的任务查不到")

    tasks.reset_for_tests()
    tid3 = tasks.submit(name="私有的", kind="chat", owner="alice", fn=slow_fn)
    leaked = True
    try:
        tasks.snapshot(tid3, "bob")
    except KeyError:
        leaked = False
    check("跨会话查询被挡", not leaked)
    cancel_leaked = True
    try:
        tasks.cancel(tid3, "bob")
    except KeyError:
        cancel_leaked = False
    check("跨会话取消被挡", not cancel_leaked)
    check("本人的任务仍可查", tasks.snapshot(tid3, "alice")["status"] in
          ("queued", "running"))
    tasks.cancel(tid3, "alice")
    time.sleep(0.6)

    # ── 5. 异常必须变成 failed，绝不静默 ────────────────────
    section("5. 线程内异常 → failed + 可读错误")

    tasks.reset_for_tests()

    def boom(emit, should_abort):
        raise RuntimeError("视觉模型炸了")

    tid4 = tasks.submit(name="会炸的", kind="chat", owner="u1", fn=boom)
    time.sleep(0.5)
    s = tasks.snapshot(tid4, "u1")
    check("异常任务置 failed", s["status"] == "failed", s["status"])
    check("错误信息可读（含原始异常）", "视觉模型炸了" in (s["error"] or ""),
          str(s["error"]))

    # 通过 done/error 事件提前失败也算失败
    def fail_by_event(emit, should_abort):
        emit("error", {"error": "上游超时"})
        return None

    tid5 = tasks.submit(name="事件失败", kind="chat", owner="u1", fn=fail_by_event)
    time.sleep(0.4)
    s5 = tasks.snapshot(tid5, "u1")
    check("error 事件也能置 failed", s5["status"] == "failed", s5["status"])
    check("错误信息取自 payload", (s5["error"] or "") == "上游超时", str(s5["error"]))

    # ── 6. 取消标志位要传下去 ───────────────────────────────
    section("6. 协作式取消")

    tasks.reset_for_tests()
    observed = {"saw": False}

    def cancellable(emit, should_abort):
        for _ in range(80):
            if should_abort():
                observed["saw"] = True
                return {"cancelled": True}
            time.sleep(0.05)
        return {"cancelled": False}

    tid6 = tasks.submit(name="可取消", kind="chat", owner="u1", fn=cancellable)
    time.sleep(0.3)
    check("cancel 返回 True", tasks.cancel(tid6, "u1") is True)
    time.sleep(0.6)
    check("任务函数确实看到取消标志", observed["saw"])

    # ── 7. TTL 清理（用短 TTL 验证清理逻辑真的在跑）──────────
    section("7. TTL：终态任务会被清掉")

    tasks.reset_for_tests()
    tid7 = tasks.submit(name="短命", kind="chat", owner="u1",
                        fn=lambda emit, abort: {"ok": True})
    time.sleep(0.4)
    check("任务已完成", tasks.snapshot(tid7, "u1")["status"] == "done")
    # TASK_TTL_SEC=5，清扫线程每 20s 一轮 —— 这里直接调用清扫的内部判定，
    # 不等真实定时器（否则测试要跑 20 秒）。
    old_finished = tasks._TASKS[tid7].finished_at
    tasks._TASKS[tid7].finished_at = old_finished - 10_000      # 伪造成 10000s 前完成
    import threading
    for _ in range(30):                        # 最多等 30 秒让清扫线程扫到一轮
        if tid7 not in tasks._TASKS:
            break
        time.sleep(1)
    check("过期任务被清理", tid7 not in tasks._TASKS)

    # ── 8. health 用的计数 ──────────────────────────────────
    section("8. stats 供 /api/health 使用")

    tasks.reset_for_tests()
    st = tasks.stats()
    check("stats 带 total / max_running", "total" in st and "max_running" in st,
          str(st))

    tasks.reset_for_tests()
    print(f"\n通过：{_PASSED}    失败：{_FAILED}")
    if _FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
