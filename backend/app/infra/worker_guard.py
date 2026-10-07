"""多worker 检测 —— 把「单进程部署」这个隐含前提变成显式检查

★ 背景（2026-10-07 压测自查的未覆盖项，现已用实验证明）
--------------------------------------------------------
`governance.guard` 的配额计数用 `threading.Lock` + **进程内内存**。
`threading.Lock` 只在单进程内有效 —— 这是 Python 的定义，不是实现疏忽。

实测（`tests/probe_multiworker.py`，零 API 花费，13 秒跑完）：

    1 进程 × 10 次，上限 10 → 总预扣 10   未超卖
    2 进程 ×  8 次，上限 10 → 总预扣 10   未超卖（碰巧没撞上）
    4 进程 ×  8 次，上限 10 → 总预扣 12   ★ 超卖

各进程自认为已用：`[12, 12, 10, 12]` —— **互相看不见对方的扣减**。

结论：只要有人给本项目加 `--workers 4`，`MAX_GENERATIONS_PER_SESSION`
这道**成本护栏就会静默失效**，而用户完全不会收到任何提示。

所以本模块的职责：**在启动时把这个风险喊出来**，而不是等线上多烧钱才发现。

★ 为什么只是告警、不是直接拒绝启动
    多进程本身不一定是错的（有人可能就是要横向扩展），
    硬拒绝会让"想试一下多 worker"的人撞墙而无从知道原因。
    说清楚"护栏已失效 + 怎么修"，让他自己决定 —— 这是成年人式的做法。
    但如果部署平台明确要求多 worker（见下方 env），就升级为**硬失败**。
"""
from __future__ import annotations

import os

# 这些环境变量出现其一，就说明部署方**主动要求**了多 worker
_WORKER_ENV_HINTS = (
    "WEB_CONCURRENCY",       # Heroku / Fly / Render
    "UVICORN_WORKERS",
    "GUNICORN_CMD",          # Heroku Python buildpack
    "SERVER_WORKER_COUNT",
)

_MSG = (
    "检测到多进程部署（%s），但配额护栏是**进程内**计数"
    "（threading.Lock + 内存），跨进程互不可见 —— "
    "MAX_GENERATIONS_PER_SESSION 已不再可靠，实际花费可能超出预期。"
    "修法：把配额计数改成数据库层的原子扣减"
    "（UPDATE ... SET used = used + 1 WHERE used < :limit，用 rowcount 判定），"
    "或退回单进程部署。"
)


def detect_workers() -> tuple[int, list[str]]:
    """返回 (检测到的 worker 数, 命中的环境变量名列表)"""
    hits = [k for k in _WORKER_ENV_HINTS if (os.getenv(k) or "").strip()]
    workers = 0
    for k in hits:
        raw = (os.getenv(k) or "").strip()
        try:
            workers = max(workers, int(float(raw)))
        except (TypeError, ValueError):
            # GUNICORN_CMD 形如 "gunicorn -w 4 ..."：从命令行里抠出数字
            import re
            m = re.search(r"-w\s*(\d+)|--workers[= ](\d+)", raw)
            if m:
                workers = max(workers, int(m.group(1) or m.group(2)))
    return workers, hits


def check_multiworker(logger=None) -> dict:
    """启动时调用。返回 {"safe": bool, "workers": int, "message": str|None}

    ★ 用法：在 main.py 的启动自检里调一次，把结果塞进 /api/health，
      这样**部署者自己就能看到**，而不是要读日志才发现。
    """
    workers, hits = detect_workers()
    if workers <= 1:
        return {"safe": True, "workers": workers, "message": None,
                "hints": hits}

    msg = _MSG % (f"{workers} 个 worker（来自 {', '.join(hits)}）")
    if logger is not None:
        logger.error("★ %s", msg)
    return {"safe": False, "workers": workers, "message": msg, "hints": hits}
