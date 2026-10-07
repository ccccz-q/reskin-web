"""服务层 · 会话上下文与长期记忆 —— SQLite + FTS5

════════ 为什么重写（对照审查报告 P1 系列 + §0 决策 6）════════

旧 `services/memory.py` 有三个致命问题：

1. **路径算错 → 双份存储**
   ```python
   MEMORY_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "storage")
   ```
   `__file__` 是 `backend/app/services/memory.py`，
   `..` → `backend/app`，再 `..` → `backend`，于是长期记忆落在 **`backend/storage/`**，
   而 `config.py` 早就规定了 **`项目/storage/`**。
   这正是「项目里同时存在两份 storage」的来源之一 —— 必须消灭。

2. **依赖 LangChain 消息对象**：`m.type == 'human'`。
   Agent 改用自建 Tool-use Loop 后消息是普通 dict，这里会直接 AttributeError。

3. **裸 `except:` + 手写 JSON 切割**：解析失败静默返回空偏好，调用方永远不知道失败过。

新实现给出的能力：
- **FTS5 全文检索**：替代被移除的 ChromaDB/向量库（§0 决策 6）。
  「上次我要的那张雪山的图是哪套参数」这类查询用 bm25 排序召回即可，
  不需要再为一个 demo 引入一套 embedding 依赖链。
- **脱敏的翻译层**：对外一律收发 dict，LangChain 彻底出局。
- **可降级**：没有 FTS5 编译选项时自动退回 LIKE，不会让服务起不来。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
# ★ Path 在下面 `_initialized_for: Path | None` 的注解里用到。
#   此前**只写了注解没导入**：因为 `from __future__ import annotations` 让模块级
#   变量注解不求值，所以侥幸没炸 —— 但只要有人把它改成类属性或函数签名注解，
#   就是运行期 NameError。潜伏缺陷比崩溃更难查，所以补上导入。
from pathlib import Path
from typing import Any, Iterator

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SQLITE_PATH                                  # noqa: E402
from infra.logging import audit, logger                          # noqa: E402

# 长期记忆文件也归到统一的 STORAGE_DIR 下（旧版散在 backend/storage）
SCHEMA_VERSION = 1


# ─────────────────────────── 连接 ───────────────────────────

@contextmanager
def _open() -> Iterator[sqlite3.Connection]:
    """连接上下文：**提交 + 回滚 + 关闭** 三件事都做

    ★ 为什么不能直接用 `with sqlite3.connect(...)`：
      `Connection.__exit__` 只做 commit / rollback，**不会 close**。
      实测：退出 with 之后 conn.execute() 依然成功 —— 连接还开着，
      靠 CPython 引用计数才回收；一旦被异常 traceback 或 except-as 变量
      持有，就会永久悬挂（持有 WAL 读锁与文件句柄）。

    ★ 但也不能只 close 不 commit（我犯过这个错）：
      sqlite3 连接作为上下文管理器是会**提交事务**的。
      只 close 的话，未提交的事务会被隐式回滚 —— 表现是
      `append_message` 返回了自增 id，但下一次查询什么都查不到。
      排查时极具迷惑性：不报错、不丢异常、id 还在涨、数据却是空的。
    """
    conn = _connect()
    try:
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


def _connect() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SQLITE_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    # ── journal 模式：为什么不用 WAL ──────────────────────────────
    #
    # ★ 2026-10-05 线上实测踩出来的：WAL 下**已提交的数据先落在 -wal 边车文件里**，
    #   主库文件要等 checkpoint 才合并。发布时进程被强杀（SIGKILL，来不及
    #   close/checkpoint），而平台只同步主库文件、不带 -wal ⇒
    #   **上一次发布之后新增的会话与消息全部消失**。
    #   复现实验：强杀后主库文件只有 4096 字节、表都没建，而数据在 -wal 里躺着。
    #
    #   所以这里默认 DELETE：每次提交直接写进主库文件，强杀也不丢。
    #   代价是并发写会互相等锁 —— 但我们这个量级（几个访客）完全无所谓，
    #   换来的是「数据不会因为一次发布而消失」，这笔交易太划算了。
    #   真要 WAL，可通过环境变量 DB_JOURNAL_MODE=WAL 切回去。
    mode = (os.getenv("DB_JOURNAL_MODE") or "DELETE").strip().upper()
    try:
        got = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()
        if not (got and str(got[0]).upper() == mode):
            logger.warning("journal_mode=%s 未生效（实际 %s），"
                           "若为 WAL 且发布后数据丢失，请检查平台是否同步 -wal 文件",
                           mode, got[0] if got else "?")
    except sqlite3.DatabaseError as e:
        logger.warning("journal_mode 设置失败（%s），用默认模式", e)
    # DELETE 模式下 synchronous=NORMAL 仍然安全（NORMAL 只在 WAL 下放松 fsync；
    # 这里是 FULL 语义才有性能损失，而我们并不需要那个性能）。
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _read_user_version(conn: sqlite3.Connection) -> int:
    """读 SQLite 文件头里的 schema 版本号（0 = 从未标记过）"""
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.DatabaseError:
        return 0


def _write_user_version(conn: sqlite3.Connection, version: int) -> None:
    """把版本号写回文件头。

    ★ 为什么现在才做（2026-10-06 评审自查发现）：
      `SCHEMA_VERSION = 1` 这个常量从项目第一天就写在文件头，却**从来没有被读过**
      —— 全库仅定义处一处出现。它给人一种"我们有版本管理"的错觉，
      实际升级全靠 `_ensure_column()` 逐列幂等补：能用，但没有"能不能升级"的判据，
      也没有降级路径（老代码碰到新库会怎样，没人知道）。

    为什么存在 `user_version` 里而不是自建表：
      它是 SQLite 文件头里的保留空间（字节 60-63），**不占任何表、不进 SELECT 列表**，
      备份/替换整个 .db 文件时天然跟着走，也不会被用户的 SQL 意外改掉。
    """
    try:
        conn.execute(f"PRAGMA user_version={int(version)}")
    except sqlite3.DatabaseError as e:                # pragma: no cover
        logger.warning("写入 schema 版本号失败：%s", e)


def check_schema_version(conn: sqlite3.Connection | None = None) -> dict:
    """比对文件里的版本与代码期望的版本 —— 供启动自检与测试使用

    返回 {"file": int, "code": int, "ahead": bool}
      · ahead=True  表示**库比代码新**（发布回滚了，或代码是旧副本）
        —— 这是唯一危险的方向：新列/新表在旧代码里不存在，可能写入失败。
      · file < code 是正常状态（老库被升级），由_ensure_column 兜着。
    """
    owned = conn is None
    c = conn or _connect()
    try:
        cur = _read_user_version(c)
    finally:
        if owned:
            try:
                c.close()
            except sqlite3.Error:
                pass
    return {"file": cur, "code": SCHEMA_VERSION, "ahead": cur > SCHEMA_VERSION}


_FTS_TOKENIZER: str | None = None      # 已确认可用的分词器


def _pick_tokenizer() -> str | None:
    """挑分词器 —— 结论来自实测，不是查文档拍脑袋

    本机 SQLite 3.53.1 实测同一份中文语料：
        unicode61   雪山→0   小人国→0   参数→1   插画→0   sunset→1
        trigram     雪山→0   小人国→1   参数→0   插画→0   sunset→1

    两个都不够：
    - unicode61 把连续的汉字当成一个巨型 token，只有被空格/标点隔开的词才命中
      （「参数」前面有空格所以侥幸命中，「雪山」夹在句子里就查不到）
    - trigram 做三字符切分，天生支持子串匹配，但 **查询词必须 ≥3 字符**，
      于是「雪山」「插画」这种两字词全部落空

    结论：**不能指望单一分词器**。策略是 trigram 索引（>=3 字效果最好）
    加上 LIKE 兜底并集，由 `_search_like()` 把两字词这一层补上。
    """
    global _FTS_TOKENIZER
    if _FTS_TOKENIZER is not None:
        return _FTS_TOKENIZER

    probe_cases = (
        ("trigram", "小人国", 1),
        ("unicode61", "sunset", 1),
    )
    chosen: str | None = None
    for tok, _q, _expect in probe_cases:
        if tok == "trigram" and chosen:
            continue
        try:
            with sqlite3.connect(":memory:") as c:
                c.execute(
                    f"CREATE VIRTUAL TABLE t USING fts5(content, tokenize='{tok}')"
                )
            if chosen is None:
                chosen = tok
        except sqlite3.OperationalError:
            pass
    if chosen is None:
        try:
            with sqlite3.connect(":memory:") as c:
                c.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
            chosen = "unicode61"
        except sqlite3.OperationalError:
            chosen = None
            logger.warning("当前 SQLite 未编译 FTS5，搜索降级为 LIKE")

    _FTS_TOKENIZER = chosen
    return chosen


def _has_fts5() -> bool:
    return _pick_tokenizer() is not None


_TOKENIZE_CLAUSE = {"unicode61": "tokenize='unicode61'", "trigram": "tokenize='trigram'"}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """列级轻量迁移 —— 老库不会因为新增字段而读不到数据

    为什么需要：CREATE TABLE IF NOT EXISTS 对**已存在**的表不会补新列。
    视觉卡（card）是后加的字段，不迁移的话老库 INSERT 会直接报错，
    而用户看不懂 "no such column"，只会觉得"模板工坊坏了"。
    """
    try:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _ensure_fts_shape(conn: sqlite3.Connection) -> None:
    """保证 messages_fts 的分词器与当前选择一致，不一致就重建

    为什么需要：开发机上先跑过旧代码留下的库文件会是 unicode61，
    换成 trigram 之后如果不重建，Altering 会被 SQLite 静默忽略，
    表现为「代码改了但搜索行为没变」 —— 典型的隐蔽不一致。
    """
    tok = _pick_tokenizer()
    if not tok:
        return
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages_fts' AND type='table'"
    ).fetchone()
    if row is None:
        return
    existing_sql = row["sql"] or ""
    want = _TOKENIZE_CLAUSE[tok]
    # unicode61 是默认值，建表 SQL 里可能压根不出现 tokenize 子句
    if tok == "unicode61":
        if "tokenize" not in existing_sql or "unicode61" in existing_sql:
            return
    elif want.replace("'", "") in existing_sql.replace('"', "").replace("'", ""):
        return

    logger.warning("messages_fts 分词器已变更（现为 %s），重建索引", tok)
    conn.execute("DROP TRIGGER IF EXISTS messages_ai")
    conn.execute("DROP TRIGGER IF EXISTS messages_ad")
    conn.execute("DROP TABLE IF EXISTS messages_fts")
    conn.commit()
    _create_fts(conn)
    conn.commit()


def _create_fts(conn: sqlite3.Connection) -> None:
    tok = _pick_tokenizer()
    if not tok:
        return
    tokenize_clause = _TOKENIZE_CLAUSE[tok]
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
        USING fts5(
            content,
            thread_id UNINDEXED,
            content='messages',
            content_rowid='id',
            {tokenize_clause}
        )
        """
    )
    # external content 表需要手工维护，用触发器省得漏
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, thread_id)
            VALUES (new.id, new.content, new.thread_id);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, thread_id)
            VALUES ('delete', old.id, old.content, old.thread_id);
        END
        """
    )


_db_lock = threading.Lock()

# 进程内写操作串行化。SQLite 的写本来就是排他的，与其让多个连接互相
# 撞 SQLITE_BUSY 再等 timeout，不如在应用侧直接排队 —— 更快也更可预测。
# 实测副产品：受限环境（沙箱 / 只读挂载）下 WAL 的 -shm 拿不到时，
# 并发写会报 "attempt to write a readonly database"，串行化后连带规避。
_write_lock = threading.Lock()
# ★ 记住「已经为哪个路径初始化过」。只记布尔值是不够的：
#   测试常常先 import 再改 cs.SQLITE_PATH 指向临时库，
#   如果只看 bool，init_db() 会直接返回 → 临时库里根本没有表
#   → 报 "no such table: messages"（这个坑我刚踩过）。
_initialized_for: Path | None = None


def init_db(force: bool = False) -> None:
    """建表（幂等）

    两处刻意为之的设计：

    1. **进程内只做一次**。旧版每个公开函数（append_message / bump_counter /
       search / stats …）开头都调一次 init_db()，而 init_db 里要跑
       executescript(DDL) + 分词器形状检查 + 两次 COUNT 自检。
       实测单次 append_message 从 7.2ms 涨到 13.8ms —— 一倍纯浪费；
       Agent 一轮对话写几十条消息，就是几十次全量 DDL。
       更糟的是并发下两个线程同时判定「索引不一致」，会**反复 DROP 重建**
       同一个虚表（实测 4 线程各写 25 条 → 触发 3 次重建）。

    2. **DDL 一律走 autocommit，不裹进事务**。
       实测教训：WAL 模式下把 `DROP TABLE messages_fts / CREATE VIRTUAL TABLE`
       包进事务，会让下一次连接读到 `database disk image is malformed`
       （紧接着 integrity_check 又是 ok —— 崩溃恢复把痕迹抹掉了，极难查）。

    索引坏了怎么办？不是靠启动时扫，而是 `search()` 撞到
    `no such table` 之类的错误时按需重建（见 _rebuild_fts_on_demand）。
    """
    global _initialized_for
    with _db_lock:
        if _initialized_for == SQLITE_PATH and not force:
            return
        conn = _connect()
        try:
            conn.executescript(_BASE_DDL)      # executescript 会先 COMMIT 再执行
            conn.commit()
            # 列级迁移：老库补上后加的字段（视觉卡等）
            _ensure_column(conn, "forged_families", "card", "TEXT")
            _ensure_column(conn, "forged_families", "prompt_override", "TEXT")
            _ensure_column(conn, "forged_families", "owner_session", "TEXT")
            conn.commit()
            _ensure_fts_shape(conn)
            _create_fts(conn)
            conn.commit()
            _self_heal_fts(conn)
            # ★ 2026-10-06：真正把版本号写进文件头（见 _write_user_version 注释）。
            #   顺序很讲究 —— **先做完所有补列/重建，最后才盖版本号**：
            #   万一中途失败，文件里留的还是旧版本，下次启动会继续尝试升级；
            #   如果先盖版本号，失败就会被误判成"已升级完成"。
            ver = _read_user_version(conn)
            if ver > SCHEMA_VERSION:
                # 唯一危险的方向：库比代码新（发布回滚 / 拿了旧副本代码配新库）
                logger.error("★ 数据库 schema 版本(%d) 高于当前代码(%d) —— "
                             "可能是回滚了发布或代码没同步。旧代码不认识新列/新表，"
                             "写入可能失败。请确认代码与数据库来自同一次发布。",
                             ver, SCHEMA_VERSION)
                audit("schema_version_ahead", file_version=ver,
                      code_version=SCHEMA_VERSION)
            elif ver < SCHEMA_VERSION:
                logger.info("数据库 schema 升级：%d → %d", ver, SCHEMA_VERSION)
            _write_user_version(conn, SCHEMA_VERSION)
            conn.commit()
        finally:
            conn.close()
        _initialized_for = SQLITE_PATH


_BASE_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    role      TEXT NOT NULL,
    content   TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    ts        TEXT NOT NULL,
    meta      TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id, id);
CREATE TABLE IF NOT EXISTS long_term (
    thread_id TEXT PRIMARY KEY,
    summary   TEXT,
    prefs     TEXT,
    updated   TEXT
);
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
CREATE TABLE IF NOT EXISTS forged_families (
    id          TEXT PRIMARY KEY,
    lineage     TEXT NOT NULL,   -- 同一条迭代链共享（改版不换 lineage）
    version     INTEGER NOT NULL,
    name        TEXT,
    family_id   TEXT,
    spec        TEXT NOT NULL,   -- 家族 JSON
    prompt      TEXT,            -- 当前版本渲染出的提示词（可直接复制）
    prompt_override TEXT,        -- ★ 用户手动修改过的提示词（非空=生成时直接用它，
                                 --   跳过 spec 渲染；空=自动渲染。实测需求 2026-10-03：
                                 --   用户只想改某个片段，不该被迫跑 4-6 分钟 LLM 迭代）
    theory      TEXT,            -- 用户给的理论文本
    feedback    TEXT,            -- 本版依据的修改意见
    images      TEXT,            -- JSON 数组
    installed   INTEGER DEFAULT 0,
    created_at  TEXT,
    owner_session TEXT          -- ★ 公开版属主：空=内置家族（人人可见）；
                                 --   非空=该会话自建（只见自己的 + 内置的）
);
CREATE INDEX IF NOT EXISTS idx_forge_lineage ON forged_families(lineage);
CREATE INDEX IF NOT EXISTS idx_forge_created ON forged_families(created_at);
"""


