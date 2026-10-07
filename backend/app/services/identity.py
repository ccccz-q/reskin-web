"""身份层 · 匿名会话 + 访客码 + 可选账号 —— 公开版多用户隔离的唯一真源

════════ 设计契约（发布方案计划书 v1.2）════════

三层身份，一个隔离键：

1. **匿名访客（默认）**：前端首访生成 UUID 存 localStorage，所有请求带
   `X-Session-Id` 头。后端从 header 解析；缺失/非法回退 "default"（本地
   开发与旧前端兼容）。对话/习惯/额度本就按 thread_id 隔离——换了取值
   来源即完成隔离，表结构零迁移。

2. **6 位访客码（兜底找回）**：每个会话懒生成一个访客码（剔除易混淆
   字符 I/L/O/0/1），显示给用户抄存。换设备/清缓存后凭码恢复会话，
   找回全部数据。恢复接口限速防猜码。

3. **账号（自愿绑定）**：任意时刻设用户名+密码绑定当前会话；登录成功
   签发 HMAC 令牌，令牌即会话凭证。忘记密码由管理员后台重置。

**安全红线**：session_id 会成为图片目录的路径段——必须过严格白名单
（十六进制 + 连字符，8–64 位），任何非法输入一律回退 "default"，
绝不进文件系统。访客码/密码校验接口共享滑动窗口限速。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time

from starlette.requests import Request

from infra.logging import audit, logger

# ── 会话 ID 白名单 ─────────────────────────────────────────────
# 前端用 crypto.randomUUID()（36 位带连字符）；也接受纯 hex 短形式。
# 路径安全：这个值会被用作 images/<session>/ 的目录名，白名单外的
# 一律回退 "default"——绝不让任意字符串进文件系统。
_SESSION_RE = re.compile(r"^[0-9a-f][0-9a-f-]{7,63}$", re.IGNORECASE)
DEFAULT_SESSION = "default"
SESSION_HEADER = "X-Session-Id"
TOKEN_HEADER = "X-Auth-Token"
# ★ 取图为什么必须有一条 Cookie 通道：`<img src>` 发不出自定义请求头，
#   前端那个 X-Session-Id 对图片请求一次也带不上。浏览器只对**同站请求**
#   自动携带 Cookie —— 所以把它并行一份到 Cookie，画像才能在不改任何
#   前端代码的前提下认出访客（详见 services/image_access.may_read）。
#   值是标准的登录令牌（sid.exp.sig，HMAC 签名），伪造不了、也会过期。
SESSION_COOKIE = "tv_session"


def _public_mode() -> bool:
    """运行期读开关（不放模块级常量：本地 import 后 monkeypatch 才改得动）"""
    from config import PUBLIC_MODE
    return PUBLIC_MODE


def is_anonymous_fallback(sid: str) -> bool:
    """这个隔离键，究竟是「某个访客」，还是「没能认出访客」？

    ★ 为什么要区分（2026-10-03 线上实测的血案）：
      `default` 在**本地/内网**是唯一用户的合理取值，扫全目录正是期望行为。
      但在**公开版**里，任何没带 X-Session-Id 的请求都会落到它——爬虫、
      手敲 curl、清理过 localStorage、尚在首帧的浏览器——那是"身份未知"，
      不是"共享身份"。若照旧给它全目录，一次 gallery 就把所有访客的上传图
      连同 URL 一起列给陌生人（实测：无偿列出 7 个会话 13 张图）。

      消费方向只有两条，且必须同口径：
        · 读：只给公共展示图（见 routers/image._gallery_sync）
        · 写：直接拒绝，而不是写进一个谁也看不到的共享空间
    """
    return sid == DEFAULT_SESSION and _public_mode()


def _clean_session(raw: str | None) -> str:
    s = (raw or "").strip()
    if not s:
        return DEFAULT_SESSION
    if _SESSION_RE.match(s) and "--" not in s and not s.endswith("-"):
        return s.lower()
    return DEFAULT_SESSION


def resolve_session(request: Request) -> str:
    """请求 → 隔离键。

    优先级：登录令牌 > X-Session-Id > **会话 Cookie** > default。

    ★ Cookie 这一层是为图片请求加的（见 SESSION_COOKIE 注释）：
      `<img>`、`<a download>`、预加载器都发不出自定义头，只有 Cookie 会自动带上。
      少了它，取图路由会把自家用户当成陌生人，页面上一张图都出不来。
    """
    tok = request.headers.get(TOKEN_HEADER, "")
    if tok:
        sid = verify_token(tok)
        if sid:
            return sid
    explicit = _clean_session(request.headers.get(SESSION_HEADER))
    if explicit != DEFAULT_SESSION:
        return explicit
    try:
        cookie = request.cookies.get(SESSION_COOKIE, "")
    except Exception:                                     # noqa: BLE001
        cookie = ""
    if cookie:
        sid = verify_token(cookie)
        if sid:
            return sid
    return DEFAULT_SESSION


def session_dep(request: Request) -> str:
    """FastAPI 依赖：路由里 `sid: str = Depends(session_dep)` 即得隔离键

    中间件已经算过一遍并塞进 scope，这里直接复用 —— 省一次令牌验签，
    也保证「中间件决定 Cookie 的那个人」与「路由看到的那个人」是同一个人。
    直接单元测试依赖函数时没有中间件，走原路径。
    """
    cached = request.scope.get("sid")
    return cached if isinstance(cached, str) and cached else resolve_session(request)


# ── 额度账本键（与历史键刻意分开）───────────────────────────────
#
# ★ 为什么必须分开（2026-10-07 评审自查发现的真洞）
#   `merge_thread` 在「没有 X-Session-Id」时会回退到**用户可控的 thread_id**。
#   这对**历史隔离**没问题（它只是命名空间），但如果**配额账本也用它**，
#   匿名请求换一个随机的 thread_id 就能拿到一份全新额度 ——
#   MAX_GENERATIONS_PER_SESSION 这道成本护栏等于形同虚设，
#   而它恰恰是保护 API 花费的那道闸。
#
#   所以引入独立的 quota_key：
#     · 有身份 → 用 sid（不可伪造，因为要凭空猜中别人的 128 位 UUID）
#     · 无身份 → 用**服务端推导的客户端指纹**（IP + UA 的哈希），
#       用户能伪造 thread_id，但伪造不了自己连上来的 IP。
#   代价（诚实记录）：同一 NAT 出口后的多个访客会共享配额。
#   这是"防绕过"与"公平计量"之间的取舍，我选了前者 ——
#   护栏失效是**真金白银**的损失，而 NAT 共享只影响极端部署形态。
def quota_key(explicit: str | None, sid: str, request) -> str:
    """额度账本键。**不要**用 merge_thread 的结果当配额键（见上方注释）。"""
    if sid != DEFAULT_SESSION:
        return sid
    try:
        from starlette.requests import Request  # noqa: F401  （仅类型友好）
        client = getattr(request, "client", None)
        ip = getattr(client, "host", "") or "unknown"
    except Exception:                                    # noqa: BLE001
        ip = "unknown"
    ua = ""
    try:
        ua = (request.headers.get("user-agent") or "")[:120]
    except Exception:                                    # noqa: BLE001
        pass
    import hashlib

    raw = f"{ip}|{ua}".encode("utf-8", "replace")
    return "anon:" + hashlib.sha256(raw).hexdigest()[:16]


def merge_thread(explicit: str | None, sid: str) -> str:
    """header 会话优先；旧前端没带 X-Session-Id 时回退 body/query 的 thread_id。

    ★ 回退分支必须用**旧的 thread_id 白名单**（字母数字下划线连字符，见
      agents/image_agent.resolve_thread_id）——旧前端的 "disc"、"小旅_1" 都是
      合法历史键；若误用会话 hex 白名单，老用户的对话历史会全部「失踪」。
    """
    if sid != DEFAULT_SESSION:
        return sid
    t = (explicit or "").strip()
    if not t:
        return DEFAULT_SESSION
    cleaned = "".join(ch for ch in t if ch.isalnum() or ch in "-_")
    return cleaned or DEFAULT_SESSION


# ── 滑动窗口限速（进程内；单实例部署够用）──────────────────────
_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}


def rate_ok(key: str, limit: int = 5, window_sec: float = 60.0) -> bool:
    """允许返回 True 并记账；超限返回 False。key 建议 'ip:动作' 形式。"""
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < window_sec]
        if len(hits) >= limit:
            _rate_hits[key] = hits
            return False
        hits.append(now)
        _rate_hits[key] = hits
        # 防dict无界增长：清理空桶
        if len(_rate_hits) > 4096:
            for k in [k for k, v in _rate_hits.items() if not v]:
                _rate_hits.pop(k, None)
        return True


def client_key(request: Request, action: str) -> str:
    ip = (request.client.host if request.client else "") or "unknown"
    return f"{ip}:{action}"


# ── 访客码 ─────────────────────────────────────────────────────
# 去掉 I/L/O（与 1/0 混淆）与 0/1 本身；6 位 ≈ 30^6 = 7.3 亿组合
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(rf"^[{''.join(_CODE_ALPHABET)}]{{6}}$")


def new_session_id() -> str:
    import uuid as _uuid
    return str(_uuid.uuid4())


def gen_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def _code_ok(code: str | None) -> bool:
    return bool(code) and bool(_CODE_RE.match(code.strip().upper()))


# ── 访客码 / 账号的存取（懒建表，避免与 context_store 的初始化时序耦合）──
_sessions_table_ready = False


def _ensure_tables() -> None:
    """sessions/users 表随首次身份操作建表（幂等）。"""
    global _sessions_table_ready
    if _sessions_table_ready:
        return
    from services import context_store as cs
    with cs._db_lock:
        conn = cs._connect()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                code       TEXT UNIQUE,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                username   TEXT PRIMARY KEY,
                pw_hash    TEXT NOT NULL,
                hint       TEXT,
                session_id TEXT UNIQUE NOT NULL,
                created_at TEXT
            );
            """)
            # ★ 列级迁移：老库是 CREATE TABLE IF NOT EXISTS 建出来的，
            #   它**不会给已存在的表补新列** —— 不补的话下面所有涉及
            #   pw_version 的 SQL 都会报 "no such column"。
            #   报错已存在无所谓，正是幂等想要的结果。
            try:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN pw_version INTEGER DEFAULT 1")
            except Exception:                                 # noqa: BLE001
                pass                                          # 列已存在
            conn.commit()
        finally:
            conn.close()
    _sessions_table_ready = True


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="seconds")


