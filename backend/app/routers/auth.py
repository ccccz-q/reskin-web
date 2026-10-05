"""身份接口 · 访客码 / 绑定账号 / 登录 —— 公开版的全部鉴权入口

端点一览（全部挂 /api/auth 与 /api/session）：

- GET  /api/auth/me                当前会话状态（账号 / 访客码）
- POST /api/auth/bind              访客态设用户名+密码绑定当前会话
- POST /api/auth/login             登录 → 签名令牌（令牌即会话凭证）
- POST /api/session/restore        凭 6 位访客码找回会话
- GET  /api/auth/whoami/<sid>      （内部调试用，仅本地回环可见——暂不暴露）

限速：登录 / 恢复 / 绑定均为每 IP 每分钟 5 次——公开版防猜码与撞库。
"""
from __future__ import annotations

import sys
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.logging import audit                                       # noqa: E402
from services import identity as ident                                # noqa: E402

router = APIRouter(prefix="/api", tags=["auth"])


class BindRequest(BaseModel):
    username: str = Field(..., max_length=40)
    password: str = Field(..., max_length=128)
    hint: str = Field("", max_length=100)


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=40)
    password: str = Field(..., max_length=128)


class RestoreRequest(BaseModel):
    code: str = Field(..., max_length=12)


@router.get("/auth/me", summary="当前会话状态：账号信息 + 访客码")
async def me(request: Request, sid: str = Depends(ident.session_dep)) -> dict:
    code = ident.ensure_session_code(sid)
    acct = ident.account_of(sid)
    return {
        "ok": True,
        "session_id": sid,
        "code": code,
        "account": acct,
        "logged_in": bool(acct),
    }


@router.post("/auth/bind", summary="绑定账号：给当前会话设用户名+密码")
async def bind(req: BindRequest, request: Request,
               sid: str = Depends(ident.session_dep)) -> dict:
    if not ident.rate_ok(ident.client_key(request, "bind"), limit=5, window_sec=60):
        raise HTTPException(429, "操作太频繁了，请一分钟后再试")
    if sid == ident.DEFAULT_SESSION:
        raise HTTPException(400, "当前缺少有效会话，请刷新页面后再试")
    ok, why = ident.bind_account(sid, req.username, req.password, req.hint)
    if not ok:
        raise HTTPException(400, why)
    return {"ok": True, "message": "账号已绑定，当前所有作品已归入账号",
            "token": ident.issue_token(sid)}


@router.post("/auth/login", summary="登录：账密换会话令牌")
async def login(req: LoginRequest, request: Request) -> dict:
    if not ident.rate_ok(ident.client_key(request, "login"), limit=5, window_sec=60):
        raise HTTPException(429, "尝试太频繁了，请一分钟后再试")
    sid = ident.verify_login(req.username, req.password)
    if not sid:
        raise HTTPException(401, "用户名或密码不对，再试一次或找回访客码")
    return {"ok": True, "session_id": sid, "token": ident.issue_token(sid)}


@router.post("/session/restore", summary="凭访客码找回会话")
async def restore(req: RestoreRequest, request: Request) -> dict:
    if not ident.rate_ok(ident.client_key(request, "restore"), limit=5, window_sec=60):
        raise HTTPException(429, "尝试太频繁了，请一分钟后再试")
    sid = ident.restore_by_code(req.code)
    if not sid:
        raise HTTPException(404, "访客码没有对应到任何记录，请核对后重试")
    audit("session_restored", session_id=sid)
    return {"ok": True, "session_id": sid, "token": ident.issue_token(sid)}
