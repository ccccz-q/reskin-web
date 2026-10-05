"""错误分诊测试 · 「第一次必失败」这件事不许再复发

背景（2026-10-04 用户实测）：
    每次会话的**第一次**生成必弹 `BadRequestError: Error code: 400`，原文是中文
    「您的请求无法……」；用户手动再点一次就成功 —— 连续两次都这样。
    随后本机连打两枪：冷请求那一轮 65.8s / **重试 4 次**（502 → 502 → 503），
    第二轮 26.2s / 一次通过。

结论要说清楚：根因不在提示词内容，而在**中转站冷启动时会把"暂时没通道"
包装成 400 + 中文文案**。我们把 400 一律当成「请求写错了」是错的，
结果就是用户的第一次操作永远失败，而且原始英文报文直接糊到界面上。

所以这组测试锁三件事：
    1. 网关喊忙的 400（含中文措辞）→ 必须重试；
    2. 真写错的 400（invalid size / unsupported parameter）→ 立刻失败，不许浪费重试；
    3. 内容策略类 → 早退 + 文案里给出"换个说法"这种可执行建议，
       且**任何情况下原文都不许出现在给用户的字段里**。

★ 全程离线：只喂构造出来的异常对象，不打网络、不花钱。
"""
from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(_root))

import services.image_generator as ig                                 # noqa: E402
import services.llm as llm                                            # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


class _Err(Exception):
    """够用的异常替身：带 status_code，模仿 OpenAI SDK 的 APIStatusError"""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status_code = status


print("\n── 1. 网关「假 400」必须重试（用户报的那种）──")
for label, exc in [
    ("中文「您的请求无法完成」",
     _Err("BadRequestError: Error code: 400 - {'error': {'message': '您的请求无法完成，请稍后再试'}}", 400)),
    ("中文「服务繁忙」",
     _Err("Error code: 400 - {'error': {'message': '服务繁忙，请稍后重试'}}", 400)),
    ("上游未就绪（英语）",
     _Err("Error code: 400 - upstream service is unavailable", 400)),
    ("网关喊忙（英语）",
     _Err("BadRequestError: gateway busy, please retry later", 400)),
    ("无可用通道（这条以前被误判成确定性失败）",
     _Err("Error code: 400 - No available compatible account", 400)),
]:
    check(f"{label} → 重试", ig._is_transient_image_error(exc) is True)

print("\n── 2. 真写错的 400 不许重试（重试=白烧钱+掩盖错误）──")
for label, exc in [
    ("尺寸参数非法", _Err("400 Invalid 'size' value: 999x999", 400)),
    ("不支持的参数", _Err("400 unsupported parameter: 'quality'", 400)),
    ("鉴权失败", _Err("401 Incorrect API key provided", 401)),
    ("模型不存在", _Err("404 The model does not exist", 404)),
]:
    check(f"{label} → 立刻失败", ig._is_transient_image_error(exc) is False)

print("\n── 3. 老口径没被改坏（回归）──")
for label, exc, want in [
    ("503", _Err("No available compatible account", 503), True),
    ("502", _Err("502 Bad Gateway", 502), True),
    ("429", _Err("429 rate limit exceeded", 429), True),
    ("超时", _Err("Request timed out.", None), True),
    ("空 choices", Exception(f"{llm.EMPTY_UPSTREAM}: 上游返回了空的 choices"), True),
]:
    check(f"{label} → {'重试' if want else '立刻失败'}",
          ig._is_transient_image_error(exc) is want)

print("\n── 4. 内容策略类：早退 + 给可执行建议 ──")
pol = _Err("BadRequestError: 400 - Your request was rejected by the content policy", 400)
check("识别为策略拦截", ig._is_policy_image_error(pol) is True)
check("策略类不重试", ig._is_transient_image_error(pol) is False)
msg = ig.friendly_error(pol)
check("文案给出可执行建议", "换个说法" in msg, msg)
check("文案不含原文片段", "content policy" not in msg.lower())

print("\n── 5. 给用户的文案：永远中文、永远不带原文 ──")
raw_cases = [
    ("假 400", "BadRequestError: Error code: 400 - {'error': {'message': '您的请求无法完成'}}"),
    ("策略（异常对象）",
     _Err("BadRequestError: Error code: 400 - rejected by the content policy", 400)),
    # ★ 这一条是生产真实形态：image_generator 传进来的是**字符串**
    ("策略（字符串，生产真实形态）",
     "BadRequestError: Error code: 400 - rejected by the content policy"),
    ("超时", "APITimeoutError: Request timed out."),
    ("鉴权", "AuthenticationError: Error code: 401 - Incorrect API key provided"),
    ("未知错误", "Some weird internal boom"),
]
for label, raw in raw_cases:
    friendly = ig.friendly_error(raw)
    ascii_leak = [tok for tok in ("BadRequestError", "Error code", "Traceback",
                                  "content policy", "API key", "http")
                  if tok.lower() in friendly.lower()]
    check(f"{label} → 中文且不漏原文", not ascii_leak, f"泄漏词={ascii_leak} 文案={friendly[:30]}…")

print("\n── 6. 同一错误，异常与字符串必须给出同一个答案 ──")
same_raw = "Error code: 400 - rejected by the content policy"
check("策略：异常 == 字符串",
      ig.friendly_error(_Err(same_raw, 400)) == ig.friendly_error(same_raw))
busy = "Error code: 400 - 您的请求无法完成，请稍后再试"
check("网关忙：异常 == 字符串",
      ig.friendly_error(_Err(busy, 400)) == ig.friendly_error(busy))

print(f"\n{'=' * 56}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 56}")
sys.exit(1 if FAIL else 0)