def ensure_session_code(session_id: str) -> str:
    """取该会话的访客码；没有就生成（懒建，幂等）。"""
    _ensure_tables()
    from services import context_store as cs
    with cs._db_lock:
        conn = cs._connect()
        try:
            row = conn.execute(
                "SELECT code FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row and row["code"]:
                return row["code"]
            # 生成唯一码（冲突重试）
            for _ in range(10):
                code = gen_code()
                try:
                    conn.execute(
                        "INSERT INTO sessions(session_id, code, created_at) "
                        "VALUES(?,?,?) "
                        "ON CONFLICT(session_id) DO UPDATE SET code=excluded.code",
                        (session_id, code, _now()),
                    )
                    conn.commit()
                    return code
                except Exception:
                    continue
            raise RuntimeError("访客码生成失败：冲突过多")
        finally:
            conn.close()


def restore_by_code(code: str) -> str | None:
    """访客码 → 会话 ID。码无效返回 None（调用方负责限速与提示）。"""
    if not _code_ok(code):
        return None
    _ensure_tables()
    from services import context_store as cs
    with cs._db_lock:
        conn = cs._connect()
        try:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE code=?",
                (code.strip().upper(),),
            ).fetchone()
            return row["session_id"] if row else None
        finally:
            conn.close()


# ── 账号：pbkdf2 哈希（标准库，无新依赖）───────────────────────
_PBKDF2_ITER = int(os.getenv("AUTH_PBKDF2_ITER", "120000"))
# 中文用户名允许 2 字起（「小旅」是自然称呼）——{2,20}
_USERNAME_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fa5]{2,20}$")