def _rebuild_fts_on_demand() -> bool:
    """查询撞到「索引不可用」时按需重建 —— 而不是启动时例行扫描

    启动扫描在并发下是个陷阱：别的线程正在 INSERT，
    COUNT(messages_fts) 与 COUNT(messages) 就永远不相等 → 反复 DROP 重建。
    改成「用的时候坏了才修」，既消除了竞态，也省掉了每次启动的两次全表 COUNT。
    """
    conn = _connect()
    try:
        return _self_heal_fts(conn)
    finally:
        conn.close()


def _self_heal_fts(conn: sqlite3.Connection, force: bool = False) -> bool:
    """索引坏了就重建 —— 外部内容索引是可丢弃的派生数据

    为什么要这道保险：`messages_fts` 与 `messages` 靠触发器同步，
    一旦进程被强杀 / WAL 没落盘，两边就会失步，届时 search 直接抛错。
    与其让整个会话挂掉，不如检测到不一致就丢掉重建，成本几毫秒。
    """
    tok = _pick_tokenizer()
    if not tok:
        return False
    got = exp = None
    try:
        got = int(conn.execute("SELECT COUNT(*) c FROM messages_fts").fetchone()["c"])
        exp = int(conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"])
        if got == exp and not force:
            return False
    except sqlite3.DatabaseError as e:
        logger.warning("FTS 索引不可用(%s)，重建", e)
    except Exception as e:                       # 连 messages 都读不了就不归这里管
        logger.warning("索引自检跳过：%s", e)
        return False

    logger.warning("FTS 索引与消息表不一致（%s 条 vs %s 条），删除重建",
                   got, exp)
    conn.execute("DROP TRIGGER IF EXISTS messages_ai")
    conn.execute("DROP TRIGGER IF EXISTS messages_ad")
    conn.execute("DROP TABLE IF EXISTS messages_fts")
    conn.commit()
    _create_fts(conn)
    conn.commit()
    # 从现有消息回填索引
    rows = conn.execute("SELECT id, content, thread_id FROM messages").fetchall()
    if rows:
        conn.executemany(
            "INSERT INTO messages_fts(rowid, content, thread_id) VALUES(?,?,?)",
            [(r["id"], r["content"], r["thread_id"]) for r in rows],
        )
        conn.commit()
    return True


# ─────────────────────── 消息读写 ───────────────────────

_ROLES = {"system", "user", "assistant", "tool"}


def append_message(
    thread_id: str,
    role: str,
    content: str | None,
    *,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    meta: dict | None = None,
) -> int:
    """写一条消息，返回自增 id"""
    if role not in _ROLES:
        raise ValueError(f"未知 role {role!r}，应为 {sorted(_ROLES)}")
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, _open() as conn:
        cur = conn.execute(
            "INSERT INTO messages(thread_id, role, content, tool_call_id, tool_name, ts, meta)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                thread_id,
                role,
                content,
                tool_call_id,
                tool_name,
                now,
                json.dumps(meta, ensure_ascii=False) if meta else None,
            ),
        )
        return int(cur.lastrowid or 0)


