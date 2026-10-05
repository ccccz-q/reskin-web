"""基础设施层 · 访问控制 —— 挡浏览器 drive-by，可选令牌强校验

════════ 要防的是什么（审查发现 P1-5）════════

本项目**没有任何鉴权**。CORS 白名单能阻止恶意网页**读取**响应，
但完全阻止不了它**发出**请求 —— 这不直观但很关键：

    <form id=f action="http://127.0.0.1:8000/api/chat/reset-quota?thread_id=default"
          method="POST" enctype="text/plain"><input name=x value=1></form>
    <script>f.submit()</script>

`POST` + `text/plain` 属于 CORS「简单请求」，**不触发预检**。
浏览器会照发，白名单只让 JS 读不到响应 —— 但服务端已经执行了。
后果：`MAX_GENERATIONS_PER_SESSION` 这个「花了多少钱的最后一道闸」
在任何人手里都是无穷大。

════════ 两道闸，各挡一类威胁 ════════

1. **Origin 白名单（默认开启）**：写操作若带 `Origin` 且不在白名单 → 403。
   - 浏览器跨源请求**一定**带 Origin → 恶意网页被挡
   - curl / 本地脚本不带 Origin → 不受影响（本地进程本来就有全部权限）

2. **本地令牌（可选，配了才生效）**：写操作必须带 `X-Local-Token`。
   适用于「要把后端暴露到局域网演示」的场景 —— 那种情况下
   Origin 校验挡不住一个会写 curl 的人，需要真令牌。

这个分层是刻意的：默认配置就能挡住现实中最容易发生的攻击（恶意网页），
而需要更强保护时有一个明确的开关，不用改代码。
"""
from __future__ import annotations

import os
import sys

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CORS_ORIGINS                              # noqa: E402
from infra.logging import audit, logger                          # noqa: E402

# ★ 令牌与白名单一律**运行时读 config**，不要在 import 时快照成模块常量。
#   踩过的坑：写成 `from config import LOCAL_TOKEN` 再在函数里用它，
#   值就被冻结在 import 那一刻 —— 测试里改环境变量再 reload 主模块也不生效，
#   表现为「配了令牌却依然不校验」这种极难查的不一致。
#   （CORS_ORIGINS 上面仍按值导入，因为中间件在 main.py 里构造时就要用，
#     且它不会在运行期变 —— 两处的差异是有意的。）

TOKEN_HEADER = "X-Local-Token"

# 这些方法会改变世界（花钱 / 写库 / 清历史），必须过闸
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 少数端点即使不带 Origin 也应放行：健康检查是只读的，但万一有人用 POST 探活…
# 目前没有这种端点，所以不放行任何例外。
EXEMPT_PATHS: frozenset[str] = frozenset()


def _origin_allowed(origin: str | None, request=None) -> tuple[bool, str]:
    """没带 Origin 视为非浏览器请求 → 放行（本地脚本不该被拦）"""
    if not origin:
        return True, "no-origin"
    # 归一：去尾斜杠 + 小写。浏览器不会带尾斜杠，但手写 curl / 某些网关会，
    # 而白名单是精确匹配 —— 归一之后既不会误放也不会误拦。
    normalized = origin.strip().rstrip("/").lower()
    allowed = {o.strip().rstrip("/").lower() for o in CORS_ORIGINS}
    if normalized in allowed:
        return True, "whitelisted"
    # ★ 同源豁免（S5 验收实测发现的发布形态阻断）：单端口托管后，页面与 API
    #   同源，但浏览器的 POST/PUT/DELETE **也带 Origin**（如 http://127.0.0.1:8002
    #   或发布域名）—— 不在 dev 白名单里，写操作全被 403。
    #   判据：Origin 的 host[:port] 与请求 Host 头一致 → 请求就是打到本服务的
    #   页面发出的，天然可信。只比 host 部分、不比 scheme —— 反代 HTTPS 终结
    #   到 HTTP 时 scheme 对不上，host 一致即同源（反代本身是可信层）。
    if request is not None:
        req_host = (request.headers.get("host") or "").strip().lower()
        origin_host = normalized.split("://", 1)[-1]
        if req_host and origin_host == req_host:
            return True, "same-origin"
    # ★ PUBLIC_MODE 兜底（S6 线上实测发现的反代阻断）：
    #   WorkBuddy sites 这类托管是「反代 HTTPS → 内网 HTTP」：
    #   浏览器的 Origin 是外网域名，而进程收到的 Host 是反代转发时的**内部地址**，
    #   两者永远对不上 —— 上面这条同源判定在云上恒不成立，写操作全线 403。
    #   为什么此时可以放行、而不是引入安全隐患：
    #     ① 会话身份凭证是**自定义头** X-Session-Id / X-Auth-Token，**不是 Cookie**。
    #        浏览器不会自动附加自定义头；跨站 JS 要设它必然触发 CORS 预检，
    #        而白名单里没有攻击者域名 → 浏览器直接拦下。CSRF 的前提本就不成立。
    #     ② Origin 白名单真正保护的场景是「本机服务不被局域网里的别的网页滥用」。
    #        公开发布模式下服务本来就是对全网开放的，不存在这层内网边界。
    #     ③ 本地（PUBLIC_MODE=0）行为一字未改，白名单 + 同源判定照旧生效。
    #   运行时读 config —— 与令牌同一套路，便于测试里动态开关。
    import config as _cfg
    if getattr(_cfg, "PUBLIC_MODE", False):
        return True, "public-mode"
    return False, "origin-not-allowed"


