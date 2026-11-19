# -*- coding: utf-8 -*-
"""统一响应信封：{"ok": bool, "data": ..., "error": {"code","msg"}}。"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def ok(data: Any) -> dict[str, Any]:
    """成功响应；error 必须显式为 None，避免前端出现两套判断。"""
    return {"ok": True, "data": data, "error": None}


def fail(code: str, msg: str, status: int) -> JSONResponse:
    """失败响应；ok=false 时 data 必须为 None（硬约束）。"""
    return JSONResponse(
        status_code=status,
        content={"ok": False, "data": None, "error": {"code": code, "msg": msg}},
    )