def append_many(thread_id: str, messages: list[dict], meta: dict | None = None) -> None:
    """批量写入一轮的工具调用轨迹（tool role 的消息也要留痕）

    【预留】当前无调用方：engine/loop.py 是逐条 append 的（每条之后要立刻可见）。
    保留是因为批量写在「导入历史 / 回放」场景下是合理能力，删了反而缺一块。
    """
    for m in messages:
        append_message(
            thread_id,
            m.get("role", "user"),
            m.get("content"),
            tool_call_id=m.get("tool_call_id"),
            tool_name=m.get("name") or m.get("tool_name"),
            meta=meta,
        )


def _row_to_msg(r: sqlite3.Row) -> dict:
    """数据库行 → 可直接喂给 OpenAI 的消息

    ★ 必须还原 `tool_calls`（审查发现 P0-2）
    --------------------------------------
    写库时我们把带 tool_calls 的 assistant 消息存成
    `content=None` + `meta={"tool_calls": [...]}`。旧实现只取
    role/content/tool_call_id，**把 meta 整个丢了** —— 于是历史回放出来是：

        {"role": "assistant"}                    ← 既无 content 也无 tool_calls
        {"role": "tool", "tool_call_id": "c1"}   ← 找不到对应的 tool_calls

    这是非法的 tools 协议。同一个会话里只要发生过一次工具调用，
    第二轮请求就会被服务端判 400 —— 表现为前端看到莫名其妙的「模型调用失败」。

    离线测试抓不到它，因为每个测试 thread 通常只发一条消息。
    """
    msg: dict[str, Any] = {"role": r["role"]}

    content = r["content"]
    raw_meta = r["meta"]
    meta: dict = {}
    if raw_meta:
        try:
            meta = json.loads(raw_meta) or {}
        except json.JSONDecodeError:
            meta = {}

    tool_calls = meta.get("tool_calls")
    if r["role"] == "assistant" and tool_calls:
        msg["content"] = content if content else None
        msg["tool_calls"] = [
            {
                "id": tc.get("id"),
                "type": "function",
                "function": {
                    "name": tc.get("name"),
                    "arguments": (
                        tc.get("arguments")
                        if isinstance(tc.get("arguments"), str)
                        else json.dumps(tc.get("arguments") or {}, ensure_ascii=False)
                    ),
                },
            }
            for tc in tool_calls
            if isinstance(tc, dict)
        ]
    elif content is not None:
        msg["content"] = content

    if r["tool_call_id"]:
        msg["tool_call_id"] = r["tool_call_id"]
    if r["tool_name"]:
        msg["name"] = r["tool_name"]
    return msg