def _single_origin(request) -> tuple[str | None, bool]:
    """取出唯一的 Origin；返回 (值, 是否重复)

    ★ 必须检测重复头（审查发现 P1-13）
    实测：同时发 `Origin: http://localhost:5173` 和 `Origin: https://evil.com`
    时，`headers.get("origin")` 只返回**第一个** → 校验通过。
    浏览器不会这么发，但经过代理 / 网关 / 自定义客户端就可能。
    协议上 Origin 是单值头，出现多个就该直接拒绝，而不是挑一个信。
    """
    values = request.headers.getlist("origin")
    if len(values) > 1:
        return values[0], True
    return (values[0] if values else None), False


def _token_ok(token: str | None) -> tuple[bool, str]:
    """未配置 LOCAL_TOKEN 时不做校验（默认只监听本机，风险面已被 BACKEND_HOST 收住）

    ★ 运行时读 `config.LOCAL_TOKEN`，不用模块级快照 —— 见文件头注释。
    """
    import config as _cfg

    expected = _cfg.LOCAL_TOKEN
    if not expected:
        return True, "token-not-configured"
    if token and token == expected:
        return True, "token-ok"
    return False, "token-invalid"


class LocalAccessMiddleware(BaseHTTPMiddleware):
    """写操作访问控制 —— Origin 白名单（默认）+ 本地令牌（可选）"""

    async def dispatch(self, request, call_next):
        path = request.url.path
        method = request.method.upper()

        if method in STATE_CHANGING and path not in EXEMPT_PATHS:
            origin, duplicated = _single_origin(request)
            if duplicated:
                logger.warning("拒绝写操作 %s %s：Origin 头重复", method, path)
                audit("access_denied", reason="duplicate-origin", path=path,
                      method=method)
                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "code": "origin_denied",
                        "error": "Origin 头重复，拒绝该请求。",
                    },
                )

            ok, why = _origin_allowed(origin, request)
            if not ok:
                logger.warning("拒绝写操作 %s %s：Origin=%r 不在白名单",
                               method, path, origin)
                audit("access_denied", reason=why, path=path,
                      origin=origin or "", method=method)
                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "code": "origin_denied",
                        "error": "请求来源不在允许列表内。"
                                 "如需从其它地址访问，请把它加进 .env 的 CORS_ORIGINS。",
                    },
                )

            ok, why = _token_ok(request.headers.get(TOKEN_HEADER))
            if not ok:
                logger.warning("拒绝写操作 %s %s：令牌无效", method, path)
                audit("access_denied", reason=why, path=path, method=method)
                return JSONResponse(
                    status_code=401,
                    content={
                        "ok": False,
                        "code": "token_required",
                        "error": f"缺少或错误的 {TOKEN_HEADER} 请求头。",
                    },
                )

        return await call_next(request)


def security_snapshot() -> dict:
    """给 /api/health 看的访问控制状态（绝不回显令牌本身）"""
    import config as _cfg

    return {
        "origin_allowlist": list(CORS_ORIGINS),
        # PUBLIC_MODE 下写操作不再按 Origin 拦截（理由见 _origin_allowed 的注释），
        # 这里如实说出来 —— health 是给排障看的，说假话比没信息更糟。
        "origin_check_enabled": not getattr(_cfg, "PUBLIC_MODE", False),
        "token_required": bool(_cfg.LOCAL_TOKEN),
        "token_header": TOKEN_HEADER,
        "state_changing_methods": sorted(STATE_CHANGING),
    }
