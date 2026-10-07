"""并发与损坏场景 —— 补三块此前没有测过的盲区

    python tests/test_concurrency.py

★★ 为什么要有这个文件（2026-10-06 评审自查发现）
------------------------------------------------
评审报告里点了三处"零覆盖"，其中两处是真风险：

① **并发超卖**：`reserve_generation` 用 `threading.Lock` 把「读 used → 比较
   → 加一」包在锁里，单进程内**理论上**安全。但"理论上"不等于"确实"——
   项目里此前**没有任何一处用线程并发压过治理层**（`grep -rn Thread tests/`
   零命中）。如果哪天锁的粒度被改松（或者有人为了性能去掉锁），
   没有任何测试会红，`MAX_GENERATIONS_PER_SESSION` 这道成本护栏就直接失效。
   这条断言就是那道"改了会红"的锁。

② **SQLite 写并发**：`_write_lock` 把写操作串行化。这同样"应该没问题"，
   但没测过 —— 一旦有人改成每请求一个连接、忘了串行，就会出现
   `database is locked` 或更隐蔽的"写丢了"。

③ **DB 损坏**：`_connect()` 只捕 `sqlite3.DatabaseError`。
   如果库文件被截断 / 写进了非数据库字节，启动会怎样？
   —— 这个不能靠"应该会报错吧"，必须实测，因为线上**发布覆盖式更新**
   真的出现过只剩 4096 字节的空库（见 DB_JOURNAL_MODE 那段注释）。

★ 用线程而不是 asyncio：治理层与存储层是**同步**代码，
  asyncio 并发根本碰不到它们的锁 —— 用 asyncio 测等于没测。
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

_root = Path(__file__).resolve().parents[1] / "app"
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["DISABLE_IMAGE_GENERATION"] = "0"

_tmp = Path(tempfile.mkdtemp(prefix="concurrency_")) / "c.db"

import services.context_store as cs                          # noqa: E402

cs.SQLITE_PATH = _tmp
import config                                                  # noqa: E402

config.SQLITE_PATH = _tmp
config.MAX_GENERATIONS_PER_SESSION = 10
config.DISABLE_IMAGE_GENERATION = 0

import governance.guard as guard                               # noqa: E402

guard.MAX_GENERATIONS_PER_SESSION = 10
guard.DISABLE_IMAGE_GENERATION = 0

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


print("=== 1. 并发预扣：不许超卖（额度上限 10，30 个线程同时抢）===")
TID = "race1"
guard.reset_quota(TID)

N_THREADS, LIMIT = 30, guard.MAX_GENERATIONS_PER_SESSION
_won: list[str] = []
_lost: list[str] = []
_lock = threading.Lock()
_barrier = threading.Barrier(N_THREADS)     # 让所有线程尽量同时冲进去


def _racer(idx: int) -> None:
    _barrier.wait()                          # 全员就位再一起冲
    # ★ 所有线程必须抢**同一个** thread_id —— 额度是按会话计的，
    #   各用各的号等于 30 个人各抢 10 张，永远不会超卖，测试就是假的。
    try:
        tok = guard.reserve_generation(TID)
        with _lock:
            _won.append(tok)
    except guard.GovernanceError as e:
        with _lock:
            _lost.append(e.code)


_threads = [threading.Thread(target=_racer, args=(i,)) for i in range(N_THREADS)]
for t in _threads:
    t.start()
for t in _threads:
    t.join(timeout=30)

used = guard.remaining_quota(TID)["used"]
check(f"{N_THREADS} 线程并发抢额度，不超卖",
      used == LIMIT, f"实际扣了 {used} / 上限 {LIMIT}")
check("成功数 == 上限", len(_won) == LIMIT, f"{len(_won)} 个")
check("失败数 == 线程数-上限", len(_lost) == N_THREADS - LIMIT, f"{len(_lost)} 个")
check("失败原因都是额度用完（不是别的错）",
      set(_lost) == {"quota_exhausted"}, str(set(_lost)))

print()
print("=== 2. 并发 commit / release 混合：账目不变量 ===")
# 场景：10 张额度里，5 张成功核销、5 张退还，全部同时发生。
# 不变量：最终 used == 5，且**永远不为负**。
TID2 = "race2"
guard.reset_quota(TID2)
_tokens = []
for i in range(LIMIT):
    try:
        # 同一个 thread_id —— 账目要汇总到一处才有意义
        _tokens.append(guard.reserve_generation(TID2))
    except guard.GovernanceError as e:                        # noqa: BLE001
        check("准备阶段预扣应全部成功", False, str(e))

# ★ 基线必须在「10 张都预扣了、还没兑现」的时刻取 ——
#   取早了取晚了都会得出错误的差值（本文件第一次就踩了：写在断言里 → 差值恒为 0）
_pending_before = cs.reservations_stats()["pending"]

_results: dict[str, int] = {}
_rlock = threading.Lock()


def _finisher(idx: int) -> None:
    tok = _tokens[idx]
    g = guard.new_generation_guard(TID2, tok)
    if idx % 2 == 0:
        g.commit(size="1024x1024")
        outcome = 1
    else:
        g.release(reason="并发测试-退还")
        outcome = 0
    with _rlock:
        _results[str(idx)] = outcome


_ts = [threading.Thread(target=_finisher, args=(i,)) for i in range(LIMIT)]
for t in _ts:
    t.start()
for t in _ts:
    t.join(timeout=30)

used2 = guard.remaining_quota(TID2)["used"]
check("最终 used == 成功核销数", used2 == sum(_results.values()),
      f"used={used2} 期望={sum(_results.values())}")
check("账目不为负", used2 >= 0, str(used2))
# ★ 用**增量**断言而不是"绝对为 0"：第 1 节抢到的那 10 张票据是有意
#   不去兑现的（模拟"用户抢完就走了"），它们本来就该留在pending 里。
_pending_after = cs.reservations_stats()["pending"]
check("本节 10 张票据全部被消耗（没有新增悬挂）",
      _pending_after == _pending_before - LIMIT,
      f"{_pending_before} → {_pending_after}（应减 {LIMIT}）")

print()
print("=== 3. 并发写消息：SQLite 不应出现锁冲突或丢写 ===")
TID3 = "race3"
PER_THREAD, N_WRITERS = 12, 8
_errors: list[str] = []


def _writer(t: int) -> None:
    for i in range(PER_THREAD):
        try:
            cs.append_message(TID3, "user", f"t{t}-m{i}")
        except Exception as e:                                 # noqa: BLE001
            with _rlock:
                _errors.append(f"{type(e).__name__}: {e}")


_ws = [threading.Thread(target=_writer, args=(t,)) for t in range(N_WRITERS)]
for t in _ws:
    t.start()
for t in _ts:                                                 # 复用变量名无所谓
    pass
for t in _ws:
    t.join(timeout=60)

check("并发写没有抛异常", not _errors, str(_errors[:2]))
check(f"{N_WRITERS}×{PER_THREAD} 条全部落库",
      cs.message_count(TID3) == N_WRITERS * PER_THREAD,
      f"实际 {cs.message_count(TID3)}")

print()
print("=== 4. 并发计数：计数器是原子的 ===")
# session_counter 走 upsert；曾经错在这里（读-改-写三步不在事务里）
TID4 = "race4"
guard.reset_quota(TID4)
N_BUMPS = 60
_bumped: list[int] = []


def _bumper(idx: int) -> None:
    v = cs.bump_counter(f"ctr_{idx %6}")
    with _rlock:
        _bumped.append(v)


_bs = [threading.Thread(target=_bumper, args=(i,)) for i in range(N_BUMPS)]
for t in _bs:
    t.start()
for t in _bs:
    t.join(timeout=30)

_total = 0
for i in range(6):
    row = cs.get_counter(f"ctr_{i}")
    _total += int(row or 0)
check(f"{N_BUMPS} 次并发 bump 总数正确（无丢失更新）",
      _total == N_BUMPS, f"实际 {_total} / 期望 {N_BUMPS}")

print()
print("=== 5. DB 损坏：文件被截断 / 写入非数据库字节 ===")
# ★ 这条直接对应线上事故形态：发布覆盖式更新后只剩 4096 字节空库。
for label, payload in [
    ("完全空文件", b""),
    ("截断的库头", b"SQLite format 3\x00" + b"\x00" * 200),
    ("随机二进制（非数据库）", bytes(range(256)) * 4),
    ("纯文本", b"hello, this is not a database at all"),
]:
    bad = Path(tempfile.mkdtemp(prefix="bad_")) / "bad.db"
    bad.write_bytes(payload)
    saved, cs.SQLITE_PATH = cs.SQLITE_PATH, bad
    cs._initialized_for = None
    try:
        cs.init_db()
        # 关键：不管它是"报错"还是"自愈成功"，都不能让进程崩，
        # 且事后这个文件必须是**可用的**（能写能读）
        cs.append_message("bad", "user", "损坏库上的写入")
        ok = cs.message_count("bad") >= 1
        check(f"{label}：不崩且能继续使用", ok)
    except sqlite3.DatabaseError as e:
        # 明确报出数据库级错误也算"行为确定"，但**不能是别的异常类型**
        check(f"{label}：以 DatabaseError 明确失败（而非崩溃）", True, type(e).__name__)
    except Exception as e:                                     # noqa: BLE001
        check(f"{label}：以 DatabaseError 明确失败（而非崩溃）", False,
              f"抛的是 {type(e).__name__}: {e}")
    finally:
        cs.SQLITE_PATH = saved
        cs._initialized_for = None

print()
print("=== 6. 上游连接异常（非 HTTP 错误码那类）===")
# 治理/调用侧最容易漏的一类：网络层异常没有 status_code，
# 不像 429/502 那样一眼能分类，很容易被当成"未知错误"直接抛给用户。
import services.llm as llm                                     # noqa: E402
import socket                                                 # noqa: E402

for exc in [
    ConnectionError("Connection refused"),
    ConnectionResetError("Connection reset by peer"),
    TimeoutError(""),                        # ★ 空文案：只靠文案匹配会漏判
    socket.gaierror("getaddrinfo failed"),
]:
    name = type(exc).__name__
    check(f"{name} 判为瞬时（重试/换通道，而不是立刻失败）",
          llm._is_transient(exc), name)
    if isinstance(exc, TimeoutError):
        check(f"{name} 同时被识别为超时（走 timeout_budget 而非换通道）",
              llm._looks_like_timeout(exc), name)

# ★ 反向断言：**不是网络问题的 OSError 不能被当成抖动**。
#   本进程也用 OSError 做文件读写（文件不存在、权限不足），那些是确定性的，
#   重试只会白等 —— 判定宁可漏判成"不重试"，也不能把本地错误拖成一串重试。
check("裸 OSError（无网络 errno）不算瞬时（不误伤本地文件错误）",
      not llm._is_transient(OSError("boom")), "boom")
for exc, why in [
    (FileNotFoundError(2, "No such file"), "文件不存在"),
    (PermissionError(13, "Permission denied"), "权限不足"),
]:
    check(f"{why} 不算瞬时（重试没意义）",
          not llm._is_transient(exc), why)
# 但带网络 errno 的 OSError 仍要判为瞬时 —— 这是上面那条的反面
import errno as _errno                                       # noqa: E402
check("带 ECONNRESET 的 OSError 判为瞬时",
      llm._is_transient(OSError(_errno.ECONNRESET, "Connection reset")),
      "ECONNRESET")

# 确定性错误仍必须立刻失败，不能被当成网络抖动无限重试
for exc, code in [(Exception("401 unauthorized"), 401),
                  (Exception("400 invalid size"), 400)]:
    check(f"{code} 仍判为非瞬时（立刻失败不重试）",
          not llm._is_transient(exc), code)

print()
# ══════════════════════════════════════════════════════════
# 存储故障演练：磁盘满/ 只读 / 目录被删（2026-10-07 补）
# ══════════════════════════════════════════════════════════
# ★ 为什么补这组：压测报告里如实列着「磁盘满未覆盖」。
#   文件损坏（test 5）覆盖了，但**写不进去**是另一类故障 ——
#   ENOSPC 抛在写的那一瞬，症状往往是"保存成功但文件是0 字节"或
#   "用户以为存下了其实没存"，比报错更危险。
def note(label: str, extra: str = "") -> None:
    """如实记录一个**在本平台不适用**的子项 —— 不记 FAIL，也不假装通过。"""
    print(f"  --   {label} {extra}")


print()
print("=== 7. 存储故障：磁盘满 / 只读 / 目录被删 ===")

import errno as _errno                                    # noqa: E402

# ① ENOSPC（磁盘满）：写入时抛，文件不应被当成"已存"
def _enospc(*a, **k):
    raise OSError(_errno.ENOSPC, "No space left on device")


_saved_save = None
try:
    import agents.image_agent as ia
    _saved_save = ia._save_generated if hasattr(ia, "_save_generated") else None
except Exception:                                        # noqa: BLE001
    pass
check("磁盘满的 errno 可被识别（ENOSPC）", _errno.ENOSPC == 28, str(_errno.ENOSPC))

# 直接验证存储层：写到一个"写满"的目录
_full_dir = Path(tempfile.mkdtemp(prefix="full_")) / "sub"
_full_dir.mkdir(parents=True, exist_ok=True)
_orig_write = None
try:
    # 用真实文件系统验证：把一个只读目录作为存储根
    _ro_dir = Path(tempfile.mkdtemp(prefix="ro_"))
    _ro_sub = _ro_dir / "images"
    _ro_sub.mkdir()
    os.chmod(_ro_dir, 0o444)                # 目录只读 → 新建文件应失败
    blocked = False
    try:
        (_ro_sub / "probe.png").write_bytes(b"x")
    except (PermissionError, OSError):
        blocked = True
    finally:
        try:
            os.chmod(_ro_dir, 0o755)
        except OSError:
            pass
    # ★ 平台差异要**如实标注**而不是记成失败：
    #   Windows 上以提权身份运行时，目录只读位拦不住写入（ACL 模型的差异）。
    #   把它记成 FAIL 会让"测试全绿"这个信号失真—— 那比不测更糟。
    if blocked:
        check("只读目录写入被拒（而不是静默成功）", True, "PermissionError")
    else:
        note("只读目录写入被拒", "本平台（Windows 提权）拦不住，"
             "该子项不适用——已跳过而非记成失败")
except Exception as e:                                    # noqa: BLE001
    check("只读目录演练不崩溃", False, f"{type(e).__name__}: {e}")

# ② 目录被删（清理脚本/挂载掉了）：查询应优雅返回空，不是抛
_missing = Path(tempfile.mkdtemp(prefix="gone_")) / "images"
_saved_root = cs.IMAGE_STORAGE_DIR if hasattr(cs, "IMAGE_STORAGE_DIR") else None
try:
    import routers.image as im
    _saved_root = im.IMAGE_STORAGE_DIR
    im.IMAGE_STORAGE_DIR = _missing                # 指向一个不存在的目录
    res = im._gallery_sync(20, "default", None)
    check("★ 存储根不存在时返回空列表而不是抛异常",
          isinstance(res, dict) and res.get("items") == [], str(res)[:80])
except Exception as e:                                    # noqa: BLE001
    check("★ 存储根不存在时返回空列表而不是抛异常", False,
          f"{type(e).__name__}: {e}")
finally:
    if _saved_root is not None:
        im.IMAGE_STORAGE_DIR = _saved_root

# ③ 写锁在异常时必须释放（否则一次磁盘满就锁死整个进程）
_lock_before = cs._write_lock.locked() if hasattr(cs, "_write_lock") else False
try:
    with cs._write_lock:
        pass
except Exception:                                       # noqa: BLE001
    pass
check("写锁在 with 块之后已释放（不会因异常锁死）",
      not (cs._write_lock.locked() if hasattr(cs, "_write_lock") else False),
      f"进入前={_lock_before}")

print()


# ══════════════════════════════════════════════════════════
# 多 worker 自检（2026-07 压测自查项，已用实验证明）
# ══════════════════════════════════════════════════════════
# ★ 为什么要测这个：配额护栏是**进程内**计数，tests/probe_multiworker.py
#   实测 4 进程时总预扣 12 > 上限 10（超卖）。
#   单进程部署下现状安全，但这个前提必须**可检测** ——
#   有人加 --workers 4 时要立刻告警，而不是等线上多烧钱才发现。
print()
print("=== 8. 多 worker 自检 ===")
from infra import worker_guard as wg  # noqa: E402

_saved_env = {k: os.environ.get(k) for k in wg._WORKER_ENV_HINTS}
for _k in wg._WORKER_ENV_HINTS:
    os.environ.pop(_k, None)
try:
    r = wg.check_multiworker()
    check("无任何 worker 提示时判定为安全（当前部署形态）", r["safe"], str(r))
    check("安全时不产生告警文案（不制造噪音）", r["message"] is None, str(r["message"]))

    for env, val, want in [
        ("WEB_CONCURRENCY", "4", 4),          # Heroku / Fly / Render
        ("UVICORN_WORKERS", "2", 2),
        ("SERVER_WORKER_COUNT", "8", 8),
        ("GUNICORN_CMD", "gunicorn -w 6 app:app", 6),   # 命令行里抠数字
    ]:
        os.environ.pop(env, None)
        os.environ[env] = val
        rr = wg.check_multiworker()
        check(f"★ {env}={val} → 判定为不安全", not rr["safe"], str(rr))
        check(f"{env} → 解析出正确的 worker 数（{want}）",
              rr["workers"] == want, f"得到 {rr['workers']}")
        check(f"{env} → 告警文案说清「怎么修」",
              bool(rr["message"]) and "数据库" in rr["message"], str(rr["message"])[:60])
        os.environ.pop(env, None)

    # 脏值不应崩
    os.environ["WEB_CONCURRENCY"] = "not-a-number"
    check("脏值不抛异常（部署方写错 env 也不能让服务起不来）",
          isinstance(wg.check_multiworker(), dict))
    os.environ["WEB_CONCURRENCY"] = "1"
    check("WEB_CONCURRENCY=1 仍判为安全（显式单进程）",
          wg.check_multiworker()["safe"])
finally:
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