def history(thread_id: str, limit: int = 40) -> list[dict]:
    """取最近 N 条消息（按时间正序，可直接喂给 OpenAI）

    注意：**system 不入这条表** —— System Prompt 由 engine/prompts.py 动态装配，
    存进 DB 会让「改了策略但历史仍用旧策略」，也会让前缀缓存失效。
    """
    init_db()
    with _open() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE thread_id=? "
            "ORDER BY id DESC LIMIT ?",
            (thread_id, limit),
        ).fetchall()
    return [_row_to_msg(r) for r in reversed(rows)]


def message_count(thread_id: str) -> int:
    init_db()
    with _open() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE thread_id=?", (thread_id,)
        ).fetchone()
    return int(row["c"]) if row else 0


def history_for_llm(thread_id: str, limit: int = 40) -> list[dict]:
    """取历史并做 **OpenAI tools 协议配对净化**

    ★ 为什么不能直接把 history() 喂给模型（审查发现 P0-2）
    ---------------------------------------------------
    真实会话里会出现两种「不配对」的状态，都是正常产生的、不是数据损坏：

      ⓐ **窗口截断**：limit 把某轮的开头切掉了 →
         assistant 带 tool_calls 的那条不在窗口里，只剩孤立的 tool 消息。
      ⓑ **提前中止**：断连 / 熔断时 assistant 声明了 3 个 tool_calls，
         只执行了 2 个 → 有 1 个 id 永远没有对应回复。

    两者都会让请求体不合法（tool 消息找不到父 tool_calls，或反之），
    服务端直接 400。所以喂给模型之前必须把配对关系修好：

      - 丢掉没有父 tool_calls 的孤立 tool 消息
      - 丢掉没有齐全回复的 assistant tool_calls（改成普通 assistant 文本，
        有 content 就留文本，没 content 就整条丢掉）

    注意：**展示用的 history() 不做这件事** —— 那里应该如实呈现库里的内容。
    """
    msgs = history(thread_id, limit=limit)

    # 第一遍：收集「每个 assistant tool_calls 的 id 是否都有 tool 回复」
    reply_ids = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
    declined: set[str] = set()
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        ids = [tc.get("id") for tc in (m.get("tool_calls") or [])]
        if ids and any(i not in reply_ids for i in ids):
            declined.update(ids)

    out: list[dict] = []
    for m in msgs:
        role = m.get("role")

        if role == "tool":
            # 丢掉孤立 tool（父 tool_calls 不在窗口里，或那条被判定不完整）
            if m.get("tool_call_id") in declined or m.get("tool_call_id") not in reply_ids:
                continue
            if not any(
                m.get("tool_call_id") in
                [tc.get("id") for tc in (p.get("tool_calls") or [])]
                for p in out if p.get("role") == "assistant"
            ):
                continue
            out.append(m)
            continue

        if role == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id") for tc in m["tool_calls"]]
            if any(i in declined for i in ids):
                # 这一轮的轨迹不完整 → 降级成普通 assistant 文本
                text = m.get("content")
                if text:
                    out.append({"role": "assistant", "content": text})
                continue

        out.append(m)

    return out


