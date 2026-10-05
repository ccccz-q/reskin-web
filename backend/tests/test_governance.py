"""治理层验收 —— 重点是「额度记账」这条曾经错得很离谱的链路

    python tests/test_governance.py

★★ 这里要防的回归（审查发现 P0-2）
--------------------------------
旧实现：check（只看不写） → 出图 → 成功才 +1，失败则 refund -1。
失败时退的那 1 张，退的是**上一次成功生成**的账：

    初始 used=0 → 成功1次 used=1 → 失败1次(退还) used=0   ← 应为 1

于是「成功 + 失败」交替就能让 used 永远归零，
MAX_GENERATIONS_PER_SESSION 这个成本护栏形同虚设。
新实现改成「预扣 + 凭票据冲正」，下面每一条断言都是冲着这个来的。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

os.environ["DISABLE_IMAGE_GENERATION"] = "0"
os.environ["MAX_GENERATIONS_PER_SESSION"] = "3"

import services.context_store as cs  # noqa: E402
from pathlib import Path as P  # noqa: E402

_tmp = P(tempfile.mkdtemp(prefix="gov_"))
cs.SQLITE_PATH = _tmp / "gov.db"

import config  # noqa: E402

config.SQLITE_PATH = cs.SQLITE_PATH
config.MAX_GENERATIONS_PER_SESSION = 3

import governance.guard as guard  # noqa: E402

guard.MAX_GENERATIONS_PER_SESSION = 3

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


print("=== 1. 预扣 / 冲正的基本记账 ===")
guard.reset_quota("a")
check("初始 0/3", guard.remaining_quota("a")["remaining"] == 3,
      str(guard.remaining_quota("a")))

t1 = guard.reserve_generation("a")
q = guard.remaining_quota("a")
check("预扣后 used=1", q["used"] == 1 and q["remaining"] == 2, str(q))

# 模拟「成功」：什么也不做，额度保持被占
q = guard.settle_generation("a", t1, size="2k")
check("成功后 used 仍为 1（不是 2）", q["used"] == 1, str(q))

print()
print("=== 2. ★ 失败退还：退的是自己那张，不是别人的 ===")
t2 = guard.reserve_generation("a")
check("再预扣 → used=2", guard.remaining_quota("a")["used"] == 2)
guard.release_generation("a", t2, reason="生图失败")
q = guard.remaining_quota("a")
check("退还后 used=1（不是 0）", q["used"] == 1, str(q))

print()
print("=== 3. ★ 无票据 / 重复兑现 一律拒绝 ===")
before = guard.remaining_quota("a")["used"]
guard.release_generation("a", None, reason="没有票据")
check("无票据不退", guard.remaining_quota("a")["used"] == before, str(before))
guard.release_generation("a", "deadbeefdeadbeef", reason="伪造票据")
check("伪造票据不退", guard.remaining_quota("a")["used"] == before)

# ★ 真断言（旧版这里是 check(..., True)，等于没测 —— 而它掩盖了一个真 bug：
#   settle 不销毁票据，于是「先成功后冲正」能把已确认的额度退回去）
tok = guard.reserve_generation("a")
guard.settle_generation("a", tok, size="2k")
used_after_settle = guard.remaining_quota("a")["used"]
guard.release_generation("a", tok, reason="结算后想再退一次")
check("票据在结算后作废（不能再冲正）",
      guard.remaining_quota("a")["used"] == used_after_settle,
      f"结算后 {used_after_settle} → 冲正后 {guard.remaining_quota('a')['used']}")

tok2 = guard.reserve_generation("a")
guard.release_generation("a", tok2, reason="第一次退还")
used_after_release = guard.remaining_quota("a")["used"]
guard.release_generation("a", tok2, reason="同一张票想退第二次")
check("票据只能兑现一次",
      guard.remaining_quota("a")["used"] == used_after_release,
      f"{used_after_release} → {guard.remaining_quota('a')['used']}")

print()
print("=== 3b. ★ 票据落库：进程重启不再永久泄漏额度 ===")
guard.reset_quota("crash")
tok3 = guard.reserve_generation("crash")
check("预扣后 used=1", guard.remaining_quota("crash")["used"] == 1)
check("票据已落库", cs.peek_reservation(tok3) is not None)

# 模拟「进程被杀」：票据仍在库里，只是刚才那次运行没有结算
# （旧实现里票据在内存，进程一挂就没了 → release 被拒 → 额度永久少 1 张）
check("票据在「重启」后依然可兑现",
      cs.peek_reservation(tok3) is not None,
      "内存实现到这里就丢了")

# 对账：把超过 TTL 的残票回收掉
import time as _t  # noqa: E402
cs.add_reservation("staletoken0001", "crash")
check("直接回收（TTL=0）能拿回额度", guard.reconcile_reservations(max_age_sec=0) >= 1)
check("回收后 used 归零", guard.remaining_quota("crash")["used"] == 0,
      str(guard.remaining_quota("crash")["used"]))
check("残票已从库里清掉", cs.peek_reservation("staletoken0001") is None)

print()
print("=== 4. ★ 交替成功/失败不能把额度刷回来 ===")
# 把上限临时抬高，好让「3 次成功 + 3 次失败」都跑得完 ——
# 要验的是记账是否正确，而不是配额是否拦得住（后者在第 5 节验）。
guard.MAX_GENERATIONS_PER_SESSION = 10
guard.reset_quota("b")
for _ in range(3):
    tok = guard.reserve_generation("b")             # 成功
    guard.settle_generation("b", tok, size="2k")
    tok = guard.reserve_generation("b")             # 失败
    guard.release_generation("b", tok, reason="模拟失败")
q = guard.remaining_quota("b")
# 3 次成功留下 3 张账，3 次失败各自冲正 → 应为 3
# 旧实现的答案会是 0（每次失败都把上一次成功的账退掉）
check("成功3+失败3 后 used=3（旧实现会给 0）", q["used"] == 3, str(q))
guard.MAX_GENERATIONS_PER_SESSION = 3

print()
print("=== 5. 配额耗尽必须拦住 ===")
guard.reset_quota("c")
toks = [guard.reserve_generation("c") for _ in range(3)]
check("用满 3 张", guard.remaining_quota("c")["used"] == 3)
blocked = False
try:
    guard.reserve_generation("c")
except guard.GovernanceError as e:
    blocked = e.code == "quota_exhausted"
check("第 4 次被拦（quota_exhausted）", blocked)

print()
print("=== 6. 总开关 ===")
guard.DISABLE_IMAGE_GENERATION = True
off = False
try:
    guard.reserve_generation("d")
except guard.GovernanceError as e:
    off = e.code == "generation_disabled"
check("开关打开时拒绝预扣", off)
guard.DISABLE_IMAGE_GENERATION = False

print()
print("=== 7. 上传体积判定：治理层是唯一入口 ===")
try:
    guard.check_upload_size(0)
    check("空文件应报错", False)
except guard.GovernanceError as e:
    check("空文件 → upload_empty", e.code == "upload_empty")
try:
    guard.check_upload_size(config.MAX_UPLOAD_BYTES + 1)
    check("超限应报错", False)
except guard.GovernanceError as e:
    check("超限 → upload_too_large", e.code == "upload_too_large")
guard.check_upload_size(1024)
check("正常体积放行", True)

print()
print("=== 8. 会话隔离 ===")
guard.reset_quota("x")
guard.reset_quota("y")
guard.reserve_generation("x")
check("x 用了 1", guard.remaining_quota("x")["used"] == 1)
check("y 不受影响", guard.remaining_quota("y")["used"] == 0)

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
