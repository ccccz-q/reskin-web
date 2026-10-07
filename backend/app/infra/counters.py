"""基础设施层 · 治理计数与预扣票据（窄接口 + 实现）

为什么需要这个文件（分层纪律，不是洁癖）
--------------------------------------
`governance/` 是比 `services/` 更底层的一层：它决定"能不能花钱"，
`services/` 只负责"怎么做"。让 `governance` 去 `import services`
就是**下层反过来依赖上层**。看着只是多一行 import，实际后果有两个：

1. `services/context_store.py` 有 1100+ 行、还带一整套 FTS5 建表与
   自愈逻辑。治理层只想借 6 个计数器函数，却被迫在 import 期把整个
   记忆层拖进来 —— 而 `infra/logging.py` 已经能直接用 `config.STORAGE_DIR`，
   正是同一条纪律的另一个证据。
2. 更麻烦的是**测试**：只要治理层 import 了记忆层，任何想单独测治理的
   用例都得先准备一个 SQLite 库。分层一旦倒过来，依赖就再也拆不开。

所以这六个函数连同实现一起下沉到这里，依赖方向变成

    governance ──▶ infra.counters ◀── services.context_store
                  （谁都不反向依赖谁）

★ 为什么实现可以搬下来，而连接与写锁必须**共用**同一份
------------------------------------------------
这两个表（session_counter / reservations）与消息、FTS 毫无关系，
自成一体，搬下来没有副作用。但它们**写在同一个 SQLite 文件里**，
于是两件事必须小心：

① **写锁必须共用一个**。context_store 的 `_write_lock` 若与这里各一把，
   两边同时写就会真的撞上 `database is locked`。
   所以本模块导出 `write_lock`，由 context_store 直接引用同一对象——
   进程内所有写仍然只排队一次。
② **journal_mode 的口径必须只有一份**。那个"默认 DELETE、别用 WAL"的
   决定是踩过一次线上数据丢失才定下来的（见 context_store 里的注释），
   复制第二份实现等于给那颗雷又埋一个引信。所以连接逻辑也在这里，
   context_store 的 `_connect()` 改为调用 `connect()`。

★ 为什么不用「后绑定」（先声明接口、让 services 运行时 bind 进来）
----------------------------------------------------------------
第一版就是这么写的，并且它**看起来**能跑 —— 直到 test_repair.py 报
`CountersUnbound`：那个用例 import 了 `governance.guard` 却没 import
记忆层，于是治理层拿到一个没人绑过的空接口。

这类"能用但会突然炸"的接线依赖太危险：它把一个import 顺序问题
变成运行期随机失败（线上表现是「重启一下就好了」，最难查的那种）。
下沉实现虽然多写了约 60 行SQL，却让依赖变成**静态可查**的：
`grep -rn "services" app/infra/ app/governance/` 必须零命中，
这条纪律从此由编译器之外的手段真正守住。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as _config                                         # noqa: E402
from infra.logging import logger                               # noqa: E402


# ★ SQLITE_PATH 必须在**调用时**从config 读，不能在 import 时绑成局部常量。
#   原因与 storage.ensure_within 的注释是同一条纪律：测试与运维都会在
#   import **之后**改 `config.SQLITE_PATH` 把库重定向到临时目录
#   （test_governance / test_concurrency / test_db_persistence 都这么干）。
#   一绑成模块常量，治理层就会继续往真实的库文件里写 —— 那是"测试污染
#   生产数据"，比测试失败严重得多。
def _db_path(db: Path | str | None = None) -> Path:
    return Path(db) if db is not None else Path(_config.SQLITE_PATH)


# ★ 进程内**唯一一把**写锁。context_store 引用的是同一个对象（见模块说明）。
write_lock = threading.Lock()

_COUNTER_DDL = """
CREATE TABLE IF NOT EXISTS session_counter (
    key       TEXT PRIMARY KEY,
    value     INTEGER NOT NULL DEFAULT 0,
    updated   TEXT
);
CREATE TABLE IF NOT EXISTS reservations (
    token     TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resv_thread ON reservations(thread_id);
"""

# ★ 哪些库文件已经建过表。SQLite 的 `CREATE TABLE IF NOT EXISTS` 虽然便宜，
#   但每次计数都跑一遍 DDL 仍是纯浪费；按路径记住即可。
_initialized_for: set[str] = set()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开治理用的 SQLite 连接

    ★ journal_mode 的默认值与理由**只在这里写一份**（DELETE，不能用 WAL：
    已提交数据会先落在 -wal 边车里，而发布平台只同步主库文件，
    强杀进程就会丢掉上一次发布后的全部计数 —— 事故细节见
    context_store.py 里的 DB_JOURNAL_MODE 注释）。
    context_store._connect() 现在也走这里，保证全进程只有一种口径。
    """
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    mode = (os.getenv("DB_JOURNAL_MODE") or "DELETE").strip().upper()
    try:
        got = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()
        if not (got and str(got[0]).upper() == mode):
            logger.warning("journal_mode=%s 未生效（实际 %s），"
                           "若为 WAL 且发布后数据丢失，请检查平台是否同步 -wal 文件",
                           mode, got[0] if got else "?")
    except sqlite3.DatabaseError as e:
        logger.warning("journal_mode 设置失败（%s），用默认模式", e)
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _open(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """连接上下文：**提交 + 回滚 + 关闭** 三件事都做

    与 context_store 同名函数同一套规矩（那里解释了为什么不能直接用
    `with sqlite3.connect(...)` —— 它只commit/rollback，不close）。
    """
    path = _db_path(db_path)
    key = str(path)
    conn = connect(path)
    try:
        if key not in _initialized_for:
            # ★ DDL 走 autocommit：不裹进事务。
            #   实测教训见 context_store.init_db —— 事务里重建虚表会让下一次
            #   连接读到 "database disk image is malformed"，极难查。
            conn.executescript(_COUNTER_DDL)
            conn.commit()
            _initialized_for.add(key)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ─────────────────────── 会话计数器 ───────────────────────
# 「本会话已生成几张」这类治理计数的落盘处。
# 余额必须落库而不是放内存变量：内存会在重启后清零，
# 「重启一下额度就回来了」对一个要有说服力的工程来说是致命的。

def bump_counter(key: str, delta: int = 1, *, db: Path | str | None = None) -> int:
    """原子自增，返回新值"""
    now = datetime.now().isoformat(timespec="seconds")
    with write_lock, _open(db) as conn:
        conn.execute(
            "INSERT INTO session_counter(key, value, updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=value+?, updated=excluded.updated",
            (key, delta, now, delta),
        )
        row = conn.execute(
            "SELECT value FROM session_counter WHERE key=?", (key,)
        ).fetchone()
    return int(row["value"]) if row else 0


def try_increment_capped(key: str, limit: int, *,
                         db: Path | str | None = None) -> tuple[bool, int]:
    """**条件自增**：只有当前值 < limit 时才加1。返回 (是否抢到, 加完后的值)

    ★ 为什么必须有这个（2026-10-07 M1：多worker 下配额护栏失效）
    ------------------------------------------------------
    原来的写法是「读 → 判断 → 加」三步，外面套一把 `threading.Lock`：

        with _reserve_lock:
            if remaining_quota(tid)["exhausted"]: raise
            bump_counter(key, 1)

    在**单进程**内这把锁够用。但 `threading.Lock` 只在进程内有效 ——
    部署成 4 个 worker 时，四个进程各自持有自己的锁、各自维护内存视图，
    互相看不见对方的扣减。实测（tests/probe_multiworker.py）：
    上限 10、4 进程各预扣 8 次 → **总预扣 12，超卖**。
    配额是**保护 API 花费**的闸，失效就是真金白银的损失。

    修法：把"判断"和"加"合并成**一条 SQL**，让数据库自己保证原子性：

        UPDATE session_counter SET value = value + 1
         WHERE key = ? AND value < ?

    `cursor.rowcount == 1` 表示抢到了额度，`0` 表示已满。
    SQLite 保证单条 UPDATE 的原子性，**天然跨进程**。

    ★ 为什么要 INSERT ... ON CONFLICT 而不是直接 UPDATE：
      第一次用这个 key 时行还不存在，UPDATE 影响 0 行，会被误判成"已满"。
      所以先用 upsert 播种（value=0），再走条件 UPDATE。

    ★ limit <= 0 表示不限额度（与 MAX_GENERATIONS_PER_SESSION<=0 同义），
      此时直接自增并返回 True。
    """
    now = datetime.now().isoformat(timespec="seconds")
    with write_lock, _open(db) as conn:
        if limit <= 0:
            conn.execute(
                "INSERT INTO session_counter(key, value, updated) VALUES(?,1,?) "
                "ON CONFLICT(key) DO UPDATE SET value=value+1, updated=excluded.updated",
                (key, now),
            )
            row = conn.execute(
                "SELECT value FROM session_counter WHERE key=?", (key,)
            ).fetchone()
            return True, (int(row["value"]) if row else 1)
        # ① 播种：没有这一行就先建一个 0
        conn.execute(
            "INSERT INTO session_counter(key, value, updated) VALUES(?,0,?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, now),
        )
        # ② 条件自增：只有 value < limit 才加 —— 这一步是原子的
        cur = conn.execute(
            "UPDATE session_counter SET value = value + 1, updated=? "
            "WHERE key=? AND value < ?",
            (now, key, int(limit)),
        )
        got = cur.rowcount == 1
        row = conn.execute(
            "SELECT value FROM session_counter WHERE key=?", (key,)
        ).fetchone()
        return got, (int(row["value"]) if row else 0)


def get_counter(key: str, *, db: Path | str | None = None) -> int:
    with _open(db) as conn:
        row = conn.execute("SELECT value FROM session_counter WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row else 0


def reset_counter(key: str, *, db: Path | str | None = None) -> None:
    with write_lock, _open(db) as conn:
        conn.execute("DELETE FROM session_counter WHERE key=?", (key,))


# ─────────────── 额度预扣票据（治理层用）───────────────
#
# ★ 为什么票据必须落库而不是放内存（审查发现 P1-3 / P1-4）
# ------------------------------------------------------
# 第一版把票据放在进程内的 set 里，两个后果都很实在：
#   ① 无界增长：每次成功生成都留一条永不清理的条目（settle 没 discard）。
#   ② **崩溃即泄漏**：reserve 已经把 DB 计数 +1 了，票据却在内存里；
#      进程一重启票据没了 → release 因「无票据」被拒 → 那 1 张额度永久拿不回来，
#      只能靠 /reset-quota 手动清。
# 落库之后：票据有持久凭据，启动时还能按 TTL 回收「预扣了但没结果」的残票。

def add_reservation(token: str, thread_id: str, *,
                    db: Path | str | None = None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with write_lock, _open(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reservations(token, thread_id, ts) VALUES(?,?,?)",
            (token, thread_id, now),
        )


def consume_reservation(token: str, *, db: Path | str | None = None) -> bool:
    """兑现票据（一次性）。返回 True 表示这张票之前确实存在。

    用 DELETE 的 rowcount 做原子判定 —— 不要先 SELECT 再 DELETE，
    那中间会被并发请求插进来，导致同一张票被兑现两次。
    这是「票据只能兑现一次」这条不变式的落点，**不要改写这里的写法**。
    """
    with write_lock, _open(db) as conn:
        cur = conn.execute("DELETE FROM reservations WHERE token=?", (token,))
        return (cur.rowcount or 0) > 0


def peek_reservation(token: str, *, db: Path | str | None = None) -> dict | None:
    with _open(db) as conn:
        row = conn.execute(
            "SELECT token, thread_id, ts FROM reservations WHERE token=?", (token,)
        ).fetchone()
    return dict(row) if row else None


def list_reservations(older_than_sec: int | None = None, *,
                      db: Path | str | None = None) -> list[dict]:
    """列出未兑现的票据；给了older_than_sec 就只返回超过该年龄的"""
    with _open(db) as conn:
        rows = conn.execute(
            "SELECT token, thread_id, ts FROM reservations ORDER BY ts"
        ).fetchall()
    out = [dict(r) for r in rows]
    if older_than_sec is None:
        return out
    cutoff = datetime.now().timestamp() - older_than_sec
    stale = []
    for r in out:
        try:
            if datetime.fromisoformat(r["ts"]).timestamp() < cutoff:
                stale.append(r)
        except (ValueError, TypeError):
            stale.append(r)          # 时间戳解析不了的一律当残票
    return stale


def reservations_stats(*, db: Path | str | None = None) -> dict:
    with _open(db) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM reservations").fetchone()
    return {"pending": int(row["c"]) if row else 0}


def forget_initialized(db_path: Path | str | None = None) -> None:
    """让指定库（或全部）下次使用时重建表结构

    存在的意义：context_store 有 `init_db(force=True)` 这类"强制重跑
    迁移"的入口，本模块必须能被同步地清掉自己的"已建过表"记忆，
    否则测试把SQLITE_PATH 指向一个新文件后会误以为表已存在。
    """
    if db_path is None:
        _initialized_for.clear()
    else:
        _initialized_for.discard(str(Path(db_path)))


__all__ = [
    "add_reservation",
    "bump_counter",
    "connect",
    "consume_reservation",
    "forget_initialized",
    "get_counter",
    "list_reservations",
    "peek_reservation",
    "reset_counter",
    "reservations_stats",
    "write_lock",
]