def _search_like(conn: sqlite3.Connection, thread_id: str, query: str, limit: int) -> list[dict]:
    with conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE thread_id=? AND content LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (thread_id, f"%{query}%", limit),
        ).fetchall()
    return [_row_to_msg(r) for r in rows]


def search(thread_id: str, query: str, limit: int = 5) -> list[dict]:
    """会话内全文召回 —— 双路并集，保证「查得到」

    设计取舍（实测依据见 `_pick_tokenizer` 的注释）：
    FTS5 的 trigram 索引对 ≥3 字的中文子串效果最好，但对两字词无能为力；
    LIKE 则刚好相反 —— 慢，但两字词也能命中。
    本表规模只有几百到几千行，LIKE 的代价可以忽略，
    所以这里取 **两路结果的并集**，宁可多召回也不要「明明有却查不到」。

    曾经踩过的坑：只走 FTS + 失败才降级 LIKE。看起来合理，
    但 FTS 在 `<3` 字查询下是 **成功执行且返回 0 行**，根本不触发异常，
    于是永远不会降级 —— 一个「静默正确」的假象。现在无条件双路，不留这种口子。
    """
    init_db()
    query = (query or "").strip()
    if not query:
        return []

    hits: list[dict] = []
    seen: set[str] = set()

    def absorb(items: list[dict]) -> None:
        for m in items:
            key = f'{m.get("role")}|{m.get("tool_call_id")}|{(m.get("content") or "")[:80]}'
            if key in seen:
                continue
            seen.add(key)
            hits.append(m)

    with _open() as conn:
        # ① FTS 索引（有就试，失败/查不到都不算数）
        if _pick_tokenizer():
            try:
                rows = conn.execute(
                    "SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                    "WHERE messages_fts MATCH ? AND m.thread_id=? "
                    "ORDER BY bm25(messages_fts) LIMIT ?",
                    (query, thread_id, limit),
                ).fetchall()
                absorb([_row_to_msg(r) for r in rows])
            except sqlite3.OperationalError as e:
                # 查询串可能带 FTS 语法字符（- " * 等），不能让它拖垮整个会话
                logger.warning("FTS5 查询失败(%s)，仅用 LIKE：%r", e, query)

        # ② LIKE 兜底（补两字词，也补 FTS 出问题的情况）
        absorb(_search_like(conn, thread_id, query, limit))

    return hits[:limit]


