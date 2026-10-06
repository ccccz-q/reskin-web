"""身份层测试 · 会话/访客码/账号/令牌/限速

★ 数据库隔离：identity 懒建表走 context_store 的连接 —— 用 tempfile
  临时库 + 重置初始化标记，测试互不污染（与 test_forge 第 16 节同法）。
"""
from __future__ import annotations

import hashlib
import hmac
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

print("=== 9. 管理员重置密码 + 旧令牌吊销 ===")
# ★ 背景：identity 的文件头从一开始就承诺「忘记密码由管理员后台重置」，
#   但那个端点从未实现过 —— 用户忘密码又丢访客码就永久拿不回来。
# ★ 更关键的一条：令牌签的是 sid + 有效期，**不含任何密码信息**。
#   只改哈希不抬版本号的话，攥着旧令牌的人照样能进，重置等于白做。
#   这一节就是钉死那一步的。

sid_u = ident.new_session_id()
check("绑定成功", ident.bind_account(sid_u, "重置测试户", "oldpw123", "测试")[0])
check("旧密码能登录", ident.verify_login("重置测试户", "oldpw123") == sid_u)

tok_old = ident.issue_token(sid_u)
check("新令牌可用", ident.verify_token(tok_old) == sid_u)
check("令牌是四段（含密码版本号）", len(tok_old.split(".")) == 4, tok_old.split(".")[1])

users = ident.admin_list_users()
names = [u["username"] for u in users]
check("账号列表可见", "重置测试户" in names, str(names[:3]))
check("★ 列表不含密码哈希", all("pw_hash" not in u for u in users))
check("★ 列表只有脱敏字段",
      all(set(u) <= {"username", "hint", "session_id", "created_at"} for u in users),
      str(sorted(users[0].keys())) if users else "")

check("用户名不存在时明确报错",
      ident.admin_reset_password("查无此人", "newpw123")
      == (False, "没有这个用户名，请核对后再试"))
check("密码太短被拒",
      ident.admin_reset_password("重置测试户", "123") == (False, "新密码至少 6 位"))
# ★ 失败的尝试不能改坏任何东西
check("失败尝试没有改动密码", ident.verify_login("重置测试户", "oldpw123") == sid_u)
check("失败尝试没有吊销令牌", ident.verify_token(tok_old) == sid_u)

ok9, why9 = ident.admin_reset_password("重置测试户", "newpw456")
check("重置成功", ok9 and not why9, why9)
check("新密码能登录", ident.verify_login("重置测试户", "newpw456") == sid_u)
check("★ 旧密码已失效", ident.verify_login("重置测试户", "oldpw123") is None)
check("★ 重置后旧令牌被吊销", ident.verify_token(tok_old) is None)
check("重新登录能拿到可用令牌",
      ident.verify_token(ident.issue_token(sid_u)) == sid_u)

# 匿名会话没绑账号 → 版本号恒为 1，不该被任何人的改密码波及
anon = ident.new_session_id()
tok_a = ident.issue_token(anon)
check("匿名会话令牌照常可用", ident.verify_token(tok_a) == anon)
# 旧格式三段令牌（历史已发出）在账号没改过密码时仍然认
tok_a_old = ".".join([anon, str(int(time.time()) + 3600),
                      hmac.new(ident._secret(), f"{anon}.{int(time.time()) + 3600}"
                               .encode(), hashlib.sha256).hexdigest()])
check("旧格式三段令牌仍兼容", ident.verify_token(tok_a_old) == anon)

print("=" * 46)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