def username_ok(name: str | None) -> bool:
    return bool(name) and bool(_USERNAME_RE.match(name.strip()))


def _hash_pw(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return f"pbkdf2:{_PBKDF2_ITER}:{salt.hex()}:{dk.hex()}"


def _verify_pw(password: str, stored: str) -> bool:
    try:
        kind, iters, salt_hex, hash_hex = stored.split(":")
        if kind != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def bind_account(session_id: str, username: str,
                 password: str, hint: str = "") -> tuple[bool, str]:
    """把当前会话绑定到新账号。成功返回 (True, '')，失败返回 (False, 原因)。"""
    name = (username or "").strip()
    if not username_ok(name):
        # 2 位起：中文用户名里"小旅"这类两字称呼是自然的（正则也是 {2,20}）
        return False, "用户名需要 2–20 位字母、数字、下划线或中文"
    if len(password or "") < 6:
        return False, "密码至少 6 位"
    _ensure_tables()
    from services import context_store as cs
    with cs._db_lock:
        conn = cs._connect()
        try:
            dup = conn.execute(
                "SELECT 1 FROM users WHERE username=?", (name,)).fetchone()
            if dup:
                return False, "这个用户名已被使用，换一个试试"
            conn.execute(
                "INSERT INTO users(username, pw_hash, hint, session_id, created_at) "
                "VALUES(?,?,?,?,?)",
                (name, _hash_pw(password), (hint or "").strip()[:100],
                 session_id, _now()),
            )
            conn.execute(
                "INSERT INTO sessions(session_id, code, created_at) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO NOTHING",
                (session_id, gen_code(), _now()),
            )
            conn.commit()
            audit("account_bound", session_id=session_id, username=name)
            return True, ""
        finally:
            conn.close()


def verify_login(username: str, password: str) -> str | None:
    """账密 → 绑定的会话 ID。失败返回 None。"""
    name = (username or "").strip()
    _ensure_tables()
    from services import context_store as cs
    with cs._db_lock:
        conn = cs._connect()
        try:
            row = conn.execute(
                "SELECT pw_hash, session_id FROM users WHERE username=?",
                (name,)).fetchone()
            if not row or not _verify_pw(password or "", row["pw_hash"]):
                audit("login_failed", username=name[:40])
                return None
            audit("login_ok", username=name[:40], session_id=row["session_id"])
            return row["session_id"]
        finally:
            conn.close()


# ── 密码版本号：让「重置密码」真的能挡住人 ──────────────────────
#
# ★ 为什么必须有它（2026-10-06）：登录令牌 HMAC 签的内容是 sid + 有效期，
#   **里面没有任何密码信息**。管理员替用户重置密码后，盗走令牌的人手上的凭证
#   在 180 天有效期内照用不误 —— 那样「重置密码」形同虚设，
#   只是在数据库里换了个哈希而已。
#   版本号进签名之后：重置一次 +1，旧令牌当场验签失败。
#
# ★ 兼容怎么做的：旧令牌是三段 `sid.exp.sig`，隐含 ver=1。未被重置过的账号
#   ver 就是 1，旧令牌照用（不会把所有人踢下线）；一旦重置过（ver≥2），
#   旧格式令牌立即失效 —— 正好是想要的结果。

_PW_VER_CACHE: dict[str, int] = {}


def _pw_version(session_id: str) -> int:
    """会话所属账号的密码版本号；未绑定账号恒为 1

    进程内缓存避免每次验签都查库 —— 和 rate_limit 一个口径：本项目是单实例部署，
    多实例场景下缓存会各自为政（那时应改为从共享存储读）。
    """
    cached = _PW_VER_CACHE.get(session_id)
    if cached is not None:
        return cached
    ver = 1
    try:
        _ensure_tables()
        from services import context_store as cs
        with cs._db_lock:
            conn = cs._connect()
            try:
                row = conn.execute(
                    "SELECT pw_version FROM users WHERE session_id=?",
                    (session_id,)).fetchone()
                if row and row["pw_version"]:
                    ver = int(row["pw_version"])
            finally:
                conn.close()
    except Exception as e:                                    # noqa: BLE001
        # 库忙 / 表还没准备好：按 1 处理，令牌照签发 —— 读不到版本号不该让用户登不进去
        logger.warning("读取密码版本号失败（按 1 处理）：%s", type(e).__name__)
    if len(_PW_VER_CACHE) > 4096:
        _PW_VER_CACHE.clear()
    _PW_VER_CACHE[session_id] = ver
    return ver


def admin_list_users(limit: int = 200) -> list[dict]:
    """列出全部账号（脱敏，不含哈希）—— 管理员找回某个账号的检索入口"""
    _ensure_tables()
    from services import context_store as cs
    with cs._db_lock:
        conn = cs._connect()
        try:
            rows = conn.execute(
                "SELECT username, hint, session_id, created_at FROM users "
                "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def admin_reset_password(username: str, new_password: str) -> tuple[bool, str]:
    """管理员强制重置某个账号的密码。成功返回 (True, '')，失败返回 (False, 原因)。

    ★ 抬版本号这一步不能省：新哈希只拦得住「重新输密码」的人，
      真正需要挡的是**手里已经攥着有效令牌**的那个人，而令牌不看密码。
    """
    name = (username or "").strip()
    if len(new_password or "") < 6:
        return False, "新密码至少 6 位"
    _ensure_tables()
    from services import context_store as cs
    with cs._db_lock:
        conn = cs._connect()
        try:
            row = conn.execute(
                "SELECT session_id, pw_version FROM users WHERE username=?",
                (name,)).fetchone()
            if not row:
                return False, "没有这个用户名，请核对后再试"
            conn.execute(
                "UPDATE users SET pw_hash=?, pw_version=? WHERE username=?",
                (_hash_pw(new_password), int(row["pw_version"] or 1) + 1, name),
            )
            conn.commit()
            # 让下一次查询读到新版本号 → 旧令牌验签失败
            _PW_VER_CACHE.pop(row["session_id"], None)
            audit("password_reset_by_admin", username=name[:40],
                  session_id=row["session_id"])
            return True, ""
        finally:
            conn.close()


def account_of(session_id: str) -> dict | None:
    """会话 → 账号信息（脱敏，不含哈希）。未绑定返回 None。"""
    _ensure_tables()
    from services import context_store as cs
    with cs._db_lock:
        conn = cs._connect()
        try:
            row = conn.execute(
                "SELECT username, hint, created_at FROM users WHERE session_id=?",
                (session_id,)).fetchone()
            if not row:
                return None
            return {"username": row["username"], "hint": row["hint"],
                    "bound_at": row["created_at"]}
        finally:
            conn.close()


# ── 登录令牌：HMAC 签名的 session_id.exp ──────────────────────
_SECRET_FILE = None
_secret_cache: bytes | None = None


def _secret() -> bytes:
    """令牌签名密钥：首次生成后落盘（storage/ 下，不进代码库）。"""
    global _secret_cache, _SECRET_FILE
    if _secret_cache:
        return _secret_cache
    from config import STORAGE_DIR
    _SECRET_FILE = STORAGE_DIR / "token_secret.key"
    if _SECRET_FILE.exists():
        _secret_cache = _SECRET_FILE.read_bytes().strip()
        if len(_secret_cache) >= 32:
            return _secret_cache
    _secret_cache = secrets.token_bytes(48)
    _SECRET_FILE.write_bytes(_secret_cache)
    try:
        os.chmod(_SECRET_FILE, 0o600)
    except OSError:
        pass
    logger.info("已生成本轮部署的登录令牌签名密钥：%s", _SECRET_FILE)
    return _secret_cache


_TOKEN_TTL_SEC = int(os.getenv("AUTH_TOKEN_TTL_SEC", str(180 * 24 * 3600)))  # 180 天

# 公开别名：会话 Cookie 的有效期跟着令牌走，两者必须同生同死 ——
# Cookie 比令牌活得久会出现「令牌过期后仍能凭 Cookie 取图」的时间窗。
TOKEN_TTL_SEC = _TOKEN_TTL_SEC


def issue_token(session_id: str) -> str:
    """签发令牌 `sid.ver.exp.sig` —— ver 是密码版本号（见 _pw_version 的注释）"""
    exp = int(time.time()) + _TOKEN_TTL_SEC
    ver = _pw_version(session_id)
    body = f"{session_id}.{ver}.{exp}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token: str) -> str | None:
    """令牌 → 会话 ID。无效/过期/**密码已重置**返回 None（调用方静默降级为访客态）"""
    try:
        parts = token.split(".")
        if len(parts) == 3:
            # 旧格式 sid.exp.sig：隐含 ver=1，签名体里也不带 ver
            sid, exp, sig = parts
            body = f"{sid}.{exp}"
            if _pw_version(sid) != 1:
                return None      # ★ 这个账号改过密码 → 旧令牌作废
        elif len(parts) == 4:
            sid, ver_s, exp, sig = parts
            ver = int(ver_s)
            if ver != _pw_version(sid):
                return None      # ★ 同上，针对新格式令牌
            body = f"{sid}.{ver}.{exp}"
        else:
            return None
        if not sid or not exp or not sig:
            return None
        if int(exp) < time.time():
            return None
        expect = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        return _clean_session(sid)
    except Exception:
        return None