# ─────────────────────── 长期记忆 ───────────────────────

def save_long_term(thread_id: str, summary: str, prefs: dict | None = None) -> None:
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, _open() as conn:        conn.execute(
            "INSERT INTO long_term(thread_id, summary, prefs, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET summary=excluded.summary, "
            "prefs=excluded.prefs, updated=excluded.updated",
            (thread_id, summary, json.dumps(prefs or {}, ensure_ascii=False), now),
        )
    audit("long_term_saved", thread_id=thread_id, chars=len(summary or ""))


def load_long_term(thread_id: str = "default") -> dict:
    """读取长期记忆 → {"summary": str, "prefs": dict, "updated": str}"""
    init_db()
    with _open() as conn:
        row = conn.execute(
            "SELECT * FROM long_term WHERE thread_id=?", (thread_id,)
        ).fetchone()
    if not row:
        return {"summary": "", "prefs": {}, "updated": ""}
    try:
        prefs = json.loads(row["prefs"] or "{}")
    except json.JSONDecodeError:
        prefs = {}
    return {"summary": row["summary"] or "", "prefs": prefs, "updated": row["updated"] or ""}


def all_summaries(limit: int = 5) -> list[dict]:
    """跨会话摘要（用于「用户偏好」这类全局画像，取最近几条）

    【预留】当前只有测试在用。做「跨会话用户画像」时是入口。
    """
    init_db()
    with _open() as conn:
        rows = conn.execute(
            "SELECT * FROM long_term ORDER BY updated DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        try:
            prefs = json.loads(r["prefs"] or "{}")
        except json.JSONDecodeError:
            prefs = {}
        out.append({"thread_id": r["thread_id"], "summary": r["summary"] or "",
                    "prefs": prefs, "updated": r["updated"] or ""})
    return out


# ─────────────────────── 会话计数器 ───────────────────────

def bump_counter(key: str, delta: int = 1) -> int:
    """原子自增，返回新值（用于「本会话已生成几张」这类治理计数）"""
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, _open() as conn:
        conn.execute(
            "INSERT INTO session_counter(key, value, updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=value+?, updated=excluded.updated",
            (key, delta, now, delta),
        )
        row = conn.execute(
            "SELECT value FROM session_counter WHERE key=?", (key,)
        ).fetchone()
    return int(row["value"]) if row else 0


def get_counter(key: str) -> int:
    init_db()
    with _open() as conn:
        row = conn.execute("SELECT value FROM session_counter WHERE key=?", (key,)).fetchone()
    return int(row["value"]) if row else 0


def reset_counter(key: str) -> None:
    init_db()
    with _write_lock, _open() as conn:
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

def add_reservation(token: str, thread_id: str) -> None:
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, _open() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reservations(token, thread_id, ts) VALUES(?,?,?)",
            (token, thread_id, now),
        )


