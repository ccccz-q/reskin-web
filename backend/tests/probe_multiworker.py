"""多worker 风险实证 —— 单进程部署下治理层是否安全

    python tests/probe_multiworker.py

★ 为什么要有这个文件（2026-10-07 压测自查列出的未覆盖项之一）
--------------------------------------------------------------
`governance.guard` 的配额计数用 `threading.Lock` + 进程内内存。
threading.Lock 只在**单进程内**有效 —— 这是 Python 的定义，不是实现疏忽。

那么问题来了：**本项目如果起多个 worker会怎样？**
  · 真的起多个进程跑同一份 SQLite，各写各的内存计数 → 必然超卖。
  · 但这个项目**当前是单进程部署**，所以现状安全。

两种态度都可以接受：
  (a) 靠"我们是单进程"这个部署事实回避；
  (b) 把这个前提**变成可检测的东西** —— 一旦有人加了 --workers 4，
      启动时立刻告诉他"配额护栏已失效"，而不是让线上多烧钱之后才发现。

本文件做 (b) 的一部分：**先用实验把风险复现出来**（证明这不是空想），
再由 `guard.check_multiworker_risk()` 在启动时检查（见 infra/worker_guard.py）。

★ 成本：零真实 API 调用，纯本地实验。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
import time
from pathlib import Path

_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _worker(args) -> tuple[int, int]:
    """在**独立进程**里做 N 次预扣，返回 (成功次数, 退出时读到的 used)"""
    db_path, tid, n, limit = args
    # ★ 必须是 Path：`context_store._connect()` 会做
    #   `SQLITE_PATH.parent.mkdir(...)`，传 str 会 AttributeError。
    #   （踩过一次：子进程里设成 str，第一个 worker 就炸了。）
    from pathlib import Path as _P
    _db = _P(db_path)
    import config
    config.SQLITE_PATH = _db
    config.MAX_GENERATIONS_PER_SESSION = limit
    import services.context_store as cs
    cs.SQLITE_PATH = _db
    cs.init_db()
    import governance.guard as guard
    guard.MAX_GENERATIONS_PER_SESSION = limit

    ok = 0
    for _ in range(n):
        try:
            guard.reserve_generation(tid)   # type: ignore[arg-type]
            ok += 1
        except Exception:                 # noqa: BLE001
            pass
    return ok, guard.remaining_quota(tid)["used"]


def run(workers: int, per_worker: int, limit: int) -> dict:
    db = Path(tempfile.mkdtemp(prefix="mw_")) / "x.db"
    tid = "mw-probe"
    t0 = time.perf_counter()
    with mp.get_context("spawn").Pool(workers) as pool:
        results = pool.map(
            _worker, [(str(db), tid, per_worker, limit)] * workers)
    elapsed = time.perf_counter() - t0

    total_ok = sum(r[0] for r in results)
    # 每个进程各自读自己的内存计数（互不可见）
    seen = [r[1] for r in results]
    return {
        "workers": workers,
        "limit": limit,
        "total_reserved": total_ok,
        "oversold": total_ok > limit,
        "per_process_seen": seen,
        "elapsed": elapsed,
    }


def main() -> int:
    print("=" * 72)
    print("多worker 风险实证 —— 配额护栏在多进程下是否还成立")
    print("=" * 72)
    print()
    print("实验设计：N 个**独立进程**共享同一个 SQLite，各预扣 K 次，")
    print("配额上限 L。阈值：只要总预扣数 > L，就说明护栏失效。")
    print()

    LIMIT = 10
    cases = [(1, 10), (2, 8), (4, 8)]
    rows = []
    for workers, per in cases:
        r = run(workers, per, LIMIT)
        rows.append(r)
        flag = "★ 超卖（护栏失效）" if r["oversold"] else "未超卖"
        print(f"  {workers} 进程 × 每进程 {per} 次，上限 {LIMIT}"
              f" → 总预扣 {r['total_reserved']:>3}  {flag}")
        print(f"      各进程自认为已用：{r['per_process_seen']}")
    print()
    multi = [r for r in rows if r["workers"] > 1]
    over = [r for r in multi if r["oversold"]]
    if over:
        print("=" * 72)
        print("结论（实证成立）")
        print("=" * 72)
        print(f"  ★ {len(over)}/{len(multi)} 组多进程实验出现**超卖**。")
        print("  原因很明确：`threading.Lock` 只在单进程内有效，")
        print("  每个进程各自维护内存计数，**互相看不见对方的扣减**。")
        print()
        print("  这意味着：只要有人给本项目加 --workers N，")
        print("  MAX_GENERATIONS_PER_SESSION 这道**成本护栏就会静默失效**。")
        print()
        print("  现状是安全的（本项目单进程部署），但这个前提必须**可检测**：")
        print("    · 启动自检见 infra/worker_guard.py —— 检测到多 worker 时")
        print("      明确告警并指出护栏已失效，而不是等线上多烧钱才发现。")
        print("    · 若将来真要横向扩展，必须先把配额计数挪到数据库层，")
        print("      用带条件的 UPDATE 实现原子扣减（见报告「已知限制」）。")
    else:
        print("结论：本次实验**未观察到**超卖。")
        print("  （可能是平台进程启动方式导致写入没真正并发，")
        print("    但不应据此认为多进程是安全的 —— 换平台/换部署方式结论可能变。）")
        print("  保守起见，启动自检仍然保留：检测到多 worker 就告警。")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())