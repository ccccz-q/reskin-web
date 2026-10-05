"""数据库持久性：把「平台只同步主库文件」这个场景固化成回归测试

事故经过（2026-10-05 线上实测）：
    线上 `/api/health` 的 threads/messages 每次发布后都归零，而图片一直活着。
    排查了两轮：
      · 探针实验（2026-10-05）→ 平台是「上传覆盖」，那不带 agent.db 就该保住
      · 实测：改了之后**还是**归零 ⇒ 说明有别的原因
    真因：**SQLite 的 WAL 模式**。已提交的数据先写在 `-wal` 边车文件里，
    主库文件要等 checkpoint 才合并；而发布时进程被强杀（来不及 close/checkpoint），
    平台又只同步主库文件、不带 `-wal` ⇒ 上次发布之后的会话与消息凭空消失。

    复现（强杀、不 checkpoint、只复制主库文件）：主库只剩 4096 字节、表都没建。

所以现在 `journal_mode` 默认 DELETE：提交直接进主库文件，强杀也不丢。
本测试就是防止有人哪天"顺手把 WAL 加回去"。

★ 全程离线：临时目录，不打网络。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

_root = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(_root))
os.chdir(_root)

import services.context_store as cs                                  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


TMP = Path(tempfile.mkdtemp(prefix="dbpersist_"))
DB = TMP / "agent.db"
cs.SQLITE_PATH = DB
cs._initialized_for = None
os.environ.pop("DB_JOURNAL_MODE", None)          # 用默认（DELETE）

print("\n── 1. 默认不是 WAL ──")
conn = cs._connect()
mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
check("journal_mode 是 DELETE（提交直接进主库文件）", str(mode).upper() == "DELETE", str(mode))
conn.close()

print("\n── 2. 写数据后，主库文件本身就有内容 ──")
cs.init_db(force=True)
cs.append_message("t1", "user", "数据持久性验证 2026-10-05")
cs.append_message("t1", "assistant", "收到")
conn = sqlite3.connect(DB)
n = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
conn.close()
check("messages 表里有 2 条", n == 2, f"{n} 条")
check("主库文件大小明显大于空库", DB.stat().st_size > 20000, f"{DB.stat().st_size} 字节")

print("\n── 3. 模拟平台：强杀进程后只同步主库文件 ──")
killer = TMP / "killer.py"
killer.write_text(textwrap.dedent(f"""
    import os, sys
    sys.path.insert(0, {str(_root)!r})
    os.chdir({str(_root)!r})
    import services.context_store as cs
    from pathlib import Path as _P
    cs.SQLITE_PATH = _P(r"{DB}")          # ★ 必须是 Path：_connect() 会用 .parent
    cs._initialized_for = None
    cs.init_db(force=True)
    cs.append_message("t1", "user", "发布前最后一条")
    os._exit(0)        # ★ 强杀：没有 close、没有 checkpoint
"""), encoding="utf-8")
subprocess.run([sys.executable, str(killer)], check=True)

snapshot = TMP / "snapshot"
snapshot.mkdir()
shutil.copy2(DB, snapshot / "agent.db")          # ★ 平台只带主库文件
con = sqlite3.connect(snapshot / "agent.db")
after = con.execute("SELECT count(*) FROM messages").fetchone()[0]
last = con.execute("SELECT content FROM messages ORDER BY id DESC LIMIT 1").fetchone()
con.close()
check("强杀 + 只同步主库后：数据仍在", after == 3, f"{after} 条（原 2 + 新增 1）")
check("最后一条内容完整", bool(last) and "发布前最后一条" in last[0], str(last)[:40] if last else "无")

print("\n── 4. 回归防护：WAL 模式下同样的场景会丢数据 ──")
wal_dir = TMP / "wal"
wal_dir.mkdir()
wal_db = wal_dir / "agent.db"
w = sqlite3.connect(wal_db)
w.execute("PRAGMA journal_mode=WAL")
w.execute("CREATE TABLE t(x TEXT)")
w.executemany("INSERT INTO t VALUES(?)", [(f"row{i}",) for i in range(30)])
w.commit()
w.close()                                          # 干净关闭 → checkpoint
# 重新打开、写入、不关闭 —— 模拟"服务还在跑，平台来取快照"
w2 = sqlite3.connect(wal_db)
w2.execute("PRAGMA journal_mode=WAL")
w2.execute("INSERT INTO t VALUES('after-restart')")
w2.commit()
snap = wal_dir / "snap"
snap.mkdir()
shutil.copy2(wal_db, snap / "agent.db")            # 只复制主库（-wal 没跟）
c = sqlite3.connect(snap / "agent.db")
wal_rows = c.execute("SELECT count(*) FROM t").fetchone()[0]
c.close()
w2.close()
check("对照组：WAL 下同样操作会丢数据（所以必须用 DELETE）", wal_rows == 30,
      f"WAL 快照只剩 {wal_rows} 条 / 实际 31 条")

print("\n── 5. 可通过环境变量切回 WAL（逃生口）──")
os.environ["DB_JOURNAL_MODE"] = "WAL"
cs._initialized_for = None
c2 = cs._connect()
got = c2.execute("PRAGMA journal_mode").fetchone()[0]
c2.close()
check("DB_JOURNAL_MODE=WAL 能切回去", str(got).upper() == "WAL", str(got))
os.environ.pop("DB_JOURNAL_MODE", None)
cs._initialized_for = None

print(f"\n{'=' * 56}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