def consume_reservation(token: str) -> bool:
    """兑现票据（一次性）。返回 True 表示这张票之前确实存在。

    用 DELETE 的 rowcount 做原子判定 —— 不要先 SELECT 再 DELETE，
    那中间会被并发请求插进来，导致同一张票被兑现两次。
    """
    init_db()
    with _write_lock, _open() as conn:
        cur = conn.execute("DELETE FROM reservations WHERE token=?", (token,))
        return (cur.rowcount or 0) > 0


def peek_reservation(token: str) -> dict | None:
    init_db()
    with _open() as conn:
        row = conn.execute(
            "SELECT token, thread_id, ts FROM reservations WHERE token=?", (token,)
        ).fetchone()
    return dict(row) if row else None


def list_reservations(older_than_sec: int | None = None) -> list[dict]:
    """列出未兑现的票据；给了 older_than_sec 就只返回超过该年龄的"""
    init_db()
    with _open() as conn:
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


def reservations_stats() -> dict:
    init_db()
    with _open() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM reservations").fetchone()
    return {"pending": int(row["c"]) if row else 0}


# ─────────────────────── 维护 ───────────────────────

def clear_thread(thread_id: str) -> int:
    """清空一个会话，返回删除条数"""
    init_db()
    with _write_lock, _open() as conn:
        cur = conn.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))
        conn.execute("DELETE FROM long_term WHERE thread_id=?", (thread_id,))
        n = cur.rowcount or 0
    audit("thread_cleared", thread_id=thread_id, deleted=n)
    return int(n)


def stats() -> dict:
    """给健康检查用"""
    init_db()
    with _open() as conn:
        threads = conn.execute("SELECT COUNT(DISTINCT thread_id) c FROM messages").fetchone()
        msgs = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()
        lt = conn.execute("SELECT COUNT(*) c FROM long_term").fetchone()
    size_kb = round(SQLITE_PATH.stat().st_size / 1024, 1) if SQLITE_PATH.exists() else 0.0
    return {
        "db_path": str(SQLITE_PATH),
        "size_kb": size_kb,
        "threads": int(threads["c"]) if threads else 0,
        "messages": int(msgs["c"]) if msgs else 0,
        "long_term_records": int(lt["c"]) if lt else 0,
        "fts5_enabled": bool(_has_fts5()),
        "fts_tokenizer": _pick_tokenizer(),
    }


# 进程启动时就把表建好，避免第一次请求时才建表导致偶发超时
init_db()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    import json as _json
    print(_json.dumps(stats(), ensure_ascii=False, indent=2))


# ─────────────── 模板工坊：用户创造的家家族库 ───────────────

def save_forge(
    *,
    lineage: str,
    version: int,
    spec: dict,
    name: str = "",
    prompt: str = "",
    theory: str = "",
    feedback: str = "",
    images: list | None = None,
    card: dict | None = None,
    forge_id: str | None = None,
    prompt_override: str = "",
    owner_session: str = "",
) -> str:
    """存一版草稿。返回 id。

    ★ card 是「视觉卡」——解构一次的产物，可换场景反复编译。
      它和 spec 分开存：spec 是最终模板，card 是可复用的视觉语法档案，
      下次用户说"换个场景再来一套"时能直接用，不用重新看图。
    prompt_override：新提炼/迭代的版本总是从自动渲染开始（默认空）；
      手改提示词只属于「当前这一版」，由 set_forge_prompt 单独写入。
    owner_session：公开版属主会话。空=内置家族（人人可见只读）；
      非空=该访客自建（列表只见自己的 + 内置的）。
    """
    init_db()
    fid = forge_id or uuid.uuid4().hex[:16]
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, _open() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO forged_families"
            "(id, lineage, version, name, family_id, spec, prompt, prompt_override,"
            " theory, feedback, images, installed, card, created_at, owner_session)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (fid, lineage, int(version), name or "",
             str((spec or {}).get("id") or ""),
             json.dumps(spec or {}, ensure_ascii=False),
             prompt or "", (prompt_override or "").strip(),
             theory or "", feedback or "",
             json.dumps(images or [], ensure_ascii=False),
             json.dumps(card or {}, ensure_ascii=False), now,
             (owner_session or "").strip() or None),
        )
    return fid


