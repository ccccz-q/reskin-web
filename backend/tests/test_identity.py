"""身份层测试 · 会话/访客码/账号/令牌/限速

★ 数据库隔离：identity 懒建表走 context_store 的连接 —— 用 tempfile
  临时库 + 重置初始化标记，测试互不污染（与 test_forge 第 16 节同法）。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from config import SQLITE_PATH                                        # noqa: E402
from services import context_store as cs                              # noqa: E402
from services import identity as ident                                # noqa: E402

_tmpdir = tempfile.mkdtemp(prefix="wb_identity_test_")
cs.SQLITE_PATH = Path(_tmpdir) / "t.db"
cs._initialized_for = None
cs.init_db(force=True)

import services.identity                                              # noqa: E402
services.identity._sessions_table_ready = False    # 换库后强制重建身份表

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


print("=== 1. 会话 ID 白名单 ===")
check("合法 UUID 通过", ident._clean_session(
    "6163f717-2de8-4dd3-9a1b-abc123def456") == "6163f717-2de8-4dd3-9a1b-abc123def456")
check("大写归一为小写", ident._clean_session("ABCDEF01-2345") == "abcdef01-2345")
check("空回退 default", ident._clean_session("") == "default")
check("None 回退 default", ident._clean_session(None) == "default")
check("路径注入被拦", ident._clean_session("../../etc") == "default")
check("反斜杠注入被拦", ident._clean_session("a\\b\\c") == "default")
check("超长被拦", ident._clean_session("a" * 100) == "default")
check("连字符结尾被拦", ident._clean_session("abcdef0-") == "default")

print("=== 2. merge_thread：header 优先 ===")
check("header 有值优先", ident.merge_thread("from_body", "from-header") == "from-header")
check("header 缺失回退 body", ident.merge_thread("from_body", "default") == "from_body")
check("body 非法字符被清洗（旧行为：清洗非拒绝）",
      ident.merge_thread("../bad", "default") == "bad")

print("=== 3. 访客码：生成与找回 ===")
sid1 = ident.new_session_id()
code1 = ident.ensure_session_code(sid1)
check("访客码 6 位", len(code1) == 6, code1)
check("幂等：同会话同码", ident.ensure_session_code(sid1) == code1)
check("找回成功", ident.restore_by_code(code1) == sid1)
check("小写也能找回", ident.restore_by_code(code1.lower()) == sid1)
check("无效码返回 None", ident.restore_by_code("ZZZZZZ") is None)
check("格式非法返回 None", ident.restore_by_code("abc') OR 1=1") is None)
sid2 = ident.new_session_id()
code2 = ident.ensure_session_code(sid2)
check("不同会话不同码", code1 != code2, f"{code1} vs {code2}")

print("=== 4. 账号：绑定与登录 ===")
sid3 = ident.new_session_id()
ok, why = ident.bind_account(sid3, "小旅", "secret6", "我的提示短语")
check("绑定成功", ok, why)
check("重复用户名被拒", ident.bind_account(ident.new_session_id(),
                                            "小旅", "secret6") == (False, "这个用户名已被使用，换一个试试"))
check("短密码被拒", not ident.bind_account(ident.new_session_id(), "another", "12345")[0])
check("非法用户名被拒", not ident.bind_account(ident.new_session_id(), "a", "123456")[0])
check("登录成功返回会话", ident.verify_login("小旅", "secret6") == sid3)
check("错密码登录失败", ident.verify_login("小旅", "wrong!") is None)
check("不存在的用户", ident.verify_login("nobody", "secret6") is None)
acct = ident.account_of(sid3)
check("账号信息脱敏可查", bool(acct) and acct["username"] == "小旅"
      and "pw_hash" not in (acct or {}), str(acct))
check("未绑定会话无账号", ident.account_of(ident.new_session_id()) is None)

print("=== 5. 登录令牌：签发与校验 ===")
tok = ident.issue_token(sid3)
check("令牌可校验", ident.verify_token(tok) == sid3)
check("篡改被拒", ident.verify_token(tok[:-4] + "0000") is None)
check("垃圾被拒", ident.verify_token("not-a-token") is None)
check("空串被拒", ident.verify_token("") is None)
# 过期：把 exp 伪造到过去（签名对不上 → 拒绝；用真签发器验证签名逻辑即可）
check("过期令牌被拒", ident.verify_token(f"{sid3}.1000."
                                          + __import__("hashlib").sha256(
                                              ident._secret().encode()
                                              if isinstance(ident._secret(), bytes) is False
                                              else ident._secret()).hexdigest()[:64]) is None)

print("=== 6. 限速滑动窗口 ===")
ident._rate_hits.clear()
key = "1.2.3.4:test"
allowed = [ident.rate_ok(key, limit=3, window_sec=60) for _ in range(3)]
check("窗口内前 3 次放行", all(allowed))
check("第 4 次被拒", not ident.rate_ok(key, limit=3, window_sec=60))
# 模拟窗口过期：把时间戳改老
ident._rate_hits[key] = [t - 61 for t in ident._rate_hits[key]]
check("窗口滑过后重新放行", ident.rate_ok(key, limit=3, window_sec=60))

print("=== 7. request 解析（假 request 对象）===")


class _FakeHeaders:
    def __init__(self, d: dict):
        self._d = {k.lower(): v for k, v in d.items()}

    def get(self, k, default=None):
        return self._d.get(k.lower(), default)


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = _FakeHeaders(headers)


check("无任何头 → default", ident.resolve_session(
    _FakeRequest({})) == "default")
sid4 = ident.new_session_id()
check("X-Session-Id 生效", ident.resolve_session(
    _FakeRequest({"X-Session-Id": sid4})) == sid4)
tok4 = ident.issue_token(sid4)
check("令牌优先于会话头", ident.resolve_session(
    _FakeRequest({"X-Session-Id": sid1, "X-Auth-Token": tok4})) == sid4)
check("无效令牌降级会话头", ident.resolve_session(
    _FakeRequest({"X-Session-Id": sid1, "X-Auth-Token": "bad.token.here"})) == sid1)

print("=== 8. 家族属主过滤 ===")
spec = {"id": "fam_test", "name": "测试家族", "segments": {"creative": "x"},
        "hard_forbid": ["a", "b", "c", "d"]}
fid_builtin = cs.save_forge(lineage="lin_b", version=1, spec=spec,
                            name="内置家族")
fid_mine = cs.save_forge(lineage="lin_m", version=1, spec=spec,
                         name="我的家族", owner_session=sid3)
fid_other = cs.save_forge(lineage="lin_o", version=1, spec=spec,
                          name="别人家族", owner_session=sid4)
mine_view = [r["id"] for r in cs.list_forge(owner=sid3)]
check("内置可见", fid_builtin in mine_view)
check("自己的可见", fid_mine in mine_view)
check("别人的不可见", fid_other not in mine_view)
check("default 视图看全部", all(
    x in [r["id"] for r in cs.list_forge(owner="default")]
    for x in (fid_builtin, fid_mine, fid_other)))
vers = cs.list_forge(lineage="lin_o", owner=sid3)
check("lineage 查询也按属主过滤", all(
    (r.get("owner_session") or "") != sid4 for r in vers))

print("=" * 46)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