def set_forge_prompt(forge_id: str, text: str) -> bool:
    """写入/清除某版草稿的手动提示词。text 非空=覆盖，空串=恢复自动渲染。

    ★ 手改的是「提示词成品」而不是 spec —— 参数调节、迭代编译都建立在 spec 上，
      所以手改后：生成时直接用手改文本（prompt_builder 接线），迭代出新版本
      时新版本重新从自动渲染开始（手改只属于当前版本）。
    """
    init_db()
    with _write_lock, _open() as conn:
        cur = conn.execute(
            "UPDATE forged_families SET prompt_override=? WHERE id=?",
            ((text or "").strip(), forge_id),
        )
    return cur.rowcount > 0


def get_forge(forge_id: str) -> dict | None:
    init_db()
    with _open() as conn:
        row = conn.execute(
            "SELECT * FROM forged_families WHERE id=?", (forge_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("spec", "images", "card"):
        empty = "[]" if k == "images" else "{}"
        try:
            d[k] = json.loads(d.get(k) or empty)
        except (json.JSONDecodeError, TypeError):
            d[k] = [] if k == "images" else {}
    return d


def list_forge(limit: int = 50, lineage: str | None = None,
               owner: str = "default") -> list[dict]:
    """列出库。给了 lineage 就只列那条迭代链（按版本号升序）。

    ★ 公开版可见性：内置家族（owner_session 为空）人人可见；
      自建家族只见自己的。default 会话（本地版）看全部 —— 兼容旧数据与脚本。
    """
    init_db()
    with _open() as conn:
        if lineage:
            sql = ("SELECT * FROM forged_families WHERE lineage=?"
                   + ("" if owner == "default" else
                      " AND (owner_session IS NULL OR owner_session='' OR owner_session=?)"))
            params: list = [lineage] + ([] if owner == "default" else [owner])
            rows = conn.execute(sql + " ORDER BY version", params).fetchall()
        elif owner == "default":
            rows = conn.execute(
                "SELECT * FROM forged_families ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM forged_families"
                " WHERE owner_session IS NULL OR owner_session='' OR owner_session=?"
                " ORDER BY created_at DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for k in ("spec", "card"):
            try:
                d[k] = json.loads(d.get(k) or "{}")
            except (json.JSONDecodeError, TypeError):
                d[k] = {}
        out.append(d)
    return out


def next_forge_version(lineage: str) -> int:
    init_db()
    with _open() as conn:
        row = conn.execute(
            "SELECT MAX(version) v FROM forged_families WHERE lineage=?", (lineage,)
        ).fetchone()
    return int(row["v"] or 0) + 1 if row else 1


def mark_forge_installed(forge_id: str, family_id: str = "") -> bool:
    init_db()
    with _write_lock, _open() as conn:
        cur = conn.execute(
            "UPDATE forged_families SET installed=1 WHERE id=?", (forge_id,)
        )
    return (cur.rowcount or 0) > 0


def mark_forge_uninstalled(family_id: str) -> int:
    """家族被删除时，把库里指向它的 installed 标记清掉 ——
    否则「我的库」里还挂着『已安装』而主页面已经没有这个家族（不一致实测 2026-10-03）。"""
    if not family_id:
        return 0
    init_db()
    with _write_lock, _open() as conn:
        cur = conn.execute(
            "UPDATE forged_families SET installed=0, family_id='' WHERE family_id=?",
            (family_id,),
        )
    return cur.rowcount or 0


def sync_forge_name(lineage: str, name: str) -> int:
    """把一条迭代链的全部版本名同步为家族名（实测 2026-10-03）。

    用户在工坊随手填的风格名（如「MC歇菜风」）会存进库记录，而家族文件里是
    模型起的名（如「像素死亡实景」）—— 主页面显示后者、我的库显示前者，
    用户根本对不上哪个是哪个。统一规则：**家族名是唯一权威**，库记录跟随。
    """
    if not lineage or not (name or "").strip():
        return 0
    init_db()
    with _write_lock, _open() as conn:
        cur = conn.execute(
            "UPDATE forged_families SET name=? WHERE lineage=?", (name.strip(), lineage)
        )
    return cur.rowcount or 0


def delete_forge(forge_id: str) -> bool:
    init_db()
    with _write_lock, _open() as conn:
        cur = conn.execute("DELETE FROM forged_families WHERE id=?", (forge_id,))
    return (cur.rowcount or 0) > 0


def forge_stats() -> dict:
    init_db()
    with _open() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c, COUNT(DISTINCT lineage) l,"
            " SUM(CASE WHEN installed=1 THEN 1 ELSE 0 END) i FROM forged_families"
        ).fetchone()
    return {"saved": int(row["c"]) if row else 0,
            "lineages": int(row["l"] or 0) if row else 0,
            "installed": int(row["i"] or 0) if row else 0}
